# -*- coding: utf-8 -*-
"""Recoverable background runtime for presentation generation jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from inspect import Parameter, signature
from pathlib import Path
from typing import Callable, Iterator, Literal, Protocol, Sequence, cast
from uuid import uuid4

from httpx import RemoteProtocolError
from pydantic import ValidationError

from lantai.app.agent_context import current_session_id_scope
from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.app.utility_model import UtilityTaskTimeoutError

from .authoring_batch import author_pages_ordered
from .design_intelligence import (
    compose_deck_ir_with_design_context,
    enrich_plan_with_design_context,
    resolve_design_context,
    resolve_template_mode,
)
from .design_system import prepare_deck_for_composition
from .editing import PresentationEditTransactionService
from .failure import (
    failure_message_for_exception,
    failure_route_for_exception,
    failure_routes_for_quality_issues,
)
from .jobs import (
    PresentationJobInterruptedError,
    PresentationJobRecord,
    PresentationWriterBusyError,
    PresentationWriterLease,
)
from .models import (
    DeckIR,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    EvidenceState,
    JobStage,
    JobStatus,
    PlanningMode,
    PresentationBrief,
    PresentationDesignContext,
    PresentationEditOperation,
    PresentationEditPreview,
    PresentationFailureRoute,
    PresentationJobRequest,
    PresentationObjectEditPlan,
    PresentationProviderCostStatus,
    QualityIssue,
    QualityReport,
    SkillExecutionIdentity,
    SourceFactCorrection,
    PresentationVersion,
    is_native_pptx_engine,
)
from .natural_edit import (
    apply_object_edit_plan,
    apply_source_fact_corrections,
    apply_slide_intent_patch,
    plan_text_edit,
    validate_requested_object_edit_scope,
    validate_required_object_edit_coverage,
)
from .page_authoring import (
    PresentationPageAuthorContentError,
    PresentationPageAuthorSceneError,
    grounded_source_text,
    scope_authored_page_object_ids,
    validate_authored_page,
)
from .planner import (
    build_brief,
    build_deck_ir,
    build_deterministic_fixture_plan,
)
from .quality import (
    delivery_evidence_issues,
    file_sha256,
    inspect_pptx,
    is_verified_delivery,
)
from .reconcile import reconcile_pptx
from .reference_deck import ReferenceDeckAnalyzer
from .reference_deck.analyzer import reference_source_sha256
from .reference_deck.models import ReferenceLearningResult
from .render import slide_image_paths
from .rendered_geometry import GLYPH_DIVIDER_RULESET
from .repair import PresentationRepairDesigner, repair_deck_with_designer
from .service import PresentationService
from .template_binding import (
    TemplateAnchorCoverageError,
    require_template_anchor_coverage,
)
from .versioning import PresentationVersionStore, deck_ir_matches_version
from .visual_qa import PresentationVisualCritic


_PAGE_AUTHOR_VALIDATION_CODES = frozenset({
    "extra_forbidden", "missing", "literal_error", "enum", "list_type",
    "dict_type", "model_type", "model_attributes_type", "string_type",
    "string_too_short", "string_too_long", "string_unicode",
    "string_pattern_mismatch", "int_type", "int_parsing", "int_from_float",
    "float_type", "float_parsing", "finite_number", "greater_than",
    "greater_than_equal", "less_than", "less_than_equal", "too_short",
    "too_long", "json_invalid", "json_type", "value_error", "assertion_error",
    "union_tag_invalid", "union_tag_not_found", "schema_error",
    "authored_text_representation", "authored_text_reference_runs",
    "authored_shape_caption_text", "authored_shape_line_caption",
})


def _page_author_validation_path_pattern() -> re.Pattern[str]:
    """Use only application-owned schema names, not model-authored keys."""

    schema = DeckSlide.model_json_schema()
    names = {"text", "shape", "metric", "table", "chart", "image"}
    for definition in [schema, *schema.get("$defs", {}).values()]:
        names.update(definition.get("properties", {}))
    return re.compile(
        r"\$(?:\.(?:"
        + "|".join(re.escape(name) for name in sorted(names))
        + r")|\[[0-9]{1,6}\])*",
    )


_PAGE_AUTHOR_VALIDATION_PATH = _page_author_validation_path_pattern()
_PAGE_AUTHOR_VALIDATION_BRANCH_PATH = re.compile(
    r"^(\$\.objects\[[0-9]{1,6}\])\.(?:text|shape|metric|table|chart|image)(?=\.|\[|$)",
)

_PAGE_AUTHOR_FEEDBACK_PARTS = frozenset({
    "template_anchors", "scene", "speaker_notes", "numeric",
    "fact_reference", "fact_visibility", "chart_contract", "schema",
})


_PAGE_AUTHOR_NUMERIC_PATH = re.compile(
    r"\$\.objects\[(0|[1-9][0-9]?)\]\."
    r"(?:text|label|value|detail"
    r"|runs\[(?:0|[1-9][0-9]{0,5})\]\.text"
    r"|headers\[[0-7]\]"
    r"|rows\[(?:[0-9]|1[0-9])\]\[[0-7]\])",
)


def _safe_page_author_numeric_paths(value: object) -> list[str]:
    """Allow only bounded canonical visible-field paths, never model text."""

    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for path in value[:16]:
        if not isinstance(path, str):
            continue
        match = _PAGE_AUTHOR_NUMERIC_PATH.fullmatch(path)
        if match is not None and int(match.group(1)) < 80 and path not in paths:
            paths.append(path)
    return paths


def _safe_page_author_fact_paths(value: object, planned: DeckPlanSlide) -> list[str]:
    """Keep only bounded indexes into this accepted page's fact contract."""

    if not isinstance(value, list):
        return []
    allowed = {
        f"{field}[{index}]"
        for field in ("required_facts", "immutable_facts")
        for index in range(len(getattr(planned.intent, field)))
    }
    return list(dict.fromkeys(
        path for path in value if isinstance(path, str) and path in allowed
    ))


def _safe_page_author_validation_issues(
    error: StructuredTaskValidationError,
) -> tuple[str, ...]:
    issues: list[str] = []
    for raw in error.validation_issues[:8]:
        code, separator, path = raw.partition("@")
        if not separator or code not in _PAGE_AUTHOR_VALIDATION_CODES:
            code, path = "schema_error", "$"
        elif _PAGE_AUTHOR_VALIDATION_PATH.fullmatch(path) is None:
            path = "$"
        elif isinstance(error.__cause__, ValidationError):
            # Fresh Pydantic locations include a discriminated-union selector
            # after the object index. It is not an authored JSON field. Stored
            # feedback has no ValidationError cause and is already canonical;
            # do not strip a genuine field such as objects[0].text on restore.
            path = _PAGE_AUTHOR_VALIDATION_BRANCH_PATH.sub(r"\1", path, count=1)
        safe_issue = f"{code}@{path}"
        if safe_issue not in issues:
            issues.append(safe_issue)
    return tuple(issues)


class PresentationSemanticPlanner(Protocol):
    def plan(
        self,
        request: PresentationJobRequest,
        brief: PresentationBrief,
        *,
        design_context: PresentationDesignContext | None = None,
    ) -> DeckPlan:
        ...


class PresentationObjectEditPlanner(Protocol):
    def plan(
        self,
        deck: DeckIR,
        slide_id: str,
        instruction: str,
        *,
        deck_plan: DeckPlan | None = None,
        source_brief: PresentationBrief | None = None,
        additional_slide_ids: list[str] | None = None,
        required_slide_ids: list[str] | None = None,
    ) -> PresentationObjectEditPlan:
        ...


class PresentationPageAuthor(Protocol):
    def author(
        self,
        request: PresentationJobRequest,
        brief: PresentationBrief,
        plan: DeckPlan,
        planned_slide: DeckPlanSlide,
        scaffold_slide: DeckSlide,
        *,
        retry_directive: str | None = None,
    ) -> DeckSlide:
        ...


def _track_runtime_operation(method):
    @wraps(method)
    def tracked(self, *args, **kwargs):
        with self.synchronous_operation():
            return method(self, *args, **kwargs)

    return tracked


def _admit_runtime_task(method):
    @wraps(method)
    def admitted(self, *args, **kwargs):
        # Durable admission and submission must both precede shutdown. Keeping
        # the same lock through both prevents an orphaned Pending record.
        with self.task_admission():
            return method(self, *args, **kwargs)

    return admitted


