# -*- coding: utf-8 -*-
"""L3 steering recovery locks using real worker death and process
sleep/wake.
"""

from __future__ import annotations

import operator

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from lantai.runtime.coherence import ModelRequestManifestStore
from lantai.runtime.coherence.contracts import ExecutionFence
from lantai.runtime.cost_accounting import BudgetLedger, BudgetMode
from lantai.runtime.inbox_admission import RuntimeInboxAdmission
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.turn_control import (
    RuntimeInputConflictError,
    RuntimeTurnControlStore,
)


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "tests" / "support" / "steering_recovery_worker.py"
EFFECT_SERVICE = ROOT / "tests" / "support" / "runtime_effect_service.py"
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


def _start_worker(
    tmp_path: Path,
    scenario: str,
    *,
    service_url: str = "",
    attachment_file: Path | None = None,
    release_file: Path | None = None,
    result_file: Path | None = None,
) -> tuple[subprocess.Popen[str], Path, Path]:
    state_root = tmp_path / "runtime-state"
    barrier_file = tmp_path / f"{scenario}-barrier.json"
    command = [
        sys.executable,
        str(WORKER),
        "--scenario",
        scenario,
        "--state-root",
        str(state_root),
        "--barrier-file",
        str(barrier_file),
    ]
    if service_url:
        command.extend(["--service-url", service_url])
    if attachment_file is not None:
        command.extend(["--attachment-file", str(attachment_file)])
    if release_file is not None:
        command.extend(["--release-file", str(release_file)])
    if result_file is not None:
        command.extend(["--result-file", str(result_file)])
    # The caller owns the worker and kills/waits in its explicit finally.
    # pylint: disable=consider-using-with
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=_python_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # pylint: enable=consider-using-with
    return process, state_root, barrier_file


def _barrier_or_worker_error(
    process: subprocess.Popen[str],
    barrier_file: Path,
) -> dict[str, Any]:
    try:
        barrier = _wait_for_json(barrier_file)
    except AssertionError as exc:
        output, error = process.communicate(timeout=2)
        raise AssertionError(
            f"{exc}; worker exit={process.returncode}; "
            f"stdout={output!r}; stderr={error!r}",
        ) from exc
    target_pid = int(
        barrier.get("target_worker_pid") or barrier.get("pid") or 0,
    )
    assert target_pid == process.pid
    return barrier


def _kill_at_barrier(
    tmp_path: Path,
    scenario: str,
    *,
    service_url: str = "",
) -> tuple[Path, dict[str, Any]]:
    process, state_root, barrier_file = _start_worker(
        tmp_path,
        scenario,
        service_url=service_url,
    )
    try:
        barrier = _barrier_or_worker_error(process, barrier_file)
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=5) == -signal.SIGKILL
        return state_root, barrier
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


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


def _service_get(service_url: str, path: str) -> dict[str, Any]:
    with urlopen(f"{service_url}{path}", timeout=5) as response:  # noqa: S310
        return json.loads(response.read())


