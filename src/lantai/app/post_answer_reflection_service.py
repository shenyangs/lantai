# -*- coding: utf-8 -*-
"""Durable, non-blocking post-answer reflection delivery."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, uuid5

from ..agents.persona.post_answer_reflection import (
    PostAnswerReflection,
    PostAnswerReflectionRequest,
    build_post_answer_reflection_prompt,
    parse_post_answer_reflection,
)
from ..agents.persona.subject_continuity import SubjectContinuityStore
from ..config import load_config
from ..runtime.messages import RuntimeMessage
from ..security.storage_keys import write_text_atomic_no_follow
from .experience.events import (
    RuntimeEventSequenceAppendError,
    append_chat_event,
)
from .utility_model import (
    ResolvedUtilityModel,
    consume_model_response,
    resolve_active_model,
    resolve_utility_model,
)
from .background_semantics import PrimaryModelPreempted, run_primary_when_idle

logger = logging.getLogger(__name__)

_REFLECTION_TIMEOUT_SECONDS = 120.0
_MAX_ATTEMPTS = 3
_QUQU_LINE_RE = re.compile(r"^\s*~>\[(lead|echo)\]\s*(\S.*)$", re.MULTILINE)
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)

PersistMessage = Callable[[RuntimeMessage], Any | Awaitable[Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_reflection_model(agent_id: str) -> ResolvedUtilityModel | None:
    """Prefer the configured fast utility model, then the active chat model."""

    config = load_config().utility_intelligence
    if not bool(getattr(config, "background_processing_enabled", True)):
        return None
    resolved = None
    if config.mode != "disabled":
        try:
            resolved = resolve_utility_model(config, agent_id)
        except Exception:
            logger.warning("Dedicated reflection model unavailable", exc_info=True)
    if resolved is not None:
        return resolved
    try:
        return resolve_active_model(agent_id)
    except Exception:
        logger.warning("Active reflection fallback model unavailable", exc_info=True)
        return None


def _fingerprint(value: str) -> str:
    match = _QUQU_LINE_RE.search(_FENCED_CODE_RE.sub("", str(value or "")))
    body = match.group(2) if match is not None else str(value or "")
    return re.sub(r"[\W_]+", "", body, flags=re.UNICODE).casefold()


def _overlaps(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 8:
        return False
    if left in right or right in left:
        return True
    shorter, longer = sorted((left, right), key=len)
    return any(
        shorter[index : index + 12] in longer
        for index in range(max(0, len(shorter) - 11))
    )


def _ququ_repeats_request(
    ququ_line: str,
    request: PostAnswerReflectionRequest,
) -> bool:
    candidate = _fingerprint(ququ_line)
    return any(
        _overlaps(candidate, _fingerprint(text))
        for text in [*request.assistant_history, request.final_answer]
        if str(text or "").strip()
    )


class PostAnswerReflectionService:
    """Run model-owned reflection after the foreground turn is terminal.

    Jobs are persisted before dispatch. A later turn in the same chat/session
    can recover a failed or process-interrupted job. There is no local prose
    fallback: only validated model output can become visible.
    """

    _active_tasks: ClassVar[dict[str, asyncio.Task[None]]] = {}

    def __init__(
        self, workspace_dir: str | Path, *, agent_id: str,
        research_workspace: str | Path | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        # Reflection state may live in a topic; research belongs to agent home.
        self.research_workspace = Path(
            research_workspace if research_workspace is not None else workspace_dir
        ).expanduser().resolve()
        self.agent_id = str(agent_id or "default")
        safe_agent = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.agent_id)
        self.jobs_dir = (
            self.workspace_dir
            / ".lantai"
            / "persona"
            / "post-answer-reflections"
            / safe_agent
        )

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _read_job(self, job_id: str) -> dict[str, Any] | None:
        path = self._job_path(job_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_job(self, job: dict[str, Any]) -> None:
        job["updated_at"] = _now_iso()
        write_text_atomic_no_follow(
            self._job_path(str(job["job_id"])),
            json.dumps(job, ensure_ascii=False, indent=2),
        )

    def _new_job(self, request: PostAnswerReflectionRequest) -> dict[str, Any]:
        from ..research.context import current_binding

        binding = current_binding()
        if binding is not None and binding.service.workspace != self.research_workspace:
            raise ValueError("reflection research workspace does not match origin")
        return {
            "schema_version": 1,
            "job_id": request.job_id,
            "status": "pending",
            "attempts": 0,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "last_error": "",
            "reflection_model": "",
            "reflection": None,
            "subject_persisted": False,
            "message_persisted": False,
            "event_persisted": False,
            "request": request.to_dict(),
            "research_project_id": binding.project if binding is not None else None,
        }

    def schedule(
        self,
        request: PostAnswerReflectionRequest,
        *,
        persist_message: PersistMessage | None = None,
    ) -> asyncio.Task[None] | None:
        config = load_config().utility_intelligence
        if not bool(getattr(config, "background_processing_enabled", True)):
            return None
        if not request.need_persona and not request.need_ququ:
            return None
        if not request.workspace_dir:
            request = request.with_workspace_dir(str(self.workspace_dir))
        job_id = request.job_id
        active = self._active_tasks.get(job_id)
        if active is not None and not active.done():
            return active
        job = self._read_job(job_id)
        if job is None:
            job = self._new_job(request)
            self._write_job(job)
        if job.get("status") == "completed":
            return None
        if int(job.get("attempts") or 0) >= _MAX_ATTEMPTS:
            return None
        task = asyncio.create_task(
            self._run(job_id, persist_message=persist_message),
            name=f"post-answer-reflection:{job_id[:12]}",
        )
        self._active_tasks[job_id] = task

        def forget(done: asyncio.Task[None]) -> None:
            if self._active_tasks.get(job_id) is done:
                self._active_tasks.pop(job_id, None)

        task.add_done_callback(forget)
        return task

    def recover_pending(
        self,
        *,
        chat_id: str | None = None,
        session_id: str | None = None,
        persist_message: PersistMessage | None = None,
    ) -> list[asyncio.Task[None]]:
        if not self.jobs_dir.exists():
            return []
        tasks: list[asyncio.Task[None]] = []
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                request = PostAnswerReflectionRequest.from_dict(
                    dict(payload.get("request") or {})
                )
            except (OSError, ValueError, TypeError):
                continue
            if chat_id is not None and request.chat_id != chat_id:
                continue
            if session_id is not None and request.session_id != session_id:
                continue
            task = self.schedule(request, persist_message=persist_message)
            if task is not None and task not in tasks:
                tasks.append(task)
        return tasks

    async def _run(
        self,
        job_id: str,
        *,
        persist_message: PersistMessage | None,
    ) -> None:
        job = self._read_job(job_id)
        if job is None or job.get("status") == "completed":
            return
        request = PostAnswerReflectionRequest.from_dict(dict(job.get("request") or {}))
        job["status"] = "running"
        job["attempts"] = int(job.get("attempts") or 0) + 1
        self._write_job(job)
        from ..research.chat import ResearchChatBinding
        from ..research.context import set_binding, reset_binding
        from ..research.projects import ResearchService

        # Never inherit the recovering turn's owner, including for legacy jobs
        # whose original association was not persisted.
        binding_token = set_binding(None)
        try:
            project_id = job.get("research_project_id")
            if project_id is not None:
                if not isinstance(project_id, str) or not project_id.strip():
                    raise ValueError("invalid reflection research project association")
                set_binding(ResearchChatBinding(
                    ResearchService(self.research_workspace), project_id,
                ))
            saved_reflection = job.get("reflection")
            reflection = (
                PostAnswerReflection(**saved_reflection)
                if isinstance(saved_reflection, dict)
                else None
            )
            if reflection is None:
                resolved = resolve_reflection_model(request.agent_id)
                if resolved is None:
                    raise RuntimeError("no reflection model available")
                prompt = build_post_answer_reflection_prompt(
                    user_message=request.user_message,
                    final_answer=request.final_answer,
                    plan=request.plan,
                    subject_context=request.subject_context,
                    need_persona=request.need_persona,
                    need_ququ=request.need_ququ,
                )
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "你只负责一次私有的结构化答后反思。"
                            "先输出 JSON，不要写推理过程、Markdown 或正文。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ]

                config = load_config().utility_intelligence
                dedicated_label = (
                    f"{config.model.provider_id}/{config.model.model}"
                    if config.mode == "dedicated"
                    else ""
                )
                async def attempt(candidate: ResolvedUtilityModel) -> PostAnswerReflection:
                    async def consume_reflection() -> str:
                        return await consume_model_response(
                            candidate.model,
                            messages,
                            first_output_timeout_seconds=45.0,
                            idle_timeout_seconds=45.0,
                            total_timeout_seconds=_REFLECTION_TIMEOUT_SECONDS,
                        )

                    if dedicated_label and candidate.label == dedicated_label:
                        raw = await consume_reflection()
                    else:
                        while True:
                            try:
                                raw = await run_primary_when_idle(
                                    request.agent_id,
                                    consume_reflection,
                                )
                                break
                            except PrimaryModelPreempted:
                                # The durable job remains running and waits for
                                # the newly visible turn to finish.
                                await asyncio.sleep(0)
                    parsed = parse_post_answer_reflection(raw)
                    if parsed is None:
                        raise ValueError("invalid model reflection")
                    if request.need_ququ and not parsed.ququ_line:
                        raise ValueError("hard-hit reflection produced no ququ")
                    if parsed.ququ_line and _ququ_repeats_request(
                        parsed.ququ_line,
                        request,
                    ):
                        raise ValueError(
                            "reflection repeated delivered assistant text",
                        )
                    return parsed

                if dedicated_label and resolved.label == dedicated_label:
                    try:
                        reflection = await attempt(resolved)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "Reflection accelerator failed; using idle primary",
                            exc_info=True,
                        )
                        fallback = resolve_active_model(request.agent_id)
                        if fallback is None:
                            raise RuntimeError(
                                "no active reflection fallback available",
                            )
                        reflection = await attempt(fallback)
                        resolved = fallback
                else:
                    reflection = await attempt(resolved)
                job["reflection_model"] = resolved.label
                # Persist the generated semantic result before delivery. If
                # the process dies between memory, message and event writes,
                # recovery resumes this exact reflection instead of asking
                # the model to invent a different one.
                job["reflection"] = asdict(reflection)
                self._write_job(job)
            await self._persist_result(
                job,
                request=request,
                reflection=reflection,
                persist_message=persist_message,
            )
            job["status"] = "completed"
            job["last_error"] = ""
        except asyncio.CancelledError:
            job["status"] = "pending"
            job["last_error"] = "cancelled before delivery"
            self._write_job(job)
            raise
        except Exception as exc:  # pylint: disable=broad-except
            job["status"] = "failed"
            job["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning(
                "Post-answer reflection sidecar failed; no local prose emitted",
                exc_info=True,
            )
        finally:
            reset_binding(binding_token)
        self._write_job(job)

    async def _persist_result(
        self,
        job: dict[str, Any],
        *,
        request: PostAnswerReflectionRequest,
        reflection: PostAnswerReflection,
        persist_message: PersistMessage | None,
    ) -> None:
        if not job.get("subject_persisted"):
            plan = request.plan
            SubjectContinuityStore(
                request.workspace_dir,
                agent_id=request.agent_id,
            ).record_reflection(
                reflection.subject_reflection(
                    turn_id=request.run_id,
                    session_id=request.session_id,
                    causal_anchor=str(plan.get("causal_anchor") or ""),
                    grounding_refs=tuple(plan.get("selected_memory_ids") or ()),
                    experience_sources=tuple(plan.get("experience_sources") or ()),
                    main_persona_missed=request.need_persona,
                )
            )
            job["subject_persisted"] = True
            self._write_job(job)

        ququ_line = reflection.ququ_line
        if not ququ_line:
            return
        route = (
            "semantic_bypass"
            if bool(request.plan.get("ququ_bypass_worthy"))
            else "baseline"
        )
        message_id = f"post-reflection-ququ-{job['job_id']}"
        message = RuntimeMessage.text(
            "assistant",
            ququ_line,
            id=message_id,
            metadata={
                "client_turn_id": request.client_turn_id,
                "source_message_id": message_id,
                "answer_message_id": request.answer_message_id,
                "post_answer_reflection_job_id": job["job_id"],
                "ququ_delivery": {
                    "route": route,
                    "repaired": False,
                    "source": "async_post_answer_reflection",
                },
            },
        )
        if not job.get("message_persisted") and persist_message is not None:
            result = persist_message(message)
            if inspect.isawaitable(result):
                await result
            job["message_persisted"] = True
            self._write_job(job)

        if not job.get("event_persisted"):
            event_id = str(uuid5(NAMESPACE_URL, f"lantai:{job['job_id']}:ququ"))
            sidecar_run_id = f"{request.run_id}:post-answer:{job['job_id'][:12]}"
            try:
                await append_chat_event(
                    workspace_dir=request.workspace_dir,
                    agent_id=request.agent_id,
                    chat_id=request.chat_id,
                    session_id=request.session_id,
                    run_id=sidecar_run_id,
                    item_id=message_id,
                    kind="chat.message.completed",
                    type="message.completed",
                    status="completed",
                    source="assistant",
                    summary=ququ_line,
                    event_id=event_id,
                    payload={
                        "client_turn_id": request.client_turn_id,
                        "answer_message_id": request.answer_message_id,
                        "source_message_id": message_id,
                        "content_type": "text",
                        "stream_message_type": "message",
                        "text": ququ_line,
                        "message": message.model_dump(mode="json"),
                        "details": {
                            "job_id": job["job_id"],
                            "original_run_id": request.run_id,
                            "reflection_model": job.get("reflection_model") or "",
                            "delivery": "late_bound_sidecar",
                        },
                    },
                )
            except RuntimeEventSequenceAppendError as exc:
                if "duplicate_event_id" not in exc.codes:
                    raise
            job["event_persisted"] = True
            self._write_job(job)


__all__ = ["PostAnswerReflectionService", "resolve_reflection_model"]
