# -*- coding: utf-8 -*-
"""Independent RUN-02 worker for ordinary replies whose text is exactly ``1``."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lantai.app.routers import console as console_router
from lantai.app.runner import runner as runner_module
from lantai.app.runner.runner import AgentRunner
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
from lantai.runtime.runs import (
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.tools import LantaiToolRegistry, ToolSpec


SCENARIOS = {
    "math": {
        "previous_objective": "Answer the arithmetic check without creating a file",
        "messages": (
            ("user", "2 - 1 等于多少？请让我直接输入答案。"),
            ("assistant", "请直接输入答案。"),
            ("user", "1"),
        ),
        "reply": "答案正确。",
    },
    "multiple_choice": {
        "previous_objective": "Choose one ordinary reading preference",
        "messages": (
            ("assistant", "请选择：1. 保留原文；2. 改写。"),
            ("user", "1"),
        ),
        "reply": "已选择保留原文。",
    },
}


class _PlainReplyModelClient:
    provider_id = "run02-controlled"
    model_name = "run02-plain-reply"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest):
        self.requests.append(request)
        message_id = f"run02-message-{len(self.requests)}"
        yield ModelStreamEvent(
            kind="message_completed",
            message_id=message_id,
            response=ModelResponse(
                message=ModelMessage(
                    id=message_id,
                    role="assistant",
                    content=[
                        ModelContentBlock(type="text", text=self.reply),
                    ],
                ),
            ),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(message=ModelMessage.text("assistant", self.reply))


async def _seed_non_deliverable_previous_run(
    state_workspace_dir: Path,
    *,
    scenario: str,
) -> RuntimeRunContract:
    now = datetime.now(timezone.utc)
    previous_run_id = f"run02-{scenario}-previous"
    previous_attempt_id = f"{previous_run_id}:attempt-1"
    task = build_chat_task_contract(
        objective=str(SCENARIOS[scenario]["previous_objective"]),
        client_turn_id=f"run02-{scenario}-previous-turn",
        complexity="standard",
        work_method_id="standard",
        run_id=previous_run_id,
        attempt_id=previous_attempt_id,
    )
    if task.deliverable_contract is not None:
        raise AssertionError("ordinary RUN-02 fixture unexpectedly has a deliverable")
    contract = RuntimeRunContract(
        run_id=previous_run_id,
        attempt_id=previous_attempt_id,
        source="chat",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="default",
            session_id=f"console:run02:{scenario}",
            workspace_id=str(state_workspace_dir),
            channel="console",
        ),
        status="succeeded",
        revision=0,
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        settled_at=now,
        delivery_status="not_delivered",
        task_contract=task,
        release_edition="official",
    )
    await RuntimeRunStore(state_workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    persisted = await RuntimeRunStore(state_workspace_dir).load_contract(
        previous_run_id,
    )
    if persisted is None:
        raise AssertionError("RUN-02 previous run was not persisted")
    return persisted


def _install_minimal_runner_registries(tool_calls: list[str]) -> None:
    async def forbidden_create_presentation(**_kwargs: Any) -> dict[str, Any]:
        tool_calls.append("create_presentation")
        raise AssertionError("ordinary numeric choice started a PPT task")

    base = LantaiToolRegistry()
    base.register_tool(
        ToolSpec(
            name="create_presentation",
            description="RUN-02 sentinel presentation author",
            input_schema={"type": "object", "additionalProperties": True},
            origin="builtin",
            read_only=False,
            concurrency_safe=False,
        ),
        forbidden_create_presentation,
    )
    runner_module.build_lantai_tool_registry = lambda **_kwargs: base
    runner_module.build_memory_tool_registry = (
        lambda _manager: LantaiToolRegistry()
    )
    runner_module.build_mcp_client_registry = (
        lambda _clients, **_kwargs: LantaiToolRegistry()
    )


async def _run_case(workspace_dir: Path, *, scenario: str) -> dict[str, Any]:
    state_workspace_dir = workspace_dir / "state"
    project_workspace_dir = workspace_dir / "project"
    state_workspace_dir.mkdir(parents=True, exist_ok=True)
    project_workspace_dir.mkdir(parents=True, exist_ok=True)

    previous = await _seed_non_deliverable_previous_run(
        state_workspace_dir,
        scenario=scenario,
    )
    chat_meta = {
        "last_turn_run_id": previous.run_id,
        "last_turn_outcome_status": "delivered",
        "last_turn_delivery_status": "not_delivered",
    }
    pending_context = console_router._pending_deliverable_context_from_previous_run(
        chat_meta=chat_meta,
        previous_contract=previous,
    )

    tool_calls: list[str] = []
    _install_minimal_runner_registries(tool_calls)
    current_run_id = f"run02-{scenario}-current"
    request_context = {
        "run_id": current_run_id,
        "chat_id": f"chat-run02-{scenario}",
        "workspace_dir": str(project_workspace_dir),
        "state_workspace_dir": str(state_workspace_dir),
        "works_workspace_dir": str(project_workspace_dir),
    }
    if pending_context is not None:
        request_context["pending_deliverable_context"] = pending_context

    registry = AgentRunner(
        agent_id="default",
        workspace_dir=project_workspace_dir,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context=request_context,
        mcp_clients=[],
        current_user_text="1",
        runtime_attempt_id=f"{current_run_id}:attempt-1",
    )
    client = _PlainReplyModelClient(str(SCENARIOS[scenario]["reply"]))
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    messages = [
        RuntimeMessage.text(role, text)
        for role, text in SCENARIOS[scenario]["messages"]
    ]
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id=f"console:run02:{scenario}",
            user_id="default",
            channel="console",
            chat_id=request_context["chat_id"],
            run_id=current_run_id,
            workspace_dir=str(project_workspace_dir),
            metadata={
                key: value
                for key, value in request_context.items()
                if key == "pending_deliverable_context"
            },
        ),
        messages=messages,
    )
    events = [event async for event in runtime.run_turn(runtime_input)]
    if not client.requests:
        raise AssertionError("RUN-02 model boundary was not reached")
    first_request = client.requests[0]
    tool_names = [tool.name for tool in first_request.tools]
    loaded_previous = await RuntimeRunStore(state_workspace_dir).load_contract(
        previous.run_id,
    )
    presentation_job_root = state_workspace_dir / "presentation-jobs"
    pptx_paths = sorted(
        str(path.relative_to(workspace_dir))
        for path in workspace_dir.rglob("*.pptx")
    )
    tool_event_types = [
        event.type for event in events if event.type.startswith("tool.")
    ]
    message_completed_events = sum(
        event.type == "message.completed" for event in events
    )
    return {
        "pid": os.getpid(),
        "scenario": scenario,
        "current_user_text": "1",
        "current_user_text_sha256": hashlib.sha256(b"1").hexdigest(),
        "previous_run_persisted": loaded_previous is not None,
        "previous_deliverable_contract": (
            None
            if loaded_previous is None or loaded_previous.task_contract is None
            else loaded_previous.task_contract.deliverable_contract
        ),
        "console_pending_context_projected": pending_context is not None,
        "runner_pending_context_present": (
            "pending_deliverable_context" in request_context
        ),
        "resolver_registered": (
            registry.get_tool("resolve_pending_deliverable_choice") is not None
        ),
        "resolver_exposed_to_model": (
            "resolve_pending_deliverable_choice" in tool_names
        ),
        "presentation_tool_exposed_to_model": (
            "create_presentation" in tool_names
        ),
        "first_request_tool_choice": first_request.tool_choice,
        "model_request_count": len(client.requests),
        "tool_calls": tool_calls,
        "tool_event_types": tool_event_types,
        "presentation_job_root_exists": presentation_job_root.exists(),
        "pptx_paths": pptx_paths,
        "terminal_event": events[-1].type if events else None,
        "message_completed_events": message_completed_events,
        "scripted_reply_sha256": hashlib.sha256(
            str(SCENARIOS[scenario]["reply"]).encode("utf-8")
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(_run_case(args.workspace, scenario=args.scenario))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
