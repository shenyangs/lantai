#!/usr/bin/env python3
"""Run the production presentation pipeline with an isolated configured provider.

The user's encrypted provider store and the production job state are copied to
a private temporary directory and removed after the child exits.  A passing
PPTX may be exported to the requested output directory.  When an explicit
external recovery root is supplied, only the failed job state is retained
there for an exact retry; the staged provider store is still removed.  Saved
completed jobs may likewise be retained in an explicit private root for native
Office round trips; the default still removes successful job state.  Saved
evidence contains only route identities, hashes, counts, timings, budget data,
and the exported PPTX path -- never credentials, prompts, model output, or
captured diagnostics.  A retained job can be resumed in place after a reviewed
implementation fix; this reuses its original owner, budget, history, and
checkpoints while restaging the encrypted provider profile only for the child.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
CONFIGURED_PROVIDER_SMOKE_PATH = (
    PROJECT_ROOT / "scripts" / "source_configured_provider_stream_smoke.py"
)
DEFAULT_IMPLEMENTATION_RECOVERY_BASIS = (
    "d01_r03_before_after_cardinality.v1"
)
DEFAULT_IMPLEMENTATION_FIX_IDENTITY = (
    "semantic_before_after_cardinality.v1"
)
DEFAULT_IMPLEMENTATION_FAILURE_CATEGORY = "content"
DEFAULT_IMPLEMENTATION_RECOVERY_ACTION = "revise_content"
DEFAULT_IMPLEMENTATION_ISSUE_CODE = "content.before_after_cardinality"
DEFAULT_IMPLEMENTATION_RETRY_STAGE = "plan"
_LAST_PROVIDER_MEASUREMENT: dict[str, Any] | None = None


@lru_cache(maxsize=1)
def _load_provider_measurement():
    spec = importlib.util.spec_from_file_location(
        "presentation_provider_measurement",
        PROJECT_ROOT / "scripts" / "presentation_provider_measurement.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load provider measurement helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _measure_provider_invocations():
    global _LAST_PROVIDER_MEASUREMENT
    from lantai.token_usage import TokenRecordingModelWrapper

    with _load_provider_measurement().ProviderMeasurement(
        TokenRecordingModelWrapper,
    ) as observer:
        try:
            yield
        finally:
            _LAST_PROVIDER_MEASUREMENT = observer.summary()


def _include_provider_measurement(
    evidence: dict[str, Any],
    child: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(child.get("provider_measurement"), dict):
        evidence[
            "provider_measurement"
        ] = _load_provider_measurement().sanitize_report(
            child["provider_measurement"],
        )
    return evidence


def _load_configured_provider_smoke():
    spec = importlib.util.spec_from_file_location(
        "source_configured_provider_stream_smoke",
        CONFIGURED_PROVIDER_SMOKE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load configured-provider smoke helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_summary(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    return {
        "name": resolved.name,
        "sha256": _sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _material_summaries(
    request_path: Path,
    workspace_root: Path,
) -> list[dict[str, Any]]:
    request = _read_json(request_path.expanduser().resolve())
    root = workspace_root.expanduser().resolve()
    summaries: list[dict[str, Any]] = []
    materials = request.get("materials")
    if not isinstance(materials, list):
        return summaries
    for material in materials:
        if not isinstance(material, dict):
            continue
        raw_path = str(material.get("path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"presentation material must stay inside workspace: {path.name}",
            ) from exc
        if not path.is_file():
            raise FileNotFoundError(f"presentation material is unavailable: {path.name}")
        summaries.append(_file_summary(path))
    return summaries


def _provider_cost_evidence(value: dict[str, Any]) -> tuple[str, int | None]:
    """Keep an unknown bill distinct from an explicitly zero-priced call."""

    status = value.get("provider_cost_status")
    amount = value.get("provider_cost_micro_cny")
    if (
        status == "exact"
        and isinstance(amount, int)
        and not isinstance(amount, bool)
        and amount >= 0
    ):
        return "exact", amount
    if status == "not_applicable" and amount is None:
        return "not_applicable", None
    return "unknown", None


def _safe_numeric_tree(value: object) -> object:
    """Retain names plus numeric/bool/null metrics while dropping free text."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            safe = _safe_numeric_tree(item)
            if safe is not None or item is None:
                result[str(key)] = safe
        if "provider_cost_status" in value or "provider_cost_micro_cny" in value:
            status, amount = _provider_cost_evidence(value)
            result["provider_cost_status"] = status
            result["provider_cost_micro_cny"] = amount
        return result
    return None


def _summarize_stage_metrics(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        safe = _safe_numeric_tree(value)
        return safe if isinstance(safe, dict) else {}
    if not isinstance(value, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "unknown")
        if not stage.replace("_", "").replace("-", "").isalnum():
            stage = "unknown"
        bucket = result.setdefault(
            stage,
            {
                "call_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "duration_ms": 0.0,
                "cost_units": 0,
                "provider_cost_status": "not_applicable",
                "provider_cost_micro_cny": None,
                "provider_cost_known_micro_cny": 0,
                "provider_cost_exact_call_count": 0,
                "provider_cost_unknown_call_count": 0,
                "provider_cost_not_applicable_call_count": 0,
            },
        )
        bucket["call_count"] += 1
        outcome = str(item.get("outcome") or "")
        if outcome == "passed":
            bucket["passed_count"] += 1
        elif outcome == "failed":
            bucket["failed_count"] += 1
        duration = item.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            bucket["duration_ms"] += float(duration)
        cost = item.get("cost_units")
        if isinstance(cost, int) and not isinstance(cost, bool):
            bucket["cost_units"] += cost
        status, amount = _provider_cost_evidence(item)
        bucket[f"provider_cost_{status}_call_count"] += 1
        if amount is not None:
            bucket["provider_cost_known_micro_cny"] += amount
    for bucket in result.values():
        bucket["duration_ms"] = round(float(bucket["duration_ms"]), 3)
        if bucket["provider_cost_unknown_call_count"]:
            bucket["provider_cost_status"] = "unknown"
        elif bucket["provider_cost_exact_call_count"]:
            bucket["provider_cost_status"] = "exact"
            bucket["provider_cost_micro_cny"] = bucket["provider_cost_known_micro_cny"]
    return result


def _quality_summary(path: Path) -> dict[str, Any]:
    quality = _read_json(path)
    issues = quality.get("issues")
    visual_issues = quality.get("visual_issues")
    return {
        "passed": bool(quality.get("passed")),
        "delivery_status": str(quality.get("delivery_status") or ""),
        "render_verified": bool(quality.get("render_verified")),
        "visual_verified": bool(quality.get("visual_verified")),
        "editable": bool(quality.get("editable")),
        "rendered_slide_count": int(quality.get("rendered_slide_count") or 0),
        "repair_rounds": int(quality.get("repair_rounds") or 0),
        "issue_count": len(issues) if isinstance(issues, list) else 0,
        "visual_issue_count": (
            len(visual_issues) if isinstance(visual_issues, list) else 0
        ),
        "renderer_id": str(quality.get("renderer_id") or ""),
        "renderer_version": str(quality.get("renderer_version") or ""),
        "visual_critic_id_sha256": (
            _sha256_bytes(str(quality.get("visual_critic_id")).encode("utf-8"))
            if quality.get("visual_critic_id")
            else None
        ),
    }


def _deck_summary(path: Path) -> dict[str, Any]:
    deck = _read_json(path)
    slides = deck.get("slides")
    if not isinstance(slides, list):
        slides = []
    object_types: Counter[str] = Counter()
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        objects = slide.get("objects")
        if not isinstance(objects, list):
            continue
        for item in objects:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "unknown")
            if kind.replace("_", "").replace("-", "").isalnum():
                object_types[kind] += 1
    return {
        "slide_count": len(slides),
        "object_count": sum(object_types.values()),
        "object_type_counts": dict(sorted(object_types.items())),
    }


