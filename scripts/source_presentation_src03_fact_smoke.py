#!/usr/bin/env python3
"""Isolated real-provider evidence for presentation acceptance SRC-03.

The harness copies the selected desktop profile and encrypted provider store
to a temporary directory, invokes the production semantic planner, and keeps
the request plus model-authored plan in memory only.  Persisted evidence is a
content-free set of deterministic factual assertions, hashes, counts, route,
and timing metadata.
"""

from __future__ import annotations

import argparse
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
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
CONFIGURED_PROVIDER_SMOKE_PATH = (
    PROJECT_ROOT / "scripts" / "source_configured_provider_stream_smoke.py"
)
if TYPE_CHECKING:
    from lantai.presentation.models import DeckPlan


_QUARTERS = ("Q1", "Q2", "Q3", "Q4")
_NEGATIONS = ("不", "非", "未", "没有", "不能", "不可", "无法", "不得", "禁止")
_SAFE_ERROR_CODES = {
    "semantic planner exceeded the slide-count ceiling": (
        "semantic_plan_slide_count_exceeded"
    ),
    "semantic outline must return the exact requested slide count": (
        "semantic_plan_slide_count_mismatch"
    ),
    "semantic planner returned an unknown source reference": (
        "semantic_plan_unknown_source_reference"
    ),
    "planned chart requires data, source references, derivation and unit": (
        "semantic_plan_incomplete_chart_evidence"
    ),
    "planned chart returned an unknown source reference": (
        "semantic_plan_chart_unknown_source_reference"
    ),
    "semantic planner invented immutable data": (
        "semantic_plan_unknown_immutable_fact"
    ),
    "semantic planner returned prohibited filler content": (
        "semantic_plan_prohibited_filler"
    ),
    "every grounded slide requires a source reference": (
        "semantic_plan_ungrounded_slide"
    ),
    "semantic detail batch must return exact target orders": (
        "semantic_plan_detail_order_set_mismatch"
    ),
    "semantic detail omitted required claim text": (
        "semantic_plan_detail_claim_omitted"
    ),
    "semantic detail changed or omitted chart contract": (
        "semantic_plan_detail_chart_contract_changed"
    ),
    "semantic claim ledger references unknown delivery contract ids": (
        "semantic_plan_unknown_delivery_contract"
    ),
    "semantic claim ledger must cover every required delivery contract": (
        "semantic_plan_required_delivery_omitted"
    ),
    "semantic claim delivery contract exceeds source scope": (
        "semantic_plan_delivery_source_scope"
    ),
    "semantic claim omitted required delivery value": (
        "semantic_plan_required_delivery_value_omitted"
    ),
    "semantic claim omitted required delivery semantics": (
        "semantic_plan_required_delivery_semantics_omitted"
    ),
}


def _semantic_outer_timeout_seconds(
    *,
    per_call_timeout: float,
    slide_count: int,
    detail_batch_size: int,
    detail_concurrency: int,
    claim_attempt_limit: int,
    outline_attempt_limit: int,
    detail_attempt_limit: int,
) -> float:
    """Cover every bounded serial stage and concurrent detail retry wave."""

    detail_batches = math.ceil(slide_count / detail_batch_size)
    detail_waves = math.ceil(detail_batches / detail_concurrency)
    call_waves = (
        claim_attempt_limit
        + outline_attempt_limit
        + detail_attempt_limit * detail_waves
    )
    return per_call_timeout * call_waves + 30


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


def _file_summary(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "name": path.name,
        "sha256": _sha256_bytes(content),
        "bytes": len(content),
    }


def _plan_text(plan: "DeckPlan") -> str:
    parts = [plan.title, plan.narrative_arc]
    for slide in plan.slides:
        parts.extend(
            [
                slide.title,
                slide.intent.purpose,
                slide.intent.key_message,
                *slide.intent.evidence,
                *slide.intent.required_facts,
                *slide.intent.immutable_facts,
                *slide.content_blocks,
                slide.takeaway,
            ]
        )
        for chart in slide.charts:
            parts.extend([*chart.series.keys(), chart.unit, chart.derivation])
    return "\n".join(str(part) for part in parts if str(part).strip())


