# -*- coding: utf-8 -*-
"""Thin command-line boundary for the deterministic presentation runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
from pathlib import Path

from pydantic import ValidationError

from lantai.security.output_sanitizer import sanitize_public_output

from .intelligence import (
    UtilityPresentationObjectEditPlanner,
    UtilityPresentationPageAuthor,
    UtilityPresentationRepairDesigner,
    UtilityPresentationSemanticPlanner,
    UtilityPresentationVisualCritic,
)
from .models import (
    DeckIR,
    PresentationJobRequest,
    QualityReport,
    SkillExecutionIdentity,
)
from .naming import unique_presentation_path
from .quality import is_verified_delivery
from .render import slide_image_paths
from .runtime import PresentationJobRuntime
from .theme import normalize_theme_id


_NESTED_REQUEST_KEYS = frozenset(
    {
        "audience",
        "language",
        "materials",
        "mode",
        "model_id",
        "model_config_fingerprint",
        "planning_mode",
        "pipeline_version",
        "idempotency_key",
        "input_manifest_hash",
        "timeout_seconds",
        "cost_budget_units",
        "seed",
        "slide_count",
        "speaker_notes",
        "references",
        "template_path",
        "template_mode",
        "theme",
        "theme_id",
        "user_request",
    },
)


def _artifact_descriptor(
    path: Path | None,
    *,
    role: str,
) -> dict[str, object]:
    """Describe an artifact without claiming that a missing file exists."""

    if path is None:
        return {"role": role, "available": False}
    resolved = path.resolve()
    if not resolved.is_file():
        return {"role": role, "available": False}
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "role": role,
        "available": True,
        "path": str(resolved),
        "media_type": mimetypes.guess_type(resolved.name)[0]
        or "application/octet-stream",
        "size_bytes": resolved.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def _write_delivery_sidecars(
    *,
    destination_root: Path,
    job_root: Path,
    job_id: str,
    delivery_status: str,
    quality: object,
    delivered: Path | None,
    render_paths: tuple[Path, ...] = (),
) -> dict[str, str]:
    """Write a truthful, typed recovery manifest beside the deliverable."""

    destination_root.mkdir(parents=True, exist_ok=True)
    quality_path = destination_root / "quality-report.json"
    if quality is not None:
        quality_path.write_text(
            json.dumps(quality, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    durable_job = job_root.resolve() / job_id
    durable_render_paths: list[Path] = []
    if delivered is not None:
        render_root = destination_root / "render-evidence" / job_id
        render_root.mkdir(parents=True, exist_ok=True)
        for index, source in enumerate(render_paths, start=1):
            destination = render_root / f"slide-{index}.png"
            temporary = destination.with_suffix(".tmp")
            shutil.copy2(source, temporary)
            temporary.replace(destination)
            durable_render_paths.append(destination)
    artifacts = {
        "presentation": _artifact_descriptor(
            delivered,
            role="editable_presentation",
        ),
        "quality_report": _artifact_descriptor(
            quality_path,
            role="quality_evidence",
        ),
        "deck_ir": _artifact_descriptor(
            durable_job / "checkpoints" / "deck-ir.json",
            role="editable_structure_checkpoint",
        ),
        "renders": [
            _artifact_descriptor(path, role="visual_render")
            for path in durable_render_paths
        ],
    }
    manifest = {
        "schema_version": "2.0",
        "job_id": job_id,
        "delivery_status": delivery_status,
        "recoverable": bool(
            artifacts["deck_ir"].get("available")
            and artifacts["presentation"].get("available"),
        ),
        "visual_verified": bool(
            delivered is not None
            and delivery_status == "passed"
            and durable_render_paths,
        ),
        "artifacts": artifacts,
    }
    manifest_path = destination_root / "generation-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "quality": str(quality_path) if quality is not None else "",
        "record": str(manifest_path),
    }


def _repair_nested_request_controls(payload: object) -> object:
    """Recover two provider-observed wrappers without weakening strictness."""

    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    controls: dict[str, object] | None = None
    wrapped = normalized.pop("presentation_request", None)
    if isinstance(wrapped, dict):
        controls = dict(wrapped)
    else:
        raw_user_request = normalized.get("user_request")
        if isinstance(raw_user_request, dict):
            controls = dict(raw_user_request)
        elif isinstance(raw_user_request, str):
            try:
                decoded = json.loads(raw_user_request)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict) and _NESTED_REQUEST_KEYS.intersection(
                decoded,
            ):
                controls = dict(decoded)
    if controls is None:
        if wrapped is not None:
            normalized["presentation_request"] = wrapped
        return normalized

    for key in (
        "audience",
        "language",
        "materials",
        "mode",
        "model_id",
        "seed",
        "slide_count",
        "template_path",
        "template_mode",
    ):
        if key not in normalized and key in controls:
            normalized[key] = controls[key]
    if "theme_id" not in normalized:
        theme = controls.get("theme_id") or controls.get("theme")
        if theme is not None:
            normalized["theme_id"] = theme

    nested_user_request = controls.get("user_request")
    existing_user_request = normalized.get("user_request")
    if isinstance(nested_user_request, str) and nested_user_request.strip():
        normalized["user_request"] = nested_user_request.strip()
    elif (
        isinstance(existing_user_request, str)
        and existing_user_request.strip()
        and not existing_user_request.lstrip().startswith("{")
    ):
        normalized["user_request"] = existing_user_request.strip()
    else:
        language = str(
            normalized.get("language") or controls.get("language") or "",
        ).lower()
        normalized["user_request"] = (
            "根据提供的材料制作演示文稿"
            if language.startswith("zh")
            else "Create a presentation from the provided materials"
        )
    return normalized


def _load_request(
    request_path: Path,
    *,
    workspace_root: Path | None = None,
) -> PresentationJobRequest:
    """Load the strict request while repairing two observed model aliases.

    The public schema remains strict. This CLI boundary only translates the
    intuitive material shapes emitted by providers before validation, keeping
    unknown fields visible as validation errors.
    """

    payload = _repair_nested_request_controls(
        json.loads(request_path.read_text(encoding="utf-8")),
    )
    if isinstance(payload, dict) and isinstance(
        payload.get("materials"),
        list,
    ):
        normalized_materials = []
        for index, material in enumerate(payload["materials"]):
            if not isinstance(material, dict):
                normalized_materials.append(material)
                continue
            normalized = dict(material)
            title = normalized.pop("title", None)
            material_type = normalized.pop("type", None)
            description = normalized.pop("description", None)
            normalized.pop("role", None)
            path = str(normalized.get("path") or "").strip()
            if path:
                request_root = (
                    workspace_root or request_path.parent
                ).resolve()
                material_path = Path(path).expanduser()
                if not material_path.is_absolute():
                    material_path = request_root / material_path
                material_path = material_path.resolve()
                try:
                    material_path.relative_to(request_root)
                except ValueError as exc:
                    raise ValueError(
                        "presentation material must stay inside the request workspace: "
                        f"{material_path}",
                    ) from exc
                normalized["path"] = str(material_path)
                path = normalized["path"]
            if not str(normalized.get("name") or "").strip():
                normalized["name"] = (
                    str(title or "").strip()
                    or (Path(path).name if path else "")
                    or str(description or "").strip()
                    or f"material-{index + 1}"
                )
            if (
                not str(normalized.get("media_type") or "").strip()
                and isinstance(material_type, str)
                and "/" in material_type
            ):
                normalized["media_type"] = material_type
            normalized_materials.append(normalized)
        payload = {**payload, "materials": normalized_materials}
    if isinstance(payload, dict):
        payload = {
            **payload,
            "theme_id": normalize_theme_id(payload.get("theme_id")),
        }
    return PresentationJobRequest.model_validate(payload)


def generate(
    request_path: Path,
    job_root: Path,
    output_path: Path | None = None,
    output_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> dict[str, object]:
    request = _load_request(request_path, workspace_root=workspace_root)
    skill_identity = _skill_identity_from_host_env()
    agent_id = os.environ.get("LANTAI_ACTIVE_AGENT_ID", "").strip()
    if agent_id:
        runtime = PresentationJobRuntime(
            job_root,
            semantic_planner=UtilityPresentationSemanticPlanner(agent_id),
            page_author=UtilityPresentationPageAuthor(agent_id),
            object_edit_planner=UtilityPresentationObjectEditPlanner(agent_id),
            visual_critic=UtilityPresentationVisualCritic(agent_id),
            repair_designer=UtilityPresentationRepairDesigner(agent_id),
        )
    else:
        runtime = PresentationJobRuntime(job_root)
    try:
        if skill_identity is None:
            record = runtime.create_and_start(request)
        else:
            record = runtime.create_and_start(
                request,
                skill_identity=skill_identity,
            )
        # The job owns its deadline.  A fixed five-minute CLI wait could
        # detach an otherwise live generation before its own timeout expires.
        runtime._futures[record.job_id].result(
            timeout=request.timeout_seconds + 30,
        )
        completed = runtime.get(record.job_id, request.conversation_id)
        artifacts = runtime.artifacts(record.job_id, request.conversation_id)
        quality = artifacts.get("quality") or {}
        deck_payload = artifacts.get("deck_ir") or {}
        try:
            generated = runtime.output_path(
                record.job_id,
                request.conversation_id,
            )
        except (FileNotFoundError, RuntimeError, ValueError):
            generated = (
                job_root.resolve()
                / record.job_id
                / "output"
                / "presentation.pptx"
            )
        report: QualityReport | None = None
        deck: DeckIR | None = None
        final_render_paths: tuple[Path, ...] = ()
        try:
            report = QualityReport.model_validate(quality)
            deck = DeckIR.model_validate(deck_payload)
            render_set_id = (
                report.render_set_id or f"round-{report.repair_rounds}"
            )
            if Path(render_set_id).name != render_set_id:
                raise ValueError("invalid render evidence directory")
            final_render_paths = slide_image_paths(
                job_root.resolve() / record.job_id / "renders" / render_set_id,
            )
        except (ValueError, ValidationError):
            pass
        verified = bool(
            completed.status.value == "completed"
            and report is not None
            and deck is not None
            and is_verified_delivery(
                deck,
                report,
                generated,
                final_render_paths,
            ),
        )
        delivery_status = "passed" if verified else "failed"
        delivered: Path | None = generated if verified else None
        if (
            verified
            and deck is not None
            and report is not None
            and output_path is not None
        ):
            output_path = output_path.resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = output_path.with_suffix(
                f".{record.job_id}.tmp",
            )
            shutil.copy2(generated, temporary)
            if is_verified_delivery(
                deck,
                report,
                temporary,
                final_render_paths,
            ):
                temporary.replace(output_path)
                delivered = output_path
            else:
                temporary.unlink(missing_ok=True)
                verified = False
        elif (
            verified
            and deck is not None
            and report is not None
            and output_dir is not None
        ):
            output_dir = output_dir.resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            title = deck.title or request.user_request
            candidate = unique_presentation_path(output_dir, str(title))
            temporary = candidate.with_suffix(
                f".{record.job_id}.tmp",
            )
            shutil.copy2(generated, temporary)
            if is_verified_delivery(
                deck,
                report,
                temporary,
                final_render_paths,
            ):
                temporary.replace(candidate)
                delivered = candidate
            else:
                temporary.unlink(missing_ok=True)
                verified = False
        if not verified:
            delivery_status = "failed"
            delivered = None
        fallback_destination = (
            output_dir
            if output_dir is not None
            else output_path.parent
            if output_path is not None
            else request_path.parent
        )
        destination_root = (
            delivered.parent
            if delivered is not None
            else fallback_destination.resolve()
        )
        sidecars = _write_delivery_sidecars(
            destination_root=destination_root,
            job_root=job_root,
            job_id=record.job_id,
            delivery_status=delivery_status,
            quality=quality,
            delivered=delivered,
            render_paths=final_render_paths if verified else (),
        )
        if not verified:
            return {
                "job_id": record.job_id,
                "status": "failed",
                "message": (
                    completed.events[-1].message
                    if completed.events
                    else "generation failed"
                ),
                "output": str(delivered) if delivered is not None else None,
                "output_path": (
                    str(delivered) if delivered is not None else None
                ),
                "quality": quality,
                "qa_report": sidecars.get("quality") or None,
                "manifest": sidecars["record"],
            }
        return {
            "job_id": record.job_id,
            "status": delivery_status,
            # run_skill_script recognizes this conventional artifact key.
            "output": str(delivered) if delivered is not None else None,
            "output_path": str(delivered) if delivered is not None else None,
            "quality": quality,
            "qa_report": sidecars.get("quality"),
            "manifest": sidecars.get("record"),
            "editor_handoff": artifacts.get("editor_handoff"),
        }
    finally:
        runtime.shutdown()


def _skill_identity_from_host_env() -> SkillExecutionIdentity | None:
    """Read only the complete host-owned identity exported by the runner."""

    values = {
        "name": os.environ.get("LANTAI_ACTIVE_SKILL_NAME", "").strip(),
        "source": os.environ.get("LANTAI_ACTIVE_SKILL_SOURCE", "").strip(),
        "content_sha256": os.environ.get(
            "LANTAI_ACTIVE_SKILL_SHA256",
            "",
        ).strip(),
        "resolution_priority": os.environ.get(
            "LANTAI_ACTIVE_SKILL_PRIORITY",
            "",
        ).strip(),
        "version_text": os.environ.get(
            "LANTAI_ACTIVE_SKILL_VERSION",
            "",
        ).strip(),
    }
    if not any(values.values()):
        return None
    if not all(
        values[key]
        for key in (
            "name",
            "source",
            "content_sha256",
            "resolution_priority",
        )
    ):
        raise ValueError("incomplete host Skill execution identity")
    return SkillExecutionIdentity.model_validate(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an editable PPTX through Lantai DeckIR",
    )
    parser.add_argument(
        "--request",
        type=Path,
        help="PresentationJobRequest JSON",
    )
    parser.add_argument(
        "--job-root",
        type=Path,
        help="Durable job/checkpoint directory",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path.cwd(),
        help="Root used to resolve and confine relative material paths",
    )
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the accepted request JSON schema and exit",
    )
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--output",
        type=Path,
        help="Explicit final PPTX destination",
    )
    destination.add_argument(
        "--output-dir",
        type=Path,
        help="Directory where a DeckIR-title-derived PPTX filename is created",
    )
    args = parser.parse_args(argv)
    if args.print_schema:
        print(
            json.dumps(
                PresentationJobRequest.model_json_schema(),
                ensure_ascii=False,
            ),
        )
        return 0
    if args.request is None or args.job_root is None:
        parser.error(
            "--request and --job-root are required unless --print-schema is used",
        )
    try:
        result = generate(
            args.request,
            args.job_root,
            args.output,
            args.output_dir,
            workspace_root=args.workspace_root,
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        result = {
            "status": "failed",
            "error": "invalid_presentation_request",
            "message": sanitize_public_output(
                str(exc),
                private_roots=(args.workspace_root, args.request.parent),
            ),
        }
    # A single JSON line is intentionally machine-readable by run_skill_script.
    print(json.dumps(result, ensure_ascii=False))
    return 0 if str(result.get("status")) == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
