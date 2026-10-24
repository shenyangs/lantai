#!/usr/bin/env python3
"""Real-provider RUN-09 smoke for replacing an unfinished PPT with text.

The command runs only against an isolated copy of the selected desktop
profile and encrypted provider store.  The current user message is accepted
on stdin, retained only in memory, and represented in evidence by a SHA-256
digest.  Neither credentials nor model text are printed or persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
CONFIGURED_PROVIDER_SMOKE_PATH = (
    PROJECT_ROOT / "scripts" / "source_configured_provider_stream_smoke.py"
)
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from lantai.app.runner.manager import ChatManager
from lantai.app.runner.models import ChatSpec
from lantai.app.runner.repo import JsonChatRepository
from lantai.presentation.jobs import PresentationJobStore
from lantai.presentation.models import JobStatus, PresentationJobRequest
from lantai.runtime.runs import (
    RuntimeDeliverableContract,
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore


class ProviderTurnFailure(RuntimeError):
    """Sanitized provider-turn failure with safe partial SSE metadata."""

    def __init__(
        self,
        *,
        kind: str,
        frames: list[tuple[int, dict[str, Any]]],
        elapsed_ms: int,
        stderr_sha256: str | None,
        returncode: int | None,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.frames = frames
        self.elapsed_ms = elapsed_ms
        self.stderr_sha256 = stderr_sha256
        self.returncode = returncode


def _load_configured_provider_smoke():
    spec = importlib.util.spec_from_file_location(
        "source_configured_provider_stream_smoke",
        CONFIGURED_PROVIDER_SMOKE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "Unable to load configured-provider smoke helpers"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _opaque_id(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _source_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _safe_file_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "bytes": 0, "sha256": None}
    content = path.read_bytes()
    return {
        "present": True,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


async def _seed_pending_presentation_fixture(
    workspace_dir: Path,
    *,
    session_id: str,
) -> dict[str, str]:
    """Seed one exact unfinished PPT plus an unrelated same-workspace job."""

    manager = ChatManager(
        repo=JsonChatRepository(workspace_dir / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            name="RUN-09 provider smoke",
            session_id=session_id,
            user_id="default",
            channel="console",
        )
    )
    state_workspace_dir = (
        workspace_dir
        / "topic-workspaces"
        / f"run09-{uuid.uuid4().hex}"
    )
    state_workspace_dir.mkdir(parents=True, exist_ok=True)
    source_run_id = f"run-run09-source-{uuid.uuid4().hex}"
    source_attempt_id = f"{source_run_id}:attempt-1"
    now = datetime.now(timezone.utc)
    deliverable = RuntimeDeliverableContract(
        kind="presentation",
        accepted_extensions=(".pptx",),
        visual_verification_required=True,
    )
    source_task = build_chat_task_contract(
        objective="RUN-09 unfinished presentation fixture",
        client_turn_id=f"run09-source-{uuid.uuid4().hex}",
        complexity="standard",
        work_method_id="standard",
        run_id=source_run_id,
        attempt_id=source_attempt_id,
    ).model_copy(update={"deliverable_contract": deliverable})
    await RuntimeRunStore(state_workspace_dir).save_contract(
        RuntimeRunContract(
            run_id=source_run_id,
            attempt_id=source_attempt_id,
            source="chat",
            owner=RuntimeRunOwner(
                user_id="default",
                agent_id="default",
                session_id=session_id,
                workspace_id=str(state_workspace_dir),
                channel="console",
            ),
            status="failed",
            revision=0,
            created_at=now,
            started_at=now,
            heartbeat_at=now,
            settled_at=now,
            delivery_status="not_delivered",
            task_contract=source_task,
            release_edition="official",
        ),
        expected_revision=None,
    )

    conversation_id = "native-presentation-tool"
    jobs = PresentationJobStore(state_workspace_dir / "presentation-jobs")
    unrelated = jobs.create(
        PresentationJobRequest(
            conversation_id=conversation_id,
            user_request="RUN-09 unrelated presentation fixture",
            slide_count=3,
        ),
        runtime_run_id=f"run-run09-unrelated-{uuid.uuid4().hex}",
    )
    pending = jobs.create(
        PresentationJobRequest(
            conversation_id=conversation_id,
            user_request="RUN-09 unfinished presentation fixture",
            slide_count=19,
        ),
        runtime_run_id=source_run_id,
    )
    updated = await manager.merge_chat_meta(
        chat.id,
        {
            "topic_workspace_dir": str(state_workspace_dir),
            "last_turn_run_id": source_run_id,
            "last_turn_attempt_id": source_attempt_id,
            "last_turn_status": "failed",
            "last_turn_outcome_status": "incomplete",
            "last_turn_delivery_status": "not_delivered",
            "last_turn_settled_at": now.isoformat(),
        },
    )
    if updated is None:
        raise AssertionError("RUN-09 fixture chat was not persisted")
    return {
        "chat_id": chat.id,
        "source_run_id": source_run_id,
        "pending_job_id": pending.job_id,
        "unrelated_job_id": unrelated.job_id,
        "conversation_id": conversation_id,
        "state_workspace_dir": str(state_workspace_dir),
    }


def _safe_tool_event_trace(
    frames: list[tuple[int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Retain tool ordering without arguments, outputs, errors, or raw IDs."""

    call_slots: dict[str, str] = {}

    def call_slot(value: object) -> str:
        key = str(value or "").strip()
        if not key:
            return ""
        if key not in call_slots:
            call_slots[key] = f"call{len(call_slots) + 1}"
        return call_slots[key]

    safe_statuses = {
        "cancelled",
        "completed",
        "error",
        "failed",
        "pending",
        "running",
        "succeeded",
    }
    trace: list[dict[str, Any]] = []
    for elapsed_ms, frame in frames:
        event_type = str(frame.get("type") or "")
        if event_type not in {"plugin_call", "plugin_call_output"}:
            continue
        content = frame.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            data = block.get("data")
            if not isinstance(data, dict):
                continue
            raw_status = str(data.get("status") or "").strip().lower()
            trace.append(
                {
                    "elapsed_ms": elapsed_ms,
                    "type": event_type,
                    "tool_name": str(data.get("name") or ""),
                    "call": call_slot(data.get("call_id")),
                    "ok": (
                        data.get("ok")
                        if isinstance(data.get("ok"), bool)
                        else None
                    ),
                    "status": (
                        raw_status if raw_status in safe_statuses else ""
                    ),
                }
            )
    return trace


