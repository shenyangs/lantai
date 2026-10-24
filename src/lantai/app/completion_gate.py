# -*- coding: utf-8 -*-
"""System-owned completion gate for chat deliverables.

Model prose is never evidence of delivery.  This module validates durable
files, Runtime effects, explicit acceptance evidence, and the Works record
before an artifact run may become ``succeeded + verified``.
"""

from __future__ import annotations

import json
import hashlib
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from PIL import Image, UnidentifiedImageError

from lantai.ooxml_content_types import find_content_type_sequence_issue
from lantai.runtime.effects import RuntimeEffectJournal
from lantai.runtime.artifact_effects import (
    RuntimeArtifactEffectJournal,
    RuntimeArtifactEffectRecord,
    resolve_artifact_effect_path,
)
from lantai.runtime.runs import (
    RuntimeCompletionEvidence,
    RuntimeDeliveryStatus,
    RuntimeRunContract,
    RuntimeValidationMetadata,
)
from lantai.runtime.store import RuntimeRunStore

from .works import WorkItem, add_work


_IGNORED_PARTS = {
    ".git",
    ".lantai",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
}
_TERMINAL_EFFECT_STATUSES = {
    "blocked",
    "committed",
    "failed",
    "probed_committed",
    "safe_to_retry",
    "compensated",
    "human_resolved",
    "cancelled",
}
_TEST_REQUEST_RE = re.compile(r"(?:测试|单元测试|\btests?\b)", re.IGNORECASE)
_ASSERTION_RE = re.compile(
    r"(?:\bassert\b|\bexpect\s*\(|\bassert(?:Equal|True|False|Raises)\b|"
    r"\bit\s*\(|\btest\s*\(|pytest\.)",
    re.IGNORECASE,
)
_TEST_EXECUTION_RE = re.compile(
    r"(?:\bpytest\b|\bvitest\b|\bjest\b|\bplaywright\b|\bcypress\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|"
    r"\b(?:go|cargo|swift)\s+test\b|"
    r"(?:^|[/\\\s])test_[^\s]+\.py\b|\.test\.[a-z0-9]+\b|"
    r"\.spec\.[a-z0-9]+\b|_test\.py\b)",
    re.IGNORECASE,
)
_CAPABILITY_TERMS = ("农历", "真太阳时", "节气")
_DURABLE_REQUEST_VERB_RE = re.compile(
    r"(?:创建|制作|生成|做|写|开发|实现|导出|产出|整理成|转换成|设计|"
    r"\b(?:create|build|make|write|develop|implement|export|generate|design)\b)",
    re.IGNORECASE,
)
_DURABLE_REQUEST_NOUN_RE = re.compile(
    r"(?:网站|网页|应用|游戏|报告|文档|文件|表格|演示文稿|海报|图片|视频|音频|代码|脚本|PPTX?|PDF|"
    r"\b(?:html|website|webpage|app|application|game|report|document|file|"
    r"spreadsheet|slides?|pptx?|pdf|image|video|audio|code|script)\b)",
    re.IGNORECASE,
)
_COMPLETION_CLAIM_RE = re.compile(
    r"(?:^|[。！？\n])\s*(?:我|现在|目前|任务|工作|网站|网页|应用|游戏|报告|文档|文件|"
    r"表格|代码|脚本)?\s*(?:已经|现已|已|都|全部)?\s*"
    r"(?:完成|做好|做完|创建好|制作好|生成好|导出好|交付)|"
    r"\b(?:done|completed|finished|delivered)\b",
    re.IGNORECASE,
)
_INCOMPLETE_DISCLOSURE_RE = re.compile(
    r"(?:未完成|没完成|没能|无法完成|尚未完成|还没完成|并未完成|没有完成|"
    r"完成(?:了)?(?:一半|部分|\s*\d{1,3}%))",
    re.IGNORECASE,
)
_TERMINAL_COLLABORATION_MEMBER_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "interrupted",
    "timed_out",
    "timeout",
    "replaced",
}
_VALIDATOR_ID = "runtime-completion-gate"
_VALIDATOR_VERSION = "2"
_RULESET_ID = "delivery-readiness"
_RULESET_VERSION = "2"

ValidationOutcome = Literal["passed", "failed", "not_run", "error", "waived"]


@dataclass(frozen=True)
class CompletionGateResult:
    allow_success: bool
    delivery_status: RuntimeDeliveryStatus
    evidence: RuntimeCompletionEvidence
    acceptance_results: tuple[dict[str, object], ...]
    primary_path: str | None = None
    work_item: WorkItem | None = None
    candidate_work_item: WorkItem | None = None
    failure_summary: str | None = None
    verification_receipt: dict[str, object] | None = None


def presentation_delivery_repair(
    result: CompletionGateResult,
) -> dict[str, object] | None:
    """Describe the existing trusted recovery route, without changing any check.

    A file/render can exist without a full-scope system review. Do not turn model
    assertions or ordinary image views into an attestation or a verified Works item.
    """
    if (
        result.work_item is not None
        or Path(result.primary_path or "").suffix.lower() != ".pptx"
    ):
        return None
    visual_checks = {"visual_render_evidence", "visual_verification_evidence"}
    failed = [
        str(check["id"])
        for check in result.acceptance_results
        if check.get("id") in visual_checks and not check.get("passed")
    ]
    if not failed:
        return None
    return {
        "schema_version": 1,
        "candidate_preserved": bool(result.primary_path),
        "failed_checks": sorted(set(failed)),
        "instructions": [
            "Preserve the existing candidate. Do not overwrite it, fabricate a "
            "receipt, or claim completed delivery while these checks fail.",
            "verification_paths must be actual, decodable workspace render files "
            "(PNG/JPEG/WebP/PDF), not the PPTX, a script, or a note. Use the "
            "verification_paths in the presentation result's model-visible "
            "finalize_deliverable_inputs.",
            "Rendering, prose acceptance_evidence and ordinary view_image calls "
            "are not a system-owned full-scope visual review; every required page "
            "of the exact current PPTX must pass the canonical review.",
            "If a presentation job already exists, call "
            "get_presentation_generation_status with its job_id. Follow its "
            "allowed recovery actions and use finalize_presentation_generation "
            "only when the job is publish-ready; do not submit a duplicate job "
            "when an existing outcome is unknown.",
            "If no successful attested presentation job exists (for example a "
            "Python-generated fallback), correct the original create_presentation "
            "call using source_index_repair / grounding_repair, complete sources, "
            "storyboard and slides. Generate to a new output_filename so the "
            "candidate is preserved. Do not erase source bindings or bypass review.",
            "Only a successful create_presentation or "
            "finalize_presentation_generation result with same-attempt, "
            "system-owned full-page verification can support finalize_deliverable. "
            "Then use that result's finalize_deliverable_inputs (primary_path, "
            "entrypoint, kind, verification_paths and acceptance_evidence) and the "
            "original title; retain "
            "the original user task and required facts.",
        ],
    }


