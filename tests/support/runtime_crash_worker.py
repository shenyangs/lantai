# -*- coding: utf-8 -*-
"""Create durable Runtime state and pause at deterministic crash barriers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from lantai.utils.authoritative_store import fsync_directory
from lantai.app.experience.runtime_runs import record_chat_runtime_run_started
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.runs import (
    RuntimeEffectRecord,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore


def _durable_barrier(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def _mutate(
    service_url: str,
    *,
    effect_id: str,
    nonce: str,
    hold_response_barrier: Path | None = None,
) -> dict[str, object]:
    payload = {
        "effect_id": effect_id,
        "nonce": nonce,
        "worker_pid": os.getpid(),
    }
    if hold_response_barrier is not None:
        payload["hold_response_barrier"] = str(hold_response_barrier)
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{service_url}/mutate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(
        request,
        timeout=5,
    ) as response:  # noqa: S310 - loopback fixture
        return json.loads(response.read())


async def _prepare(args: argparse.Namespace) -> None:
    task_contract = build_chat_task_contract(
        objective="process crash recovery fixture",
        client_turn_id=f"client-{args.nonce}",
        complexity="light",
        work_method_id="standard",
    )
    await record_chat_runtime_run_started(
        workspace_dir=args.state_root,
        agent_id="default",
        chat_id="chat-crash",
        session_id="console:chat-crash",
        run_id=args.run_id,
        effective_workspace_dir=str(args.state_root),
        task_contract=task_contract,
    )
    contract = await RuntimeRunStore(args.state_root).load_contract(
        args.run_id,
    )
    if contract is None:
        raise RuntimeError("RuntimeRun contract was not persisted")

    effect = RuntimeEffectRecord(
        run_id=contract.run_id,
        attempt_id=contract.attempt_id,
        step_id="step-crash",
        effect_id=args.effect_id,
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:chat-crash",
            workspace_id=str(args.state_root),
        ),
        tool_name="fixture.non_idempotent_write",
        args_hash="sha256:fixture-args",
        policy_snapshot_hash="sha256:fixture-policy",
        idempotency_key=f"fixture:{args.effect_id}",
        created_at=datetime.now(timezone.utc),
        release_edition="official",
    )
    journal = RuntimeEffectJournal(args.state_root)
    journal.append_effect(effect)

    if args.scenario != "after_intent":
        running = reduce_runtime_effect(
            effect,
            RuntimeEffectReducerAction(type="start"),
        ).effect
        journal.append_effect(running)
        result = _mutate(
            args.service_url,
            effect_id=args.effect_id,
            nonce=args.nonce,
            hold_response_barrier=(
                args.barrier_file
                if args.scenario == "during_external_response"
                else None
            ),
        )
        if args.scenario == "after_commit":
            result_hash = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(result, sort_keys=True).encode("utf-8"),
                ).hexdigest()
            )
            committed = reduce_runtime_effect(
                running,
                RuntimeEffectReducerAction(
                    type="commit",
                    result_hash=result_hash,
                ),
            ).effect
            journal.append_effect(committed)

    _durable_barrier(
        args.barrier_file,
        {
            "pid": os.getpid(),
            "nonce": args.nonce,
            "scenario": args.scenario,
            "run_id": args.run_id,
            "effect_id": args.effect_id,
        },
    )
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=(
            "after_intent",
            "during_external_response",
            "after_external",
            "after_commit",
        ),
        required=True,
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--barrier-file", type=Path, required=True)
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--effect-id", required=True)
    parser.add_argument("--nonce", required=True)
    asyncio.run(_prepare(parser.parse_args()))


if __name__ == "__main__":
    main()