def _admission(store: RuntimeTurnControlStore) -> RuntimeInboxAdmission:
    return RuntimeInboxAdmission(
        store=store,
        run_id=RUN_ID,
        runtime_metadata=RUNTIME_METADATA,
    )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_cr01_kill_before_admission_keeps_draft_without_fake_receipt(
    tmp_path: Path,
) -> None:
    state_root, barrier = _kill_at_barrier(tmp_path, "cr01")
    assert (
        Path(barrier["upload"]).read_bytes()
        == b"durable-upload-before-admission"
    )
    assert (
        json.loads(Path(barrier["draft"]).read_text(encoding="utf-8"))[
            "status"
        ]
        == "local_draft"
    )
    store = RuntimeTurnControlStore(state_root)
    assert (
        store.get_input_receipt(
            chat_id=CHAT_ID,
            command_id="cr01-command",
        )
        is None
    )
    assert operator.eq(store.read_inbox(RUN_ID), [])


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_cr02_admission_survives_lost_notification_and_only_owner_claims(
    tmp_path: Path,
) -> None:
    state_root, _barrier = _kill_at_barrier(tmp_path, "cr02")
    store = RuntimeTurnControlStore(state_root)
    stale = RuntimeInboxAdmission(
        store=store,
        run_id=RUN_ID,
        runtime_metadata={
            **RUNTIME_METADATA,
            "runtime_lease_owner": "owner-stale",
        },
    ).claim_next(after_seq=0)
    assert stale.claim is None
    assert stale.conflict is not None
    assert stale.conflict.reason == "inbox execution fence does not match"
    assert stale.conflict.kind == "run_inbox_fence_rejected"
    assert operator.eq(store.current_dispositions(RUN_ID), {})

    active = _admission(store)
    first = active.claim_next(after_seq=0)
    repeated = active.claim_next(after_seq=0)
    assert first.claim is not None
    assert repeated.claim is not None
    assert repeated.claim.item_seq == first.claim.item_seq == 1
    active.bind_to_model_request(
        first.claim,
        model_request_id="request-cr02",
        model_epoch=1,
        intent_revision=1,
    )
    active.settle_consumed(first.claim)
    assert (
        store.get_input_receipt(
            chat_id=CHAT_ID,
            command_id="cr02-command",
        ).execution_status
        == "applied"
    )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_cr03_claim_recovery_reuses_command_and_client_turn(
    tmp_path: Path,
) -> None:
    state_root, barrier = _kill_at_barrier(tmp_path, "cr03")
    store = RuntimeTurnControlStore(state_root)
    assert store.current_dispositions(RUN_ID)[1].status == "claimed"
    recovered = _admission(store)
    outcome = recovered.claim_next(after_seq=0)
    assert outcome.claim is not None
    assert outcome.claim.client_turn_id == barrier["client_turn_id"]
    assert len(store.read_inbox(RUN_ID)) == 1
    recovered.bind_to_model_request(
        outcome.claim,
        model_request_id="request-cr03",
        model_epoch=1,
        intent_revision=1,
    )
    recovered.settle_consumed(outcome.claim)
    assert len(store.read_inbox(RUN_ID)) == 1


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_cr04_manifest_and_budget_reservation_reconcile_before_dispatch(
    tmp_path: Path,
) -> None:
    state_root, barrier = _kill_at_barrier(tmp_path, "cr04")
    manifest = ModelRequestManifestStore(state_root).load_from_path(
        barrier["manifest_ref"],
    )
    assert manifest.verify_integrity()
    assert manifest.model_request_id.startswith("request-cr04:")
    ledger = BudgetLedger(
        state_root,
        limit_micro_cny=10_000,
        scope_run_id=RUN_ID,
    )
    recovered = ledger.reserve(
        reservation_id="reservation-cr04",
        run_id=RUN_ID,
        amount_micro_cny=700,
        mode=BudgetMode.ESTIMATE_GUARD,
        upper_bound_proven=False,
    )
    assert recovered.status == "reserved"
    assert (
        ledger.claim_dispatch(recovered.reservation_id).status == "dispatched"
    )
    with pytest.raises(ValueError, match="not fresh"):
        ledger.claim_dispatch(recovered.reservation_id)
    assert ledger.settle(
        recovered.reservation_id,
        actual_micro_cny=650,
    ).status == ("settled")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