def _number_equal(values: list[float], expected: list[float]) -> bool:
    return len(values) == len(expected) and all(
        abs(float(actual) - wanted) < 1e-6
        for actual, wanted in zip(values, expected, strict=True)
    )


def _quarter_categories(categories: list[str]) -> bool:
    normalized = [re.sub(r"\s+", "", value).upper() for value in categories]
    return len(normalized) == 4 and all(
        value.endswith(quarter)
        for value, quarter in zip(normalized, _QUARTERS, strict=True)
    )


def _contains_near(text: str, label: str, value: str, span: int = 28) -> bool:
    escaped_label = re.escape(label)
    escaped_value = re.escape(value)
    return bool(
        re.search(
            rf"(?:{escaped_label}.{{0,{span}}}{escaped_value}|"
            rf"{escaped_value}.{{0,{span}}}{escaped_label})",
            text,
            flags=re.DOTALL,
        )
    )


def _has_unnegated_profit_growth(text: str) -> bool:
    for match in re.finditer(r"(?:净)?利润.{0,10}(?:增长|上升|改善)", text):
        context = text[max(0, match.start() - 6) : match.end()]
        if not any(token in context for token in _NEGATIONS):
            return True
    return False


def _has_unsupported_customer_metric(text: str) -> bool:
    uncertainty = (
        "待补充",
        "待核实",
        "待验证",
        "未提供",
        "暂无",
        "缺少",
        "缺失",
        "未知",
        "数据不足",
        "无法判断",
        "不能判断",
        "不可推断",
        "不能推出",
    )
    asserted = re.compile(
        r"(?:为|达到|达|增长|上升|提升|下降|回落|改善|恶化|较高|较低|"
        r"良好|不佳|\d)",
    )
    for clause in re.split(r"[。；;\n]", text):
        if any(metric in clause for metric in ("活跃人数", "留存", "LTV")):
            if any(token in clause for token in (*_NEGATIONS, *uncertainty)):
                continue
            if asserted.search(clause):
                return True
    return False


