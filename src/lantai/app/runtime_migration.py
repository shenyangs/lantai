# -*- coding: utf-8 -*-
"""Dry-run Runtime migration inventory reporter.

This module intentionally does not mutate workspace state. It scans legacy app
storage, projects records into canonical Runtime contracts where possible, and
returns a bounded report that later migration jobs can use as their manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lantai.app.capabilities.legacy_adapter import LegacyTaskRunCompatibilityAdapter
from lantai.app.capabilities.models import EvaluationReport, TaskRunsFile
from lantai.app.runtime_legacy_writers import (
    LegacyRuntimeWriterMode,
    get_legacy_runtime_writer_mode,
)
from lantai.app.browser_workbench.models import (
    BrowserAction,
    BrowserTaskEvent,
    BrowserTaskRun,
)
from lantai.app.experience.legacy_adapter import LegacyExperienceEventAdapter
from lantai.app.experience.schemas import ChatExperienceEvent
from lantai.runtime.runs import (
    RuntimeReleaseEdition,
    RuntimeRunContractMigrationError,
    migrate_runtime_run_contract,
)
from lantai.runtime.store import RuntimeRunStore


RUNTIME_MIGRATION_REPORT_SCHEMA_VERSION = 2
RUNTIME_MIGRATION_REPORT_WRITER_VERSION = "lantai-runtime-migration-report/1"

RuntimeMigrationSourceType = Literal[
    "experience_ledger",
    "task_runs",
    "evaluation_reports",
    "mission_state",
    "browser_task_state",
]

RuntimeMigrationWriterStatus = Literal[
    "legacy_write_active",
    "legacy_write_stopped_read_compatibility",
]


class RuntimeMigrationIssue(BaseModel):
    """One problem discovered during dry-run migration inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: RuntimeMigrationSourceType
    path: str
    code: str
    message: str = ""
    record_id: str | None = None


class RuntimeMigrationSourceReport(BaseModel):
    """Dry-run report for one legacy source file or directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: RuntimeMigrationSourceType
    path: str
    exists: bool = True
    bytes: int = 0
    content_sha256: str | None = None
    line_count: int = 0
    record_count: int = 0
    migratable_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    projected_runtime_run_ids: list[str] = Field(default_factory=list)
    projected_runtime_event_ids: list[str] = Field(default_factory=list)
    backup_path: str | None = None
    issues: list[RuntimeMigrationIssue] = Field(default_factory=list)


class RuntimeMigrationTotals(BaseModel):
    """Aggregate dry-run counts across all scanned legacy sources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_count: int = 0
    bytes: int = 0
    record_count: int = 0
    migratable_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    issue_count: int = 0


