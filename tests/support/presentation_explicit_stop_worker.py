# -*- coding: utf-8 -*-
"""Independent worker for the RUN-06 explicit-Stop process boundary."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from PIL import Image

from lantai.presentation.jobs import (
    PresentationJobInterruptedError,
    PresentationJobStore,
)
from lantai.presentation.models import (
    CompositionEngine,
    DeckIR,
    DeckSlide,
    JobStatus,
    PlanningMode,
    PresentationJobRequest,
    TextObject,
    VisualCriticResult,
)
from lantai.presentation.render import RenderResult
from lantai.presentation.service import PresentationService


CONVERSATION_ID = "conversation-run06-explicit-stop"
RUNTIME_RUN_ID = "native-run-run06-explicit-stop"


def _write_marker(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _wait_for_release(path: Path, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise TimeoutError("RUN-06 worker was not released")


class _BlockingCompiler:
    def __init__(
        self,
        delegate,
        *,
        marker: Path,
        release: Path,
        job_id: str,
    ) -> None:
        self.delegate = delegate
        self.marker = marker
        self.release = release
        self.job_id = job_id

    def compile(self, deck: DeckIR, output_path: str | Path):
        result = self.delegate.compile(deck, output_path)
        _write_marker(
            self.marker,
            {
                "pid": os.getpid(),
                "job_id": self.job_id,
                "runtime_run_id": RUNTIME_RUN_ID,
                "blocked_stage": "compile",
                "candidate_path": str(output_path),
                "candidate_exists": Path(output_path).is_file(),
            },
        )
        _wait_for_release(self.release)
        return result


class _BlockingCritic:
    def __init__(self, *, marker: Path, release: Path, job_id: str) -> None:
        self.marker = marker
        self.release = release
        self.job_id = job_id

    def review(self, slides) -> VisualCriticResult:
        _write_marker(
            self.marker,
            {
                "pid": os.getpid(),
                "job_id": self.job_id,
                "runtime_run_id": RUNTIME_RUN_ID,
                "blocked_stage": "critic",
                "critic_input_count": len(slides),
            },
        )
        _wait_for_release(self.release)
        return VisualCriticResult(critic_id="run06-blocking-critic", issues=[])


def _render_one(_source, output_dir, *, timeout=120) -> RenderResult:
    _ = timeout
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    pdf_path = destination / "presentation.pdf"
    pdf_path.write_bytes(b"RUN-06 render evidence\n")
    image_path = destination / "slide-1.png"
    Image.new("RGB", (640, 360), "white").save(image_path)
    return RenderResult(pdf_path=pdf_path, slide_images=(image_path,))


def _deck() -> DeckIR:
    return DeckIR(
        title="RUN-06 显式停止边界",
        slides=[
            DeckSlide(
                id="run06-slide-1",
                layout_id="cover",
                objects=[
                    TextObject(
                        id="run06-title",
                        role="title",
                        text="停止后不得进入新阶段或交付",
                        slot="title",
                    ),
                ],
            )
        ],
    )


def _summary(store: PresentationJobStore, job_id: str) -> dict[str, object]:
    record = store.get(job_id)
    job_dir = store.job_dir(job_id)
    return {
        "pid": os.getpid(),
        "job_id": record.job_id,
        "runtime_run_id": record.runtime_run_id,
        "status": record.status.value,
        "stage": record.stage.value,
        "active_lease_id": record.active_lease_id,
        "event_stages": [event.stage.value for event in record.events],
        "event_statuses": [event.status.value for event in record.events],
        "metric_operations": [metric.operation for metric in record.stage_metrics],
        "metric_outcomes": [metric.outcome for metric in record.stage_metrics],
        "authoritative_output_exists": (
            job_dir / "output" / "presentation.pptx"
        ).is_file(),
        "candidate_count": len(list((job_dir / "candidates").rglob("*.pptx"))),
        "completed_event_count": sum(
            event.status == JobStatus.COMPLETED for event in record.events
        ),
    }


def _compose(state_root: Path, marker: Path, release: Path, stage: str) -> int:
    from lantai.presentation import service as service_module

    service_module.render_pptx = _render_one
    service = PresentationService(
        state_root / "presentation-jobs",
        visual_critic=None,
    )
    record = service.create_job(
        PresentationJobRequest(
            conversation_id=CONVERSATION_ID,
            user_request=f"RUN-06 {stage} 阶段显式停止",
            slide_count=1,
            planning_mode=PlanningMode.DETERMINISTIC_FIXTURE,
            composition_engine=CompositionEngine.LEGACY_PYTHON_PPTX,
            vision_critique_required=stage == "critic",
        ),
        runtime_run_id=RUNTIME_RUN_ID,
    )
    if stage == "compile":
        service.compiler = _BlockingCompiler(
            service.compiler,
            marker=marker,
            release=release,
            job_id=record.job_id,
        )
    else:
        service.visual_critic = _BlockingCritic(
            marker=marker,
            release=release,
            job_id=record.job_id,
        )
    try:
        service.compose(
            record.job_id,
            _deck(),
            runtime_run_id=RUNTIME_RUN_ID,
        )
    except PresentationJobInterruptedError as exc:
        result = _summary(service.store, record.job_id)
        result["interrupted"] = True
        result["error"] = str(exc)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    result = _summary(service.store, record.job_id)
    result["interrupted"] = False
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("compose", "observe"), required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--stage", choices=("compile", "critic"))
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--release", type=Path)
    args = parser.parse_args()

    if args.mode == "observe":
        if not args.job_id:
            parser.error("--job-id is required in observe mode")
        store = PresentationJobStore(
            args.state_root / "presentation-jobs",
            read_only=True,
        )
        print(
            json.dumps(
                _summary(store, args.job_id),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.stage is None or args.marker is None or args.release is None:
        parser.error("--stage, --marker, and --release are required")
    return _compose(args.state_root, args.marker, args.release, args.stage)


if __name__ == "__main__":
    raise SystemExit(main())