def _job_summary(path: Path) -> dict[str, Any]:
    job = _read_json(path)
    return {
        "status": str(job.get("status") or ""),
        "stage": str(job.get("stage") or ""),
        "generation": int(job.get("generation") or 0),
        "attempt_count": int(job.get("attempt_count") or 0),
        "recovery_attempt_count": int(job.get("recovery_attempt_count") or 0),
        "external_recovery_attempt_count": int(
            job.get("external_recovery_attempt_count") or 0
        ),
        "implementation_recovery_attempt_count": int(
            job.get("implementation_recovery_attempt_count") or 0
        ),
        "cost_budget_units": int(job.get("cost_budget_units") or 0),
        "cost_used_units": int(job.get("cost_used_units") or 0),
        "stage_metrics": _summarize_stage_metrics(job.get("stage_metrics")),
    }


_SAFE_FAILURE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_FAILURE_CATEGORIES = {
    "content",
    "geometry",
    "aesthetic",
    "file",
    "model_service",
    "authorization",
    "unknown",
}
_SAFE_RECOVERY_ACTIONS = {
    "revise_content",
    "patch_geometry",
    "redesign_slide",
    "retry_file_stage",
    "retry_model_service",
    "request_authorization",
    "inspect_failure",
}
_SAFE_JOB_STAGES = {
    "created",
    "brief",
    "plan",
    "compose",
    "render",
    "quality",
    "repair",
    "finalize",
    "completed",
    "failed",
}
_SAFE_JOB_STATUSES = {
    "pending",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
    "passed",
}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _safe_failure_token(value: object) -> str:
    return value if isinstance(value, str) and _SAFE_FAILURE_TOKEN.fullmatch(value) else ""


def _safe_sha256(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.lower()
    return normalized if _SHA256_HEX.fullmatch(normalized) else ""


def _safe_failure_route(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    category = str(value.get("category") or "")
    action = str(value.get("recovery_action") or "")
    if category not in _SAFE_FAILURE_CATEGORIES:
        category = "unknown"
    if action not in _SAFE_RECOVERY_ACTIONS:
        action = "inspect_failure"
    retry_stage = str(value.get("retry_stage") or "")
    if retry_stage not in _SAFE_JOB_STAGES:
        retry_stage = ""
    issue_codes = value.get("issue_codes")
    safe_issue_codes = [
        str(code)
        for code in issue_codes
        if isinstance(code, str) and _SAFE_FAILURE_TOKEN.fullmatch(code)
    ] if isinstance(issue_codes, list) else []
    return {
        "category": category,
        "recovery_action": action,
        "issue_codes": safe_issue_codes,
        "retry_stage": retry_stage,
        "automatic_retry_allowed": bool(value.get("automatic_retry_allowed")),
        "requires_human": bool(value.get("requires_human")),
    }


def _failed_job_summary(path: Path) -> dict[str, Any]:
    """Hash private failure prose while retaining its typed recovery route."""

    job = _read_json(path)
    events = job.get("events")
    if not isinstance(events, list):
        return {}
    failed_event = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, dict)
            and (
                str(event.get("status") or "") == "failed"
                or str(event.get("stage") or "") == "failed"
            )
        ),
        None,
    )
    if failed_event is None:
        return {}
    routes = failed_event.get("failure_routes")
    safe_routes = [
        safe
        for route in routes
        if (safe := _safe_failure_route(route)) is not None
    ] if isinstance(routes, list) else []
    error_code = next(
        (
            code
            for route in safe_routes
            for code in route["issue_codes"]
        ),
        "presentation_job_failed",
    )
    message = str(failed_event.get("message") or "")
    failure_stage = str(failed_event.get("stage") or "")
    if failure_stage not in _SAFE_JOB_STAGES:
        failure_stage = "failed"
    sequence = failed_event.get("sequence")
    return {
        "error_type": "PresentationJobFailed",
        "error_code": error_code,
        "error_sha256": _sha256_bytes(message.encode("utf-8")),
        "failure_stage": failure_stage,
        "event_sequence": (
            int(sequence)
            if isinstance(sequence, int) and not isinstance(sequence, bool)
            else 0
        ),
        "recoverable": bool(failed_event.get("recoverable")),
        "failure_routes": safe_routes,
    }


def _external_recovery_route_payloads(record: Any) -> list[dict[str, Any]]:
    """Build reviewed model-service routes from the latest durable failure.

    The external-recovery lane is intentionally separate from ordinary retry
    attempts.  It may be used only after an independently verified Provider
    health check, and never converts an authorization failure into a retry.
    """

    failed_event = next(
        (
            event
            for event in reversed(list(getattr(record, "events", [])))
            if getattr(getattr(event, "status", None), "value", None) == "failed"
        ),
        None,
    )
    routes = list(getattr(failed_event, "failure_routes", []) or [])
    if not routes:
        raise ValueError("external recovery requires a durable failure route")
    payloads: list[dict[str, Any]] = []
    for route in routes:
        category = getattr(getattr(route, "category", None), "value", None)
        action = getattr(
            getattr(route, "recovery_action", None),
            "value",
            None,
        )
        retry_stage = getattr(
            getattr(route, "retry_stage", None),
            "value",
            None,
        )
        issue_codes = [
            str(code)
            for code in list(getattr(route, "issue_codes", []) or [])
            if isinstance(code, str) and _SAFE_FAILURE_TOKEN.fullmatch(code)
        ]
        if (
            category != "model_service"
            or action != "retry_model_service"
            or retry_stage not in _SAFE_JOB_STAGES
            or not issue_codes
        ):
            raise ValueError(
                "external recovery is limited to reviewed model-service failures",
            )
        payloads.append({
            "category": category,
            "recovery_action": action,
            "issue_codes": issue_codes,
            "retry_stage": retry_stage,
            "automatic_retry_allowed": True,
            "requires_human": False,
        })
    return payloads


