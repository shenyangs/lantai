# -*- coding: utf-8 -*-
"""Runtime Effect reducer and append-only journal."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from lantai.utils.authoritative_store import fsync_directory
from lantai.utils.file_lock import locked_file_descriptor
from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)

from .runs import RuntimeEffectRecord, RuntimeEffectStatus


RUNTIME_EFFECT_JOURNAL_SCHEMA_VERSION = 1
RUNTIME_EFFECT_JOURNAL_WRITER_VERSION = "lantai-runtime-effect-journal/1"

RuntimeEffectActionType = Literal[
    "require_approval",
    "approve",
    "block",
    "start",
    "commit",
    "fail",
    "mark_ambiguous",
    "probe_committed",
    "mark_safe_to_retry",
    "compensate",
    "human_resolve",
    "cancel",
]
RuntimeEffectIdempotency = Literal[
    "read_only",
    "idempotency_key",
    "natural_key",
    "not_idempotent",
]
RuntimeEffectProbeCapability = Literal[
    "not_required",
    "tool_specific",
    "browser_observation",
    "manual",
    "none",
]
RuntimeEffectCompensationCapability = Literal[
    "not_required",
    "tool_specific",
    "manual_or_domain_specific",
    "none",
]
RuntimeEffectAmbiguityStrategy = Literal[
    "safe_to_retry",
    "probe_required",
    "compensate",
    "manual_disambiguation",
]
RuntimeEffectReplayCrashWindow = Literal[
    "before_intent",
    "after_intent_before_execution",
    "waiting_for_approval",
    "during_or_after_external_call",
    "after_commit_before_checkpoint",
    "terminal",
]
RuntimeEffectKillPoint = Literal[
    "before_intent_write",
    "after_intent_fsync_before_execution",
    "after_external_call_before_response",
    "after_external_success_before_commit",
    "after_commit_before_checkpoint",
    "after_checkpoint_before_terminal",
]
RuntimeEffectObservedStatus: TypeAlias = (
    RuntimeEffectStatus | Literal["missing"]
)

_TERMINAL_EFFECT_STATUSES: set[str] = {
    "blocked",
    "committed",
    "failed",
    "probed_committed",
    "safe_to_retry",
    "compensated",
    "human_resolved",
    "cancelled",
}


class RuntimeEffectTransitionError(ValueError):
    """Raised when an effect transition would violate the state machine."""


class RuntimeEffectJournalError(ValueError):
    """Base error for RuntimeEffectJournal."""


class RuntimeEffectJournalConflictError(RuntimeEffectJournalError):
    """Raised when a stale or duplicate effect revision is appended."""


class RuntimeEffectJournalCorruptionError(RuntimeEffectJournalError):
    """Raised when a journal envelope or checksum is invalid."""


class RuntimeEffectUnresolvedError(RuntimeEffectJournalError):
    """A previous invocation with this identity has no settled outcome."""

    def __init__(self, effect: RuntimeEffectRecord) -> None:
        super().__init__(
            "An unsettled external effect must be reconciled before retry.",
        )
        self.effect = effect


class RuntimeEffectReducerAction(BaseModel):
    """One intended transition for a RuntimeEffectRecord."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: RuntimeEffectActionType
    approval_id: str | None = None
    result_hash: str | None = None
    ambiguity_reason: str | None = None
    now: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RuntimeEffectReducerResult(BaseModel):
    """Result of applying an effect reducer action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    effect: RuntimeEffectRecord
    idempotent: bool = False


class RuntimeEffectJournalEnvelope(BaseModel):
    """Versioned checksum envelope for one journaled effect snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = RUNTIME_EFFECT_JOURNAL_SCHEMA_VERSION
    writer_version: str = RUNTIME_EFFECT_JOURNAL_WRITER_VERSION
    checksum: str
    payload: dict[str, object]