def _read_seeded_console_sse(
    *,
    provider_smoke: object,
    helpers: object,
    base_url: str,
    desktop_token: str,
    timeout: float,
    session_id: str,
    chat_id: str,
    prompt: str,
) -> list[tuple[int, dict[str, Any]]]:
    turn_id = uuid.uuid4().hex[:12]
    payload = provider_smoke._configured_provider_request_payload(
        turn_id,
        session_id=session_id,
        prompt=prompt,
    )
    payload["chat_id"] = chat_id
    curl_path = shutil.which("curl")
    if curl_path is None:
        raise RuntimeError("curl is required for the RUN-09 wall-clock deadline")
    headers = {
        "Content-Type": "application/json",
        **helpers._state_headers({"desktop_token": desktop_token}),
    }
    command = [
        curl_path,
        "--silent",
        "--show-error",
        "--no-buffer",
        "--fail",
        "--max-time",
        str(timeout),
        "--request",
        "POST",
    ]
    for name, value in headers.items():
        command.extend(["--header", f"{name}: {value}"])
    command.extend(
        [
            "--data-binary",
            "@-",
            f"{base_url}/api/console/chat",
        ]
    )
    started = time.monotonic()
    deadline = started + timeout
    frames: list[tuple[int, dict[str, Any]]] = []
    data_lines: list[str] = []
    stderr_parts: list[str] = []
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdin.write(json.dumps(payload, ensure_ascii=False))
    process.stdin.close()
    process.stdin = None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(timeout=min(0.25, remaining))
            if not events and process.poll() is not None:
                for stream in (process.stdout, process.stderr):
                    try:
                        selector.unregister(stream)
                    except KeyError:
                        pass
                break
            for key, _ in events:
                line = key.fileobj.readline()
                if line == "":
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_parts.append(line)
                    continue
                text = line.rstrip("\r\n")
                if text:
                    if text.startswith("data:"):
                        data_lines.append(text[5:].lstrip())
                    continue
                if data_lines:
                    frames.append(
                        (
                            round((time.monotonic() - started) * 1000),
                            helpers._parse_sse_data_lines(
                                data_lines,
                                source="RUN-09 configured provider",
                            ),
                        )
                    )
                    data_lines = []
    finally:
        selector.close()
    if timed_out and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    else:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait(timeout=5)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    stderr_text = "".join(stderr_parts)
    stderr_sha256 = (
        hashlib.sha256(stderr_text.encode("utf-8")).hexdigest()
        if stderr_text
        else None
    )
    returncode = process.returncode
    if timed_out or returncode == 28:
        raise ProviderTurnFailure(
            kind="wall_clock_timeout",
            frames=frames,
            elapsed_ms=elapsed_ms,
            stderr_sha256=stderr_sha256,
            returncode=returncode,
        )
    if returncode != 0:
        raise ProviderTurnFailure(
            kind="transport_error",
            frames=frames,
            elapsed_ms=elapsed_ms,
            stderr_sha256=stderr_sha256,
            returncode=returncode,
        )
    return frames