def _evaluate_src03_plan(plan: "DeckPlan") -> dict[str, Any]:
    """Return deterministic, content-free SRC-03 acceptance assertions."""

    text = re.sub(r"[,，\s]+", "", _plan_text(plan))
    charts = [chart for slide in plan.slides for chart in slide.charts]
    grounded_slide_count = sum(
        1 for slide in plan.slides if slide.intent.source_refs
    )

    has_revenue_cost_chart = False
    has_contribution_chart = False
    monetary_units_preserved = True
    for chart in charts:
        if not _quarter_categories(chart.categories):
            continue
        revenue_values = next(
            (values for name, values in chart.series.items() if "收入" in name),
            None,
        )
        cost_values = next(
            (values for name, values in chart.series.items() if "成本" in name),
            None,
        )
        contribution_values = next(
            (
                values
                for name, values in chart.series.items()
                if "经营贡献" in name or name == "贡献"
            ),
            None,
        )
        if revenue_values is not None or cost_values is not None or contribution_values is not None:
            monetary_units_preserved = monetary_units_preserved and chart.unit == "万元"
        if (
            revenue_values is not None
            and cost_values is not None
            and _number_equal(revenue_values, [120, 145, 160, 175])
            and _number_equal(cost_values, [95, 108, 126, 150])
        ):
            has_revenue_cost_chart = True
        if contribution_values is not None and _number_equal(
            contribution_values,
            [25, 37, 34, 25],
        ):
            has_contribution_chart = True

    revenue_growth_correct = bool(
        re.search(
            r"Q4.{0,30}(?:收入|营收).{0,20}(?:9\.375%|9\.4%)",
            text,
        )
        or re.search(
            r"(?:9\.375%|9\.4%).{0,30}(?:Q4).{0,20}(?:收入|营收)",
            text,
        )
    )
    contribution_change_correct = bool(
        re.search(
            r"Q4.{0,30}(?:经营)?贡献.{0,20}(?:减少|下降|回落).{0,8}9万元",
            text,
        )
        or re.search(
            r"(?:经营)?贡献.{0,25}Q4.{0,20}(?:减少|下降|回落).{0,8}9万元",
            text,
        )
    )
    revenue_up_claim = bool(
        re.search(r"(?:收入|营收).{0,16}(?:增长|上升|增加|走高)", text)
    )
    contribution_down_claim = bool(
        re.search(r"(?:经营)?贡献.{0,18}(?:下降|减少|回落|收窄)", text)
    )
    annual_values = all(
        (
            _contains_near(text, label, value)
            or _contains_near(text, f"全年{label}", value)
        )
        for label, value in (("收入", "600"), ("成本", "479"), ("经营贡献", "121"))
    )
    formula_correct = bool(
        "收入减成本" in text
        or re.search(r"600(?:−|-|－)479(?:=|＝)121", text)
    )
    cause_uncertainty = bool(
        re.search(
            r"(?:成本|原因).{0,24}(?:待核实|需核实|待验证|未知|未提供|"
            r"不能判断|无法判断|尚不能归因|不可归因)",
            text,
        )
    )

    assertions = {
        "requested_slide_count_met": len(plan.slides) == 10,
        "every_slide_grounded": grounded_slide_count == len(plan.slides),
        "native_revenue_cost_trend_chart": has_revenue_cost_chart,
        "native_quarter_contribution_chart": has_contribution_chart,
        "monetary_chart_units_preserved": monetary_units_preserved,
        "annual_totals_and_formula_correct": annual_values and formula_correct,
        "q4_revenue_growth_correct": revenue_growth_correct,
        "q4_contribution_change_correct": contribution_change_correct,
        "core_judgment_revenue_up_contribution_down": (
            revenue_up_claim and contribution_down_claim
        ),
        "no_positive_profit_growth_claim": not _has_unnegated_profit_growth(text),
        "cost_cause_uncertainty_preserved": cause_uncertainty,
        "actions_marked_as_recommendations": "建议" in text,
        "no_customer_metric_extrapolation": not _has_unsupported_customer_metric(text),
    }
    failed = sorted(name for name, passed in assertions.items() if not passed)
    return {
        "ok": not failed,
        "assertions": assertions,
        "failed_assertions": failed,
        "metrics": {
            "slide_count": len(plan.slides),
            "chart_count": len(charts),
            "grounded_slide_count": grounded_slide_count,
        },
    }


def _build_safe_evidence(
    *,
    source_head: str,
    provider_id: str,
    model: str,
    request_sha256: str,
    material_summaries: list[dict[str, Any]],
    evaluation: dict[str, Any],
    elapsed_ms: int,
    timeout_seconds: float,
    output_token_budget: int,
    stage_output_token_budgets: dict[str, int],
    stage_timings_ms: dict[str, int],
    stage_attempt_counts: dict[str, int],
    utility_call_count: int,
    estimated_cost_units: int,
    validated_plan_json_bytes: int,
    model_label_sha256: str | None = None,
) -> dict[str, Any]:
    metrics = dict(evaluation.get("metrics") or {})
    metrics["validated_plan_json_bytes"] = validated_plan_json_bytes
    evaluation = {**evaluation, "metrics": metrics}
    return {
        "schema_version": "1.0",
        "run_id": "SRC-03",
        "ok": bool(evaluation.get("ok")),
        "source_head": source_head,
        "provider": {
            "provider_id": provider_id,
            "model": model,
            "route": f"{provider_id}/{model}",
            "selectable_before_call": True,
            "model_label_sha256": model_label_sha256,
        },
        "billing": {
            "basis": "user_subscription",
            "per_run_monetary_cost": "unavailable",
        },
        "input": {
            "transport": "runtime_material_paths",
            "sha256": request_sha256,
            "persisted": False,
            "materials": material_summaries,
        },
        "evaluation": evaluation,
        "generation_limits": {
            "max_output_tokens_total": output_token_budget,
            "stages": stage_output_token_budgets,
        },
        "budget": {
            "estimated_existing_cost_units": estimated_cost_units,
            "actual_existing_cost_units": utility_call_count * 10,
            "utility_call_count": utility_call_count,
            "stage_attempt_counts": stage_attempt_counts,
        },
        "timing_ms": {
            "semantic_plan_and_validation": elapsed_ms,
            "configured_timeout_per_call": round(timeout_seconds * 1000),
            "stages": stage_timings_ms,
        },
        "privacy": {
            "credentials_persisted": False,
            "model_text_persisted": False,
            "prompt_text_persisted": False,
        },
    }