class RuntimeMigrationWriterInventoryItem(BaseModel):
    """CLOSE-01 inventory row for one legacy Runtime writer surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    writer_id: str
    source_type: RuntimeMigrationSourceType
    legacy_writer: str
    legacy_path_glob: str
    canonical_writer: str
    last_dependent_readers: list[str] = Field(default_factory=list)
    migration_status: RuntimeMigrationWriterStatus
    legacy_write_allowed: bool
    can_stop_writes: bool
    stop_writes_blockers: list[str] = Field(default_factory=list)
    delete_preconditions: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    observed_source_count: int = 0
    observed_record_count: int = 0
    observed_issue_count: int = 0
    observed_paths: list[str] = Field(default_factory=list)


class RuntimeMigrationReport(BaseModel):
    """Side-effect-free Runtime migration inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = RUNTIME_MIGRATION_REPORT_SCHEMA_VERSION
    writer_version: str = RUNTIME_MIGRATION_REPORT_WRITER_VERSION
    dry_run: Literal[True] = True
    side_effect_free: Literal[True] = True
    side_effects_executed: Literal[0] = 0
    workspace_dir: str
    release_edition: RuntimeReleaseEdition = "official"
    legacy_writer_mode: LegacyRuntimeWriterMode = "active"
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    totals: RuntimeMigrationTotals = Field(default_factory=RuntimeMigrationTotals)
    backup_targets: list[str] = Field(default_factory=list)
    sources: list[RuntimeMigrationSourceReport] = Field(default_factory=list)
    writer_inventory: list[RuntimeMigrationWriterInventoryItem] = Field(
        default_factory=list,
    )


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _directory_sha256(paths: list[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item.relative_to(root))):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _safe_fragment(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    text = text.strip(".-")
    return text[:80] or "source"


def _backup_path_for_source(
    workspace_dir: Path,
    *,
    source_type: RuntimeMigrationSourceType,
    source_path: Path,
    content_sha256: str | None,
) -> str:
    checksum = (
        content_sha256.removeprefix("sha256:")
        if content_sha256
        else hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()
    )
    name = f"{source_type}.{_safe_fragment(source_path.name)}.{checksum[:16]}.bak"
    return str(workspace_dir / ".lantai" / "runtime" / "v1" / "migration-backups" / name)


def _issue(
    source_type: RuntimeMigrationSourceType,
    path: Path,
    code: str,
    *,
    message: str = "",
    record_id: str | None = None,
) -> RuntimeMigrationIssue:
    return RuntimeMigrationIssue(
        source_type=source_type,
        path=str(path),
        code=code,
        message=message,
        record_id=record_id,
    )


def _contract_exists(workspace_dir: Path, run_id: str) -> bool:
    return RuntimeRunStore(workspace_dir).contract_path(run_id).exists()


def _experience_ledger_paths(workspace_dir: Path) -> list[Path]:
    root = workspace_dir / ".lantai" / "experience"
    if not root.exists() or not root.is_dir():
        return []
    paths: dict[str, Path] = {}
    active = root / "events.jsonl"
    if active.exists() and active.is_file():
        paths[str(active)] = active
    for candidate in root.glob("events.*-*.jsonl"):
        if candidate.is_file():
            paths[str(candidate)] = candidate
    return sorted(paths.values(), key=lambda item: item.name)


def _scan_experience_ledger(
    workspace_dir: Path,
    path: Path,
    *,
    seen_event_ids: set[str],
    seen_sequences: dict[int, str],
) -> RuntimeMigrationSourceReport:
    source_type: RuntimeMigrationSourceType = "experience_ledger"
    issues: list[RuntimeMigrationIssue] = []
    projected_event_ids: list[str] = []
    line_count = 0
    record_count = 0
    migratable_count = 0
    skipped_count = 0
    conflict_count = 0
    content_sha256 = _file_sha256(path)

    adapter = LegacyExperienceEventAdapter()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line_count += 1
        raw = line.strip()
        if not raw:
            skipped_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "empty_line",
                    message=f"line {line_number} is empty",
                )
            )
            continue
        try:
            payload = json.loads(raw)
            event = ChatExperienceEvent.model_validate(payload)
            adapter.to_runtime_event(event)
        except json.JSONDecodeError as exc:
            skipped_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "invalid_json",
                    message=f"line {line_number}: {exc.msg}",
                )
            )
            continue
        except Exception as exc:  # pragma: no cover - exact pydantic text varies
            skipped_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "invalid_event",
                    message=f"line {line_number}: {exc}",
                )
            )
            continue

        record_count += 1
        migratable_count += 1
        projected_event_ids.append(event.id)

        previous_path = seen_sequences.get(event.seq)
        if previous_path is not None and previous_path != str(path):
            conflict_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "duplicate_event_sequence",
                    message=f"seq {event.seq} also appears in {previous_path}",
                    record_id=event.id,
                )
            )
        seen_sequences.setdefault(event.seq, str(path))

        if event.id in seen_event_ids:
            conflict_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "duplicate_event_id",
                    message=f"event id {event.id} appears more than once",
                    record_id=event.id,
                )
            )
        seen_event_ids.add(event.id)

    return RuntimeMigrationSourceReport(
        source_type=source_type,
        path=str(path),
        bytes=path.stat().st_size,
        content_sha256=content_sha256,
        line_count=line_count,
        record_count=record_count,
        migratable_count=migratable_count,
        skipped_count=skipped_count,
        conflict_count=conflict_count,
        projected_runtime_event_ids=projected_event_ids,
        backup_path=_backup_path_for_source(
            workspace_dir,
            source_type=source_type,
            source_path=path,
            content_sha256=content_sha256,
        ),
        issues=issues,
    )


