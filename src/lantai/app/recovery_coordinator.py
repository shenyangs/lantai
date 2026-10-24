# -*- coding: utf-8 -*-
"""Recover unfinished chat RuntimeRuns after an application restart."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.runs import (
    ACTIVE_RUNTIME_RUN_STATUSES,
    RuntimeRunReason,
    RuntimeAutoResumeCondition,
    RuntimeRunReducerAction,
    evaluate_runtime_autonomy,
    evaluate_runtime_wake,
    reduce_runtime_run,
    start_runtime_run_attempt,
)
from lantai.runtime.store import RuntimeRunStore, RuntimeRunStoreConflictError
from lantai.security.beta_access import AccessBoundary

from .beta_access.admission import BetaAccessAdmissionDenied
from .beta_access.wiring import require_workspace_beta_access
from .channels.runtime_schema import AgentRequest
from .completion_gate import finalize_deliverable
from .experience import (
    append_chat_event,
    build_runtime_run_events_response,
    record_chat_turn_continue_requested,
    record_chat_turn_settled,
    resolve_effective_workspace_dir_from_meta,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    scanned: int = 0
    resumed: int = 0
    stalled: int = 0
    skipped: int = 0


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _assistant_text_from_event(
    payload: dict[str, Any],
    message_texts: dict[str, str],
) -> None:
    if payload.get("object") != "message" or payload.get("role") != "assistant":
        return
    if str(payload.get("type") or "message") != "message":
        return
    item_id = str(payload.get("id") or "")
    chunks: list[str] = []
    for item in payload.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    text = "".join(chunks)
    if not item_id or not text:
        return
    if any(
        isinstance(item, dict) and item.get("delta") is True
        for item in payload.get("content") or []
    ):
        message_texts[item_id] = message_texts.get(item_id, "") + text
    else:
        message_texts[item_id] = text


class RecoveryCoordinator:
    """Resume safe unfinished runs; stall ambiguous effects without replay."""

    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace

    async def dispatch_prepared_attempt(
        self,
        chat: Any,
        run_id: str,
        *,
        state_workspace_dir: str | Path | None = None,
    ) -> Any:
        """Claim, start, and dispatch a queued retry/resume attempt."""

        require_workspace_beta_access(
            self.workspace,
            boundary=AccessBoundary.RESUME_OR_WAKE,
            capability="runtime.attempt.dispatch",
        )

        state_root = Path(state_workspace_dir or self.workspace.workspace_dir)
        store = RuntimeRunStore(state_root)
        contract = await store.load_contract(run_id)
        if contract is None or contract.status != "queued":
            raise RuntimeError("Prepared RuntimeRun attempt is not queued.")
        now = datetime.now(timezone.utc)
        lease_owner = contract.lease_owner or f"retry:{os.getpid()}:{run_id}"
        claimed = reduce_runtime_run(
            contract,
            RuntimeRunReducerAction(
                type="claim",
                expected_attempt_id=contract.attempt_id,
                expected_revision=contract.revision,
                lease_owner=lease_owner,
                now=now,
            ),
        ).run
        claimed = await store.save_contract(
            claimed,
            expected_revision=contract.revision,
            expected_attempt_id=contract.attempt_id,
        )
        started = reduce_runtime_run(
            claimed,
            RuntimeRunReducerAction(
                type="start",
                expected_attempt_id=claimed.attempt_id,
                expected_revision=claimed.revision,
                lease_owner=claimed.lease_owner,
                now=now,
            ),
        ).run
        started = await store.save_contract(
            started,
            expected_revision=claimed.revision,
            expected_attempt_id=claimed.attempt_id,
            lease_owner=claimed.lease_owner,
        )
        await self._dispatch(chat, started, state_workspace_dir=state_root)
        return started

    async def recover(
        self,
        *,
        auto_resume: bool = False,
        require_autonomy_grant: bool = False,
        due_wakes_only: bool = False,
        autonomy_resume_condition: RuntimeAutoResumeCondition | None = None,
    ) -> RecoveryReport:
        """Reconcile unfinished runs after restart.

        Explicit resume endpoints may opt into dispatch with
        ``auto_resume=True`` because the current user action is the authority.
        Startup recovery must additionally pass
        ``require_autonomy_grant=True`` so ordinary tasks remain fail-closed
        while a current, task-scoped user grant may use the same dispatch path.
        """
        chats = await self.workspace.chat_manager.list_chats()
        state_roots: dict[str, Path] = {
            str(Path(self.workspace.workspace_dir)): Path(self.workspace.workspace_dir)
        }
        for chat in chats:
            topic_root = resolve_effective_workspace_dir_from_meta(
                {"topic_workspace_dir": (chat.meta or {}).get("topic_workspace_dir")},
                root_workspace_dir=self.workspace.workspace_dir,
            )
            if topic_root is not None:
                state_roots[str(topic_root)] = topic_root
        contracts: list[tuple[Any, Path]] = []
        active_contract_run_ids: set[str] = set()
        for state_root in state_roots.values():
            for contract in await RuntimeRunStore(state_root).list_contracts_async():
                if contract.source == "chat" and contract.status in ACTIVE_RUNTIME_RUN_STATUSES:
                    active_contract_run_ids.add(contract.run_id)
                if (
                    contract.source == "chat"
                    and contract.status in ACTIVE_RUNTIME_RUN_STATUSES
                    and contract.task_contract is not None
                    and (not due_wakes_only or contract.wake_intent is not None)
                ):
                    contracts.append((contract, state_root))
        resumed = stalled = skipped = 0
        for contract, state_root in contracts:
            store = RuntimeRunStore(state_root)
            chat = next(
                (
                    item
                    for item in chats
                    if str((item.meta or {}).get("last_turn_run_id") or "")
                    == contract.run_id
                    or str(
                        ((item.meta or {}).get("pending_turn") or {}).get("run_id")
                        if isinstance((item.meta or {}).get("pending_turn"), dict)
                        else ""
                    )
                    == contract.run_id
                ),
                None,
            )
            if chat is None:
                await self._stall(
                    store,
                    contract,
                    code="recovery_chat_missing",
                    message="Cannot recover the run because its chat is missing.",
                )
                stalled += 1
                continue
            safe, blockers = await self._settle_crash_window_effects(
                contract,
                state_workspace_dir=state_root,
            )
            if not safe:
                await self._stall(
                    store,
                    contract,
                    code="recovery_effect_unknown",
                    message=(
                        "Recovery stopped because external effects require a probe: "
                        + ", ".join(blockers)
                    ),
                )
                await self._mark_chat_stalled(chat, contract.run_id, blockers)
                stalled += 1
                continue
            if not auto_resume:
                await self._stall(
                    store,
                    contract,
                    code="recovery_explicit_resume_required",
                    message=(
                        "The application restarted while this run was active. "
                        "Resume it explicitly to continue from the checkpoint."
                    ),
                )
                await self._mark_chat_resumable(chat, contract.run_id)
                stalled += 1
                continue
            if require_autonomy_grant:
                if contract.status in {
                    "paused",
                    "waiting_approval",
                    "stop_requested",
                }:
                    # Startup must preserve explicit pause, approval, and stop
                    # boundaries instead of rewriting them as recovery errors.
                    skipped += 1
                    continue
                now = datetime.now(timezone.utc)
                resume_condition = autonomy_resume_condition or (
                    "network_recovery" if due_wakes_only else "app_reopen"
                )
                authority = evaluate_runtime_autonomy(
                    contract,
                    operation="automatic_resume",
                    chat_id=chat.id,
                    workspace_id=str(state_root),
                    now=now,
                    resume_condition=resume_condition,
                )
                if authority.allowed and contract.wake_intent is not None:
                    authority = evaluate_runtime_wake(
                        contract,
                        chat_id=chat.id,
                        workspace_id=str(state_root),
                        now=now,
                        resume_condition=resume_condition,
                    )
                    if authority.code == "wake_not_due":
                        skipped += 1
                        continue
                if not authority.allowed:
                    await self._stall(
                        store,
                        contract,
                        code="recovery_autonomy_grant_required",
                        message=(
                            "Automatic restart recovery is outside the current "
                            f"user grant: {authority.code}. Resume explicitly to continue."
                        ),
                    )
                    await self._mark_chat_resumable(chat, contract.run_id)
                    stalled += 1
                    continue
            try:
                require_workspace_beta_access(
                    self.workspace,
                    boundary=AccessBoundary.RESUME_OR_WAKE,
                    capability="runtime.attempt.recover",
                )
            except BetaAccessAdmissionDenied:
                skipped += 1
                continue
            try:
                resumed_contract = await self._prepare_resume_attempt(store, contract)
                started = await self._dispatch(
                    chat,
                    resumed_contract,
                    state_workspace_dir=state_root,
                )
            except RuntimeRunStoreConflictError:
                # Another scanner, explicit continue, or recovery worker won
                # the same persisted fence. The current authoritative run must
                # remain untouched; the losing observer only reloads later.
                skipped += 1
                continue
            except Exception:
                logger.exception("Failed to dispatch recovered RuntimeRun %s", contract.run_id)
                current = await store.load_contract(contract.run_id)
                if current is not None and current.status in ACTIVE_RUNTIME_RUN_STATUSES:
                    await self._stall(
                        store,
                        current,
                        code="recovery_dispatch_failed",
                        message="Recovery dispatch failed before model execution.",
                    )
                stalled += 1
                continue
            if started:
                resumed += 1
            else:
                skipped += 1

        orphaned = 0
        if due_wakes_only:
            return RecoveryReport(
                scanned=len(contracts),
                resumed=resumed,
                stalled=stalled,
                skipped=skipped,
            )
        for chat in chats:
            meta = dict(chat.meta or {})
            pending = (
                dict(meta.get("pending_turn") or {})
                if isinstance(meta.get("pending_turn"), dict)
                else {}
            )
            status = str(
                pending.get("status") or meta.get("last_turn_status") or ""
            ).strip().lower()
            if status != "running":
                continue
            run_id = str(
                pending.get("run_id")
                or pending.get("turn_id")
                or meta.get("last_turn_run_id")
                or chat.id
            ).strip()
            if run_id in active_contract_run_ids:
                continue
            state_root = resolve_effective_workspace_dir_from_meta(
                {"topic_workspace_dir": meta.get("topic_workspace_dir")},
                root_workspace_dir=self.workspace.workspace_dir,
            ) or Path(self.workspace.workspace_dir)
            await self._mark_orphaned_chat_stalled(
                chat,
                run_id,
                state_workspace_dir=state_root,
            )
            orphaned += 1
            stalled += 1
        return RecoveryReport(
            scanned=len(contracts) + orphaned,
            resumed=resumed,
            stalled=stalled,
            skipped=skipped,
        )

    async def _mark_orphaned_chat_stalled(
        self,
        chat: Any,
        run_id: str,
        *,
        state_workspace_dir: Path,
    ) -> None:
        """Settle legacy running metadata that has no recoverable checkpoint."""

        await self.workspace.chat_manager.merge_chat_meta(
            chat.id,
            {
                "last_turn_status": "stalled",
                "last_turn_final_task_status": "stalled",
                "last_turn_settled_at": datetime.now(timezone.utc).isoformat(),
                "last_result_summary": (
                    "上次任务异常中断，未找到可恢复检查点，请重新发送"
                ),
                "last_turn_outcome_status": "needs_input",
                "last_turn_outcome_run_id": run_id,
                "last_turn_delivery_status": "not_delivered",
                "recovery_orphaned_run_id": run_id,
            },
            remove_keys=["pending_turn"],
        )
        try:
            await record_chat_turn_settled(
                workspace_dir=state_workspace_dir,
                agent_id=self.workspace.agent_id,
                chat_id=chat.id,
                session_id=chat.session_id,
                final_task_status="stalled",
                outcome_status="needs_input",
                delivery_status="not_delivered",
                pending_turn_cleared=True,
                active_run_event_count=0,
                run_id=run_id,
            )
        except Exception:
            logger.exception(
                "Failed to record orphaned RuntimeRun settlement for %s",
                run_id,
            )

    async def _mark_chat_resumable(self, chat: Any, run_id: str) -> None:
        """Expose an interrupted run without silently dispatching it."""

        pending = (
            dict((chat.meta or {}).get("pending_turn") or {})
            if isinstance((chat.meta or {}).get("pending_turn"), dict)
            else {}
        )
        await self.workspace.chat_manager.merge_chat_meta(
            chat.id,
            {
                "last_turn_status": "stalled",
                "last_turn_final_task_status": "stalled",
                "last_turn_settled_at": datetime.now(timezone.utc).isoformat(),
                "last_result_summary": "上次任务因应用重启而中断，可手动继续",
                "last_turn_outcome_status": "needs_input",
                "last_turn_outcome_run_id": run_id,
                "pending_turn": {
                    **pending,
                    "run_id": run_id,
                    "status": "stalled",
                    "recovery_requires_explicit_resume": True,
                },
            },
        )

    async def _settle_crash_window_effects(
        self,
        contract: Any,
        *,
        state_workspace_dir: str | Path | None = None,
    ) -> tuple[bool, list[str]]:
        journal = RuntimeEffectJournal(
            state_workspace_dir or self.workspace.workspace_dir
        )
        records = journal.list_effects(contract.run_id, contract.attempt_id)
        latest: dict[str, Any] = {}
        for record in records:
            current = latest.get(record.effect_id)
            if current is None or record.revision > current.revision:
                latest[record.effect_id] = record
        checkpoint_effects = set(
            contract.checkpoint.effect_ids if contract.checkpoint is not None else ()
        )
        blockers: list[str] = []
        for effect_id, effect in latest.items():
            if effect.status in {"intent", "waiting_approval", "approved"}:
                journal.append_effect(
                    reduce_runtime_effect(
                        effect,
                        RuntimeEffectReducerAction(type="cancel"),
                    ).effect
                )
            elif effect.status == "running":
                ambiguous = reduce_runtime_effect(
                    effect,
                    RuntimeEffectReducerAction(
                        type="mark_ambiguous",
                        ambiguity_reason="process_restarted_during_external_effect",
                    ),
                ).effect
                journal.append_effect(ambiguous)
                blockers.append(effect_id)
            elif effect.status == "ambiguous":
                blockers.append(effect_id)
            elif effect.status in {"committed", "probed_committed"}:
                if effect_id not in checkpoint_effects:
                    blockers.append(effect_id)
        return not blockers, sorted(set(blockers))

    async def _prepare_resume_attempt(self, store: RuntimeRunStore, contract: Any) -> Any:
        now = datetime.now(timezone.utc)
        attempt = start_runtime_run_attempt(
            contract,
            attempt_id=f"{contract.run_id}:resume-{uuid4().hex[:12]}",
            attempt_reason="resume",
            lease_owner=f"recovery:{os.getpid()}:{contract.run_id}",
            now=now,
        )
        saved = await store.save_contract(
            attempt,
            expected_revision=contract.revision,
            expected_attempt_id=contract.attempt_id,
        )
        claimed = reduce_runtime_run(
            saved,
            RuntimeRunReducerAction(
                type="claim",
                expected_attempt_id=saved.attempt_id,
                expected_revision=saved.revision,
                lease_owner=saved.lease_owner,
                now=now,
            ),
        ).run
        claimed = await store.save_contract(
            claimed,
            expected_revision=saved.revision,
            expected_attempt_id=saved.attempt_id,
        )
        started = reduce_runtime_run(
            claimed,
            RuntimeRunReducerAction(
                type="start",
                expected_attempt_id=claimed.attempt_id,
                expected_revision=claimed.revision,
                lease_owner=claimed.lease_owner,
                now=now,
            ),
        ).run
        return await store.save_contract(
            started,
            expected_revision=claimed.revision,
            expected_attempt_id=claimed.attempt_id,
            lease_owner=claimed.lease_owner,
        )

    async def _dispatch(
        self,
        chat: Any,
        contract: Any,
        *,
        state_workspace_dir: str | Path | None = None,
    ) -> bool:
        state_root = Path(state_workspace_dir or self.workspace.workspace_dir)
        effective_workspace = resolve_effective_workspace_dir_from_meta(
            chat.meta or {},
            root_workspace_dir=self.workspace.workspace_dir,
        ) or self.workspace.workspace_dir
        objective = contract.task_contract.objective
        request = AgentRequest(
            input=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": objective}],
                }
            ],
            session_id=chat.session_id,
            user_id=chat.user_id,
            channel="console",
            root_session_id=chat.session_id,
            channel_meta={
                "chat_id": chat.id,
                "run_id": contract.run_id,
                "turn_id": contract.run_id,
                "workspace_dir": str(effective_workspace),
                "state_workspace_dir": str(state_root),
                "work_method_id": contract.task_contract.work_method_id,
                "recovery_reason": "resume",
            },
        )
        await self.workspace.chat_manager.merge_chat_meta(
            chat.id,
            {
                "last_turn_run_id": contract.run_id,
                "last_turn_status": "running",
                "pending_turn": {
                    "run_id": contract.run_id,
                    "turn_id": contract.run_id,
                    "status": "running",
                    "text": objective,
                    "session_id": chat.session_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "recovered": True,
                },
            },
            remove_keys=[
                "last_turn_final_task_status",
                "last_turn_settled_at",
                "last_turn_outcome_status",
                "last_turn_outcome_run_id",
            ],
        )
        await record_chat_turn_continue_requested(
            workspace_dir=state_root,
            agent_id=self.workspace.agent_id,
            chat_id=chat.id,
            session_id=chat.session_id,
            run_id=contract.run_id,
            mode="resume",
            reason="application_restart",
            status="running",
            summary="应用重启后已从检查点恢复",
            attempt_id=contract.attempt_id,
        )

        async def stream_recovered(_payload: Any):
            async for event_sse in self._run_recovered_turn(
                chat=chat,
                contract=contract,
                request=request,
                effective_workspace=Path(effective_workspace),
                state_workspace=state_root,
            ):
                yield event_sse

        queue, is_new = await self.workspace.task_tracker.attach_or_start(
            chat.id,
            request,
            stream_recovered,
            attempt_id=contract.run_id,
            root_session_id=chat.session_id,
            request_id=contract.task_contract.client_turn_id,
            reject_conflicting_request=True,
        )
        if is_new:

            async def drain_initial_queue() -> None:
                async for _ in self.workspace.task_tracker.stream_from_queue(
                    queue,
                    chat.id,
                ):
                    pass

            asyncio.create_task(drain_initial_queue())
        return is_new

    async def _run_recovered_turn(
        self,
        *,
        chat: Any,
        contract: Any,
        request: AgentRequest,
        effective_workspace: Path,
        state_workspace: Path,
    ):
        terminal_payload: dict[str, Any] | None = None
        message_texts: dict[str, str] = {}
        response_status = "failed"
        try:
            async for event in self.workspace.runner.stream_query(request):
                payload = event.model_dump(mode="json")
                _assistant_text_from_event(payload, message_texts)
                if payload.get("object") == "response" and payload.get("status") in {
                    "completed",
                    "failed",
                }:
                    terminal_payload = payload
                    response_status = str(payload.get("status"))
                    continue
                yield _sse(payload)
        except asyncio.CancelledError:
            await record_chat_turn_settled(
                workspace_dir=state_workspace,
                agent_id=self.workspace.agent_id,
                chat_id=chat.id,
                session_id=chat.session_id,
                final_task_status="interrupted",
                outcome_status="incomplete",
                delivery_status="partial",
                pending_turn_cleared=True,
                active_run_event_count=0,
                run_id=contract.run_id,
            )
            raise
        except Exception as exc:
            logger.exception("Recovered RuntimeRun failed: %s", contract.run_id)
            terminal_payload = {
                "id": f"response_recovery_failed_{contract.run_id}",
                "object": "response",
                "status": "failed",
                "type": "response.failed",
                "output": [],
                "error": {"message": str(exc) or "Recovery execution failed."},
            }
            response_status = "failed"

        assistant_text = "\n".join(
            text.strip() for text in message_texts.values() if text.strip()
        )
        final_response_id = str((terminal_payload or {}).get("id") or "") or None
        gate = await finalize_deliverable(
            state_workspace_dir=state_workspace,
            effective_workspace_dir=effective_workspace,
            works_workspace_dir=self.workspace.workspace_dir,
            run_id=contract.run_id,
            chat_id=chat.id,
            response_status=response_status,
            assistant_text=assistant_text,
            final_response_id=final_response_id,
            runtime_events=build_runtime_run_events_response(
                [state_workspace],
                contract.run_id,
                attempt_id=contract.attempt_id,
            ).events,
        )
        final_status = "completed" if gate.allow_success else "failed"
        outcome_status = (
            "delivered"
            if gate.allow_success
            else "incomplete"
            if gate.primary_path
            else "blocked"
        )
        terminal_payload = terminal_payload or {
            "id": f"response_recovery_missing_{contract.run_id}",
            "object": "response",
            "output": [],
        }
        terminal_payload.update(
            {
                "status": final_status,
                "type": f"response.{final_status}",
                "metadata": {
                    **dict(terminal_payload.get("metadata") or {}),
                    "run_id": contract.run_id,
                    "attempt_id": contract.attempt_id,
                    "recovered": True,
                    "delivery_status": gate.delivery_status,
                    "completion_gate_passed": gate.allow_success,
                    "outcome_status": outcome_status,
                },
            }
        )
        if not gate.allow_success:
            terminal_payload["error"] = {
                "code": "completion_gate_failed",
                "message": gate.failure_summary or "Recovered delivery was not verified.",
            }
        if gate.work_item is not None:
            await append_chat_event(
                workspace_dir=state_workspace,
                agent_id=self.workspace.agent_id,
                chat_id=chat.id,
                session_id=chat.session_id,
                run_id=contract.run_id,
                kind="chat.artifact.created",
                type="artifact.created",
                status="completed",
                source="system",
                item_id=gate.work_item.artifact_id,
                summary=f"作品已入库：{gate.work_item.title}",
                payload={
                    "artifact_id": gate.work_item.artifact_id,
                    "work_id": gate.work_item.id,
                    "source_run_id": contract.run_id,
                    "path": gate.work_item.path,
                    "title": gate.work_item.title,
                },
            )
        await record_chat_turn_settled(
            workspace_dir=state_workspace,
            agent_id=self.workspace.agent_id,
            chat_id=chat.id,
            session_id=chat.session_id,
            final_task_status=final_status,
            outcome_status=outcome_status,
            delivery_status=gate.delivery_status,
            completion_evidence=gate.evidence,
            pending_turn_cleared=True,
            active_run_event_count=len(message_texts),
            run_id=contract.run_id,
        )
        await self.workspace.chat_manager.merge_chat_meta(
            chat.id,
            {
                "last_turn_status": final_status,
                "last_turn_final_task_status": final_status,
                "last_turn_settled_at": datetime.now(timezone.utc).isoformat(),
                "last_result_summary": assistant_text[:160]
                or gate.failure_summary
                or "恢复运行已结束",
                "last_turn_outcome_status": outcome_status,
                "last_turn_outcome_run_id": contract.run_id,
                "last_turn_delivery_status": gate.delivery_status,
            },
            remove_keys=["pending_turn"],
        )
        yield _sse(terminal_payload)

    async def _stall(
        self,
        store: RuntimeRunStore,
        contract: Any,
        *,
        code: str,
        message: str,
    ) -> None:
        stalled = reduce_runtime_run(
            contract,
            RuntimeRunReducerAction(
                type="settle",
                status="stalled",
                expected_attempt_id=contract.attempt_id,
                expected_revision=contract.revision,
                lease_owner=contract.lease_owner,
                reason=RuntimeRunReason(
                    code=code,
                    message=message,
                    category="runtime",
                    retryable=True,
                ),
            ),
        ).run
        await store.save_contract(
            stalled,
            expected_revision=contract.revision,
            expected_attempt_id=contract.attempt_id,
            lease_owner=contract.lease_owner,
        )

    async def _mark_chat_stalled(
        self,
        chat: Any,
        run_id: str,
        blockers: list[str],
    ) -> None:
        await self.workspace.chat_manager.merge_chat_meta(
            chat.id,
            {
                "last_turn_status": "stalled",
                "last_turn_final_task_status": "stalled",
                "last_turn_settled_at": datetime.now(timezone.utc).isoformat(),
                "last_result_summary": "恢复需要先确认未知副作用",
                "last_turn_outcome_status": "needs_input",
                "last_turn_outcome_run_id": run_id,
                "recovery_blocking_effect_ids": blockers,
            },
            remove_keys=["pending_turn"],
        )


__all__ = ["RecoveryCoordinator", "RecoveryReport"]
