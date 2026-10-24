# -*- coding: utf-8 -*-
"""Deterministic Runtime health audit and bounded self-recovery."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)

from ..effects import RuntimeEffectJournal
from ..runs import RuntimeEffectRecord, TERMINAL_RUNTIME_RUN_STATUSES
from ..store import RuntimeRunStore, RuntimeRunStoreError
from ..turn_control import RuntimeInputConflictError, RuntimeTurnControlStore
from .contracts import ExecutionFence
from .integrity import canonical_sha256
from .surface import (
    ModelRequestManifestConflictError,
    ModelRequestManifestStore,
)


HealthLevel = Literal["L0", "L1", "L2", "L3"]
FindingCategory = Literal[
    "identity",
    "input",
    "surface",
    "effect",
    "storage",
    "authority",
]
RecoveryStatus = Literal["executed", "noop", "blocked", "failed"]

_LEVEL_RANK: dict[HealthLevel, int] = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HarnessHealthFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    level: HealthLevel
    category: FindingCategory
    summary: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    user_visible: bool = False
    safe_automatic_repair: bool = False


class HarnessRecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    kind: Literal["release_orphaned_input_claim"]
    run_id: str
    attempt_id: str
    safe_automatic: bool = True
    precondition_sha256: str
    parameters: dict[str, Any]


class HarnessHealthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    snapshot_id: str
    run_id: str
    attempt_id: str
    checked_at: datetime
    level: HealthLevel
    findings: tuple[HarnessHealthFinding, ...] = ()
    recovery_actions: tuple[HarnessRecoveryAction, ...] = ()
    state_sha256: str
    integrity_sha256: str

    def verify_integrity(self) -> bool:
        return (
            canonical_sha256(
                self.model_dump(mode="json", exclude={"integrity_sha256"}),
            )
            == self.integrity_sha256
        )

    def model_capsule(self) -> dict[str, Any] | None:
        """Expose only relevant control facts, never raw audit evidence."""

        if _LEVEL_RANK[self.level] < _LEVEL_RANK["L2"]:
            return None
        return {
            "schema_version": 1,
            "level": self.level,
            "codes": [finding.code for finding in self.findings],
            "stop_external_effects": self.level == "L3",
            "guidance": (
                "Do not execute or retry external effects. Explain the "
                "bounded runtime uncertainty and ask the user before "
                "proceeding."
                if self.level == "L3"
                else (
                    "Continue with available evidence, do not claim missing "
                    "evidence as fact, and do not silently retry ambiguous "
                    "work."
                )
            ),
            "snapshot_sha256": self.integrity_sha256,
        }


class HarnessRecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    status: RecoveryStatus
    occurred_at: datetime = Field(default_factory=_utc_now)
    evidence: dict[str, Any] = Field(default_factory=dict)


class HarnessHealthService:
    """Audit existing Runtime authorities; repair only proven-safe cases."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )
        self.run_store = RuntimeRunStore(self.workspace_dir)
        self.effect_journal = RuntimeEffectJournal(self.workspace_dir)
        self.turn_store = RuntimeTurnControlStore(self.workspace_dir)
        self.manifest_store = ModelRequestManifestStore(self.workspace_dir)

    async def audit(
        self,
        *,
        run_id: str,
        attempt_id: str,
        stale_claim_after_seconds: float = 30.0,
        persist: bool = True,
    ) -> HarnessHealthSnapshot:
        now = _utc_now()
        findings: list[HarnessHealthFinding] = []
        actions: list[HarnessRecoveryAction] = []
        state_rows: dict[str, Any] = {
            "run_id": run_id,
            "attempt_id": attempt_id,
        }

        contract = None
        try:
            contract = await self.run_store.load_contract(run_id)
        except RuntimeRunStoreError as exc:
            findings.append(
                HarnessHealthFinding(
                    code="runtime_contract_corrupt",
                    level="L3",
                    category="storage",
                    summary="Runtime authority could not be verified.",
                    evidence={"error_type": type(exc).__name__},
                    user_visible=True,
                ),
            )
        if contract is None:
            findings.append(
                HarnessHealthFinding(
                    code="runtime_contract_missing",
                    level="L3",
                    category="identity",
                    summary="Runtime authority is missing for the active run.",
                    user_visible=True,
                ),
            )
        else:
            state_rows["contract"] = contract.model_dump(mode="json")
            if contract.attempt_id != attempt_id:
                findings.append(
                    HarnessHealthFinding(
                        code="runtime_attempt_mismatch",
                        level="L3",
                        category="identity",
                        summary=(
                            "The active Attempt does not own this RuntimeRun."
                        ),
                        evidence={
                            "expected_attempt_id": attempt_id,
                            "actual_attempt_id": contract.attempt_id,
                        },
                        user_visible=True,
                    ),
                )

        manifests = []
        try:
            manifests = self.manifest_store.list_for_attempt(
                run_id=run_id,
                attempt_id=attempt_id,
            )
        except (ModelRequestManifestConflictError, OSError, ValueError) as exc:
            findings.append(
                HarnessHealthFinding(
                    code="model_surface_corrupt",
                    level="L3",
                    category="surface",
                    summary=(
                        "A model-visible Surface Manifest failed integrity."
                    ),
                    evidence={"error_type": type(exc).__name__},
                    user_visible=True,
                ),
            )
        state_rows["manifest_hashes"] = [
            manifest.integrity_sha256 for manifest in manifests
        ]
        admitted_input_ids = {
            client_turn_id
            for manifest in manifests
            for client_turn_id in manifest.admitted_input_ids
        }
        for manifest in manifests:
            if manifest.attempt_id != attempt_id:
                findings.append(
                    HarnessHealthFinding(
                        code="surface_attempt_mismatch",
                        level="L3",
                        category="surface",
                        summary="A model surface belongs to another Attempt.",
                        evidence={"manifest_id": manifest.manifest_id},
                        user_visible=True,
                    ),
                )
            witness = manifest.progress_witness
            if witness.unpaired_tool_result_ids:
                findings.append(
                    HarnessHealthFinding(
                        code="surface_unpaired_tool_result",
                        level="L2",
                        category="surface",
                        summary=(
                            "A tool result has no matching call on the model "
                            "surface."
                        ),
                        evidence={
                            "manifest_id": manifest.manifest_id,
                            "tool_result_ids": list(
                                witness.unpaired_tool_result_ids
                            ),
                        },
                        user_visible=True,
                    ),
                )
            for compaction in manifest.compactions:
                source = compaction.source
                if source.retrievable and source.storage_ref:
                    source_path = contained_storage_path(
                        self.root,
                        *Path(source.storage_ref).parts,
                    )
                    if not source_path.is_file():
                        findings.append(
                            HarnessHealthFinding(
                                code="source_receipt_missing",
                                level="L2",
                                category="storage",
                                summary=(
                                    "Compacted evidence cannot currently be "
                                    "retrieved."
                                ),
                                evidence={"source_id": source.source_id},
                                user_visible=True,
                            ),
                        )

        inbox_items = {
            item.seq: item for item in self.turn_store.read_inbox(run_id)
        }
        dispositions = self.turn_store.current_dispositions(run_id)
        state_rows["input_dispositions"] = {
            str(seq): disposition.model_dump(mode="json")
            for seq, disposition in dispositions.items()
        }
        for seq, disposition in dispositions.items():
            item = inbox_items.get(seq)
            if item is None:
                findings.append(
                    HarnessHealthFinding(
                        code="input_disposition_without_item",
                        level="L3",
                        category="input",
                        summary=(
                            "Input state references a missing admitted input."
                        ),
                        evidence={"item_seq": seq},
                        user_visible=True,
                    ),
                )
                continue
            if disposition.status == "claimed":
                age_seconds = max(
                    0.0,
                    (now - disposition.occurred_at).total_seconds(),
                )
                if age_seconds < max(0.0, stale_claim_after_seconds):
                    continue
                if disposition.client_turn_id in admitted_input_ids:
                    findings.append(
                        HarnessHealthFinding(
                            code="input_claim_dispatched_without_settlement",
                            level="L2",
                            category="input",
                            summary=(
                                "Input reached a model surface but its claim "
                                "did not settle."
                            ),
                            evidence={
                                "item_seq": seq,
                                "client_turn_id": disposition.client_turn_id,
                            },
                            user_visible=True,
                        ),
                    )
                    continue
                precondition = canonical_sha256(
                    disposition.model_dump(mode="json"),
                )
                action = HarnessRecoveryAction(
                    action_id=(
                        f"repair:release-input:{run_id}:{seq}:"
                        f"{precondition[:12]}"
                    ),
                    kind="release_orphaned_input_claim",
                    run_id=run_id,
                    attempt_id=attempt_id,
                    precondition_sha256=precondition,
                    parameters={
                        "item_seq": seq,
                        "fence": disposition.fence.model_dump(mode="json"),
                    },
                )
                actions.append(action)
                findings.append(
                    HarnessHealthFinding(
                        code="input_claim_orphaned_before_dispatch",
                        level="L1",
                        category="input",
                        summary=(
                            "An input claim has no model surface and can be "
                            "released safely."
                        ),
                        evidence={"item_seq": seq},
                        safe_automatic_repair=True,
                    ),
                )
            elif (
                disposition.status == "consumed"
                and disposition.client_turn_id not in admitted_input_ids
            ):
                findings.append(
                    HarnessHealthFinding(
                        code="input_consumed_without_surface",
                        level="L2",
                        category="input",
                        summary=(
                            "Consumed input lacks a reconstructable model "
                            "surface."
                        ),
                        evidence={"item_seq": seq},
                        user_visible=True,
                    ),
                )

        effects = self.effect_journal.list_effects(run_id, attempt_id)
        state_rows["effects"] = [
            effect.model_dump(mode="json") for effect in effects
        ]
        latest_effects: dict[str, RuntimeEffectRecord] = {}
        for effect in effects:
            current = latest_effects.get(effect.effect_id)
            if current is None or effect.revision > current.revision:
                latest_effects[effect.effect_id] = effect
        for effect in latest_effects.values():
            if effect.status == "ambiguous":
                findings.append(
                    HarnessHealthFinding(
                        code="effect_ambiguous",
                        level="L3",
                        category="effect",
                        summary=(
                            "An external effect has an ambiguous settlement."
                        ),
                        evidence={"effect_id": effect.effect_id},
                        user_visible=True,
                    ),
                )
            elif (
                effect.status == "running"
                and contract is not None
                and contract.status in TERMINAL_RUNTIME_RUN_STATUSES
            ):
                findings.append(
                    HarnessHealthFinding(
                        code="effect_running_after_terminal_run",
                        level="L3",
                        category="effect",
                        summary=(
                            "A terminal run still owns an unsettled external "
                            "effect."
                        ),
                        evidence={"effect_id": effect.effect_id},
                        user_visible=True,
                    ),
                )

        level: HealthLevel = max(
            (finding.level for finding in findings),
            key=_LEVEL_RANK.__getitem__,
            default="L0",
        )
        checked_at = now
        state_sha256 = canonical_sha256(state_rows)
        snapshot_payload = {
            "schema_version": 1,
            "snapshot_id": (
                f"health:{run_id}:{attempt_id}:{state_sha256[:12]}:"
                f"{canonical_sha256(checked_at)[:8]}"
            ),
            "run_id": run_id,
            "attempt_id": attempt_id,
            "checked_at": checked_at,
            "level": level,
            "findings": tuple(findings),
            "recovery_actions": tuple(actions),
            "state_sha256": state_sha256,
        }
        snapshot = HarnessHealthSnapshot(
            **snapshot_payload,
            integrity_sha256=canonical_sha256(snapshot_payload),
        )
        if persist:
            self._persist_snapshot(snapshot)
        return snapshot

    def execute_safe_recovery(
        self,
        action: HarnessRecoveryAction,
    ) -> HarnessRecoveryResult:
        """Execute one idempotent L1 action or fail without widening scope."""

        existing_path = self._recovery_path(action.run_id, action.action_id)
        if existing_path.is_file():
            return HarnessRecoveryResult.model_validate_json(
                read_text_no_follow(existing_path),
            )

        if not action.safe_automatic:
            result = HarnessRecoveryResult(
                action_id=action.action_id,
                status="blocked",
                evidence={"reason": "action is not safe automatic recovery"},
            )
            self._persist_recovery(action.run_id, result)
            return result
        if action.kind != "release_orphaned_input_claim":
            result = HarnessRecoveryResult(
                action_id=action.action_id,
                status="blocked",
                evidence={"reason": "unsupported recovery action"},
            )
            self._persist_recovery(action.run_id, result)
            return result
        item_seq = int(action.parameters["item_seq"])
        current = self.turn_store.current_dispositions(action.run_id).get(
            item_seq
        )
        if current is None:
            result = HarnessRecoveryResult(
                action_id=action.action_id,
                status="noop",
                evidence={"reason": "claim no longer exists"},
            )
            self._persist_recovery(action.run_id, result)
            return result
        current_sha256 = canonical_sha256(current.model_dump(mode="json"))
        if current_sha256 != action.precondition_sha256:
            result = HarnessRecoveryResult(
                action_id=action.action_id,
                status="blocked",
                evidence={"reason": "recovery precondition changed"},
            )
            self._persist_recovery(action.run_id, result)
            return result
        try:
            released = self.turn_store.release_input_claim(
                run_id=action.run_id,
                item_seq=item_seq,
                recovery_actor_id="harness:l1-recovery",
                fence=ExecutionFence.model_validate(
                    action.parameters["fence"]
                ),
                reason="orphaned before any durable model surface",
            )
        except RuntimeInputConflictError as exc:
            result = HarnessRecoveryResult(
                action_id=action.action_id,
                status="blocked",
                evidence={"reason": str(exc)},
            )
        else:
            result = HarnessRecoveryResult(
                action_id=action.action_id,
                status="executed",
                evidence={
                    "item_seq": item_seq,
                    "disposition_event_id": released.event_id,
                },
            )
        self._persist_recovery(action.run_id, result)
        return result

    def _health_dir(self, run_id: str) -> Path:
        run_key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(self.root, "runs", run_key, "health")

    def _persist_snapshot(self, snapshot: HarnessHealthSnapshot) -> None:
        filename = safe_storage_key(
            "harness-health",
            snapshot.snapshot_id,
            suffix=".json",
        )
        path = contained_storage_path(
            self._health_dir(snapshot.run_id),
            "snapshots",
            filename,
        )
        write_text_atomic_no_follow(path, snapshot.model_dump_json(indent=2))

    def _persist_recovery(
        self,
        run_id: str,
        result: HarnessRecoveryResult,
    ) -> None:
        path = self._recovery_path(run_id, result.action_id)
        write_text_atomic_no_follow(path, result.model_dump_json(indent=2))

    def _recovery_path(self, run_id: str, action_id: str) -> Path:
        filename = safe_storage_key(
            "harness-recovery",
            action_id,
            suffix=".json",
        )
        return contained_storage_path(
            self._health_dir(run_id),
            "recoveries",
            filename,
        )


__all__ = [
    "FindingCategory",
    "HarnessHealthFinding",
    "HarnessHealthService",
    "HarnessHealthSnapshot",
    "HarnessRecoveryAction",
    "HarnessRecoveryResult",
    "HealthLevel",
    "RecoveryStatus",
]