def _scan_task_runs(
    workspace_dir: Path,
    *,
    release_edition: RuntimeReleaseEdition,
) -> tuple[RuntimeMigrationSourceReport | None, set[str]]:
    path = workspace_dir / "capabilities" / "task-runs.json"
    source_type: RuntimeMigrationSourceType = "task_runs"
    known_task_run_ids: set[str] = set()
    if not path.exists():
        return None, known_task_run_ids

    content_sha256 = _file_sha256(path)
    issues: list[RuntimeMigrationIssue] = []
    projected_run_ids: list[str] = []
    record_count = 0
    migratable_count = 0
    skipped_count = 0
    conflict_count = 0

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        data = TaskRunsFile.model_validate(payload)
    except Exception as exc:
        skipped_count += 1
        issues.append(
            _issue(
                source_type,
                path,
                "invalid_task_runs_file",
                message=str(exc),
            )
        )
        data = TaskRunsFile()

    adapter = LegacyTaskRunCompatibilityAdapter(release_edition=release_edition)
    for run in data.runs:
        record_count += 1
        known_task_run_ids.add(run.id)
        try:
            contract = adapter.to_runtime_run_contract(run)
        except Exception as exc:  # pragma: no cover - validation text varies
            skipped_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "task_run_projection_failed",
                    message=str(exc),
                    record_id=run.id,
                )
            )
            continue
        migratable_count += 1
        projected_run_ids.append(contract.run_id)
        if _contract_exists(workspace_dir, contract.run_id):
            conflict_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "canonical_contract_exists",
                    message="RuntimeRunStore already has a contract for this run id",
                    record_id=contract.run_id,
                )
            )

    return (
        RuntimeMigrationSourceReport(
            source_type=source_type,
            path=str(path),
            bytes=path.stat().st_size,
            content_sha256=content_sha256,
            record_count=record_count,
            migratable_count=migratable_count,
            skipped_count=skipped_count,
            conflict_count=conflict_count,
            projected_runtime_run_ids=projected_run_ids,
            backup_path=_backup_path_for_source(
                workspace_dir,
                source_type=source_type,
                source_path=path,
                content_sha256=content_sha256,
            ),
            issues=issues,
        ),
        known_task_run_ids,
    )


def _scan_evaluation_reports(
    workspace_dir: Path,
    *,
    known_task_run_ids: set[str],
) -> RuntimeMigrationSourceReport | None:
    root = workspace_dir / "capabilities" / "evaluations"
    source_type: RuntimeMigrationSourceType = "evaluation_reports"
    if not root.exists() or not root.is_dir():
        return None

    paths = sorted(
        {
            path
            for pattern in ("*.json", "*/*.json")
            for path in root.glob(pattern)
            if path.is_file()
        },
        key=lambda item: str(item.relative_to(root)),
    )
    content_sha256 = _directory_sha256(paths, root=root) if paths else None
    issues: list[RuntimeMigrationIssue] = []
    record_count = 0
    migratable_count = 0
    skipped_count = 0
    conflict_count = 0
    total_bytes = sum(path.stat().st_size for path in paths)

    for path in paths:
        try:
            report = EvaluationReport.model_validate(
                json.loads(path.read_text(encoding="utf-8")),
            )
        except Exception as exc:
            skipped_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "invalid_evaluation_report",
                    message=str(exc),
                )
            )
            continue

        record_count += 1
        if report.task_run_id not in known_task_run_ids:
            conflict_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "orphan_evaluation",
                    message="EvaluationReport points to a TaskRun absent from task-runs.json",
                    record_id=report.task_run_id,
                )
            )
            continue
        migratable_count += 1

    return RuntimeMigrationSourceReport(
        source_type=source_type,
        path=str(root),
        bytes=total_bytes,
        content_sha256=content_sha256,
        record_count=record_count,
        migratable_count=migratable_count,
        skipped_count=skipped_count,
        conflict_count=conflict_count,
        backup_path=_backup_path_for_source(
            workspace_dir,
            source_type=source_type,
            source_path=root,
            content_sha256=content_sha256,
        ),
        issues=issues,
    )


def _mission_status_from_prd(prd: dict[str, object]) -> str:
    stories = prd.get("userStories")
    if isinstance(stories, list) and stories:
        if all(isinstance(story, dict) and story.get("passes") for story in stories):
            return "completed"
        return "running"
    return "running"