def _inner_run(spec: dict[str, Any], *, agent_id: str, timeout: float) -> dict[str, Any]:
    from lantai.config.config import load_agent_config
    from lantai.presentation.intelligence import UtilityPresentationSemanticPlanner
    from lantai.presentation.models import InputMaterial, PresentationJobRequest
    from lantai.presentation.planner import build_brief
    from lantai.providers.provider_manager import ProviderManager

    brief_path = Path(str(spec["brief_path"])).expanduser().resolve()
    data_path = Path(str(spec["data_path"])).expanduser().resolve()
    user_request = brief_path.read_text(encoding="utf-8")
    request = PresentationJobRequest(
        conversation_id=f"src03-provider-{uuid.uuid4().hex}",
        user_request=user_request,
        audience="管理层",
        slide_count=10,
        timeout_seconds=max(30, int(timeout)),
        materials=[
            InputMaterial(id="d03-brief", name=brief_path.name, path=str(brief_path)),
            InputMaterial(
                id="d03-quarter-data",
                name=data_path.name,
                path=str(data_path),
                media_type="application/json",
            ),
        ],
    )
    manager = ProviderManager.get_instance()
    active = load_agent_config(agent_id).active_model or manager.get_active_model()
    if active is None or not manager.is_chat_model_slot_selectable(active):
        raise RuntimeError("configured chat model is not selectable")
    expected = (str(spec["expected_provider_id"]), str(spec["expected_model"]))
    actual = (active.provider_id, active.model)
    if actual != expected:
        raise RuntimeError("configured provider route does not match SRC-03 request")

    brief = build_brief(request)
    started = time.monotonic()
    planner = UtilityPresentationSemanticPlanner(
        agent_id,
        timeout_seconds=timeout,
    )
    try:
        plan = planner.plan(request, brief)
    except BaseException as exc:
        exc.presentation_stage_timings_ms = dict(
            planner.last_stage_timings_ms,
        )
        exc.presentation_stage_output_token_budgets = dict(
            planner.last_stage_output_token_budgets,
        )
        exc.presentation_stage_attempt_counts = dict(
            planner.last_stage_attempt_counts,
        )
        exc.presentation_utility_call_count = planner.last_utility_call_count
        raise
    elapsed_ms = round((time.monotonic() - started) * 1000)
    evaluation = _evaluate_src03_plan(plan)
    validated_plan_json_bytes = len(
        plan.model_dump_json().encode("utf-8"),
    )
    model_label_sha256 = (
        _sha256_bytes(planner.last_model_label.encode("utf-8"))
        if planner.last_model_label
        else None
    )
    return {
        "evaluation": evaluation,
        "elapsed_ms": elapsed_ms,
        "model_label_sha256": model_label_sha256,
        "output_token_budget": int(planner.last_output_token_budget or 0),
        "stage_output_token_budgets": dict(
            planner.last_stage_output_token_budgets,
        ),
        "stage_timings_ms": dict(planner.last_stage_timings_ms),
        "stage_attempt_counts": dict(planner.last_stage_attempt_counts),
        "utility_call_count": planner.last_utility_call_count,
        "estimated_cost_units": planner.estimated_cost_units(request),
        "validated_plan_json_bytes": validated_plan_json_bytes,
    }


