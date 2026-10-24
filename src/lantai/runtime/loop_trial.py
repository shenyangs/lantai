# -*- coding: utf-8 -*-
"""Small recoverable LOOP-05 trial runner.

This module is deliberately deterministic and side-effect free. It exercises the
same RuntimeRunStore, LoopContract, progress sidecar, evaluator, and reducer
paths that a real long-running worker must use, without touching external
systems.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from .loop_evaluator import (
    LoopEvaluationExecutor,
    LoopEvaluationGateRequest,
    LoopEvaluationRubricRequest,
)
from .loop_runtime import (
    assess_loop_runtime_run_stop_conditions_from_store,
    save_runtime_run_loop_stop_assessment,
)
from .loops import (
    LoopBudget,
    LoopBudgetUsage,
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopDeterministicGateResult,
    LoopEditionPolicy,
    LoopHumanCheckpoint,
    LoopInput,
    LoopProgressWindow,
    LoopRubric,
    LoopRubricDimension,
    LoopRubricDimensionScore,
    LoopStopConditions,
    advance_loop_progress_window,
    loop_contract_with_hash,
)
from .runs import (
    RuntimeBudget,
    RuntimeAttemptContract,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReducerAction,
    RuntimeRunReason,
    RuntimeStepRef,
    assess_runtime_run_heartbeat,
    reduce_runtime_run,
    start_runtime_run_attempt,
)
from .store import RuntimeRunStore


ReleaseTrialDistribution = Literal["official", "mas"]
BrowserContentHumanGateDecision = Literal["pending", "approved", "rejected"]
LoopInterruptionMatrixScenario = Literal[
    "transport_disconnect",
    "process_kill_recovery",
    "pause_resume",
    "failure_retry",
    "budget_exhaustion",
    "user_stop",
    "browser_human_gate",
]

LOOP_INTERRUPTION_MATRIX_SCENARIOS: tuple[LoopInterruptionMatrixScenario, ...] = (
    "transport_disconnect",
    "process_kill_recovery",
    "pause_resume",
    "failure_retry",
    "budget_exhaustion",
    "user_stop",
    "browser_human_gate",
)
_LOOP_INTERRUPTION_MATRIX_EXPECTED_STATUSES: Mapping[
    LoopInterruptionMatrixScenario,
    str,
] = {
    "transport_disconnect": "running",
    "process_kill_recovery": "stalled",
    "pause_resume": "running",
    "failure_retry": "running",
    "budget_exhaustion": "stalled",
    "user_stop": "cancelled",
    "browser_human_gate": "succeeded",
}
_LOOP_INTERRUPTION_MATRIX_EXPECTED_ERROR_CODES: Mapping[
    LoopInterruptionMatrixScenario,
    str | None,
] = {
    "transport_disconnect": None,
    "process_kill_recovery": "loop_no_progress_exhausted",
    "pause_resume": None,
    "failure_retry": None,
    "budget_exhaustion": "loop_step_budget_exhausted",
    "user_stop": None,
    "browser_human_gate": None,
}
_LOOP_INTERRUPTION_MATRIX_EXPECTED_STOP_REASONS: Mapping[
    LoopInterruptionMatrixScenario,
    str | None,
] = {
    "transport_disconnect": None,
    "process_kill_recovery": None,
    "pause_resume": None,
    "failure_retry": None,
    "budget_exhaustion": None,
    "user_stop": "loop_human_stop_requested",
    "browser_human_gate": None,
}


@dataclass(frozen=True)
class LoopTrialResult:
    run_id: str
    attempt_id: str
    status: str
    revision: int
    iterations_executed: int
    progress: LoopProgressWindow
    error_code: str | None = None
    evidence_hash: str | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseVerificationEvidence:
    distribution: ReleaseTrialDistribution
    artifact_manifest_path: str
    release_version: str
    release_arch: str
    signing_mode: str
    artifact_names: tuple[str, ...]
    evidence_hash: str
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserContentDraftEvidence:
    draft_id: str
    title: str
    draft_status: str
    preview_url: str
    queue_status: str
    requires_confirmation: bool
    evidence_hash: str
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoopInterruptionMatrixResult:
    run_id_prefix: str
    ok: bool
    scenario_statuses: Mapping[str, str]
    error_codes: Mapping[str, str | None]
    stop_reason_codes: Mapping[str, str | None]
    attempt_ids: Mapping[str, str | None]
    evidence_hash: str
    issues: tuple[str, ...] = ()
    details: Mapping[str, Any] | None = None


class _PassingGateRunner:
    def run_gate(self, request: LoopEvaluationGateRequest) -> LoopDeterministicGateResult:
        return LoopDeterministicGateResult(
            gate_id=request.gate_id,
            status="passed",
            evidence_hash=f"sha256:{request.gate_id}",
            summary="Trial gate passed.",
        )


class _TrialRubricScorer:
    def score_dimension(
        self,
        request: LoopEvaluationRubricRequest,
    ) -> LoopRubricDimensionScore:
        return LoopRubricDimensionScore(
            dimension_id=request.dimension_id,
            score=3.5,
            rationale_summary="Trial has not reached the passing target yet.",
            evidence_hash=f"sha256:{request.dimension_id}",
        )


class _ReleaseVerificationGateRunner:
    def __init__(self, *, issues: tuple[str, ...], evidence_hash: str) -> None:
        self._issues = issues
        self._evidence_hash = evidence_hash

    def run_gate(self, request: LoopEvaluationGateRequest) -> LoopDeterministicGateResult:
        if self._issues:
            return LoopDeterministicGateResult(
                gate_id=request.gate_id,
                status="failed",
                evidence_hash=self._evidence_hash,
                summary=f"Release verification failed with {len(self._issues)} issue(s).",
            )
        return LoopDeterministicGateResult(
            gate_id=request.gate_id,
            status="passed",
            evidence_hash=self._evidence_hash,
            summary="Release artifacts are locally verifiable and no publish receipt is present.",
        )


class _ReleaseVerificationRubricScorer:
    def __init__(self, *, issues: tuple[str, ...], evidence_hash: str) -> None:
        self._issues = issues
        self._evidence_hash = evidence_hash

    def score_dimension(
        self,
        request: LoopEvaluationRubricRequest,
    ) -> LoopRubricDimensionScore:
        if self._issues:
            return LoopRubricDimensionScore(
                dimension_id=request.dimension_id,
                score=1,
                rationale_summary="Release verification has blocking manifest or artifact issues.",
                evidence_hash=self._evidence_hash,
            )
        return LoopRubricDimensionScore(
            dimension_id=request.dimension_id,
            score=5,
            rationale_summary="Release verification evidence satisfied the no-publish trial.",
            evidence_hash=self._evidence_hash,
        )


class _BrowserContentGateRunner:
    def __init__(self, *, issues: tuple[str, ...], evidence_hash: str) -> None:
        self._issues = issues
        self._evidence_hash = evidence_hash

    def run_gate(self, request: LoopEvaluationGateRequest) -> LoopDeterministicGateResult:
        if self._issues:
            return LoopDeterministicGateResult(
                gate_id=request.gate_id,
                status="failed",
                evidence_hash=self._evidence_hash,
                summary=f"Browser content draft/preview evidence has {len(self._issues)} issue(s).",
            )
        return LoopDeterministicGateResult(
            gate_id=request.gate_id,
            status="passed",
            evidence_hash=self._evidence_hash,
            summary="Browser content remained at local draft/preview and publish requires a human gate.",
        )


class _BrowserContentRubricScorer:
    def __init__(self, *, issues: tuple[str, ...], evidence_hash: str) -> None:
        self._issues = issues
        self._evidence_hash = evidence_hash

    def score_dimension(
        self,
        request: LoopEvaluationRubricRequest,
    ) -> LoopRubricDimensionScore:
        if self._issues:
            return LoopRubricDimensionScore(
                dimension_id=request.dimension_id,
                score=1,
                rationale_summary="Browser content trial has unsafe publish or preview evidence.",
                evidence_hash=self._evidence_hash,
            )
        return LoopRubricDimensionScore(
            dimension_id=request.dimension_id,
            score=5,
            rationale_summary="Browser content trial satisfied draft/preview human-gate policy.",
            evidence_hash=self._evidence_hash,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_payload(value: object) -> str:
    return f"sha256:{hashlib.sha256(_stable_json(value).encode('utf-8')).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trial_contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-trial-contract",
        objective="Exercise a recoverable long-running Loop trial",
        scope=["RuntimeRunStore", "progress sidecar", "evaluation report"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="trial-state",
                title="Recoverable trial state",
                acceptance=["Progress and evaluation survive process restart"],
            )
        ],
        acceptance=["The trial stops through the RuntimeRun reducer"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="trial-gate",
                name="Trial deterministic gate",
                kind="manual_check",
                expected="Trial gate passes without external side effects",
            )
        ],
        rubric=LoopRubric(
            rubric_id="loop-trial-rubric",
            passing_score=4,
            dimensions=[
                LoopRubricDimension(
                    dimension_id="evidence",
                    name="Evidence",
                    weight=1,
                    target_score=4,
                )
            ],
        ),
        stop_conditions=LoopStopConditions(max_no_progress_iterations=2),
    )


def _release_verification_contract(
    evidence: tuple[ReleaseVerificationEvidence, ...],
) -> LoopContract:
    return LoopContract(
        contract_id="loop-release-verification-contract",
        objective="Verify dual-distribution release artifacts without publishing",
        scope=[
            "official artifact manifest",
            "MAS artifact manifest",
            "local artifact integrity",
            "supply-chain manifest",
            "no-publish guard",
        ],
        out_of_scope=[
            "notarization submission",
            "GitHub Release upload",
            "App Store Connect upload",
            "TestFlight submission",
        ],
        inputs=[
            LoopInput(
                input_id=f"{item.distribution}-artifact-manifest",
                kind="artifact",
                title=f"{item.distribution} artifact manifest",
                uri=item.artifact_manifest_path,
                evidence_hash=item.evidence_hash,
            )
            for item in evidence
        ],
        deliverables=[
            LoopDeliverable(
                deliverable_id="release-dry-run-report",
                title="Local release verification report",
                acceptance=[
                    "Both distribution manifests are present and match their expected signing mode",
                    "Referenced local artifacts and supply-chain evidence are hash-checked",
                    "No publish/upload receipt is present in the inspected manifests",
                ],
            )
        ],
        acceptance=[
            "Release verification succeeds without invoking publishing, upload, or notarization commands",
            "Official and MAS manifests remain distinct and edition-aware",
        ],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="release-dry-run-verification",
                name="Release dry-run manifest verification",
                kind="artifact_exists",
                expected="Local artifacts verify and no publish receipt is present",
            )
        ],
        rubric=LoopRubric(
            rubric_id="loop-release-verification-rubric",
            passing_score=4,
            dimensions=[
                LoopRubricDimension(
                    dimension_id="artifact-integrity",
                    name="Artifact integrity",
                    weight=1,
                    target_score=4,
                ),
                LoopRubricDimension(
                    dimension_id="dual-distribution",
                    name="Dual-distribution separation",
                    weight=1,
                    target_score=4,
                ),
                LoopRubricDimension(
                    dimension_id="release-safety",
                    name="No-publish release safety",
                    weight=1,
                    target_score=4,
                ),
            ],
        ),
        edition_policy=LoopEditionPolicy(
            release_scope="both",
            tool_policy="disabled",
            network_policy="disabled",
            file_policy="read_only",
            browser_policy="disabled",
            shell_policy="disabled",
        ),
        stop_conditions=LoopStopConditions(rubric_score_at_least=4),
    )


def _browser_content_contract(evidence: BrowserContentDraftEvidence) -> LoopContract:
    return LoopContract(
        contract_id="loop-browser-content-draft-preview-contract",
        objective="Prepare Browser content only to draft/preview with publish behind a human gate",
        scope=[
            "content draft",
            "preview evidence",
            "publishing queue confirmation",
            "human gate",
        ],
        out_of_scope=[
            "automatic publish",
            "platform write after approval",
            "social account submission",
        ],
        inputs=[
            LoopInput(
                input_id="browser-content-draft",
                kind="artifact",
                title=evidence.title,
                uri=evidence.preview_url,
                evidence_hash=evidence.evidence_hash,
            )
        ],
        deliverables=[
            LoopDeliverable(
                deliverable_id="browser-content-draft-preview",
                title="Browser content draft and preview",
                acceptance=[
                    "Draft remains local or ready-for-review before approval",
                    "Preview evidence exists before publish",
                    "Publish queue requires explicit human confirmation",
                ],
            )
        ],
        acceptance=[
            "No publish/upload receipt exists before the human gate",
            "Pending gate leaves the RuntimeRun waiting for approval",
            "Approved gate resumes the same run and settles through the reducer",
        ],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="browser-draft-preview-only",
                name="Browser draft/preview-only safety gate",
                kind="manual_check",
                expected="Draft and preview exist; publish remains gated by a human confirmation queue.",
            )
        ],
        rubric=LoopRubric(
            rubric_id="loop-browser-content-human-gate-rubric",
            passing_score=4,
            dimensions=[
                LoopRubricDimension(
                    dimension_id="draft-preview",
                    name="Draft and preview evidence",
                    weight=1,
                    target_score=4,
                ),
                LoopRubricDimension(
                    dimension_id="publish-gate",
                    name="Publish human gate",
                    weight=1,
                    target_score=4,
                ),
            ],
        ),
        edition_policy=LoopEditionPolicy(
            release_scope="both",
            tool_policy="disabled",
            network_policy="disabled",
            file_policy="read_only",
            browser_policy="interactive_only",
            shell_policy="disabled",
        ),
        human_checkpoints=[
            LoopHumanCheckpoint(
                checkpoint_id="browser-publish-confirmation",
                title="Approve Browser content publish handoff",
                required_before="publish_or_platform_write",
            )
        ],
        stop_conditions=LoopStopConditions(rubric_score_at_least=4),
    )


def _interruption_matrix_budget_contract() -> LoopContract:
    return _trial_contract().model_copy(
        update={
            "contract_id": "loop-interruption-matrix-budget-contract",
            "objective": "Exercise explicit Loop budget exhaustion handling",
            "budgets": LoopBudget(max_steps=1),
            "stop_conditions": LoopStopConditions(),
        },
    )


def _interruption_matrix_browser_payload() -> Mapping[str, Any]:
    return {
        "draft": {
            "id": "matrix-draft-1",
            "platform": "local_markdown",
            "title": "Runtime migration field notes",
            "body": "Draft body prepared from browser research evidence.",
            "summary": "A local draft prepared for review.",
            "status": "ready_for_review",
            "preview_url": "preview://matrix-draft-1",
        },
        "preview": {
            "url": "preview://matrix-draft-1",
            "status": "ready",
        },
        "queue_item": {
            "id": "matrix-queue-1",
            "draft_id": "matrix-draft-1",
            "platform": "wechat_official_account",
            "status": "needs_confirmation",
            "requires_confirmation": True,
        },
        "publish_action": {
            "action": "publish",
            "page_kind": "publish",
            "requires_human_gate": True,
            "requires_manual_takeover": True,
            "executed": False,
        },
    }


def _interruption_matrix_run_id(
    run_id_prefix: str,
    scenario: LoopInterruptionMatrixScenario,
) -> str:
    return f"{run_id_prefix}-{scenario.replace('_', '-')}"


def _interruption_matrix_result_from_state(
    *,
    run_id_prefix: str,
    scenario_statuses: Mapping[str, str],
    error_codes: Mapping[str, str | None],
    stop_reason_codes: Mapping[str, str | None],
    attempt_ids: Mapping[str, str | None],
    extra_issues: tuple[str, ...] = (),
    details: Mapping[str, Any] | None = None,
) -> LoopInterruptionMatrixResult:
    issues = list(extra_issues)
    for scenario in LOOP_INTERRUPTION_MATRIX_SCENARIOS:
        actual_status = scenario_statuses.get(scenario, "missing")
        expected_status = _LOOP_INTERRUPTION_MATRIX_EXPECTED_STATUSES[scenario]
        if actual_status != expected_status:
            issues.append(
                f"interruption-matrix:{scenario} expected status "
                f"{expected_status}, got {actual_status}",
            )

        actual_error = error_codes.get(scenario)
        expected_error = _LOOP_INTERRUPTION_MATRIX_EXPECTED_ERROR_CODES[scenario]
        if actual_error != expected_error:
            issues.append(
                f"interruption-matrix:{scenario} expected error "
                f"{expected_error!r}, got {actual_error!r}",
            )

        actual_stop_reason = stop_reason_codes.get(scenario)
        expected_stop_reason = _LOOP_INTERRUPTION_MATRIX_EXPECTED_STOP_REASONS[scenario]
        if actual_stop_reason != expected_stop_reason:
            issues.append(
                f"interruption-matrix:{scenario} expected stop reason "
                f"{expected_stop_reason!r}, got {actual_stop_reason!r}",
            )

    evidence_payload = {
        "run_id_prefix": run_id_prefix,
        "scenario_statuses": dict(sorted(scenario_statuses.items())),
        "error_codes": dict(sorted(error_codes.items())),
        "stop_reason_codes": dict(sorted(stop_reason_codes.items())),
        "attempt_ids": dict(sorted(attempt_ids.items())),
        "issues": sorted(issues),
        "details": details or {},
    }
    return LoopInterruptionMatrixResult(
        run_id_prefix=run_id_prefix,
        ok=not issues,
        scenario_statuses=dict(scenario_statuses),
        error_codes=dict(error_codes),
        stop_reason_codes=dict(stop_reason_codes),
        attempt_ids=dict(attempt_ids),
        evidence_hash=_sha256_payload(evidence_payload),
        issues=tuple(sorted(issues)),
        details=dict(details or {}),
    )


async def _collect_interruption_matrix_state(
    store: RuntimeRunStore,
    *,
    run_id_prefix: str,
    extra_issues: tuple[str, ...] = (),
    details: Mapping[str, Any] | None = None,
) -> LoopInterruptionMatrixResult:
    scenario_statuses: dict[str, str] = {}
    error_codes: dict[str, str | None] = {}
    stop_reason_codes: dict[str, str | None] = {}
    attempt_ids: dict[str, str | None] = {}
    collected_details: dict[str, Any] = dict(details or {})
    previous_attempt_ids: dict[str, str | None] = {}

    for scenario in LOOP_INTERRUPTION_MATRIX_SCENARIOS:
        run = await store.load_contract(
            _interruption_matrix_run_id(run_id_prefix, scenario),
        )
        if run is None:
            scenario_statuses[scenario] = "missing"
            error_codes[scenario] = None
            stop_reason_codes[scenario] = None
            attempt_ids[scenario] = None
            previous_attempt_ids[scenario] = None
            continue
        scenario_statuses[scenario] = run.status
        error_codes[scenario] = run.error.code if run.error else None
        stop_reason_codes[scenario] = run.stop_reason.code if run.stop_reason else None
        attempt_ids[scenario] = run.attempt_id
        previous_attempt_ids[scenario] = (
            run.previous_attempt_id
            if isinstance(run, RuntimeAttemptContract)
            else None
        )

    collected_details.setdefault("previous_attempt_ids", previous_attempt_ids)
    return _interruption_matrix_result_from_state(
        run_id_prefix=run_id_prefix,
        scenario_statuses=scenario_statuses,
        error_codes=error_codes,
        stop_reason_codes=stop_reason_codes,
        attempt_ids=attempt_ids,
        extra_issues=extra_issues,
        details=collected_details,
    )


async def inspect_loop_interruption_matrix_trial(
    workspace_dir: Path | str,
    *,
    run_id_prefix: str = "loop-interruption-matrix",
) -> LoopInterruptionMatrixResult:
    """Inspect a persisted LOOP interruption matrix without mutating it."""

    workspace = Path(workspace_dir).expanduser()
    store = RuntimeRunStore(workspace)
    return await _collect_interruption_matrix_state(
        store,
        run_id_prefix=run_id_prefix,
    )


def _owner(
    workspace_dir: Path,
    *,
    session_id: str = "console:loop-trial",
) -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="default",
        agent_id="agent-default",
        session_id=session_id,
        workspace_id=str(workspace_dir),
        channel="console",
    )


def _base_run(
    workspace_dir: Path,
    *,
    run_id: str,
    attempt_id: str,
    contract_hash: str,
    release_edition: Literal["official", "mas"],
    lease_owner: str = "loop-trial-worker",
    session_id: str = "console:loop-trial",
    policy_snapshot_hash: str = "sha256:loop-trial-policy",
    now: datetime,
) -> RuntimeRunContract:
    return RuntimeRunContract(
        run_id=run_id,
        attempt_id=attempt_id,
        source="capability",
        owner=_owner(workspace_dir, session_id=session_id),
        status="queued",
        revision=0,
        lease_owner=lease_owner,
        lease_expires_at=now + timedelta(minutes=15),
        created_at=now,
        budget=RuntimeBudget(max_steps=10, deadline_at=now + timedelta(minutes=15)),
        contract_hash=contract_hash,
        policy_snapshot_hash=policy_snapshot_hash,
        release_edition=release_edition,
    )


async def _load_or_start_run(
    store: RuntimeRunStore,
    workspace_dir: Path,
    *,
    run_id: str,
    contract_hash: str,
    release_edition: Literal["official", "mas"],
    lease_owner: str = "loop-trial-worker",
    session_id: str = "console:loop-trial",
    policy_snapshot_hash: str = "sha256:loop-trial-policy",
    now: datetime,
) -> RuntimeRunContract:
    existing = await store.load_contract(run_id)
    if existing is not None:
        return existing

    queued = _base_run(
        workspace_dir,
        run_id=run_id,
        attempt_id=f"{run_id}:attempt-1",
        contract_hash=contract_hash,
        release_edition=release_edition,
        lease_owner=lease_owner,
        session_id=session_id,
        policy_snapshot_hash=policy_snapshot_hash,
        now=now,
    )
    await store.save_contract(queued, expected_revision=None)
    claimed = reduce_runtime_run(
        queued,
        RuntimeRunReducerAction(
            type="claim",
            lease_owner=lease_owner,
            now=now,
        ),
    ).run
    await store.save_contract(
        claimed,
        expected_revision=queued.revision,
        expected_attempt_id=queued.attempt_id,
    )
    started = reduce_runtime_run(
        claimed,
        RuntimeRunReducerAction(
            type="start",
            expected_attempt_id=claimed.attempt_id,
            expected_revision=claimed.revision,
            lease_owner=lease_owner,
            now=now,
        ),
    ).run
    return await store.save_contract(
        started,
        expected_revision=claimed.revision,
        expected_attempt_id=claimed.attempt_id,
        lease_owner=lease_owner,
    )


def _read_json_mapping(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _local_manifest_path(raw: object, *, manifest_dir: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if "://" in text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = manifest_dir / path
    return path


_PUBLISH_RECEIPT_KEYS = {
    "appstoreconnectupload",
    "githubrelease",
    "githubreleaseupload",
    "publish",
    "published",
    "publishreceipt",
    "releasepublished",
    "testflightupload",
    "upload",
    "uploaded",
    "uploadreceipt",
}


def _find_publish_receipts(value: object, *, path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            normalized = re.sub(r"[^a-z]", "", key_text.lower())
            child_path = f"{path}.{key_text}"
            if normalized in _PUBLISH_RECEIPT_KEYS and child not in (
                None,
                False,
                "",
                [],
                {},
            ):
                issues.append(f"no-publish:{child_path} contains a publish/upload receipt")
            issues.extend(_find_publish_receipts(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            issues.extend(_find_publish_receipts(child, path=f"{path}[{index}]"))
    return issues


def _validate_file_hash(
    path: Path,
    *,
    expected_sha256: object,
    label: str,
) -> list[str]:
    if not path.is_file():
        return [f"artifact:{label} missing file at {path}"]
    if isinstance(expected_sha256, str) and expected_sha256:
        actual = _sha256_file(path)
        if actual != expected_sha256:
            return [f"artifact:{label} sha256 mismatch"]
    return []


def _validate_supply_chain_manifest(
    manifest: Mapping[str, Any],
    *,
    artifact_manifest_dir: Path,
    distribution: ReleaseTrialDistribution,
    release_version: str,
    release_arch: str,
    source_sha: str,
) -> list[str]:
    issues: list[str] = []
    supply_chain = manifest.get("supplyChainManifest")
    if not isinstance(supply_chain, Mapping):
        return [f"supply-chain:{distribution} missing supplyChainManifest"]

    if supply_chain.get("schema") != "com.lantai.release-supply-chain-manifest.v1":
        issues.append(f"supply-chain:{distribution} unexpected schema")
    release = supply_chain.get("release")
    if not isinstance(release, Mapping):
        issues.append(f"supply-chain:{distribution} missing release metadata")
    else:
        if str(release.get("version") or "") != release_version:
            issues.append(f"supply-chain:{distribution} version mismatch")
        if str(release.get("arch") or "") != release_arch:
            issues.append(f"supply-chain:{distribution} arch mismatch")

    source = supply_chain.get("source")
    if not isinstance(source, Mapping) or str(source.get("sha") or "") != source_sha:
        issues.append(f"supply-chain:{distribution} source sha mismatch")

    binaries = supply_chain.get("binarySources")
    if not isinstance(binaries, list) or not binaries:
        issues.append(f"supply-chain:{distribution} missing binarySources")

    sbom = supply_chain.get("sbom")
    if not isinstance(sbom, Mapping):
        issues.append(f"supply-chain:{distribution} missing SBOM reference")
    else:
        sbom_path = _local_manifest_path(sbom.get("path"), manifest_dir=artifact_manifest_dir)
        if sbom_path is None:
            issues.append(f"supply-chain:{distribution} SBOM path is not local")
        else:
            issues.extend(
                _validate_file_hash(
                    sbom_path,
                    expected_sha256=sbom.get("sha256"),
                    label=f"{distribution}:sbom",
                )
            )
    return issues


def validate_release_verification_manifest(
    manifest_path: Path | str,
    *,
    distribution: ReleaseTrialDistribution,
) -> ReleaseVerificationEvidence:
    """Validate one local release manifest for the no-publish LOOP trial."""

    path = Path(manifest_path).expanduser().resolve()
    issues: list[str] = []
    try:
        manifest = _read_json_mapping(path)
    except Exception as exc:
        return ReleaseVerificationEvidence(
            distribution=distribution,
            artifact_manifest_path=str(path),
            release_version="",
            release_arch="",
            signing_mode="",
            artifact_names=(),
            evidence_hash=_sha256_payload({"path": str(path), "error": str(exc)}),
            issues=(f"manifest:{distribution} cannot be read: {exc}",),
        )

    if manifest.get("schema") != "com.lantai.release-artifact-manifest.v1":
        issues.append(f"manifest:{distribution} unexpected schema")

    release = manifest.get("release")
    if not isinstance(release, Mapping):
        release = {}
        issues.append(f"manifest:{distribution} missing release metadata")
    release_version = str(release.get("version") or "").strip()
    release_arch = str(release.get("arch") or "").strip()
    if not release_version:
        issues.append(f"manifest:{distribution} missing release version")
    if release_arch not in {"arm64", "x64", "universal"}:
        issues.append(f"manifest:{distribution} unsupported release arch")

    source = manifest.get("source")
    source_sha = ""
    if not isinstance(source, Mapping):
        issues.append(f"manifest:{distribution} missing source metadata")
    else:
        source_sha = str(source.get("sha") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            issues.append(f"manifest:{distribution} invalid source sha")

    signing = manifest.get("signing")
    signing_mode = ""
    if not isinstance(signing, Mapping):
        issues.append(f"signing:{distribution} missing signing metadata")
    else:
        signing_mode = str(signing.get("mode") or "").strip()
        identity_class = str(signing.get("identityClass") or "").strip()
        expected_mode = "developer-id" if distribution == "official" else "mas"
        if signing_mode != expected_mode or identity_class != expected_mode:
            issues.append(
                f"signing:{distribution} expected {expected_mode}, "
                f"got mode={signing_mode!r} identityClass={identity_class!r}",
            )

    artifacts = manifest.get("artifacts")
    artifact_names: list[str] = []
    if not isinstance(artifacts, Mapping):
        issues.append(f"artifact:{distribution} missing artifacts object")
        artifacts = {}

    required_artifacts = (
        {"app", "dmg", "zip", "latestMacYml"}
        if distribution == "official"
        else {"app"}
    )
    missing = sorted(required_artifacts - set(str(key) for key in artifacts))
    for name in missing:
        issues.append(f"artifact:{distribution} missing {name}")

    for name, raw_entry in sorted(artifacts.items()):
        if not isinstance(raw_entry, Mapping):
            issues.append(f"artifact:{distribution}:{name} entry is not an object")
            continue
        artifact_name = str(raw_entry.get("name") or name)
        artifact_names.append(artifact_name)
        artifact_path = _local_manifest_path(raw_entry.get("path"), manifest_dir=path.parent)
        if artifact_path is None:
            issues.append(f"artifact:{distribution}:{name} path is not local")
            continue
        if name == "app":
            if not artifact_path.is_dir():
                issues.append(f"artifact:{distribution}:app missing bundle at {artifact_path}")
            elif not (artifact_path / "Contents" / "Info.plist").is_file():
                issues.append(f"artifact:{distribution}:app missing Info.plist")
        else:
            issues.extend(
                _validate_file_hash(
                    artifact_path,
                    expected_sha256=raw_entry.get("sha256"),
                    label=f"{distribution}:{name}",
                )
            )

    if source_sha and release_version and release_arch:
        issues.extend(
            _validate_supply_chain_manifest(
                manifest,
                artifact_manifest_dir=path.parent,
                distribution=distribution,
                release_version=release_version,
                release_arch=release_arch,
                source_sha=source_sha,
            )
        )
    issues.extend(_find_publish_receipts(manifest))

    evidence_payload = {
        "distribution": distribution,
        "manifest_path": str(path),
        "release_version": release_version,
        "release_arch": release_arch,
        "signing_mode": signing_mode,
        "artifact_names": sorted(artifact_names),
        "issues": sorted(issues),
        "source_sha": source_sha,
    }
    return ReleaseVerificationEvidence(
        distribution=distribution,
        artifact_manifest_path=str(path),
        release_version=release_version,
        release_arch=release_arch,
        signing_mode=signing_mode,
        artifact_names=tuple(sorted(artifact_names)),
        evidence_hash=_sha256_payload(evidence_payload),
        issues=tuple(sorted(issues)),
    )


def validate_release_verification_manifests(
    manifests: Mapping[ReleaseTrialDistribution, Path | str],
) -> tuple[ReleaseVerificationEvidence, ...]:
    expected = {"official", "mas"}
    provided = set(manifests)
    missing = sorted(expected - provided)
    evidence = [
        validate_release_verification_manifest(path, distribution=distribution)
        for distribution, path in sorted(manifests.items())
    ]
    if missing:
        missing_evidence = [
            ReleaseVerificationEvidence(
                distribution=distribution,  # type: ignore[arg-type]
                artifact_manifest_path="",
                release_version="",
                release_arch="",
                signing_mode="",
                artifact_names=(),
                evidence_hash=_sha256_payload({"missing": distribution}),
                issues=(f"manifest:{distribution} missing required distribution manifest",),
            )
            for distribution in missing
        ]
        evidence.extend(missing_evidence)
    return tuple(sorted(evidence, key=lambda item: item.distribution))


_ALLOWED_BROWSER_DRAFT_STATUSES = {
    "local_draft",
    "ready_for_review",
}
_ALLOWED_BROWSER_QUEUE_STATUSES = {
    "queued",
    "needs_confirmation",
}
_PUBLISHING_DRAFT_TERMINAL_STATUSES = {
    "write_requested",
    "saved_to_platform",
}
_PUBLISHING_QUEUE_TERMINAL_STATUSES = {
    "ready_to_write",
    "write_requested",
    "saved",
    "scheduled",
}


def validate_browser_content_draft_payload(
    payload: Mapping[str, Any],
) -> BrowserContentDraftEvidence:
    """Validate Browser content draft/preview evidence for the human-gate trial."""

    issues: list[str] = []
    draft = payload.get("draft")
    if not isinstance(draft, Mapping):
        draft = {}
        issues.append("browser-content:draft missing")
    draft_id = str(draft.get("id") or "").strip()
    title = str(draft.get("title") or "").strip()
    body = str(draft.get("body") or "").strip()
    draft_status = str(draft.get("status") or "").strip()
    preview = payload.get("preview")
    preview_url = ""
    if isinstance(preview, Mapping):
        preview_url = str(preview.get("url") or preview.get("path") or "").strip()
    if not preview_url:
        preview_url = str(draft.get("preview_url") or "").strip()

    if not draft_id:
        issues.append("browser-content:draft id missing")
    if not title:
        issues.append("browser-content:draft title missing")
    if not body:
        issues.append("browser-content:draft body missing")
    if draft_status not in _ALLOWED_BROWSER_DRAFT_STATUSES:
        issues.append(f"browser-content:draft status is not preview-safe: {draft_status!r}")
    if not preview_url:
        issues.append("browser-content:preview evidence missing")
    if str(draft.get("platform_url") or "").strip():
        issues.append("browser-content:draft already has a platform URL")
    if draft.get("published_at"):
        issues.append("browser-content:draft already has published_at")
    if draft_status in _PUBLISHING_DRAFT_TERMINAL_STATUSES:
        issues.append(f"browser-content:draft has platform-write status: {draft_status}")

    queue = payload.get("queue_item")
    if not isinstance(queue, Mapping):
        queue = {}
        issues.append("browser-content:queue item missing")
    queue_status = str(queue.get("status") or "").strip()
    requires_confirmation = bool(queue.get("requires_confirmation"))
    if queue_status not in _ALLOWED_BROWSER_QUEUE_STATUSES:
        issues.append(f"browser-content:queue status is not human-gated: {queue_status!r}")
    if queue_status in _PUBLISHING_QUEUE_TERMINAL_STATUSES:
        issues.append(f"browser-content:queue has platform-write status: {queue_status}")
    if not requires_confirmation:
        issues.append("browser-content:queue does not require confirmation")

    publish_action = payload.get("publish_action")
    if isinstance(publish_action, Mapping):
        publish_requires_gate = bool(
            publish_action.get("requires_human_gate")
            or publish_action.get("requires_confirmation")
            or publish_action.get("requires_manual_takeover")
        )
        if not publish_requires_gate:
            issues.append("browser-content:publish action is not human-gated")
        if publish_action.get("executed") or publish_action.get("ok"):
            issues.append("browser-content:publish action already executed")
    else:
        issues.append("browser-content:publish action metadata missing")

    issues.extend(_find_publish_receipts(payload))
    evidence_payload = {
        "draft_id": draft_id,
        "title": title,
        "draft_status": draft_status,
        "preview_url": preview_url,
        "queue_status": queue_status,
        "requires_confirmation": requires_confirmation,
        "issues": sorted(issues),
    }
    return BrowserContentDraftEvidence(
        draft_id=draft_id,
        title=title,
        draft_status=draft_status,
        preview_url=preview_url,
        queue_status=queue_status,
        requires_confirmation=requires_confirmation,
        evidence_hash=_sha256_payload(evidence_payload),
        issues=tuple(sorted(issues)),
    )


def validate_browser_content_draft_file(
    path: Path | str,
) -> BrowserContentDraftEvidence:
    payload = _read_json_mapping(Path(path).expanduser().resolve())
    return validate_browser_content_draft_payload(payload)


async def run_loop_trial(
    workspace_dir: Path | str,
    *,
    run_id: str = "loop-trial",
    max_iterations: int = 10,
    release_edition: Literal["official", "mas"] = "official",
) -> LoopTrialResult:
    """Run or resume the deterministic long-running Loop trial."""

    workspace = Path(workspace_dir).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    store = RuntimeRunStore(workspace)
    contract = loop_contract_with_hash(_trial_contract())
    now = _now()
    run = await _load_or_start_run(
        store,
        workspace,
        run_id=run_id,
        contract_hash=contract.contract_hash,
        release_edition=release_edition,
        now=now,
    )
    await store.save_loop_contract(
        run.run_id,
        attempt_id=run.attempt_id,
        contract=contract,
        saved_at=now,
    )

    progress = await store.load_loop_progress_window(
        run.run_id,
        expected_attempt_id=run.attempt_id,
    )
    iterations_executed = 0
    executor = LoopEvaluationExecutor(
        gate_runner=_PassingGateRunner(),
        rubric_scorer=_TrialRubricScorer(),
        evaluator="loop-trial",
        evaluator_version="1",
    )

    while run.status not in {"succeeded", "failed", "cancelled", "stalled"}:
        if iterations_executed >= max_iterations:
            break
        iterations_executed += 1
        iteration_index = (progress.consecutive_no_progress_iterations if progress else 0) + 1
        progress = advance_loop_progress_window(
            progress,
            iteration_failed=False,
            made_progress=False,
        )
        await store.save_loop_progress_window(
            run.run_id,
            attempt_id=run.attempt_id,
            progress=progress,
            updated_at=_now(),
        )
        report = executor.evaluate(
            contract,
            allowed_evidence_hashes=[f"sha256:loop-trial-{iteration_index}"],
            report_id=f"{run.run_id}-eval-{iteration_index}",
        )
        await store.save_loop_evaluation_report(
            run.run_id,
            attempt_id=run.attempt_id,
            report=report,
            saved_at=_now(),
        )
        heartbeat = reduce_runtime_run(
            run,
            RuntimeRunReducerAction(
                type="heartbeat",
                expected_attempt_id=run.attempt_id,
                expected_revision=run.revision,
                lease_owner=run.lease_owner,
                current_step=RuntimeStepRef(
                    step_id=f"loop-trial-step-{iteration_index}",
                    index=iteration_index - 1,
                    kind="evaluation",
                    title="Loop trial iteration",
                ),
                now=_now(),
            ),
        ).run
        run = await store.save_contract(
            heartbeat,
            expected_revision=run.revision,
            expected_attempt_id=run.attempt_id,
            lease_owner=run.lease_owner,
        )
        assessment = await assess_loop_runtime_run_stop_conditions_from_store(
            store,
            contract,
            run,
            usage=LoopBudgetUsage(steps=iteration_index),
            now=_now(),
        )
        saved = await save_runtime_run_loop_stop_assessment(
            store,
            run,
            assessment,
            expected_attempt_id=run.attempt_id,
            expected_revision=run.revision,
            lease_owner=run.lease_owner,
            now=_now(),
        )
        if saved is not None:
            run = saved

    progress = progress or LoopProgressWindow()
    return LoopTrialResult(
        run_id=run.run_id,
        attempt_id=run.attempt_id,
        status=run.status,
        revision=run.revision,
        iterations_executed=iterations_executed,
        progress=progress,
        error_code=run.error.code if run.error else None,
    )


async def run_release_verification_loop_trial(
    workspace_dir: Path | str,
    *,
    manifests: Mapping[ReleaseTrialDistribution, Path | str],
    run_id: str = "loop-release-verification",
    max_iterations: int = 1,
    release_edition: Literal["official", "mas"] = "official",
) -> LoopTrialResult:
    """Run a no-publish dual-distribution release verification Loop trial."""

    workspace = Path(workspace_dir).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    store = RuntimeRunStore(workspace)
    evidence = validate_release_verification_manifests(manifests)
    evidence_hash = _sha256_payload(
        [item.__dict__ for item in evidence]
    )
    issues = tuple(
        issue
        for item in evidence
        for issue in item.issues
    )
    contract = loop_contract_with_hash(_release_verification_contract(evidence))
    now = _now()
    run = await _load_or_start_run(
        store,
        workspace,
        run_id=run_id,
        contract_hash=contract.contract_hash,
        release_edition=release_edition,
        lease_owner="loop-release-verification-worker",
        session_id="console:loop-release-verification",
        policy_snapshot_hash="sha256:loop-release-verification-policy",
        now=now,
    )
    if run.contract_hash != contract.contract_hash:
        raise ValueError(
            "release verification run_id already belongs to a different "
            "contract hash; use a new run_id for changed artifact evidence",
        )
    await store.save_loop_contract(
        run.run_id,
        attempt_id=run.attempt_id,
        contract=contract,
        saved_at=now,
    )

    progress = await store.load_loop_progress_window(
        run.run_id,
        expected_attempt_id=run.attempt_id,
    )
    iterations_executed = 0
    executor = LoopEvaluationExecutor(
        gate_runner=_ReleaseVerificationGateRunner(
            issues=issues,
            evidence_hash=evidence_hash,
        ),
        rubric_scorer=_ReleaseVerificationRubricScorer(
            issues=issues,
            evidence_hash=evidence_hash,
        ),
        evaluator="loop-release-verification",
        evaluator_version="1",
    )

    while run.status not in {"succeeded", "failed", "cancelled", "stalled"}:
        if iterations_executed >= max_iterations:
            break
        iterations_executed += 1
        progress = advance_loop_progress_window(
            progress,
            iteration_failed=bool(issues),
            made_progress=not issues,
            failure_fingerprint=evidence_hash if issues else None,
        )
        await store.save_loop_progress_window(
            run.run_id,
            attempt_id=run.attempt_id,
            progress=progress,
            updated_at=_now(),
        )
        report = executor.evaluate(
            contract,
            allowed_evidence_hashes=[item.evidence_hash for item in evidence],
            report_id=f"{run.run_id}-eval-release-verification-{iterations_executed}",
        )
        await store.save_loop_evaluation_report(
            run.run_id,
            attempt_id=run.attempt_id,
            report=report,
            saved_at=_now(),
        )
        heartbeat = reduce_runtime_run(
            run,
            RuntimeRunReducerAction(
                type="heartbeat",
                expected_attempt_id=run.attempt_id,
                expected_revision=run.revision,
                lease_owner=run.lease_owner,
                current_step=RuntimeStepRef(
                    step_id=f"release-verification-step-{iterations_executed}",
                    index=iterations_executed - 1,
                    kind="evaluation",
                    title="Release verification dry-run",
                ),
                now=_now(),
            ),
        ).run
        run = await store.save_contract(
            heartbeat,
            expected_revision=run.revision,
            expected_attempt_id=run.attempt_id,
            lease_owner=run.lease_owner,
        )
        assessment = await assess_loop_runtime_run_stop_conditions_from_store(
            store,
            contract,
            run,
            usage=LoopBudgetUsage(steps=iterations_executed),
            now=_now(),
        )
        saved = await save_runtime_run_loop_stop_assessment(
            store,
            run,
            assessment,
            expected_attempt_id=run.attempt_id,
            expected_revision=run.revision,
            lease_owner=run.lease_owner,
            now=_now(),
        )
        if saved is not None:
            run = saved

    progress = progress or LoopProgressWindow()
    return LoopTrialResult(
        run_id=run.run_id,
        attempt_id=run.attempt_id,
        status=run.status,
        revision=run.revision,
        iterations_executed=iterations_executed,
        progress=progress,
        error_code=run.error.code if run.error else None,
        evidence_hash=evidence_hash,
        issues=issues,
    )


async def run_browser_content_loop_trial(
    workspace_dir: Path | str,
    *,
    payload: Mapping[str, Any] | None = None,
    payload_path: Path | str | None = None,
    run_id: str = "loop-browser-content-draft-preview",
    human_gate_decision: BrowserContentHumanGateDecision = "pending",
    max_iterations: int = 1,
    release_edition: Literal["official", "mas"] = "official",
) -> LoopTrialResult:
    """Run a Browser content draft/preview trial with an explicit human gate."""

    if payload is None:
        if payload_path is None:
            raise ValueError("browser content trial requires payload or payload_path")
        raw_payload = _read_json_mapping(Path(payload_path).expanduser().resolve())
    else:
        raw_payload = payload

    workspace = Path(workspace_dir).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    store = RuntimeRunStore(workspace)
    evidence = validate_browser_content_draft_payload(raw_payload)
    evidence_hash = evidence.evidence_hash
    issues = evidence.issues
    contract = loop_contract_with_hash(_browser_content_contract(evidence))
    now = _now()
    run = await _load_or_start_run(
        store,
        workspace,
        run_id=run_id,
        contract_hash=contract.contract_hash,
        release_edition=release_edition,
        lease_owner="loop-browser-content-worker",
        session_id="console:loop-browser-content",
        policy_snapshot_hash="sha256:loop-browser-content-policy",
        now=now,
    )
    if run.contract_hash != contract.contract_hash:
        raise ValueError(
            "browser content run_id already belongs to a different contract "
            "hash; use a new run_id for changed draft/preview evidence",
        )
    await store.save_loop_contract(
        run.run_id,
        attempt_id=run.attempt_id,
        contract=contract,
        saved_at=now,
    )

    progress = await store.load_loop_progress_window(
        run.run_id,
        expected_attempt_id=run.attempt_id,
    )
    if run.status == "waiting_approval" and human_gate_decision == "pending":
        return LoopTrialResult(
            run_id=run.run_id,
            attempt_id=run.attempt_id,
            status=run.status,
            revision=run.revision,
            iterations_executed=0,
            progress=progress or LoopProgressWindow(),
            error_code=run.error.code if run.error else None,
            evidence_hash=evidence_hash,
            issues=issues,
        )

    if run.status == "waiting_approval" and human_gate_decision == "approved":
        resumed = reduce_runtime_run(
            run,
            RuntimeRunReducerAction(
                type="resume",
                expected_attempt_id=run.attempt_id,
                expected_revision=run.revision,
                lease_owner=run.lease_owner,
                now=_now(),
            ),
        ).run
        run = await store.save_contract(
            resumed,
            expected_revision=run.revision,
            expected_attempt_id=run.attempt_id,
            lease_owner=run.lease_owner,
        )

    iterations_executed = 0
    executor = LoopEvaluationExecutor(
        gate_runner=_BrowserContentGateRunner(
            issues=issues,
            evidence_hash=evidence_hash,
        ),
        rubric_scorer=_BrowserContentRubricScorer(
            issues=issues,
            evidence_hash=evidence_hash,
        ),
        evaluator="loop-browser-content-human-gate",
        evaluator_version="1",
    )

    while run.status not in {"succeeded", "failed", "cancelled", "stalled"}:
        if iterations_executed >= max_iterations:
            break
        iterations_executed += 1
        progress = advance_loop_progress_window(
            progress,
            iteration_failed=bool(issues),
            made_progress=not issues,
            failure_fingerprint=evidence_hash if issues else None,
        )
        await store.save_loop_progress_window(
            run.run_id,
            attempt_id=run.attempt_id,
            progress=progress,
            updated_at=_now(),
        )
        report = executor.evaluate(
            contract,
            allowed_evidence_hashes=[evidence_hash],
            human_gate_decision=human_gate_decision,
            report_id=f"{run.run_id}-eval-browser-content-{iterations_executed}-{human_gate_decision}",
        )
        await store.save_loop_evaluation_report(
            run.run_id,
            attempt_id=run.attempt_id,
            report=report,
            saved_at=_now(),
        )
        heartbeat = reduce_runtime_run(
            run,
            RuntimeRunReducerAction(
                type="heartbeat",
                expected_attempt_id=run.attempt_id,
                expected_revision=run.revision,
                lease_owner=run.lease_owner,
                current_step=RuntimeStepRef(
                    step_id=f"browser-content-step-{iterations_executed}",
                    index=iterations_executed - 1,
                    kind="approval" if human_gate_decision == "pending" else "evaluation",
                    title="Browser content draft/preview human gate",
                ),
                now=_now(),
            ),
        ).run
        run = await store.save_contract(
            heartbeat,
            expected_revision=run.revision,
            expected_attempt_id=run.attempt_id,
            lease_owner=run.lease_owner,
        )

        if issues:
            assessment = await assess_loop_runtime_run_stop_conditions_from_store(
                store,
                contract,
                run,
                usage=LoopBudgetUsage(steps=iterations_executed),
                now=_now(),
            )
            saved = await save_runtime_run_loop_stop_assessment(
                store,
                run,
                assessment,
                expected_attempt_id=run.attempt_id,
                expected_revision=run.revision,
                lease_owner=run.lease_owner,
                now=_now(),
            )
            if saved is not None:
                run = saved
            break

        if human_gate_decision == "pending":
            waiting = reduce_runtime_run(
                run,
                RuntimeRunReducerAction(
                    type="wait_approval",
                    expected_attempt_id=run.attempt_id,
                    expected_revision=run.revision,
                    lease_owner=run.lease_owner,
                    now=_now(),
                ),
            ).run
            run = await store.save_contract(
                waiting,
                expected_revision=run.revision,
                expected_attempt_id=run.attempt_id,
                lease_owner=run.lease_owner,
            )
            break

        if human_gate_decision == "rejected":
            cancelled = reduce_runtime_run(
                run,
                RuntimeRunReducerAction(
                    type="settle",
                    status="cancelled",
                    expected_attempt_id=run.attempt_id,
                    expected_revision=run.revision,
                    lease_owner=run.lease_owner,
                    reason=RuntimeRunReason(
                        code="browser_publish_gate_rejected",
                        message="Browser content publish handoff was rejected by the human gate.",
                        category="user",
                        retryable=False,
                    ),
                    now=_now(),
                ),
            ).run
            run = await store.save_contract(
                cancelled,
                expected_revision=run.revision,
                expected_attempt_id=run.attempt_id,
                lease_owner=run.lease_owner,
            )
            break

        assessment = await assess_loop_runtime_run_stop_conditions_from_store(
            store,
            contract,
            run,
            usage=LoopBudgetUsage(steps=iterations_executed),
            now=_now(),
        )
        saved = await save_runtime_run_loop_stop_assessment(
            store,
            run,
            assessment,
            expected_attempt_id=run.attempt_id,
            expected_revision=run.revision,
            lease_owner=run.lease_owner,
            now=_now(),
        )
        if saved is not None:
            run = saved

    progress = progress or LoopProgressWindow()
    return LoopTrialResult(
        run_id=run.run_id,
        attempt_id=run.attempt_id,
        status=run.status,
        revision=run.revision,
        iterations_executed=iterations_executed,
        progress=progress,
        error_code=run.error.code if run.error else None,
        evidence_hash=evidence_hash,
        issues=issues,
    )


async def run_loop_interruption_matrix_trial(
    workspace_dir: Path | str,
    *,
    run_id_prefix: str = "loop-interruption-matrix",
    release_edition: Literal["official", "mas"] = "official",
) -> LoopInterruptionMatrixResult:
    """Run a deterministic source-level interruption matrix for LOOP recovery."""

    workspace = Path(workspace_dir).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    store = RuntimeRunStore(workspace)
    issues: list[str] = []
    details: dict[str, Any] = {}

    transport_run_id = _interruption_matrix_run_id(
        run_id_prefix,
        "transport_disconnect",
    )
    transport = await run_loop_trial(
        workspace,
        run_id=transport_run_id,
        max_iterations=0,
        release_edition=release_edition,
    )
    transport_run = await store.load_contract(transport_run_id)
    if transport_run is None:
        issues.append("interruption-matrix:transport_disconnect run missing")
    else:
        heartbeat = transport_run.heartbeat_at or transport_run.started_at or _now()
        stall_assessment = assess_runtime_run_heartbeat(
            transport_run,
            now=heartbeat + timedelta(seconds=30),
            timeout_seconds=1,
        )
        details["transport_disconnect"] = {
            "initial_status": transport.status,
            "suspected_stalled": stall_assessment.suspected_stalled,
            "assessment_status": stall_assessment.run.status,
        }
        if not stall_assessment.suspected_stalled:
            issues.append(
                "interruption-matrix:transport_disconnect did not flag "
                "a stale heartbeat for external recovery",
            )
        if stall_assessment.run.status != "running":
            issues.append(
                "interruption-matrix:transport_disconnect heartbeat assessment "
                f"changed status to {stall_assessment.run.status}",
            )

    kill_run_id = _interruption_matrix_run_id(
        run_id_prefix,
        "process_kill_recovery",
    )
    kill_first = await run_loop_trial(
        workspace,
        run_id=kill_run_id,
        max_iterations=1,
        release_edition=release_edition,
    )
    kill_second = await run_loop_trial(
        workspace,
        run_id=kill_run_id,
        max_iterations=10,
        release_edition=release_edition,
    )
    details["process_kill_recovery"] = {
        "first_status": kill_first.status,
        "second_status": kill_second.status,
        "first_attempt_id": kill_first.attempt_id,
        "second_attempt_id": kill_second.attempt_id,
    }
    if kill_first.status != "running":
        issues.append(
            "interruption-matrix:process_kill_recovery first process "
            f"expected running, got {kill_first.status}",
        )
    if kill_first.attempt_id != kill_second.attempt_id:
        issues.append(
            "interruption-matrix:process_kill_recovery changed attempt id "
            "while resuming the same attempt",
        )

    pause_run_id = _interruption_matrix_run_id(run_id_prefix, "pause_resume")
    await run_loop_trial(
        workspace,
        run_id=pause_run_id,
        max_iterations=0,
        release_edition=release_edition,
    )
    pause_run = await store.load_contract(pause_run_id)
    if pause_run is None:
        issues.append("interruption-matrix:pause_resume run missing")
    else:
        paused = reduce_runtime_run(
            pause_run,
            RuntimeRunReducerAction(
                type="pause",
                expected_attempt_id=pause_run.attempt_id,
                expected_revision=pause_run.revision,
                lease_owner=pause_run.lease_owner,
                now=_now(),
            ),
        ).run
        paused = await store.save_contract(
            paused,
            expected_revision=pause_run.revision,
            expected_attempt_id=pause_run.attempt_id,
            lease_owner=pause_run.lease_owner,
        )
        resumed = reduce_runtime_run(
            paused,
            RuntimeRunReducerAction(
                type="resume",
                expected_attempt_id=paused.attempt_id,
                expected_revision=paused.revision,
                lease_owner=paused.lease_owner,
                now=_now(),
            ),
        ).run
        resumed = await store.save_contract(
            resumed,
            expected_revision=paused.revision,
            expected_attempt_id=paused.attempt_id,
            lease_owner=paused.lease_owner,
        )
        details["pause_resume"] = {
            "paused_status": paused.status,
            "resumed_status": resumed.status,
            "attempt_id": resumed.attempt_id,
        }
        if paused.status != "paused" or resumed.status != "running":
            issues.append(
                "interruption-matrix:pause_resume did not round-trip "
                f"paused->running, got {paused.status}->{resumed.status}",
            )

    failure_run_id = _interruption_matrix_run_id(run_id_prefix, "failure_retry")
    contract = loop_contract_with_hash(_trial_contract())
    failure_run = await _load_or_start_run(
        store,
        workspace,
        run_id=failure_run_id,
        contract_hash=contract.contract_hash,
        release_edition=release_edition,
        lease_owner="loop-interruption-matrix-worker",
        session_id="console:loop-interruption-matrix",
        policy_snapshot_hash="sha256:loop-interruption-matrix-policy",
        now=_now(),
    )
    await store.save_loop_contract(
        failure_run.run_id,
        attempt_id=failure_run.attempt_id,
        contract=contract,
        saved_at=_now(),
    )
    failed = reduce_runtime_run(
        failure_run,
        RuntimeRunReducerAction(
            type="settle",
            status="failed",
            expected_attempt_id=failure_run.attempt_id,
            expected_revision=failure_run.revision,
            lease_owner=failure_run.lease_owner,
            reason=RuntimeRunReason(
                code="matrix_retryable_failure",
                message="Synthetic retryable failure for interruption matrix.",
                category="runtime",
                retryable=True,
            ),
            now=_now(),
        ),
    ).run
    failed = await store.save_contract(
        failed,
        expected_revision=failure_run.revision,
        expected_attempt_id=failure_run.attempt_id,
        lease_owner=failure_run.lease_owner,
    )
    retry_attempt_id = f"{failure_run_id}:attempt-2"
    retry = start_runtime_run_attempt(
        failed,
        attempt_id=retry_attempt_id,
        attempt_reason="retry",
        lease_owner="loop-interruption-matrix-worker",
        now=_now(),
    )
    retry = await store.save_contract(
        retry,
        expected_revision=failed.revision,
        expected_attempt_id=failed.attempt_id,
        lease_owner=failed.lease_owner,
    )
    claimed_retry = reduce_runtime_run(
        retry,
        RuntimeRunReducerAction(
            type="claim",
            lease_owner="loop-interruption-matrix-worker",
            now=_now(),
        ),
    ).run
    claimed_retry = await store.save_contract(
        claimed_retry,
        expected_revision=retry.revision,
        expected_attempt_id=retry.attempt_id,
        lease_owner=retry.lease_owner,
    )
    started_retry = reduce_runtime_run(
        claimed_retry,
        RuntimeRunReducerAction(
            type="start",
            expected_attempt_id=claimed_retry.attempt_id,
            expected_revision=claimed_retry.revision,
            lease_owner=claimed_retry.lease_owner,
            now=_now(),
        ),
    ).run
    started_retry = await store.save_contract(
        started_retry,
        expected_revision=claimed_retry.revision,
        expected_attempt_id=claimed_retry.attempt_id,
        lease_owner=claimed_retry.lease_owner,
    )
    details["failure_retry"] = {
        "failed_status": failed.status,
        "retry_status": started_retry.status,
        "retry_attempt_id": started_retry.attempt_id,
        "previous_attempt_id": (
            started_retry.previous_attempt_id
            if isinstance(started_retry, RuntimeAttemptContract)
            else None
        ),
    }
    if started_retry.attempt_id != retry_attempt_id:
        issues.append(
            "interruption-matrix:failure_retry did not persist the retry attempt id",
        )
    if not isinstance(started_retry, RuntimeAttemptContract):
        issues.append(
            "interruption-matrix:failure_retry retry attempt lost attempt metadata",
        )
    elif started_retry.previous_attempt_id != failed.attempt_id:
        issues.append(
            "interruption-matrix:failure_retry previous_attempt_id was not preserved",
        )

    budget_run_id = _interruption_matrix_run_id(run_id_prefix, "budget_exhaustion")
    budget_contract = loop_contract_with_hash(_interruption_matrix_budget_contract())
    budget_run = await _load_or_start_run(
        store,
        workspace,
        run_id=budget_run_id,
        contract_hash=budget_contract.contract_hash,
        release_edition=release_edition,
        lease_owner="loop-interruption-matrix-worker",
        session_id="console:loop-interruption-matrix",
        policy_snapshot_hash="sha256:loop-interruption-matrix-policy",
        now=_now(),
    )
    await store.save_loop_contract(
        budget_run.run_id,
        attempt_id=budget_run.attempt_id,
        contract=budget_contract,
        saved_at=_now(),
    )
    budget_assessment = await assess_loop_runtime_run_stop_conditions_from_store(
        store,
        budget_contract,
        budget_run,
        usage=LoopBudgetUsage(steps=1),
        now=_now(),
    )
    budget_saved = await save_runtime_run_loop_stop_assessment(
        store,
        budget_run,
        budget_assessment,
        expected_attempt_id=budget_run.attempt_id,
        expected_revision=budget_run.revision,
        lease_owner=budget_run.lease_owner,
        now=_now(),
    )
    details["budget_exhaustion"] = {
        "assessment_should_stop": budget_assessment.should_stop,
        "reason_codes": [reason.code for reason in budget_assessment.reasons],
        "saved_status": budget_saved.status if budget_saved is not None else None,
    }
    if budget_saved is None:
        issues.append("interruption-matrix:budget_exhaustion did not settle the run")

    user_stop_run_id = _interruption_matrix_run_id(run_id_prefix, "user_stop")
    user_stop_contract = loop_contract_with_hash(_trial_contract())
    user_stop_run = await _load_or_start_run(
        store,
        workspace,
        run_id=user_stop_run_id,
        contract_hash=user_stop_contract.contract_hash,
        release_edition=release_edition,
        lease_owner="loop-interruption-matrix-worker",
        session_id="console:loop-interruption-matrix",
        policy_snapshot_hash="sha256:loop-interruption-matrix-policy",
        now=_now(),
    )
    await store.save_loop_contract(
        user_stop_run.run_id,
        attempt_id=user_stop_run.attempt_id,
        contract=user_stop_contract,
        saved_at=_now(),
    )
    stop_requested = reduce_runtime_run(
        user_stop_run,
        RuntimeRunReducerAction(
            type="request_stop",
            expected_attempt_id=user_stop_run.attempt_id,
            expected_revision=user_stop_run.revision,
            lease_owner=user_stop_run.lease_owner,
            reason=RuntimeRunReason(
                code="matrix_user_requested_stop",
                message="Synthetic user stop request for interruption matrix.",
                category="user",
                retryable=False,
            ),
            now=_now(),
        ),
    ).run
    stop_requested = await store.save_contract(
        stop_requested,
        expected_revision=user_stop_run.revision,
        expected_attempt_id=user_stop_run.attempt_id,
        lease_owner=user_stop_run.lease_owner,
    )
    user_stop_assessment = await assess_loop_runtime_run_stop_conditions_from_store(
        store,
        user_stop_contract,
        stop_requested,
        usage=LoopBudgetUsage(),
        human_stop_requested=True,
        now=_now(),
    )
    user_stop_saved = await save_runtime_run_loop_stop_assessment(
        store,
        stop_requested,
        user_stop_assessment,
        expected_attempt_id=stop_requested.attempt_id,
        expected_revision=stop_requested.revision,
        lease_owner=stop_requested.lease_owner,
        now=_now(),
    )
    details["user_stop"] = {
        "requested_status": stop_requested.status,
        "assessment_should_stop": user_stop_assessment.should_stop,
        "reason_codes": [reason.code for reason in user_stop_assessment.reasons],
        "saved_status": user_stop_saved.status if user_stop_saved is not None else None,
    }
    if user_stop_saved is None:
        issues.append("interruption-matrix:user_stop did not settle the run")

    browser_run_id = _interruption_matrix_run_id(run_id_prefix, "browser_human_gate")
    browser_payload = _interruption_matrix_browser_payload()
    browser_pending = await run_browser_content_loop_trial(
        workspace,
        payload=browser_payload,
        run_id=browser_run_id,
        human_gate_decision="pending",
        release_edition=release_edition,
    )
    browser_approved = await run_browser_content_loop_trial(
        workspace,
        payload=browser_payload,
        run_id=browser_run_id,
        human_gate_decision="approved",
        release_edition=release_edition,
    )
    details["browser_human_gate"] = {
        "pending_status": browser_pending.status,
        "approved_status": browser_approved.status,
        "pending_attempt_id": browser_pending.attempt_id,
        "approved_attempt_id": browser_approved.attempt_id,
    }
    if browser_pending.status != "waiting_approval":
        issues.append(
            "interruption-matrix:browser_human_gate did not wait for approval",
        )
    if browser_pending.attempt_id != browser_approved.attempt_id:
        issues.append(
            "interruption-matrix:browser_human_gate changed attempt id on approval",
        )

    return await _collect_interruption_matrix_state(
        store,
        run_id_prefix=run_id_prefix,
        extra_issues=tuple(issues),
        details=details,
    )