def _scan_mission_dir(
    workspace_dir: Path,
    mission_dir: Path,
    *,
    release_edition: RuntimeReleaseEdition,
) -> RuntimeMigrationSourceReport:
    source_type: RuntimeMigrationSourceType = "mission_state"
    relevant_paths = [
        path
        for path in (
            mission_dir / "loop_config.json",
            mission_dir / "prd.json",
            mission_dir / "progress.txt",
            mission_dir / "task.md",
        )
        if path.exists() and path.is_file()
    ]
    content_sha256 = (
        _directory_sha256(relevant_paths, root=mission_dir)
        if relevant_paths
        else None
    )
    issues: list[RuntimeMigrationIssue] = []
    record_count = 0
    migratable_count = 0
    skipped_count = 0
    conflict_count = 0
    projected_run_ids: list[str] = []

    try:
        config_path = mission_dir / "loop_config.json"
        prd_path = mission_dir / "prd.json"
        config = (
            json.loads(config_path.read_text(encoding="utf-8"))
            if config_path.exists()
            else {}
        )
        prd = (
            json.loads(prd_path.read_text(encoding="utf-8"))
            if prd_path.exists()
            else {}
        )
    except Exception as exc:
        skipped_count += 1
        issues.append(
            _issue(
                source_type,
                mission_dir,
                "invalid_mission_state",
                message=str(exc),
                record_id=mission_dir.name,
            )
        )
        config = {}
        prd = {}

    if skipped_count == 0:
        record_count = 1
        raw_contract = {
            "schema_version": 0,
            "id": mission_dir.name,
            "source": "mission",
            "agent_id": str(
                config.get("agent_id")
                or config.get("active_agent")
                or config.get("agent")
                or "default",
            ),
            "session_id": config.get("session_id"),
            "workspace_id": str(workspace_dir),
            "channel": "mission",
            "status": _mission_status_from_prd(prd),
            "release_edition": release_edition,
        }
        try:
            contract = migrate_runtime_run_contract(raw_contract)
            migratable_count += 1
            projected_run_ids.append(contract.run_id)
            if _contract_exists(workspace_dir, contract.run_id):
                conflict_count += 1
                issues.append(
                    _issue(
                        source_type,
                        mission_dir,
                        "canonical_contract_exists",
                        message="RuntimeRunStore already has a contract for this mission",
                        record_id=contract.run_id,
                    )
                )
        except RuntimeRunContractMigrationError as exc:
            skipped_count += 1
            issues.append(
                _issue(
                    source_type,
                    mission_dir,
                    "mission_projection_failed",
                    message=str(exc),
                    record_id=mission_dir.name,
                )
            )

    return RuntimeMigrationSourceReport(
        source_type=source_type,
        path=str(mission_dir),
        bytes=sum(path.stat().st_size for path in relevant_paths),
        content_sha256=content_sha256,
        record_count=record_count,
        migratable_count=migratable_count,
        skipped_count=skipped_count,
        conflict_count=conflict_count,
        projected_runtime_run_ids=projected_run_ids,
        backup_path=_backup_path_for_source(
            workspace_dir,
            source_type=source_type,
            source_path=mission_dir,
            content_sha256=content_sha256,
        ),
        issues=issues,
    )


