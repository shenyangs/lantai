# -*- coding: utf-8 -*-
"""QA-02 worker: prove final rendered pixels catch post-export geometry drift."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from PIL import Image

from lantai.presentation.jobs import PresentationJobStore
from lantai.presentation.models import (
    CompositionAuthority,
    CompositionEngine,
    DeckIR,
    DeckSlide,
    ObjectFrame,
    PlanningMode,
    PresentationJobRequest,
    ShapeObject,
    TextObject,
    VisualCriticResult,
    VisualIssue,
)
from lantai.presentation.quality import file_sha256, is_verified_delivery
from lantai.presentation.service import PresentationService


CONVERSATION_ID = "conversation-qa02-post-export-shift"
RUNTIME_RUN_ID = "native-run-qa02-post-export-shift"
CRITIC_IDENTITY = "qa02-pixel-position-critic/v1"
TARGET_ID = "qa02-target-marker"
MARKER_HEX = "FF2DAA"
SLIDE_WIDTH_IN = 13.333
SLIDE_HEIGHT_IN = 7.5
EXPECTED_FRAME = ObjectFrame(x=1.25, y=2.25, w=1.6, h=1.2)
SHIFTED_X_IN = 8.75
EMU_PER_INCH = 914400

PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _deck() -> DeckIR:
    return DeckIR(
        id="qa02-post-export-shift-deck",
        title="QA-02 导出后像素检查",
        slides=[
            DeckSlide(
                id="qa02-slide-01",
                layout_id="freeform",
                composition_authority=CompositionAuthority.AUTHOR,
                speaker_notes=(
                    "亮色标记必须位于页面左侧指定框；导出后移位必须阻断交付。"
                ),
                objects=[
                    TextObject(
                        id="qa02-title",
                        role="title",
                        text="导出后的像素必须服从作者 scene",
                        slot="title",
                        frame=ObjectFrame(x=0.8, y=0.55, w=8.8, h=0.8),
                    ),
                    ShapeObject(
                        id=TARGET_ID,
                        shape_type="rect",
                        slot="canvas",
                        frame=EXPECTED_FRAME,
                        fill_color=MARKER_HEX,
                        line_color=MARKER_HEX,
                        line_width=0,
                    ),
                    TextObject(
                        id="qa02-explanation",
                        role="body",
                        text="作者指定位置",
                        slot="body",
                        color="171717",
                        frame=ObjectFrame(x=1.25, y=3.75, w=2.8, h=0.55),
                    ),
                ],
            ),
        ],
    )


def _request() -> PresentationJobRequest:
    return PresentationJobRequest(
        conversation_id=CONVERSATION_ID,
        user_request="生成一页原生演示文稿，并在导出后检查作者对象是否发生像素移位",
        slide_count=1,
        cost_budget_units=12,
        vision_critique_required=True,
        planning_mode=PlanningMode.DETERMINISTIC_FIXTURE,
        composition_engine=CompositionEngine.PPTXGENJS_V3,
    )


def _copy_zip_info(info: ZipInfo) -> ZipInfo:
    copied = ZipInfo(info.filename, date_time=info.date_time)
    copied.compress_type = info.compress_type
    copied.comment = info.comment
    copied.extra = info.extra
    copied.internal_attr = info.internal_attr
    copied.external_attr = info.external_attr
    copied.create_system = info.create_system
    copied.flag_bits = info.flag_bits
    return copied


def _shift_exported_shape(source: Path, destination: Path) -> dict[str, int]:
    """Move one native shape in the serialized PPTX, after DeckIR compilation."""

    replacement = destination.with_suffix(".staged.pptx")
    old_x: int | None = None
    new_x = round(SHIFTED_X_IN * EMU_PER_INCH)
    with ZipFile(source) as reader, ZipFile(
        replacement,
        "w",
        compression=ZIP_DEFLATED,
    ) as writer:
        for info in reader.infolist():
            payload = reader.read(info.filename)
            if info.filename == "ppt/slides/slide1.xml":
                root = ET.fromstring(payload)
                for shape in root.iter(f"{{{PRESENTATION_NS}}}sp"):
                    identity = shape.find(
                        f"./{{{PRESENTATION_NS}}}nvSpPr/"
                        f"{{{PRESENTATION_NS}}}cNvPr",
                    )
                    if identity is None or identity.get("name") != TARGET_ID:
                        continue
                    offset = shape.find(
                        f"./{{{PRESENTATION_NS}}}spPr/"
                        f"{{{DRAWING_NS}}}xfrm/{{{DRAWING_NS}}}off",
                    )
                    if offset is None or offset.get("x") is None:
                        raise RuntimeError("QA-02 target shape has no serialized x offset")
                    old_x = int(offset.get("x", "0"))
                    offset.set("x", str(new_x))
                    break
                if old_x is None:
                    raise RuntimeError("QA-02 target shape not found in exported slide XML")
                payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            writer.writestr(_copy_zip_info(info), payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(replacement, destination)
    return {"old_x_emu": old_x, "new_x_emu": new_x}


def _marker_geometry(path: str | Path) -> dict[str, float | int]:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        points: list[tuple[int, int]] = []
        for y in range(rgb.height):
            for x in range(rgb.width):
                red, green, blue = rgb.getpixel((x, y))
                if red >= 210 and green <= 100 and blue >= 120:
                    points.append((x, y))
        if not points:
            raise RuntimeError("rendered QA-02 marker pixels were not found")
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return {
            "width": rgb.width,
            "height": rgb.height,
            "pixel_count": len(points),
            "left": min(xs),
            "right": max(xs),
            "top": min(ys),
            "bottom": max(ys),
            "center_x": sum(xs) / len(xs),
            "center_y": sum(ys) / len(ys),
        }


class _PixelPositionCritic:
    """Deterministic critic that reads the exact PNG given by production QA."""

    def __init__(self) -> None:
        self.observations: list[dict[str, object]] = []

    def cache_identity(self) -> str:
        return CRITIC_IDENTITY

    def review_slide(self, slide) -> VisualCriticResult:
        geometry = _marker_geometry(slide.rendered_path)
        expected_x = (
            (EXPECTED_FRAME.x + EXPECTED_FRAME.w / 2)
            / SLIDE_WIDTH_IN
            * int(geometry["width"])
        )
        expected_y = (
            (EXPECTED_FRAME.y + EXPECTED_FRAME.h / 2)
            / SLIDE_HEIGHT_IN
            * int(geometry["height"])
        )
        drift_x = abs(float(geometry["center_x"]) - expected_x)
        drift_y = abs(float(geometry["center_y"]) - expected_y)
        tolerance_x = int(geometry["width"]) * 0.025
        tolerance_y = int(geometry["height"]) * 0.025
        shifted = drift_x > tolerance_x or drift_y > tolerance_y
        self.observations.append(
            {
                "rendered_path": slide.rendered_path,
                "rendered_sha256": slide.rendered_sha256,
                "geometry": geometry,
                "expected_center": {"x": expected_x, "y": expected_y},
                "drift_pixels": {"x": drift_x, "y": drift_y},
                "shifted": shifted,
            },
        )
        issues = []
        if shifted:
            issues.append(
                VisualIssue(
                    slide_id=slide.slide_id,
                    severity="error",
                    issue_type="composition",
                    object_ids=[TARGET_ID],
                    description=(
                        "final PPTX render moved the authored marker outside its "
                        "scene position"
                    ),
                    proposed_action="reject the export and recompile the affected page",
                ),
            )
        return VisualCriticResult(critic_id=CRITIC_IDENTITY, issues=issues)

    def review_deck(self, _slides) -> VisualCriticResult:
        return VisualCriticResult(critic_id=CRITIC_IDENTITY, issues=[])


def _report_summary(report, rendered: tuple[Path, ...]) -> dict[str, object]:
    return {
        "passed": report.passed,
        "delivery_status": report.delivery_status,
        "render_evidence": report.render_evidence.value,
        "visual_evidence": report.visual_evidence.value,
        "visual_verified": report.visual_verified,
        "issue_codes": [issue.code for issue in report.issues],
        "blocking_gaps": report.blocking_gaps,
        "renderer_id": report.renderer_id,
        "renderer_version": report.renderer_version,
        "output_sha256": report.output_sha256,
        "render_sha256": [file_sha256(path) for path in rendered],
        "rendered_paths": [str(path) for path in rendered],
        "visual_review_evidence": [
            evidence.model_dump(mode="json")
            for evidence in report.visual_review_evidence
        ],
    }


def run(state_root: Path) -> dict[str, object]:
    critic = _PixelPositionCritic()
    service = PresentationService(state_root, visual_critic=critic)
    record = service.create_job(_request(), runtime_run_id=RUNTIME_RUN_ID)
    deck = _deck()
    job_dir = PresentationJobStore(state_root).job_dir(record.job_id)
    authored = job_dir / "candidates" / "qa02-authored.pptx"
    shifted = job_dir / "candidates" / "qa02-export-shifted.pptx"
    authored.parent.mkdir(parents=True, exist_ok=True)
    compilation = service.compiler_for(CompositionEngine.PPTXGENJS_V3).compile(
        deck,
        authored,
    )
    shift = _shift_exported_shape(authored, shifted)

    authored_report, authored_rendered = service.verify_artifact(
        deck,
        authored,
        job_dir / "renders" / "qa02-authored",
        visual_required=True,
        vision_critique_required=True,
        job_id=record.job_id,
    )
    shifted_report, shifted_rendered = service.verify_artifact(
        deck,
        shifted,
        job_dir / "renders" / "qa02-shifted",
        visual_required=True,
        vision_critique_required=True,
        job_id=record.job_id,
    )
    final_record = service.store.get(record.job_id)
    return {
        "matrix_id": "QA-02",
        "minimum_evidence": "render",
        "pid": os.getpid(),
        "job_id": record.job_id,
        "deck_ir_sha256": file_sha256(
            service.store.write_checkpoint(
                record.job_id,
                "qa02-deck.json",
                deck.model_dump_json(indent=2),
            ),
        ),
        "compile_engine": service.compiler_for(
            CompositionEngine.PPTXGENJS_V3,
        ).engine_id,
        "editable_object_count": compilation.editable_object_count,
        "authored_pptx": str(authored),
        "shifted_pptx": str(shifted),
        "authored_pptx_sha256": file_sha256(authored),
        "shifted_pptx_sha256": file_sha256(shifted),
        "serialized_shift": shift,
        "authored": _report_summary(authored_report, authored_rendered),
        "shifted": _report_summary(shifted_report, shifted_rendered),
        "critic_observations": critic.observations,
        "authored_verified_delivery": is_verified_delivery(
            deck,
            authored_report,
            authored,
            authored_rendered,
        ),
        "shifted_verified_delivery": is_verified_delivery(
            deck,
            shifted_report,
            shifted,
            shifted_rendered,
        ),
        "cost_used_units": final_record.cost_used_units,
        "job_dir_count": len(list(state_root.glob("presentation-*"))),
        "manifest_count": len(
            list(state_root.glob("presentation-*/manifest.json")),
        ),
    }


def _write_evidence_bundle(
    result: dict[str, object],
    evidence_dir: Path,
) -> dict[str, object]:
    """Persist the exact candidate PPTXs, renders, and a portable manifest."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    authored_report = result["authored"]
    shifted_report = result["shifted"]
    if not isinstance(authored_report, dict) or not isinstance(shifted_report, dict):
        raise TypeError("QA-02 report summaries are invalid")
    sources = {
        "authored_pptx": Path(str(result["authored_pptx"])),
        "shifted_pptx": Path(str(result["shifted_pptx"])),
        "authored_render": Path(str(authored_report["rendered_paths"][0])),
        "shifted_render": Path(str(shifted_report["rendered_paths"][0])),
    }
    names = {
        "authored_pptx": "qa02-authored.pptx",
        "shifted_pptx": "qa02-export-shifted.pptx",
        "authored_render": "qa02-authored-slide-1.png",
        "shifted_render": "qa02-export-shifted-slide-1.png",
    }
    artifacts: dict[str, dict[str, str]] = {}
    for label, source in sources.items():
        destination = evidence_dir / names[label]
        shutil.copy2(source, destination)
        artifacts[label] = {
            "path": destination.name,
            "sha256": file_sha256(destination),
        }

    result["authored_pptx"] = names["authored_pptx"]
    result["shifted_pptx"] = names["shifted_pptx"]
    authored_report["rendered_paths"] = [names["authored_render"]]
    shifted_report["rendered_paths"] = [names["shifted_render"]]
    observations = result["critic_observations"]
    if not isinstance(observations, list) or len(observations) != 2:
        raise TypeError("QA-02 critic observations are incomplete")
    authored_observation = observations[0]
    shifted_observation = observations[1]
    if not isinstance(authored_observation, dict) or not isinstance(
        shifted_observation,
        dict,
    ):
        raise TypeError("QA-02 critic observations are invalid")
    authored_observation["rendered_path"] = names["authored_render"]
    shifted_observation["rendered_path"] = names["shifted_render"]
    result["artifact_files"] = artifacts
    evidence_path = evidence_dir / "qa02-post-export-shift-render.json"
    evidence_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    args.state_root.mkdir(parents=True, exist_ok=True)
    result = run(args.state_root)
    if args.evidence_dir is not None:
        result = _write_evidence_bundle(result, args.evidence_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
