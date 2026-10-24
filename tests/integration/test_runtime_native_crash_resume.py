# -*- coding: utf-8 -*-
"""Crash and safely resume a real Native Runtime tool transaction."""

from __future__ import annotations

import operator

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

import pytest

from lantai.app.recovery_coordinator import RecoveryCoordinator
from lantai.app.runner.models import ChatSpec
from lantai.app.runner.task_tracker import TaskTracker
from lantai.app.routers import runtime as runtime_router
from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.converters.legacy_messages import (
    legacy_stream_item_to_channel_events,
    runtime_event_to_legacy_stream_item,
)
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectRecoveryCapability,
)
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import ModelMessage, ModelResponse, ModelStreamEvent
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.tools import LantaiToolRegistry, ToolSpec


ROOT = Path(__file__).resolve().parents[2]
SERVICE_SCRIPT = ROOT / "tests" / "support" / "runtime_effect_service.py"
WORKER_SCRIPT = ROOT / "tests" / "support" / "runtime_native_crash_worker.py"


class _ChatManager:
    def __init__(self, run_id: str) -> None:
        self.chat = ChatSpec(
            id="chat-native-crash",
            session_id="console:chat-native-crash",
            user_id="user-native-crash",
            channel="console",
            meta={
                "last_turn_run_id": run_id,
                "last_turn_status": "running",
                "pending_turn": {"run_id": run_id, "status": "running"},
            },
        )

    async def list_chats(self):
        return [self.chat]

    async def merge_chat_meta(self, _chat_id, values, remove_keys=()):
        meta = dict(self.chat.meta)
        meta.update(values)
        for key in remove_keys:
            meta.pop(key, None)
        self.chat = self.chat.model_copy(update={"meta": meta})
        return self.chat