def _scan_mission_dirs(
    workspace_dir: Path,
    *,
    release_edition: RuntimeReleaseEdition,
) -> list[RuntimeMigrationSourceReport]:
    root = workspace_dir / "missions"
    if not root.exists() or not root.is_dir():
        return []
    return [
        _scan_mission_dir(
            workspace_dir,
            mission_dir,
            release_edition=release_edition,
        )
        for mission_dir in sorted(root.iterdir(), key=lambda item: item.name)
        if mission_dir.is_dir() and mission_dir.name.startswith("mission-")
    ]


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def _scan_browser_task_state(workspace_dir: Path) -> RuntimeMigrationSourceReport | None:
    root = workspace_dir / "browser_workbench" / "tasks"
    source_type: RuntimeMigrationSourceType = "browser_task_state"
    if not root.exists() or not root.is_dir():
        return None

    paths = sorted((path for path in root.glob("*.json") if path.is_file()), key=str)
    content_sha256 = _directory_sha256(paths, root=root) if paths else None
    issues: list[RuntimeMigrationIssue] = []
    record_count = 0
    migratable_count = 0
    skipped_count = 0
    conflict_count = 0
    projected_run_ids: list[str] = []
    total_bytes = sum(path.stat().st_size for path in paths)

    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("browser task file must contain a JSON object")
            run = BrowserTaskRun.model_validate(payload.get("run") or {})
            events = payload.get("events") or []
            actions = payload.get("actions") or []
            if not isinstance(events, list):
                raise ValueError("browser task events must be a list")
            if not isinstance(actions, list):
                raise ValueError("browser task actions must be a list")
            for item in events:
                if isinstance(item, dict):
                    BrowserTaskEvent.model_validate(item)
            for item in actions:
                if isinstance(item, dict):
                    BrowserAction.model_validate(item)
        except Exception as exc:
            skipped_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "invalid_browser_task_state",
                    message=str(exc),
                )
            )
            continue

        record_count += 1
        raw_workspace_dir = str(run.metadata.get("workspace_dir") or "").strip()
        if not raw_workspace_dir:
            skipped_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "missing_browser_task_workspace_metadata",
                    message="BrowserTaskRun metadata.workspace_dir is required for canonical RuntimeRunStore projection",
                    record_id=run.run_id,
                )
            )
            continue
        if not _same_path(Path(raw_workspace_dir), workspace_dir):
            skipped_count += 1
            conflict_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "browser_task_workspace_mismatch",
                    message=f"run belongs to workspace {raw_workspace_dir}",
                    record_id=run.run_id,
                )
            )
            continue

        migratable_count += 1
        projected_run_ids.append(run.run_id)
        if _contract_exists(workspace_dir, run.run_id):
            conflict_count += 1
            issues.append(
                _issue(
                    source_type,
                    path,
                    "canonical_contract_exists",
                    message="RuntimeRunStore already has a contract for this browser task",
                    record_id=run.run_id,
                )
            )

    return RuntimeMigrationSourceReport(
        source_type=source_type,
        path=str(root),
        bytes=total_bytes,
        content_sha256=content_sha256,
        record_count=record_count,
        migratable_count=migratable_count,
        skipped_count=skipped_count,
        conflict_count=conflict_count,
        projected_runtime_run_ids=projected_run_ids,
        backup_path=_backup_path_for_source(
            workspace_dir,
            source_type=source_type,
            source_path=root,
            content_sha256=content_sha256,
        ),
        issues=issues,
    )


def _totals_for_sources(
    sources: list[RuntimeMigrationSourceReport],
) -> RuntimeMigrationTotals:
    return RuntimeMigrationTotals(
        source_count=len(sources),
        bytes=sum(source.bytes for source in sources),
        record_count=sum(source.record_count for source in sources),
        migratable_count=sum(source.migratable_count for source in sources),
        skipped_count=sum(source.skipped_count for source in sources),
        conflict_count=sum(source.conflict_count for source in sources),
        issue_count=sum(len(source.issues) for source in sources),
    )


def _legacy_writer_report_state(
    mode: LegacyRuntimeWriterMode,
) -> tuple[RuntimeMigrationWriterStatus, bool, bool]:
    if mode == "read_only":
        return ("legacy_write_stopped_read_compatibility", False, True)
    return ("legacy_write_active", True, False)


def _writer_inventory_item(
    *,
    workspace_dir: Path,
    sources: list[RuntimeMigrationSourceReport],
    writer_id: str,
    source_type: RuntimeMigrationSourceType,
    legacy_writer: str,
    legacy_path_glob: str,
    canonical_writer: str,
    last_dependent_readers: list[str],
    migration_status: RuntimeMigrationWriterStatus = "legacy_write_active",
    legacy_write_allowed: bool = True,
    can_stop_writes: bool = False,
    stop_writes_blockers: list[str],
    delete_preconditions: list[str],
    verification: list[str],
) -> RuntimeMigrationWriterInventoryItem:
    observed = [source for source in sources if source.source_type == source_type]
    return RuntimeMigrationWriterInventoryItem(
        writer_id=writer_id,
        source_type=source_type,
        legacy_writer=legacy_writer,
        legacy_path_glob=legacy_path_glob,
        canonical_writer=canonical_writer,
        last_dependent_readers=last_dependent_readers,
        migration_status=migration_status,
        legacy_write_allowed=legacy_write_allowed,
        can_stop_writes=can_stop_writes,
        stop_writes_blockers=stop_writes_blockers,
        delete_preconditions=delete_preconditions,
        verification=verification,
        observed_source_count=len(observed),
        observed_record_count=sum(source.record_count for source in observed),
        observed_issue_count=sum(len(source.issues) for source in observed),
        observed_paths=[source.path for source in observed],
    )