def _safe_failure(exc: BaseException) -> dict[str, Any]:
    value = f"{type(exc).__name__}:{exc}"
    issues: list[dict[str, Any]] = []
    error_origin: dict[str, Any] | None = None
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for frame in traceback.extract_tb(current.__traceback__):
            try:
                relative = Path(frame.filename).resolve().relative_to(PROJECT_ROOT)
            except (OSError, ValueError):
                continue
            if not relative.parts or relative.parts[0] not in {"src", "scripts"}:
                continue
            if frame.name.startswith("<"):
                continue
            error_origin = {
                "file": relative.as_posix(),
                "function": frame.name,
                "line": int(frame.lineno),
            }
        errors = getattr(current, "errors", None)
        if callable(errors):
            try:
                for item in errors():
                    if not isinstance(item, dict):
                        continue
                    raw_loc = item.get("loc", ())
                    issues.append(
                        {
                            "loc": [
                                value
                                for value in raw_loc
                                if isinstance(value, (str, int))
                            ],
                            "type": str(item.get("type") or "unknown"),
                        }
                    )
            except Exception:
                issues = []
            break
        current = current.__cause__ or current.__context__
    result = {
        "ok": False,
        "error_type": type(exc).__name__,
        "error_sha256": _sha256_bytes(value.encode("utf-8")),
    }
    error_code = _SAFE_ERROR_CODES.get(str(exc))
    if error_code is not None:
        result["error_code"] = error_code
    if error_origin is not None:
        result["error_origin"] = error_origin
    phase = getattr(exc, "phase", None)
    timeout_seconds = getattr(exc, "timeout_seconds", None)
    if isinstance(phase, str) and phase in {"first_output", "idle", "total"}:
        result["timeout_phase"] = phase
    if isinstance(timeout_seconds, (int, float)):
        result["timeout_seconds"] = float(timeout_seconds)
    if issues:
        result["validation_issues"] = issues
    numeric_token = getattr(exc, "presentation_numeric_token", None)
    numeric_location = getattr(exc, "presentation_numeric_location", None)
    if isinstance(numeric_token, str) and isinstance(numeric_location, str):
        if re.fullmatch(
            r"(?:deck|slide-\d{3}|outline-slide-\d{3}|"
            r"claim-ledger-claim-\d{3})",
            numeric_location,
        ):
            result["error_code"] = "semantic_plan_uncited_numeric_claim"
            result["numeric_location"] = numeric_location
            result["numeric_token_sha256"] = _sha256_bytes(
                numeric_token.encode("utf-8"),
            )
    batch_stage = getattr(exc, "presentation_detail_batch_stage", None)
    target_orders = getattr(exc, "presentation_detail_target_orders", None)
    returned_orders = getattr(exc, "presentation_detail_returned_orders", None)
    safe_order_lists = (
        isinstance(target_orders, list)
        and isinstance(returned_orders, list)
        and len(target_orders) <= 8
        and len(returned_orders) <= 8
        and all(
            isinstance(order, int) and not isinstance(order, bool) and 1 <= order <= 999
            for order in [*target_orders, *returned_orders]
        )
    )
    if (
        isinstance(batch_stage, str)
        and re.fullmatch(r"detail:\d{3}-\d{3}", batch_stage)
        and safe_order_lists
    ):
        result["detail_batch_stage"] = batch_stage
        result["detail_target_orders"] = target_orders
        result["detail_returned_orders"] = returned_orders
    stage_timings = getattr(exc, "presentation_stage_timings_ms", None)
    if isinstance(stage_timings, dict):
        result["stage_timings_ms"] = {
            str(key): int(value)
            for key, value in stage_timings.items()
            if isinstance(key, str) and isinstance(value, int)
        }
    stage_budgets = getattr(
        exc,
        "presentation_stage_output_token_budgets",
        None,
    )
    if isinstance(stage_budgets, dict):
        result["stage_output_token_budgets"] = {
            str(key): int(value)
            for key, value in stage_budgets.items()
            if isinstance(key, str) and isinstance(value, int)
        }
    stage_attempts = getattr(exc, "presentation_stage_attempt_counts", None)
    if isinstance(stage_attempts, dict):
        result["stage_attempt_counts"] = {
            str(key): int(value)
            for key, value in stage_attempts.items()
            if (
                isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= 3
            )
        }
    utility_call_count = getattr(exc, "presentation_utility_call_count", None)
    if (
        isinstance(utility_call_count, int)
        and not isinstance(utility_call_count, bool)
        and 0 <= utility_call_count <= 100
    ):
        result["utility_call_count"] = utility_call_count
    return result


