# -*- coding: utf-8 -*-
"""Independent RUN-10 worker for one authoritative PPT budget ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
from pathlib import Path

from lantai.presentation.models import (
    CompositionAuthority,
    InputMaterial,
    JobStage,
    JobStatus,
    PresentationJobRequest,
    PresentationProviderCostStatus,
    VisualCriticResult,
    VisualCriticSlideInput,
)
from lantai.presentation.planner import (
    build_brief,
    build_deck_ir,
    build_deterministic_fixture_plan,
)
from lantai.presentation.runtime import PresentationJobRuntime


CONVERSATION_ID = "conversation-run10-parent-budget"
RUNTIME_RUN_ID = "native-run-run10-parent-budget"
PLANNER_ROUTE = "kimi-codingplan/kimi-for-coding"
AUTHOR_ROUTE = "kimi-codingplan/kimi-for-coding"
CRITIC_IDENTITY = "run10-visual-critic/v1"
CRITIC_ROUTE = "minimax-agent/visual-critic"
SHARED_DEPENDENCIES = {
    "design_context": "run10-design-context",
    "font_inventory": "run10-font-inventory",
}


class _Planner:
    def __init__(self) -> None:
        self.last_model_label: str | None = None

    def plan(self, request, brief, *, design_context=None):
        _ = design_context
        self.last_model_label = PLANNER_ROUTE
        return build_deterministic_fixture_plan(request, brief)


class _PageAuthor:
    def __init__(self) -> None:
        self.last_model_label: str | None = None

    def cache_identity(self) -> str:
        return "run10-native-page-author/v1"

    def author(
        self,
        request,
        brief,
        plan,
        planned_slide,
        scaffold_slide,
        *,
        retry_directive=None,
    ):
        _ = (request, brief, plan, planned_slide, retry_directive)
        self.last_model_label = AUTHOR_ROUTE
        return scaffold_slide.model_copy(
            update={
                "composition_authority": CompositionAuthority.AUTHOR,
                "scene_hash": None,
                "speaker_notes": ("模型调用归入父任务预算，保留真实路由，" "缺少可信账单的费用保持未知。"),
                "objects": [
                    obj
                    for obj in scaffold_slide.objects
                    if getattr(obj, "slot", None)
                    not in {
                        "relationship-marker",
                        "page-marker",
                        "page-number",
                        "step-number",
                        "sequence-number",
                        "numeric-badge",
                    }
                ],
                "object_groups": [],
                "relationships": [],
            },
        )


class _VisualCritic:
    def __init__(self, ledger: Path) -> None:
        self.ledger = ledger
        self._lock = threading.Lock()

    def cache_identity(self) -> str:
        return CRITIC_IDENTITY

    def _record(self, payload: dict[str, object]) -> None:
        with self._lock:
            with self.ledger.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    + "\n",
                )
                handle.flush()
                os.fsync(handle.fileno())

    def review_slide(self, slide) -> VisualCriticResult:
        self._record({"scope": "slide", "slide_id": slide.slide_id})
        return VisualCriticResult(critic_id=CRITIC_ROUTE, issues=[])

    def review_deck(self, slides) -> VisualCriticResult:
        self._record({"scope": "deck", "slide_count": len(slides)})
        return VisualCriticResult(critic_id=CRITIC_ROUTE, issues=[])


def _request() -> PresentationJobRequest:
    return PresentationJobRequest(
        conversation_id=CONVERSATION_ID,
        user_request="生成一份三页、原生可编辑且经过逐页审查的预算验收演示",
        slide_count=3,
        cost_budget_units=50,
        materials=[
            InputMaterial(
                name="RUN-10预算材料.md",
                content=(
                    "# 预算验收\n"
                    "## 判断\n所有模型调用都必须进入同一个父任务预算。\n"
                    "## 路由\n每一笔调用必须保留真实模型路由。\n"
                    "## 费用\n无法取得可信账单时必须记为未知，不能记为零。"
                ),
            ),
        ],
    )


def _render_inputs(state_root: Path, deck) -> list[VisualCriticSlideInput]:
    render_root = state_root / "rendered"
    render_root.mkdir(parents=True, exist_ok=True)
    inputs: list[VisualCriticSlideInput] = []
    deck_context = [slide.speaker_notes for slide in deck.slides]
    for index, slide in enumerate(deck.slides, start=1):
        path = render_root / f"slide-{index:02d}.png"
        payload = f"RUN-10 deterministic render {slide.id}\n".encode("utf-8")
        path.write_bytes(payload)
        inputs.append(
            VisualCriticSlideInput(
                slide_id=slide.id,
                slide_index=index,
                rendered_path=str(path),
                rendered_sha256=hashlib.sha256(payload).hexdigest(),
                key_message=slide.speaker_notes or f"第 {index} 页",
                object_ids=[obj.id for obj in slide.objects],
                full_deck_context=deck_context,
            ),
        )
    return inputs


def _execute(state_root: Path) -> dict[str, object]:
    critic_ledger = state_root / "critic-calls.jsonl"
    planner = _Planner()
    author = _PageAuthor()
    critic = _VisualCritic(critic_ledger)
    runtime = PresentationJobRuntime(
        state_root / "presentation-jobs",
        semantic_planner=planner,
        page_author=author,
        visual_critic=critic,
    )
    try:
        record = runtime.service.create_job(
            _request(),
            runtime_run_id=RUNTIME_RUN_ID,
        )
        request = record.request
        with runtime.service.store.writer_lease(
            record.job_id,
            runtime_run_id=RUNTIME_RUN_ID,
        ):
            runtime.service.store.append_event(
                record.job_id,
                stage=JobStage.BRIEF,
                status=JobStatus.RUNNING,
                message="RUN-10 开始父预算验收",
            )
            brief = build_brief(request)
            design_context = getattr(
                runtime,
                "_resolve_job_design_context",
            )(  # noqa: SLF001
                record.job_id,
                request,
                brief,
            )
            planner.last_model_label = None
            with getattr(runtime, "_measure_stage")(  # noqa: SLF001
                record.job_id,
                stage=JobStage.PLAN,
                operation="plan",
                route_id=type(planner).__name__,
                route_resolver=lambda: planner.last_model_label,
                cost_units=10,
                provider_cost_status=PresentationProviderCostStatus.UNKNOWN,
            ):
                runtime.service.store.consume_cost(record.job_id, 10)
                plan = getattr(runtime, "_semantic_plan")(  # noqa: SLF001
                    request,
                    brief,
                    design_context,
                )
            deck = getattr(runtime, "_author_pages")(  # noqa: SLF001
                record.job_id,
                request,
                brief,
                plan,
                build_deck_ir(request, plan),
            )
            slides = _render_inputs(state_root, deck)
            first_review = getattr(
                runtime.service,
                "_review_visuals_with_checkpoints",
            )(  # noqa: SLF001
                slides,
                job_id=record.job_id,
                repair_rounds=0,
                shared_dependencies=SHARED_DEPENDENCIES,
            )
            second_review = getattr(
                runtime.service,
                "_review_visuals_with_checkpoints",
            )(  # noqa: SLF001
                slides,
                job_id=record.job_id,
                repair_rounds=0,
                shared_dependencies=SHARED_DEPENDENCIES,
            )
            assert first_review is not None
            assert second_review is not None
            runtime.service.store.append_event(
                record.job_id,
                stage=JobStage.QUALITY,
                status=JobStatus.RUNNING,
                message="RUN-10 父预算、路由与缓存复用证据已持久化",
            )
        final = runtime.service.store.get(record.job_id)
        return {
            "job_id": record.job_id,
            "runtime_run_id": final.runtime_run_id,
            "worker_pid": os.getpid(),
            "cost_used_units": final.cost_used_units,
            "stage_metric_count": len(final.stage_metrics),
            "critic_call_count": len(
                critic_ledger.read_text(encoding="utf-8").splitlines(),
            ),
        }
    finally:
        runtime.shutdown()


def _observe(state_root: Path, job_id: str) -> dict[str, object]:
    from lantai.presentation.jobs import PresentationJobStore

    root = state_root / "presentation-jobs"
    store = PresentationJobStore(root, read_only=True)
    record = store.get(job_id)
    metrics = [
        metric.model_dump(mode="json") for metric in record.stage_metrics
    ]
    charged = [
        metric for metric in record.stage_metrics if metric.cost_units > 0
    ]
    critic_metrics = [
        metric
        for metric in record.stage_metrics
        if metric.operation == "critic"
    ]
    job_dirs = [path for path in root.iterdir() if path.is_dir()]
    return {
        "observer_pid": os.getpid(),
        "job_dir_count": len(job_dirs),
        "manifest_count": len(list(root.glob("*/manifest.json"))),
        "runtime_run_id": record.runtime_run_id,
        "active_lease_id": record.active_lease_id,
        "status": record.status.value,
        "stage": record.stage.value,
        "cost_budget_units": record.cost_budget_units,
        "cost_used_units": record.cost_used_units,
        "charged_metric_cost_units": sum(item.cost_units for item in charged),
        "charged_metric_count": len(charged),
        "charged_routes": [item.route_id for item in charged],
        "charged_provider_cost_statuses": [
            item.provider_cost_status.value for item in charged
        ],
        "charged_provider_cost_micro_cny": [
            item.provider_cost_micro_cny for item in charged
        ],
        "critic_metric_cost_units": [
            item.cost_units for item in critic_metrics
        ],
        "critic_metric_provider_cost_statuses": [
            item.provider_cost_status.value for item in critic_metrics
        ],
        "provider_amount_zero_count": sum(
            1
            for item in record.stage_metrics
            if item.provider_cost_micro_cny == 0
        ),
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("execute", "observe"),
        required=True,
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--job-id")
    arguments = parser.parse_args()
    arguments.state_root.mkdir(parents=True, exist_ok=True)
    if arguments.mode == "execute":
        result = _execute(arguments.state_root)
    else:
        if not arguments.job_id:
            parser.error("--job-id is required for observe")
        result = _observe(arguments.state_root, arguments.job_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
