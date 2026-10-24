# -*- coding: utf-8 -*-
"""L3 steering locks with real child processes and an external effect
service.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from urllib.request import Request, urlopen

import pytest

from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)
from lantai.runtime.tool_response import FileBlock, TextBlock, ToolResponse
from lantai.runtime.tools import (
    LantaiToolRegistry,
    ToolCall,
    ToolOutputSemantics,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolSpec,
)
from lantai.runtime.turn_control import RuntimeTurnControlStore
from lantai.security.tool_guard.approval import ApprovalDecision


ROOT = Path(__file__).resolve().parents[2]
EFFECT_SERVICE = ROOT / "tests" / "support" / "runtime_effect_service.py"
TOOL_PROCESS = ROOT / "tests" / "support" / "steering_tool_process.py"


class _ScriptedClient:
    provider_id = "test"
    model_name = "steering-l3"

    def __init__(self, scripts: list[list[ModelStreamEvent]]) -> None:
        self.scripts = list(scripts)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest):
        self.requests.append(request)
        for event in self.scripts.pop(0):
            yield event


def _completed(text: str, *, message_id: str) -> ModelStreamEvent:
    return ModelStreamEvent(
        kind="message_completed",
        message_id=message_id,
        response=ModelResponse(
            message=ModelMessage(
                id=message_id,
                role="assistant",
                content=[ModelContentBlock(type="text", text=text)],
            ),
        ),
    )


def _tool_turn(*calls: ToolCall, message_id: str) -> list[ModelStreamEvent]:
    return [
        *[
            ModelStreamEvent(
                kind="tool_call_completed",
                message_id=message_id,
                tool_call=call,
            )
            for call in calls
        ],
        ModelStreamEvent(
            kind="message_completed",
            message_id=message_id,
            response=ModelResponse(
                message=ModelMessage(id=message_id, role="assistant"),
            ),
        ),
    ]


def _runtime_input(tmp_path: Path, *, attempt_id: str) -> RuntimeInput:
    return RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="chat-steering-l3",
            user_id="user-steering-l3",
            run_id="run-steering-l3",
            workspace_dir=str(tmp_path / "project"),
        ),
        messages=[RuntimeMessage.text("user", "执行原任务")],
        metadata={
            "state_workspace_dir": str(tmp_path / "state"),
            "attempt_id": attempt_id,
        },
    )


async def _collect(runtime: NativeLantaiRuntime, runtime_input: RuntimeInput):
    return [event async for event in runtime.run_turn(runtime_input)]


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
    raise AssertionError(f"timed out waiting for {path}: {last_error}")


def _python_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    entries = [str(ROOT / "src"), str(ROOT)]
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(isolated_home)
    env["LANTAI_WORKING_DIR"] = str(isolated_home / "lantai-state")
    return env


@contextmanager
def _effect_service(tmp_path: Path) -> Iterator[str]:
    ready_file = tmp_path / "effect-service-ready.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(EFFECT_SERVICE),
            "--state-file",
            str(tmp_path / "effect-service-state.json"),
            "--ready-file",
            str(ready_file),
        ],
        cwd=ROOT,
        env=_python_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = _wait_for_json(ready_file, process=process)
        assert int(ready["pid"]) == process.pid
        yield str(ready["url"])
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _snapshot(service_url: str) -> dict[str, Any]:
    with urlopen(
        f"{service_url}/snapshot",
        timeout=5,
    ) as response:  # noqa: S310
        return json.loads(response.read())


def _post_mutation(
    service_url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request = Request(  # noqa: S310
        f"{service_url}/mutate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:  # noqa: S310
        return json.loads(response.read())


def _append_redirect(
    tmp_path: Path,
    *,
    attempt_id: str,
    command_id: str,
    text: str,
):
    return RuntimeTurnControlStore(tmp_path / "state").append_input(
        command_id=command_id,
        run_id="run-steering-l3",
        attempt_id=attempt_id,
        chat_id="chat-steering-l3",
        message=RuntimeMessage.text("user", text),
        intent="redirect",
    )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX child termination")
@pytest.mark.asyncio
async def test_redirect_reaps_read_only_process_and_fences_later_effect(
    tmp_path: Path,
) -> None:
    """ST-FX-01/02: cancel the active read and never start the queued write."""

    ready_file = tmp_path / "lookup-ready.json"
    stopped_file = tmp_path / "lookup-stopped.json"
    child: asyncio.subprocess.Process | None = None
    child_ready = asyncio.Event()

    async def slow_lookup() -> str:
        nonlocal child
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(TOOL_PROCESS),
            "--mode",
            "wait",
            "--ready-file",
            str(ready_file),
            "--stopped-file",
            str(stopped_file),
            cwd=ROOT,
            env=_python_env(tmp_path),
        )
        await asyncio.to_thread(_wait_for_json, ready_file)
        child_ready.set()
        try:
            await child.wait()
            return "stale lookup"
        except asyncio.CancelledError:
            child.terminate()
            await child.wait()
            raise

    with _effect_service(tmp_path) as service_url:
        writes = 0

        async def external_write() -> str:
            nonlocal writes
            writes += 1
            await asyncio.to_thread(
                _post_mutation,
                service_url,
                {"effect_id": "fx01", "operation_id": "fx01-write"},
            )
            return "written"

        registry = LantaiToolRegistry()
        registry.register_tool(
            ToolSpec(name="slow_lookup", read_only=True),
            slow_lookup,
        )
        registry.register_tool(ToolSpec(name="external_write"), external_write)
        client = _ScriptedClient(
            [
                _tool_turn(
                    ToolCall(id="call-lookup", name="slow_lookup"),
                    ToolCall(id="call-write", name="external_write"),
                    message_id="msg-old-batch",
                ),
                [_completed("已按新要求停止旧批次。", message_id="msg-new")],
            ],
        )
        runtime = NativeLantaiRuntime(
            model_client_factory=lambda _agent_id: client,
            tool_registry=registry,
        )
        task = asyncio.create_task(
            _collect(
                runtime,
                _runtime_input(tmp_path, attempt_id="attempt-fx01"),
            ),
        )
        try:
            await asyncio.wait_for(child_ready.wait(), timeout=3)
            _append_redirect(
                tmp_path,
                attempt_id="attempt-fx01",
                command_id="redirect-fx01",
                text="停止查询，也不要执行排队中的外部写入。",
            )
            events = await asyncio.wait_for(task, timeout=5)
        finally:
            if child is not None and child.returncode is None:
                child.terminate()
                await child.wait()

        assert child is not None and child.returncode == 0
        stopped = _wait_for_json(stopped_file)
        assert stopped == {"pid": child.pid, "status": "terminated"}
        assert writes == 0
        assert _snapshot(service_url)["mutations"] == []
        completed = [
            event for event in events if event.type == "tool.call.completed"
        ]
        assert [event.item_id for event in completed[:2]] == [
            "call-lookup",
            "call-write",
        ]
        assert completed[0].payload["error_code"] == "steered"
        assert (
            completed[0].payload["metadata"]["invocation_status"]
            == "interrupted"
        )
        assert completed[0].payload["metadata"]["side_effect_outcome"] == (
            "known_not_applied"
        )
        assert completed[1].payload["error_code"] == "steered_before_effect"
        assert (
            completed[1].payload["metadata"]["invocation_status"]
            == "not_started"
        )
        assert "不要执行排队" in client.requests[1].messages[-1].text_content()


@pytest.mark.skipif(
    os.name == "nt",
    reason="requires POSIX child process semantics",
)
@pytest.mark.asyncio
async def test_redirect_waits_for_atomic_local_commit_and_retains_result(
    tmp_path: Path,
) -> None:
    (
        "ST-FX-03: an atomic local write settles before the redir"
        "ect is applied."
    )

    ready_file = tmp_path / "atomic-ready.json"
    release_file = tmp_path / "atomic-release"
    done_file = tmp_path / "atomic-done.json"
    output_file = tmp_path / "project" / "candidate.txt"
    content = "atomic-candidate\n" * 256
    child: asyncio.subprocess.Process | None = None
    child_ready = asyncio.Event()

    async def atomic_write(file_path: str) -> ToolResponse:
        nonlocal child
        assert file_path == str(output_file)
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(TOOL_PROCESS),
            "--mode",
            "atomic-write",
            "--ready-file",
            str(ready_file),
            "--release-file",
            str(release_file),
            "--output-file",
            str(output_file),
            "--done-file",
            str(done_file),
            "--content",
            content,
            cwd=ROOT,
            env=_python_env(tmp_path),
        )
        await asyncio.to_thread(_wait_for_json, ready_file)
        child_ready.set()
        assert await child.wait() == 0
        return ToolResponse(
            content=[TextBlock(text=f"committed candidate: {file_path}")],
        )

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="atomic_write",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        ),
        atomic_write,
    )
    client = _ScriptedClient(
        [
            _tool_turn(
                ToolCall(
                    id="call-atomic",
                    name="atomic_write",
                    arguments={"file_path": str(output_file)},
                ),
                message_id="msg-atomic",
            ),
            [_completed("候选已保留，现已改向。", message_id="msg-after-atomic")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    task = asyncio.create_task(
        _collect(runtime, _runtime_input(tmp_path, attempt_id="attempt-fx03")),
    )
    try:
        await asyncio.wait_for(child_ready.wait(), timeout=3)
        assert not output_file.exists()
        _append_redirect(
            tmp_path,
            attempt_id="attempt-fx03",
            command_id="redirect-fx03",
            text="保留已开始写入的候选，然后改为本地检查。",
        )
        done, _ = await asyncio.wait({task}, timeout=0.15)
        assert not done
        release_file.write_text("release", encoding="utf-8")
        events = await asyncio.wait_for(task, timeout=5)
    finally:
        if child is not None and child.returncode is None:
            child.terminate()
            await child.wait()

    assert output_file.read_text(encoding="utf-8") == content
    done_payload = _wait_for_json(done_file)
    assert done_payload["status"] == "committed"
    assert done_payload["size"] == len(content.encode("utf-8"))
    completed = next(
        event
        for event in events
        if event.type == "tool.call.completed"
        and event.item_id == "call-atomic"
    )
    assert completed.status == "completed"
    assert completed.payload["metadata"]["side_effect_outcome"] == "committed"
    request_tail = json.dumps(
        client.requests[1].messages[-2].model_dump(mode="json"),
        ensure_ascii=False,
    )
    assert "committed candidate" in request_tail
    assert str(output_file) in request_tail


class _ApprovalPolicy:
    async def evaluate(self, tool_call, spec, context):
        del tool_call, spec, context
        return ToolPolicyOutcome(
            action="needs_approval",
            reason="requires explicit approval",
            policy_payload={"decision": "prompt"},
        )


class _ApprovalService:
    def __init__(self) -> None:
        self.created = asyncio.Event()
        self.future: asyncio.Future[ApprovalDecision] | None = None
        self.resolved: list[tuple[str, ApprovalDecision]] = []

    async def create_pending(self, **_kwargs):
        self.future = asyncio.get_running_loop().create_future()
        self.created.set()
        return SimpleNamespace(
            request_id="approval-steering-l3",
            future=self.future,
            timeout_seconds=None,
        )

    async def resolve_request(self, request_id, decision):
        self.resolved.append((request_id, decision))
        if self.future is not None and not self.future.done():
            self.future.set_result(decision)


@pytest.mark.asyncio
async def test_redirect_supersedes_old_approval_without_authorizing_new_params(
    tmp_path: Path,
) -> None:
    """ST-FX-05: a pending approval is denied before any external effect."""

    with _effect_service(tmp_path) as service_url:
        calls = 0

        async def approved_write(value: str) -> str:
            nonlocal calls
            calls += 1
            await asyncio.to_thread(
                _post_mutation,
                service_url,
                {"effect_id": "fx05", "operation_id": value},
            )
            return value

        registry = LantaiToolRegistry()
        registry.register_tool(
            ToolSpec(
                name="approved_write",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            ),
            approved_write,
        )
        registry.register_policy_middleware(_ApprovalPolicy())
        approval = _ApprovalService()
        client = _ScriptedClient(
            [
                _tool_turn(
                    ToolCall(
                        id="call-approved-old",
                        name="approved_write",
                        arguments={"value": "old-parameters"},
                    ),
                    message_id="msg-approved-old",
                ),
                [_completed("旧审批已作废。", message_id="msg-approved-new")],
            ],
        )
        runtime = NativeLantaiRuntime(
            model_client_factory=lambda _agent_id: client,
            tool_registry=registry,
            approval_service=approval,
            tool_policy_context=ToolPolicyContext(
                session_id="chat-steering-l3",
            ),
        )
        task = asyncio.create_task(
            _collect(
                runtime,
                _runtime_input(tmp_path, attempt_id="attempt-fx05"),
            ),
        )
        await asyncio.wait_for(approval.created.wait(), timeout=3)
        _append_redirect(
            tmp_path,
            attempt_id="attempt-fx05",
            command_id="redirect-fx05",
            text="参数已改变，不批准旧写入，也不要沿用旧许可。",
        )
        events = await asyncio.wait_for(task, timeout=5)

        assert calls == 0
        assert _snapshot(service_url)["mutations"] == []
        assert approval.resolved == [
            ("approval-steering-l3", ApprovalDecision.DENIED),
        ]
        completed = next(
            event for event in events if event.type == "tool.call.completed"
        )
        assert completed.payload["error_code"] == "steered_before_effect"
        assert (
            completed.payload["metadata"]["invocation_status"] == "not_started"
        )
        assert "不要沿用旧许可" in client.requests[1].messages[-1].text_content()


@pytest.mark.asyncio
async def test_started_batch_effect_settles_once_and_next_effect_is_fenced(
    tmp_path: Path,
) -> None:
    """ST-FX-06: keep the first external fact and block the second call."""

    with _effect_service(tmp_path) as service_url:
        first_effect_done = asyncio.Event()
        release_first = asyncio.Event()
        calls: list[str] = []

        async def external_write(label: str) -> str:
            calls.append(label)
            response = await asyncio.to_thread(
                _post_mutation,
                service_url,
                {"effect_id": f"fx06-{label}", "operation_id": label},
            )
            if label == "first":
                first_effect_done.set()
                await release_first.wait()
            return json.dumps(response, sort_keys=True)

        registry = LantaiToolRegistry()
        registry.register_tool(
            ToolSpec(
                name="external_write",
                input_schema={
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                },
            ),
            external_write,
        )
        client = _ScriptedClient(
            [
                _tool_turn(
                    ToolCall(
                        id="call-first",
                        name="external_write",
                        arguments={"label": "first"},
                    ),
                    ToolCall(
                        id="call-second",
                        name="external_write",
                        arguments={"label": "second"},
                    ),
                    message_id="msg-batch-effects",
                ),
                [_completed("首个作用已记账，第二个未开始。", message_id="msg-batch-new")],
            ],
        )
        runtime = NativeLantaiRuntime(
            model_client_factory=lambda _agent_id: client,
            tool_registry=registry,
        )
        task = asyncio.create_task(
            _collect(
                runtime,
                _runtime_input(tmp_path, attempt_id="attempt-fx06"),
            ),
        )
        await asyncio.wait_for(first_effect_done.wait(), timeout=3)
        assert len(_snapshot(service_url)["mutations"]) == 1
        _append_redirect(
            tmp_path,
            attempt_id="attempt-fx06",
            command_id="redirect-fx06",
            text="保留已发生的第一个作用，不要开始第二个。",
        )
        release_first.set()
        events = await asyncio.wait_for(task, timeout=5)

        assert calls == ["first"]
        mutations = _snapshot(service_url)["mutations"]
        assert [item["operation_id"] for item in mutations] == ["first"]
        completed = [
            event for event in events if event.type == "tool.call.completed"
        ]
        assert completed[0].item_id == "call-first"
        assert (
            completed[0].payload["metadata"]["side_effect_outcome"]
            == "committed"
        )
        assert completed[1].item_id == "call-second"
        assert completed[1].payload["error_code"] == "steered_before_effect"
        second_request_payload = json.dumps(
            [
                message.model_dump(mode="json")
                for message in client.requests[1].messages
            ],
            sort_keys=True,
        )
        assert "accepted" in second_request_payload


@pytest.mark.asyncio
async def test_redirect_blocks_unstarted_delivery_and_keeps_candidate(
    tmp_path: Path,
) -> None:
    """ST-FX-08: redirect before approval leaves a reusable local candidate."""

    candidate = tmp_path / "project" / "candidate.txt"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("reusable candidate", encoding="utf-8")
    with _effect_service(tmp_path) as service_url:
        deliveries = 0

        async def send_file_to_user(file_path: str) -> ToolResponse:
            nonlocal deliveries
            deliveries += 1
            await asyncio.to_thread(
                _post_mutation,
                service_url,
                {"effect_id": "fx08", "operation_id": file_path},
            )
            return ToolResponse(
                content=[
                    FileBlock(
                        filename=Path(file_path).name,
                        source={
                            "type": "url",
                            "url": Path(file_path).as_uri(),
                        },
                    ),
                ],
            )

        registry = LantaiToolRegistry()
        registry.register_tool(
            ToolSpec(
                name="send_file_to_user",
                input_schema={
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
                output_semantics=ToolOutputSemantics(
                    artifact_role="delivery",
                    argument_path_fields=("file_path",),
                ),
            ),
            send_file_to_user,
        )
        registry.register_policy_middleware(_ApprovalPolicy())
        approval = _ApprovalService()
        client = _ScriptedClient(
            [
                _tool_turn(
                    ToolCall(
                        id="call-send-candidate",
                        name="send_file_to_user",
                        arguments={"file_path": str(candidate)},
                    ),
                    message_id="msg-send-candidate",
                ),
                [_completed("候选保留在本地。", message_id="msg-keep-candidate")],
            ],
        )
        runtime = NativeLantaiRuntime(
            model_client_factory=lambda _agent_id: client,
            tool_registry=registry,
            approval_service=approval,
            tool_policy_context=ToolPolicyContext(
                session_id="chat-steering-l3",
            ),
        )
        task = asyncio.create_task(
            _collect(
                runtime,
                _runtime_input(tmp_path, attempt_id="attempt-fx08"),
            ),
        )
        await asyncio.wait_for(approval.created.wait(), timeout=3)
        _append_redirect(
            tmp_path,
            attempt_id="attempt-fx08",
            command_id="redirect-fx08",
            text="先不要发送，改为本地继续处理。",
        )
        events = await asyncio.wait_for(task, timeout=5)

        assert deliveries == 0
        assert _snapshot(service_url)["mutations"] == []
        assert candidate.read_text(encoding="utf-8") == "reusable candidate"
        completed = next(
            event for event in events if event.type == "tool.call.completed"
        )
        assert completed.payload["error_code"] == "steered_before_effect"
        assert completed.payload["metadata"]["side_effect_outcome"] == (
            "known_not_applied"
        )


@pytest.mark.asyncio
async def test_redirect_after_delivery_preserves_history_without_resend(
    tmp_path: Path,
) -> None:
    """ST-FX-09: a delivered asset remains one real effect after redirect."""

    candidate = tmp_path / "project" / "delivered.txt"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("delivered once", encoding="utf-8")
    with _effect_service(tmp_path) as service_url:
        delivered = asyncio.Event()
        release = asyncio.Event()
        deliveries = 0

        async def send_file_to_user(file_path: str) -> ToolResponse:
            nonlocal deliveries
            deliveries += 1
            response = await asyncio.to_thread(
                _post_mutation,
                service_url,
                {"effect_id": "fx09", "operation_id": file_path},
            )
            delivered.set()
            await release.wait()
            return ToolResponse(
                content=[
                    FileBlock(
                        filename=Path(file_path).name,
                        source={
                            "type": "url",
                            "url": Path(file_path).as_uri(),
                        },
                    ),
                    TextBlock(text=json.dumps(response, sort_keys=True)),
                ],
            )

        registry = LantaiToolRegistry()
        registry.register_tool(
            ToolSpec(
                name="send_file_to_user",
                input_schema={
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
                output_semantics=ToolOutputSemantics(
                    artifact_role="delivery",
                    argument_path_fields=("file_path",),
                ),
            ),
            send_file_to_user,
        )
        client = _ScriptedClient(
            [
                _tool_turn(
                    ToolCall(
                        id="call-send-once",
                        name="send_file_to_user",
                        arguments={"file_path": str(candidate)},
                    ),
                    message_id="msg-send-once",
                ),
                [_completed("已记录既有发送，后续不重发。", message_id="msg-no-resend")],
            ],
        )
        runtime = NativeLantaiRuntime(
            model_client_factory=lambda _agent_id: client,
            tool_registry=registry,
        )
        task = asyncio.create_task(
            _collect(
                runtime,
                _runtime_input(tmp_path, attempt_id="attempt-fx09"),
            ),
        )
        await asyncio.wait_for(delivered.wait(), timeout=3)
        assert len(_snapshot(service_url)["mutations"]) == 1
        _append_redirect(
            tmp_path,
            attempt_id="attempt-fx09",
            command_id="redirect-fx09",
            text="文件既然已经送达，不要撤销也不要重发，转去总结。",
        )
        release.set()
        events = await asyncio.wait_for(task, timeout=5)

        assert deliveries == 1
        mutations = _snapshot(service_url)["mutations"]
        assert len(mutations) == 1
        assert mutations[0]["operation_id"] == str(candidate)
        completed = next(
            event
            for event in events
            if event.type == "tool.call.completed"
            and event.item_id == "call-send-once"
        )
        assert completed.status == "completed"
        assert (
            completed.payload["metadata"]["side_effect_outcome"] == "committed"
        )
        assert "不要撤销也不要重发" in client.requests[1].messages[-1].text_content()
        assert "delivered.txt" in json.dumps(
            client.requests[1].messages[-2].model_dump(mode="json"),
            ensure_ascii=False,
        )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX child termination")
@pytest.mark.asyncio
async def test_redirect_joins_two_collaboration_children_under_one_parent(
    tmp_path: Path,
) -> None:
    """ST-FX-07: two child processes keep revision and parent ownership."""

    child_ready = asyncio.Event()
    cleanup_done = asyncio.Event()
    children: list[asyncio.subprocess.Process] = []
    ready_files = [tmp_path / f"child-{index}-ready.json" for index in (1, 2)]
    stopped_files = [
        tmp_path / f"child-{index}-stopped.json" for index in (1, 2)
    ]

    async def collaborate_with_agents(tasks: list[dict[str, str]]):
        assert [task["label"] for task in tasks] == ["甲", "乙"]
        try:
            for ready_file, stopped_file in zip(ready_files, stopped_files):
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(TOOL_PROCESS),
                    "--mode",
                    "wait",
                    "--ready-file",
                    str(ready_file),
                    "--stopped-file",
                    str(stopped_file),
                    cwd=ROOT,
                    env=_python_env(tmp_path),
                )
                children.append(child)
            await asyncio.gather(
                *[
                    asyncio.to_thread(_wait_for_json, ready_file)
                    for ready_file in ready_files
                ],
            )
            child_ready.set()
            yield ToolResponse(
                content=[TextBlock(text="两个子任务均在运行")],
                metadata={
                    "collaboration": {
                        "batch_id": "batch-fx07",
                        "revision": 1,
                        "status": "running",
                        "phase": "executing",
                        "members": [
                            {
                                "member_id": f"member-{index}",
                                "agent_id": f"agent-{index}",
                                "status": "running",
                            }
                            for index in (1, 2)
                        ],
                    },
                },
                stream=True,
                is_last=False,
            )
            await asyncio.Event().wait()
        finally:
            for child in children:
                if child.returncode is None:
                    child.terminate()
            await asyncio.gather(*[child.wait() for child in children])
            cleanup_done.set()

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="collaborate_with_agents",
            input_schema={
                "type": "object",
                "properties": {"tasks": {"type": "array"}},
                "required": ["tasks"],
            },
            metadata={"progressive_tool": "collaboration"},
        ),
        collaborate_with_agents,
    )
    client = _ScriptedClient(
        [
            _tool_turn(
                ToolCall(
                    id="call-collaboration",
                    name="collaborate_with_agents",
                    arguments={
                        "tasks": [
                            {
                                "to_agent": "agent-1",
                                "text": "甲任务",
                                "label": "甲",
                            },
                            {
                                "to_agent": "agent-2",
                                "text": "乙任务",
                                "label": "乙",
                            },
                        ],
                    },
                ),
                message_id="msg-collaboration",
            ),
            [_completed("子任务已停止并归属新方向。", message_id="msg-collaboration-new")],
        ],
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
        progressive_tools=True,
    )
    runtime_input = _runtime_input(tmp_path, attempt_id="attempt-fx07")
    task = asyncio.create_task(_collect(runtime, runtime_input))
    try:
        await asyncio.wait_for(child_ready.wait(), timeout=5)
        _append_redirect(
            tmp_path,
            attempt_id="attempt-fx07",
            command_id="redirect-fx07",
            text="停止旧协作分工，两个子任务都不要再回写旧结果。",
        )
        events = await asyncio.wait_for(task, timeout=5)
    finally:
        for child in children:
            if child.returncode is None:
                child.terminate()
                await child.wait()

    assert cleanup_done.is_set()
    assert len(children) == 2
    assert all(child.returncode == 0 for child in children)
    stopped = [_wait_for_json(path) for path in stopped_files]
    assert [item["pid"] for item in stopped] == [
        child.pid for child in children
    ]
    completed = next(
        event
        for event in events
        if event.type == "tool.call.completed"
        and event.item_id == "call-collaboration"
    )
    assert completed.payload["error_code"] == "steered"
    assert completed.payload["metadata"]["invocation_status"] == "interrupted"
    collaboration_deltas = [
        event
        for event in events
        if event.type == "tool.call.delta"
        and event.item_id == "call-collaboration"
    ]
    assert len(collaboration_deltas) == 1
    assert collaboration_deltas[0].payload["collaboration"]["revision"] == 1
    assert {event.turn_id for event in events} == {
        runtime_input.request.run_id,
    }
    assert len(client.requests) == 2
    assert "不要再回写旧结果" in client.requests[1].messages[-1].text_content()