class RuntimeEffectRecoveryCapability(BaseModel):
    """How a tool or action can be recovered after an ambiguous crash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_side_effect: bool = False
    idempotency: RuntimeEffectIdempotency = "read_only"
    idempotency_key_transport: Literal["none", "runtime_context"] = "none"
    probe: RuntimeEffectProbeCapability = "not_required"
    compensation: RuntimeEffectCompensationCapability = "not_required"
    on_ambiguity: RuntimeEffectAmbiguityStrategy = "safe_to_retry"
    notes: str = ""


class RuntimeApprovalBinding(BaseModel):
    """Identity binding required for approval requests and resolutions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    attempt_id: str
    effect_id: str
    contract_hash: str
    policy_snapshot_hash: str
    tool_call_id: str
    tool_name: str


class RuntimeEffectReplayAssessment(BaseModel):
    """Side-effect-free assessment for replaying a crashed effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    effect_id: str | None = None
    observed_status: RuntimeEffectObservedStatus
    crash_window: RuntimeEffectReplayCrashWindow
    automatic_external_call_allowed: bool = False
    requires_probe: bool = False
    requires_human: bool = False
    recommended_reducer_actions: tuple[RuntimeEffectActionType, ...] = ()
    reason: str


class RuntimeEffectKillPointReplayAssessment(BaseModel):
    """Replay assessment annotated with an explicit crash injection point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kill_point: RuntimeEffectKillPoint
    effect_id: str | None = None
    observed_status: RuntimeEffectObservedStatus
    external_call_may_have_been_issued: bool = False
    automatic_external_call_allowed: bool = False
    external_call_must_not_be_replayed: bool = False
    requires_probe: bool = False
    requires_human: bool = False
    needs_checkpoint_rebuild: bool = False
    needs_terminal_rewrite: bool = False
    recommended_reducer_actions: tuple[RuntimeEffectActionType, ...] = ()
    base_assessment: RuntimeEffectReplayAssessment
    reason: str


_KILL_POINTS_AFTER_EXTERNAL_CALL: set[RuntimeEffectKillPoint] = {
    "after_external_call_before_response",
    "after_external_success_before_commit",
    "after_commit_before_checkpoint",
    "after_checkpoint_before_terminal",
}


def _can_retry_ambiguous_effect(
    recovery: RuntimeEffectRecoveryCapability,
) -> bool:
    if not recovery.external_side_effect:
        return True
    if recovery.idempotency == "read_only":
        return True
    return recovery.on_ambiguity == "safe_to_retry" and (
        recovery.idempotency == "natural_key"
        or (
            recovery.idempotency == "idempotency_key"
            and recovery.idempotency_key_transport == "runtime_context"
        )
    )


def _probe_available(recovery: RuntimeEffectRecoveryCapability) -> bool:
    return recovery.probe not in {"none", "not_required"}


def _compensation_available(recovery: RuntimeEffectRecoveryCapability) -> bool:
    return recovery.compensation not in {"none", "not_required"}


def assess_runtime_effect_ambiguity(
    recovery: RuntimeEffectRecoveryCapability,
    *,
    effect_id: str | None = None,
    observed_status: RuntimeEffectObservedStatus = "ambiguous",
    mark_ambiguous_first: bool = False,
) -> RuntimeEffectReplayAssessment:
    """Classify an invocation whose external outcome cannot be observed.

    This is shared by live tool settlement and crash replay so both paths use
    the same idempotency, probe, compensation, and human-review rules.
    """

    prefix: tuple[RuntimeEffectActionType, ...] = (
        ("mark_ambiguous",) if mark_ambiguous_first else ()
    )
    if _can_retry_ambiguous_effect(recovery):
        return RuntimeEffectReplayAssessment(
            effect_id=effect_id,
            observed_status=observed_status,
            crash_window="during_or_after_external_call",
            automatic_external_call_allowed=True,
            recommended_reducer_actions=prefix + ("mark_safe_to_retry",),
            reason=(
                "The effect may have reached the external system, but its "
                "declared recovery capability makes a retry safe."
            ),
        )
    if recovery.on_ambiguity == "probe_required" or _probe_available(recovery):
        return RuntimeEffectReplayAssessment(
            effect_id=effect_id,
            observed_status=observed_status,
            crash_window="during_or_after_external_call",
            requires_probe=True,
            recommended_reducer_actions=prefix,
            reason=(
                "The effect may have reached the external system; probe the "
                "external state before any retry."
            ),
        )
    if recovery.on_ambiguity == "compensate" and _compensation_available(
        recovery,
    ):
        return RuntimeEffectReplayAssessment(
            effect_id=effect_id,
            observed_status=observed_status,
            crash_window="during_or_after_external_call",
            recommended_reducer_actions=prefix + ("compensate",),
            reason=(
                (
                    "The effect may have reached the external system and shou"
                    "ld be compensated before further action."
                )
            ),
        )
    return RuntimeEffectReplayAssessment(
        effect_id=effect_id,
        observed_status=observed_status,
        crash_window="during_or_after_external_call",
        requires_human=True,
        recommended_reducer_actions=prefix,
        reason=(
            "The effect may have reached the external system and has no safe "
            "automatic retry, probe, or compensation path."
        ),
    )