@dataclass(frozen=True)
class _VisualVerificationScope:
    """System-owned statement of exactly which source pages were inspected."""

    source_page_count: int
    required_page_indexes: tuple[int, ...]
    observed_page_indexes: tuple[int, ...]
    validator_id: str
    ruleset_id: str
    ruleset_version: str

    def to_receipt(self) -> dict[str, object]:
        return {
            "scope_kind": "pages",
            "source_page_count": self.source_page_count,
            "required_page_indexes": list(self.required_page_indexes),
            "observed_page_indexes": list(self.observed_page_indexes),
            "validator_id": self.validator_id,
            "ruleset_id": self.ruleset_id,
            "ruleset_version": self.ruleset_version,
        }


def _visual_scope_from_receipt(value: object) -> _VisualVerificationScope | None:
    if not isinstance(value, dict) or value.get("scope_kind") != "pages":
        return None
    try:
        source_page_count = int(value.get("source_page_count") or 0)
        required_page_indexes = tuple(
            int(item) for item in value.get("required_page_indexes", ())
        )
        observed_page_indexes = tuple(
            int(item) for item in value.get("observed_page_indexes", ())
        )
    except (TypeError, ValueError):
        return None
    validator_id = str(value.get("validator_id") or "").strip()
    ruleset_id = str(value.get("ruleset_id") or "").strip()
    ruleset_version = str(value.get("ruleset_version") or "").strip()
    full_scope = tuple(range(1, source_page_count + 1))
    if not (
        source_page_count > 0
        and required_page_indexes == full_scope
        and observed_page_indexes == full_scope
        and validator_id
        and ruleset_id
        and ruleset_version
    ):
        return None
    return _VisualVerificationScope(
        source_page_count=source_page_count,
        required_page_indexes=required_page_indexes,
        observed_page_indexes=observed_page_indexes,
        validator_id=validator_id,
        ruleset_id=ruleset_id,
        ruleset_version=ruleset_version,
    )


def _result(
    check_id: str,
    passed: bool,
    detail: str,
    *,
    required: bool = True,
    outcome: ValidationOutcome | None = None,
    evidence_refs: Iterable[str] = (),
) -> dict[str, object]:
    resolved_outcome: ValidationOutcome = outcome or (
        "passed" if passed else "failed"
    )
    expected_passed = resolved_outcome in {"passed", "waived"}
    if passed is not expected_passed:
        raise ValueError("validation outcome and passed compatibility flag disagree")
    normalized_refs = tuple(
        dict.fromkeys(str(value).strip() for value in evidence_refs if str(value).strip())
    )
    if resolved_outcome == "waived" and not normalized_refs:
        raise ValueError("waived validation requires an explicit approval evidence ref")
    result: dict[str, object] = {
        "id": check_id,
        "passed": passed,
        "outcome": resolved_outcome,
        "required": required,
        "detail": detail,
    }
    if normalized_refs:
        result["evidence_refs"] = normalized_refs
    return result