class _ResumeModel:
    provider_id = "fixture"
    model_name = "native-safe-resume"

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        self.saw_bound_receipt = False
        self.call_count = 0

    async def stream(self, request):
        self.call_count += 1
        serialized = json.dumps(
            [message.model_dump(mode="json") for message in request.messages],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.saw_bound_receipt = (
            self.operation_id in serialized
            and '"role": "tool"' in serialized
            and '"tool_call_id": "call-native-write"' in serialized
        )
        if not self.saw_bound_receipt:
            raise AssertionError(
                "resume model did not receive the bound probe receipt",
            )
        yield ModelStreamEvent(
            kind="message_completed",
            message_id="msg-native-resumed",
            response=ModelResponse(
                message=ModelMessage.text(
                    "assistant",
                    (
                        "The committed operation was recovered from its durab"
                        "le receipt."
                    ),
                ).model_copy(update={"id": "msg-native-resumed"}),
            ),
        )

    async def complete(self, _request):
        return ModelResponse(message=ModelMessage.text("assistant", "unused"))


class _NativeResumeRunner:
    def __init__(self, state_root: Path, operation_id: str) -> None:
        self.state_root = state_root
        self.model = _ResumeModel(operation_id)
        self.tool_executions: list[dict[str, object]] = []

    async def stream_query(self, request):
        run_id = str(request.channel_meta["run_id"])
        contract = await RuntimeRunStore(self.state_root).load_contract(run_id)
        assert contract is not None and contract.checkpoint is not None
        registry = LantaiToolRegistry()

        def mutate(nonce: str) -> dict[str, Any]:
            self.tool_executions.append({"nonce": nonce})
            raise AssertionError("a probed committed effect was replayed")

        registry.register_tool(
            ToolSpec(
                name="fixture_non_idempotent_write",
                input_schema={
                    "type": "object",
                    "properties": {"nonce": {"type": "string"}},
                    "required": ["nonce"],
                },
                effect_recovery=RuntimeEffectRecoveryCapability(
                    external_side_effect=True,
                    idempotency="not_idempotent",
                    probe="tool_specific",
                    compensation="none",
                    on_ambiguity="probe_required",
                ),
            ),
            mutate,
        )
        native = NativeLantaiRuntime(
            model_client_factory=lambda _agent_id: self.model,
            tool_registry=registry,
            effect_journal=RuntimeEffectJournal(self.state_root),
            progressive_tools=False,
        )
        runtime_input = RuntimeInput(
            request=RuntimeRequestContext(
                agent_id="default",
                session_id=request.session_id,
                user_id=request.user_id,
                channel=request.channel,
                chat_id=str(request.channel_meta["chat_id"]),
                run_id=run_id,
                root_session_id=request.root_session_id,
                workspace_dir=str(self.state_root),
            ),
            messages=[
                RuntimeMessage.text("user", contract.task_contract.objective),
            ],
            metadata={
                "state_workspace_dir": str(self.state_root),
                "attempt_id": contract.attempt_id,
                "runtime_revision": contract.revision,
                "runtime_lease_owner": contract.lease_owner,
                "runtime_attempt_reason": contract.attempt_reason,
                "runtime_checkpoint": contract.checkpoint.model_dump(
                    mode="json",
                ),
                "task_contract": contract.task_contract.model_dump(
                    mode="json",
                ),
            },
        )
        started_message_ids: set[str] = set()
        streamed_message_texts: dict[str, str] = {}
        async for event in native.run_turn(runtime_input):
            legacy_item = runtime_event_to_legacy_stream_item(event)
            if legacy_item is None:
                continue
            for channel_event in legacy_stream_item_to_channel_events(
                legacy_item,
                started_message_ids=started_message_ids,
                streamed_message_texts=streamed_message_texts,
            ):
                yield channel_event


def _wait_for_json(
    path: Path,
    *,
    timeout: float = 10,
    process: subprocess.Popen[str] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            last_error = exc
            if process is not None and process.poll() is not None:
                output, error = process.communicate(timeout=2)
                raise AssertionError(
                    (
                        "process exited before durable JSON "
                        f"{path}"
                        "; exit="
                        f"{process.returncode}"
                        "; stdout="
                        f"{output!r}"
                        "; stderr="
                        f"{error!r}"
                    ),
                ) from exc
            time.sleep(0.02)
    raise AssertionError(
        f"timed out waiting for durable barrier {path}: {last_error}",
    )


def _python_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    entries = [str(ROOT / "src"), str(ROOT)]
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["LANTAI_WORKING_DIR"] = str(home / "lantai-state")
    return env


def _service_get(service_url: str, path: str) -> dict[str, Any]:
    with urlopen(f"{service_url}{path}", timeout=5) as response:  # noqa: S310
        return json.loads(response.read())


def _receipt_hash(receipt: dict[str, object]) -> str:
    canonical = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


# pylint: disable=R0915
@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
@pytest.mark.asyncio
async def test_native_tool_transaction_crash_probe_and_resume_without_replay(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "runtime-state"
    service_state = tmp_path / "external-service.json"
    service_ready = tmp_path / "external-service-ready.json"
    barrier_file = tmp_path / "native-worker-barrier.json"
    nonce = uuid4().hex
    run_id = f"run-native-crash-{nonce[:8]}"
    tool_call_id = "call-native-write"
    effect_id = f"effect:{tool_call_id}"
    operation_id = f"operation:{nonce}"
    env = _python_env(tmp_path / "isolated-home")

    service = subprocess.Popen(
        [
            sys.executable,
            str(SERVICE_SCRIPT),
            "--state-file",
            str(service_state),
            "--ready-file",
            str(service_ready),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker: subprocess.Popen[str] | None = None
    try:
        ready = _wait_for_json(service_ready, process=service)
        service_url = str(ready["url"])
        worker = subprocess.Popen(
            [
                sys.executable,
                str(WORKER_SCRIPT),
                "--state-root",
                str(state_root),
                "--project-root",
                str(state_root),
                "--barrier-file",
                str(barrier_file),
                "--service-url",
                service_url,
                "--run-id",
                run_id,
                "--tool-call-id",
                tool_call_id,
                "--nonce",
                nonce,
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            barrier = _wait_for_json(barrier_file)
        except AssertionError as exc:
            output, error = worker.communicate(timeout=2)
            raise AssertionError(
                f"{exc}; worker exit={worker.returncode}; "
                f"stdout={output!r}; stderr={error!r}",
            ) from exc
        assert int(barrier["target_worker_pid"]) == worker.pid
        assert barrier["run_id"] == run_id
        assert barrier["attempt_id"]
        assert barrier["tool_call_id"] == tool_call_id
        assert barrier["effect_id"] == effect_id
        assert barrier["effect_phase"] == "running"
        assert barrier["checkpoint_phase"] == "before_tool"
        assert barrier["operation_id"] == operation_id

        os.kill(worker.pid, signal.SIGKILL)
        assert worker.wait(timeout=5) == -signal.SIGKILL
        before_recovery = _service_get(service_url, "/snapshot")
        assert len(before_recovery["mutations"]) == 1

        chat_manager = _ChatManager(run_id)
        runner = _NativeResumeRunner(state_root, operation_id)
        workspace = SimpleNamespace(
            workspace_dir=state_root,
            agent_id="default",
            chat_manager=chat_manager,
            task_tracker=TaskTracker(),
            runner=runner,
        )
        monkeypatch.setattr(
            runtime_router,
            "_workspace_for_request",
            lambda _request: asyncio.sleep(0, result=workspace),
        )

        report = await RecoveryCoordinator(workspace).recover()
        stalled = await RuntimeRunStore(state_root).load_contract(run_id)
        assert stalled is not None and stalled.checkpoint is not None
        assert report.scanned == 1 and report.stalled == 1
        assert stalled.status == "stalled"
        assert stalled.error is not None
        assert stalled.error.code == "recovery_effect_unknown"
        ambiguous = RuntimeEffectJournal(state_root).latest_effect(
            run_id,
            stalled.attempt_id,
            effect_id,
        )
        assert ambiguous is not None and ambiguous.status == "ambiguous"
        assert len(_service_get(service_url, "/snapshot")["mutations"]) == 1

        probe = _service_get(
            service_url,
            "/probe?" + urlencode({"operation_id": operation_id}),
        )
        receipt = dict(probe["receipt"])
        assert probe["committed"] is True
        assert receipt["ordinal"] == 1
        resolution = await runtime_router.resolve_runtime_run_effect(
            SimpleNamespace(),
            run_id,
            effect_id,
            runtime_router.RuntimeEffectResolveRequest(
                resolution="probe_committed",
                reason="independent receipt probe confirmed the mutation",
                result_hash=_receipt_hash(receipt),
                expected_attempt_id=stalled.attempt_id,
                expected_revision=stalled.revision,
                expected_effect_revision=ambiguous.revision,
                chat_id=chat_manager.chat.id,
                session_id=chat_manager.chat.session_id,
                workspace_dir=str(state_root),
            ),
        )
        assert resolution["effect_status"] == "probed_committed"

        committed = RuntimeEffectJournal(state_root).latest_effect(
            run_id,
            stalled.attempt_id,
            effect_id,
        )
        assert committed is not None and committed.status == "probed_committed"
        repair = await runtime_router.repair_runtime_run_effect(
            SimpleNamespace(),
            run_id,
            effect_id,
            runtime_router.RuntimeEffectRepairRequest(
                action="rebuild_checkpoint",
                reason="bind the trusted probe result before resuming",
                expected_attempt_id=stalled.attempt_id,
                expected_revision=stalled.revision,
                expected_effect_revision=committed.revision,
                chat_id=chat_manager.chat.id,
                session_id=chat_manager.chat.session_id,
                workspace_dir=str(state_root),
                tool_result={
                    "call_id": tool_call_id,
                    "name": "fixture_non_idempotent_write",
                    "ok": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                probe,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        },
                    ],
                    "metadata": {
                        "structured_content": probe,
                        "probe_verified": True,
                    },
                },
            ),
        )
        assert repair["checkpoint"]["next_execution_position"] == "after_tool"

        repaired = await RuntimeRunStore(state_root).load_contract(run_id)
        assert repaired is not None and repaired.checkpoint is not None
        assert repaired.status == "stalled"
        assert repaired.checkpoint.pending_tool_call is None
        continuation = await runtime_router.continue_runtime_run(
            SimpleNamespace(),
            run_id,
            runtime_router.RuntimeRunContinueRequest(
                mode="resume",
                expected_attempt_id=repaired.attempt_id,
                expected_revision=repaired.revision,
                chat_id=chat_manager.chat.id,
                session_id=chat_manager.chat.session_id,
                workspace_dir=str(state_root),
            ),
        )
        assert continuation["attempt_prepared"] is True
        assert continuation["reason"] == "resume_requires_frontend_resubmit"

        queued = await RuntimeRunStore(state_root).load_contract(run_id)
        assert queued is not None and queued.status == "queued"
        assert queued.attempt_reason == "resume"
        coordinator = RecoveryCoordinator(workspace)
        dispatched = await coordinator.dispatch_prepared_attempt(
            chat_manager.chat,
            run_id,
            state_workspace_dir=state_root,
        )
        assert dispatched.status == "running"
        assert await workspace.task_tracker.wait_all_done(timeout=15)

        finished = await RuntimeRunStore(state_root).load_contract(run_id)
        after_resume = _service_get(service_url, "/snapshot")
        assert finished is not None
        assert finished.status == "succeeded"
        assert operator.eq(runner.tool_executions, [])
        assert runner.model.call_count == 1
        assert runner.model.saw_bound_receipt is True
        assert len(after_resume["mutations"]) == 1
        assert chat_manager.chat.meta["last_turn_status"] == "completed"
        assert "pending_turn" not in chat_manager.chat.meta

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "crashed_attempt_id": stalled.attempt_id,
                    "resumed_attempt_id": finished.attempt_id,
                    "worker_pid": worker.pid,
                    "service_pid": service.pid,
                    "sigkill_exit": worker.returncode,
                    "checkpoint_before_kill": barrier["checkpoint_phase"],
                    "effect_before_kill": barrier["effect_phase"],
                    "effect_after_recovery": ambiguous.status,
                    "effect_after_probe": committed.status,
                    "checkpoint_after_repair": repair["checkpoint"][
                        "next_execution_position"
                    ],
                    "external_actions_before_recovery": 1,
                    "external_actions_after_recovery": 1,
                    "external_actions_after_resume": len(
                        after_resume["mutations"],
                    ),
                    "resume_tool_executions": len(runner.tool_executions),
                    "resume_model_calls": runner.model.call_count,
                    "resume_model_saw_bound_receipt": (
                        runner.model.saw_bound_receipt
                    ),
                    "final_runtime_status": finished.status,
                    "final_chat_status": chat_manager.chat.meta[
                        "last_turn_status"
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        service.terminate()
        try:
            service.wait(timeout=5)
        except subprocess.TimeoutExpired:
            service.kill()
            service.wait(timeout=5)


# pylint: enable=R0915