class PresentationJobRuntime:
    _PAGE_AUTHOR_TRANSIENT_ATTEMPT_LIMIT = 5
    _PAGE_AUTHOR_RETRY_LIMIT_PER_CAUSE = 2

    @staticmethod
    def _page_author_schema_retry_cause(
        error: StructuredTaskValidationError,
    ) -> str:
        """Group repeated schema defects without conflating distinct fields.

        Object array indexes are deliberately normalized: a model returning
        the same invalid field on a different object is still the same cause.
        A different field or validation rule gets its own bounded allowance,
        while the overall page-attempt ceiling remains authoritative.
        """

        normalized = sorted(
            {
                re.sub(r"\[\d+\]", "[]", issue)
                for issue in error.validation_issues
                if issue
            },
        )
        return "schema:" + ("|".join(normalized) if normalized else "unknown")

    def __init__(
        self,
        root: str | Path,
        *,
        semantic_planner: PresentationSemanticPlanner | None = None,
        page_author: PresentationPageAuthor | None = None,
        object_edit_planner: PresentationObjectEditPlanner | None = None,
        visual_critic: PresentationVisualCritic | None = None,
        repair_designer: PresentationRepairDesigner | None = None,
    ) -> None:
        self.service = PresentationService(
            root,
            visual_critic=visual_critic,
            repair_designer=repair_designer,
        )
        self.semantic_planner = semantic_planner
        self.page_author = page_author
        self.object_edit_planner = object_edit_planner
        self.runtime_run_id = f"run-{uuid4().hex}"
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="lantai-presentation",
        )
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self._edit_locks: dict[str, threading.Lock] = {}
        self._closed = False
        self._operation_condition = threading.Condition(self._lock)
        self._synchronous_operations = 0
        self.recover_unfinished()

    @contextmanager
    def _synchronous_operation(self):
        with self._operation_condition:
            if self._closed:
                raise PresentationJobInterruptedError(
                    "Presentation runtime is closed",
                )
            self._synchronous_operations += 1
        try:
            yield
        finally:
            with self._operation_condition:
                self._synchronous_operations -= 1
                self._operation_condition.notify_all()

    @contextmanager
    def _writer_lease(
        self,
        *args,
        **kwargs,
    ) -> Iterator[PresentationWriterLease]:
        # Closing and writer admission share the same lock. An operation that
        # started preflight before shutdown cannot claim a fresh writer later.
        with self._lock:
            if self._closed:
                raise PresentationJobInterruptedError(
                    "Presentation runtime is closed",
                )
            kwargs["runtime_run_id"] = self.runtime_run_id
            context = self.service.store.writer_lease(*args, **kwargs)
            lease = context.__enter__()
        try:
            yield lease
        finally:
            context.__exit__(None, None, None)

    def _resolve_job_design_context(
        self,
        job_id: str,
        request: PresentationJobRequest,
        brief: PresentationBrief,
    ) -> PresentationDesignContext:
        """Resolve the one design policy shared by every downstream stage.

        References are an optional input layer.  With no reference the same
        Universal Design Intelligence autonomously synthesizes the profile;
        it never routes to a legacy or generic template.
        """

        context_path = self.service.store.checkpoint_path(
            job_id,
            "presentation-design-context.json",
        )
        if context_path.is_file():
            context = PresentationDesignContext.model_validate_json(
                context_path.read_text(encoding="utf-8"),
            )
            if request.template_path:
                expected_artifact_id = context.reference_artifact_id
                analysis_path = self.service.store.checkpoint_path(
                    job_id,
                    "reference-learning.json",
                )
                if analysis_path.is_file():
                    learning = ReferenceLearningResult.model_validate_json(
                        analysis_path.read_text(encoding="utf-8"),
                    )
                    learning_artifact_id = (
                        f"sha256:{learning.measurement.source_sha256}"
                    )
                    if (
                        expected_artifact_id is not None
                        and expected_artifact_id != learning_artifact_id
                    ):
                        raise ValueError(
                            (
                                "presentation reference checkpoint identities "
                                "disagree; existing checkpoints are preserved"
                            ),
                        )
                    expected_artifact_id = learning_artifact_id
                self._assert_reference_source_unchanged(
                    request.template_path,
                    expected_artifact_id,
                )
                if context.reference_artifact_id is None:
                    return context.model_copy(
                        update={"reference_artifact_id": expected_artifact_id},
                    )
            return context

        analysis: ReferenceLearningResult | None = None
        if request.template_path:
            analysis_path = self.service.store.checkpoint_path(
                job_id,
                "reference-learning.json",
            )
            if analysis_path.is_file():
                analysis = ReferenceLearningResult.model_validate_json(
                    analysis_path.read_text(encoding="utf-8"),
                )
                self._assert_reference_source_unchanged(
                    request.template_path,
                    f"sha256:{analysis.measurement.source_sha256}",
                )
            else:
                source = Path(request.template_path).expanduser().resolve()
                if not source.is_file():
                    raise FileNotFoundError(
                        f"presentation reference does not exist: {source}",
                    )
                analysis = ReferenceDeckAnalyzer().analyze(
                    source,
                    output_dir=(
                        self.service.store.job_dir(job_id)
                        / "reference-learning"
                    ),
                    profile_id=f"custom-reference-{file_sha256(source)[:12]}",
                    template_mode=resolve_template_mode(request),
                    generated_for_request=request.user_request,
                    variation_seed=request.seed,
                )
                self.service.store.assert_execution_budget(job_id)
                analysis_path = self.service.store.write_checkpoint(
                    job_id,
                    "reference-learning.json",
                    analysis.model_dump_json(indent=2),
                )
                self.service.store.append_event(
                    job_id,
                    stage=JobStage.BRIEF,
                    status=JobStatus.RUNNING,
                    message="已解析参考并完成质量仲裁；仅继承允许的品牌约束与设计语法",
                    checkpoint=str(analysis_path),
                )
            context = analysis.design_context
        else:
            context = resolve_design_context(request, brief)

        self.service.store.assert_execution_budget(job_id)
        self.service.store.write_checkpoint(
            job_id,
            "presentation-design-context.json",
            context.model_dump_json(indent=2),
        )
        return context

    @staticmethod
    def _assert_reference_source_unchanged(
        template_path: str,
        expected_artifact_id: str | None,
    ) -> None:
        if expected_artifact_id is None:
            raise ValueError(
                (
                    "presentation reference identity cannot be verified from "
                    "this checkpoint; existing checkpoints are preserved"
                ),
            )
        source = Path(template_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(
                f"presentation reference does not exist: {source}",
            )
        current_artifact_id = f"sha256:{reference_source_sha256(source)}"
        if expected_artifact_id != current_artifact_id:
            raise ValueError(
                (
                    "presentation reference changed since this job's design "
                    "checkpoint; existing checkpoints are preserved and a new "
                    "job is required for the changed reference"
                ),
            )

    @contextmanager
    def _measure_stage(
        self,
        job_id: str,
        *,
        stage: JobStage,
        operation: str,
        route_id: str | None = None,
        route_resolver: Callable[[], str | None] | None = None,
        cost_units: int = 0,
        slide_id: str | None = None,
        provider_cost_status: PresentationProviderCostStatus = (
            PresentationProviderCostStatus.NOT_APPLICABLE
        ),
    ) -> Iterator[None]:
        """Record actual stage duration without mutating lifecycle state."""

        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()

        def resolved_route_id() -> str | None:
            if route_resolver is None:
                return route_id
            return route_resolver() or route_id

        try:
            yield
        except Exception:
            status = self.service.store.get(job_id).status
            outcome = (
                "cancelled"
                if status in {JobStatus.PAUSED, JobStatus.CANCELLED}
                else "failed"
            )
            self.service.record_stage_metric(
                job_id,
                stage=stage,
                operation=operation,
                repair_round=0,
                started_at=started_at,
                started_clock=started_clock,
                outcome=outcome,
                route_id=resolved_route_id(),
                cost_units=cost_units,
                slide_id=slide_id,
                provider_cost_status=provider_cost_status,
            )
            raise
        self.service.record_stage_metric(
            job_id,
            stage=stage,
            operation=operation,
            repair_round=0,
            started_at=started_at,
            started_clock=started_clock,
            outcome="passed",
            route_id=resolved_route_id(),
            cost_units=cost_units,
            slide_id=slide_id,
            provider_cost_status=provider_cost_status,
        )

    def synchronous_operation(self):
        return self._synchronous_operation()

    @contextmanager
    def task_admission(self) -> Iterator[None]:
        with self._lock:
            if self._closed:
                raise PresentationJobInterruptedError(
                    "Presentation runtime is closed",
                )
            yield

    @staticmethod
    def _invoke_callback(callback: Callable[..., object], *args) -> object:
        return callback(*args)

    def _page_retry_description(
        self,
        exc: BaseException,
        retry_directive_parts: dict[str, str],
        missing_fact_paths_seen: list[str],
        schema_issues_seen: list[str],
        required_page=None,
        planned_page: DeckPlanSlide | None = None,
        numeric_paths_seen: list[str] | None = None,
    ) -> str:
        if isinstance(exc, TemplateAnchorCoverageError):
            failure_kind = "模板必需锚点覆盖未通过"
            missing = [
                f"source_shape_id={anchor.source_shape_id}/purpose={anchor.purpose}"
                for anchor in required_page.anchors
                if (anchor.source_shape_id, anchor.purpose) in exc.missing_anchors
            ] if required_page is not None else []
            retry_directive_parts["template_anchors"] = (
                "上一版缺少 template_anchor_requirements 中的必需锚点。"
                "请作者显式选择输入契约中固定的 source_slide_number、"
                "source_shape_id 和 purpose，完整保留所有必需锚点；"
                + ("本次缺失：" + ",".join(missing) + "；" if missing else "")
                + "Runtime 不会自动补选。title_frame 引用本次唯一 title；"
                "static_brand 和 page_number 使用不在 objects 中的新 "
                "target_object_id。不得复制旧正文，不得修改事实或图表数据。"
                "返回完整 scene。"
            )
        elif isinstance(exc, PresentationPageAuthorSceneError):
            failure_kind = "场景关系未通过"
            retry_directive_parts["scene"] = (
                "上一版场景关系未通过。object_groups 和 "
                "relationships 中的每个对象 id 都必须来自"
                "同一返回 scene 的 objects；删除悬空引用或补回"
                "对应原生对象。两者都不是视觉构图必填项；没有"
                "必要的机器可读语义时直接返回空列表，不要为了"
                "形式完整而生成关系。返回完整 scene，不得修改 "
                "required_facts、immutable_facts 或 "
                "storyboard.charts。"
            )
        elif isinstance(exc, PresentationPageAuthorContentError):
            failure_kind = "事实门禁未通过"
            missing_fact_paths = [
                path
                for path in getattr(
                    exc,
                    "presentation_missing_fact_paths",
                    [],
                )
                if isinstance(path, str)
                and re.fullmatch(
                    r"(?:required_facts|immutable_facts)\[\d+\]",
                    path,
                )
            ]
            if planned_page is not None:
                missing_fact_paths = _safe_page_author_fact_paths(
                    missing_fact_paths, planned_page,
                )
            for path in missing_fact_paths:
                if path not in missing_fact_paths_seen:
                    missing_fact_paths_seen.append(path)
            if exc.violation_code == "numeric_speaker_notes":
                retry_directive_parts["speaker_notes"] = (
                    "上一版的 speaker_notes 含有数字。"
                    "speaker_notes 必须返回空字符串；"
                    "不要保留、改写或概括上一版备注。"
                    "不得修改可见对象、必需事实或图表数据。"
                    "返回完整 scene。"
                )
            elif exc.violation_code in {
                "unsupported_number",
                "decorative_numbering",
            }:
                if numeric_paths_seen is None:
                    numeric_paths_seen = []
                for path in _safe_page_author_numeric_paths(getattr(
                    exc, "presentation_numeric_object_paths", None,
                )):
                    if path not in numeric_paths_seen and len(numeric_paths_seen) < 16:
                        numeric_paths_seen.append(path)
                retry_directive_parts["numeric"] = (
                    "上一版未通过事实门禁："
                    "出现来源材料不支持的数字。"
                    + (
                        "本次受影响的安全对象字段路径："
                        + ",".join(numeric_paths_seen) + "；"
                        if numeric_paths_seen else ""
                    )
                    + "删除该数字；可见文字只能使用 "
                    "page_numeric_contract.allowed_numeric_tokens "
                    "中的数字。"
                    "不得添加页码、步骤编号、章节编号、数字徽章或"
                    "其他装饰数字。不得修改必需事实或图表数据。"
                    "返回完整 scene。"
                )
            elif exc.violation_code == "invalid_fact_reference":
                retry_directive_parts["fact_reference"] = (
                    "上一版使用了本页目录之外的文字引用。TextObject.text_refs "
                    "只能显式选择 fact_reference_transport.references 中的 ref，"
                    "不能引用周边页、brief 或自造路径；选择顺序和可见对象仍由你决定。"
                    "text 与 text_refs 只能选一种，引用不能与非空 runs 同用。"
                    "Runtime 不会替你补选缺失事实；完整覆盖本页必需事实和唯一标题，"
                    "返回完整 scene，不得改变事实、图表、模板锚点或布局要求。"
                )
            elif exc.violation_code in {
                "invalid_chart_reference",
                "planned_chart_data_changed",
            }:
                retry_directive_parts["chart_contract"] = (
                    "上一版未完整保留本页图表契约。ChartObject.chart_ref "
                    "只能显式选择 chart_reference_transport.references 中的 ref，"
                    "不能引用周边页、brief 或自造路径。引用形式只返回 "
                    "kind、id、slot、frame 和 chart_ref，不得同时提供数据或样式字段；"
                    "也可返回完整且逐项原样的 storyboard.charts 数据契约。"
                    "完整覆盖所有本页图表，不得遗漏或重复；保留图表类型、类别、系列、"
                    "数值、图例、坐标轴、绘图区、单位、格式、推导、证明和来源引用。"
                    "可见对象选择、id 和坐标仍由你决定，Runtime 不会自动补选或补图。"
                    "返回完整 scene，不得改变事实、模板锚点或布局要求。"
                )
            else:
                retry_directive_parts["fact_visibility"] = (
                    "上一版未通过事实门禁。逐字保留 required_facts、immutable_facts，"
                    + (
                        "补回这些缺失的安全路径："
                        + ",".join(missing_fact_paths_seen)
                        + "；"
                        if missing_fact_paths_seen
                        else ""
                    )
                    + "并原样保留 storyboard.charts 的数据契约；返回完整 scene。"
                )
        elif isinstance(exc, StructuredTaskValidationError):
            failure_kind = "结构化输出未通过 schema"
            safe_schema_issues = _safe_page_author_validation_issues(exc)
            for issue in safe_schema_issues:
                if (
                    issue not in schema_issues_seen
                    and len(schema_issues_seen) < 16
                ):
                    schema_issues_seen.append(issue)
            if safe_schema_issues:
                failure_kind += "（安全路径：" + ",".join(safe_schema_issues) + "）"
            retry_directive_parts["schema"] = (
                "上一版未通过结构化 schema。严格按 native_scene_contract 修复"
                + (
                    "这些安全路径：" + ",".join(schema_issues_seen) + "。"
                    if schema_issues_seen
                    else "所有字段。"
                )
                + (
                    "objects 必须是 JSON array，其中每个条目必须是 JSON object，"
                    "不能是字符串、数组或 null；每个条目都显式填写 kind，"
                    "取值必须来自 native_scene_contract.object_types_by_kind 的键，"
                    "并按该 kind 的 required_fields 和 allowed_fields 填写。"
                    "Runtime 不会猜测对象类型或把字符串、数组转成对象。"
                    if any(
                        issue.partition("@")[0] in {
                            "union_tag_not_found", "union_tag_invalid",
                        }
                        or (
                            issue.partition("@")[0] in {
                                "model_type", "model_attributes_type",
                            }
                            and re.fullmatch(
                                r"\$\.objects\[[0-9]{1,6}\]",
                                issue.partition("@")[2],
                            ) is not None
                        )
                        for issue in schema_issues_seen
                    )
                    else ""
                )
                + (
                    "数值字段的非 null 值必须直接写成 JSON number；"
                    "不得附加 pt、px 或英寸单位，也不得写成字符串、对象或数组。"
                    "只有 schema 允许 null 的字段才能返回 null；"
                    "Runtime 不会提取单位、转换类型或代填数值。"
                    if any(
                        issue.partition("@")[0] in {"float_type", "float_parsing"}
                        for issue in schema_issues_seen
                    )
                    else ""
                )
                + (
                    "TextObject 的 text 或 text_refs 只能选一种且必须选一种；"
                    "选择 text 时省略 text_refs 或设为 null，选择 text_refs "
                    "时省略 text 或设为 null。"
                    if any(
                        issue.startswith("authored_text_representation@")
                        for issue in schema_issues_seen
                    )
                    else ""
                )
                + (
                    "选择 text_refs 时 runs 必须为空；如需 rich text runs，"
                    "改用 text 并省略 text_refs 或设为 null。"
                    if any(
                        issue.startswith("authored_text_reference_runs@")
                        for issue in schema_issues_seen
                    )
                    else ""
                )
                + (
                    "ShapeObject.text 是唯一权威可见标签；需要标签时在 text "
                    "中填写完整可见文字，runs 只能提供该标签的富文本样式，"
                    "不能只填 runs 或依赖它覆盖 text。不需要标签时 text 与 "
                    "runs 都留空。Runtime 不会把 runs 中的文字抄入 text "
                    "或替你补写标签。"
                    if any(
                        issue.startswith("authored_shape_caption_text@")
                        for issue in schema_issues_seen
                    )
                    else ""
                )
                + (
                    "shape_type=line 不能承载标签；线条的 text 与 runs 都留空，"
                    "标签另用有独立坐标的 TextObject。Runtime 不会自动新增"
                    "文字对象、搬运文字或改写线条类型。"
                    if any(
                        issue.startswith("authored_shape_line_caption@")
                        for issue in schema_issues_seen
                    )
                    else ""
                )
                + "title 角色的 TextObject.font_size 必须在 "
                "32 到 72 之间，其 RichTextRun.font_size 只能为 "
                "null 或 32 到 72；其他 TextObject.font_size 必须在 "
                "17 到 72 之间；ShapeObject.font_size 必须在 17 "
                "到 72 之间；其他 RichTextRun.font_size 只能为 "
                "null 或 17 到 72。frame.x 为 0 到 13.333，"
                "frame.y 为 0 到 7.5，frame.w 必须大于 0 "
                "且不超过 13.333，frame.h 为 0 到 7.5。"
                "objects 中的 id 必须唯一；template_anchors 的 title_frame "
                "必须引用本次返回的唯一 title；static_brand 和 page_number 的 "
                "target_object_id 必须使用不在 objects 中的新 ID。"
                "只使用 template_anchor_catalog 中原样固定的 source_slide_number "
                "和 source_shape_id；没有授权目录时省略 template_anchors。"
                "删除未知字段，返回完整 scene；不得修改事实或"
                "图表数据。"
            )
        elif isinstance(exc, RemoteProtocolError):
            failure_kind = "模型服务连接中断"
        else:
            failure_kind = "模型服务超时"

        return failure_kind

    def _restore_page_author_feedback(
        self, path: Path, dependency: str, planned: DeckPlanSlide, required_page,
    ) -> tuple[dict[str, str], list[str], list[str], list[int], list[str]]:
        """Rebuild owned instructions, never load prompt text from a file."""

        parts: dict[str, str] = {}
        fact_paths: list[str] = []
        schema_issues: list[str] = []
        anchor_indexes: list[int] = []
        numeric_paths: list[str] = []
        empty = (parts, fact_paths, schema_issues, anchor_indexes, numeric_paths)
        try:
            if path.stat().st_size > 65536:
                return empty
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "1.0"
            or payload.get("dependency_sha256") != dependency
        ):
            return empty
        keys = payload.get("parts")
        if (
            not isinstance(keys, list)
            or len(keys) > len(_PAGE_AUTHOR_FEEDBACK_PARTS)
            or any(
                not isinstance(key, str) or key not in _PAGE_AUTHOR_FEEDBACK_PARTS
                for key in keys
            )
        ):
            return empty
        fact_paths.extend(_safe_page_author_fact_paths(
            payload.get("missing_fact_paths"), planned,
        ))
        if "numeric" in keys:
            numeric_paths.extend(_safe_page_author_numeric_paths(
                payload.get("numeric_object_paths"),
            ))
        raw_issues = payload.get("schema_issues")
        if isinstance(raw_issues, list):
            raw_issues = [item for item in raw_issues[:16] if isinstance(item, str)]
            for offset in (0, 8):
                schema_issues.extend(_safe_page_author_validation_issues(
                    StructuredTaskValidationError(
                        "", validation_issues=tuple(raw_issues[offset:offset + 8]),
                    ),
                ))
        raw_indexes = payload.get("template_anchor_indexes")
        if required_page is not None and isinstance(raw_indexes, list):
            anchor_indexes.extend(dict.fromkeys(
                index for index in raw_indexes[:16]
                if type(index) is int and 0 <= index < len(required_page.anchors)
            ))
        content_codes = {
            "speaker_notes": "numeric_speaker_notes",
            "numeric": "unsupported_number",
            "fact_reference": "invalid_fact_reference",
            "fact_visibility": "required_visible_fact_omitted",
            "chart_contract": "planned_chart_data_changed",
        }
        for key in dict.fromkeys(keys):
            if key == "template_anchors":
                if required_page is None:
                    continue
                error = TemplateAnchorCoverageError(
                    required_page.source_slide_number,
                    tuple(
                        (
                            required_page.anchors[index].source_shape_id,
                            required_page.anchors[index].purpose,
                        )
                        for index in anchor_indexes
                    ),
                )
            elif key == "scene":
                error = PresentationPageAuthorSceneError(
                    "", violation_code="dangling_object_reference",
                )
            elif key == "schema":
                error = StructuredTaskValidationError(
                    "", validation_issues=tuple(schema_issues[:8]),
                )
            else:
                error = PresentationPageAuthorContentError(
                    "", violation_code=content_codes[key],
                )
                error.presentation_missing_fact_paths = fact_paths
                error.presentation_numeric_object_paths = numeric_paths
            self._page_retry_description(
                error, parts, fact_paths, schema_issues, required_page, planned,
                numeric_paths,
            )
        return parts, fact_paths, schema_issues, anchor_indexes, numeric_paths

    def _author_uncached_page(
        self,
        job_id: str,
        request: PresentationJobRequest,
        brief: PresentationBrief,
        plan: DeckPlan,
        planned: DeckPlanSlide,
        scaffold: DeckSlide,
        route_id: str,
        source_text: str,
        dependency_sha256: str,
    ) -> DeckSlide:
        required_page = next((
            page for page in request.template_anchor_requirements.pages
            if page.target_slide_number == planned.order
        ), None) if request.template_anchor_requirements is not None else None
        cached = None
        retry_counts: dict[str, int] = {}
        feedback_name = f"page-author-feedback-{planned.order:03d}.json"
        (
            retry_directive_parts, missing_fact_paths_seen, schema_issues_seen,
            template_anchor_indexes, numeric_paths_seen,
        ) = self._restore_page_author_feedback(
            self.service.store.checkpoint_path(job_id, feedback_name),
            dependency_sha256, planned, required_page,
        )
        retry_directive = "\n".join(retry_directive_parts.values()) or None
        for author_attempt in range(
            1,
            self._PAGE_AUTHOR_TRANSIENT_ATTEMPT_LIMIT + 1,
        ):
            if hasattr(self.page_author, "last_model_label"):
                self.page_author.last_model_label = None
            try:
                with self._measure_stage(
                    job_id,
                    stage=JobStage.COMPOSE,
                    operation="author",
                    route_id=route_id,
                    route_resolver=lambda: getattr(
                        self.page_author,
                        "last_model_label",
                        None,
                    ),
                    cost_units=1,
                    slide_id=scaffold.id,
                    provider_cost_status=(
                        PresentationProviderCostStatus.UNKNOWN
                    ),
                ):
                    self.service.store.consume_cost(job_id, 1)
                    if retry_directive is None:
                        authored = self.page_author.author(
                            request,
                            brief,
                            plan,
                            planned,
                            scaffold,
                        )
                    else:
                        authored = self.page_author.author(
                            request,
                            brief,
                            plan,
                            planned,
                            scaffold,
                            retry_directive=retry_directive,
                        )
                    if not isinstance(authored, DeckSlide):
                        raise TypeError(
                            "page author returned an invalid scene",
                        )
                    authored = scope_authored_page_object_ids(authored)
                    cached = validate_authored_page(
                        authored,
                        planned,
                        scaffold,
                        source_text=source_text,
                        source_brief=brief,
                    )
                    require_template_anchor_coverage(cached.template_anchors, required_page)
                    self.service.store.assert_execution_budget(job_id)
            except (
                TemplateAnchorCoverageError,
                PresentationPageAuthorContentError,
                PresentationPageAuthorSceneError,
                StructuredTaskValidationError,
                UtilityTaskTimeoutError,
                RemoteProtocolError,
            ) as exc:
                if isinstance(exc, TemplateAnchorCoverageError):
                    retry_cause = "template_anchors"
                elif isinstance(exc, PresentationPageAuthorSceneError):
                    retry_cause = "scene"
                elif isinstance(exc, PresentationPageAuthorContentError):
                    retry_cause = "content"
                elif isinstance(exc, StructuredTaskValidationError):
                    retry_cause = self._page_author_schema_retry_cause(exc)
                elif isinstance(exc, RemoteProtocolError):
                    retry_cause = "remote_protocol"
                else:
                    retry_cause = "timeout"
                retry_counts[retry_cause] = (
                    retry_counts.get(retry_cause, 0) + 1
                )
                failure_kind = self._page_retry_description(
                    exc, retry_directive_parts, missing_fact_paths_seen,
                    schema_issues_seen, required_page, planned, numeric_paths_seen,
                )
                if (
                    isinstance(exc, TemplateAnchorCoverageError)
                    and required_page is not None
                ):
                    template_anchor_indexes = [
                        index for index, anchor in enumerate(required_page.anchors)
                        if (anchor.source_shape_id, anchor.purpose) in exc.missing_anchors
                    ]
                if retry_directive_parts:
                    self.service.store.write_checkpoint(
                        job_id, feedback_name,
                        json.dumps({
                            "schema_version": "1.0",
                            "dependency_sha256": dependency_sha256,
                            "parts": list(retry_directive_parts),
                            "missing_fact_paths": missing_fact_paths_seen,
                            "schema_issues": schema_issues_seen,
                            "template_anchor_indexes": template_anchor_indexes,
                            "numeric_object_paths": numeric_paths_seen,
                        }, ensure_ascii=False, indent=2),
                    )
                if (
                    author_attempt >= self._PAGE_AUTHOR_TRANSIENT_ATTEMPT_LIMIT
                    or retry_counts[retry_cause]
                    > self._PAGE_AUTHOR_RETRY_LIMIT_PER_CAUSE
                ):
                    raise
                self.service.store.assert_execution_budget(job_id)
                retry_directive = (
                    "\n".join(retry_directive_parts.values()) or None
                )
                self.service.store.append_event(
                    job_id,
                    stage=JobStage.COMPOSE,
                    status=JobStatus.RUNNING,
                    slide_id=scaffold.id,
                    message=(
                        f"第 {planned.order} 页{failure_kind}；"
                        "仅重试当前页"
                        f"（{author_attempt + 1}/"
                        f"{self._PAGE_AUTHOR_TRANSIENT_ATTEMPT_LIMIT}）"
                    ),
                    recoverable=True,
                )
                continue
            break

        return cached

    def _author_page(
        self,
        job_id: str,
        request: PresentationJobRequest,
        brief: PresentationBrief,
        plan: DeckPlan,
        planned: DeckPlanSlide,
        scaffold: DeckSlide,
        route_id: str,
        source_text: str,
    ) -> DeckSlide:
        required_page = next((
            page for page in request.template_anchor_requirements.pages
            if page.target_slide_number == planned.order
        ), None) if request.template_anchor_requirements is not None else None
        if not self._active(job_id):
            raise PresentationJobInterruptedError(
                "presentation authoring stopped",
            )
        identity_resolver: Callable[[], str] = getattr(
            self.page_author,
            "cache_identity",
            None,
        )
        author_identity = (
            self._invoke_callback(identity_resolver)
            if callable(identity_resolver)
            else route_id
        )
        if not isinstance(author_identity, str) or not author_identity:
            raise ValueError("page author returned an invalid cache identity")
        checkpoint_name = f"page-scene-{planned.order:03d}.json"
        checkpoint_path = self.service.store.checkpoint_path(
            job_id,
            checkpoint_name,
        )
        dependency_resolver: Callable[..., object] = getattr(
            self.page_author,
            "cache_dependencies",
            None,
        )
        # Authors without an explicit dependency contract may inspect any
        # part of the plan, so they retain conservative full-plan invalidation.
        author_inputs = (
            self._invoke_callback(
                dependency_resolver,
                request,
                brief,
                plan,
                planned,
                scaffold,
            )
            if callable(dependency_resolver)
            else plan.model_dump(mode="json")
        )
        dependency = hashlib.sha256(
            json.dumps(
                {
                    "request": request.model_dump(mode="json"),
                    "brief": brief.model_dump(mode="json"),
                    "author_inputs": author_inputs,
                    "slide": scaffold.id,
                    "route": route_id,
                    "author_identity": author_identity,
                    "author_agent_id": getattr(
                        self.page_author,
                        "agent_id",
                        None,
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        cached = None
        if checkpoint_path.is_file():
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if payload.get("dependency_sha256") == dependency:
                cached = scope_authored_page_object_ids(
                    DeckSlide.model_validate(payload["slide"]),
                )
                try:
                    require_template_anchor_coverage(cached.template_anchors, required_page)
                except TemplateAnchorCoverageError:
                    cached = None
        if cached is None:
            cached = self._author_uncached_page(
                job_id,
                request,
                brief,
                plan,
                planned,
                scaffold,
                route_id,
                source_text,
                dependency,
            )
            model_route = getattr(self.page_author, "last_model_label", None)
            checkpoint_path = self.service.store.write_checkpoint(
                job_id,
                checkpoint_name,
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "dependency_sha256": dependency,
                        "model_route": model_route,
                        "slide": cached.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            self.service.store.append_event(
                job_id,
                stage=JobStage.COMPOSE,
                status=JobStatus.RUNNING,
                slide_id=scaffold.id,
                message=(
                    f"第 {planned.order} 页原生 scene 已检查并保存"
                    + (f"（模型路由：{model_route}）" if model_route else "")
                ),
                checkpoint=str(checkpoint_path),
            )
        return validate_authored_page(
            cached,
            planned,
            scaffold,
            source_text=source_text,
            source_brief=brief,
        )

    def _revalidate_legacy_edit_base(
        self,
        job_id: str,
        deck: DeckIR,
        source: Path,
        quality_path: Path,
        original_deck_text: str,
        original_quality_bytes: bytes,
        old_report: QualityReport,
        old_rendered: list[Path],
        current_version: PresentationVersion | None,
        version_store: PresentationVersionStore,
        base_output_sha256: str,
        requested_slide_ids: Sequence[str],
    ) -> bool:
        """Admit a bounded repair of owned pre-glyph QA, not a delivery pass.

        Historical QA must retain all of its original status and artifact
        bindings. Re-render under the current writer/deadline and append the
        new assessment, including failures. Only glyph collisions in pages
        explicitly authorized by the caller may proceed to candidate QA.
        The old report/version are never relabelled or rewritten.
        """
        if not (
            current_version is not None
            and current_version.quality_snapshot
            and old_report.rendered_glyph_check_version is None
            and old_report.passed
            and old_report.delivery_status == "passed"
            and old_report.render_verified
            and old_report.content_evidence == EvidenceState.PASSED
            and old_report.structure_evidence == EvidenceState.PASSED
            and old_report.render_evidence == EvidenceState.PASSED
            and old_report.editability_evidence == EvidenceState.PASSED
            and old_report.provenance_evidence == EvidenceState.PASSED
            and not old_report.blocking_gaps
            and not any(issue.severity == "error" for issue in old_report.issues)
            and (
                not old_report.vision_critique_required
                or (old_report.visual_verified and old_report.visual_evidence == EvidenceState.PASSED)
            )
            and old_report.deck_id == deck.id
            and {issue.code for issue in delivery_evidence_issues(
                deck, old_report, source, old_rendered,
            )} == {"evidence.glyph_geometry_missing"}
        ):
            return False
        job_dir = self.service.store.job_dir(job_id)
        snapshot = (job_dir / current_version.quality_snapshot).resolve()
        artifacts_root = (job_dir / "versions/artifacts").resolve()
        if (
            artifacts_root not in snapshot.parents
            or not snapshot.is_file()
            or snapshot.read_bytes() != original_quality_bytes
        ):
            return False
        base = version_store.assert_writer_base(
            source, deck, expected_pptx_sha256=base_output_sha256,
        )
        if base is None or base.version_id != current_version.version_id:
            raise ValueError("旧版本校验期间基底版本已变化")
        structural = inspect_pptx(deck, source, rendered_slide_count=len(old_rendered))
        if not structural.passed or structural.output_sha256 != base_output_sha256:
            return False

        assessment_id = f"edit-base-{uuid4().hex}"
        report, images = self.service.verify_artifact(
            deck, source, job_dir / "renders" / assessment_id,
            visual_required=True, vision_critique_required=False,
            render_set_id=assessment_id, job_id=job_id,
        )
        # A long real render does not grant ownership of external changes.
        checked_base = version_store.assert_writer_base(
            source, deck, expected_pptx_sha256=base_output_sha256,
        )
        if (
            checked_base is None or checked_base.version_id != base.version_id
            or quality_path.read_bytes() != original_quality_bytes
            or self.service.store.checkpoint_path(job_id, "deck-ir.json").read_text(
                encoding="utf-8",
            ) != original_deck_text
        ):
            raise ValueError("旧版本校验期间基底或证据已变化")
        self.service.store.write_checkpoint(
            job_id, f"edit-base-quality-report-{assessment_id}.json",
            report.model_dump_json(indent=2),
        )
        if is_verified_delivery(deck, report, source, images):
            return True
        errors = [issue for issue in report.issues if issue.severity == "error"]
        return bool(
            errors
            and all(
                issue.code == "layout.glyph_divider_collision"
                and issue.repairable and issue.slide_id in requested_slide_ids
                for issue in errors
            )
            and report.deck_id == deck.id
            and report.output_sha256 == base_output_sha256
            and report.rendered_glyph_check_version == GLYPH_DIVIDER_RULESET
            and report.render_verified
            and report.content_evidence == EvidenceState.PASSED
            and report.structure_evidence == EvidenceState.PASSED
            and report.editability_evidence == EvidenceState.PASSED
            and report.provenance_evidence == EvidenceState.PASSED
            and report.render_evidence == EvidenceState.FAILED
            and set(report.blocking_gaps) == {"render evidence"}
            and not delivery_evidence_issues(deck, report, source, images)
        )

    def _validated_edit_base(
        self,
        job_id: str,
        record: PresentationJobRecord,
        source: Path,
        quality_path: Path,
        original_deck_text: str,
        original_quality_bytes: bytes,
        current_version: PresentationVersion | None,
        version_store: PresentationVersionStore,
        base_output_sha256: str,
        requested_slide_ids: Sequence[str],
    ) -> tuple:
        deck = DeckIR.model_validate_json(original_deck_text)
        checkpoint_deck = deck
        old_report = QualityReport.model_validate_json(
            original_quality_bytes.decode("utf-8"),
        )
        old_render_dir = self._render_dir_for_report(job_id, old_report)
        old_rendered = slide_image_paths(old_render_dir)
        verified_delivery = is_verified_delivery(
            deck,
            old_report,
            source,
            old_rendered,
        )
        reconciled_external_base = (
            current_version is not None
            and current_version.writer == "external_reconcile"
            and current_version.reconciled
            and current_version.pptx_sha256 == base_output_sha256
            and deck_ir_matches_version(deck, current_version.deck_ir_version)
            and old_report.output_sha256 == base_output_sha256
            and not delivery_evidence_issues(
                deck,
                old_report,
                source,
                old_rendered,
            )
        )
        revalidated_legacy_base = False
        if not verified_delivery and not reconciled_external_base:
            revalidated_legacy_base = self._revalidate_legacy_edit_base(
                job_id, deck, source, quality_path, original_deck_text,
                original_quality_bytes, old_report, old_rendered,
                current_version, version_store, base_output_sha256,
                requested_slide_ids,
            )
        if not verified_delivery and not reconciled_external_base and not revalidated_legacy_base:
            raise ValueError("当前版本没有完整的渲染证据，不能在其上提交修改")

        if version_store.current() is None:
            version_store.bootstrap(
                source,
                deck,
                writer=record.request.composition_engine.value,
                quality_report_path=quality_path,
                render_dir=old_render_dir,
            )
        base_version = version_store.assert_writer_base(
            source,
            deck,
            expected_pptx_sha256=base_output_sha256,
        )

        # Delivery evidence and version ownership describe the exact
        # persisted DeckIR checkpoint.  Only after those hashes are
        # verified may the native PPTX become the geometry authority for
        # an edit. Reimport supported objects and positioning before
        # targeted redo; this is not a lossless roundtrip for arbitrary
        # native features. Reconcile rejects known unsupported structures.
        if is_native_pptx_engine(record.request.composition_engine):
            deck = reconcile_pptx(
                deck,
                source,
                assets_dir=(
                    self.service.store.job_dir(job_id)
                    / "assets"
                    / f"edit-base-{base_output_sha256[:12]}"
                ),
            )
            deck = prepare_deck_for_composition(deck)

        return deck, checkpoint_deck, base_version, revalidated_legacy_base

    def _natural_edit_candidate(
        self,
        job_id: str,
        record: PresentationJobRecord,
        deck: DeckIR,
        plan: DeckPlan | None,
        slide_id: str,
        instruction: str,
    ) -> tuple:
        is_semantic_redesign = False
        replacements = None
        try:
            changed_deck, replacements = plan_text_edit(
                deck,
                slide_id,
                instruction,
            )
            changed_object_ids = [item.object_id for item in replacements]
        except ValueError as exc:
            if self.service.repair_designer is None:
                raise
            if not is_native_pptx_engine(
                record.request.composition_engine,
            ):
                raise ValueError(
                    "整页语义重做只支持 Presentation v3；旧版演示请先迁移或重新生成",
                ) from exc
            self.service.store.consume_cost(job_id, 8)
            redesign = repair_deck_with_designer(
                deck,
                plan,
                [
                    QualityIssue(
                        code="user.redesign",
                        severity="error",
                        slide_id=slide_id,
                        message=instruction.strip(),
                        repairable=True,
                    ),
                ],
                self.service.repair_designer,
                usage_session_id=record.request.conversation_id,
            )
            if not redesign.changed:
                raise ValueError("整页重做没有产生可验证的设计变化") from exc
            changed_deck = redesign.deck
            is_semantic_redesign = True
            redesigned_slide = next(
                item for item in changed_deck.slides if item.id == slide_id
            )
            changed_object_ids = [obj.id for obj in redesigned_slide.objects]

        return (
            changed_deck,
            replacements,
            changed_object_ids,
            is_semantic_redesign,
        )

    def _prepare_edit_candidate(
        self,
        job_id: str,
        record: PresentationJobRecord,
        deck: DeckIR,
        checkpoint_deck: DeckIR,
        original_plan_text: str | None,
        slide_id: str,
        instruction: str,
        *,
        additional_slide_ids: list[str] | None = None,
        required_slide_ids: list[str] | None = None,
        source_fact_corrections: list[SourceFactCorrection | dict] | None = None,
    ) -> tuple:
        plan = (
            DeckPlan.model_validate_json(
                original_plan_text,
            )
            if original_plan_text is not None
            else None
        )
        changed_plan = plan
        edit_brief: PresentationBrief | None = None
        replacements = None
        is_semantic_redesign = False
        is_intent_only = False
        is_speaker_notes_only = False
        intent_patch_slide_ids: list[str] = []
        changed_object_ids: list[str] = []
        object_edit_slide_ids = [slide_id]
        compiled_slide_ids = [slide_id]
        if self.object_edit_planner is not None:
            brief_path = self.service.store.checkpoint_path(job_id, "brief.json")
            edit_brief = (
                PresentationBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
                if brief_path.is_file() else build_brief(record.request)
            )
            # Caller-pinned corrections are validated again under the writer
            # fence before dispatch. They do not authorize a model to change
            # any other source, immutable fact or native object.
            deck, changed_plan = apply_source_fact_corrections(
                deck, changed_plan, edit_brief, source_fact_corrections or [],
                allowed_slide_ids=[slide_id, *(additional_slide_ids or [])],
            )
            # Historical injected planners only accept deck_plan. Pass the
            # additive context when supported, without retrying a failed call
            # or inferring source facts from their generated output.
            try:
                planner_parameters = signature(self.object_edit_planner.plan).parameters
            except (TypeError, ValueError):
                planner_parameters = {}
            source_context = (
                {"source_brief": edit_brief}
                if "source_brief" in planner_parameters or any(
                    item.kind == Parameter.VAR_KEYWORD for item in planner_parameters.values()
                ) else {}
            )
            retry_budget_context = (
                {"retry_cost_reserver": lambda: self.service.store.consume_cost(job_id, 8)}
                if "retry_cost_reserver" in planner_parameters else {}
            )
            self.service.store.consume_cost(job_id, 8)
            with current_session_id_scope(record.request.conversation_id):
                object_plan = self.object_edit_planner.plan(
                    deck,
                    slide_id,
                    instruction,
                    deck_plan=changed_plan,
                    **source_context,
                    **retry_budget_context,
                    **({"additional_slide_ids": list(additional_slide_ids)}
                       if additional_slide_ids else {}),
                    **({"required_slide_ids": list(required_slide_ids)}
                       if required_slide_ids else {}),
                )
            validate_requested_object_edit_scope(
                deck, slide_id, additional_slide_ids, plan=object_plan,
                required_slide_ids=required_slide_ids,
            )
            object_edit_slide_ids = [
                object_plan.slide_id,
                *object_plan.additional_slide_ids,
            ]
            intent_patches = object_plan.resolved_intent_patches()
            is_speaker_notes_only = bool(object_plan.speaker_notes_patches)
            if source_fact_corrections and not object_plan.actions:
                raise ValueError("source_fact_correction: native object actions are required")
            if intent_patches:
                if changed_plan is None:
                    raise ValueError(
                        "当前作品缺少 DeckPlan，不能修订作者主判断",
                    )
                changed_plan = apply_slide_intent_patch(
                    deck,
                    changed_plan,
                    object_plan,
                    brief=edit_brief,
                )
                intent_patch_slide_ids = [patch.slide_id for patch in intent_patches]
            if object_plan.actions or is_speaker_notes_only:
                changed_deck, changed_object_ids = apply_object_edit_plan(
                    deck,
                    object_plan,
                )
                # Declared scope permits edits; it does not require rewriting
                # untouched or intent-only native pages.
                original_slides = {item.id: item for item in deck.slides}
                compiled_slide_ids = [
                    item.id
                    for item in changed_deck.slides
                    if item != original_slides.get(item.id)
                ]
                is_semantic_redesign = True
            else:
                # An intent correction changes critic context, not the
                # native slide bytes.  Keep the exact verified DeckIR as
                # the artifact authority instead of persisting harmless
                # reconciliation/measurement noise as a false page edit.
                changed_deck = checkpoint_deck
                is_intent_only = True
            validate_required_object_edit_coverage(
                deck, changed_deck if object_plan.actions or is_speaker_notes_only else deck,
                required_slide_ids or [],
                changed_intent_slide_ids=intent_patch_slide_ids,
            )
        else:
            (
                changed_deck,
                replacements,
                changed_object_ids,
                is_semantic_redesign,
            ) = self._natural_edit_candidate(
                job_id,
                record,
                deck,
                plan,
                slide_id,
                instruction,
            )

        if (
            is_native_pptx_engine(record.request.composition_engine)
            and not is_intent_only
            and not is_speaker_notes_only
        ):
            if is_semantic_redesign:
                if edit_brief is None:
                    brief_path = self.service.store.checkpoint_path(job_id, "brief.json")
                    edit_brief = (
                        PresentationBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
                        if brief_path.is_file() else build_brief(record.request)
                    )
                changed_deck = compose_deck_ir_with_design_context(
                    changed_deck,
                    record.request,
                    edit_brief,
                    plan=changed_plan,
                    slide_ids=compiled_slide_ids,
                )
            changed_deck = prepare_deck_for_composition(changed_deck)

        changed_slides_by_id = {item.id: item for item in changed_deck.slides}
        missing_edit_slides = [
            target_id
            for target_id in object_edit_slide_ids
            if target_id not in changed_slides_by_id
        ]
        if missing_edit_slides:
            raise ValueError(f"未知幻灯片：{missing_edit_slides[0]}")
        target_slide = changed_slides_by_id[slide_id]
        operation = PresentationEditOperation(
            deck_id=deck.id,
            slide_ids=list(
                dict.fromkeys(
                    [
                        *object_edit_slide_ids,
                        *intent_patch_slide_ids,
                    ],
                ),
            ),
            object_ids=(
                changed_object_ids
                if changed_object_ids
                else (
                    []
                    if is_intent_only or is_speaker_notes_only
                    else [obj.id for obj in target_slide.objects]
                )
            ),
            allowed_content_types=(
                {"speaker_notes"}
                if is_speaker_notes_only
                else {"text", "image", "metric", "table", "chart", "shape"}
                if is_semantic_redesign
                else (set() if is_intent_only else {"text"})
            ),
            instruction=instruction.strip(),
        )

        return (
            plan,
            changed_plan,
            replacements,
            is_semantic_redesign,
            is_intent_only,
            intent_patch_slide_ids,
            object_edit_slide_ids,
            compiled_slide_ids,
            changed_deck,
            operation,
        )

    def _verify_edit_candidate(
        self,
        job_id: str,
        operation: PresentationEditOperation,
        changed_deck: DeckIR,
        candidate_path: Path,
        is_semantic_redesign: bool,
        is_intent_only: bool,
        changed_plan: DeckPlan | None,
        *,
        revalidated_legacy_base: bool = False,
    ) -> tuple:
        render_dir = (
            self.service.store.job_dir(job_id)
            / "renders"
            / operation.operation_id
        )
        report, rendered_images = self.service.verify_artifact(
            changed_deck,
            candidate_path,
            render_dir,
            visual_required=True,
            vision_critique_required=(is_semantic_redesign or is_intent_only or revalidated_legacy_base),
            plan=changed_plan,
            render_set_id=operation.operation_id,
            # An explicit edit has its own bounded operation. Verification
            # shares that operation's writer, deadline and critic budget.
            job_id=job_id,
        )
        # Keep accepted candidate diagnostics even when verification rejects
        # publication. This writer-fenced checkpoint never replaces the base
        # delivery report or enters the authoritative version ledger.
        self.service.store.write_checkpoint(
            job_id,
            f"edit-quality-report-{operation.operation_id}.json",
            report.model_dump_json(indent=2),
        )
        if not is_verified_delivery(
            changed_deck,
            report,
            candidate_path,
            rendered_images,
        ):
            blockers = "、".join(
                sorted(
                    {
                        issue.code
                        for issue in report.issues
                        if issue.severity == "error"
                    },
                ),
            )
            detail = f"（{blockers}）" if blockers else ""
            raise ValueError(
                "候选修改未通过内容、可编辑性和逐页视觉验证" + detail,
            )

        return render_dir, report, rendered_images

    def _complete_edit_publication(
        self,
        job_id: str,
        source: Path,
        changed_deck: DeckIR,
        plan: DeckPlan | None,
        changed_plan: DeckPlan | None,
        original_plan_text: str | None,
        plan_path: Path,
        quality_path: Path,
        report: QualityReport,
        committed_sha256: str,
        rendered_images: tuple[Path, ...],
        is_intent_only: bool,
        version_store: PresentationVersionStore,
        base_version: PresentationVersion,
        writer_lease: PresentationWriterLease,
        render_dir: Path,
        operation: PresentationEditOperation,
    ) -> None:
        if changed_plan != plan:
            if original_plan_text is None or changed_plan is None:
                raise RuntimeError("作者主判断修订缺少可回滚的 DeckPlan")
            plan_history = (
                self.service.store.job_dir(job_id)
                / "checkpoints"
                / "history"
                / f"intent-edit-{operation.operation_id}"
                / "deck-plan.json"
            )
            if plan_history.exists():
                raise RuntimeError("作者主判断历史检查点已存在")
            plan_history.parent.mkdir(parents=True, exist_ok=False)
            self._atomic_write_bytes(
                plan_history,
                original_plan_text.encode("utf-8"),
            )
            self._atomic_write_bytes(
                plan_path,
                changed_plan.model_dump_json(indent=2).encode("utf-8"),
            )
        self._atomic_write_bytes(
            quality_path,
            report.model_dump_json(indent=2).encode("utf-8"),
        )
        if (
            committed_sha256 != report.output_sha256
            or not is_verified_delivery(
                changed_deck,
                report,
                source,
                rendered_images,
            )
        ):
            raise RuntimeError("提交后的最终文件与候选验证证据不一致")

        if not is_intent_only:
            version_store.record(
                source,
                changed_deck,
                writer="lantai_ai_edit",
                parent=base_version,
                generation=writer_lease.generation,
                base_pptx_sha256=writer_lease.base_pptx_sha256,
                expected_pptx_sha256=committed_sha256,
                quality_report_path=quality_path,
                render_dir=render_dir,
            )

    @staticmethod
    def _missing_visual_verification(report: QualityReport) -> bool:
        return (
            not report.vision_critique_required
            or not report.visual_verified
            or report.visual_evidence != EvidenceState.PASSED
        )

    def _load_verified_delivery_checkpoint(
        self,
        job_id: str,
    ) -> tuple[DeckIR, QualityReport, Path, Path] | None:
        deck_path = self.service.store.checkpoint_path(job_id, "deck-ir.json")
        quality_path = (
            self.service.store.job_dir(job_id)
            / "quality"
            / "quality-report.json"
        )
        output = (
            self.service.store.job_dir(job_id) / "output" / "presentation.pptx"
        )
        if not (
            deck_path.is_file() and quality_path.is_file() and output.is_file()
        ):
            return None
        try:
            deck = DeckIR.model_validate_json(
                deck_path.read_text(encoding="utf-8"),
            )
            report = QualityReport.model_validate_json(
                quality_path.read_text(encoding="utf-8"),
            )
        except (OSError, ValueError):
            return None
        try:
            rendered_paths = slide_image_paths(
                self._render_dir_for_report(job_id, report),
            )
        except ValueError:
            return None
        if not is_verified_delivery(deck, report, output, rendered_paths):
            return None
        rechecked = inspect_pptx(
            deck,
            output,
            rendered_slide_count=len(rendered_paths),
            repair_rounds=report.repair_rounds,
        )
        if (
            not rechecked.passed
            or rechecked.delivery_status != "passed"
            or rechecked.output_sha256 != report.output_sha256
        ):
            return None

        return deck, report, output, quality_path

    def _load_or_create_brief(
        self,
        job_id: str,
        request: PresentationJobRequest,
        brief_path: Path,
    ) -> PresentationBrief | None:
        if not brief_path.is_file():
            if not self._active(job_id):
                return None
            with self._measure_stage(
                job_id,
                stage=JobStage.BRIEF,
                operation="brief",
                route_id="deterministic-brief-v1",
            ):
                brief = build_brief(request)
                self.service.store.assert_execution_budget(job_id)
            brief_path = self.service.store.write_checkpoint(
                job_id,
                "brief.json",
                brief.model_dump_json(indent=2),
            )
            self.service.store.append_event(
                job_id,
                stage=JobStage.BRIEF,
                status=JobStatus.RUNNING,
                message="已形成演示目标与叙事约束",
                checkpoint=str(brief_path),
            )
        else:
            brief = PresentationBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8"),
            )
        return brief

    def _load_or_create_plan(
        self,
        job_id: str,
        request: PresentationJobRequest,
        plan_path: Path,
        brief: PresentationBrief,
        design_context: PresentationDesignContext,
    ) -> DeckPlan | None:
        if not plan_path.is_file():
            if not self._active(job_id):
                return None
            self.service.store.assert_execution_budget(job_id)
            if request.planning_mode == PlanningMode.MODEL:
                if hasattr(self.semantic_planner, "last_model_label"):
                    self.semantic_planner.last_model_label = None
                plan_cost_units = self._semantic_plan_cost_units(request)
                with self._measure_stage(
                    job_id,
                    stage=JobStage.PLAN,
                    operation="plan",
                    route_id=type(self.semantic_planner).__name__,
                    route_resolver=lambda: getattr(
                        self.semantic_planner,
                        "last_model_label",
                        None,
                    ),
                    cost_units=plan_cost_units,
                    provider_cost_status=(
                        PresentationProviderCostStatus.UNKNOWN
                    ),
                ):
                    self.service.store.consume_cost(job_id, plan_cost_units)
                    try:
                        plan = self._semantic_plan(
                            request,
                            brief,
                            design_context,
                        )
                    finally:
                        self._write_semantic_plan_diagnostics(job_id)
                    self.service.store.assert_execution_budget(job_id)
            else:
                with self._measure_stage(
                    job_id,
                    stage=JobStage.PLAN,
                    operation="plan",
                    route_id="deterministic-fixture-planner-v1",
                ):
                    plan = enrich_plan_with_design_context(
                        request,
                        brief,
                        build_deterministic_fixture_plan(request, brief),
                        reference_context=design_context,
                    )
                    self.service.store.assert_execution_budget(job_id)
            plan = plan.model_copy(
                update={"source_coverage": list(brief.source_coverage)},
            )
            plan_path = self.service.store.write_checkpoint(
                job_id,
                "deck-plan.json",
                plan.model_dump_json(indent=2),
            )
            self.service.store.append_event(
                job_id,
                stage=JobStage.PLAN,
                status=JobStatus.RUNNING,
                message=f"大纲已确定，共 {len(plan.slides)} 页",
                checkpoint=str(plan_path),
            )
        else:
            plan = DeckPlan.model_validate_json(
                plan_path.read_text(encoding="utf-8"),
            )
        return plan

    def _load_or_create_deck(
        self,
        job_id: str,
        request: PresentationJobRequest,
        deck_path: Path,
        brief: PresentationBrief,
        plan: DeckPlan,
    ) -> DeckIR | None:
        if not deck_path.is_file():
            if not self._active(job_id):
                return None
            with self._measure_stage(
                job_id,
                stage=JobStage.COMPOSE,
                operation="author",
                route_id="deck-ir-scaffold-v2",
            ):
                deck = build_deck_ir(request, plan)
                self.service.store.assert_execution_budget(job_id)
            if request.planning_mode == PlanningMode.MODEL:
                deck = self._author_pages(job_id, request, brief, plan, deck)
            deck_path = self.service.store.write_checkpoint(
                job_id,
                "deck-ir.json",
                deck.model_dump_json(indent=2),
            )
        elif request.planning_mode == PlanningMode.MODEL:
            (
                deck,
                migrated_local_ids,
            ) = self._load_saved_model_deck_checkpoint(deck_path)
            if migrated_local_ids:
                legacy_id_path = self.service.store.checkpoint_path(
                    job_id,
                    "deck-ir-legacy-page-local-object-ids.json",
                )
                if not legacy_id_path.exists():
                    shutil.copy2(deck_path, legacy_id_path)
                deck_path = self.service.store.write_checkpoint(
                    job_id,
                    "deck-ir.json",
                    deck.model_dump_json(indent=2),
                )
                self.service.store.append_event(
                    job_id,
                    stage=JobStage.DECK_IR,
                    status=JobStatus.RUNNING,
                    message="已将旧页级作者的局部对象 ID 作用域化；文字、数据、坐标及原生关系未改变",
                    checkpoint=str(deck_path),
                )
            scaffold = build_deck_ir(request, plan)
            if deck == scaffold:
                legacy_path = self.service.store.checkpoint_path(
                    job_id,
                    "deck-ir-legacy-before-page-author.json",
                )
                if not legacy_path.exists():
                    shutil.copy2(deck_path, legacy_path)
                deck = self._author_pages(
                    job_id,
                    request,
                    brief,
                    plan,
                    scaffold,
                )
                deck_path = self.service.store.write_checkpoint(
                    job_id,
                    "deck-ir.json",
                    deck.model_dump_json(indent=2),
                )
            else:
                deck = self._validate_saved_model_deck(
                    request,
                    brief,
                    plan,
                    deck,
                )
        else:
            deck = DeckIR.model_validate_json(
                deck_path.read_text(encoding="utf-8"),
            )
        return deck

    def _load_edit_record(
        self,
        job_id: str,
        conversation_id: str,
        source: Path,
        deck_path: Path,
        quality_path: Path,
        base_output_sha256: str,
    ) -> tuple:
        record = self.get(job_id, conversation_id)
        if record.status != JobStatus.COMPLETED:
            raise ValueError("只有已完成并通过验证的演示文稿可以编辑")
        if (
            not source.is_file()
            or not deck_path.is_file()
            or not quality_path.is_file()
        ):
            raise ValueError("演示文稿缺少可验证的交付文件或检查点")
        if file_sha256(source) != base_output_sha256:
            raise RuntimeError("文件已被其他操作修改；请刷新后基于最新版本重试")

        version_store = PresentationVersionStore(
            self.service.store.job_dir(job_id),
        )
        current_version = version_store.current()

        return record, version_store, current_version

    def _settle_failed_edit(
        self,
        job_id: str,
        slide_id: str,
        source: Path,
        preview,
        receipt,
        base_output_sha256: str,
        deck_path: Path,
        original_deck_text: str | None,
        plan_path: Path,
        original_plan_text: str | None,
        quality_path: Path,
        original_quality_bytes: bytes | None,
        *,
        restored_result: bool | None = None,
    ) -> None:
        restored = restored_result if restored_result is not None else self._restore_edit_source(
            source,
            preview,
            (receipt.committed_sha256 if receipt is not None else None),
            base_output_sha256,
            deck_path,
            original_deck_text,
            plan_path,
            original_plan_text,
            quality_path,
            original_quality_bytes,
        )
        current = self.service.store.get(job_id)
        if current.status in {
            JobStatus.CANCELLED, JobStatus.PAUSED, JobStatus.FAILED,
        }:
            return
        self.service.store.append_event(
            job_id,
            stage=(JobStage.COMPLETED if restored else JobStage.FAILED),
            status=(JobStatus.COMPLETED if restored else JobStatus.FAILED),
            slide_id=slide_id,
            message=(
                "候选修改未通过验证，原版本已完整保留" if restored else "修改失败且无法确认原版本已完整恢复"
            ),
            checkpoint=str(source),
            recoverable=not restored,
            settlement=True,
        )

    def _acquire_edit_lock(self, job_id: str):
        with self._lock:
            edit_lock = self._edit_locks.setdefault(job_id, threading.Lock())
        if not edit_lock.acquire(blocking=False):
            raise RuntimeError("该演示文稿已有一项修改正在验证")

        return edit_lock

    def _edit_paths(self, job_id: str) -> tuple[Path, Path, Path, Path]:
        source = (
            self.service.store.job_dir(job_id) / "output" / "presentation.pptx"
        )
        deck_path = self.service.store.checkpoint_path(job_id, "deck-ir.json")
        plan_path = self.service.store.checkpoint_path(
            job_id,
            "deck-plan.json",
        )
        quality_path = (
            self.service.store.job_dir(job_id)
            / "quality"
            / "quality-report.json"
        )

        return source, deck_path, plan_path, quality_path

    def _prepare_edit_preview(
        self,
        record: PresentationJobRecord,
        edit_service: PresentationEditTransactionService,
        source: Path,
        deck: DeckIR,
        changed_deck: DeckIR,
        operation: PresentationEditOperation,
        is_semantic_redesign: bool,
        compiled_slide_ids: list[str],
        replacements,
    ) -> PresentationEditPreview:
        if operation.allowed_content_types == {"speaker_notes"}:
            preview = edit_service.prepare_speaker_notes(
                source, deck, changed_deck, operation,
            )
        elif is_semantic_redesign:
            preview = edit_service.prepare_compiled_redesign(
                source,
                deck,
                changed_deck,
                operation,
                self.service.compiler_for(
                    record.request.composition_engine,
                ),
                compiled_slide_ids=compiled_slide_ids,
            )
        else:
            preview = edit_service.prepare(
                source,
                deck,
                operation,
                replacements or [],
            )

        return preview

    @staticmethod
    def _assert_intent_base(source: Path, base_output_sha256: str) -> None:
        if file_sha256(source) != base_output_sha256:
            raise RuntimeError(
                "文件在主判断复核期间发生变化；已拒绝提交过期元数据",
            )

    def _author_pages(
        self,
        job_id: str,
        request: PresentationJobRequest,
        brief: PresentationBrief,
        plan: DeckPlan,
        deck: DeckIR,
    ) -> DeckIR:
        if self.page_author is None:
            raise RuntimeError(
                "模型规划的演示缺少页级原生作者；禁止静默退回旧构图器",
            )
        route_id = type(self.page_author).__name__
        source_text = grounded_source_text(request, brief)
        parallelism = getattr(self.page_author, "max_parallel_pages", 1)
        if isinstance(parallelism, bool) or not isinstance(parallelism, int):
            parallelism = 1
        authored_slides = author_pages_ordered(
            list(zip(plan.slides, deck.slides, strict=True)),
            lambda planned, scaffold: self._author_page(
                job_id, request, brief, plan, planned, scaffold, route_id,
                source_text,
            ),
            max_workers=parallelism,
        )
        return DeckIR.model_validate(
            {
                **deck.model_dump(mode="json"),
                "slides": [
                    slide.model_dump(mode="json") for slide in authored_slides
                ],
            },
        )

    @staticmethod
    def _load_saved_model_deck_checkpoint(
        deck_path: Path,
    ) -> tuple[DeckIR, bool]:
        """Read one model checkpoint and migrate the old page-local id bug.

        Only the exact cross-slide duplicate-id validation failure is eligible.
        Every other malformed checkpoint remains a hard failure.  The
        migration changes native identity references only and re-validates the
        full DeckIR before returning it.
        """

        payload = json.loads(deck_path.read_text(encoding="utf-8"))
        try:
            return DeckIR.model_validate(payload), False
        except ValidationError as exc:
            errors = exc.errors()
            if not errors or any(
                error.get("msg")
                != "Value error, object ids must be unique across the deck"
                for error in errors
            ):
                raise
        if not isinstance(payload, dict) or not isinstance(
            payload.get("slides"),
            list,
        ):
            raise ValueError("saved model deck has no slide payload")
        slides = [
            scope_authored_page_object_ids(DeckSlide.model_validate(raw_slide))
            for raw_slide in payload["slides"]
        ]
        migrated = DeckIR.model_validate(
            {
                **payload,
                "slides": [slide.model_dump(mode="json") for slide in slides],
            },
        )
        return migrated, True

    @staticmethod
    def _validate_saved_model_deck(
        request: PresentationJobRequest,
        brief: PresentationBrief,
        plan: DeckPlan,
        deck: DeckIR,
    ) -> DeckIR:
        """Never resume a model job through an unchecked legacy composition."""

        scaffold = build_deck_ir(request, plan)
        if len(deck.slides) != len(plan.slides):
            raise ValueError("saved model deck does not match the storyboard")
        source_text = grounded_source_text(request, brief)
        slides = [
            validate_authored_page(
                slide,
                planned,
                base,
                source_text=source_text,
                source_brief=brief,
            )
            for slide, planned, base in zip(
                deck.slides,
                plan.slides,
                scaffold.slides,
                strict=True,
            )
        ]
        return DeckIR.model_validate(
            {
                **deck.model_dump(mode="json"),
                "slides": [slide.model_dump(mode="json") for slide in slides],
            },
        )

    def _semantic_plan(
        self,
        request: PresentationJobRequest,
        brief: PresentationBrief,
        design_context: PresentationDesignContext,
    ) -> DeckPlan:
        """
        Call the new planner contract while preserving injected v2 adapters.
        """

        if self.semantic_planner is None:
            raise RuntimeError(
                "默认语义规划器不可用；生产请求禁止静默降级为规则式 fixture",
            )
        try:
            plan = self.semantic_planner.plan(
                request,
                brief,
                design_context=design_context,
            )
        except TypeError as exc:
            # Compatibility is intentionally limited to the historical
            # two-argument planner interface.  The returned plan is still
            # promoted into the current unified context before composition.
            if "design_context" not in str(exc):
                raise
            plan = self.semantic_planner.plan(request, brief)
        return enrich_plan_with_design_context(
            request,
            brief,
            plan,
            reference_context=design_context,
        )

    def _semantic_plan_cost_units(
        self,
        request: PresentationJobRequest,
    ) -> int:
        """Reserve every utility call while preserving legacy planner cost."""

        estimator: Callable[[PresentationJobRequest], object] = getattr(
            self.semantic_planner,
            "estimated_cost_units",
            None,
        )
        units = (
            self._invoke_callback(estimator, request)
            if callable(estimator)
            else 10
        )
        if isinstance(units, bool) or not isinstance(units, int) or units < 1:
            raise ValueError(
                "semantic planner cost estimate must be a positive integer",
            )
        return units

    @staticmethod
    def _safe_semantic_numeric_map(
        value: object,
        *,
        integer_only: bool,
    ) -> dict[str, int | float]:
        """Copy planner telemetry without prompts, model text or exceptions."""

        if not isinstance(value, dict):
            return {}
        safe: dict[str, int | float] = {}
        for key, item in value.items():
            if not isinstance(key, str) or isinstance(item, bool):
                continue
            if integer_only:
                if isinstance(item, int) and item >= 0:
                    safe[key] = item
            elif isinstance(item, (int, float)) and item >= 0:
                safe[key] = item
        return dict(sorted(safe.items()))

    @staticmethod
    def _safe_semantic_contract_failures(
        value: object,
    ) -> list[dict[str, object]]:
        """
        Whitelist structural retry facts without persisting model content.
        """

        if not isinstance(value, list):
            return []
        from .intelligence import safe_semantic_schema_issues

        safe_failures: list[dict[str, object]] = []
        count_keys = (
            "attempt",
            "expected_claim_count",
            "content_block_count",
            "claim_group_count",
        )
        identifier_keys = (
            "missing_claim_ids",
            "duplicate_claim_ids",
            "unknown_claim_ids",
        )
        failure_kinds = {
            "empty_output",
            "invalid_json",
            "schema_validation",
            "numeric_grounding",
            "semantic_contract",
            "ledger_capacity",
            "ledger_contiguous_ids",
            "ledger_known_source_refs",
            "ledger_delivery_ids",
            "ledger_delivery_coverage",
            "ledger_delivery_source_scope",
            "ledger_delivery_value",
            "ledger_delivery_semantics",
            "calculation_schema",
            "calculation_scope",
            "calculation_authorization",
            "calculation_input_excerpt",
            "calculation_input_value_unit",
            "calculation_recomputation",
            "calculation_individual_sources",
        }
        for raw in value[:32]:
            if not isinstance(raw, dict):
                continue
            stage = raw.get("stage")
            violation_code = raw.get("violation_code")
            if not isinstance(stage, str) or not re.fullmatch(
                r"(?:claims(?::\d{3})?|outline|detail:\d{3}-\d{3})",
                stage,
            ):
                continue
            if not isinstance(violation_code, str) or not re.fullmatch(
                r"[a-z][a-z0-9_]{0,63}",
                violation_code,
            ):
                continue
            item: dict[str, object] = {
                "stage": stage,
                "violation_code": violation_code,
            }
            failure_kind = raw.get("failure_kind")
            if isinstance(failure_kind, str) and failure_kind in failure_kinds:
                item["failure_kind"] = failure_kind
            schema_issues = safe_semantic_schema_issues(raw.get("schema_issues"))
            if schema_issues:
                item["schema_issues"] = schema_issues
            for key in count_keys:
                count = raw.get(key)
                if (
                    isinstance(count, int)
                    and not isinstance(count, bool)
                    and count >= 0
                ):
                    item[key] = count
            for key in identifier_keys:
                identifiers = raw.get(key)
                if not isinstance(identifiers, list):
                    continue
                item[key] = sorted(
                    dict.fromkeys(
                        str(identifier)
                        for identifier in identifiers
                        if re.fullmatch(r"claim-\d{3}", str(identifier))
                    ),
                )[:16]
            target_orders = raw.get("target_orders")
            if isinstance(target_orders, list):
                item["target_orders"] = sorted(
                    dict.fromkeys(
                        order
                        for order in target_orders
                        if (
                            isinstance(order, int)
                            and not isinstance(order, bool)
                            and 1 <= order <= 999
                        )
                    ),
                )[:16]
            safe_failures.append(item)
        return sorted(
            safe_failures,
            key=lambda item: (
                str(item.get("stage", "")),
                int(cast(int, item.get("attempt", 0))),
            ),
        )

    def _write_semantic_plan_diagnostics(self, job_id: str) -> Path | None:
        """
        Persist safe per-substage timing for the current planning attempt.
        """

        planner = self.semantic_planner
        if planner is None:
            return None
        timings = self._safe_semantic_numeric_map(
            getattr(planner, "last_stage_timings_ms", None),
            integer_only=False,
        )
        attempts = self._safe_semantic_numeric_map(
            getattr(planner, "last_stage_attempt_counts", None),
            integer_only=True,
        )
        budgets = self._safe_semantic_numeric_map(
            getattr(planner, "last_stage_output_token_budgets", None),
            integer_only=True,
        )
        if not (timings or attempts or budgets):
            return None
        utility_call_count = getattr(planner, "last_utility_call_count", 0)
        if isinstance(utility_call_count, bool) or not isinstance(
            utility_call_count,
            int,
        ):
            utility_call_count = 0
        total_output_token_budget = getattr(
            planner,
            "last_output_token_budget",
            None,
        )
        if (
            isinstance(total_output_token_budget, bool)
            or not isinstance(total_output_token_budget, int)
            or total_output_token_budget < 0
        ):
            total_output_token_budget = None
        record = self.service.store.get(job_id)
        payload = {
            "schema_version": "1.0",
            "recovery_attempt_count": record.recovery_attempt_count,
            "external_recovery_attempt_count": (
                record.external_recovery_attempt_count
            ),
            "implementation_recovery_attempt_count": (
                record.implementation_recovery_attempt_count
            ),
            "stage_timings_ms": timings,
            "stage_attempt_counts": attempts,
            "stage_output_token_budgets": budgets,
            "utility_call_count": max(0, utility_call_count),
            "total_output_token_budget": total_output_token_budget,
        }
        contract_failures = self._safe_semantic_contract_failures(
            getattr(
                planner,
                "last_stage_contract_diagnostics",
                getattr(planner, "last_stage_contract_failures", None),
            ),
        )
        if contract_failures:
            payload["stage_contract_failures"] = contract_failures
        path = self.service.store.write_checkpoint(
            job_id,
            (
                "semantic-plan-diagnostics-recovery-"
                f"{record.recovery_attempt_count:03d}"
                + (
                    f"-external-{record.external_recovery_attempt_count:03d}"
                    if record.external_recovery_attempt_count
                    else ""
                )
                + (
                    "-implementation-"
                    f"{record.implementation_recovery_attempt_count:03d}"
                    if record.implementation_recovery_attempt_count
                    else ""
                )
                + ".json"
            ),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )
        current = self.service.store.get(job_id)
        if current.status not in {JobStatus.PAUSED, JobStatus.CANCELLED}:
            self.service.store.append_event(
                job_id,
                stage=JobStage.PLAN,
                status=JobStatus.RUNNING,
                message="已记录语义规划各子阶段耗时与尝试次数",
                checkpoint=str(path),
            )
        return path

    @_admit_runtime_task
    def create_and_start(
        self,
        request: PresentationJobRequest,
        *,
        skill_identity: SkillExecutionIdentity | None = None,
    ) -> PresentationJobRecord:
        record = self.service.create_job(
            request,
            skill_identity=skill_identity,
        )
        self._submit(record.job_id)
        return self.service.store.get(record.job_id)

    def list_jobs(self, conversation_id: str) -> list[PresentationJobRecord]:
        return self.service.store.list_records(conversation_id)

    def get(self, job_id: str, conversation_id: str) -> PresentationJobRecord:
        return self.service.store.get_for_conversation(job_id, conversation_id)

    def pause(self, job_id: str, conversation_id: str):
        return self.service.store.pause(job_id, conversation_id)

    @_admit_runtime_task
    def resume(self, job_id: str, conversation_id: str):
        record = self.get(job_id, conversation_id)
        self.service.pipeline_registry.assert_pinned_available(
            record.pipeline_version,
        )
        event = self.service.store.resume(job_id, conversation_id)
        self._submit(job_id, queue_after_current=True)
        return event

    @_admit_runtime_task
    def retry_failed(self, job_id: str, conversation_id: str):
        """Restart one classified transient failure on its original job."""

        record = self.get(job_id, conversation_id)
        self.service.pipeline_registry.assert_pinned_available(
            record.pipeline_version,
        )
        event = self.service.store.retry_failed(job_id, conversation_id)
        self._submit(job_id, queue_after_current=True)
        return event

    @_admit_runtime_task
    def retry_failed_after_review(
        self,
        job_id: str,
        conversation_id: str,
        *,
        reviewed_routes: Sequence[PresentationFailureRoute],
        review_basis: str,
    ):
        """Restart an explicitly reviewed failure on its original job."""

        record = self.get(job_id, conversation_id)
        self.service.pipeline_registry.assert_pinned_available(
            record.pipeline_version,
        )
        event = self.service.store.retry_failed_after_review(
            job_id,
            conversation_id,
            reviewed_routes=reviewed_routes,
            review_basis=review_basis,
        )
        self._submit(job_id, queue_after_current=True)
        return event

    @_admit_runtime_task
    def retry_failed_after_external_recovery(
        self,
        job_id: str,
        conversation_id: str,
        *,
        reviewed_routes: Sequence[PresentationFailureRoute],
        review_basis: str,
        max_attempts: int | None = None,
    ):
        """Continue the original job after verified provider-state recovery."""

        record = self.get(job_id, conversation_id)
        self.service.pipeline_registry.assert_pinned_available(
            record.pipeline_version,
        )
        event = self.service.store.retry_failed_after_external_recovery(
            job_id,
            conversation_id,
            reviewed_routes=reviewed_routes,
            review_basis=review_basis,
            **(
                {"max_attempts": max_attempts}
                if max_attempts is not None
                else {}
            ),
        )
        self._submit(job_id, queue_after_current=True)
        return event

    @_admit_runtime_task
    def retry_failed_after_implementation_fix(
        self,
        job_id: str,
        conversation_id: str,
        *,
        reviewed_routes: Sequence[PresentationFailureRoute],
        review_basis: str,
        fix_identity: str,
        max_attempts: int | None = None,
    ):
        """Continue the original job after a verified deterministic fix."""

        record = self.get(job_id, conversation_id)
        self.service.pipeline_registry.assert_pinned_available(
            record.pipeline_version,
        )
        event = self.service.store.retry_failed_after_implementation_fix(
            job_id,
            conversation_id,
            reviewed_routes=reviewed_routes,
            review_basis=review_basis,
            fix_identity=fix_identity,
            **(
                {"max_attempts": max_attempts}
                if max_attempts is not None
                else {}
            ),
        )
        self._submit(job_id, queue_after_current=True)
        return event

    def cancel(self, job_id: str, conversation_id: str):
        return self.service.store.cancel(job_id, conversation_id)

    @_track_runtime_operation
    def redo_slide(
        self,
        job_id: str,
        conversation_id: str,
        slide_id: str,
        instruction: str,
        base_output_sha256: str,
        *,
        additional_slide_ids: list[str] | None = None,
        required_slide_ids: list[str] | None = None,
        source_fact_corrections: list[SourceFactCorrection | dict] | None = None,
    ) -> PresentationJobRecord:
        """Apply, render and atomically commit one bounded natural edit."""

        edit_lock = self._acquire_edit_lock(job_id)

        preview = None
        receipt = None
        started = False
        lease_context = None
        writer_lease: PresentationWriterLease | None = None
        source, deck_path, plan_path, quality_path = self._edit_paths(job_id)
        original_deck_text: str | None = None
        original_plan_text: str | None = None
        original_quality_bytes: bytes | None = None
        rollback_edit = None
        rollback_result: bool | None = None
        candidate_commit_sha256: str | None = None
        try:
            record, version_store, current_version = self._load_edit_record(
                job_id,
                conversation_id,
                source,
                deck_path,
                quality_path,
                base_output_sha256,
            )
            # Read-only authority preflight must not consume an edit attempt.
            preflight_deck = DeckIR.model_validate_json(
                deck_path.read_text(encoding="utf-8"),
            )
            validate_requested_object_edit_scope(
                preflight_deck, slide_id, additional_slide_ids,
                required_slide_ids=required_slide_ids,
            )
            if additional_slide_ids and self.object_edit_planner is None:
                raise ValueError("multi-page edit scope requires an object edit planner")
            if required_slide_ids and self.object_edit_planner is None:
                raise ValueError("required edit page scope requires an object edit planner")
            if source_fact_corrections and (
                self.object_edit_planner is None or not is_native_pptx_engine(record.request.composition_engine)
            ):
                raise ValueError("source_fact_correction: a native object edit planner is required")
            if source_fact_corrections or preflight_deck.source_fact_corrections:
                preflight_plan = (
                    DeckPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
                    if plan_path.is_file() else None
                )
                _, source_brief = self.service._visual_source_context(job_id)
                apply_source_fact_corrections(
                    preflight_deck, preflight_plan, source_brief, source_fact_corrections or [],
                    allowed_slide_ids=[slide_id, *(additional_slide_ids or [])],
                )
            lease_context = self._writer_lease(
                job_id,
                base_pptx_sha256=base_output_sha256,
                minimum_generation=(
                    current_version.generation if current_version else 0
                ),
                runtime_run_id=self.runtime_run_id,
            )
            writer_lease = lease_context.__enter__()
            self.service.store.assert_execution_budget(job_id)

            original_deck_text = deck_path.read_text(encoding="utf-8")
            original_plan_text = (
                plan_path.read_text(encoding="utf-8")
                if plan_path.is_file()
                else None
            )
            original_quality_bytes = quality_path.read_bytes()
            requested_slide_ids = validate_requested_object_edit_scope(
                DeckIR.model_validate_json(original_deck_text),
                slide_id, additional_slide_ids,
                required_slide_ids=required_slide_ids,
            )
            deck, checkpoint_deck, base_version, revalidated_legacy_base = self._validated_edit_base(
                job_id,
                record,
                source,
                quality_path,
                cast(str, original_deck_text),
                cast(bytes, original_quality_bytes),
                current_version,
                version_store,
                base_output_sha256,
                requested_slide_ids,
            )
            ledger_checkpoint = version_store.publication_checkpoint(
                parent=base_version,
            )

            def rollback_edit() -> None:
                nonlocal rollback_result
                try:
                    ledger_restored = version_store.rollback_unpublished_record(
                        ledger_checkpoint,
                        writer="lantai_ai_edit",
                        generation=writer_lease.generation,
                        base_pptx_sha256=writer_lease.base_pptx_sha256,
                        expected_pptx_sha256=(
                            candidate_commit_sha256 or base_output_sha256
                        ),
                    )
                    # A newer external ledger and its matching file are not
                    # owned by this failed edit.
                    rollback_result = ledger_restored and self._restore_edit_source(
                        source, preview, candidate_commit_sha256,
                        base_output_sha256, deck_path, original_deck_text,
                        plan_path, original_plan_text, quality_path,
                        original_quality_bytes,
                    )
                except (OSError, ValueError):
                    rollback_result = False

            (
                plan,
                changed_plan,
                replacements,
                is_semantic_redesign,
                is_intent_only,
                intent_patch_slide_ids,
                object_edit_slide_ids,
                compiled_slide_ids,
                changed_deck,
                operation,
            ) = self._prepare_edit_candidate(
                job_id,
                record,
                deck,
                checkpoint_deck,
                original_plan_text,
                slide_id,
                instruction,
                additional_slide_ids=additional_slide_ids,
                required_slide_ids=required_slide_ids,
                source_fact_corrections=source_fact_corrections,
            )
            self.service.store.append_event(
                job_id,
                stage=JobStage.REPAIR,
                status=JobStatus.RUNNING,
                slide_id=slide_id,
                message=(
                    (
                        "正在修订并复核本页作者主判断："
                        if is_intent_only
                        else (
                            "正在原子重做、渲染并整套审查关联页面："
                            if len(object_edit_slide_ids) > 1
                            else "正在重新设计、渲染并视觉审查本页："
                        )
                    )
                    if is_semantic_redesign or is_intent_only
                    else "正在应用并验证单页文字修改："
                )
                + instruction.strip()[:120],
                checkpoint=str(source),
                recoverable=True,
            )
            started = True

            candidate_path = source
            edit_service = None
            if not is_intent_only:
                edit_service = PresentationEditTransactionService(
                    self.service.store.job_dir(job_id) / "edits",
                )
                preview = self._prepare_edit_preview(
                    record,
                    edit_service,
                    source,
                    deck,
                    changed_deck,
                    operation,
                    is_semantic_redesign,
                    compiled_slide_ids,
                    replacements,
                )
                if preview.source_sha256 != base_output_sha256:
                    raise RuntimeError(
                        "文件在修改准备期间发生变化；已拒绝覆盖其他操作的结果",
                    )
                candidate_path = Path(preview.candidate_path)
            render_dir, report, rendered_images = self._verify_edit_candidate(
                job_id,
                operation,
                changed_deck,
                candidate_path,
                is_semantic_redesign,
                is_intent_only,
                changed_plan,
                revalidated_legacy_base=revalidated_legacy_base,
            )
            candidate_commit_sha256 = report.output_sha256

            def publish_verified_edit() -> None:
                nonlocal receipt
                if is_intent_only:
                    self._assert_intent_base(source, base_output_sha256)
                    committed_sha256 = base_output_sha256
                else:
                    assert edit_service is not None and preview is not None
                    receipt = edit_service.commit(preview)
                    committed_sha256 = receipt.committed_sha256
                    self.service.store.write_checkpoint(
                        job_id,
                        "deck-ir.json",
                        changed_deck.model_dump_json(indent=2),
                    )
                self._complete_edit_publication(
                    job_id,
                    source,
                    changed_deck,
                    plan,
                    changed_plan,
                    original_plan_text,
                    plan_path,
                    quality_path,
                    report,
                    committed_sha256,
                    rendered_images,
                    is_intent_only,
                    version_store,
                    base_version,
                    writer_lease,
                    render_dir,
                    operation,
                )

            with self.service.store.publication_guard(job_id):
                self.service.store.complete_after_verified_publication(
                    job_id,
                    slide_id=slide_id,
                    message=(
                        "作者主判断修订已提交并通过整套事实与视觉审查"
                        if is_intent_only
                        else (
                            (
                                "跨页原子修订已提交并通过整套事实与视觉审查；变更对象："
                                if len(operation.slide_ids) > 1
                                else "整页重做已提交并通过真实渲染与视觉审查；变更对象："
                                if is_semantic_redesign
                                else "单页修改已提交并通过逐页验证；变更对象："
                            )
                            + "、".join(preview.changed_object_ids)
                        )
                    ),
                    checkpoint=str(source),
                    publish=publish_verified_edit,
                    rollback=rollback_edit,
                )
                return self.service.store.get(job_id)
        except Exception:
            if started:
                if rollback_result is None and rollback_edit is not None:
                    rollback_edit()
                self._settle_failed_edit(
                    job_id,
                    slide_id,
                    source,
                    preview,
                    receipt,
                    base_output_sha256,
                    deck_path,
                    original_deck_text,
                    plan_path,
                    original_plan_text,
                    quality_path,
                    original_quality_bytes,
                    restored_result=rollback_result,
                )
            raise
        finally:
            if lease_context is not None:
                lease_context.__exit__(None, None, None)
            edit_lock.release()

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)

    @classmethod
    def _restore_edit_source(
        cls,
        source: Path,
        preview,
        committed_sha256: str | None,
        base_output_sha256: str,
        deck_path: Path,
        original_deck_text: str | None,
        plan_path: Path,
        original_plan_text: str | None,
        quality_path: Path,
        original_quality_bytes: bytes | None,
    ) -> bool:
        try:
            pptx_restored = False
            if source.is_file():
                current_sha256 = file_sha256(source)
                pptx_restored = current_sha256 == base_output_sha256
                if (
                    not pptx_restored
                    and preview is not None
                    and committed_sha256 is not None
                    and current_sha256 == committed_sha256
                ):
                    snapshot = Path(preview.snapshot_path)
                    temporary = source.with_name(
                        f".{source.name}.rollback.tmp",
                    )
                    shutil.copy2(snapshot, temporary)
                    os.replace(temporary, source)
                    pptx_restored = (
                        file_sha256(source) == base_output_sha256
                    )
            if not pptx_restored:
                # A newer human save is authoritative; do not rewind its
                # adjacent metadata merely because this AI edit failed.
                return False
            if original_deck_text is not None:
                cls._atomic_write_bytes(
                    deck_path,
                    original_deck_text.encode("utf-8"),
                )
            if original_plan_text is not None:
                cls._atomic_write_bytes(
                    plan_path,
                    original_plan_text.encode("utf-8"),
                )
            if original_quality_bytes is not None:
                cls._atomic_write_bytes(quality_path, original_quality_bytes)
            return pptx_restored
        except (OSError, ValueError):
            return False

    def output_path(self, job_id: str, conversation_id: str) -> Path:
        record = self.get(job_id, conversation_id)
        path = (
            self.service.store.job_dir(job_id) / "output" / "presentation.pptx"
        )
        if record.status != JobStatus.COMPLETED or not path.is_file():
            raise FileNotFoundError("presentation output is not ready")
        return path

    def artifacts(
        self,
        job_id: str,
        conversation_id: str,
    ) -> dict[str, object]:
        record = self.get(job_id, conversation_id)
        result: dict[str, object] = {"record": record.model_dump(mode="json")}
        for key, filename, model in (
            ("brief", "brief.json", None),
            ("plan", "deck-plan.json", DeckPlan),
            ("deck_ir", "deck-ir.json", DeckIR),
        ):
            path = self.service.store.checkpoint_path(job_id, filename)
            if not path.is_file():
                continue
            if model is None:
                result[key] = json.loads(path.read_text(encoding="utf-8"))
            else:
                result[key] = model.model_validate_json(
                    path.read_text(encoding="utf-8"),
                ).model_dump(mode="json")
        quality_path = (
            self.service.store.job_dir(job_id)
            / "quality"
            / "quality-report.json"
        )
        quality: dict[str, object] | None = None
        if quality_path.is_file():
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
            result["quality"] = quality
        version_store = PresentationVersionStore(
            self.service.store.job_dir(job_id),
        )
        version = version_store.current()
        if version is not None:
            result["presentation_version"] = version.model_dump(mode="json")
            result["presentation_versions"] = [
                item.model_dump(mode="json")
                for item in version_store.history()
            ]
        render_root = self.service.store.job_dir(job_id) / "renders"
        render_set_id = (
            str(quality.get("render_set_id"))
            if quality and quality.get("render_set_id")
            else None
        )
        if render_set_id and Path(render_set_id).name == render_set_id:
            thumbnail_paths = slide_image_paths(render_root / render_set_id)
        else:
            thumbnail_paths = tuple(sorted(render_root.glob("round-*/*.png")))
        result["thumbnails"] = [str(path) for path in thumbnail_paths]
        output = (
            self.service.store.job_dir(job_id) / "output" / "presentation.pptx"
        )
        deck_checkpoint = self.service.store.checkpoint_path(
            job_id,
            "deck-ir.json",
        )
        if (
            record.status == JobStatus.COMPLETED
            and output.is_file()
            and deck_checkpoint.is_file()
        ):
            deck = DeckIR.model_validate_json(
                deck_checkpoint.read_text(encoding="utf-8"),
            )
            result["editor_handoff"] = {
                "action": "open_office_editor",
                "suite": "slides",
                "job_id": job_id,
                "deck_id": deck.id,
                "presentation_path": str(output),
                "deck_ir_path": str(deck_checkpoint),
                "version_id": version.version_id if version else None,
                "base_pptx_sha256": (
                    version.pptx_sha256 if version else file_sha256(output)
                ),
                "separate_edit_session": True,
            }
        return result

    @_track_runtime_operation
    def register_editor_save(
        self,
        job_id: str,
        conversation_id: str,
        *,
        base_output_sha256: str,
        writer: Literal["genoffice_human", "office_cli"] = "genoffice_human",
    ):
        """
        Register a GenOffice/OfficeCLI save.

        Do not pretend DeckIR is current.
        """

        self.get(job_id, conversation_id)
        if writer not in {"genoffice_human", "office_cli"}:
            raise ValueError("external writer must be GenOffice or OfficeCLI")
        output = (
            self.service.store.job_dir(job_id) / "output" / "presentation.pptx"
        )
        version_store = PresentationVersionStore(
            self.service.store.job_dir(job_id),
        )
        current = version_store.current()
        if current is None:
            raise RuntimeError("外部保存前必须存在受管版本")
        with self._writer_lease(
            job_id,
            base_pptx_sha256=base_output_sha256,
            minimum_generation=current.generation,
            runtime_run_id=self.runtime_run_id,
        ) as writer_lease:
            with self.service.store.publication_guard(job_id):
                return version_store.register_external_save(
                    output,
                    writer=writer,
                    expected_parent_sha256=base_output_sha256,
                    generation=writer_lease.generation,
                    base_pptx_sha256=writer_lease.base_pptx_sha256,
                )

    @_track_runtime_operation
    def reconcile_editor_save(
        self,
        job_id: str,
        conversation_id: str,
        *,
        base_output_sha256: str,
    ):
        """Re-import and visually verify the latest external PPTX save.

        No PPTX mutation happens in this operation.  A new reconciled semantic
        checkpoint is appended only after the exact human-edited file renders,
        reopens and passes the configured vision critic.
        """

        record = self.get(job_id, conversation_id)
        if record.status != JobStatus.COMPLETED:
            raise ValueError("只有已完成的演示文稿可以重导入人工修改")
        job_dir = self.service.store.job_dir(job_id)
        output = job_dir / "output" / "presentation.pptx"
        deck_path = self.service.store.checkpoint_path(job_id, "deck-ir.json")
        plan_path = self.service.store.checkpoint_path(
            job_id,
            "deck-plan.json",
        )
        quality_path = job_dir / "quality" / "quality-report.json"
        if not output.is_file() or not deck_path.is_file():
            raise FileNotFoundError("演示文稿缺少 PPTX 或 DeckIR 检查点")
        if file_sha256(output) != base_output_sha256:
            raise RuntimeError("人工保存文件已再次变化；请刷新后重试")

        version_store = PresentationVersionStore(job_dir)
        current = version_store.current()
        if current is None:
            raise RuntimeError("演示文稿没有受管版本记录")
        if current.pptx_sha256 != base_output_sha256:
            raise RuntimeError("重导入请求不是基于最新 PPTX 版本")
        if current.reconciled:
            raise ValueError("当前 PPTX 已与 DeckIR 对齐，无需重复重导入")

        with self._writer_lease(
            job_id,
            base_pptx_sha256=base_output_sha256,
            minimum_generation=current.generation,
            runtime_run_id=self.runtime_run_id,
        ) as writer_lease:
            original_deck = DeckIR.model_validate_json(
                deck_path.read_text(encoding="utf-8"),
            )
            reconciled = reconcile_pptx(
                original_deck,
                output,
                assets_dir=job_dir
                / "assets"
                / f"reimport-{base_output_sha256[:12]}",
            )
            plan = (
                DeckPlan.model_validate_json(
                    plan_path.read_text(encoding="utf-8"),
                )
                if plan_path.is_file()
                else None
            )
            render_set_id = f"reconcile-{writer_lease.generation}"
            render_dir = job_dir / "renders" / render_set_id
            report, rendered_images = self.service.verify_artifact(
                reconciled,
                output,
                render_dir,
                visual_required=True,
                vision_critique_required=True,
                plan=plan,
                render_set_id=render_set_id,
                job_id=job_id,
            )
            if (
                self._missing_visual_verification(report)
                or any((issue.severity == "error" for issue in report.issues))
                or (
                    not is_verified_delivery(
                        reconciled,
                        report,
                        output,
                        rendered_images,
                    )
                )
                or (report.output_sha256 != base_output_sha256)
            ):
                self._atomic_write_bytes(
                    render_dir / "failed-quality-report.json",
                    report.model_dump_json(indent=2).encode("utf-8"),
                )
                self.service.store.append_event(
                    job_id,
                    stage=JobStage.FAILED,
                    status=JobStatus.FAILED,
                    message="人工保存后的 PPTX 未通过重导入与视觉验证；原文件及语义检查点已保留",
                    failure_routes=failure_routes_for_quality_issues(
                        report.issues,
                    ),
                )
                raise ValueError("人工保存后的 PPTX 未通过重导入与视觉验证")

            with self.service.store.publication_guard(job_id):
                if file_sha256(output) != base_output_sha256:
                    raise RuntimeError("人工保存文件在验证期间再次变化；请刷新后重试")

                previous_deck_bytes = deck_path.read_bytes()
                previous_quality_bytes = (
                    quality_path.read_bytes()
                    if quality_path.is_file()
                    else None
                )
                try:
                    self.service.store.write_checkpoint(
                        job_id,
                        "deck-ir.json",
                        reconciled.model_dump_json(indent=2),
                    )
                    self._atomic_write_bytes(
                        quality_path,
                        report.model_dump_json(indent=2).encode("utf-8"),
                    )
                    version = version_store.record(
                        output,
                        reconciled,
                        writer="external_reconcile",
                        parent=current,
                        generation=writer_lease.generation,
                        base_pptx_sha256=writer_lease.base_pptx_sha256,
                        expected_pptx_sha256=base_output_sha256,
                        quality_report_path=quality_path,
                        render_dir=render_dir,
                    )
                except Exception:
                    # Failed evidence/version persistence must not leave a new
                    # semantic checkpoint attached to the unreconciled version.
                    self._atomic_write_bytes(deck_path, previous_deck_bytes)
                    if previous_quality_bytes is None:
                        quality_path.unlink(missing_ok=True)
                    else:
                        self._atomic_write_bytes(
                            quality_path,
                            previous_quality_bytes,
                        )
                    raise
                self.service.store.append_event(
                    job_id,
                    stage=JobStage.COMPLETED,
                    status=JobStatus.COMPLETED,
                    message=(
                        "已从最新人工保存的 PPTX 重建对象身份；视觉审查发现 "
                        f"{len(report.visual_issues)} 项问题，可继续用自然语言修复"
                        if report.visual_issues
                        else "已从最新人工保存的 PPTX 重建对象身份并通过视觉审查"
                    ),
                    checkpoint=str(deck_path),
                )
                return version

    @_track_runtime_operation
    def rollback_version(
        self,
        job_id: str,
        conversation_id: str,
        *,
        target_version_id: str,
        base_output_sha256: str,
    ):
        """Restore an evidence-bound version without regenerating the deck."""

        record = self.get(job_id, conversation_id)
        if record.status != JobStatus.COMPLETED:
            raise ValueError("只有已完成的演示文稿可以执行版本回滚")
        job_dir = self.service.store.job_dir(job_id)
        output = job_dir / "output" / "presentation.pptx"
        deck_path = self.service.store.checkpoint_path(job_id, "deck-ir.json")
        quality_path = job_dir / "quality" / "quality-report.json"
        version_store = PresentationVersionStore(job_dir)
        current = version_store.current()
        if current is None:
            raise RuntimeError("演示文稿没有受管版本记录")
        with self._writer_lease(
            job_id,
            base_pptx_sha256=base_output_sha256,
            minimum_generation=current.generation,
            runtime_run_id=self.runtime_run_id,
        ) as writer_lease:
            with self.service.store.publication_guard(job_id):
                restored, deck, report = version_store.rollback_to(
                    target_version_id,
                    pptx_path=output,
                    deck_ir_path=deck_path,
                    quality_report_path=quality_path,
                    render_root=job_dir / "renders",
                    generation=writer_lease.generation,
                    base_pptx_sha256=writer_lease.base_pptx_sha256 or "",
                )
                rendered = slide_image_paths(
                    self._render_dir_for_report(job_id, report),
                )
                if not is_verified_delivery(deck, report, output, rendered):
                    raise RuntimeError("回滚后的文件与场景/预览证据不一致")
                self.service.store.append_event(
                    job_id,
                    stage=JobStage.COMPLETED,
                    status=JobStatus.COMPLETED,
                    message=f"已回滚至受验证版本 {target_version_id}",
                    checkpoint=str(output),
                )
                return restored

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        for record in self.service.store.list_records():
            self.service.store.retire_runtime_owner(
                record.job_id,
                self.runtime_run_id,
            )
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._operation_condition:
            while self._synchronous_operations:
                self._operation_condition.wait()
        for record in self.service.store.list_records():
            if (
                record.runtime_run_id == self.runtime_run_id
                and record.resource_settlement_pending
            ):
                raise RuntimeError(
                    "Presentation runtime still owns unsettled resources",
                )

    def recover_unfinished(self) -> None:
        for record in self.service.store.list_records():
            if record.status in {JobStatus.PENDING, JobStatus.RUNNING}:
                self._submit(record.job_id)

    def _submit(
        self,
        job_id: str,
        force_compose: bool = False,
        *,
        queue_after_current: bool = False,
    ) -> None:
        wait_for = None
        with self._lock:
            if self._closed:
                return
            existing = self._futures.get(job_id)
            if existing and not existing.done():
                if not queue_after_current:
                    return
                wait_for = existing
            else:
                # A queued resume may already have been consumed by the first
                # worker. Its completion callback must recheck durable state
                # before dispatching another writer for that same request.
                if self.service.store.get(job_id).status not in {
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                }:
                    return
                self._futures[job_id] = self._executor.submit(
                    self._run,
                    job_id,
                    force_compose,
                )
                return

        # A failed/paused event can be durable just before its worker Future
        # flips to done. Register outside the lock because add_done_callback()
        # may invoke synchronously when that race has already resolved.
        assert wait_for is not None
        wait_for.add_done_callback(
            lambda _future: self._submit(job_id, force_compose),
        )

    def _active(self, job_id: str) -> bool:
        try:
            self.service.store.assert_execution_active(job_id)
            return True
        except PresentationJobInterruptedError:
            return False

    def _complete_from_verified_checkpoint(
        self,
        job_id: str,
        writer_lease: PresentationWriterLease | None = None,
    ) -> bool:
        """
        Finish interrupted delivery only after evidence and ownership verify.
        """

        verified_checkpoint = self._load_verified_delivery_checkpoint(job_id)
        if verified_checkpoint is None:
            return False
        deck, report, output, quality_path = verified_checkpoint
        version_store = PresentationVersionStore(
            self.service.store.job_dir(job_id),
        )
        current_version = version_store.current()
        output_hash = file_sha256(output)
        if current_version is None and writer_lease is None:
            # An unowned promoted file may only be adopted under a fresh
            # writer lease after all evidence has been rechecked.
            return False
        if current_version is not None and (
            current_version.pptx_sha256 != output_hash
            or not deck_ir_matches_version(deck, current_version.deck_ir_version)
            or not current_version.reconciled
        ):
            return False
        recovered_writer = (
            self.service.store.get(job_id).request.composition_engine.value
            if current_version is None
            else None
        )

        def publish_recovered_checkpoint() -> None:
            if file_sha256(output) != report.output_sha256:
                raise RuntimeError(
                    "recovered PPTX changed after evidence verification",
                )
            if current_version is None:
                assert writer_lease is not None
                assert recovered_writer is not None
                version_store.record(
                    output,
                    deck,
                    writer=recovered_writer,
                    generation=writer_lease.generation,
                    base_pptx_sha256=None,
                    expected_pptx_sha256=report.output_sha256,
                    quality_report_path=quality_path,
                    render_dir=self._render_dir_for_report(job_id, report),
                )
            elif version_store.current() != current_version:
                raise RuntimeError(
                    "presentation version changed during checkpoint recovery",
                )

        self.service.store.complete_after_verified_publication(
            job_id,
            message="已从经验证的交付检查点恢复",
            checkpoint=str(output),
            publish=publish_recovered_checkpoint,
        )
        return True

    def _render_dir_for_report(
        self,
        job_id: str,
        report: QualityReport,
    ) -> Path:
        render_set_id = report.render_set_id or f"round-{report.repair_rounds}"
        if Path(render_set_id).name != render_set_id:
            raise ValueError("invalid render evidence directory")
        return self.service.store.job_dir(job_id) / "renders" / render_set_id

    def _run(self, job_id: str, force_compose: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
        original_control_epoch = self.service.store.get(job_id).control_epoch
        version_store = PresentationVersionStore(
            self.service.store.job_dir(job_id),
        )
        current_version = version_store.current()
        try:
            with self._writer_lease(
                job_id,
                base_pptx_sha256=(
                    current_version.pptx_sha256 if current_version else None
                ),
                minimum_generation=(
                    current_version.generation if current_version else 0
                ),
                runtime_run_id=self.runtime_run_id,
            ) as writer_lease:
                with self._lock:
                    if self._closed:
                        return
                self.service.pipeline_registry.assert_pinned_available(
                    self.service.store.get(job_id).pipeline_version,
                )
                if writer_lease.recovered_lease_id:
                    self.service.store.append_event(
                        job_id,
                        stage=self.service.store.get(job_id).stage,
                        status=JobStatus.RUNNING,
                        message="已回收中断进程遗留的写入租约并从检查点恢复",
                        recoverable=True,
                    )
                self._run_with_lease(job_id, writer_lease, force_compose)
        except (PresentationWriterBusyError, PresentationJobInterruptedError):
            return
        except Exception as exc:  # the durable event is the recovery boundary
            record = self.service.store.get(job_id)
            if record.resource_settlement_pending:
                return
            if record.control_epoch != original_control_epoch:
                return
            if record.status not in {JobStatus.CANCELLED, JobStatus.PAUSED}:
                failure_route = failure_route_for_exception(
                    exc,
                    stage=record.stage,
                )
                try:
                    self.service.store.settle_failed_recovery(
                        job_id,
                        message=failure_message_for_exception(
                            exc,
                            route=failure_route,
                        ),
                        failure_routes=[failure_route],
                        expected_control_epoch=original_control_epoch,
                    )
                except (
                    PresentationJobInterruptedError,
                    PresentationWriterBusyError,
                ):
                    return

    def _run_with_lease(
        self,
        job_id: str,
        writer_lease: PresentationWriterLease,
        force_compose: bool = False,
    ) -> None:
        record = self.service.store.get(job_id)
        request = record.request
        brief_path = self.service.store.checkpoint_path(
            job_id,
            "brief.json",
        )
        plan_path = self.service.store.checkpoint_path(
            job_id,
            "deck-plan.json",
        )
        deck_path = self.service.store.checkpoint_path(
            job_id,
            "deck-ir.json",
        )

        if not force_compose and self._complete_from_verified_checkpoint(
            job_id,
            writer_lease,
        ):
            return

        brief = self._load_or_create_brief(job_id, request, brief_path)
        if brief is None:
            return

        context_path = self.service.store.checkpoint_path(
            job_id,
            "presentation-design-context.json",
        )
        if context_path.is_file():
            design_context = self._resolve_job_design_context(
                job_id,
                request,
                brief,
            )
        else:
            with self._measure_stage(
                job_id,
                stage=JobStage.BRIEF,
                operation="design_context",
                route_id=(
                    "reference-deck-analyzer-v1"
                    if request.template_path
                    else "universal-design-intelligence-v1"
                ),
            ):
                design_context = self._resolve_job_design_context(
                    job_id,
                    request,
                    brief,
                )

        plan = self._load_or_create_plan(
            job_id,
            request,
            plan_path,
            brief,
            design_context,
        )
        if plan is None:
            return

        deck = self._load_or_create_deck(
            job_id,
            request,
            deck_path,
            brief,
            plan,
        )
        if deck is None:
            return
        if not self._active(job_id):
            return
        self.service.compose(job_id, deck, writer_lease=writer_lease)
