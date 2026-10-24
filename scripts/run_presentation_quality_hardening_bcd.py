#!/usr/bin/env python3
"""Production composition diagnostic, not a hand-authored style showcase.

Test B reuses grounded content from Test A, discards authored geometry and
routes identical semantic inputs through the actual planner for each style.
It deliberately makes no claim about raw-material ingestion or visual acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from run_editorial_real_material_quality_hardening import _montage, DATA_INTENTS
from lantai.presentation.design_intelligence import (
    ColorStrategyEngine, enrich_plan_with_design_context, load_style_profile,
    visual_direction_from_context,
)
from lantai.presentation.models import (
    DeckIR, DeckPlan, DeckPlanSlide, PresentationBrief, PresentationDesignContext,
    PresentationDesignProfile, PresentationJobRequest,
)
from lantai.presentation.planner import build_deck_ir
from lantai.presentation.pptx_compiler import NativePptxCompiler
from lantai.presentation.quality import inspect_pptx
from lantai.presentation.render import render_pptx


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def geometry(deck: DeckIR) -> list[str]:
    return [hashlib.sha256(json.dumps([
        {"kind": obj.kind, "frame": obj.frame.model_dump() if obj.frame else None}
        for obj in slide.objects
    ], sort_keys=True).encode()).hexdigest() for slide in deck.slides]


def run(source: Path, output: Path, storyboard: Path | None = None) -> dict:
    if output.exists():
        raise ValueError("Use a fresh output directory; prior evidence is immutable")
    original = DeckIR.model_validate_json(source.read_text())
    # IDs from the authored deck are retained for evidence tracing. Titles,
    # footers and editorial furniture are not fed back as body content.
    planned = []
    for index, slide in enumerate(original.slides, 1):
        intent = DATA_INTENTS[index - 1]
        blocks = [obj.text for obj in slide.objects
                  if obj.kind == "text" and obj.role != "title" and obj.text.strip()
                  and not any(token in obj.id for token in
                              ("footer", "source", "folio", "section", "kicker"))]
        planned.append(DeckPlanSlide(id=slide.id, order=index,
            title=intent.key_message, intent=intent, content_blocks=blocks))
    if storyboard is not None:
        planned = [DeckPlanSlide.model_validate(item)
                   for item in json.loads(storyboard.read_text())]
    request = PresentationJobRequest(conversation_id="quality-hardening-b",
        user_request="依据既有材料说明 WPS 舆情结构与响应证据，不增加事实",
        audience="管理层", slide_count=len(planned), seed=913)
    brief = PresentationBrief(objective=request.user_request,
        audience=request.audience, narrative="波形、风险对象、争议、回应与持续治理")
    output.mkdir(parents=True)
    write(output / "input-materials.json", {
        "original_materials": original.source_refs, "grounded_deck": str(source),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "storyboard": str(storyboard) if storyboard else None,
        "storyboard_sha256": hashlib.sha256(storyboard.read_bytes()).hexdigest() if storyboard else None,
        "scope": "composition generalization; source compression already authored in Test A",
        "slides": [item.model_dump(mode="json") for item in planned]})
    results = []
    for style_id in ("editorial-executive", "minimal-keynote", "data-analytical"):
        destination = output / style_id
        destination.mkdir()
        style = load_style_profile(style_id)
        color = ColorStrategyEngine().resolve(style=style,
            user_request=request.user_request, audience=request.audience,
            objective=brief.objective, narrative=brief.narrative, seed=913)
        profile = PresentationDesignProfile(identity=style_id, source="builtin",
            style_profile=style, color_profile=color)
        context = PresentationDesignContext(profile=profile)
        plan = DeckPlan(title=original.title, narrative_arc=brief.narrative,
            visual_direction=visual_direction_from_context(context), slides=planned)
        plan = enrich_plan_with_design_context(request, brief, plan,
                                              reference_context=context)
        deck = build_deck_ir(request, plan)
        write(destination / "design-context.json", context.model_dump(mode="json"))
        write(destination / "composition-plan.json", plan.model_dump(mode="json"))
        write(destination / "deck-ir.json", deck.model_dump(mode="json"))
        pptx = destination / "production-composition.pptx"
        compiled = NativePptxCompiler().compile(deck, pptx)
        rendered = render_pptx(pptx, destination / "render")
        _montage(rendered.slide_images, destination / "full-montage.png",
                 columns=3, label=style_id + " / PRODUCT PLANNER")
        quality = inspect_pptx(deck, pptx,
                              rendered_slide_count=len(rendered.slide_images))
        item = {"style": style_id, "pptx": str(pptx),
            "montage": str(destination / "full-montage.png"),
            "geometry": geometry(deck), "slides": len(deck.slides),
            "editable_objects": compiled.editable_object_count,
            "structural_quality": quality.model_dump(mode="json"),
            "visual_acceptance": "pending_actual_image_review",
            "art_director_candidates_invoked": False}
        write(destination / "results.json", item)
        results.append(item)
        print(style_id, len(rendered.slide_images), "slides rendered", flush=True)
    comparison = [{"left": a["style"], "right": b["style"],
        "identical_geometry_pages": [i + 1 for i, (x, y) in
            enumerate(zip(a["geometry"], b["geometry"])) if x == y]}
        for i, a in enumerate(results) for b in results[i + 1:]]
    report = {"test": "B production-path diagnostic", "decks": results,
              "geometry_comparison": comparison,
              "c_and_d": "not executed by this diagnostic"}
    write(output / "suite-results.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--storyboard", type=Path,
                        help="Optional explicitly authored semantic input; never model-planning evidence")
    args = parser.parse_args()
    run(args.source.resolve(), args.output.resolve(),
        args.storyboard.resolve() if args.storyboard else None)
