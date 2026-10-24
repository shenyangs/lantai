# -*- coding: utf-8 -*-
"""SRC-02 worker: carry a complete authoring brief across real processes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from time import perf_counter

from lantai.config.context import WorkspaceContext, set_current_workspace_context
from lantai.presentation.jobs import PresentationJobStore
from lantai.presentation.models import DeckIR, DeckPlan, PresentationBrief


REQUEST_FIXTURE = "src02-request-fixture.json"
DESIGN_CAPTURE = "src02-design-capture.json"
INVOKE_CAPTURE = "src02-invoke-capture.json"
RESULT_FILE = "src02-complete-brief-cross-boundary.json"
CONTROLLED_STOP = "SRC02_CONTROLLED_STOP_AFTER_CHECKPOINTS"
TITLE = "Q4取舍"
AUDIENCE = "2026年10月董事会；非技术决策者；会前独立阅读"
FULL_BRIEF = (
    "会议目的：在8分钟内决定是否把Q4资源从功能扩张转向可靠性交付。\n"
    "受众条件：2026年10月董事会，成员为非技术决策者，并会在会前独立阅读。\n"
    "来源边界：只使用已核验的内部交付记录；没有证据时明确留白。\n"
    "主判断：先保住可恢复、可观察的交付，再讨论功能扩张。\n"
    "使用方式：会前阅读形成问题，会上只讨论资源取舍。\n"
    "硬约束：不得编造因果；不得把草稿写成正式通过；保留‘8分钟’与‘会前独立阅读’。"
)
NARRATIVE_ARC = "先给资源取舍结论，再说明边界与不可牺牲项。"
SHARED_NOTES = (
    "讲者边界：本页用于8分钟董事会决策；面对非技术决策者，"
    "同时支持会前独立阅读。不得编造因果；不得把草稿写成正式通过。"
)


def _paths(state_root: Path) -> dict[str, Path]:
    return {
        "fixture": state_root / REQUEST_FIXTURE,
        "design_capture": state_root / DESIGN_CAPTURE,
        "invoke_capture": state_root / INVOKE_CAPTURE,
        "project_root": state_root / "workspace",
        "runtime_root": state_root / "runtime",
        "agent_home": state_root / "agent-home",
    }


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _request_payload() -> dict[str, object]:
    return {
        "title": TITLE,
        "audience": AUDIENCE,
        "brief": FULL_BRIEF,
        "output_filename": "src02-complete-brief.pptx",
        "narrative_arc": NARRATIVE_ARC,
        "slides": [
            {
                "id": "src02-slide-01",
                "layout_id": "cover",
                "speaker_notes": f"{SHARED_NOTES} 开场只给资源取舍结论。",
                "objects": [
                    {
                        "id": "src02-title-01",
                        "kind": "text",
                        "role": "title",
                        "text": "先保可靠性交付，再扩功能",
                        "slot": "title",
                    },
                    {
                        "id": "src02-subtitle-01",
                        "kind": "text",
                        "role": "body",
                        "text": "Q4董事会资源取舍",
                        "slot": "subtitle",
                    },
                ],
            },
            {
                "id": "src02-slide-02",
                "layout_id": "key_judgment",
                "speaker_notes": f"{SHARED_NOTES} 本页明确证据不足处必须留白。",
                "objects": [
                    {
                        "id": "src02-title-02",
                        "kind": "text",
                        "role": "title",
                        "text": "三条不可牺牲的交付边界",
                        "slot": "title",
                    },
                    {
                        "id": "src02-body-02",
                        "kind": "text",
                        "role": "body",
                        "text": "可恢复｜可观察｜草稿与正式通过分离",
                        "slot": "body",
                    },
                ],
            },
        ],
        "storyboard": [
            {
                "purpose": "让董事会先看到资源取舍结论",
                "key_message": "Q4先保可靠性交付，再讨论功能扩张",
                "evidence": ["决策只依赖已核验的内部交付记录"],
                "relationship_type": "thesis",
                "visual_role": "thesis",
                "source_indexes": [],
            },
            {
                "purpose": "交代不能为扩张牺牲的底线",
                "key_message": "可恢复、可观察、验真状态分离是三条边界",
                "evidence": ["无证据处留白，不把草稿伪装为正式通过"],
                "relationship_type": "hierarchy",
                "visual_role": "evidence",
                "source_indexes": [],
            },
        ],
    }


def _phase_prepare(state_root: Path) -> dict[str, object]:
    started = perf_counter()
    paths = _paths(state_root)
    for key in ("project_root", "runtime_root", "agent_home"):
        paths[key].mkdir(parents=True, exist_ok=True)
    payload = _request_payload()
    _write_json(paths["fixture"], payload)
    return {
        "phase": "prepare",
        "pid": os.getpid(),
        "duration_seconds": round(perf_counter() - started, 6),
        "title": payload["title"],
        "audience": payload["audience"],
        "full_brief": payload["brief"],
        "narrative_arc": payload["narrative_arc"],
        "slide_count": len(payload["slides"]),
    }


def _phase_invoke(state_root: Path) -> dict[str, object]:
    started = perf_counter()
    paths = _paths(state_root)
    payload = json.loads(paths["fixture"].read_text(encoding="utf-8"))

    from lantai.agents.tools import presentation as presentation_tool
    from lantai.presentation.design_intelligence import (
        compose_deck_ir_with_design_context as real_design_composer,
    )

    def capture_design(
        deck,
        request,
        brief,
        *,
        plan=None,
        author_direction=None,
    ):
        capture = {
            "title": deck.title,
            "request_user_request": request.user_request,
            "request_audience": request.audience,
            "brief_objective": brief.objective,
            "brief_audience": brief.audience,
            "narrative_arc": plan.narrative_arc if plan is not None else None,
            "storyboard_key_messages": (
                [slide.intent.key_message for slide in plan.slides]
                if plan is not None
                else []
            ),
            "speaker_notes": [slide.speaker_notes for slide in deck.slides],
            "author_direction_supplied": author_direction is not None,
        }
        _write_json(paths["design_capture"], capture)
        return real_design_composer(
            deck,
            request,
            brief,
            plan=plan,
            author_direction=author_direction,
        )

    def controlled_compose(
        service,
        job_id,
        deck,
        *,
        runtime_run_id=None,
    ):
        checkpoint_dir = service.store.job_dir(job_id) / "checkpoints"
        capture = {
            "job_id": job_id,
            "runtime_run_id": runtime_run_id,
            "checkpoint_names": sorted(
                path.name for path in checkpoint_dir.glob("*.json")
            ),
            "speaker_notes": [slide.speaker_notes for slide in deck.slides],
            "controlled_stop": CONTROLLED_STOP,
        }
        _write_json(paths["invoke_capture"], capture)
        raise RuntimeError(CONTROLLED_STOP)

    presentation_tool.compose_deck_ir_with_design_context = capture_design
    presentation_tool.PresentationService.compose = controlled_compose
    presentation_tool.UtilityPresentationVisualCritic = lambda _agent_id: None
    presentation_tool.UtilityPresentationRepairDesigner = lambda _agent_id: None

    context = WorkspaceContext(
        agent_home=paths["agent_home"],
        state_root=paths["runtime_root"],
        project_root=paths["project_root"],
        workspace_grant_id="src02-complete-brief",
        trust_state="trusted",
    )
    set_current_workspace_context(context)
    try:
        response = asyncio.run(presentation_tool.create_presentation(**payload))
    finally:
        set_current_workspace_context(None)

    design_capture = json.loads(
        paths["design_capture"].read_text(encoding="utf-8")
    )
    invoke_capture = json.loads(
        paths["invoke_capture"].read_text(encoding="utf-8")
    )
    response_text = "\n".join(
        str(block.get("text", ""))
        for block in response.content
        if isinstance(block, dict)
    )
    return {
        "phase": "invoke",
        "pid": os.getpid(),
        "duration_seconds": round(perf_counter() - started, 6),
        "response_is_error": response.is_error,
        "response_error_code": response.metadata.get("error_code"),
        "response_job_id": response.metadata.get("job_id"),
        "controlled_stop_observed": CONTROLLED_STOP in response_text,
        "design_capture": design_capture,
        "invoke_capture": invoke_capture,
    }


def _phase_observe(state_root: Path) -> dict[str, object]:
    started = perf_counter()
    paths = _paths(state_root)
    store = PresentationJobStore(
        paths["runtime_root"] / "presentation-jobs",
        read_only=True,
    )
    records = store.list_records("src02-complete-brief")
    if len(records) != 1:
        raise RuntimeError(f"SRC-02 expected one durable job, got {len(records)}")
    record = records[0]
    checkpoint_dir = store.job_dir(record.job_id) / "checkpoints"
    checkpoint_names = sorted(path.name for path in checkpoint_dir.glob("*.json"))
    authored_deck = DeckIR.model_validate_json(
        store.checkpoint_path(
            record.job_id,
            "authored-deck-ir.json",
        ).read_text(encoding="utf-8")
    )
    brief = PresentationBrief.model_validate_json(
        store.checkpoint_path(
            record.job_id,
            "brief.json",
        ).read_text(encoding="utf-8")
    )
    plan = DeckPlan.model_validate_json(
        store.checkpoint_path(
            record.job_id,
            "deck-plan.json",
        ).read_text(encoding="utf-8")
    )
    authored_intent_ids = [slide.intent_id for slide in authored_deck.slides]
    plan_intent_ids = [slide.intent.id for slide in plan.slides]
    output_path = paths["project_root"] / "src02-complete-brief.pptx"
    return {
        "phase": "observe",
        "pid": os.getpid(),
        "duration_seconds": round(perf_counter() - started, 6),
        "job_count": len(records),
        "job_id": record.job_id,
        "job_status": record.status.value,
        "job_stage": record.stage.value,
        "request_user_request": record.request.user_request,
        "request_audience": record.request.audience,
        "brief_objective": brief.objective,
        "brief_audience": brief.audience,
        "plan_narrative_arc": plan.narrative_arc,
        "plan_slide_count": len(plan.slides),
        "authored_slide_count": len(authored_deck.slides),
        "storyboard_key_messages": [
            slide.intent.key_message for slide in plan.slides
        ],
        "speaker_notes": [slide.speaker_notes for slide in authored_deck.slides],
        "authored_intent_ids": authored_intent_ids,
        "plan_intent_ids": plan_intent_ids,
        "intent_ids_match": authored_intent_ids == plan_intent_ids,
        "checkpoint_names": checkpoint_names,
        "output_exists": output_path.exists(),
    }


def _run_phase(state_root: Path, phase: str) -> dict[str, object]:
    if phase == "prepare":
        return _phase_prepare(state_root)
    if phase == "invoke":
        return _phase_invoke(state_root)
    if phase == "observe":
        return _phase_observe(state_root)
    raise ValueError(f"unknown SRC-02 phase: {phase}")


def _invoke_phase(state_root: Path, phase: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--state-root",
            str(state_root),
            "--phase",
            phase,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"SRC-02 {phase} failed: stdout={completed.stdout!r}; "
            f"stderr={completed.stderr!r}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _copy_evidence(source: Path, destination: Path) -> dict[str, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {"path": destination.name, "sha256": _sha256(destination)}


def _write_evidence_bundle(
    state_root: Path,
    result: dict[str, object],
    evidence_dir: Path,
) -> None:
    paths = _paths(state_root)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    store = PresentationJobStore(
        paths["runtime_root"] / "presentation-jobs",
        read_only=True,
    )
    records = store.list_records("src02-complete-brief")
    if len(records) != 1:
        raise RuntimeError("SRC-02 evidence bundle requires exactly one job")
    record = records[0]
    sources = {
        "request_fixture": paths["fixture"],
        "design_capture": paths["design_capture"],
        "invoke_capture": paths["invoke_capture"],
        "job_manifest": store.job_dir(record.job_id) / "manifest.json",
        "authored_deck": store.checkpoint_path(
            record.job_id,
            "authored-deck-ir.json",
        ),
        "brief": store.checkpoint_path(record.job_id, "brief.json"),
        "deck_plan": store.checkpoint_path(record.job_id, "deck-plan.json"),
    }
    result["artifact_files"] = {
        label: _copy_evidence(source, evidence_dir / source.name)
        for label, source in sources.items()
    }
    _write_json(evidence_dir / RESULT_FILE, result)


def run(state_root: Path, evidence_dir: Path | None = None) -> dict[str, object]:
    state_root.mkdir(parents=True, exist_ok=True)
    phase_names = ("prepare", "invoke", "observe")
    phases = {phase: _invoke_phase(state_root, phase) for phase in phase_names}
    result: dict[str, object] = {
        "matrix_id": "SRC-02",
        "minimum_evidence": "cross_boundary",
        "orchestrator_pid": os.getpid(),
        "child_pids": [int(phases[phase]["pid"]) for phase in phase_names],
        "phases": phases,
        "total_phase_seconds": round(
            sum(float(phases[phase]["duration_seconds"]) for phase in phase_names),
            6,
        ),
    }
    if evidence_dir is not None:
        _write_evidence_bundle(state_root, result, evidence_dir)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--phase", choices=("prepare", "invoke", "observe"))
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    result = (
        _run_phase(args.state_root, args.phase)
        if args.phase is not None
        else run(args.state_root, args.evidence_dir)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
