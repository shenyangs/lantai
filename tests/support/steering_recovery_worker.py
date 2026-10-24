# -*- coding: utf-8 -*-
"""Pause steering state at durable crash and sleep/wake boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen

from lantai.utils.authoritative_store import fsync_directory
from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.runtime.coherence.contracts import ExecutionFence
from lantai.runtime.context_guard import prepare_model_request_for_context
from lantai.runtime.cost_accounting import BudgetLedger, BudgetMode
from lantai.runtime.inbox_admission import RuntimeInboxAdmission
from lantai.runtime.messages import ContentBlock, RuntimeMessage
from lantai.runtime.models import ModelMessage, ModelRequest
from lantai.runtime.turn_control import (
    RuntimeTurnControlStore,
    control_input_grant_ref,
)


RUN_ID = "run-steering-recovery"
ATTEMPT_ID = "attempt-steering-recovery"
CHAT_ID = "chat-steering-recovery"
FENCE = ExecutionFence(
    attempt_id=ATTEMPT_ID,
    generation=2,
    expected_revision=7,
    lease_owner="owner-current",
)
RUNTIME_METADATA = {
    "attempt_id": ATTEMPT_ID,
    "runtime_generation": 2,
    "runtime_revision": 7,
    "runtime_lease_owner": "owner-current",
}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def _wait_forever() -> None:
    while True:
        time.sleep(60)


def _wait_for(path: Path) -> None:
    while not path.exists():
        time.sleep(0.02)


def _store(args: argparse.Namespace) -> RuntimeTurnControlStore:
    return RuntimeTurnControlStore(args.state_root)


def _append(
    store: RuntimeTurnControlStore,
    command_id: str,
    text: str,
) -> tuple[object, object]:
    return store.append_input(
        command_id=command_id,
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        chat_id=CHAT_ID,
        message=RuntimeMessage.text("user", text),
        intent="redirect",
        fence=FENCE,
    )


def _claim(store: RuntimeTurnControlStore, *, after_seq: int = 0):
    admission = RuntimeInboxAdmission(
        store=store,
        run_id=RUN_ID,
        runtime_metadata=RUNTIME_METADATA,
    )
    outcome = admission.claim_next(after_seq=after_seq)
    if outcome.claim is None:
        raise RuntimeError(
            f"fixture failed to claim input: {outcome.conflict}",
        )
    return admission, outcome.claim


def _post_accepted_without_response(args: argparse.Namespace) -> None:
    store = _store(args)
    command_id = f"{args.scenario}-command"
    _append(store, command_id, f"{args.scenario} redirect")
    admission, claim = _claim(store)
    model_request_id = f"request-{args.scenario}"
    admission.bind_to_model_request(
        claim,
        model_request_id=model_request_id,
        model_epoch=3,
        intent_revision=2,
    )
    reservation_id = f"reservation-{args.scenario}"
    ledger = BudgetLedger(
        args.state_root,
        limit_micro_cny=10_000,
        scope_run_id=RUN_ID,
    )
    ledger.reserve(
        reservation_id=reservation_id,
        run_id=RUN_ID,
        amount_micro_cny=500,
        mode=BudgetMode.ESTIMATE_GUARD,
        upper_bound_proven=False,
    )
    ledger.claim_dispatch(reservation_id)
    operation_id = f"operation-{args.scenario}"
    payload = {
        "effect_id": f"effect-{args.scenario}",
        "operation_id": operation_id,
        "nonce": args.scenario,
        "worker_pid": os.getpid(),
        "hold_response_barrier": str(args.barrier_file),
        "barrier_metadata": {
            "scenario": args.scenario,
            "run_id": RUN_ID,
            "command_id": command_id,
            "model_request_id": model_request_id,
            "reservation_id": reservation_id,
            "operation_id": operation_id,
            "item_seq": claim.item_seq,
        },
    }
    request = Request(
        f"{args.service_url}/mutate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=300):  # noqa: S310 - loopback fixture
        pass
    raise RuntimeError("held provider response unexpectedly returned")


# pylint: disable=R0915
def _run(args: argparse.Namespace) -> None:
    args.state_root.mkdir(parents=True, exist_ok=True)
    store = _store(args)

    if args.scenario == "cr01":
        upload = args.state_root / "uploads" / "guidance.png"
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"durable-upload-before-admission")
        with upload.open("rb") as handle:
            os.fsync(handle.fileno())
        draft = args.state_root / "drafts" / "pending-command.json"
        _write_json(
            draft,
            {
                "command_id": "cr01-command",
                "attachment": str(upload),
                "status": "local_draft",
            },
        )
        _write_json(
            args.barrier_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "upload": str(upload),
                "draft": str(draft),
            },
        )
        _wait_forever()

    if args.scenario == "cr02":
        receipt, item = _append(store, "cr02-command", "recover admission")
        _write_json(
            args.barrier_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "command_id": item.command_id,
                "server_seq": receipt.server_seq,
            },
        )
        _wait_forever()

    if args.scenario == "cr03":
        _append(store, "cr03-command", "recover claimed command")
        _admission, claim = _claim(store)
        _write_json(
            args.barrier_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "command_id": "cr03-command",
                "client_turn_id": claim.client_turn_id,
                "item_seq": claim.item_seq,
            },
        )
        _wait_forever()

    if args.scenario == "cr04":
        request = ModelRequest(
            messages=[ModelMessage.text("user", "durable request manifest")],
            metadata={
                "state_workspace_dir": str(args.state_root),
                "turn_id": RUN_ID,
                "run_id": RUN_ID,
                "attempt_id": ATTEMPT_ID,
                "runtime_generation": 2,
                "runtime_revision": 7,
                "runtime_lease_owner": "owner-current",
                "model_request_id": "request-cr04",
                "model_request_created_at": "2026-09-21T00:00:00+00:00",
                "harness_mode": "enforced",
            },
        )
        prepared = prepare_model_request_for_context(
            request,
            ProviderModelEndpoint(provider_id="openai", model_id="gpt-4o"),
        )
        reservation_id = "reservation-cr04"
        reservation = BudgetLedger(
            args.state_root,
            limit_micro_cny=10_000,
            scope_run_id=RUN_ID,
        ).reserve(
            reservation_id=reservation_id,
            run_id=RUN_ID,
            amount_micro_cny=700,
            mode=BudgetMode.ESTIMATE_GUARD,
            upper_bound_proven=False,
        )
        _write_json(
            args.barrier_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "manifest_ref": str(
                    prepared.metadata["model_request_manifest_ref"],
                ),
                "reservation_id": reservation_id,
                "reservation_status": reservation.status,
            },
        )
        _wait_forever()

    if args.scenario in {"cr05", "cr08"}:
        _post_accepted_without_response(args)

    if args.scenario == "cr06":
        _append(store, "cr06-command", "application before notification")
        admission, claim = _claim(store)
        admission.bind_to_model_request(
            claim,
            model_request_id="request-cr06",
            model_epoch=4,
            intent_revision=3,
        )
        admission.settle_consumed(claim)
        _write_json(
            args.barrier_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "command_id": "cr06-command",
                "item_seq": claim.item_seq,
            },
        )
        _wait_forever()

    if args.scenario == "cr07":
        _append(store, "cr07-command", "cancel this input")
        receipt = store.cancel_input(
            chat_id=CHAT_ID,
            command_id="cr07-command",
            actor_id="user:chat-steering-recovery",
        )
        _write_json(
            args.barrier_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "command_id": receipt.command_id,
                "execution_status": receipt.execution_status,
            },
        )
        _wait_forever()

    if args.scenario == "cr09":
        _append(store, "cr09-old", "old epoch")
        admission, old_claim = _claim(store)
        admission.bind_to_model_request(
            old_claim,
            model_request_id="request-cr09-old",
            model_epoch=1,
            intent_revision=1,
        )
        admission.settle_consumed(old_claim)
        old_receipt = store.get_input_receipt(
            chat_id=CHAT_ID,
            command_id="cr09-old",
        )
        _append(store, "cr09-new", "new epoch")
        admission, new_claim = _claim(store, after_seq=old_claim.item_seq)
        admission.bind_to_model_request(
            new_claim,
            model_request_id="request-cr09-new",
            model_epoch=2,
            intent_revision=2,
        )
        admission.settle_consumed(new_claim)
        _write_json(
            args.barrier_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "old_snapshot": (
                    old_receipt.model_dump(mode="json") if old_receipt else {}
                ),
                "old_seq": old_claim.item_seq,
                "new_seq": new_claim.item_seq,
            },
        )
        _wait_forever()

    if args.scenario == "cr10":
        attachment = args.attachment_file.resolve()
        content = attachment.read_bytes()
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        store.append_input(
            command_id="cr10-stale-owner",
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
            chat_id=CHAT_ID,
            message=RuntimeMessage.text("user", "stale owner input"),
            intent="redirect",
            fence=ExecutionFence(
                attempt_id=ATTEMPT_ID,
                generation=2,
                expected_revision=7,
                lease_owner="owner-old",
            ),
        )
        store.append_input(
            command_id="cr10-attachment",
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
            chat_id=CHAT_ID,
            message=RuntimeMessage(
                role="user",
                content=[
                    ContentBlock(
                        type="image",
                        data={
                            "source": {"type": "url", "url": str(attachment)},
                            "version": 1,
                            "digest": digest,
                            "grant_ref": control_input_grant_ref(
                                chat_id=CHAT_ID,
                                run_id=RUN_ID,
                                canonical_path=str(attachment),
                                digest=digest,
                            ),
                            "grant_issued_at": time.time(),
                            "grant_expires_at": time.time() + 3600,
                        },
                    ),
                ],
            ),
            intent="redirect",
            fence=FENCE,
        )
        _write_json(
            args.barrier_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "stage": "sleep_ready",
            },
        )
        _wait_for(args.release_file)
        admission = RuntimeInboxAdmission(
            store=RuntimeTurnControlStore(args.state_root),
            run_id=RUN_ID,
            runtime_metadata=RUNTIME_METADATA,
        )
        owner_outcome = admission.claim_next(after_seq=0)
        attachment_outcome = admission.claim_next(
            after_seq=owner_outcome.next_after_seq,
        )
        _write_json(
            args.result_file,
            {
                "scenario": args.scenario,
                "pid": os.getpid(),
                "owner_conflict": (
                    owner_outcome.conflict.reason
                    if owner_outcome.conflict is not None
                    else ""
                ),
                "owner_kind": (
                    owner_outcome.conflict.kind
                    if owner_outcome.conflict is not None
                    else ""
                ),
                "attachment_conflict": (
                    attachment_outcome.conflict.reason
                    if attachment_outcome.conflict is not None
                    else ""
                ),
                "attachment_kind": (
                    attachment_outcome.conflict.kind
                    if attachment_outcome.conflict is not None
                    else ""
                ),
            },
        )
        _wait_forever()

    raise ValueError(f"unsupported scenario: {args.scenario}")


# pylint: enable=R0915


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=(
            "cr01",
            "cr02",
            "cr03",
            "cr04",
            "cr05",
            "cr06",
            "cr07",
            "cr08",
            "cr09",
            "cr10",
        ),
        required=True,
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--barrier-file", type=Path, required=True)
    parser.add_argument("--service-url", default="")
    parser.add_argument("--attachment-file", type=Path, default=Path("."))
    parser.add_argument("--release-file", type=Path, default=Path("release"))
    parser.add_argument(
        "--result-file",
        type=Path,
        default=Path("result.json"),
    )
    _run(parser.parse_args())


if __name__ == "__main__":
    main()