def _build_writer_inventory(
    workspace_dir: Path,
    sources: list[RuntimeMigrationSourceReport],
) -> list[RuntimeMigrationWriterInventoryItem]:
    root = str(workspace_dir)
    writer_mode = get_legacy_runtime_writer_mode()
    migration_status, legacy_write_allowed, can_stop_writes = (
        _legacy_writer_report_state(writer_mode)
    )
    release_cycle_blockers = [
        "two_official_release_cycles_without_fallback_write",
        "migration_dry_run_backup_restore_and_downgrade_evidence",
    ]
    common_delete_preconditions = [
        "read_only_legacy_adapter_retained",
        "unknown_schema_diagnostic_reports_instead_of_clearing_data",
        "official_release_readiness_gates_pass",
    ]
    common_verification = [
        "tests/unit/app/test_runtime_migration_report.py",
        "packaged official lifecycle smoke evidence",
    ]

    def _stop_blockers(*extra: str) -> list[str]:
        if writer_mode == "read_only":
            return []
        return [*release_cycle_blockers, *extra]

    return [
        _writer_inventory_item(
            workspace_dir=workspace_dir,
            sources=sources,
            writer_id="legacy_runtime_event_ledger_writer",
            source_type="experience_ledger",
            legacy_writer="lantai.app.experience.events.append_chat_event",
            legacy_path_glob=f"{root}/.lantai/experience/events*.jsonl",
            canonical_writer="lantai.runtime.store.RuntimeRunStore plus canonical RuntimeEvent projection",
            last_dependent_readers=[
                "lantai.app.experience.events.build_runtime_events_response",
                "lantai.app.experience.events.build_runtime_run_events_response",
                "console Chat RuntimeEventStreamBridge",
            ],
            migration_status=migration_status,
            legacy_write_allowed=legacy_write_allowed,
            can_stop_writes=can_stop_writes,
            stop_writes_blockers=_stop_blockers(
                "runtime event stream/read APIs no longer require the legacy experience ledger as their write source",
            ),
            delete_preconditions=[
                *common_delete_preconditions,
                "historical event replay is covered by compatibility adapter fixtures",
            ],
            verification=[
                *common_verification,
                "tests/unit/app/test_runtime_events.py",
                "tests/unit/app/test_chat_runtime_run_adapter.py",
            ],
        ),
        _writer_inventory_item(
            workspace_dir=workspace_dir,
            sources=sources,
            writer_id="legacy_task_run_snapshot_writer",
            source_type="task_runs",
            legacy_writer="lantai.app.capabilities.repo.CapabilityRepository.save_task_runs",
            legacy_path_glob=f"{root}/capabilities/task-runs.json",
            canonical_writer="lantai.app.capabilities.runtime_runs.record_task_run_runtime_contract",
            last_dependent_readers=[
                "lantai.app.capabilities.repo.CapabilityRepository.list_task_runs",
                "Task Center legacy capability/task projections",
            ],
            migration_status=migration_status,
            legacy_write_allowed=legacy_write_allowed,
            can_stop_writes=can_stop_writes,
            stop_writes_blockers=_stop_blockers(
                "Task Center and capability APIs read TaskRun snapshots only as projections",
            ),
            delete_preconditions=[
                *common_delete_preconditions,
                "TaskRun snapshot materializer can rebuild from RuntimeRunStore or archived compatibility data",
            ],
            verification=[
                *common_verification,
                "tests/unit/app/test_capability_runtime_run_adapter.py",
                "tests/unit/app/test_capabilities.py",
            ],
        ),
        _writer_inventory_item(
            workspace_dir=workspace_dir,
            sources=sources,
            writer_id="legacy_evaluation_report_writer",
            source_type="evaluation_reports",
            legacy_writer="lantai.app.capabilities.repo.CapabilityRepository.save_evaluation_report",
            legacy_path_glob=f"{root}/capabilities/evaluations/**/*.json",
            canonical_writer="RuntimeRunStore loop/evaluation sidecars and versioned EvaluationReport history",
            last_dependent_readers=[
                "lantai.app.capabilities.repo.CapabilityRepository.list_evaluation_reports",
                "runtime export and loop evaluation evidence readers",
            ],
            migration_status=migration_status,
            legacy_write_allowed=legacy_write_allowed,
            can_stop_writes=can_stop_writes,
            stop_writes_blockers=_stop_blockers(
                "evaluation history ownership is documented as read-only evidence rather than run-state authority",
            ),
            delete_preconditions=[
                *common_delete_preconditions,
                "flat and nested evaluation report formats have downgrade/read compatibility coverage",
            ],
            verification=[
                *common_verification,
                "tests/unit/runtime/test_loop_evaluator_executor.py",
                "tests/unit/runtime/test_loop_evaluation.py",
            ],
        ),
        _writer_inventory_item(
            workspace_dir=workspace_dir,
            sources=sources,
            writer_id="legacy_mission_state_writer",
            source_type="mission_state",
            legacy_writer="lantai.agents.mission.state.write_loop_config/write_prd_json/init_progress_txt",
            legacy_path_glob=f"{root}/missions/mission-*/*",
            canonical_writer="lantai.agents.mission.runtime_runs.record_mission_runtime_run_started/settled",
            last_dependent_readers=[
                "lantai.app.runner.mission_dispatch.detect_active_mission_phase",
                "lantai.agents.mission.mission_runner",
            ],
            migration_status=migration_status,
            legacy_write_allowed=legacy_write_allowed,
            can_stop_writes=can_stop_writes,
            stop_writes_blockers=_stop_blockers(
                "Mission phase recovery no longer depends on loop_config.json/prd.json/progress.txt as mutable state",
            ),
            delete_preconditions=[
                *common_delete_preconditions,
                "Mission adapter can rebuild or diagnose legacy phase state after downgrade",
            ],
            verification=[
                *common_verification,
                "tests/unit/app/test_mission_runtime_run_adapter.py",
                "tests/unit/app/test_runner_behavior.py",
            ],
        ),
        _writer_inventory_item(
            workspace_dir=workspace_dir,
            sources=sources,
            writer_id="legacy_browser_task_state_writer",
            source_type="browser_task_state",
            legacy_writer="lantai.app.browser_workbench.tasks.BrowserTaskStore.save",
            legacy_path_glob=f"{root}/browser_workbench/tasks/*.json",
            canonical_writer="lantai.app.browser_workbench.runtime_runs.record_browser_task_runtime_run_started/settled plus RuntimeEffectJournal",
            last_dependent_readers=[
                "lantai.app.browser_workbench.tasks.BrowserTaskStore.load_all",
                "BrowserTaskRuntime restart/replay recovery",
            ],
            migration_status=migration_status,
            legacy_write_allowed=legacy_write_allowed,
            can_stop_writes=can_stop_writes,
            stop_writes_blockers=_stop_blockers(
                "Browser task restart/replay recovery reads canonical run/effect state without the task JSON ledger",
            ),
            delete_preconditions=[
                *common_delete_preconditions,
                "browser task effect replay and MAS policy evidence pass after legacy task JSON writes are disabled",
            ],
            verification=[
                *common_verification,
                "tests/unit/app/test_browser_runtime_run_adapter.py",
                "tests/unit/app/test_browser_effect_journal.py",
                "scripts/run_browser_task_effect_crash_smoke.py",
            ],
        ),
    ]


