"""Behavior locks for the RUN-09 real-provider smoke harness."""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app.runner.manager import ChatManager
from lantai.app.runner.repo import JsonChatRepository
from lantai.presentation.jobs import (
    PresentationJobStore,
    cancel_active_job_for_runtime_run,
)
from lantai.presentation.models import JobStatus
from lantai.runtime.deliverable_contract import resolve_pending_deliverable
from lantai.runtime.runs import (
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "source_pending_deliverable_replacement_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "pending_deliverable_replacement_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run09_fixture_binds_one_exact_ppt_job_to_the_source_run(
    tmp_path: Path,
) -> None:
    module = _load_module()
    session_id = "console:run09-test"

    fixture = asyncio.run(
        module._seed_pending_presentation_fixture(
            tmp_path,
            session_id=session_id,
        )
    )
    state_workspace_dir = Path(fixture["state_workspace_dir"])

    source = asyncio.run(
        RuntimeRunStore(state_workspace_dir).load_contract(
            fixture["source_run_id"]
        )
    )
    assert source is not None and source.task_contract is not None
    assert source.status == "failed"
    assert source.delivery_status == "not_delivered"
    assert source.task_contract.deliverable_contract is not None
    assert source.task_contract.deliverable_contract.accepted_extensions == (
        ".pptx",
    )
    assert asyncio.run(
        RuntimeRunStore(tmp_path).load_contract(fixture["source_run_id"])
    ) is None
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = asyncio.run(manager.get_chat(fixture["chat_id"]))
    assert chat is not None
    assert (chat.meta or {}).get("topic_workspace_dir") == str(
        state_workspace_dir
    )
    jobs = PresentationJobStore(
        state_workspace_dir / "presentation-jobs",
        read_only=True,
    )
    assert jobs.get(fixture["pending_job_id"]).runtime_run_id == fixture[
        "source_run_id"
    ]
    assert jobs.get(fixture["unrelated_job_id"]).runtime_run_id != fixture[
        "source_run_id"
    ]


def test_run09_inspection_requires_replace_exact_cancel_and_no_new_ppt(
    tmp_path: Path,
) -> None:
    module = _load_module()
    session_id = "console:run09-test"
    fixture = asyncio.run(
        module._seed_pending_presentation_fixture(
            tmp_path,
            session_id=session_id,
        )
    )
    state_workspace_dir = Path(fixture["state_workspace_dir"])
    jobs = PresentationJobStore(state_workspace_dir / "presentation-jobs")
    initial_job_ids = {
        record.job_id
        for record in jobs.list_records(fixture["conversation_id"])
    }
    current_run_id = "run-run09-current"
    current_attempt_id = f"{current_run_id}:attempt-1"
    now = datetime.now(timezone.utc)
    owner = RuntimeRunOwner(
        user_id="default",
        agent_id="default",
        session_id=session_id,
        workspace_id=str(state_workspace_dir),
        channel="console",
    )
    current_task = build_chat_task_contract(
        objective="RUN-09 text-only replacement fixture",
        client_turn_id="run09-current",
        complexity="standard",
        work_method_id="standard",
        run_id=current_run_id,
        attempt_id=current_attempt_id,
    )
    store = RuntimeRunStore(state_workspace_dir)
    asyncio.run(
        store.save_contract(
            RuntimeRunContract(
                run_id=current_run_id,
                attempt_id=current_attempt_id,
                source="chat",
                owner=owner,
                status="running",
                revision=0,
                created_at=now,
                started_at=now,
                heartbeat_at=now,
                task_contract=current_task,
                release_edition="official",
            ),
            expected_revision=None,
        )
    )
    source = asyncio.run(store.load_contract(fixture["source_run_id"]))
    assert source is not None and source.task_contract is not None
    inherited = source.task_contract.deliverable_contract
    assert inherited is not None
    asyncio.run(
        resolve_pending_deliverable(
            state_workspace_dir=state_workspace_dir,
            run_id=current_run_id,
            source_run_id=fixture["source_run_id"],
            source_contract_revision=source.revision,
            source_task_revision=source.task_contract.revision,
            pending_job_id=fixture["pending_job_id"],
            decision="replace",
            inherited_contract=inherited,
        )
    )
    assert cancel_active_job_for_runtime_run(
        state_workspace_dir,
        fixture["source_run_id"],
        fixture["pending_job_id"],
        conversation_id=fixture["conversation_id"],
    )
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    asyncio.run(
        manager.merge_chat_meta(
            fixture["chat_id"],
            {
                "last_turn_run_id": current_run_id,
                "last_turn_status": "succeeded",
                "last_turn_outcome_status": "delivered",
                "last_turn_delivery_status": "not_delivered",
            },
        )
    )
    frames = [
        (100, {"object": "response.output_text.delta", "type": "message"}),
        (250, {"object": "response", "status": "completed"}),
    ]

    evidence = asyncio.run(
        module._inspect_run09_state(
            tmp_path,
            fixture=fixture,
            frames=frames,
            initial_job_ids=initial_job_ids,
        )
    )

    assert evidence["decision"] == "replace"
    assert evidence["pending_job_status"] == JobStatus.CANCELLED.value
    assert evidence["unrelated_job_status"] == JobStatus.PENDING.value
    assert evidence["new_presentation_job_created"] is False
    assert evidence["pptx_file_count"] == 0
    assert evidence["current_deliverable_contract"] is None
    assert fixture["source_run_id"] not in str(evidence)


def test_run09_opaque_evidence_never_copies_raw_identifiers() -> None:
    module = _load_module()

    masked = module._opaque_id("run", "sensitive-runtime-identifier")

    assert masked.startswith("run-")
    assert "sensitive-runtime-identifier" not in masked
    assert len(masked) == len("run-") + 12


def test_run09_incomplete_inspection_is_content_free_and_scoped(
    tmp_path: Path,
) -> None:
    module = _load_module()
    fixture = asyncio.run(
        module._seed_pending_presentation_fixture(
            tmp_path,
            session_id="console:run09-incomplete-test",
        )
    )
    state_workspace_dir = Path(fixture["state_workspace_dir"])
    jobs = PresentationJobStore(
        state_workspace_dir / "presentation-jobs",
        read_only=True,
    )
    initial_job_ids = {
        record.job_id
        for record in jobs.list_records(fixture["conversation_id"])
    }

    evidence = asyncio.run(
        module._inspect_incomplete_run09_state(
            tmp_path,
            fixture=fixture,
            initial_job_ids=initial_job_ids,
        )
    )

    assert evidence["new_runtime_run_persisted"] is False
    assert evidence["resolution_decision"] is None
    assert evidence["pending_job_status"] == JobStatus.PENDING.value
    assert evidence["unrelated_job_status"] == JobStatus.PENDING.value
    assert evidence["presentation_job_count_before"] == 2
    assert evidence["presentation_job_count_after"] == 2
    assert evidence["pptx_file_count"] == 0
    assert fixture["source_run_id"] not in str(evidence)


def test_run09_tool_trace_never_persists_arguments_outputs_or_errors() -> None:
    module = _load_module()
    secret = "must-not-be-persisted"
    frames = [
        (
            100,
            {
                "type": "plugin_call",
                "content": [
                    {
                        "type": "data",
                        "data": {
                            "call_id": "raw-call-id",
                            "name": "resolve_pending_deliverable",
                            "arguments": {"prompt": secret},
                        },
                    }
                ],
            },
        ),
        (
            200,
            {
                "type": "plugin_call_output",
                "content": [
                    {
                        "type": "data",
                        "data": {
                            "call_id": "raw-call-id",
                            "name": "resolve_pending_deliverable",
                            "output": secret,
                            "ok": False,
                            "status": "failed",
                            "error": {"message": secret},
                        },
                    }
                ],
            },
        ),
    ]

    trace = module._safe_tool_event_trace(frames)

    assert [item["call"] for item in trace] == ["call1", "call1"]
    assert trace[0]["tool_name"] == "resolve_pending_deliverable"
    assert trace[1]["ok"] is False
    assert trace[1]["status"] == "failed"
    assert "raw-call-id" not in str(trace)
    assert secret not in str(trace)


def test_run09_provider_turn_has_true_wall_clock_deadline() -> None:
    module = _load_module()

    class EndlessHeartbeatHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            content_length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(content_length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.05)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), EndlessHeartbeatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider_smoke = SimpleNamespace(
        _configured_provider_request_payload=lambda *args, **kwargs: {
            "prompt": kwargs["prompt"]
        }
    )
    helpers = SimpleNamespace(
        _state_headers=lambda state: {
            "X-Lantai-Desktop-Token": state["desktop_token"]
        },
        _parse_sse_data_lines=lambda lines, source: {},
    )
    started = time.monotonic()
    try:
        with pytest.raises(module.ProviderTurnFailure) as caught:
            module._read_seeded_console_sse(
                provider_smoke=provider_smoke,
                helpers=helpers,
                base_url=f"http://127.0.0.1:{server.server_port}",
                desktop_token="test-token",
                timeout=0.25,
                session_id="console:run09-deadline-test",
                chat_id="chat-run09-deadline-test",
                prompt="content-never-persisted",
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    elapsed = time.monotonic() - started
    assert caught.value.kind == "wall_clock_timeout"
    assert caught.value.elapsed_ms < 1500
    assert elapsed < 2
    assert "content-never-persisted" not in str(caught.value)