def _normalize_external_recovery_max_attempts(value: int | None) -> int | None:
    """Bound reviewed smoke recovery, including one post-reset verification."""

    if value is None:
        return None
    if value < 1 or value > 4:
        raise ValueError(
            "smoke external recovery max attempts must be between 1 and 4",
        )
    return value


def _normalize_implementation_recovery_max_attempts(
    value: int | None,
) -> int | None:
    """Allow one explicitly audited smoke-only recovery beyond the product default."""

    if value is None:
        return None
    if value < 1 or value > 3:
        raise ValueError(
            "smoke implementation recovery max attempts must be between 1 and 3",
        )
    return value


def _version_summary(path: Path) -> dict[str, Any]:
    current = _read_json(path)
    return {
        "version_id": str(current.get("version_id") or ""),
        "pptx_sha256": str(current.get("pptx_sha256") or ""),
        "generation": int(current.get("generation") or 0),
        "reconciled": bool(current.get("reconciled")),
        "writer": str(current.get("writer") or ""),
    }


def _privacy_summary(
    *,
    durable_job_store_persisted: bool = False,
    private_job_store_removed: bool = True,
) -> dict[str, bool]:
    return {
        "credentials_persisted": False,
        "provider_store_staged_temporarily": True,
        "durable_job_store_persisted": durable_job_store_persisted,
        "private_job_store_removed": private_job_store_removed,
        "captured_stdout_persisted": False,
        "captured_stderr_persisted": False,
    }


def _finish_staged_profile(profile: Path, evidence: dict[str, Any]) -> None:
    """Fail closed on cleanup errors and report actual encrypted-store retention."""
    cleanup: dict[str, Any] = {"ok": True}
    try:
        shutil.rmtree(profile)
        if profile.exists():
            raise OSError("staged provider profile survived cleanup")
    except OSError as exc:
        cleanup = _safe_exception(exc)
    removed = not profile.exists()
    privacy = evidence.setdefault("privacy", _privacy_summary())
    privacy.update({
        "credentials_persisted": not removed,
        "plaintext_credentials_persisted": False,
        "provider_store_staged_temporarily": removed,
        "staged_provider_store_removed": removed,
    })
    if not removed and not privacy.get("durable_job_store_persisted"):
        privacy["private_job_store_removed"] = False
    evidence["cleanup"] = cleanup
    if not cleanup["ok"]:
        evidence["ok"] = False
        evidence["status"] = "failed"