def build_runtime_migration_report(
    workspace_dir: Path | str,
    *,
    release_edition: RuntimeReleaseEdition = "official",
) -> RuntimeMigrationReport:
    """Build a side-effect-free dry-run Runtime migration report."""

    root = Path(workspace_dir).expanduser()
    sources: list[RuntimeMigrationSourceReport] = []
    known_task_run_ids: set[str] = set()

    if root.exists():
        seen_event_ids: set[str] = set()
        seen_sequences: dict[int, str] = {}
        for path in _experience_ledger_paths(root):
            sources.append(
                _scan_experience_ledger(
                    root,
                    path,
                    seen_event_ids=seen_event_ids,
                    seen_sequences=seen_sequences,
                )
            )

        task_runs_report, known_task_run_ids = _scan_task_runs(
            root,
            release_edition=release_edition,
        )
        if task_runs_report is not None:
            sources.append(task_runs_report)

        evaluations_report = _scan_evaluation_reports(
            root,
            known_task_run_ids=known_task_run_ids,
        )
        if evaluations_report is not None:
            sources.append(evaluations_report)

        sources.extend(
            _scan_mission_dirs(root, release_edition=release_edition),
        )

        browser_task_report = _scan_browser_task_state(root)
        if browser_task_report is not None:
            sources.append(browser_task_report)

    backup_targets = [
        source.backup_path for source in sources if source.backup_path is not None
    ]
    return RuntimeMigrationReport(
        workspace_dir=str(root),
        release_edition=release_edition,
        legacy_writer_mode=get_legacy_runtime_writer_mode(),
        totals=_totals_for_sources(sources),
        backup_targets=backup_targets,
        sources=sources,
        writer_inventory=_build_writer_inventory(root, sources),
    )