def _reported_acceptance_results(
    evidence: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Record model-authored notes without trusting them as gate verdicts."""

    reported: list[dict[str, object]] = []
    for index, raw_item in enumerate(evidence, start=1):
        item = raw_item if isinstance(raw_item, dict) else {}
        check = str(item.get("check") or "").strip()
        result = str(item.get("result") or item.get("detail") or "").strip()
        reported_status = str(item.get("status") or "").strip()
        label = check or f"Acceptance note {index}"
        status_suffix = f" [{reported_status}]" if reported_status else ""
        detail = (
            "Reported by the agent (informational): "
            f"{label}{status_suffix}: {result}"
            if result
            else f"Reported by the agent (informational): {label}{status_suffix}"
        )
        note = _result(
            f"reported_acceptance:{index}",
            bool(check or result or reported_status),
            detail,
            required=False,
        )
        note["source"] = "model_reported"
        reported.append(note)
    return reported


def _validation_metadata(
    checks: Iterable[dict[str, object]],
) -> RuntimeValidationMetadata:
    system_checks = tuple(
        row for row in checks if row.get("source") != "model_reported"
    )
    return RuntimeValidationMetadata(
        validator_id=_VALIDATOR_ID,
        validator_version=_VALIDATOR_VERSION,
        ruleset_id=_RULESET_ID,
        ruleset_version=_RULESET_VERSION,
        assessed_at=datetime.now(timezone.utc),
        required_scope=tuple(
            str(row.get("id") or "")
            for row in system_checks
            if row.get("required", True) and row.get("id")
        ),
        observed_scope=tuple(
            str(row.get("id") or "")
            for row in system_checks
            if row.get("id")
            and row.get("outcome") in {"passed", "failed", "waived"}
        ),
    )


def _validate_explicit_path(path: str | Path, root: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        raise ValueError("deliverable cannot be a symbolic link")
    resolved = candidate.resolve(strict=True)
    allowed_root = root.resolve(strict=False)
    if allowed_root not in resolved.parents:
        raise PermissionError("deliverable is outside the active workspace")
    if not resolved.is_file():
        raise ValueError("deliverable is not a file")
    return resolved


def _html_checks(path: Path) -> list[dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [_result("html_readable", False, str(exc), outcome="error")]
    except UnicodeError as exc:
        return [_result("html_readable", False, str(exc))]
    checks = [
        _result(
            "html_entrypoint",
            bool(re.search(r"<!doctype\s+html|<html\b", text, re.IGNORECASE)),
            "HTML entrypoint contains a document root.",
        ),
    ]
    missing: list[str] = []
    for raw_ref in re.findall(
        r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]",
        text,
        flags=re.IGNORECASE,
    ):
        ref = raw_ref.split("?", 1)[0].split("#", 1)[0].strip()
        if not ref or re.match(r"^(?:https?:|data:|mailto:|javascript:|//)", ref):
            continue
        target = (path.parent / ref.lstrip("/")).resolve(strict=False)
        if not target.exists():
            missing.append(raw_ref)
    checks.append(
        _result(
            "html_local_assets",
            not missing,
            "All local HTML assets exist."
            if not missing
            else f"Missing local assets: {', '.join(missing[:8])}",
        ),
    )
    return checks


def _declared_file_openability_check(path: Path) -> dict[str, object]:
    """Validate common deliverable containers without trusting file suffixes."""

    suffix = path.suffix.lower()
    try:
        size = path.stat().st_size
        if size <= 0:
            return _result("deliverable_openable", False, "Deliverable is empty.")
        if suffix in {".pptx", ".docx", ".xlsx", ".zip"}:
            required_members = {
                ".pptx": {"[Content_Types].xml", "ppt/presentation.xml"},
                ".docx": {"[Content_Types].xml", "word/document.xml"},
                ".xlsx": {"[Content_Types].xml", "xl/workbook.xml"},
                ".zip": set(),
            }[suffix]
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                corrupt_member = archive.testzip()
            missing = sorted(required_members - names)
            if corrupt_member or missing:
                detail = (
                    f"Corrupt archive member: {corrupt_member}."
                    if corrupt_member
                    else f"Missing required package members: {', '.join(missing)}."
                )
                return _result("deliverable_openable", False, detail)
            if suffix == ".pptx":
                content_type_issue = find_content_type_sequence_issue(path)
                if content_type_issue:
                    return _result(
                        "deliverable_openable",
                        False,
                        content_type_issue,
                    )
        elif suffix == ".pdf":
            with path.open("rb") as handle:
                header = handle.read(5)
                handle.seek(max(0, size - 2048))
                trailer = handle.read()
            if header != b"%PDF-" or b"%%EOF" not in trailer:
                return _result(
                    "deliverable_openable",
                    False,
                    "PDF signature or trailer is invalid.",
                )
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    width, height = image.size
            except (OSError, UnidentifiedImageError, ValueError) as exc:
                return _result(
                    "deliverable_openable",
                    False,
                    f"Image decoder rejected {path.name}: {exc}",
                )
            if width <= 0 or height <= 0:
                return _result(
                    "deliverable_openable",
                    False,
                    f"Image has invalid dimensions: {width}x{height}.",
                )
        else:
            with path.open("rb") as handle:
                handle.read(1)
    except zipfile.BadZipFile as exc:
        return _result(
            "deliverable_openable",
            False,
            f"Deliverable package is invalid: {exc}",
        )
    except (OSError, UnicodeError) as exc:
        return _result(
            "deliverable_openable",
            False,
            f"Deliverable validator error: {exc}",
            outcome="error",
        )
    return _result(
        "deliverable_openable",
        True,
        f"Deliverable container opens successfully: {path.name}",
    )


def _visual_verification_observation(
    paths: Iterable[str | Path],
    root: Path,
    *,
    verification_scope: _VisualVerificationScope | None = None,
) -> tuple[list[dict[str, object]], tuple[dict[str, object], ...]]:
    """Validate render files without confusing rendering with visual review."""

    verified: list[Path] = []
    receipts: list[dict[str, object]] = []
    failures: list[str] = []
    validator_errors: list[str] = []
    unsupported: list[str] = []
    for raw_path in paths:
        try:
            candidate = _validate_explicit_path(raw_path, root)
        except (PermissionError, ValueError) as exc:
            failures.append(str(exc))
            continue
        except OSError as exc:
            validator_errors.append(str(exc))
            continue
        if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
            unsupported.append(f"Unsupported visual evidence: {candidate.name}")
            continue
        openability = _declared_file_openability_check(candidate)
        if not bool(openability.get("passed")):
            target = (
                validator_errors
                if openability.get("outcome") == "error"
                else failures
            )
            target.append(str(openability.get("detail") or candidate.name))
            continue
        verified.append(candidate)
        try:
            receipts.append(
                {
                    "relative_path": candidate.relative_to(
                        root.expanduser().resolve(strict=False)
                    ).as_posix(),
                    "size_bytes": candidate.stat().st_size,
                    "sha256": _sha256_file(candidate),
                    "media_type": mimetypes.guess_type(candidate.name)[0]
                    or "application/octet-stream",
                }
            )
        except (OSError, RuntimeError, ValueError) as exc:
            verified.pop()
            validator_errors.append(str(exc))
    if verified and not failures and not validator_errors and not unsupported:
        render_outcome: ValidationOutcome = "passed"
    elif validator_errors:
        render_outcome = "error"
    elif failures:
        render_outcome = "failed"
    else:
        render_outcome = "not_run"
    detail_suffix = failures + validator_errors + unsupported
    render_check = _result(
        "visual_render_evidence",
        render_outcome == "passed",
        (
            "Rendered visual evidence opens successfully: "
            + ", ".join(path.name for path in verified)
            if render_outcome == "passed"
            else "No complete, decodable render set exists."
            + (f" {'; '.join(detail_suffix[:4])}" if detail_suffix else "")
        ),
        outcome=render_outcome,
    )
    full_scope = bool(
        verification_scope is not None
        and render_outcome == "passed"
        and len(verified) == verification_scope.source_page_count
        and len(receipts) == verification_scope.source_page_count
    )
    if full_scope:
        assert verification_scope is not None
        verification_outcome: ValidationOutcome = "passed"
        verification_detail = (
            "System-owned visual review covered every required page "
            f"({len(verification_scope.observed_page_indexes)}/"
            f"{verification_scope.source_page_count}) with "
            f"{verification_scope.validator_id}."
        )
        for page_index, receipt in zip(
            verification_scope.observed_page_indexes,
            receipts,
            strict=True,
        ):
            receipt.update(
                {
                    "source_page_index": page_index,
                    "source_page_count": verification_scope.source_page_count,
                    "visually_checked": True,
                }
            )
    else:
        verification_outcome = "not_run"
        verification_detail = (
            "Render evidence exists, but no system-owned full-scope visual review "
            "covers every required page."
            if verified
            else "No system-owned visual review evidence exists."
        )
    return (
        [
            render_check,
            _result(
                "visual_verification_evidence",
                full_scope,
                verification_detail,
                outcome=verification_outcome,
            )
        ],
        tuple(receipts[:64]),
    )


def _contains_real_assertion(root: Path) -> bool:
    for candidate in root.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if any(part in _IGNORED_PARTS for part in candidate.parts):
            continue
        name = candidate.name.lower()
        if not (
            name.startswith("test_")
            or ".test." in name
            or ".spec." in name
            or name.endswith("_test.py")
        ):
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _ASSERTION_RE.search(text):
            return True
    return False


def _has_successful_test_execution(events: Iterable[Any]) -> bool:
    def exit_code_is_zero(value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "exit_code" and nested == 0:
                    return True
                if exit_code_is_zero(nested):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(exit_code_is_zero(item) for item in value)
        return False

    for event in events:
        if str(getattr(event, "type", "")) not in {
            "tool.call.completed",
            "tool.completed",
        }:
            continue
        payload = dict(getattr(event, "payload", {}) or {})
        if payload.get("ok") is False or str(getattr(event, "status", "")) == "failed":
            continue
        serialized = str(payload)
        if exit_code_is_zero(payload) and _TEST_EXECUTION_RE.search(serialized):
            return True
    return False


def _json_primary_path(value: Any) -> str | None:
    """Find a bounded primary_path inside one trusted tool-result payload."""

    if isinstance(value, dict):
        direct = value.get("primary_path")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for key in ("data", "structured_content", "result", "output", "content"):
            if key not in value:
                continue
            found = _json_primary_path(value[key])
            if found:
                return found
        text = value.get("text")
        if isinstance(text, str) and text.lstrip().startswith("{"):
            try:
                decoded = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return None
            return _json_primary_path(decoded)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _json_primary_path(item)
            if found:
                return found
    return None


def _json_finalize_payload(value: Any) -> dict[str, Any] | None:
    """Find one structured runtime finalize result without trusting prose."""

    if isinstance(value, dict):
        receipt = value.get("verification_receipt")
        if isinstance(receipt, dict):
            return value
        for key in ("data", "structured_content", "result", "output", "content"):
            if key not in value:
                continue
            found = _json_finalize_payload(value[key])
            if found is not None:
                return found
        text = value.get("text")
        if (
            isinstance(text, str)
            and len(text) <= 1_000_000
            and text.lstrip().startswith("{")
        ):
            try:
                decoded = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return None
            return _json_finalize_payload(decoded)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _json_finalize_payload(item)
            if found is not None:
                return found
    return None


def _finalized_primary_path(events: Iterable[Any]) -> str | None:
    """Read-old adapter for pre-S6 successful finalize events."""

    recovered: str | None = None
    for event in events:
        if str(getattr(event, "type", "")) not in {
            "tool.call.completed",
            "tool.completed",
        }:
            continue
        payload = dict(getattr(event, "payload", {}) or {})
        tool_name = str(payload.get("name") or payload.get("tool_name") or "")
        if tool_name != "finalize_deliverable":
            continue
        if payload.get("ok") is False or str(getattr(event, "status", "")) == "failed":
            continue
        candidate = _json_primary_path(payload.get("output"))
        if candidate:
            recovered = candidate
    return recovered


def _committed_primary_artifact_effect(
    state_workspace_dir: Path,
    contract: RuntimeRunContract,
) -> RuntimeArtifactEffectRecord | None:
    """Return the best authoritative artifact fact for terminal delivery.

    A successful ``finalize_deliverable`` primary remains preferred.  A
    committed delivery effect is still durable evidence that the exact file
    reached the user, so it must not be discarded when finalization failed
    earlier in the same turn.
    """

    records = RuntimeArtifactEffectJournal(state_workspace_dir).list_records(
        contract.run_id,
        contract.attempt_id,
    )
    latest: dict[str, RuntimeArtifactEffectRecord] = {}
    for record in records:
        current = latest.get(record.artifact_effect_id)
        if current is None or record.revision > current.revision:
            latest[record.artifact_effect_id] = record

    def newest(
        records: Iterable[RuntimeArtifactEffectRecord],
    ) -> RuntimeArtifactEffectRecord | None:
        candidates = list(records)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda record: (
                record.settled_at or record.created_at,
                record.revision,
                record.artifact_effect_id,
            ),
        )

    committed = [
        record
        for record in latest.values()
        if record.status in {"committed", "delivered"}
    ]
    return newest(
        record for record in committed if record.artifact_role == "primary"
    ) or newest(
        record for record in committed if record.artifact_role == "delivery"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _latest_committed_finalize_effect(
    state_workspace_dir: Path,
    contract: RuntimeRunContract,
    effect_id: str,
    tool_call_id: str,
) -> bool:
    try:
        records = [
            record
            for record in RuntimeEffectJournal(state_workspace_dir).list_effects(
                contract.run_id,
                contract.attempt_id,
            )
            if record.effect_id == effect_id
        ]
    except Exception:
        return False
    if not records:
        return False
    latest = max(records, key=lambda record: record.revision)
    return bool(
        latest.status == "committed"
        and latest.tool_name == "finalize_deliverable"
        and latest.step_id == f"tool-call:{tool_call_id}"
        and latest.run_id == contract.run_id
        and latest.attempt_id == contract.attempt_id
    )


def _event_run_id(event: Any) -> str:
    return str(
        getattr(event, "run_id", None)
        or getattr(event, "turn_id", None)
        or ""
    ).strip()


def _resolve_attested_path(
    value: object,
    roots: Iterable[Path],
) -> tuple[Path, Path] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    for raw_root in roots:
        root = raw_root.expanduser().resolve(strict=False)
        try:
            resolved = (
                candidate.resolve(strict=True)
                if candidate.is_absolute()
                else (root / candidate).resolve(strict=True)
            )
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_file() and not resolved.is_symlink():
            return resolved, root
    return None


def _recover_system_visual_attestation(
    events: Iterable[Any],
    *,
    state_root: Path,
    effective_root: Path,
    contract: RuntimeRunContract,
    artifact_path: Path,
) -> tuple[
    tuple[Path, ...],
    dict[str, object],
    _VisualVerificationScope | None,
    Path,
]:
    """Recover complete per-page review only from a trusted tool event."""

    failure = _result(
        "visual_verification_attestation",
        False,
        "No system-owned full-scope visual verification attestation is bound to this artifact.",
        required=False,
        outcome="not_run",
    )
    roots = (state_root, effective_root)
    expected_primary_hash = _sha256_file(artifact_path)
    for event in reversed(tuple(events)):
        if (
            str(getattr(event, "type", ""))
            not in {"tool.call.completed", "tool.completed"}
            or str(getattr(event, "source", "")) != "tool"
            or _event_run_id(event) != contract.run_id
            or str(getattr(event, "status", "")) != "completed"
        ):
            continue
        payload = dict(getattr(event, "payload", {}) or {})
        if str(payload.get("attempt_id") or "").strip() != contract.attempt_id:
            continue
        canonical = payload.get("canonical_tool")
        if not isinstance(canonical, dict) or (
            str(canonical.get("effect_attempt_id") or "").strip()
            != contract.attempt_id
        ):
            continue
        if (
            str(payload.get("name") or payload.get("tool_name") or "")
            not in {
                "create_presentation",
                "finalize_presentation_generation",
            }
            or payload.get("ok") is not True
        ):
            continue
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        attestation = metadata.get("verification_attestation")
        if not isinstance(attestation, dict):
            continue
        if (
            attestation.get("schema_version") != 1
            or attestation.get("kind") != "presentation"
            or str(attestation.get("primary_sha256") or "")
            != expected_primary_hash
        ):
            continue
        resolved_primary = _resolve_attested_path(
            attestation.get("primary_path"),
            roots,
        )
        if resolved_primary is None or resolved_primary[0] != artifact_path.resolve():
            continue
        resolved_receipt = _resolve_attested_path(
            attestation.get("receipt_path"),
            roots,
        )
        if resolved_receipt is None:
            continue
        receipt_path, _ = resolved_receipt
        try:
            if (
                receipt_path.stat().st_size > 8_000_000
                or str(attestation.get("receipt_sha256") or "")
                != _sha256_file(receipt_path)
            ):
                continue
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        receipt_primary = _resolve_attested_path(receipt.get("output_path"), roots)
        quality = receipt.get("quality")
        render_rows = receipt.get("render_evidence")
        if not (
            receipt_primary is not None
            and receipt_primary[0] == artifact_path.resolve()
            and f"sha256:{str(receipt.get('output_sha256') or '')}"
            == expected_primary_hash
            and isinstance(quality, dict)
            and isinstance(render_rows, list)
            and render_rows
            and len(render_rows) <= 64
            and quality.get("passed") is True
            and quality.get("delivery_status") == "passed"
            and quality.get("content_complete") is True
            and quality.get("editable") is True
            and quality.get("render_verified") is True
            and quality.get("vision_critique_required") is True
            and quality.get("visual_verified") is True
            and quality.get("render_evidence") == "passed"
            and quality.get("visual_evidence") == "passed"
            and quality.get("editability_evidence") == "passed"
            and quality.get("provenance_evidence") == "passed"
            and not quality.get("blocking_gaps")
            and quality.get("output_sha256") == receipt.get("output_sha256")
            and quality.get("rendered_slide_count") == len(render_rows)
        ):
            continue
        validator_id = str(quality.get("visual_critic_id") or "").strip()
        rendered_slides = quality.get("rendered_slides")
        if not validator_id or not isinstance(rendered_slides, list):
            continue
        quality_by_index = {
            row.get("slide_index"): row
            for row in rendered_slides
            if isinstance(row, dict)
        }
        page_indexes = tuple(
            row.get("slide_index")
            for row in render_rows
            if isinstance(row, dict)
        )
        full_scope = tuple(range(1, len(render_rows) + 1))
        if page_indexes != full_scope or tuple(sorted(quality_by_index)) != full_scope:
            continue
        evidence_paths: list[Path] = []
        evidence_root: Path | None = None
        valid = True
        for row in render_rows:
            assert isinstance(row, dict)
            page_index = int(row["slide_index"])
            quality_row = quality_by_index[page_index]
            resolved_evidence = _resolve_attested_path(row.get("path"), roots)
            if resolved_evidence is None:
                valid = False
                break
            candidate, candidate_root = resolved_evidence
            if evidence_root is None:
                evidence_root = candidate_root
            elif candidate_root != evidence_root:
                valid = False
                break
            try:
                evidence_hash = _sha256_file(candidate).removeprefix("sha256:")
            except OSError:
                valid = False
                break
            if not (
                evidence_hash == row.get("sha256") == quality_row.get("sha256")
                and int(row.get("width") or 0) == int(quality_row.get("width") or 0)
                and int(row.get("height") or 0) == int(quality_row.get("height") or 0)
                and int(row.get("width") or 0) > 0
                and int(row.get("height") or 0) > 0
            ):
                valid = False
                break
            evidence_paths.append(candidate)
        if not valid or evidence_root is None:
            continue
        scope = _VisualVerificationScope(
            source_page_count=len(render_rows),
            required_page_indexes=full_scope,
            observed_page_indexes=full_scope,
            validator_id=validator_id,
            ruleset_id="presentation-quality",
            ruleset_version=str(quality.get("schema_version") or "1.0"),
        )
        if not (
            attestation.get("source_page_count") == scope.source_page_count
            and tuple(attestation.get("observed_page_indexes") or ()) == full_scope
            and attestation.get("validator_id") == validator_id
            and attestation.get("ruleset_id") == scope.ruleset_id
            and str(attestation.get("ruleset_version") or "")
            == scope.ruleset_version
        ):
            continue
        return (
            tuple(evidence_paths),
            _result(
                "visual_verification_attestation",
                True,
                (
                    "System-owned presentation verification is bound to the "
                    f"exact artifact and covers all {len(render_rows)} pages."
                ),
                evidence_refs=(_sha256_file(receipt_path),),
            ),
            scope,
            evidence_root,
        )
    return (), failure, None, effective_root


def _receipt_primary_relative_path(
    value: object,
    root: Path,
) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    try:
        resolved = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (root / candidate).resolve(strict=False)
        )
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _recover_bound_visual_evidence(
    events: Iterable[Any],
    *,
    state_root: Path,
    effective_root: Path,
    contract: RuntimeRunContract,
    primary: RuntimeArtifactEffectRecord,
) -> tuple[
    tuple[Path, ...],
    dict[str, object],
    _VisualVerificationScope | None,
]:
    """Recover only evidence sealed by this run's committed finalize effect."""

    failure_detail = "No bound visual evidence receipt exists for this artifact."
    for event in reversed(tuple(events)):
        if str(getattr(event, "type", "")) not in {
            "tool.call.completed",
            "tool.completed",
        }:
            continue
        payload = dict(getattr(event, "payload", {}) or {})
        if str(payload.get("name") or payload.get("tool_name") or "") != (
            "finalize_deliverable"
        ):
            continue
        failure_detail = "Runtime finalize evidence is not bound to this artifact."
        if (
            str(getattr(event, "source", "")) != "tool"
            or _event_run_id(event) != contract.run_id
            or str(getattr(event, "status", "")) != "completed"
            or payload.get("ok") is not True
            or str(getattr(event, "item_id", "") or payload.get("id") or "")
            != primary.tool_call_id
        ):
            continue
        canonical = payload.get("canonical_tool")
        if not isinstance(canonical, dict):
            continue
        effect_id = str(payload.get("effect_id") or canonical.get("effect_id") or "")
        attempt_id = str(
            payload.get("attempt_id")
            or canonical.get("effect_attempt_id")
            or ""
        )
        if (
            effect_id != primary.source_effect_id
            or attempt_id != contract.attempt_id
            or not _latest_committed_finalize_effect(
                state_root,
                contract,
                effect_id,
                primary.tool_call_id,
            )
        ):
            continue
        if (
            str(canonical.get("effect_id") or "") != effect_id
            or str(canonical.get("effect_attempt_id") or "") != attempt_id
            or str(canonical.get("effect_status") or "") != "committed"
        ):
            continue
        artifact_rows = payload.get("artifact_effects")
        artifact_rows = artifact_rows if isinstance(artifact_rows, list) else []
        artifact_row = next(
            (
                row
                for row in artifact_rows
                if isinstance(row, dict)
                and row.get("artifact_effect_id") == primary.artifact_effect_id
                and row.get("artifact_id") == primary.artifact_id
                and row.get("artifact_role") == "primary"
                and row.get("status") in {"committed", "delivered"}
                and row.get("relative_path") == primary.relative_path
                and row.get("sha256") == primary.sha256
                and row.get("size_bytes") == primary.size_bytes
            ),
            None,
        )
        if artifact_row is None:
            continue
        result_payload = _json_finalize_payload(payload.get("output"))
        if result_payload is None or result_payload.get("delivery_status") != "verified":
            continue
        if str(result_payload.get("source_run_id") or "") != contract.run_id:
            continue
        receipt = result_payload.get("verification_receipt")
        if not isinstance(receipt, dict) or receipt.get("schema_version") != 2:
            continue
        if (
            str(receipt.get("run_id") or "") != contract.run_id
            or str(receipt.get("attempt_id") or "") != contract.attempt_id
            or _receipt_primary_relative_path(
                receipt.get("primary_path"),
                effective_root,
            )
            != primary.relative_path
            or str(receipt.get("primary_sha256") or "") != str(primary.sha256 or "")
        ):
            continue
        raw_visual = receipt.get("visual_evidence")
        if not isinstance(raw_visual, list) or not raw_visual or len(raw_visual) > 64:
            continue
        scope = _visual_scope_from_receipt(receipt.get("verification_scope"))
        if scope is None or len(raw_visual) != scope.source_page_count:
            continue
        verified: list[Path] = []
        valid = True
        for expected_page_index, row in zip(
            scope.observed_page_indexes,
            raw_visual,
            strict=True,
        ):
            if not isinstance(row, dict):
                valid = False
                break
            if not (
                row.get("source_page_index") == expected_page_index
                and row.get("source_page_count") == scope.source_page_count
                and row.get("visually_checked") is True
            ):
                valid = False
                break
            relative_path = str(row.get("relative_path") or "").strip()
            try:
                candidate = _validate_explicit_path(relative_path, effective_root)
            except (OSError, PermissionError, ValueError):
                valid = False
                break
            try:
                identity_matches = bool(
                    candidate.stat().st_size == row.get("size_bytes")
                    and _sha256_file(candidate) == row.get("sha256")
                    and (
                        mimetypes.guess_type(candidate.name)[0]
                        or "application/octet-stream"
                    )
                    == row.get("media_type")
                )
            except OSError:
                identity_matches = False
            if not identity_matches:
                valid = False
                break
            verified.append(candidate)
        if not valid:
            continue
        return (
            tuple(verified),
            _result(
                "visual_evidence_identity",
                True,
                "Visual evidence identity matches this run's committed finalize effect.",
                required=False,
            ),
            scope,
        )
    return (
        (),
        _result(
            "visual_evidence_identity",
            False,
            failure_detail,
            required=False,
            outcome="not_run",
        ),
        None,
    )


def _closed_hosted_collaboration(events: Iterable[Any]) -> bool:
    """Return whether runtime evidence proves a real hosted room closed."""

    for event in events:
        event_type = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        if event_type == "collaboration.closed":
            snapshot = payload
        elif event_type in {"tool.call.completed", "tool.completed"}:
            tool_name = str(payload.get("name") or payload.get("tool_name") or "")
            if tool_name != "collaborate_with_agents" or payload.get("ok") is False:
                continue
            raw_snapshot = payload.get("collaboration")
            snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        else:
            continue
        phase = str(snapshot.get("phase") or "").strip().lower()
        status = str(snapshot.get("status") or "").strip().lower()
        raw_members = snapshot.get("members")
        members = raw_members if isinstance(raw_members, list) else []
        terminal_members = [
            member
            for member in members
            if isinstance(member, dict)
            and str(member.get("status") or "").strip().lower()
            in _TERMINAL_COLLABORATION_MEMBER_STATUSES
        ]
        if (
            phase == "closed"
            and status in {"completed", "partial", "failed", "cancelled"}
            and len(members) >= 2
            and len(terminal_members) == len(members)
        ):
            return True
    return False


def _unverified_artifact_completion_claim(
    contract: RuntimeRunContract,
    assistant_text: str,
    *,
    durable_artifact_required: bool | None = None,
) -> bool:
    """Reject only explicit success claims, never ordinary chat or blockers.

    This is a terminal fail-closed check, not an intake classifier: it does not
    choose tools, alter the task budget, or force artifact mode before the model
    acts.  It only prevents prose from impersonating missing delivery evidence.
    """

    if durable_artifact_required is False or contract.task_contract is None:
        return False
    objective = contract.task_contract.objective
    requested = bool(
        _DURABLE_REQUEST_VERB_RE.search(objective)
        and _DURABLE_REQUEST_NOUN_RE.search(objective)
    )
    if not requested or _INCOMPLETE_DISCLOSURE_RE.search(assistant_text):
        return False
    return bool(_COMPLETION_CLAIM_RE.search(assistant_text))


def _capability_contradictions(
    contract: RuntimeRunContract,
    artifact_text: str,
    assistant_text: str,
) -> list[str]:
    if contract.task_contract is None:
        return []
    promised = "\n".join(
        (contract.task_contract.objective, *contract.task_contract.acceptance)
    )
    implementation = f"{artifact_text}\n{assistant_text}"
    contradictions: list[str] = []
    for term in _CAPABILITY_TERMS:
        if term not in promised:
            continue
        patterns = (
            rf"(?:不支持|未实现|暂不支持|无法).{{0,12}}{re.escape(term)}",
            rf"{re.escape(term)}.{{0,12}}(?:不支持|未实现|暂不支持|无法)",
        )
        if any(re.search(pattern, implementation, re.IGNORECASE) for pattern in patterns):
            contradictions.append(term)
    return contradictions


def _latest_effects(
    state_workspace_dir: Path,
    contract: RuntimeRunContract,
    runtime_events: Iterable[Any] = (),
) -> tuple[list[str], list[str]]:
    journal = RuntimeEffectJournal(state_workspace_dir)
    records = journal.list_effects(
        contract.run_id,
        contract.attempt_id,
    )
    latest: dict[str, Any] = {}
    for record in records:
        current = latest.get(record.effect_id)
        if current is None or record.revision > current.revision:
            latest[record.effect_id] = record
    settled: set[str] = set()
    unsettled: set[str] = set()
    for effect_id, record in latest.items():
        confirmed = record.status in _TERMINAL_EFFECT_STATUSES
        if record.source == "chat" and record.status == "committed":
            # The append can reach disk and still raise before the complete
            # tool transaction and its checkpoint have a durable receipt.
            confirmed = journal.is_effect_settlement_confirmed(record)
        (settled if confirmed else unsettled).add(effect_id)

    for event in runtime_events:
        if (
            _event_run_id(event) != contract.run_id
            or getattr(event, "type", None) != "tool.call.completed"
        ):
            continue
        payload = dict(getattr(event, "payload", {}) or {})
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            continue
        unresolved = (
            metadata.get("effect_settlement_status") == "unconfirmed"
            or (
                metadata.get("automatic_retry_allowed") is False
                and metadata.get("side_effect_outcome") in {"committed", "unknown"}
                and (
                    metadata.get("requires_probe") is True
                    or metadata.get("requires_human") is True
                )
            )
        )
        if not unresolved:
            continue
        canonical = payload.get("canonical_tool")
        canonical = canonical if isinstance(canonical, dict) else {}
        effect_id = str(
            canonical.get("effect_id")
            or payload.get("effect_id")
            or "unconfirmed_tool_effect"
        )
        record = latest.get(effect_id)
        event_revision = canonical.get("effect_revision")
        if (
            record is not None
            and canonical.get("effect_attempt_id") == contract.attempt_id
            and isinstance(event_revision, int)
            and record.revision > event_revision
            and record.status in {
                "probed_committed",
                "safe_to_retry",
                "compensated",
                "human_resolved",
            }
        ):
            continue
        # Admission may reject an effect belonging to an earlier attempt before
        # this run writes any ledger record. Its durable event still blocks
        # success; an empty current ledger is not proof of settlement.
        settled.discard(effect_id)
        unsettled.add(effect_id)
    return sorted(settled), sorted(unsettled)


async def finalize_deliverable(
    *,
    state_workspace_dir: str | Path,
    effective_workspace_dir: str | Path,
    works_workspace_dir: str | Path | None = None,
    run_id: str,
    chat_id: str,
    response_status: str,
    assistant_text: str,
    final_response_id: str | None,
    runtime_events: Iterable[Any] = (),
    primary_path: str | Path | None = None,
    title: str | None = None,
    kind: str | None = None,
    entrypoint: str | None = None,
    acceptance_evidence: Iterable[dict[str, object]] = (),
    verification_paths: Iterable[str | Path] = (),
    enforce_effect_settlement: bool = True,
    durable_artifact_required: bool | None = None,
) -> CompletionGateResult:
    """Validate and register a task result; never trusts a prose completion claim."""

    runtime_event_list = tuple(runtime_events)
    verification_path_items = tuple(verification_paths)
    state_root = Path(state_workspace_dir).expanduser()
    effective_root = Path(effective_workspace_dir).expanduser()
    works_root = Path(works_workspace_dir or state_root).expanduser()
    contract = await RuntimeRunStore(state_root).load_contract(run_id)
    if contract is None:
        evidence = RuntimeCompletionEvidence(final_response_id=final_response_id)
        return CompletionGateResult(
            False,
            "not_delivered",
            evidence,
            (_result("runtime_contract", False, "RuntimeRun contract is missing."),),
            failure_summary="RuntimeRun contract is missing.",
        )

    declared_deliverable = (
        contract.task_contract.deliverable_contract
        if contract.task_contract is not None
        else None
    )
    if declared_deliverable is not None and durable_artifact_required is None:
        durable_artifact_required = True

    checks = _reported_acceptance_results(acceptance_evidence)
    checks.append(
        _result(
            "terminal_response",
            response_status == "completed" and bool(final_response_id),
            "A terminal response event exists."
            if response_status == "completed" and final_response_id
            else "No successful terminal response event exists.",
        )
    )

    collaboration_required = bool(
        contract.task_contract is not None
        and contract.task_contract.work_method_id == "collaboration"
    )
    if collaboration_required:
        collaboration_closed = _closed_hosted_collaboration(runtime_event_list)
        checks.append(
            _result(
                "hosted_collaboration_closed",
                collaboration_closed,
                (
                    "A hosted collaboration room with at least two terminal "
                    "members closed."
                    if collaboration_closed
                    else "The collaboration contract has no closed hosted-room evidence."
                ),
            )
        )

    try:
        settled_effect_ids, unsettled_effect_ids = _latest_effects(
            state_root, contract, runtime_event_list
        )
    except Exception as exc:
        settled_effect_ids, unsettled_effect_ids = [], ["effect_journal_error"]
        checks.append(
            _result(
                "effects_settled",
                False,
                (
                    f"Effect journal error: {exc}"
                    if enforce_effect_settlement
                    else (
                        "Effect settlement is advisory for this run; the effect "
                        f"journal could not be inspected: {exc}"
                    )
                ),
                required=enforce_effect_settlement,
                outcome="error",
            )
        )
    else:
        if not unsettled_effect_ids:
            effect_settlement_detail = "All started effects are settled."
        elif enforce_effect_settlement:
            effect_settlement_detail = (
                f"Unsettled effects: {', '.join(unsettled_effect_ids)}"
            )
        else:
            effect_settlement_detail = (
                "Effect settlement is advisory for this run; pending effects are "
                f"recorded for recovery: {', '.join(unsettled_effect_ids)}"
            )
        checks.append(
            _result(
                "effects_settled",
                not unsettled_effect_ids,
                effect_settlement_detail,
                required=enforce_effect_settlement,
            )
        )

    primary_artifact_effect: RuntimeArtifactEffectRecord | None = None
    artifact_effect_journal_failed = False
    if primary_path is None:
        try:
            primary_artifact_effect = _committed_primary_artifact_effect(
                state_root,
                contract,
            )
        except Exception as exc:
            artifact_effect_journal_failed = True
            checks.append(
                _result(
                    "artifact_effect_journal",
                    False,
                    f"Artifact effect journal error: {exc}",
                    outcome="error",
                )
            )
        if primary_artifact_effect is not None:
            primary_path = effective_root / primary_artifact_effect.relative_path
        elif not artifact_effect_journal_failed:
            # Explicit compatibility boundary for runs created before S6.
            primary_path = _finalized_primary_path(runtime_event_list)

    # ``primary_path`` is authoritative artifact evidence. Separately, the
    # model-owned turn-intelligence result may require a durable artifact even
    # when the model failed before declaring one. The caller passes that typed
    # signal explicitly; this gate never infers mandatory file mode from a
    # casual mention of PPT, PDF, code, or documents.
    if primary_path is None and durable_artifact_required is True:
        artifact_claim_without_evidence = _unverified_artifact_completion_claim(
            contract,
            assistant_text,
            durable_artifact_required=durable_artifact_required,
        )
        detail = (
            "The response claims a requested durable artifact is complete, "
            "but no successful finalize_deliverable evidence exists."
            if artifact_claim_without_evidence
            else "A durable artifact was requested, but no successful "
            "finalize_deliverable evidence exists."
        )
        checks.append(_result("artifact_completion_evidence", False, detail))
        checks.append(
            _result(
                "visible_response",
                bool(assistant_text.strip()),
                "Visible assistant response exists."
                if assistant_text.strip()
                else "The response contains no visible answer.",
            )
        )
        evidence = RuntimeCompletionEvidence(
            settled_effect_ids=tuple(settled_effect_ids),
            acceptance_results=tuple(checks),
            final_response_id=final_response_id,
            terminal_event_id=final_response_id,
        )
        return CompletionGateResult(
            False,
            "not_delivered",
            evidence,
            tuple(checks),
            failure_summary=detail,
        )

    artifact_required = primary_path is not None
    if not artifact_required:
        artifact_claim_without_evidence = _unverified_artifact_completion_claim(
            contract,
            assistant_text,
            durable_artifact_required=durable_artifact_required,
        )
        if artifact_claim_without_evidence:
            checks.append(
                _result(
                    "artifact_completion_evidence",
                    False,
                    "The response claims a requested durable artifact is complete, "
                    "but no successful finalize_deliverable evidence exists.",
                )
            )
        checks.append(
            _result(
                "visible_response",
                bool(assistant_text.strip()),
                "Visible assistant response exists."
                if assistant_text.strip()
                else "The response contains no visible answer.",
            )
        )
        required_failures = [
            row for row in checks if row.get("required", True) and not row.get("passed")
        ]
        evidence = RuntimeCompletionEvidence(
            settled_effect_ids=tuple(settled_effect_ids),
            acceptance_results=tuple(checks),
            final_response_id=final_response_id,
            terminal_event_id=final_response_id,
        )
        return CompletionGateResult(
            not required_failures,
            "verified" if not required_failures else "not_delivered",
            evidence,
            tuple(checks),
            failure_summary=(
                None
                if not required_failures
                else str(required_failures[0].get("detail") or "Completion gate failed.")
            ),
        )

    assert primary_path is not None
    if declared_deliverable is not None:
        attempted_extension = Path(primary_path).suffix.lower()
        accepted_extensions = declared_deliverable.accepted_extensions
        checks.append(
            _result(
                "deliverable_extension",
                attempted_extension in accepted_extensions,
                (
                    f"Deliverable extension matches contract: {attempted_extension}"
                    if attempted_extension in accepted_extensions
                    else "Deliverable contract requires "
                    f"{', '.join(accepted_extensions)}, not "
                    f"{attempted_extension or 'an extensionless file'}."
                ),
            )
        )
    primary_sha256: str | None = None
    primary_hash_attempted = False
    artifact_allowed_root = effective_root
    try:
        if (
            primary_artifact_effect is not None
            and primary_artifact_effect.research_export_id is not None
        ):
            # A built-in research export is Agent-library scoped. Its durable
            # receipt must resolve under the current Agent and still match the
            # exported bytes before granting access to this exact artifact.
            managed_path = resolve_artifact_effect_path(
                primary_artifact_effect, effective_root
            )
            if managed_path is None:
                raise ValueError("Research export receipt is missing or invalid for the current Agent")
            primary_path = Path(managed_path)
            artifact_allowed_root = primary_path.parent
        artifact_path = _validate_explicit_path(primary_path, artifact_allowed_root)
    except (PermissionError, ValueError) as exc:
        artifact_path = None
        checks.append(_result("primary_artifact", False, str(exc)))
    except OSError as exc:
        artifact_path = None
        checks.append(
            _result(
                "primary_artifact",
                False,
                str(exc),
                outcome="error",
            )
        )
    else:
        checks.append(
            _result(
                "primary_artifact",
                artifact_path is not None,
                f"Primary artifact exists: {artifact_path}"
                if artifact_path is not None
                else "No primary artifact created by this run was found.",
            )
        )
        if primary_artifact_effect is not None:
            primary_hash_attempted = True
            try:
                actual_sha256 = _sha256_file(artifact_path)
            except OSError as exc:
                checks.append(
                    _result(
                        "artifact_content_identity",
                        False,
                        f"Primary artifact could not be hashed: {exc}",
                        outcome="error",
                    )
                )
            else:
                primary_sha256 = actual_sha256
                checks.append(
                    _result(
                        "artifact_content_identity",
                        actual_sha256 == primary_artifact_effect.sha256,
                        (
                            "Primary artifact matches committed EffectRecord content."
                            if actual_sha256 == primary_artifact_effect.sha256
                            else "Primary artifact content no longer matches its committed EffectRecord."
                        ),
                    )
                )

    artifact_text = ""
    visual_evidence_receipts: tuple[dict[str, object], ...] = ()
    visual_verification_scope: _VisualVerificationScope | None = None
    if artifact_path is not None:
        if declared_deliverable is not None and declared_deliverable.must_open:
            checks.append(_declared_file_openability_check(artifact_path))
        if (
            declared_deliverable is not None
            and declared_deliverable.visual_verification_required
        ):
            if primary_sha256 is None and not primary_hash_attempted:
                primary_hash_attempted = True
                try:
                    primary_sha256 = _sha256_file(artifact_path)
                except OSError as exc:
                    checks.append(
                        _result(
                            "primary_artifact_hash",
                            False,
                            f"Primary artifact could not be hashed: {exc}",
                            outcome="error",
                        )
                    )
            visual_evidence_root = effective_root
            (
                attested_paths,
                attestation_check,
                attested_scope,
                attested_root,
            ) = _recover_system_visual_attestation(
                runtime_event_list,
                state_root=state_root,
                effective_root=effective_root,
                contract=contract,
                artifact_path=artifact_path,
            )
            checks.append(attestation_check)
            if attested_scope is not None:
                verification_path_items = attested_paths
                visual_verification_scope = attested_scope
                visual_evidence_root = attested_root
            elif (
                not verification_path_items
                and primary_artifact_effect is not None
                and primary_artifact_effect.artifact_role == "primary"
            ):
                (
                    recovered_paths,
                    identity_check,
                    recovered_scope,
                ) = _recover_bound_visual_evidence(
                    runtime_event_list,
                    state_root=state_root,
                    effective_root=effective_root,
                    contract=contract,
                    primary=primary_artifact_effect,
                )
                checks.append(identity_check)
                verification_path_items = recovered_paths
                visual_verification_scope = recovered_scope
            visual_checks, visual_evidence_receipts = (
                _visual_verification_observation(
                    verification_path_items,
                    visual_evidence_root,
                    verification_scope=visual_verification_scope,
                )
            )
            checks.extend(visual_checks)
        if artifact_path.suffix.lower() in {".html", ".htm"}:
            checks.extend(_html_checks(artifact_path))
        if artifact_path.suffix.lower() in {".html", ".htm", ".md", ".txt"}:
            try:
                artifact_text = artifact_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )[:1_000_000]
            except OSError:
                artifact_text = ""

    required_text = "\n".join(
        (
            contract.task_contract.objective if contract.task_contract else "",
            *(contract.task_contract.acceptance if contract.task_contract else ()),
        )
    )
    if _TEST_REQUEST_RE.search(required_text):
        real_test = _contains_real_assertion(effective_root) and _has_successful_test_execution(
            runtime_event_list,
        )
        checks.append(
            _result(
                "real_test_execution",
                real_test,
                "A test containing assertions executed with exit code 0."
                if real_test
                else "No executed assertion-based test with exit code 0 was found; print-only scripts do not count.",
            )
        )

    contradictions = _capability_contradictions(
        contract,
        artifact_text,
        assistant_text,
    )
    checks.append(
        _result(
            "spec_implementation_consistency",
            not contradictions,
            "No explicit implementation contradiction was found."
            if not contradictions
            else f"Promised but explicitly unsupported: {', '.join(contradictions)}",
        )
    )

    required_failures = [
        row for row in checks if row.get("required", True) and not row.get("passed")
    ]
    candidate_work_item: WorkItem | None = None
    if artifact_path is not None:
        # ``add_work`` owns digest-aware idempotency.  Looking up by Run and
        # path here would bind newly written bytes to an older immutable Work.
        candidate_work_item = add_work(
            works_root,
            source_path=artifact_path,
            allowed_roots=[artifact_allowed_root],
            title=title or contract.task_contract.objective[:120],
            source_chat_id=chat_id,
            source_run_id=run_id,
        )
        checks.append(
            _result(
                "works_registration",
                True,
                f"Works candidate retained: {candidate_work_item.id}",
            )
        )
    else:
        checks.append(
            _result(
                "works_registration",
                False,
                "No primary artifact was available for Works registration.",
            )
        )

    required_failures = [
        row for row in checks if row.get("required", True) and not row.get("passed")
    ]
    work_item = candidate_work_item if not required_failures else None
    verified = not required_failures and work_item is not None
    delivery_status: RuntimeDeliveryStatus
    if verified:
        delivery_status = "verified"
    elif artifact_path is not None:
        delivery_status = "partial"
    else:
        delivery_status = "not_delivered"
    evidence = RuntimeCompletionEvidence(
        terminal_event_id=final_response_id,
        settled_effect_ids=tuple(settled_effect_ids),
        acceptance_results=tuple(checks),
        primary_artifact_ids=(
            (candidate_work_item.artifact_id,) if candidate_work_item else ()
        ),
        works_item_ids=(work_item.id,) if work_item else (),
        validation_work_item_ids=(
            (candidate_work_item.id,) if candidate_work_item else ()
        ),
        validation=_validation_metadata(checks) if candidate_work_item else None,
        final_response_id=final_response_id,
    )
    verification_receipt = None
    if (
        verified
        and artifact_path is not None
        and primary_sha256 is not None
        and declared_deliverable is not None
        and declared_deliverable.visual_verification_required
        and visual_evidence_receipts
        and visual_verification_scope is not None
    ):
        verification_receipt = {
            "schema_version": 2,
            "run_id": contract.run_id,
            "attempt_id": contract.attempt_id,
            "primary_path": (
                primary_artifact_effect.relative_path
                if primary_artifact_effect is not None
                and primary_artifact_effect.research_export_id is not None
                else artifact_path.relative_to(
                    effective_root.resolve(strict=False)
                ).as_posix()
            ),
            "primary_sha256": primary_sha256,
            "visual_evidence": list(visual_evidence_receipts),
            "verification_scope": visual_verification_scope.to_receipt(),
        }
    return CompletionGateResult(
        verified,
        delivery_status,
        evidence,
        tuple(checks),
        primary_path=str(artifact_path) if artifact_path is not None else None,
        work_item=work_item,
        candidate_work_item=candidate_work_item,
        failure_summary=(
            None
            if verified
            else str(required_failures[0].get("detail") or "Completion gate failed.")
            if required_failures
            else "Completion gate failed."
        ),
        verification_receipt=verification_receipt,
    )


__all__ = ["CompletionGateResult", "finalize_deliverable"]
