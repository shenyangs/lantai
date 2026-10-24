# -*- coding: utf-8 -*-
"""Crash a real Native Runtime turn after an external mutation is durable."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

from lantai.app.experience.runtime_runs import record_chat_runtime_run_started
from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.effects import RuntimeEffectJournal, RuntimeEffectRecoveryCapability
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import ModelMessage, ModelResponse, ModelStreamEvent
from lantai.runtime.runs import build_chat_task_contract
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.tools import LantaiToolRegistry, ToolCall, ToolSpec


class _CrashWindowModel:
    provider_id = "fixture"
    model_name = "native-crash-window"

    def __init__(self, tool_call: ToolCall) -> None:
        self._tool_call = tool_call

    async def stream(self, _request):
        yield ModelStreamEvent(
            kind="tool_call_completed",
            message_id="msg-native-crash",
            tool_call=self._tool_call,
        )
        yield ModelStreamEvent(
            kind="message_completed",
            message_id="msg-native-crash",
            response=ModelResponse(
                message=ModelMessage(id="msg-native-crash", role="assistant"),
            ),
        )

    async def complete(self, _request):
        return ModelResponse(message=ModelMessage.text("assistant", "unused"))


def _post_mutation(
    *,
    service_url: str,
    state_root: Path,
    barrier_file: Path,
    run_id: str,
    nonce: str,
    tool_call_id: str,
) -> dict[str, object]:
    contract = asyncio.run(RuntimeRunStore(state_root).load_contract(run_id))
    if contract is None or contract.checkpoint is None:
        raise RuntimeError("Native Runtime did not persist its before_tool checkpoint")
    effect_id = f"effect:{tool_call_id}"
    latest_effect = RuntimeEffectJournal(state_root).latest_effect(
        run_id,
        contract.attempt_id,
        effect_id,
    )
    if latest_effect is None:
        raise RuntimeError("ToolTransaction did not persist its effect record")
    operation_id = f"operation:{nonce}"
    payload = {
        "effect_id": effect_id,
        "operation_id": operation_id,
        "attempt_id": contract.attempt_id,
        "tool_call_id": tool_call_id,
        "idempotency_key": latest_effect.idempotency_key,
        "nonce": nonce,
        "worker_pid": os.getpid(),
        "hold_response_barrier": str(barrier_file),
        "barrier_metadata": {
            "run_id": run_id,
            "attempt_id": contract.attempt_id,
            "tool_call_id": tool_call_id,
            "effect_id": effect_id,
            "effect_revision": latest_effect.revision,
            "effect_phase": latest_effect.status,
            "checkpoint_id": contract.checkpoint.checkpoint_id,
            "checkpoint_seq": contract.checkpoint.seq,
            "checkpoint_phase": contract.checkpoint.next_execution_position,
            "operation_id": operation_id,
        },
    }
    request = Request(
        f"{service_url}/mutate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=300) as response:  # noqa: S310 - loopback fixture
        return json.loads(response.read())


async def _run(args: argparse.Namespace) -> None:
    task_contract = build_chat_task_contract(
        objective="perform one durable external mutation and report completion",
        client_turn_id=f"client-{args.nonce}",
        complexity="light",
        work_method_id="standard",
    )
    started = await record_chat_runtime_run_started(
        workspace_dir=args.state_root,
        agent_id="default",
        chat_id="chat-native-crash",
        session_id="console:chat-native-crash",
        run_id=args.run_id,
        effective_workspace_dir=str(args.project_root),
        task_contract=task_contract,
    )
    tool_call = ToolCall(
        id=args.tool_call_id,
        name="fixture_non_idempotent_write",
        arguments={"nonce": args.nonce},
    )
    registry = LantaiToolRegistry()

    def mutate(nonce: str) -> dict[str, object]:
        if nonce != args.nonce:
            raise RuntimeError("fixture nonce changed")
        return _post_mutation(
            service_url=args.service_url,
            state_root=args.state_root,
            barrier_file=args.barrier_file,
            run_id=args.run_id,
            nonce=nonce,
            tool_call_id=args.tool_call_id,
        )

    registry.register_tool(
        ToolSpec(
            name=tool_call.name,
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
                notes="Independent loopback service exposes a receipt probe.",
            ),
        ),
        mutate,
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: _CrashWindowModel(tool_call),
        tool_registry=registry,
        effect_journal=RuntimeEffectJournal(args.state_root),
        progressive_tools=False,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="console:chat-native-crash",
            user_id="user-native-crash",
            channel="console",
            chat_id="chat-native-crash",
            run_id=args.run_id,
            root_session_id="console:chat-native-crash",
            workspace_dir=str(args.project_root),
        ),
        messages=[RuntimeMessage.text("user", task_contract.objective)],
        metadata={
            "state_workspace_dir": str(args.state_root),
            "attempt_id": started.attempt_id,
            "runtime_revision": started.revision,
            "runtime_lease_owner": started.lease_owner,
            "task_contract": task_contract.model_dump(mode="json"),
        },
    )
    async for _event in runtime.run_turn(runtime_input):
        pass
    raise RuntimeError("crash fixture unexpectedly returned from external mutation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--barrier-file", type=Path, required=True)
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tool-call-id", required=True)
    parser.add_argument("--nonce", required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