async def _inspect_incomplete_run09_state(
    workspace_dir: Path,
    *,
    fixture: dict[str, str],
    initial_job_ids: set[str],
) -> dict[str, Any]:
    """Capture only non-content state after an incomplete provider turn."""

    manager = ChatManager(
        repo=JsonChatRepository(workspace_dir / "chats.json"),
    )
    chat = await manager.get_chat(fixture["chat_id"])
    current_run_id = str(
        ((chat.meta if chat is not None else {}) or {}).get("last_turn_run_id")
        or ""
    ).strip()
    state_workspace_dir = Path(fixture["state_workspace_dir"])
    current = (
        await RuntimeRunStore(state_workspace_dir).load_contract(current_run_id)
        if current_run_id
        else None
    )
    task = current.task_contract if current is not None else None
    resolution = (
        task.pending_deliverable_resolution if task is not None else None
    )
    jobs = PresentationJobStore(
        state_workspace_dir / "presentation-jobs",
        read_only=True,
    )
    records = jobs.list_records(fixture["conversation_id"])
    pending = jobs.get(fixture["pending_job_id"])
    unrelated = jobs.get(fixture["unrelated_job_id"])
    pptx_count = sum(
        1
        for path in workspace_dir.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pptx"
    )
    return {
        "current_run": _opaque_id("run", current_run_id)
        if current_run_id
        else None,
        "new_runtime_run_persisted": bool(
            current_run_id and current_run_id != fixture["source_run_id"]
        ),
        "runtime_status": getattr(current, "status", None),
        "resolution_decision": getattr(resolution, "decision", None),
        "pending_job_status": pending.status.value,
        "unrelated_job_status": unrelated.status.value,
        "presentation_job_count_before": len(initial_job_ids),
        "presentation_job_count_after": len(records),
        "pptx_file_count": pptx_count,
    }


async def _inspect_run09_state(
    workspace_dir: Path,
    *,
    fixture: dict[str, str],
    frames: list[tuple[int, dict[str, Any]]],
    initial_job_ids: set[str],
) -> dict[str, Any]:
    if not frames:
        raise AssertionError("RUN-09 provider returned no SSE frames")
    first_message_ms = next(
        (elapsed for elapsed, frame in frames if frame.get("type") == "message"),
        None,
    )
    final_ms = next(
        (
            elapsed
            for elapsed, frame in reversed(frames)
            if frame.get("object") == "response"
            and frame.get("status") == "completed"
        ),
        None,
    )
    if first_message_ms is None or final_ms is None:
        raise AssertionError(
            "RUN-09 provider did not emit answer and completed SSE frames"
        )

    manager = ChatManager(
        repo=JsonChatRepository(workspace_dir / "chats.json"),
    )
    chat = await manager.get_chat(fixture["chat_id"])
    if chat is None:
        raise AssertionError("RUN-09 chat disappeared after the provider turn")
    current_run_id = str((chat.meta or {}).get("last_turn_run_id") or "").strip()
    if not current_run_id or current_run_id == fixture["source_run_id"]:
        raise AssertionError("RUN-09 did not persist a new Runtime Run")
    state_workspace_dir = Path(fixture["state_workspace_dir"])
    current = await RuntimeRunStore(state_workspace_dir).load_contract(
        current_run_id
    )
    task = current.task_contract if current is not None else None
    resolution = (
        task.pending_deliverable_resolution if task is not None else None
    )
    if resolution is None or resolution.decision != "replace":
        observed = getattr(resolution, "decision", None)
        raise AssertionError(
            f"RUN-09 model did not resolve the pending deliverable as replace: {observed}"
        )
    if resolution.source_run_id != fixture["source_run_id"]:
        raise AssertionError("RUN-09 resolution changed the trusted source task")
    if resolution.pending_job_id != fixture["pending_job_id"]:
        raise AssertionError("RUN-09 resolution changed the trusted PPT job")
    if task is None or task.deliverable_contract is not None:
        raise AssertionError("RUN-09 replacement inherited the old PPT contract")

    jobs = PresentationJobStore(
        state_workspace_dir / "presentation-jobs",
        read_only=True,
    )
    records = jobs.list_records(fixture["conversation_id"])
    final_job_ids = {record.job_id for record in records}
    if final_job_ids != initial_job_ids:
        raise AssertionError("RUN-09 replacement created a new presentation job")
    pending = jobs.get(fixture["pending_job_id"])
    unrelated = jobs.get(fixture["unrelated_job_id"])
    if pending.status != JobStatus.CANCELLED:
        raise AssertionError("RUN-09 did not cancel the exact unfinished PPT job")
    if unrelated.status != JobStatus.PENDING:
        raise AssertionError("RUN-09 mutated an unrelated presentation job")
    pptx_files = [
        path
        for path in workspace_dir.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pptx"
    ]
    if pptx_files:
        raise AssertionError("RUN-09 text replacement produced a PPTX file")
    return {
        "decision": resolution.decision,
        "current_run": _opaque_id("run", current_run_id),
        "source_run": _opaque_id("run", fixture["source_run_id"]),
        "pending_job": _opaque_id("job", pending.job_id),
        "unrelated_job": _opaque_id("job", unrelated.job_id),
        "pending_job_status": pending.status.value,
        "unrelated_job_status": unrelated.status.value,
        "presentation_job_count_before": len(initial_job_ids),
        "presentation_job_count_after": len(final_job_ids),
        "new_presentation_job_created": False,
        "pptx_file_count": 0,
        "current_deliverable_contract": None,
        "answer_visible_ms": first_message_ms,
        "final_visible_ms": final_ms,
    }


