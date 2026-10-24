#!/usr/bin/env python3
"""Exercise production natural editing on a retained private presentation job.

Only the encrypted Provider profile is staged temporarily. The original job,
budget, version history and owner are retained, and redo_slide owns all edits,
validation and publication. Export only a verified PPTX and safe count/hash
evidence; never persist captured diagnostics, instructions or model responses.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from lantai.presentation.models import SpeakerNotesOutputContract


PROJECT_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "presentation_generation_smoke_helpers",
    PROJECT_ROOT / "scripts/source_presentation_full_generation_smoke.py",
)
if spec is None or spec.loader is None:
    raise RuntimeError("presentation smoke helpers are unavailable")
full = importlib.util.module_from_spec(spec)
spec.loader.exec_module(full)


_CRITIC_METHODS = frozenset({
    "review", "review_slide", "review_slide_cancellable",
    "review_deck", "review_deck_cancellable",
})


def _safe_critic_failures(value: object) -> list[dict[str, Any]]:
    """Allow only code locations and hashes, never provider diagnostics."""
    if not isinstance(value, list):
        return []
    return [
        {
            "method": item["method"],
            "slide_id": full._safe_failure_token(item.get("slide_id")),
            "error_type": full._safe_failure_token(item.get("error_type")),
            "error_sha256": full._safe_sha256(item.get("error_sha256")),
            "error_origin": full._safe_error_origin(item.get("error_origin")),
            "utility_timeout": (
                full._safe_utility_timeout(item.get("utility_timeout"))
                if item.get("error_type") == "UtilityTaskTimeoutError" else None
            ),
            "schema_issues": (
                full._safe_schema_issues(item.get("schema_issues"))
                if item.get("error_type") == "StructuredTaskValidationError" else []
            ),
        }
        for item in value[:32]
        if isinstance(item, dict) and isinstance(item.get("method"), str)
        and item["method"] in _CRITIC_METHODS
    ]


def _observe_critic_errors(critic: object, diagnostics: list[dict[str, Any]]) -> None:
    """Observe the unchanged critic calls and re-raise their exact exceptions."""
    for method in _CRITIC_METHODS:
        original = getattr(critic, method, None)
        if not callable(original):
            continue

        def observed(*args, _original=original, _method=method, **kwargs):
            try:
                return _original(*args, **kwargs)
            except BaseException as exc:
                diagnostics.append({
                    "method": _method,
                    "slide_id": full._safe_failure_token(
                        getattr(args[0], "slide_id", "") if args else "",
                    ),
                    **full._safe_exception(exc),
                })
                raise

        setattr(critic, method, observed)


def _validate_edit_target(
    *, job_root: Path, job_id: str, workspace_root: Path,
    instruction_file: Path, base_sha256: str,
) -> Path:
    root = full._validate_resume_target(
        resume_job_root=job_root, resume_job_id=job_id, workspace_root=workspace_root,
    )
    instruction = instruction_file.expanduser().resolve()
    if any(full._is_within(instruction, protected) for protected in (
        PROJECT_ROOT.resolve(), workspace_root.expanduser().resolve(),
    )):
        raise ValueError("edit instruction must stay outside the workspace")
    if not instruction.is_file() or not instruction.read_text(encoding="utf-8").strip():
        raise ValueError("edit instruction is unavailable")
    if full._safe_sha256(base_sha256) != base_sha256:
        raise ValueError("edit base must be an exact SHA256")
    job = root / job_id
    if full._read_json(job / "manifest.json").get("status") != "completed":
        raise ValueError("only a completed presentation can be edited")
    output = job / "output/presentation.pptx"
    if not output.is_file() or full._sha256_file(output) != base_sha256:
        raise ValueError("edit base no longer matches the retained output")
    return root


def _validate_evidence_target(
    path: Path, *, job_root: Path, instruction_file: Path, output_dir: Path,
    source_working_dir: Path, source_secret_dir: Path,
    source_fact_corrections_file: Path | None = None,
) -> Path:
    """Evidence may never be an alias of an input, owned job or Provider store."""
    return full._validate_evidence_path(path,
        protected_roots=[job_root, output_dir, source_working_dir, source_secret_dir],
        protected_files=[instruction_file, *([source_fact_corrections_file]
                         if source_fact_corrections_file is not None else [])])


def _read_source_fact_corrections(
    path: Path | None, *, protected_roots: list[Path], protected_files: list[Path],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Keep caller excerpts private and reject aliases before staging a profile."""
    if path is None:
        return [], None
    from lantai.presentation.models import SourceFactCorrection

    try:
        target = path.expanduser().resolve()
        if (any(full._is_within(target, item.expanduser().resolve()) for item in protected_roots)
                or target in {item.expanduser().resolve() for item in protected_files}
                or not target.is_file() or target.stat().st_nlink != 1
                or not 0 < target.stat().st_size <= 128 * 1024):
            raise ValueError
        raw = target.read_bytes()
        if not 0 < len(raw) <= 128 * 1024:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, list) or not 1 <= len(value) <= 10:
            raise ValueError
        requests = [SourceFactCorrection.model_validate(item).model_dump(mode="json") for item in value]
        import hashlib
        return requests, {"count": len(requests), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    except (OSError, ValueError, TypeError):
        # JSON and schema diagnostics can contain private source quotations.
        raise ValueError("source_fact_correction: invalid caller file or protected input alias") from None


def _preflight_source_fact_corrections(
    job: Path, checkpoint: dict[str, Any], corrections: list[dict[str, Any]], requested: list[str],
) -> None:
    if not corrections and not checkpoint.get("source_fact_corrections"):
        return
    from lantai.presentation.models import DeckIR, DeckPlan, PresentationBrief
    from lantai.presentation.natural_edit import apply_source_fact_corrections

    try:
        deck = DeckIR.model_validate(checkpoint)
        brief = PresentationBrief.model_validate_json((job / "checkpoints/brief.json").read_text(encoding="utf-8"))
        plan_path = job / "checkpoints/deck-plan.json"
        plan = DeckPlan.model_validate_json(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else None
        apply_source_fact_corrections(deck, plan, brief, corrections, allowed_slide_ids=requested)
    except (OSError, ValueError, TypeError):
        raise ValueError("source_fact_correction: source provenance preflight failed") from None


def _validate_notes_mode(notes_only: bool, source_fact_corrections_file: Path | None) -> None:
    if not isinstance(notes_only, bool):
        raise TypeError("notes_only must be an explicit boolean")
    if notes_only and source_fact_corrections_file is not None:
        raise ValueError("notes-only mode cannot combine source fact corrections")


def _validate_notes_output_contract(
    notes_only: bool, value: dict[str, Any] | None,
) -> SpeakerNotesOutputContract | None:
    """Validate explicit caller limits before staging, without raw diagnostics."""
    if value is None:
        return None
    from lantai.presentation.models import SpeakerNotesOutputContract

    try:
        if not notes_only or not isinstance(value, dict):
            raise ValueError
        return SpeakerNotesOutputContract.model_validate(value)
    except (ValueError, TypeError):
        raise ValueError("notes_output_contract: invalid explicit caller limits") from None


def _inner_edit(
    *, job_root: Path, job_id: str, slide_id: str, instruction_file: Path,
    base_sha256: str, agent_id: str, expected_provider_id: str, expected_model: str,
    additional_slide_ids: list[str] | None = None,
    required_slide_ids: list[str] | None = None,
    source_fact_corrections_file: Path | None = None,
    notes_only: bool = False,
    notes_output_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_notes_mode(notes_only, source_fact_corrections_file)
    output_contract = _validate_notes_output_contract(notes_only, notes_output_contract)
    full._LAST_PROVIDER_MEASUREMENT = None
    from lantai.config.config import load_agent_config
    from lantai.presentation.intelligence import (
        UtilityPresentationObjectEditPlanner, UtilityPresentationPageAuthor,
        UtilityPresentationRepairDesigner, UtilityPresentationVisualCritic,
    )
    from lantai.presentation.models import DeckIR, JobStatus, QualityReport
    from lantai.presentation.quality import is_verified_delivery
    from lantai.presentation.render import slide_image_paths
    from lantai.presentation.runtime import PresentationJobRuntime
    from lantai.providers.provider_manager import ProviderManager

    corrections, _ = _read_source_fact_corrections(
        source_fact_corrections_file,
        protected_roots=[PROJECT_ROOT, job_root, *[Path(value) for name in (
            "LANTAI_WORKING_DIR", "LANTAI_SECRET_DIR") if (value := os.environ.get(name))]],
        protected_files=[instruction_file],
    )
    manager = ProviderManager.get_instance()
    active = load_agent_config(agent_id).active_model or manager.get_active_model()
    if active is None or not manager.is_chat_model_slot_selectable(active):
        raise RuntimeError("configured chat model is not selectable")
    if (active.provider_id, active.model) != (expected_provider_id, expected_model):
        raise RuntimeError("configured provider route does not match request")
    critic = UtilityPresentationVisualCritic(agent_id)
    critic_failures: list[dict[str, Any]] = []
    _observe_critic_errors(critic, critic_failures)
    with full._measure_provider_invocations():
        runtime = PresentationJobRuntime(
            job_root, object_edit_planner=UtilityPresentationObjectEditPlanner(
                agent_id, **({"notes_only": True} if notes_only else {}),
                **({"notes_output_contract": output_contract} if output_contract is not None else {}),
            ),
            page_author=UtilityPresentationPageAuthor(agent_id),
            visual_critic=critic,
            repair_designer=UtilityPresentationRepairDesigner(agent_id),
        )
        try:
            record = runtime.service.store.get(job_id)
            completed = runtime.redo_slide(
                job_id, record.conversation_id, slide_id,
                instruction_file.read_text(encoding="utf-8"), base_sha256,
                **({"additional_slide_ids": additional_slide_ids}
                   if additional_slide_ids else {}),
                **({"required_slide_ids": required_slide_ids}
                   if required_slide_ids else {}),
                **({"source_fact_corrections": corrections} if corrections else {}),
            )
            job = job_root.resolve() / job_id
            output = job / "output/presentation.pptx"
            quality = job / "quality/quality-report.json"
            checkpoint = job / "checkpoints/deck-ir.json"
            deck = DeckIR.model_validate_json(checkpoint.read_text(encoding="utf-8"))
            report = QualityReport.model_validate_json(quality.read_text(encoding="utf-8"))
            render_set = report.render_set_id or f"round-{report.repair_rounds}"
            if Path(render_set).name != render_set:
                raise ValueError("invalid render evidence directory")
            verified = (
                completed.status == JobStatus.COMPLETED
                and is_verified_delivery(deck, report, output, slide_image_paths(job / "renders" / render_set))
            )
            inner_result = {
                "ok": verified, "status": "passed" if verified else "failed", "job_id": job_id,
                "output_path": str(output) if verified else "",
                "quality_path": str(quality), "deck_ir_path": str(checkpoint),
                "job_manifest_path": str(job / "manifest.json"),
                "current_version_path": str(job / "versions/current.json"),
                "critic_failures": _safe_critic_failures(critic_failures),
            }
        except BaseException as exc:
            exc.presentation_critic_failures = _safe_critic_failures(critic_failures)
            raise
        finally:
            runtime.shutdown()
    return full._include_provider_measurement(
        inner_result, {"provider_measurement": full._LAST_PROVIDER_MEASUREMENT},
    )


def run(
    *, source_working_dir: Path, source_secret_dir: Path,
    job_root: Path, job_id: str, workspace_root: Path,
    instruction_file: Path, slide_id: str, base_sha256: str, output_dir: Path,
    expected_provider_id: str, expected_model: str, agent_id: str = "default",
    outer_timeout_seconds: float | None = None,
    additional_slide_ids: list[str] | None = None,
    required_slide_ids: list[str] | None = None,
    source_fact_corrections_file: Path | None = None,
    notes_only: bool = False,
    notes_output_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_notes_mode(notes_only, source_fact_corrections_file)
    output_contract = _validate_notes_output_contract(notes_only, notes_output_contract)
    contract_json = (
        json.dumps(output_contract.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if output_contract is not None else None
    )
    contract_summary = (
        {"sha256": hashlib.sha256(contract_json.encode("utf-8")).hexdigest(),
         "bytes": len(contract_json.encode("utf-8")),
         "forbidden_character_count": len(output_contract.forbidden_characters)}
        if contract_json is not None else None
    )
    from lantai.presentation.models import MAX_OBJECT_EDIT_SLIDES

    root = _validate_edit_target(
        job_root=job_root, job_id=job_id, workspace_root=workspace_root,
        instruction_file=instruction_file, base_sha256=base_sha256,
    )
    if not full._safe_failure_token(slide_id):
        raise ValueError("edit slide id is invalid")
    checkpoint = full._read_json(root / job_id / "checkpoints/deck-ir.json")
    additional = [] if additional_slide_ids is None else additional_slide_ids
    if (not isinstance(additional, list) or len(additional) > MAX_OBJECT_EDIT_SLIDES - 1
            or any(not full._safe_failure_token(page) for page in additional)):
        raise ValueError("edit page scope is invalid or unbounded")
    requested = [slide_id, *additional]
    if len(set(requested)) != len(requested):
        raise ValueError("edit page scope contains duplicate slides")
    existing = {slide.get("id") for slide in checkpoint.get("slides", [])
                if isinstance(slide, dict) and isinstance(slide.get("id"), str)}
    if not set(requested).issubset(existing):
        raise ValueError("edit page scope contains a slide not in the retained presentation")
    required = [] if required_slide_ids is None else required_slide_ids
    if (not isinstance(required, list) or len(required) > MAX_OBJECT_EDIT_SLIDES
            or any(not full._safe_failure_token(page) for page in required)
            or len(set(required)) != len(required) or not set(required).issubset(requested)):
        raise ValueError("required edit page scope must be a bounded unique subset of requested scope")
    output_dir = output_dir.expanduser().resolve()
    if full._is_within(output_dir, root):
        raise ValueError("export must not overwrite the private job")
    instruction_file = instruction_file.expanduser().resolve()
    corrections, correction_summary = _read_source_fact_corrections(
        source_fact_corrections_file,
        protected_roots=[PROJECT_ROOT, workspace_root, root, output_dir,
                         source_working_dir, source_secret_dir], protected_files=[instruction_file],
    )
    _preflight_source_fact_corrections(root / job_id, checkpoint, corrections, requested)
    instruction_summary = {
        "sha256": full._sha256_file(instruction_file), "bytes": instruction_file.stat().st_size,
    }
    source_head = full._source_head()
    manifest = root / job_id / "manifest.json"
    request_timeout = float(full._read_json(manifest).get("timeout_seconds") or 300)
    timeout = outer_timeout_seconds or request_timeout + 120
    profile = Path(tempfile.mkdtemp(prefix="lantai-presentation-edit-provider-"))
    started = time.monotonic()
    result: dict[str, Any]
    try:
        provider_smoke = full._load_configured_provider_smoke()
        working, secret, configured = provider_smoke._stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(),
            source_secret_dir=source_secret_dir.expanduser().resolve(), agent_id=agent_id,
            target_root=profile,
            active_model_override={"provider_id": expected_provider_id, "model": expected_model},
        )
        if (configured.get("provider_id"), configured.get("model")) != (expected_provider_id, expected_model):
            raise RuntimeError("staged provider route does not match request")
        correction_snapshot = None
        if corrections:
            correction_snapshot = profile / "source-fact-corrections.json"
            # Snapshot validated caller data. A later source-file mutation must
            # not silently change the request, and profile cleanup removes it.
            correction_snapshot.write_text(json.dumps(corrections, ensure_ascii=False), encoding="utf-8")
            correction_snapshot.chmod(0o600)
        env = {
            **os.environ, "LANTAI_WORKING_DIR": str(working), "LANTAI_SECRET_DIR": str(secret),
            "COPAW_WORKING_DIR": str(working), "COPAW_SECRET_DIR": str(secret),
            "LANTAI_DISABLE_KEYRING": "1", "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_ACTIVE_AGENT_ID": agent_id, "PYTHONPATH": str(PROJECT_ROOT / "src"),
        }
        command = [
            sys.executable, str(Path(__file__).resolve()), "--inner",
            "--job-root", str(root), "--job-id", job_id, "--slide-id", slide_id,
            "--instruction-file", str(instruction_file), "--base-sha256", base_sha256,
            "--agent-id", agent_id, "--expected-provider-id", expected_provider_id,
            "--expected-model", expected_model,
        ]
        for page in additional:
            command.extend(["--additional-slide-id", page])
        for page in required:
            command.extend(["--required-slide-id", page])
        if notes_only:
            command.append("--notes-only")
        if contract_json is not None:
            command.extend(["--notes-output-contract-json", contract_json])
        if correction_snapshot is not None:
            command.extend(["--source-fact-corrections-file", str(correction_snapshot)])
        completed = subprocess.run(
            command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        elapsed = round((time.monotonic() - started) * 1000)
        child = full._last_json_line(completed.stdout)
        if completed.returncode == 0 and child.get("ok"):
            expected_output = root / job_id / "output/presentation.pptx"
            if Path(str(child.get("output_path") or "")).resolve() != expected_output:
                raise ValueError("verified edit did not return its owned output")
            exported = full._export_passing_pptx(child, output_dir)
            result = full._build_safe_evidence(
                source_head=source_head, provider_id=expected_provider_id, model=expected_model,
                request_summary=instruction_summary, material_summaries=[], elapsed_ms=elapsed,
                child=child, exported_output=exported, durable_job_store_persisted=True,
            )
        else:
            result = full._safe_child_failure(
                source_head=source_head, provider_id=expected_provider_id, model=expected_model,
                request_summary=instruction_summary, material_summaries=[], elapsed_ms=elapsed,
                returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr,
                child=child, durable_job_store_persisted=True,
            )
        result["critic_failures"] = _safe_critic_failures(child.get("critic_failures"))
    except BaseException as exc:
        result = {
            "schema_version": "1.0", "source_head": source_head,
            **full._safe_exception(exc),
            "timing_ms": {"wall_clock": round((time.monotonic() - started) * 1000)},
            "privacy": full._privacy_summary(durable_job_store_persisted=True, private_job_store_removed=False),
        }
    finally:
        full._finish_staged_profile(profile, result)
    output = root / job_id / "output/presentation.pptx"
    result["run_kind"] = "full-production-presentation-edit"
    result["operation"] = {
        "slide_id": slide_id, "instruction": instruction_summary,
        "additional_slide_ids": additional,
        "required_slide_ids": required,
        **({"planner_output_mode": "notes_only"} if notes_only else {}),
        **({"notes_output_contract": contract_summary} if contract_summary is not None else {}),
        **({"source_fact_corrections": correction_summary} if correction_summary else {}),
        "base_output_sha256": base_sha256,
        "base_preserved_on_failure": (
            not result.get("ok") and output.is_file() and full._sha256_file(output) == base_sha256
        ),
    }
    result["source_files"] = {
        "edit_harness_sha256": full._sha256_file(Path(__file__)),
        "native_backend_sha256": full._sha256_file(PROJECT_ROOT / "src/lantai/presentation/pptxgenjs_backend.mjs"),
        "object_planner_sha256": full._sha256_file(PROJECT_ROOT / "src/lantai/presentation/intelligence.py"),
        "object_edit_apply_sha256": full._sha256_file(PROJECT_ROOT / "src/lantai/presentation/natural_edit.py"),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    for name in ("source-working-dir", "source-secret-dir", "workspace-root", "output-dir", "evidence-output"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--instruction-file", type=Path, required=True)
    for name in ("job-id", "slide-id", "base-sha256"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--additional-slide-id", action="append", default=[])
    parser.add_argument("--required-slide-id", action="append", default=[])
    parser.add_argument("--notes-only", action="store_true",
                        help="Explicit notes-only output protocol; does not expand page scope or skip whole-deck QA")
    parser.add_argument("--notes-output-contract-json",
                        help="Explicit notes-only Han length and forbidden-character contract, validated before Provider staging")
    parser.add_argument("--source-fact-corrections-file", type=Path)
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--expected-provider-id", default="kimi-codingplan")
    parser.add_argument("--expected-model", default="kimi-for-coding")
    parser.add_argument("--outer-timeout", type=float)
    args = parser.parse_args()
    output_contract = None
    if args.notes_output_contract_json is not None:
        try:
            if not 0 < len(args.notes_output_contract_json.encode("utf-8")) <= 8192:
                raise ValueError
            _validate_notes_mode(args.notes_only, args.source_fact_corrections_file)
            output_contract = _validate_notes_output_contract(
                args.notes_only, json.loads(args.notes_output_contract_json),
            )
        except (ValueError, TypeError, UnicodeError):
            parser.error("notes_output_contract: invalid explicit caller limits")
    common = dict(job_root=args.job_root, job_id=args.job_id, slide_id=args.slide_id,
                  instruction_file=args.instruction_file, base_sha256=args.base_sha256,
                  agent_id=args.agent_id, expected_provider_id=args.expected_provider_id,
                  expected_model=args.expected_model, additional_slide_ids=args.additional_slide_id,
                  required_slide_ids=args.required_slide_id,
                  source_fact_corrections_file=args.source_fact_corrections_file,
                  **({"notes_output_contract": output_contract.model_dump(mode="json")}
                     if output_contract is not None else {}),
                  **({"notes_only": True} if args.notes_only else {}))
    if args.inner:
        try:
            result = _inner_edit(**common)
        except BaseException as exc:
            result = full._safe_exception(exc)
            result["critic_failures"] = _safe_critic_failures(
                getattr(exc, "presentation_critic_failures", []),
            )
    else:
        if any(value is None for value in (args.source_working_dir, args.source_secret_dir,
                                          args.workspace_root, args.output_dir)):
            parser.error("outer edit requires source profile, workspace root and output directory")
        evidence_output = None
        if args.evidence_output is not None:
            try:
                evidence_output = _validate_evidence_target(
                    args.evidence_output, job_root=args.job_root, instruction_file=args.instruction_file,
                    output_dir=args.output_dir, source_working_dir=args.source_working_dir,
                    source_secret_dir=args.source_secret_dir,
                    source_fact_corrections_file=args.source_fact_corrections_file,
                )
            except ValueError as exc:
                parser.error(str(exc))
        result = run(**common, source_working_dir=args.source_working_dir,
                     source_secret_dir=args.source_secret_dir, workspace_root=args.workspace_root,
                     output_dir=args.output_dir, outer_timeout_seconds=args.outer_timeout)
        if evidence_output is not None:
            _validate_evidence_target(
                evidence_output, job_root=args.job_root, instruction_file=args.instruction_file,
                output_dir=args.output_dir, source_working_dir=args.source_working_dir,
                source_secret_dir=args.source_secret_dir,
                source_fact_corrections_file=args.source_fact_corrections_file,
            )
            full._write_safe_evidence(evidence_output, result)
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