def run(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
    brief_path: Path,
    data_path: Path,
    expected_provider_id: str,
    expected_model: str,
    agent_id: str,
    timeout: float,
) -> dict[str, Any]:
    brief_path = brief_path.expanduser().resolve()
    data_path = data_path.expanduser().resolve()
    material_summaries = [_file_summary(brief_path), _file_summary(data_path)]
    request_manifest = {
        "brief_path_sha256": _sha256_bytes(str(brief_path).encode("utf-8")),
        "data_path_sha256": _sha256_bytes(str(data_path).encode("utf-8")),
        "material_sha256": [item["sha256"] for item in material_summaries],
        "expected_provider_id": expected_provider_id,
        "expected_model": expected_model,
        "slide_count": 10,
    }
    request_sha256 = _sha256_bytes(
        json.dumps(
            request_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    provider_smoke = _load_configured_provider_smoke()
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-src03-provider-"))
    try:
        working_dir, secret_dir, configured = provider_smoke._stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(),
            source_secret_dir=source_secret_dir.expanduser().resolve(),
            agent_id=agent_id,
            target_root=smoke_root,
        )
        configured_route = (
            str(configured.get("provider_id") or ""),
            str(configured.get("model") or ""),
        )
        if configured_route != (expected_provider_id, expected_model):
            raise RuntimeError("desktop configured route does not match SRC-03 request")

        spec = {
            "brief_path": str(brief_path),
            "data_path": str(data_path),
            "expected_provider_id": expected_provider_id,
            "expected_model": expected_model,
        }
        env = {
            **os.environ,
            "LANTAI_WORKING_DIR": str(working_dir),
            "LANTAI_SECRET_DIR": str(secret_dir),
            "COPAW_WORKING_DIR": str(working_dir),
            "COPAW_SECRET_DIR": str(secret_dir),
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "PYTHONPATH": str(SOURCE_ROOT),
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--inner",
                "--agent-id",
                "default",
                "--timeout",
                str(timeout),
            ],
            input=json.dumps(spec, ensure_ascii=False),
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=_semantic_outer_timeout_seconds(
                per_call_timeout=timeout,
                slide_count=request_manifest["slide_count"],
                detail_batch_size=4,
                detail_concurrency=2,
                claim_attempt_limit=3,
                outline_attempt_limit=3,
                detail_attempt_limit=3,
            ),
            check=False,
        )
        stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not stdout_lines:
            raise RuntimeError("isolated SRC-03 child returned no result")
        child = json.loads(stdout_lines[-1])
        if completed.returncode != 0 or not child.get("ok"):
            return {
                "schema_version": "1.0",
                "run_id": "SRC-03",
                "ok": False,
                "source_head": _source_head(),
                "provider": {
                    "route": f"{expected_provider_id}/{expected_model}",
                },
                "input": {
                    "sha256": request_sha256,
                    "persisted": False,
                    "materials": material_summaries,
                },
                "child": {
                    "returncode": completed.returncode,
                    "error_type": child.get("error_type"),
                    "error_code": child.get("error_code"),
                    "error_origin": child.get("error_origin"),
                    "error_sha256": child.get("error_sha256"),
                    "validation_issues": child.get("validation_issues", []),
                    "timeout_phase": child.get("timeout_phase"),
                    "timeout_seconds": child.get("timeout_seconds"),
                    "numeric_location": child.get("numeric_location"),
                    "numeric_token_sha256": child.get(
                        "numeric_token_sha256",
                    ),
                    "detail_batch_stage": child.get("detail_batch_stage"),
                    "detail_target_orders": child.get(
                        "detail_target_orders",
                        [],
                    ),
                    "detail_returned_orders": child.get(
                        "detail_returned_orders",
                        [],
                    ),
                    "stage_timings_ms": child.get("stage_timings_ms", {}),
                    "stage_output_token_budgets": child.get(
                        "stage_output_token_budgets",
                        {},
                    ),
                    "stage_attempt_counts": child.get(
                        "stage_attempt_counts",
                        {},
                    ),
                    "utility_call_count": child.get("utility_call_count", 0),
                    "stderr_bytes": len(completed.stderr.encode("utf-8")),
                    "stderr_sha256": _sha256_bytes(completed.stderr.encode("utf-8")),
                },
                "privacy": {
                    "credentials_persisted": False,
                    "model_text_persisted": False,
                    "prompt_text_persisted": False,
                },
            }
        return _build_safe_evidence(
            source_head=_source_head(),
            provider_id=expected_provider_id,
            model=expected_model,
            request_sha256=request_sha256,
            material_summaries=material_summaries,
            evaluation=child["evaluation"],
            elapsed_ms=int(child["elapsed_ms"]),
            timeout_seconds=timeout,
            output_token_budget=int(child["output_token_budget"]),
            stage_output_token_budgets={
                str(key): int(value)
                for key, value in child["stage_output_token_budgets"].items()
            },
            stage_timings_ms={
                str(key): int(value)
                for key, value in child["stage_timings_ms"].items()
            },
            stage_attempt_counts={
                str(key): int(value)
                for key, value in child["stage_attempt_counts"].items()
            },
            utility_call_count=int(child["utility_call_count"]),
            estimated_cost_units=int(child["estimated_cost_units"]),
            validated_plan_json_bytes=int(child["validated_plan_json_bytes"]),
            model_label_sha256=child.get("model_label_sha256"),
        )
    except BaseException as exc:
        failure = _safe_failure(exc)
        return {
            "schema_version": "1.0",
            "run_id": "SRC-03",
            "source_head": _source_head(),
            "provider": {"route": f"{expected_provider_id}/{expected_model}"},
            "input": {
                "sha256": request_sha256,
                "persisted": False,
                "materials": material_summaries,
            },
            **failure,
            "privacy": {
                "credentials_persisted": False,
                "model_text_persisted": False,
                "prompt_text_persisted": False,
            },
        }
    finally:
        shutil.rmtree(smoke_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-working-dir", type=Path)
    parser.add_argument("--source-secret-dir", type=Path)
    parser.add_argument("--brief-path", type=Path)
    parser.add_argument("--data-path", type=Path)
    parser.add_argument("--expected-provider-id", default="kimi-codingplan")
    parser.add_argument("--expected-model", default="kimi-for-coding")
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args()

    if args.inner:
        try:
            spec = json.loads(sys.stdin.read())
            result = {"ok": True, **_inner_run(spec, agent_id=args.agent_id, timeout=args.timeout)}
        except BaseException as exc:
            result = _safe_failure(exc)
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("ok"):
            raise SystemExit(1)
        return

    required = {
        "source-working-dir": args.source_working_dir,
        "source-secret-dir": args.source_secret_dir,
        "brief-path": args.brief_path,
        "data-path": args.data_path,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        parser.error(f"missing required arguments: {', '.join(missing)}")
    result = run(
        source_working_dir=args.source_working_dir,
        source_secret_dir=args.source_secret_dir,
        brief_path=args.brief_path,
        data_path=args.data_path,
        expected_provider_id=args.expected_provider_id,
        expected_model=args.expected_model,
        agent_id=args.agent_id,
        timeout=args.timeout,
    )
    if args.evidence_output is not None:
        args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