def run(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
    agent_id: str,
    timeout: float,
    prompt: str,
) -> dict[str, Any]:
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("RUN-09 prompt supplied on stdin must not be empty")
    provider_smoke = _load_configured_provider_smoke()
    helpers = provider_smoke._load_helpers()
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-run09-provider-smoke-"))
    process: subprocess.Popen[str] | None = None
    total_started = time.monotonic()
    log_path = smoke_root / "backend.log"
    frames: list[tuple[int, dict[str, Any]]] = []
    turn_failure: ProviderTurnFailure | None = None
    try:
        working_dir, secret_dir, configured_model = provider_smoke._stage_profile(
            source_working_dir=source_working_dir,
            source_secret_dir=source_secret_dir,
            agent_id=agent_id,
            target_root=smoke_root,
        )
        workspace_dir = working_dir / "workspaces" / "default"
        port = provider_smoke._available_port()
        base_url = f"http://127.0.0.1:{port}"
        desktop_token = "run09-provider-smoke-token"
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(SOURCE_ROOT), existing_pythonpath) if part
        )
        for key in ("LANTAI_RUNTIME_ENGINE", "LANTAI_RUNTIME_ENGINE_DEFAULT"):
            env.pop(key, None)
            env.pop(key.replace("LANTAI_", "COPAW_"), None)
        env.update(
            {
                "LANTAI_WORKING_DIR": str(working_dir),
                "LANTAI_SECRET_DIR": str(secret_dir),
                "LANTAI_DESKTOP_TOKEN": desktop_token,
                "LANTAI_AUTH_ENABLED": "0",
                "LANTAI_DISABLE_KEYRING": "1",
                "LANTAI_SKIP_KEYRING": "1",
                "LANTAI_REMINDER_DELIVERY_WORKER": "0",
            }
        )
        with log_path.open("w", encoding="utf-8") as log_file:
            project_python = PROJECT_ROOT / ".venv" / "bin" / "python"
            runtime_python = (
                str(project_python) if project_python.is_file() else sys.executable
            )
            process = subprocess.Popen(
                [
                    runtime_python,
                    "-m",
                    "lantai",
                    "app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            provider_smoke._wait_for_health(
                base_url,
                process,
                timeout=min(timeout, 60),
            )
            backend_ready_ms = round((time.monotonic() - total_started) * 1000)
            session_id = f"console:run09-provider-smoke-{uuid.uuid4().hex[:12]}"
            fixture = asyncio.run(
                _seed_pending_presentation_fixture(
                    workspace_dir,
                    session_id=session_id,
                )
            )
            state_workspace_dir = Path(fixture["state_workspace_dir"])
            initial_job_ids = {
                record.job_id
                for record in PresentationJobStore(
                    state_workspace_dir / "presentation-jobs",
                    read_only=True,
                ).list_records(fixture["conversation_id"])
            }
            turn_started = time.monotonic()
            try:
                frames = _read_seeded_console_sse(
                    provider_smoke=provider_smoke,
                    helpers=helpers,
                    base_url=base_url,
                    desktop_token=desktop_token,
                    timeout=timeout,
                    session_id=session_id,
                    chat_id=fixture["chat_id"],
                    prompt=normalized_prompt,
                )
            except ProviderTurnFailure as exc:
                frames = exc.frames
                turn_failure = exc
            provider_turn_ms = round((time.monotonic() - turn_started) * 1000)
        migration = provider_smoke._assert_profile_migrated(
            Path(configured_model["agent_path"])
        )
        if turn_failure is not None:
            incomplete_state = asyncio.run(
                _inspect_incomplete_run09_state(
                    workspace_dir,
                    fixture=fixture,
                    initial_job_ids=initial_job_ids,
                )
            )
            return {
                "schema_version": "1.0",
                "run_id": "RUN-09",
                "ok": False,
                "source_head": _source_head(),
                "provider": {
                    "provider_id": str(
                        configured_model.get("provider_id") or ""
                    ),
                    "model": str(configured_model.get("model") or ""),
                    "billing_basis": "user subscription",
                    "per_run_monetary_cost": "unavailable",
                },
                "input": {
                    "transport": "stdin",
                    "sha256": hashlib.sha256(
                        normalized_prompt.encode("utf-8")
                    ).hexdigest(),
                    "persisted": False,
                },
                "migration": migration,
                "failure": {
                    "kind": turn_failure.kind,
                    "wall_clock_limit_ms": round(timeout * 1000),
                    "elapsed_ms": turn_failure.elapsed_ms,
                    "transport_returncode": turn_failure.returncode,
                    "transport_stderr_sha256": (
                        turn_failure.stderr_sha256
                    ),
                    "backend_log": _safe_file_summary(log_path),
                },
                "observed_state": incomplete_state,
                "timing_ms": {
                    "backend_ready": backend_ready_ms,
                    "provider_turn": provider_turn_ms,
                    "total": round(
                        (time.monotonic() - total_started) * 1000
                    ),
                },
                "sse": {
                    "frame_count": len(frames),
                    "frame_types": sorted(
                        {str(frame.get("type") or "") for _, frame in frames}
                    ),
                    "frame_trace": provider_smoke._safe_frame_trace(frames),
                    "tool_trace": _safe_tool_event_trace(frames),
                    "model_text_persisted": False,
                },
            }
        state = asyncio.run(
            _inspect_run09_state(
                workspace_dir,
                fixture=fixture,
                frames=frames,
                initial_job_ids=initial_job_ids,
            )
        )
        return {
            "schema_version": "1.0",
            "run_id": "RUN-09",
            "ok": True,
            "source_head": _source_head(),
            "provider": {
                "provider_id": str(configured_model.get("provider_id") or ""),
                "model": str(configured_model.get("model") or ""),
                "billing_basis": "user subscription",
                "per_run_monetary_cost": "unavailable",
            },
            "input": {
                "transport": "stdin",
                "sha256": hashlib.sha256(
                    normalized_prompt.encode("utf-8")
                ).hexdigest(),
                "persisted": False,
            },
            "migration": migration,
            "assertions": state,
            "timing_ms": {
                "backend_ready": backend_ready_ms,
                "provider_turn": provider_turn_ms,
                "total": round((time.monotonic() - total_started) * 1000),
            },
            "sse": {
                "frame_count": len(frames),
                "frame_types": sorted(
                    {str(frame.get("type") or "") for _, frame in frames}
                ),
                "frame_trace": provider_smoke._safe_frame_trace(frames),
                "tool_trace": _safe_tool_event_trace(frames),
                "model_text_persisted": False,
            },
        }
    finally:
        provider_smoke._terminate(process)
        shutil.rmtree(smoke_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-working-dir", type=Path, required=True)
    parser.add_argument("--source-secret-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args()
    prompt = sys.stdin.readline()
    result = run(
        source_working_dir=args.source_working_dir,
        source_secret_dir=args.source_secret_dir,
        agent_id=args.agent_id,
        timeout=args.timeout,
        prompt=prompt,
    )
    if args.evidence_output is not None:
        args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