# pylint: disable=R0911
def assess_runtime_effect_replay(
    effect: RuntimeEffectRecord | None,
    recovery: RuntimeEffectRecoveryCapability | None = None,
) -> RuntimeEffectReplayAssessment:
    (
        "Classify the safe recovery path after a crash around an "
        "effect.\n\n    The assessment is deliberately pure: it d"
        "oes not write the journal, execute\n    a probe, or retr"
        "y a tool. Callers use the result to decide whether to st"
        "art,\n    retry, probe, compensate, or surface the effec"
        "t for manual disambiguation.\n    "
    )

    capability = recovery or RuntimeEffectRecoveryCapability()
    if effect is None:
        return RuntimeEffectReplayAssessment(
            observed_status="missing",
            crash_window="before_intent",
            automatic_external_call_allowed=True,
            reason=(
                "No intent record exists, so there is no evidence that the "
                "external call was issued."
            ),
        )

    if effect.status in {"intent", "approved"}:
        return RuntimeEffectReplayAssessment(
            effect_id=effect.effect_id,
            observed_status=effect.status,
            crash_window="after_intent_before_execution",
            automatic_external_call_allowed=True,
            recommended_reducer_actions=("start",),
            reason=(
                (
                    "The effect intent exists but execution had not started i"
                    "n the journal."
                )
            ),
        )
    if effect.status == "waiting_approval":
        return RuntimeEffectReplayAssessment(
            effect_id=effect.effect_id,
            observed_status=effect.status,
            crash_window="waiting_for_approval",
            reason="The effect is waiting for a bound approval decision.",
        )
    if effect.status == "running":
        return assess_runtime_effect_ambiguity(
            capability,
            effect_id=effect.effect_id,
            observed_status=effect.status,
            mark_ambiguous_first=True,
        )
    if effect.status == "ambiguous":
        return assess_runtime_effect_ambiguity(
            capability,
            effect_id=effect.effect_id,
            observed_status=effect.status,
            mark_ambiguous_first=False,
        )
    if effect.status in {"committed", "probed_committed"}:
        return RuntimeEffectReplayAssessment(
            effect_id=effect.effect_id,
            observed_status=effect.status,
            crash_window="after_commit_before_checkpoint",
            reason=(
                "The external effect is already recorded as committed; replay "
                "must resume after the effect instead of executing it again."
            ),
        )
    if effect.status == "safe_to_retry":
        return RuntimeEffectReplayAssessment(
            effect_id=effect.effect_id,
            observed_status=effect.status,
            crash_window="terminal",
            automatic_external_call_allowed=True,
            reason=(
                "A previous ambiguity was resolved as safe to retry by the "
                "runtime effect state machine."
            ),
        )
    return RuntimeEffectReplayAssessment(
        effect_id=effect.effect_id,
        observed_status=effect.status,
        crash_window="terminal",
        reason=(
            "The effect is terminal and must not be "
            "replayed: "
            f"{effect.status}"
            "."
        ),
    )


# pylint: enable=R0911