@pytest.mark.parametrize("scenario", ["cr05", "cr08"])
def test_cr05_cr08_provider_acceptance_is_probed_without_double_continuation(
    tmp_path: Path,
    scenario: str,
) -> None:
    with _effect_service(tmp_path) as service_url:
        state_root, barrier = _kill_at_barrier(
            tmp_path,
            scenario,
            service_url=service_url,
        )
        before = _service_get(service_url, "/snapshot")
        assert len(before["mutations"]) == 1
        store = RuntimeTurnControlStore(state_root)
        receipt = store.get_input_receipt(
            chat_id=CHAT_ID,
            command_id=f"{scenario}-command",
        )
        assert receipt is not None
        assert receipt.execution_status == "bound_to_request"
        ledger = BudgetLedger(
            state_root,
            limit_micro_cny=10_000,
            scope_run_id=RUN_ID,
        )
        assert ledger.get(barrier["reservation_id"]).status == "dispatched"
        assert (
            ledger.mark_ambiguous(barrier["reservation_id"]).status
            == "ambiguous"
        )

        query = urlencode({"operation_id": barrier["operation_id"]})
        probe = _service_get(service_url, f"/probe?{query}")
        assert probe["committed"] is True
        assert probe["operation_id"] == barrier["operation_id"]
        store.settle_input(
            run_id=RUN_ID,
            item_seq=int(barrier["item_seq"]),
            actor_id=f"native:{ATTEMPT_ID}:2",
            fence=FENCE,
            status="consumed",
            reason="provider receipt reconciled after connection loss",
        )
        ledger.settle(barrier["reservation_id"], actual_micro_cny=500)
        assert (
            store.get_input_receipt(
                chat_id=CHAT_ID,
                command_id=f"{scenario}-command",
            ).execution_status
            == "applied"
        )
        assert _service_get(service_url, "/snapshot") == before


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_cr06_applied_state_replays_after_ui_notification_loss(
    tmp_path: Path,
) -> None:
    state_root, _barrier = _kill_at_barrier(tmp_path, "cr06")
    store = RuntimeTurnControlStore(state_root)
    first = store.get_input_receipt(chat_id=CHAT_ID, command_id="cr06-command")
    assert first is not None
    assert first.execution_status == "applied"
    assert first.model_epoch == 4
    repeated, _item = store.append_input(
        command_id="cr06-command",
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        chat_id=CHAT_ID,
        message=RuntimeMessage.text("user", "application before notification"),
        intent="redirect",
        fence=FENCE,
    )
    assert repeated == first
    assert operator.eq(store.read_admissible_inbox(RUN_ID), [])


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_cr07_cancel_retransmit_stays_cancelled_after_restart(
    tmp_path: Path,
) -> None:
    state_root, _barrier = _kill_at_barrier(tmp_path, "cr07")
    store = RuntimeTurnControlStore(state_root)
    replayed, _item = store.append_input(
        command_id="cr07-command",
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        chat_id=CHAT_ID,
        message=RuntimeMessage.text("user", "cancel this input"),
        intent="redirect",
        fence=FENCE,
    )
    assert replayed.execution_status == "cancelled"
    assert (
        store.cancel_input(
            chat_id=CHAT_ID,
            command_id="cr07-command",
            actor_id="user:chat-steering-recovery",
        )
        == replayed
    )
    assert operator.eq(store.read_admissible_inbox(RUN_ID), [])


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_cr09_late_old_epoch_snapshot_cannot_roll_back_new_cursor(
    tmp_path: Path,
) -> None:
    state_root, barrier = _kill_at_barrier(tmp_path, "cr09")
    store = RuntimeTurnControlStore(state_root)
    old = store.get_input_receipt(chat_id=CHAT_ID, command_id="cr09-old")
    new = store.get_input_receipt(chat_id=CHAT_ID, command_id="cr09-new")
    assert old is not None and new is not None
    assert barrier["old_snapshot"]["model_epoch"] == 1
    assert old.model_epoch == 1
    assert new.model_epoch == 2
    assert new.intent_revision == 2
    with pytest.raises(RuntimeInputConflictError, match="cannot bind"):
        store.bind_input_to_model_request(
            run_id=RUN_ID,
            item_seq=int(barrier["old_seq"]),
            actor_id=f"native:{ATTEMPT_ID}:2",
            fence=FENCE,
            model_request_id="late-old-snapshot",
            model_epoch=1,
            intent_revision=1,
        )
    assert (
        store.get_input_receipt(
            chat_id=CHAT_ID,
            command_id="cr09-new",
        )
        == new
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="requires POSIX sleep/wake semantics",
)
def test_cr10_sleep_wake_revalidates_owner_and_attachment_snapshot(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "sleep-wake.png"
    attachment.write_bytes(b"original-sleep-wake-image")
    release_file = tmp_path / "resume-worker.json"
    result_file = tmp_path / "cr10-result.json"
    process, state_root, barrier_file = _start_worker(
        tmp_path,
        "cr10",
        attachment_file=attachment,
        release_file=release_file,
        result_file=result_file,
    )
    try:
        barrier = _barrier_or_worker_error(process, barrier_file)
        assert barrier["stage"] == "sleep_ready"
        os.kill(process.pid, signal.SIGSTOP)
        attachment.write_bytes(b"changed-while-worker-was-asleep")
        release_file.write_text('{"resume": true}', encoding="utf-8")
        os.kill(process.pid, signal.SIGCONT)
        result = _wait_for_json(result_file)
        assert (
            result["owner_conflict"] == "inbox execution fence does not match"
        )
        assert result["owner_kind"] == "run_inbox_fence_rejected"
        assert "digest changed" in result["attachment_conflict"]
        assert result["attachment_kind"] == "run_inbox_attachment_conflict"
        dispositions = RuntimeTurnControlStore(
            state_root,
        ).current_dispositions(RUN_ID)
        assert 1 not in dispositions
        assert dispositions[2].status == "rejected"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