def _write_safe_evidence(target: Path, evidence: dict[str, Any]) -> None:
    """Replace the target with a new inode; never truncate an input alias."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=".presentation-evidence-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(evidence, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_evidence_path(
    path: Path, *, protected_roots: list[Path], protected_files: list[Path],
) -> Path:
    """Reject evidence aliases before a Provider call and again before writing."""
    target = path.expanduser().resolve()
    if target in {item.expanduser().resolve() for item in protected_files} or any(
        _is_within(target, root.expanduser().resolve()) for root in protected_roots
    ):
        raise ValueError("evidence output collides with protected presentation data")
    if target.suffix.lower() != ".json":
        raise ValueError("evidence output must be a JSON file")
    if target.exists() and target.stat().st_nlink != 1:
        raise ValueError("evidence output must not be a hard-linked input alias")
    return target


def _validate_generation_evidence_target(path: Path, args: argparse.Namespace) -> Path:
    files = [args.request]
    materials = _read_json(args.request).get("materials")
    for material in materials if isinstance(materials, list) else []:
        if not isinstance(material, dict) or not isinstance(material.get("path"), str):
            continue
        source = Path(material["path"]).expanduser()
        files.append(source if source.is_absolute() else args.workspace_root / source)
    roots = [value for value in (
        args.source_working_dir, args.source_secret_dir, args.job_root, args.output_dir,
        args.resume_job_root, args.failure_recovery_root,
        getattr(args, "completed_job_retention_root", None),
    ) if value is not None]
    return _validate_evidence_path(path, protected_roots=roots, protected_files=files)


def _validate_resume_target(
    *,
    resume_job_root: Path,
    resume_job_id: str,
    workspace_root: Path,
) -> Path:
    """Resolve one retained job without ever treating its provider store as durable."""

    if not _SAFE_FAILURE_TOKEN.fullmatch(resume_job_id):
        raise ValueError("presentation resume job id is invalid")
    resolved_root = resume_job_root.expanduser().resolve()
    protected_roots = {PROJECT_ROOT.resolve(), workspace_root.expanduser().resolve()}
    if any(_is_within(resolved_root, root) for root in protected_roots):
        raise ValueError("presentation resume job root must stay outside the workspace")
    job_dir = resolved_root / resume_job_id
    if not job_dir.is_dir() or not (job_dir / "manifest.json").is_file():
        raise FileNotFoundError("presentation resume job is unavailable")
    return resolved_root


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _preserve_private_job(
    *,
    private_job_root: Path,
    child: dict[str, Any],
    recovery_root: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    """Move only one explicitly selected job outside the repository."""

    job_id = str(child.get("job_id") or "")
    if not _SAFE_FAILURE_TOKEN.fullmatch(job_id):
        return {"retained": False, "job_id": "", "checkpoint_count": 0}
    source = private_job_root.resolve() / job_id
    if not source.is_dir():
        return {"retained": False, "job_id": job_id, "checkpoint_count": 0}
    destination_root = recovery_root.expanduser().resolve()
    protected_roots = {PROJECT_ROOT.resolve(), workspace_root.resolve()}
    if any(_is_within(destination_root, root) for root in protected_roots):
        raise ValueError(
            "presentation private retention root must stay outside the workspace",
        )
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination_root.chmod(0o700)
    destination = destination_root / job_id
    if destination.exists():
        raise FileExistsError(
            "presentation private retention job already exists",
        )
    checkpoint_root = source / "checkpoints"
    checkpoint_count = (
        sum(1 for path in checkpoint_root.rglob("*") if path.is_file())
        if checkpoint_root.is_dir()
        else 0
    )
    shutil.move(str(source), str(destination))
    destination.chmod(0o700)
    return {
        "retained": True,
        "job_id": job_id,
        "checkpoint_count": checkpoint_count,
    }


def _export_passing_pptx(child: dict[str, Any], output_dir: Path) -> Path:
    source = Path(str(child.get("output_path") or "")).resolve()
    if not source.is_file() or source.suffix.lower() != ".pptx":
        raise FileNotFoundError("passing presentation output is unavailable")
    destination_root = output_dir.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / "presentation.pptx"
    shutil.copy2(source, destination)
    return destination


def _build_safe_evidence(
    *,
    source_head: str,
    provider_id: str,
    model: str,
    request_summary: dict[str, Any],
    material_summaries: list[dict[str, Any]],
    elapsed_ms: int,
    child: dict[str, Any],
    exported_output: Path,
    durable_job_store_persisted: bool = False,
) -> dict[str, Any]:
    quality_path = Path(str(child["quality_path"])).resolve()
    job_manifest_path = Path(str(child["job_manifest_path"])).resolve()
    deck_ir_path = Path(str(child["deck_ir_path"])).resolve()
    current_version_path = Path(str(child["current_version_path"])).resolve()
    output_summary = _file_summary(exported_output)
    output_summary["path"] = str(exported_output.resolve())
    output_summary["media_type"] = (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    evidence = {
        "schema_version": "1.0",
        "run_kind": "full-production-presentation",
        "ok": bool(child.get("ok") and child.get("status") == "passed"),
        "source_head": source_head,
        "provider": {
            "provider_id": provider_id,
            "model": model,
            "route": f"{provider_id}/{model}",
            "selectable_before_call": True,
        },
        "billing": {
            "basis": "user_subscription",
            "per_run_monetary_cost": "unavailable",
        },
        "input": {
            "request": request_summary,
            "materials": material_summaries,
        },
        "timing_ms": {"wall_clock": elapsed_ms},
        "job_id": str(child.get("job_id") or ""),
        "job": _job_summary(job_manifest_path),
        "quality": _quality_summary(quality_path),
        "deck": _deck_summary(deck_ir_path),
        "version": _version_summary(current_version_path),
        "output": output_summary,
        "privacy": _privacy_summary(
            durable_job_store_persisted=durable_job_store_persisted,
            private_job_store_removed=not durable_job_store_persisted,
        ),
    }
    return _include_provider_measurement(evidence, child)


def _safe_child_failure(
    *,
    source_head: str,
    provider_id: str,
    model: str,
    request_summary: dict[str, Any],
    material_summaries: list[dict[str, Any]],
    elapsed_ms: int,
    returncode: int,
    stdout: str,
    stderr: str,
    child: dict[str, Any],
    durable_job_store_persisted: bool = False,
) -> dict[str, Any]:
    failure_routes = child.get("failure_routes")
    safe_failure_routes = (
        [
            safe
            for route in failure_routes
            if (safe := _safe_failure_route(route)) is not None
        ]
        if isinstance(failure_routes, list)
        else []
    )
    status = str(child.get("status") or "")
    failure_stage = str(child.get("failure_stage") or "")
    event_sequence = child.get("event_sequence")
    evidence = {
        "schema_version": "1.0",
        "run_kind": "full-production-presentation",
        "ok": False,
        "source_head": source_head,
        "provider": {"route": f"{provider_id}/{model}"},
        "input": {
            "request": request_summary,
            "materials": material_summaries,
        },
        "timing_ms": {"wall_clock": elapsed_ms},
        "child": {
            "returncode": returncode,
            "error_type": _safe_failure_token(child.get("error_type")),
            "error_code": _safe_failure_token(child.get("error_code")),
            "error_sha256": _safe_sha256(child.get("error_sha256")),
            "error_origin": _safe_error_origin(child.get("error_origin")),
            "utility_timeout": (
                _safe_utility_timeout(child.get("utility_timeout"))
                if child.get("error_type") == "UtilityTaskTimeoutError" else None
            ),
            "schema_issues": (
                _safe_schema_issues(child.get("schema_issues"))
                if child.get("error_type") == "StructuredTaskValidationError" else []
            ),
            "job_id": _safe_failure_token(child.get("job_id")),
            "status": status if status in _SAFE_JOB_STATUSES else "",
            "failure_stage": failure_stage if failure_stage in _SAFE_JOB_STAGES else "",
            "event_sequence": (
                event_sequence
                if isinstance(event_sequence, int)
                and not isinstance(event_sequence, bool)
                and event_sequence >= 0
                else 0
            ),
            "recoverable": bool(child.get("recoverable")),
            "failure_routes": safe_failure_routes,
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stdout_sha256": _sha256_bytes(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
            "stderr_sha256": _sha256_bytes(stderr.encode("utf-8")),
        },
        "privacy": _privacy_summary(
            durable_job_store_persisted=durable_job_store_persisted,
            private_job_store_removed=not durable_job_store_persisted,
        ),
    }
    manifest_path = Path(str(child.get("job_manifest_path") or ""))
    if manifest_path.is_file():
        evidence["job"] = _job_summary(manifest_path)
    return _include_provider_measurement(evidence, child)


def _safe_error_origin(value: object) -> dict[str, Any] | None:
    """Keep code locations only when they name an actual local source function."""
    if not isinstance(value, dict):
        return None
    file, function, line = value.get("file"), value.get("function"), value.get("line")
    if not isinstance(file, str) or not isinstance(function, str) or not isinstance(line, int) or isinstance(line, bool):
        return None
    relative = Path(file)
    if relative.is_absolute() or relative.suffix != ".py" or ".." in relative.parts or not relative.parts or relative.parts[0] not in {"src", "scripts"}:
        return None
    source = (PROJECT_ROOT / relative).resolve()
    if not _is_within(source, PROJECT_ROOT.resolve()):
        return None
    try:
        code = source.read_text(encoding="utf-8")
        functions = {node.name for node in ast.walk(ast.parse(code)) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    except (OSError, SyntaxError, UnicodeError):
        return None
    if not 0 < line <= len(code.splitlines()) or function not in functions | {"<module>", "<listcomp>", "<dictcomp>", "<genexpr>", "<lambda>"}:
        return None
    return {"file": relative.as_posix(), "function": function, "line": line}


_SAFE_SCHEMA_ERROR_TYPES = frozenset({
    "reference_error", "extra_forbidden", "missing", "literal_error", "enum",
    "list_type", "dict_type", "model_type", "model_attributes_type",
    "string_type", "string_too_short", "string_too_long", "string_unicode",
    "string_pattern_mismatch", "int_type", "int_parsing", "int_from_float",
    "float_type", "float_parsing", "finite_number", "greater_than",
    "greater_than_equal", "less_than", "less_than_equal", "too_short",
    "too_long", "json_invalid", "json_type", "value_error", "assertion_error",
    "schema_error", "union_tag_invalid", "union_tag_not_found", "bool_type",
    "bool_parsing", "none_required",
})
_SCHEMA_PATH_COMPONENT = re.compile(
    r"\.([A-Za-z_][A-Za-z0-9_-]{0,79}|<redacted>)|\[([0-9]{1,6})\]",
)


@lru_cache(maxsize=1)
def _schema_owned_path_tokens() -> frozenset[str]:
    """Read only field/type/discriminator names from fixed local schema sources.

    Do not inspect rejected inputs or arbitrary exception messages. AST reads
    keep this privacy boundary usable by the standalone stdlib-only parent.
    """
    tokens: set[str] = set()
    for relative in ("src/lantai/presentation/models.py", "src/lantai/presentation/repair.py"):
        try:
            source = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        for node in ast.walk(source):
            if not isinstance(node, ast.ClassDef):
                continue
            tokens.add(node.name)
            for field in node.body:
                if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
                    continue
                tokens.add(field.target.id)
                for annotation in ast.walk(field.annotation):
                    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
                        tokens.add(annotation.value)
    return frozenset(token for token in tokens if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", token))


def _safe_schema_issues(value: object) -> list[dict[str, str]]:
    """Export bounded schema codes/locations; redact every model-owned key."""
    if not isinstance(value, (list, tuple)):
        return []
    owned = _schema_owned_path_tokens()
    safe: list[dict[str, str]] = []
    for item in value[:24]:
        if isinstance(item, str) and len(item) <= 600:
            code, separator, path = item.partition("@")
            if not separator:
                continue
        elif isinstance(item, dict):
            code, path = item.get("type"), item.get("path")
        else:
            continue
        if not isinstance(code, str) or code not in _SAFE_SCHEMA_ERROR_TYPES:
            continue
        if not isinstance(path, str) or len(path) > 512 or not path.startswith("$"):
            continue
        position, components, valid = 1, [], True
        while position < len(path):
            match = _SCHEMA_PATH_COMPONENT.match(path, position)
            if match is None or len(components) >= 32:
                valid = False
                break
            field, index = match.groups()
            if field is not None:
                components.append("." + (field if field in owned else "<redacted>"))
            elif int(index) <= 10000:
                components.append(f"[{int(index)}]")
            else:
                valid = False
                break
            position = match.end()
        if valid:
            issue = {"type": code, "path": "$" + "".join(components)}
            if issue not in safe:
                safe.append(issue)
    return safe


def _safe_utility_timeout(value: object) -> dict[str, Any] | None:
    """Retain only a known timeout phase and finite positive duration."""
    if not isinstance(value, dict):
        return None
    phase, seconds = value.get("phase"), value.get("timeout_seconds")
    if (not isinstance(phase, str) or phase not in {"first_output", "idle", "total"}
            or not isinstance(seconds, (int, float)) or isinstance(seconds, bool)):
        return None
    try:
        duration = float(seconds)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return {"phase": phase, "timeout_seconds": duration}


def _safe_exception(exc: BaseException) -> dict[str, Any]:
    value = f"{type(exc).__name__}:{exc}"
    origin: dict[str, Any] | None = None
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            relative = Path(frame.filename).resolve().relative_to(PROJECT_ROOT)
        except (OSError, ValueError):
            continue
        if relative.parts and relative.parts[0] in {"src", "scripts"}:
            origin = {
                "file": relative.as_posix(),
                "function": frame.name,
                "line": frame.lineno,
            }
    evidence = {
        "ok": False,
        "error_type": type(exc).__name__,
        "error_sha256": _sha256_bytes(value.encode("utf-8")),
        "error_origin": origin,
        "schema_issues": (
            _safe_schema_issues(getattr(exc, "validation_issues", ()))
            if type(exc).__name__ == "StructuredTaskValidationError"
            else []
        ),
    }
    if type(exc).__name__ == "UtilityTaskTimeoutError":
        from lantai.app.utility_model import UtilityTaskTimeoutError

        if isinstance(exc, UtilityTaskTimeoutError):
            timeout = _safe_utility_timeout({
                "phase": exc.phase, "timeout_seconds": exc.timeout_seconds,
            })
            if timeout is not None:
                evidence["utility_timeout"] = timeout

    return _include_provider_measurement(
        evidence,
        {"provider_measurement": _LAST_PROVIDER_MEASUREMENT},
    )


def _inner_run(
    *,
    request_path: Path,
    job_root: Path,
    output_dir: Path,
    workspace_root: Path,
    agent_id: str,
    expected_provider_id: str,
    expected_model: str,
) -> dict[str, Any]:
    global _LAST_PROVIDER_MEASUREMENT
    _LAST_PROVIDER_MEASUREMENT = None
    from lantai.config.config import load_agent_config
    from lantai.presentation.cli import generate
    from lantai.providers.provider_manager import ProviderManager

    manager = ProviderManager.get_instance()
    active = (
        load_agent_config(agent_id).active_model or manager.get_active_model()
    )
    if active is None or not manager.is_chat_model_slot_selectable(active):
        raise RuntimeError("configured chat model is not selectable")
    actual = (active.provider_id, active.model)
    expected = (expected_provider_id, expected_model)
    if actual != expected:
        raise RuntimeError("configured provider route does not match request")

    with _measure_provider_invocations():
        result = generate(
            request_path=request_path,
            job_root=job_root,
            output_dir=output_dir,
            workspace_root=workspace_root,
        )
    job_id = str(result.get("job_id") or "")
    durable_job = job_root.resolve() / job_id
    inner_result = {
        "ok": result.get("status") == "passed",
        "job_id": job_id,
        "status": str(result.get("status") or ""),
        "output_path": str(result.get("output_path") or ""),
        "quality_path": str(result.get("qa_report") or ""),
        "delivery_manifest_path": str(result.get("manifest") or ""),
        "job_manifest_path": str(durable_job / "manifest.json"),
        "deck_ir_path": str(durable_job / "checkpoints" / "deck-ir.json"),
        "current_version_path": str(durable_job / "versions" / "current.json"),
    }
    if not inner_result["ok"]:
        inner_result.update(
            _failed_job_summary(durable_job / "manifest.json"),
        )
    inner_result["provider_measurement"] = _LAST_PROVIDER_MEASUREMENT
    return inner_result


def _dispatch_resume_strategy(
    *,
    runtime: Any,
    record: Any,
    resume_strategy: str,
    external_recovery_basis: str | None,
    external_recovery_max_attempts: int | None,
    implementation_recovery_basis: str,
    implementation_fix_identity: str,
    implementation_failure_category: str,
    implementation_recovery_action: str,
    implementation_issue_code: str,
    implementation_retry_stage: str,
    implementation_recovery_max_attempts: int | None,
) -> None:
    """Start the requested recovery without mutating an interrupted attempt."""

    if resume_strategy == "interrupted-recovery":
        recovered_future = runtime._futures.get(record.job_id)
        if recovered_future is None:
            raise RuntimeError(
                "interrupted presentation job was not recovered by the runtime",
            )
        return

    from lantai.presentation.models import (
        JobStage,
        PresentationFailureCategory,
        PresentationFailureRoute,
        PresentationRecoveryAction,
    )

    conversation_id = record.conversation_id
    if resume_strategy == "automatic":
        runtime.retry_failed(record.job_id, conversation_id)
        return
    if resume_strategy == "external-recovery":
        if not external_recovery_basis:
            raise ValueError(
                "external recovery requires a verified evidence identifier",
            )
        reviewed_routes = [
            PresentationFailureRoute.model_validate(payload)
            for payload in _external_recovery_route_payloads(record)
        ]
        runtime.retry_failed_after_external_recovery(
            record.job_id,
            conversation_id,
            reviewed_routes=reviewed_routes,
            review_basis=external_recovery_basis,
            **(
                {"max_attempts": external_recovery_max_attempts}
                if external_recovery_max_attempts is not None
                else {}
            ),
        )
        return
    if resume_strategy == "implementation-fix":
        if not re.fullmatch(
            r"[a-z0-9_.:-]{3,120}",
            implementation_issue_code,
        ):
            raise ValueError(
                "implementation issue code must be a short safe identifier",
            )
        reviewed_route = PresentationFailureRoute(
            category=PresentationFailureCategory(
                implementation_failure_category,
            ),
            recovery_action=PresentationRecoveryAction(
                implementation_recovery_action,
            ),
            issue_codes=[implementation_issue_code],
            retry_stage=JobStage(implementation_retry_stage),
            automatic_retry_allowed=False,
            requires_human=False,
        )
        runtime.retry_failed_after_implementation_fix(
            record.job_id,
            conversation_id,
            reviewed_routes=[reviewed_route],
            review_basis=implementation_recovery_basis,
            fix_identity=implementation_fix_identity,
            **(
                {"max_attempts": implementation_recovery_max_attempts}
                if implementation_recovery_max_attempts is not None
                else {}
            ),
        )
        return
    raise ValueError("unsupported presentation resume strategy")


def _inner_resume(
    *,
    job_root: Path,
    job_id: str,
    agent_id: str,
    expected_provider_id: str,
    expected_model: str,
    resume_strategy: str,
    external_recovery_basis: str | None,
    external_recovery_max_attempts: int | None,
    implementation_recovery_basis: str,
    implementation_fix_identity: str,
    implementation_failure_category: str,
    implementation_recovery_action: str,
    implementation_issue_code: str,
    implementation_retry_stage: str,
    implementation_recovery_max_attempts: int | None,
) -> dict[str, Any]:
    """Resume one retained job through its persisted recovery contract."""

    global _LAST_PROVIDER_MEASUREMENT
    _LAST_PROVIDER_MEASUREMENT = None
    from lantai.config.config import load_agent_config
    from lantai.presentation.intelligence import (
        UtilityPresentationObjectEditPlanner,
        UtilityPresentationPageAuthor,
        UtilityPresentationRepairDesigner,
        UtilityPresentationSemanticPlanner,
        UtilityPresentationVisualCritic,
    )
    from lantai.presentation.models import (
        DeckIR,
        JobStatus,
        QualityReport,
    )
    from lantai.presentation.quality import is_verified_delivery
    from lantai.presentation.render import slide_image_paths
    from lantai.presentation.runtime import PresentationJobRuntime
    from lantai.providers.provider_manager import ProviderManager

    manager = ProviderManager.get_instance()
    active = (
        load_agent_config(agent_id).active_model or manager.get_active_model()
    )
    if active is None or not manager.is_chat_model_slot_selectable(active):
        raise RuntimeError("configured chat model is not selectable")
    actual = (active.provider_id, active.model)
    expected = (expected_provider_id, expected_model)
    if actual != expected:
        raise RuntimeError("configured provider route does not match request")

    with _measure_provider_invocations():
        runtime = PresentationJobRuntime(
            job_root,
            semantic_planner=UtilityPresentationSemanticPlanner(agent_id),
            page_author=UtilityPresentationPageAuthor(agent_id),
            object_edit_planner=UtilityPresentationObjectEditPlanner(agent_id),
            visual_critic=UtilityPresentationVisualCritic(agent_id),
            repair_designer=UtilityPresentationRepairDesigner(agent_id),
        )
        try:
            record = runtime.service.store.get(job_id)
            conversation_id = record.conversation_id
            _dispatch_resume_strategy(
                runtime=runtime,
                record=record,
                resume_strategy=resume_strategy,
                external_recovery_basis=external_recovery_basis,
                external_recovery_max_attempts=external_recovery_max_attempts,
                implementation_recovery_basis=implementation_recovery_basis,
                implementation_fix_identity=implementation_fix_identity,
                implementation_failure_category=(
                    implementation_failure_category
                ),
                implementation_recovery_action=implementation_recovery_action,
                implementation_issue_code=implementation_issue_code,
                implementation_retry_stage=implementation_retry_stage,
                implementation_recovery_max_attempts=(
                    implementation_recovery_max_attempts
                ),
            )
            runtime._futures[job_id].result(
                timeout=record.timeout_seconds + 30
            )
            completed = runtime.get(job_id, conversation_id)
            artifacts = runtime.artifacts(job_id, conversation_id)
            job_dir = job_root.resolve() / job_id
            output_path = job_dir / "output" / "presentation.pptx"
            quality_path = job_dir / "quality" / "quality-report.json"
            deck_path = job_dir / "checkpoints" / "deck-ir.json"
            current_version_path = job_dir / "versions" / "current.json"
            verified = False
            if completed.status == JobStatus.COMPLETED:
                try:
                    report = QualityReport.model_validate(
                        artifacts.get("quality") or {}
                    )
                    deck = DeckIR.model_validate(
                        artifacts.get("deck_ir") or {}
                    )
                    render_set_id = (
                        report.render_set_id or f"round-{report.repair_rounds}"
                    )
                    if Path(render_set_id).name != render_set_id:
                        raise ValueError("invalid render evidence directory")
                    render_paths = slide_image_paths(
                        job_dir / "renders" / render_set_id
                    )
                    verified = is_verified_delivery(
                        deck,
                        report,
                        output_path,
                        render_paths,
                    )
                except (OSError, ValueError):
                    verified = False
            inner_result = {
                "ok": verified,
                "job_id": job_id,
                "status": "passed" if verified else "failed",
                "output_path": str(output_path) if verified else "",
                "quality_path": str(quality_path),
                "job_manifest_path": str(job_dir / "manifest.json"),
                "deck_ir_path": str(deck_path),
                "current_version_path": str(current_version_path),
            }
            if not verified:
                inner_result.update(
                    _failed_job_summary(job_dir / "manifest.json")
                )
        finally:
            runtime.shutdown()

    inner_result["provider_measurement"] = _LAST_PROVIDER_MEASUREMENT
    return inner_result


def _last_json_line(value: str) -> dict[str, Any]:
    for line in reversed(value.splitlines()):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def run(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
    request_path: Path,
    job_root: Path,
    output_dir: Path,
    workspace_root: Path,
    expected_provider_id: str,
    expected_model: str,
    agent_id: str,
    outer_timeout_seconds: float | None = None,
    failure_recovery_root: Path | None = None,
    completed_job_retention_root: Path | None = None,
    resume_job_root: Path | None = None,
    resume_job_id: str | None = None,
    resume_strategy: str = "implementation-fix",
    external_recovery_basis: str | None = None,
    external_recovery_max_attempts: int | None = None,
    implementation_recovery_basis: str = (
        DEFAULT_IMPLEMENTATION_RECOVERY_BASIS
    ),
    implementation_fix_identity: str = DEFAULT_IMPLEMENTATION_FIX_IDENTITY,
    implementation_failure_category: str = (
        DEFAULT_IMPLEMENTATION_FAILURE_CATEGORY
    ),
    implementation_recovery_action: str = (
        DEFAULT_IMPLEMENTATION_RECOVERY_ACTION
    ),
    implementation_issue_code: str = DEFAULT_IMPLEMENTATION_ISSUE_CODE,
    implementation_retry_stage: str = DEFAULT_IMPLEMENTATION_RETRY_STAGE,
    implementation_recovery_max_attempts: int | None = None,
) -> dict[str, Any]:
    request_path = request_path.expanduser().resolve()
    workspace_root = workspace_root.expanduser().resolve()
    if completed_job_retention_root is not None:
        completed_job_retention_root = completed_job_retention_root.expanduser().resolve()
        protected = (
            PROJECT_ROOT.resolve(), workspace_root,
            source_working_dir.expanduser().resolve(),
            source_secret_dir.expanduser().resolve(),
        )
        if any(
            _is_within(completed_job_retention_root, item)
            or _is_within(item, completed_job_retention_root)
            for item in protected
        ):
            raise ValueError("completed job retention root overlaps protected data")
    request_summary = _file_summary(request_path)
    material_summaries = _material_summaries(request_path, workspace_root)
    request_payload = _read_json(request_path)
    request_timeout = float(request_payload.get("timeout_seconds") or 300)
    timeout = outer_timeout_seconds or request_timeout + 120
    source_head = _source_head()
    provider_smoke = _load_configured_provider_smoke()
    external_recovery_max_attempts = _normalize_external_recovery_max_attempts(
        external_recovery_max_attempts,
    )
    implementation_recovery_max_attempts = (
        _normalize_implementation_recovery_max_attempts(
            implementation_recovery_max_attempts,
        )
    )
    if (resume_job_root is None) != (resume_job_id is None):
        raise ValueError("presentation resume requires both job root and job id")
    is_resume = resume_job_root is not None and resume_job_id is not None
    validated_resume_root = (
        _validate_resume_target(
            resume_job_root=resume_job_root,
            resume_job_id=resume_job_id,
            workspace_root=workspace_root,
        )
        if is_resume
        else None
    )
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-full-presentation-provider-"))
    private_job_root = (
        validated_resume_root
        if is_resume
        else smoke_root / "jobs"
    )
    private_output_dir = smoke_root / "outputs"
    started = time.monotonic()
    evidence: dict[str, Any] = {}
    try:
        working_dir, secret_dir, configured = provider_smoke._stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(),
            source_secret_dir=source_secret_dir.expanduser().resolve(),
            agent_id=agent_id,
            target_root=smoke_root,
            active_model_override={
                "provider_id": expected_provider_id,
                "model": expected_model,
            },
        )
        configured_route = (
            str(configured.get("provider_id") or ""),
            str(configured.get("model") or ""),
        )
        if configured_route != (expected_provider_id, expected_model):
            raise RuntimeError("staged provider route does not match request")
        env = {
            **os.environ,
            "LANTAI_WORKING_DIR": str(working_dir),
            "LANTAI_SECRET_DIR": str(secret_dir),
            "COPAW_WORKING_DIR": str(working_dir),
            "COPAW_SECRET_DIR": str(secret_dir),
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_ACTIVE_AGENT_ID": agent_id,
            "PYTHONPATH": str(SOURCE_ROOT),
        }
        source_pptxgenjs = PROJECT_ROOT / "console" / "node_modules" / "pptxgenjs"
        if (
            "LANTAI_PPTXGENJS_MODULE" not in env
            and source_pptxgenjs.exists()
        ):
            env["LANTAI_PPTXGENJS_MODULE"] = str(source_pptxgenjs)
        child_command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--inner",
                "--request",
                str(request_path),
                "--job-root",
                str(private_job_root),
                "--output-dir",
                str(private_output_dir),
                "--workspace-root",
                str(workspace_root),
                "--agent-id",
                agent_id,
                "--expected-provider-id",
                expected_provider_id,
                "--expected-model",
                expected_model,
            ]
        if is_resume:
            child_command.extend([
                "--resume-job-id",
                str(resume_job_id),
                "--resume-strategy",
                resume_strategy,
            ])
            if external_recovery_basis is not None:
                child_command.extend([
                    "--external-recovery-basis",
                    external_recovery_basis,
                ])
            if external_recovery_max_attempts is not None:
                child_command.extend([
                    "--external-recovery-max-attempts",
                    str(external_recovery_max_attempts),
                ])
            if resume_strategy == "implementation-fix":
                child_command.extend([
                    "--implementation-recovery-basis",
                    implementation_recovery_basis,
                    "--implementation-fix-identity",
                    implementation_fix_identity,
                    "--implementation-failure-category",
                    implementation_failure_category,
                    "--implementation-recovery-action",
                    implementation_recovery_action,
                    "--implementation-issue-code",
                    implementation_issue_code,
                    "--implementation-retry-stage",
                    implementation_retry_stage,
                ])
                if implementation_recovery_max_attempts is not None:
                    child_command.extend([
                        "--implementation-recovery-max-attempts",
                        str(implementation_recovery_max_attempts),
                    ])
        completed = subprocess.run(
            child_command,
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        child = _last_json_line(completed.stdout)
        if completed.returncode != 0 or not child.get("ok"):
            evidence = _safe_child_failure(
                source_head=source_head,
                provider_id=expected_provider_id,
                model=expected_model,
                request_summary=request_summary,
                material_summaries=material_summaries,
                elapsed_ms=elapsed_ms,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                child=child,
                durable_job_store_persisted=is_resume,
            )
            if is_resume:
                evidence["recovery"] = {
                    "retained": True,
                    "job_id": str(resume_job_id),
                    "checkpoint_count": sum(
                        1
                        for path in (
                            private_job_root
                            / str(resume_job_id)
                            / "checkpoints"
                        ).rglob("*")
                        if path.is_file()
                    ),
                }
            elif failure_recovery_root is not None:
                recovery = _preserve_private_job(
                    private_job_root=private_job_root,
                    child=child,
                    recovery_root=failure_recovery_root,
                    workspace_root=workspace_root,
                )
                evidence["recovery"] = recovery
                if recovery["retained"]:
                    evidence["privacy"] = _privacy_summary(
                        durable_job_store_persisted=True,
                        private_job_store_removed=False,
                    )
            return evidence
        exported_output = _export_passing_pptx(child, output_dir)
        evidence = _build_safe_evidence(
            source_head=source_head,
            provider_id=expected_provider_id,
            model=expected_model,
            request_summary=request_summary,
            material_summaries=material_summaries,
            elapsed_ms=elapsed_ms,
            child=child,
            exported_output=exported_output,
            durable_job_store_persisted=is_resume,
        )
        if not is_resume and completed_job_retention_root is not None:
            retained = _preserve_private_job(
                private_job_root=private_job_root,
                child=child,
                recovery_root=completed_job_retention_root,
                workspace_root=workspace_root,
            )
            if not retained["retained"]:
                raise RuntimeError("completed presentation job was not retained")
            evidence["completed_job_retention"] = retained
            evidence["privacy"] = _privacy_summary(
                durable_job_store_persisted=True,
                private_job_store_removed=False,
            )
        return evidence
    except BaseException as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        evidence = {
            "schema_version": "1.0",
            "run_kind": "full-production-presentation",
            "source_head": source_head,
            "provider": {"route": f"{expected_provider_id}/{expected_model}"},
            "input": {
                "request": request_summary,
                "materials": material_summaries,
            },
            "timing_ms": {"wall_clock": elapsed_ms},
            **_safe_exception(exc),
            "privacy": _privacy_summary(
                durable_job_store_persisted=is_resume,
                private_job_store_removed=not is_resume,
            ),
        }
        return evidence
    finally:
        _finish_staged_profile(smoke_root, evidence)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-working-dir", type=Path)
    parser.add_argument("--source-secret-dir", type=Path)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--expected-provider-id", default="kimi-codingplan")
    parser.add_argument("--expected-model", default="kimi-for-coding")
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--outer-timeout", type=float)
    parser.add_argument("--failure-recovery-root", type=Path)
    parser.add_argument("--completed-job-retention-root", type=Path)
    parser.add_argument("--resume-job-root", type=Path)
    parser.add_argument("--resume-job-id")
    parser.add_argument(
        "--resume-strategy",
        choices=(
            "implementation-fix",
            "automatic",
            "external-recovery",
            "interrupted-recovery",
        ),
        default="implementation-fix",
    )
    parser.add_argument("--external-recovery-basis")
    parser.add_argument("--external-recovery-max-attempts", type=int)
    parser.add_argument(
        "--implementation-recovery-basis",
        default=DEFAULT_IMPLEMENTATION_RECOVERY_BASIS,
    )
    parser.add_argument(
        "--implementation-fix-identity",
        default=DEFAULT_IMPLEMENTATION_FIX_IDENTITY,
    )
    parser.add_argument(
        "--implementation-failure-category",
        choices=("content", "geometry", "aesthetic", "file", "model_service"),
        default=DEFAULT_IMPLEMENTATION_FAILURE_CATEGORY,
    )
    parser.add_argument(
        "--implementation-recovery-action",
        choices=(
            "revise_content",
            "patch_geometry",
            "redesign_slide",
            "retry_file_stage",
            "retry_model_service",
        ),
        default=DEFAULT_IMPLEMENTATION_RECOVERY_ACTION,
    )
    parser.add_argument(
        "--implementation-issue-code",
        default=DEFAULT_IMPLEMENTATION_ISSUE_CODE,
    )
    parser.add_argument(
        "--implementation-retry-stage",
        choices=(
            "brief",
            "research",
            "plan",
            "deck_ir",
            "compose",
            "render",
            "quality",
            "repair",
        ),
        default=DEFAULT_IMPLEMENTATION_RETRY_STAGE,
    )
    parser.add_argument("--implementation-recovery-max-attempts", type=int)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args()

    if args.inner:
        try:
            if args.resume_job_id:
                result = _inner_resume(
                    job_root=args.job_root,
                    job_id=args.resume_job_id,
                    agent_id=args.agent_id,
                    expected_provider_id=args.expected_provider_id,
                    expected_model=args.expected_model,
                    resume_strategy=args.resume_strategy,
                    external_recovery_basis=args.external_recovery_basis,
                    external_recovery_max_attempts=(
                        args.external_recovery_max_attempts
                    ),
                    implementation_recovery_basis=(
                        args.implementation_recovery_basis
                    ),
                    implementation_fix_identity=(
                        args.implementation_fix_identity
                    ),
                    implementation_failure_category=(
                        args.implementation_failure_category
                    ),
                    implementation_recovery_action=(
                        args.implementation_recovery_action
                    ),
                    implementation_issue_code=(
                        args.implementation_issue_code
                    ),
                    implementation_retry_stage=(
                        args.implementation_retry_stage
                    ),
                    implementation_recovery_max_attempts=(
                        args.implementation_recovery_max_attempts
                    ),
                )
            else:
                result = _inner_run(
                    request_path=args.request,
                    job_root=args.job_root,
                    output_dir=args.output_dir,
                    workspace_root=args.workspace_root,
                    agent_id=args.agent_id,
                    expected_provider_id=args.expected_provider_id,
                    expected_model=args.expected_model,
                )
        except BaseException as exc:
            result = _safe_exception(exc)
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("ok"):
            raise SystemExit(1)
        return

    required = {
        "source-working-dir": args.source_working_dir,
        "source-secret-dir": args.source_secret_dir,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        parser.error(f"missing required arguments: {', '.join(missing)}")
    if args.evidence_output is not None:
        try:
            _validate_generation_evidence_target(args.evidence_output, args)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    result = run(
        source_working_dir=args.source_working_dir,
        source_secret_dir=args.source_secret_dir,
        request_path=args.request,
        job_root=args.job_root,
        output_dir=args.output_dir,
        workspace_root=args.workspace_root,
        expected_provider_id=args.expected_provider_id,
        expected_model=args.expected_model,
        agent_id=args.agent_id,
        outer_timeout_seconds=args.outer_timeout,
        failure_recovery_root=args.failure_recovery_root,
        completed_job_retention_root=args.completed_job_retention_root,
        resume_job_root=args.resume_job_root,
        resume_job_id=args.resume_job_id,
        resume_strategy=args.resume_strategy,
        external_recovery_basis=args.external_recovery_basis,
        external_recovery_max_attempts=args.external_recovery_max_attempts,
        implementation_recovery_basis=args.implementation_recovery_basis,
        implementation_fix_identity=args.implementation_fix_identity,
        implementation_failure_category=args.implementation_failure_category,
        implementation_recovery_action=args.implementation_recovery_action,
        implementation_issue_code=args.implementation_issue_code,
        implementation_retry_stage=args.implementation_retry_stage,
        implementation_recovery_max_attempts=(
            args.implementation_recovery_max_attempts
        ),
    )
    if args.evidence_output is not None:
        target = _validate_generation_evidence_target(args.evidence_output, args)
        _write_safe_evidence(target, result)
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