def assess_runtime_effect_kill_point_replay(
    *,
    kill_point: RuntimeEffectKillPoint,
    effect: RuntimeEffectRecord | None,
    recovery: RuntimeEffectRecoveryCapability | None = None,
) -> RuntimeEffectKillPointReplayAssessment:
    """Classify replay safety for one explicit crash injection point.

    This is still pure: it does not execute probes, retry tools, repair
    checkpoints, or write terminal state. It gives recovery code and tests a
    precise bridge from the EFF-06 kill matrix to the effect replay decision.
    """

    base = assess_runtime_effect_replay(effect, recovery)
    external_call_may_have_been_issued = (
        kill_point in _KILL_POINTS_AFTER_EXTERNAL_CALL
    )
    needs_checkpoint_rebuild = kill_point == "after_commit_before_checkpoint"
    needs_terminal_rewrite = kill_point == "after_checkpoint_before_terminal"
    committed_or_later = kill_point in {
        "after_commit_before_checkpoint",
        "after_checkpoint_before_terminal",
    }
    external_call_must_not_be_replayed = committed_or_later or (
        external_call_may_have_been_issued
        and not base.automatic_external_call_allowed
    )

    reason = base.reason
    if needs_checkpoint_rebuild:
        reason += (
            " Rebuild or verify the local checkpoint/cursor before continuing."
        )
    if needs_terminal_rewrite:
        reason += (
            " Rewrite only missing terminal state; do not replay the effect."
        )

    return RuntimeEffectKillPointReplayAssessment(
        kill_point=kill_point,
        effect_id=base.effect_id,
        observed_status=base.observed_status,
        external_call_may_have_been_issued=external_call_may_have_been_issued,
        automatic_external_call_allowed=base.automatic_external_call_allowed,
        external_call_must_not_be_replayed=external_call_must_not_be_replayed,
        requires_probe=base.requires_probe,
        requires_human=base.requires_human,
        needs_checkpoint_rebuild=needs_checkpoint_rebuild,
        needs_terminal_rewrite=needs_terminal_rewrite,
        recommended_reducer_actions=base.recommended_reducer_actions,
        base_assessment=base,
        reason=reason,
    )


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _checksum_payload(payload: dict[str, object]) -> str:
    digest = hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _normalize_dt(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _target_status_for_action(
    action: RuntimeEffectReducerAction,
) -> RuntimeEffectStatus:
    mapping: dict[str, RuntimeEffectStatus] = {
        "require_approval": "waiting_approval",
        "approve": "approved",
        "block": "blocked",
        "start": "running",
        "commit": "committed",
        "fail": "failed",
        "mark_ambiguous": "ambiguous",
        "probe_committed": "probed_committed",
        "mark_safe_to_retry": "safe_to_retry",
        "compensate": "compensated",
        "human_resolve": "human_resolved",
        "cancel": "cancelled",
    }
    return mapping[action.type]


def _is_same_terminal_action(
    effect: RuntimeEffectRecord,
    action: RuntimeEffectReducerAction,
) -> bool:
    target = _target_status_for_action(action)
    if (
        effect.status != target
        or effect.status not in _TERMINAL_EFFECT_STATUSES
    ):
        return False
    if action.result_hash and effect.result_hash != action.result_hash:
        return False
    if (
        action.ambiguity_reason
        and effect.ambiguity_reason != action.ambiguity_reason
    ):
        return False
    return True


def _allowed_sources(action_type: str) -> set[str]:
    return {
        "require_approval": {"intent"},
        "approve": {"waiting_approval"},
        "block": {"intent", "waiting_approval", "approved"},
        "start": {"intent", "approved"},
        "commit": {"running"},
        "fail": {"running"},
        "mark_ambiguous": {"running"},
        "probe_committed": {"ambiguous"},
        "mark_safe_to_retry": {"ambiguous"},
        "compensate": {"ambiguous", "failed"},
        "human_resolve": {"ambiguous"},
        "cancel": {
            "intent",
            "waiting_approval",
            "approved",
            "running",
            "ambiguous",
        },
    }[action_type]


def _transition_message(
    effect: RuntimeEffectRecord,
    action: RuntimeEffectReducerAction,
) -> str:
    if action.type == "start" and effect.status == "waiting_approval":
        return "effect cannot start before approval is resolved"
    return f"illegal effect transition: {effect.status} -> {action.type}"


def reduce_runtime_effect(
    effect: RuntimeEffectRecord,
    action: RuntimeEffectReducerAction,
) -> RuntimeEffectReducerResult:
    """Apply one explicit RuntimeEffect transition without side effects."""

    if _is_same_terminal_action(effect, action):
        return RuntimeEffectReducerResult(effect=effect, idempotent=True)
    if effect.status in _TERMINAL_EFFECT_STATUSES:
        raise RuntimeEffectTransitionError(
            f"effect is terminal and cannot transition: {effect.status}",
        )
    if effect.status not in _allowed_sources(action.type):
        raise RuntimeEffectTransitionError(_transition_message(effect, action))

    target = _target_status_for_action(action)
    now = _normalize_dt(action.now)
    update: dict[str, object] = {
        "status": target,
        "revision": effect.revision + 1,
    }
    if action.approval_id:
        update["approval_id"] = action.approval_id
    if action.type == "start":
        update["started_at"] = effect.started_at or now
    if action.type in {
        "commit",
        "fail",
        "mark_ambiguous",
        "probe_committed",
        "mark_safe_to_retry",
        "compensate",
        "human_resolve",
        "cancel",
        "block",
    }:
        update["settled_at"] = now
    if action.result_hash:
        update["result_hash"] = action.result_hash
    if action.ambiguity_reason:
        update["ambiguity_reason"] = action.ambiguity_reason

    return RuntimeEffectReducerResult(effect=effect.model_copy(update=update))


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    fsync_directory(path)


def _create_durable_directory(path: Path) -> None:
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        _fsync_dir(directory)
        _fsync_dir(directory.parent)


@contextmanager
def _locked_file(path: Path):
    _create_durable_directory(path.parent)
    _chmod_best_effort(path.parent, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    _chmod_best_effort(path, 0o600)
    try:
        with locked_file_descriptor(fd):
            yield
    finally:
        os.close(fd)


def _effect_identity(effect: RuntimeEffectRecord) -> tuple[str, str, str]:
    return (
        _json_dumps(effect.owner.model_dump(mode="json")),
        effect.tool_name,
        effect.args_hash,
    )


def _file_stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        if path.is_symlink():
            raise RuntimeEffectJournalCorruptionError(
                "Effect journal is a symlink",
            )
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@dataclass
class _EffectIndex:
    stamp: tuple[int, int, int, int, int] | None = None
    records: list[RuntimeEffectRecord] = field(default_factory=list)
    latest: dict[str, RuntimeEffectRecord] = field(default_factory=dict)
    unresolved: dict[
        tuple[str, str, str],
        dict[str, RuntimeEffectRecord],
    ] = field(default_factory=dict)

    def add(self, record: RuntimeEffectRecord) -> None:
        previous = self.latest.get(record.effect_id)
        if previous is not None:
            self.unresolved.get(_effect_identity(previous), {}).pop(
                previous.effect_id,
                None,
            )
        self.records.append(record)
        self.latest[record.effect_id] = record
        if record.status in {"running", "ambiguous", "committed"}:
            self.unresolved.setdefault(_effect_identity(record), {})[
                record.effect_id
            ] = record


class RuntimeEffectJournal:
    (
        "Append-only effect journal rooted at ``<workspace>/.lant"
        "ai/runtime/v1``."
    )

    def __init__(self, workspace_dir: Path | str) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )
        self._indexes: dict[Path, _EffectIndex] = {}
        self._ledger_ids: dict[Path, tuple[str, str]] = {}
        self._catalog_cache: dict[
            Path,
            tuple[tuple[int, int, int, int, int], dict],
        ] = {}

    def ledger_path(self, run_id: str, attempt_id: str) -> Path:
        run_key = safe_storage_key("runtime-run", run_id, suffix="")
        attempt_key = safe_storage_key(
            "runtime-attempt",
            attempt_id,
            suffix="",
        )
        return contained_storage_path(
            self.root,
            "runs",
            run_key,
            "attempts",
            attempt_key,
            "effects.jsonl",
        )

    def _lock_path(self, run_id: str, attempt_id: str) -> Path:
        path = self.ledger_path(run_id, attempt_id)
        return path.with_name(".effects.lock")

    def append_effect(
        self,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        path = self.ledger_path(effect.run_id, effect.attempt_id)
        with _locked_file(self._lock_path(effect.run_id, effect.attempt_id)):
            index = self._load_index(effect.run_id, effect.attempt_id)
            latest = index.latest.get(effect.effect_id)
            if latest is not None and effect.revision <= latest.revision:
                raise RuntimeEffectJournalConflictError(
                    (
                        "effect revision conflict: "
                        f"{effect.revision}"
                        " <= "
                        f"{latest.revision}"
                    ),
                )
            try:
                self._append_envelope(path, effect)
            except BaseException:
                self._indexes.pop(path, None)
                raise
            stamp = _file_stamp(path)
            payload = effect.model_dump(mode="json")
            envelope = RuntimeEffectJournalEnvelope(
                checksum=_checksum_payload(payload),
                payload=payload,
            )
            expected_size = (index.stamp[2] if index.stamp else 0) + len(
                (_json_dumps(envelope.model_dump(mode="json")) + "\n").encode(
                    "utf-8",
                ),
            )
            if stamp is not None and stamp[2] == expected_size:
                index.add(effect.model_copy(deep=True))
                index.stamp = stamp
            else:
                self._indexes.pop(path, None)
                raise RuntimeEffectJournalCorruptionError(
                    "Effect journal changed during append",
                )
            self._write_catalog(effect, expected_size)
            return effect

    def confirm_effect_receipt(self, effect: RuntimeEffectRecord) -> None:
        (
            "Confirm only after the entire tool transaction has persi"
            "sted receipts."
        )
        with _locked_file(self._lock_path(effect.run_id, effect.attempt_id)):
            latest = self.latest_effect(
                effect.run_id,
                effect.attempt_id,
                effect.effect_id,
            )
            if latest != effect or effect.status != "committed":
                raise RuntimeEffectJournalConflictError(
                    "Cannot confirm a stale effect receipt",
                )
            self._confirm_settlement(effect)

    def append_effect_if_admissible(
        self,
        effect: RuntimeEffectRecord,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> RuntimeEffectRecord:
        # Admission and the durable running record form one
        # cross-process claim.
        with _locked_file(self.root / ".effect-admission.lock"):
            self.assert_effect_admissible(effect, recovery)
            return self.append_effect(effect)

    def _catalog_path(self, run_id: str, attempt_id: str) -> Path:
        return contained_storage_path(
            self.root,
            "ledger-catalog",
            safe_storage_key(
                "effect-ledger",
                _json_dumps([run_id, attempt_id]),
            ),
        )

    def _write_catalog(self, effect: RuntimeEffectRecord, size: int) -> None:
        path = self._catalog_path(effect.run_id, effect.attempt_id)
        payload = {
            "run_id": effect.run_id,
            "attempt_id": effect.attempt_id,
            "size": size,
        }
        _create_durable_directory(path.parent)
        write_text_atomic_no_follow(path, _json_dumps(payload))
        _fsync_dir(path.parent)

    def _read_catalog(self, path: Path) -> dict | None:
        stamp = _file_stamp(path)
        if stamp is None:
            if path in self._catalog_cache:
                raise RuntimeEffectJournalCorruptionError(
                    "Effect catalog was removed",
                )
            return None
        cached = self._catalog_cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            payload = json.loads(read_text_no_follow(path))
            if (
                set(payload) != {"run_id", "attempt_id", "size"}
                or not isinstance(payload["size"], int)
                or payload["size"] <= 0
                or self._catalog_path(payload["run_id"], payload["attempt_id"])
                != path
            ):
                raise ValueError("invalid effect catalog")
        except Exception as exc:
            raise RuntimeEffectJournalCorruptionError(
                "Invalid effect catalog",
            ) from exc
        self._catalog_cache[path] = (stamp, payload)
        return payload

    def _receipt_path(self, effect: RuntimeEffectRecord) -> Path:
        return contained_storage_path(
            self.ledger_path(effect.run_id, effect.attempt_id).parent,
            "receipts",
            safe_storage_key("effect-receipt", effect.effect_id),
        )

    def _confirm_settlement(self, effect: RuntimeEffectRecord) -> None:
        path = self._receipt_path(effect)
        payload = {
            "effect_id": effect.effect_id,
            "revision": effect.revision,
            "record_hash": _checksum_payload(effect.model_dump(mode="json")),
        }
        try:
            _create_durable_directory(path.parent)
            write_text_atomic_no_follow(path, _json_dumps(payload))
            _fsync_dir(path.parent)
        except BaseException:
            # A receipt is issued only after the original append returned.
            # Incomplete confirmation never makes an unknown effect retryable.
            path.unlink(missing_ok=True)
            raise

    def is_effect_settlement_confirmed(
        self, effect: RuntimeEffectRecord
    ) -> bool:
        """Verify a durable receipt for this exact committed ledger record."""
        return (
            effect.status == "committed" and self._settlement_confirmed(effect)
        )

    def _settlement_confirmed(self, effect: RuntimeEffectRecord) -> bool:
        path = self._receipt_path(effect)
        try:
            receipt = json.loads(read_text_no_follow(path))
        except FileNotFoundError:
            return False
        except (ValueError, OSError) as exc:
            raise RuntimeEffectJournalCorruptionError(
                "Invalid effect receipt",
            ) from exc
        return receipt == {
            "effect_id": effect.effect_id,
            "revision": effect.revision,
            "record_hash": _checksum_payload(effect.model_dump(mode="json")),
        }

    def list_effects(
        self,
        run_id: str,
        attempt_id: str,
    ) -> list[RuntimeEffectRecord]:
        return [
            record.model_copy(deep=True)
            for record in self._load_index(run_id, attempt_id).records
        ]

    def _load_index(self, run_id: str, attempt_id: str) -> _EffectIndex:
        path = self.ledger_path(run_id, attempt_id)
        self._ledger_ids[path] = (run_id, attempt_id)
        stamp = _file_stamp(path)
        catalog = self._read_catalog(self._catalog_path(run_id, attempt_id))
        if catalog is not None and (
            stamp is None or stamp[2] < catalog["size"]
        ):
            raise RuntimeEffectJournalCorruptionError(
                "Effect journal is below its durable watermark",
            )
        cached = self._indexes.get(path)
        if cached is not None and cached.stamp == stamp:
            return cached
        if (
            cached is not None
            and cached.stamp is not None
            and (stamp is None or stamp[2] < cached.stamp[2])
        ):
            raise RuntimeEffectJournalCorruptionError(
                "Effect journal was removed or truncated",
            )
        index = _EffectIndex(stamp=stamp)
        if stamp is None:
            self._indexes[path] = index
            return index
        latest_revision_by_effect: dict[str, int] = {}
        for line_number, line in enumerate(
            read_text_no_follow(path).splitlines(),
            1,
        ):
            if not line.strip():
                continue
            try:
                envelope = RuntimeEffectJournalEnvelope.model_validate(
                    json.loads(line),
                )
                if envelope.checksum != _checksum_payload(envelope.payload):
                    raise ValueError("checksum mismatch")
                record = RuntimeEffectRecord.model_validate(envelope.payload)
                if record.run_id != run_id:
                    raise ValueError(
                        f"effect run_id mismatch: {record.run_id} != {run_id}",
                    )
                if record.attempt_id != attempt_id:
                    raise ValueError(
                        "effect attempt_id mismatch: "
                        f"{record.attempt_id} != {attempt_id}",
                    )
                previous_revision = latest_revision_by_effect.get(
                    record.effect_id,
                )
                if (
                    previous_revision is not None
                    and record.revision <= previous_revision
                ):
                    raise ValueError(
                        (
                            "effect revision is not strictly increasing: "
                            f"{record.effect_id}"
                            " "
                            f"{record.revision}"
                            " <= "
                            f"{previous_revision}"
                        ),
                    )
                latest_revision_by_effect[record.effect_id] = record.revision
                index.add(record)
            except Exception as exc:
                raise RuntimeEffectJournalCorruptionError(
                    f"effect journal line {line_number} is corrupt: {exc}",
                ) from exc
        if _file_stamp(path) != stamp:
            raise RuntimeEffectJournalCorruptionError(
                "Effect journal changed during read",
            )
        self._indexes[path] = index
        return index

    def latest_effect(
        self,
        run_id: str,
        attempt_id: str,
        effect_id: str,
    ) -> RuntimeEffectRecord | None:
        record = self._load_index(run_id, attempt_id).latest.get(effect_id)
        return record.model_copy(deep=True) if record is not None else None

    def _load_path_index(self, path: Path) -> _EffectIndex:
        ids = self._ledger_ids.get(path)
        if ids is None:
            # Recover each ledger's unsanitized IDs once, then use its stamp.
            try:
                raw = read_text_no_follow(path).splitlines()
                if not raw:
                    raise ValueError("empty effect journal")
                payload = json.loads(raw[0])["payload"]
                ids = (str(payload["run_id"]), str(payload["attempt_id"]))
                if self.ledger_path(*ids) != path:
                    raise ValueError("effect path mismatch")
            except Exception as exc:
                raise RuntimeEffectJournalCorruptionError(
                    "Invalid effect identity",
                ) from exc
        return self._load_index(*ids)

    def _unresolved_identity_effects(self, effect: RuntimeEffectRecord):
        paths = set(self.root.glob("runs/*/attempts/*/effects.jsonl")) | set(
            self._indexes,
        )
        for catalog_path in self.root.glob("ledger-catalog/*"):
            catalog = self._read_catalog(catalog_path)
            if catalog is not None:
                paths.add(
                    self.ledger_path(catalog["run_id"], catalog["attempt_id"]),
                )
        for path in paths:
            index = self._load_path_index(path)
            yield from index.unresolved.get(
                _effect_identity(effect),
                {},
            ).values()

    def retry_idempotency_key(
        self,
        effect: RuntimeEffectRecord,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> str | None:
        if (
            recovery.idempotency != "idempotency_key"
            or recovery.idempotency_key_transport != "runtime_context"
        ):
            return None
        pending = [
            record
            for record in self._unresolved_identity_effects(effect)
            if record.status in {"running", "ambiguous"}
        ]
        keys = {record.idempotency_key for record in pending}
        return next(iter(keys)) if len(keys) == 1 else None

    def assert_effect_admissible(
        self,
        effect: RuntimeEffectRecord,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> None:
        if not recovery.external_side_effect:
            return
        for previous in self._unresolved_identity_effects(effect):
            if previous.status == "committed":
                if self._settlement_confirmed(previous):
                    continue
                raise RuntimeEffectUnresolvedError(previous)
            decision = assess_runtime_effect_ambiguity(
                recovery,
                effect_id=previous.effect_id,
            )
            if not decision.automatic_external_call_allowed or (
                recovery.idempotency == "idempotency_key"
                and previous.idempotency_key != effect.idempotency_key
            ):
                raise RuntimeEffectUnresolvedError(previous)

    def _append_envelope(
        self,
        path: Path,
        effect: RuntimeEffectRecord,
    ) -> None:
        payload = effect.model_dump(mode="json")
        envelope = RuntimeEffectJournalEnvelope(
            checksum=_checksum_payload(payload),
            payload=payload,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(self.root, 0o700)
        _chmod_best_effort(path.parent, 0o700)
        if path.is_symlink():
            raise ValueError("Storage path is a symlink")
        fd = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                fd = -1
                handle.write(
                    _json_dumps(envelope.model_dump(mode="json")) + "\n",
                )
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        _chmod_best_effort(path, 0o600)
        _fsync_dir(path.parent)
