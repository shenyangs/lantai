# -*- coding: utf-8 -*-
"""Application service for the deterministic presentation vertical slice."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import asdict, replace

from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from threading import Event
from typing import Callable

from PIL import Image

from ..app.structured_utility import StructuredTaskValidationError
from ..utils.operation_control import (
    bind_operation,
    check_operation,
    current_operation,
    remaining_timeout,
)
from .compiler import CompilationResult, PresentationCompiler
from .design_system import prepare_deck_for_composition
from .failure import failure_routes_for_quality_issues
from .jobs import (
    PresentationExecutionLimits,
    PresentationJobInterruptedError,
    PresentationJobStore,
    PresentationWriterLease,
    shared_presentation_execution_limits,
    job_operation,
)
from .models import (
    CompositionEngine,
    DeckIR,
    DeckPlan,
    EvidenceState,
    JobStage,
    JobStatus,
    PresentationJobRequest,
    PresentationBrief,
    PresentationProviderCostStatus,
    PresentationStageMetric,
    PresentationVersion,
    QualityIssue,
    QualityReport,
    RenderedSlideEvidence,
    SkillExecutionIdentity,
    TemplateMode,
    VisualCriticResult,
    VisualCriticSlideInput,
    VisualIssue,
    VisualReviewCheckpoint,
    VisualReviewEvidence,
    is_native_pptx_engine,
)
from .pipeline_registry import PresentationPipelineRegistry
from .natural_edit import validate_source_fact_correction_history
from .planner import build_brief
from .pptx_compiler import NativePptxCompiler
from .quality import (
    delivery_evidence_issues,
    delivery_status_for,
    file_sha256,
    inspect_pptx,
    is_verified_delivery,
)
from .render import (
    PresentationRenderUnavailable,
    render_pptx,
    slide_image_paths,
)
from .rendered_geometry import GLYPH_DIVIDER_RULESET, inspect_rendered_glyph_dividers
from .repair import (
    MAX_REPAIR_ROUNDS,
    PresentationRepairDesigner,
    repair_deck_once,
    repair_deck_with_designer,
)
from .theme import get_theme
from .template_binding import TemplateBindingError, bind_authorized_template
from .versioning import PresentationVersionStore
from .visual_qa import (
    PresentationVisualCritic,
    build_visual_critic_inputs,
)


VISUAL_REVIEW_RULESET_VERSION = "lantai-visual-review/v3"
VISUAL_SCHEMA_ATTEMPT_LIMIT = 2


class _VisualSourceContextError(RuntimeError):
    """A source-loading failure whose message contains no source values."""


_VISUAL_VALIDATION_CODES = frozenset({
    "reference_error", "extra_forbidden", "missing", "literal_error", "enum",
    "list_type", "dict_type", "model_type", "model_attributes_type",
    "string_type", "string_too_short", "string_too_long", "string_unicode",
    "string_pattern_mismatch", "int_type", "int_parsing", "int_from_float",
    "float_type", "float_parsing", "finite_number", "greater_than",
    "greater_than_equal", "less_than", "less_than_equal", "too_short",
    "too_long", "json_invalid", "json_type", "value_error", "assertion_error",
    "schema_error", "verification_error", "verification_extra_fields",
    "verification_blank_evidence",
})
# Only literal schema-owned names from the private source-check protocol are
# allowed here. Do not import model-authored keys or values into diagnostics.
_VISUAL_VERIFICATION_FIELDS = frozenset({
    "verification", "slide_id", "object_ids", "check_type", "verdict",
    "source_refs", "source_evidence", "candidate_evidence", "explanation",
    "issue_indices",
})
_VISUAL_VALIDATION_PATH = re.compile(
    r"\$(?:\.(?:"
    + "|".join(re.escape(name) for name in sorted({
        "issues", *VisualIssue.model_fields, *_VISUAL_VERIFICATION_FIELDS,
    }))
    + r")|\[[0-9]{1,6}\])*",
)


def _safe_visual_validation_issues(
    exc: StructuredTaskValidationError,
) -> tuple[str, ...]:
    """Keep schema-owned diagnostics, never model-owned keys or messages."""

    issues: list[str] = []
    for raw in exc.validation_issues[:24]:
        code, separator, path = raw.partition("@")
        if not separator or code not in _VISUAL_VALIDATION_CODES:
            code, path = "schema_error", "$"
        elif _VISUAL_VALIDATION_PATH.fullmatch(path) is None:
            path = "$"
        safe_issue = f"{code}@{path}"
        if safe_issue not in issues:
            issues.append(safe_issue)
    return tuple(issues)


def _safe_visual_schema_error(
    exc: StructuredTaskValidationError,
) -> StructuredTaskValidationError:
    return StructuredTaskValidationError(
        "visual critic output failed schema validation",
        validation_issues=_safe_visual_validation_issues(exc),
    )


class PresentationService:
    def __init__(
        self,
        root: str | Path,
        *,
        visual_critic: PresentationVisualCritic | None = None,
        repair_designer: PresentationRepairDesigner | None = None,
        execution_limits: PresentationExecutionLimits | None = None,
    ) -> None:
        self.store = PresentationJobStore(root)
        self.pipeline_registry = PresentationPipelineRegistry(root)
        self.compiler = PresentationCompiler()
        self._native_pptx_compiler: NativePptxCompiler | None = None
        self.visual_critic = visual_critic
        self.repair_designer = repair_designer
        self.limits = (
            execution_limits
            if execution_limits is not None
            else shared_presentation_execution_limits()
        )

    def record_stage_metric(
        self,
        job_id: str | None,
        *,
        stage: JobStage,
        operation: str,
        repair_round: int,
        started_at: datetime,
        started_clock: float,
        outcome: str,
        route_id: str | None = None,
        cost_units: int = 0,
        slide_id: str | None = None,
        provider_cost_status: PresentationProviderCostStatus = (
            PresentationProviderCostStatus.NOT_APPLICABLE
        ),
        provider_cost_micro_cny: int | None = None,
    ) -> None:
        if job_id is None:
            return
        self.store.record_stage_metric(
            job_id,
            PresentationStageMetric(
                stage=stage,
                operation=operation,
                repair_round=repair_round,
                slide_id=slide_id,
                route_id=route_id,
                outcome=outcome,
                duration_ms=max(
                    0.0,
                    (time.perf_counter() - started_clock) * 1000,
                ),
                cost_units=cost_units,
                provider_cost_status=provider_cost_status,
                provider_cost_micro_cny=provider_cost_micro_cny,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            ),
        )

    def compiler_for(self, engine: CompositionEngine):
        """Return the single authoritative writer for one composition pass."""

        if engine == CompositionEngine.LEGACY_PYTHON_PPTX:
            return self.compiler
        if self._native_pptx_compiler is None:
            self._native_pptx_compiler = NativePptxCompiler()
        return self._native_pptx_compiler

    @staticmethod
    def _visual_dependency_fingerprint(payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _visual_cache_identity(self, scope: str | None = None) -> str | None:
        if self.visual_critic is None:
            return None
        resolver = (
            getattr(self.visual_critic, f"{scope}_cache_identity", None)
            if scope is not None
            else None
        )
        if not callable(resolver):
            resolver = getattr(self.visual_critic, "cache_identity", None)
        if not callable(resolver):
            return None
        identity = resolver()
        return identity if isinstance(identity, str) and identity else None

    def _visual_source_context(
        self, job_id: str | None,
    ) -> tuple[PresentationJobRequest | None, PresentationBrief | None]:
        if job_id is None:
            return None, None
        try:
            request = self.store.get(job_id).request
            brief_path = self.store.checkpoint_path(job_id, "brief.json")
            # A historical job may lack a brief; an existing but unreadable or
            # invalid brief must never silently fall back to weaker evidence.
            try:
                brief_path.lstat()
            except FileNotFoundError:
                return request, build_brief(request)
            if not brief_path.is_file():
                raise ValueError("source brief is not a readable regular file")
            brief = PresentationBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8"),
            )
            return request, brief
        except PresentationJobInterruptedError:
            raise
        except Exception as exc:
            # Pydantic, JSON and OS exception messages can include source
            # contents or private paths. Persist only a diagnostic identity.
            digest = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()
            raise _VisualSourceContextError(
                "semantic visual review source context could not be loaded "
                f"(type={type(exc).__name__}; sha256={digest})",
            ) from None

    def _visual_shared_dependencies(
        self,
        deck: DeckIR,
        *,
        job_id: str | None,
        renderer_id: str | None,
        renderer_version: str | None,
    ) -> dict[str, object]:
        """Return dependencies whose change invalidates every visual review.

        Page pixels remain the narrow dependency for local edits. Shared
        rendering, theme, policy, and audience changes alter the meaning of
        every page even when a test renderer happens to emit identical bytes.
        """

        request_context: dict[str, object] | None = None
        if job_id is not None:
            request = self.store.get(job_id).request
            request_context = {
                "audience": request.audience,
                "user_request": request.user_request,
                "language": request.language,
                "mode": request.mode.value,
                "theme_id": request.theme_id,
                "template_mode": (
                    request.template_mode.value
                    if request.template_mode is not None
                    else None
                ),
                "model_config_fingerprint": request.model_config_fingerprint,
                "visual_verification_required": (
                    request.visual_verification_required
                ),
                "vision_critique_required": request.vision_critique_required,
            }
        theme = get_theme(deck.theme_id)
        return {
            "ruleset": VISUAL_REVIEW_RULESET_VERSION,
            "renderer": {
                "id": renderer_id,
                "version": renderer_version,
            },
            "theme": asdict(theme),
            "deck_policy": {
                "language": deck.language,
                "aspect_ratio": deck.aspect_ratio,
                **({"source_fact_corrections": [
                    record.model_dump(mode="json") for record in deck.source_fact_corrections
                ]} if deck.source_fact_corrections else {}),
                "design_profile": (
                    deck.design_profile.model_dump(mode="json")
                    if deck.design_profile is not None
                    else None
                ),
                "design_context": (
                    deck.design_context.model_dump(mode="json")
                    if deck.design_context is not None
                    else None
                ),
            },
            "request": request_context,
        }

    def _read_visual_checkpoint(
        self,
        job_id: str | None,
        name: str,
        *,
        fingerprint: str,
        critic_identity: str,
    ) -> VisualReviewCheckpoint | None:
        if job_id is None:
            return None
        path = self.store.quality_checkpoint_path(job_id, name)
        if not path.is_file():
            return None
        try:
            checkpoint = VisualReviewCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8"),
            )
        except (OSError, ValueError):
            return None
        if (
            checkpoint.dependency_fingerprint != fingerprint
            or checkpoint.critic_cache_identity != critic_identity
        ):
            return None
        return checkpoint

    def _write_visual_checkpoint(
        self,
        job_id: str | None,
        name: str,
        checkpoint: VisualReviewCheckpoint,
    ) -> None:
        if job_id is None:
            return
        self.store.write_quality_checkpoint(
            job_id,
            name,
            checkpoint.model_dump_json(indent=2),
        )

    def _review_visual_scope(
        self,
        *,
        job_id: str | None,
        repair_rounds: int,
        scope: str,
        checkpoint_name: str,
        fingerprint: str,
        critic_identity: str,
        review,
        slide_id: str | None = None,
        rendered_sha256: str | None = None,
    ) -> tuple[VisualCriticResult, VisualReviewEvidence]:
        scope_started_clock = time.perf_counter()
        cache_hit = False
        total_cost_units = 0
        result: VisualCriticResult | None = None
        if job_id is not None:
            self.store.assert_execution_budget(job_id)
        checkpoint = self._read_visual_checkpoint(
            job_id,
            checkpoint_name,
            fingerprint=fingerprint,
            critic_identity=critic_identity,
        )
        cache_hit = checkpoint is not None
        if checkpoint is not None:
            result = checkpoint.result
            started_at = datetime.now(timezone.utc)
            started_clock = time.perf_counter()
            outcome = (
                "failed"
                if any(issue.severity == "error" for issue in result.issues)
                else "passed"
            )
            self.record_stage_metric(
                job_id,
                stage=JobStage.QUALITY,
                operation="critic",
                repair_round=repair_rounds,
                started_at=started_at,
                started_clock=started_clock,
                outcome=outcome,
                route_id=result.critic_id,
                cost_units=0,
                slide_id=slide_id,
                provider_cost_status=(
                    PresentationProviderCostStatus.NOT_APPLICABLE
                ),
            )
        else:
            result, total_cost_units = self._review_uncached_visual_scope(
                job_id=job_id,
                repair_rounds=repair_rounds,
                scope=scope,
                checkpoint_name=checkpoint_name,
                fingerprint=fingerprint,
                critic_identity=critic_identity,
                review=review,
                slide_id=slide_id,
                rendered_sha256=rendered_sha256,
            )
        if (
            result is None
        ):  # pragma: no cover - protected by the branches above
            raise RuntimeError("visual critic produced no result")
        elapsed_ms = max(
            0.0,
            (time.perf_counter() - scope_started_clock) * 1000,
        )
        return result, VisualReviewEvidence(
            scope=scope,
            critic_id=result.critic_id,
            dependency_fingerprint=fingerprint,
            cache_hit=cache_hit,
            slide_id=slide_id,
            rendered_sha256=rendered_sha256,
            duration_ms=elapsed_ms,
            cost_units=total_cost_units,
        )

    @job_operation
    def _review_visuals_with_checkpoints(
        self,
        slides: list[VisualCriticSlideInput],
        *,
        job_id: str | None,
        repair_rounds: int,
        shared_dependencies: dict[str, object],
    ) -> tuple[VisualCriticResult, list[VisualReviewEvidence]] | None:
        if self.visual_critic is None:
            return None
        review_slide: Callable[..., VisualCriticResult] = getattr(
            self.visual_critic,
            "review_slide",
            None,
        )
        review_deck: Callable[..., VisualCriticResult] = getattr(
            self.visual_critic,
            "review_deck",
            None,
        )
        review_slide_cancellable: Callable[..., VisualCriticResult] = getattr(
            self.visual_critic,
            "review_slide_cancellable",
            None,
        )
        review_deck_cancellable: Callable[..., VisualCriticResult] = getattr(
            self.visual_critic,
            "review_deck_cancellable",
            None,
        )
        slide_critic_identity = self._visual_cache_identity("slide")
        deck_critic_identity = self._visual_cache_identity("deck")
        if not (
            callable(review_slide)
            and callable(review_deck)
            and slide_critic_identity is not None
            and deck_critic_identity is not None
        ):
            return None

        slide_fingerprints = {
            slide.slide_id: self._visual_dependency_fingerprint(
                {
                    "scope": "slide",
                    "critic": slide_critic_identity,
                    "shared": shared_dependencies,
                    "input": slide.model_dump(
                        mode="json",
                        exclude={"rendered_path", "usage_session_id"},
                    ),
                },
            )
            for slide in slides
        }

        def review_one(slide: VisualCriticSlideInput):
            safe_id = hashlib.sha256(
                slide.slide_id.encode("utf-8"),
            ).hexdigest()
            ensure_active = (
                (lambda: self.store.assert_execution_active(job_id))
                if job_id is not None
                else None
            )

            def execute_review(retry_directive: str | None = None):
                if ensure_active is not None and callable(
                    review_slide_cancellable,
                ):
                    if retry_directive is None:
                        return self._invoke_visual_review(
                            review_slide_cancellable,
                            slide,
                            ensure_active,
                        )
                    return self._invoke_visual_review(
                        review_slide_cancellable,
                        slide,
                        ensure_active,
                        retry_directive=retry_directive,
                    )
                if retry_directive is None:
                    return self._invoke_visual_review(review_slide, slide)
                return self._invoke_visual_review(
                    review_slide,
                    slide,
                    retry_directive=retry_directive,
                )

            return self._review_visual_scope(
                job_id=job_id,
                repair_rounds=repair_rounds,
                scope="slide",
                checkpoint_name=f"slide-{safe_id}.json",
                fingerprint=slide_fingerprints[slide.slide_id],
                critic_identity=slide_critic_identity,
                review=execute_review,
                slide_id=slide.slide_id,
                rendered_sha256=slide.rendered_sha256,
            )

        siblings_stopped = Event()
        parent_control = current_operation()

        def controlled_review(slide):
            if parent_control is None:
                return review_one(slide)

            def check():
                parent_control.ensure_active()
                if siblings_stopped.is_set():
                    raise PresentationJobInterruptedError(
                        "parallel critic group was retired",
                    )

            with bind_operation(replace(parent_control, ensure_active=check)):
                return review_one(slide)

        executor = ThreadPoolExecutor(max_workers=min(3, len(slides)))
        futures = []
        try:
            futures = [
                executor.submit(copy_context().run, controlled_review, slide)
                for slide in slides
            ]
            completed = {
                future: future.result() for future in as_completed(futures)
            }
            page_results = [completed[future] for future in futures]
        except BaseException:
            siblings_stopped.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            # Keep the writer until every running critic's actual cleanup has
            # returned, even when another critic fails or Stop repeats.
            executor.shutdown(wait=True, cancel_futures=True)

        deck_fingerprint = self._visual_dependency_fingerprint(
            {
                "scope": "deck",
                "critic": deck_critic_identity,
                "shared": shared_dependencies,
                "ordered_slides": [
                    {
                        "slide_id": slide.slide_id,
                        "fingerprint": slide_fingerprints[slide.slide_id],
                    }
                    for slide in slides
                ],
            },
        )
        ensure_deck_active = (
            (lambda: self.store.assert_execution_active(job_id))
            if job_id is not None
            else None
        )

        def execute_deck_review(retry_directive: str | None = None):
            if ensure_deck_active is not None and callable(
                review_deck_cancellable,
            ):
                if retry_directive is None:
                    return self._invoke_visual_review(
                        review_deck_cancellable,
                        slides,
                        ensure_deck_active,
                    )
                return self._invoke_visual_review(
                    review_deck_cancellable,
                    slides,
                    ensure_deck_active,
                    retry_directive=retry_directive,
                )
            if retry_directive is None:
                return self._invoke_visual_review(review_deck, slides)
            return self._invoke_visual_review(
                review_deck,
                slides,
                retry_directive=retry_directive,
            )

        deck_result = self._review_visual_scope(
            job_id=job_id,
            repair_rounds=repair_rounds,
            scope="deck",
            checkpoint_name="deck.json",
            fingerprint=deck_fingerprint,
            critic_identity=deck_critic_identity,
            review=execute_deck_review,
        )
        results = [*(item[0] for item in page_results), deck_result[0]]
        evidence = [*(item[1] for item in page_results), deck_result[1]]
        critic_ids = list(dict.fromkeys(item.critic_id for item in results))
        return (
            VisualCriticResult(
                critic_id="+".join(critic_ids),
                issues=[issue for item in results for issue in item.issues],
            ),
            evidence,
        )

    def _supports_scoped_visual_review(self) -> bool:
        if self.visual_critic is None:
            return False
        return bool(
            callable(getattr(self.visual_critic, "review_slide", None))
            and callable(getattr(self.visual_critic, "review_deck", None))
            and self._visual_cache_identity("slide") is not None
            and self._visual_cache_identity("deck") is not None,
        )

    def create_job(
        self,
        request: PresentationJobRequest,
        *,
        skill_identity: SkillExecutionIdentity | None = None,
        runtime_run_id: str | None = None,
    ):
        bound_request = self.pipeline_registry.bind_new_request(request)
        record = self.store.create(
            bound_request,
            skill_identity=skill_identity,
            runtime_run_id=runtime_run_id,
        )
        if not record.events:
            self.store.append_event(
                record.job_id,
                stage=JobStage.CREATED,
                status=JobStatus.PENDING,
                message="演示文稿任务已建立",
            )
        return self.store.get(record.job_id)

    @staticmethod
    def _invoke_visual_review(
        callback: Callable[..., VisualCriticResult],
        *args,
        **kwargs,
    ) -> VisualCriticResult:
        return callback(*args, **kwargs)

    def _review_uncached_visual_scope(
        self,
        *,
        job_id: str | None,
        repair_rounds: int,
        scope: str,
        checkpoint_name: str,
        fingerprint: str,
        critic_identity: str,
        review: Callable[[str | None], VisualCriticResult],
        slide_id: str | None,
        rendered_sha256: str | None,
    ) -> tuple[VisualCriticResult | None, int]:
        result: VisualCriticResult | None = None
        total_cost_units = 0
        retry_directive: str | None = None
        for attempt in range(1, VISUAL_SCHEMA_ATTEMPT_LIMIT + 1):
            started_at = datetime.now(timezone.utc)
            started_clock = time.perf_counter()
            attempt_cost_units = 0
            outcome = "failed"
            attempt_result: VisualCriticResult | None = None
            try:
                ensure_active = (
                    (
                        lambda: self.store.assert_execution_active(
                            job_id,
                        )
                    )
                    if job_id is not None
                    else None
                )
                with self.limits.critic(ensure_active=ensure_active):
                    if job_id is not None:
                        self.store.assert_execution_active(job_id)
                        self.store.consume_cost(job_id, 1)
                    attempt_cost_units = 1
                    total_cost_units += 1
                    attempt_result = review(retry_directive)
                    if job_id is not None:
                        self.store.assert_execution_active(job_id)
                if not isinstance(attempt_result, VisualCriticResult):
                    raise TypeError(
                        "visual critic returned an invalid result",
                    )
                result = attempt_result
                self._write_visual_checkpoint(
                    job_id,
                    checkpoint_name,
                    VisualReviewCheckpoint(
                        scope=scope,
                        dependency_fingerprint=fingerprint,
                        critic_cache_identity=critic_identity,
                        result=result,
                        rendered_sha256=rendered_sha256,
                    ),
                )
                outcome = (
                    "failed"
                    if any(
                        issue.severity == "error" for issue in result.issues
                    )
                    else "passed"
                )
            except PresentationJobInterruptedError:
                outcome = "cancelled"
                raise
            except StructuredTaskValidationError as exc:
                safe_issues = _safe_visual_validation_issues(exc)
                if attempt >= VISUAL_SCHEMA_ATTEMPT_LIMIT:
                    raise _safe_visual_schema_error(exc) from None
                issue_suffix = ",".join(safe_issues)
                retry_directive = (
                    "上次审阅结果未通过 schema；只使用 JSON Schema "
                    "允许的枚举与字段，从原始渲染证据重新审阅。"
                    "slide_id 必须来自输入页；object_ids 只能引用该页"
                    "输入的精确 ID，无法定位对象时留空，不得补造引用。"
                )
                if issue_suffix:
                    retry_directive += f"需修正：{issue_suffix}。"
                retry_directive += "不要复述上次输出，不要补造事实。"
            finally:
                self.record_stage_metric(
                    job_id,
                    stage=JobStage.QUALITY,
                    operation="critic",
                    repair_round=repair_rounds,
                    started_at=started_at,
                    started_clock=started_clock,
                    outcome=outcome,
                    route_id=(
                        attempt_result.critic_id
                        if attempt_result is not None
                        else critic_identity
                    ),
                    cost_units=attempt_cost_units,
                    slide_id=slide_id,
                    provider_cost_status=(
                        PresentationProviderCostStatus.UNKNOWN
                        if attempt_cost_units > 0
                        else PresentationProviderCostStatus.NOT_APPLICABLE
                    ),
                )
            if result is not None:
                break

        return result, total_cost_units

    def _review_unscoped_visuals(
        self,
        job_id: str | None,
        critic_inputs: list[VisualCriticSlideInput],
        critic_cost_units: int,
    ) -> VisualCriticResult:
        if job_id is not None:
            self.store.consume_cost(job_id, critic_cost_units)
            self.store.assert_execution_budget(job_id)
        critic_result = self.visual_critic.review(critic_inputs)
        if job_id is not None:
            self.store.assert_execution_budget(job_id)

        return critic_result

    def _record_cancelled_visual_metric(
        self,
        job_id: str | None,
        repair_rounds: int,
        critic_started_at: datetime,
        critic_started_clock: float,
        critic_outcome: str,
        visual_critic_id: str | None,
        critic_cost_units: int,
    ) -> bool:
        self.record_stage_metric(
            job_id,
            stage=JobStage.QUALITY,
            operation="critic",
            repair_round=repair_rounds,
            started_at=critic_started_at,
            started_clock=critic_started_clock,
            outcome=critic_outcome,
            route_id=visual_critic_id,
            cost_units=critic_cost_units,
            provider_cost_status=(
                PresentationProviderCostStatus.UNKNOWN
                if critic_cost_units > 0
                else PresentationProviderCostStatus.NOT_APPLICABLE
            ),
        )
        scoped_metrics_recorded = True

        return scoped_metrics_recorded

    @staticmethod
    def _visual_result_issues(
        critic_result: VisualCriticResult,
        visual_quality_issues: list[QualityIssue],
    ) -> tuple[str, list[VisualIssue], bool, str]:
        visual_critic_id = critic_result.critic_id
        # The critic owns semantic classification; source fidelity is never
        # advisory. Normalize copies so neither fresh nor cached findings can
        # certify unsupported content merely by reporting a lower severity.
        visual_issues = [
            issue.model_copy(update={"severity": "error"})
            if issue.issue_type in {"content_accuracy", "source_coverage"}
            else issue
            for issue in critic_result.issues
        ]
        visual_quality_issues.extend(
            QualityIssue(
                code=f"vision.{issue.issue_type}",
                severity=issue.severity,
                slide_id=issue.slide_id,
                object_id=(issue.object_ids[0] if issue.object_ids else None),
                message=issue.description,
                repairable=issue.severity == "error",
            )
            for issue in visual_issues
        )
        visual_verified = not any(
            issue.severity == "error" for issue in visual_issues
        )
        critic_outcome = "passed" if visual_verified else "failed"

        return visual_critic_id, visual_issues, visual_verified, critic_outcome

    def _required_visual_review(
        self,
        deck: DeckIR,
        rendered_images: tuple[Path, ...],
        plan: DeckPlan | None,
        job_id: str | None,
        repair_rounds: int,
        renderer_id: str | None,
        renderer_version: str | None,
        vision_critique_required: bool,
    ) -> tuple[
        list[VisualIssue],
        list[QualityIssue],
        str | None,
        list[VisualReviewEvidence],
        bool,
    ]:
        visual_issues: list[VisualIssue] = []
        visual_quality_issues: list[QualityIssue] = []
        visual_critic_id: str | None = None
        visual_review_evidence: list[VisualReviewEvidence] = []
        visual_verified = False
        if vision_critique_required:
            critic_started_at = datetime.now(timezone.utc)
            critic_started_clock = time.perf_counter()
            critic_outcome = "failed"
            scoped_metrics_recorded = False
            critic_cost_units = 0
            if len(rendered_images) != len(deck.slides):
                visual_quality_issues.append(
                    QualityIssue(
                        code="vision.render_set_incomplete",
                        severity="error",
                        message=(
                            "semantic visual review requires every rendered"
                            " slide"
                        ),
                        repairable=True,
                    ),
                )
            elif self.visual_critic is None:
                critic_outcome = "unavailable"
                visual_quality_issues.append(
                    QualityIssue(
                        code="vision.critic_unavailable",
                        severity="error",
                        message=(
                            "a semantic visual critic is required but"
                            " unavailable"
                        ),
                    ),
                )
            else:
                # Keep a durable adapter identity even when Stop or a model
                # failure arrives before the critic can return its route.
                # A successful result below replaces this with the Provider
                # route reported by the critic itself.
                visual_critic_id = (
                    self._visual_cache_identity()
                    or type(self.visual_critic).__name__
                )
                try:
                    review_request, review_brief = self._visual_source_context(job_id)
                    critic_inputs = build_visual_critic_inputs(
                        deck,
                        rendered_images,
                        plan=plan,
                        brief=review_brief,
                        original_user_request=(
                            review_request.user_request if review_request is not None else ""
                        ),
                        usage_session_id=(
                            review_request.conversation_id
                            if review_request is not None else None
                        ),
                    )
                    scoped_metrics_recorded = (
                        self._supports_scoped_visual_review()
                    )
                    scoped_review = self._review_visuals_with_checkpoints(
                        critic_inputs,
                        job_id=job_id,
                        repair_rounds=repair_rounds,
                        shared_dependencies=self._visual_shared_dependencies(
                            deck,
                            job_id=job_id,
                            renderer_id=renderer_id,
                            renderer_version=renderer_version,
                        ),
                    )
                    if scoped_review is None:
                        scoped_metrics_recorded = False
                        critic_cost_units = 3
                        critic_result = self._review_unscoped_visuals(
                            job_id,
                            critic_inputs,
                            critic_cost_units,
                        )
                    else:
                        critic_result, visual_review_evidence = scoped_review
                        scoped_metrics_recorded = True
                    (
                        visual_critic_id,
                        visual_issues,
                        visual_verified,
                        critic_outcome,
                    ) = self._visual_result_issues(
                        critic_result,
                        visual_quality_issues,
                    )
                except PresentationJobInterruptedError:
                    critic_outcome = "cancelled"
                    if not scoped_metrics_recorded:
                        scoped_metrics_recorded = (
                            self._record_cancelled_visual_metric(
                                job_id,
                                repair_rounds,
                                critic_started_at,
                                critic_started_clock,
                                critic_outcome,
                                visual_critic_id,
                                critic_cost_units,
                            )
                        )
                    raise
                except Exception as exc:
                    critic_outcome = "unavailable"
                    issue_code = (
                        "vision.source_context_unavailable"
                        if isinstance(exc, _VisualSourceContextError)
                        else "vision.critic_schema_invalid"
                        if isinstance(exc, StructuredTaskValidationError)
                        else "vision.critic_failed"
                    )
                    visual_quality_issues.append(
                        QualityIssue(
                            code=issue_code,
                            severity="error",
                            message=str(
                                _safe_visual_schema_error(exc)
                                if isinstance(exc, StructuredTaskValidationError)
                                else exc
                            ),
                        ),
                    )
            if not scoped_metrics_recorded:
                self.record_stage_metric(
                    job_id,
                    stage=JobStage.QUALITY,
                    operation="critic",
                    repair_round=repair_rounds,
                    started_at=critic_started_at,
                    started_clock=critic_started_clock,
                    outcome=critic_outcome,
                    route_id=visual_critic_id,
                    cost_units=critic_cost_units,
                    provider_cost_status=(
                        PresentationProviderCostStatus.UNKNOWN
                        if critic_cost_units > 0
                        else PresentationProviderCostStatus.NOT_APPLICABLE
                    ),
                )
            if job_id is not None:
                self.store.assert_execution_budget(job_id)

        return (
            visual_issues,
            visual_quality_issues,
            visual_critic_id,
            visual_review_evidence,
            visual_verified,
        )

    def _render_artifact(
        self,
        output: Path,
        render_dir: str | Path,
        job_id: str | None,
        repair_rounds: int,
        visual_required: bool,
    ) -> tuple[
        tuple[Path, ...],
        Path | None,
        int | None,
        str | None,
        str | None,
        list[QualityIssue],
    ]:
        rendered_count: int | None = None
        rendered_images: tuple[Path, ...] = ()
        rendered_pdf: Path | None = None
        renderer_id: str | None = None
        renderer_version: str | None = None
        render_issues: list[QualityIssue] = []
        render_started_at = datetime.now(timezone.utc)
        render_started_clock = time.perf_counter()
        render_outcome = "passed"
        try:
            with self.limits.rendering():
                rendered = render_pptx(
                    output,
                    render_dir,
                    timeout=remaining_timeout(120),
                )
            rendered_images = rendered.slide_images
            rendered_pdf = rendered.pdf_path
            rendered_count = len(rendered_images)
            renderer_id = rendered.renderer_id
            renderer_version = rendered.renderer_version
        except (PresentationJobInterruptedError, TimeoutError):
            raise
        except PresentationRenderUnavailable as exc:
            render_outcome = "unavailable"
            render_issues.append(
                QualityIssue(
                    code="render.unavailable",
                    severity="error" if visual_required else "warning",
                    message=str(exc),
                ),
            )
        except Exception as exc:
            render_outcome = "failed"
            rendered_count = 0
            render_issues.append(
                QualityIssue(
                    code="render.failed",
                    severity="error",
                    message=str(exc),
                    repairable=True,
                ),
            )

        self.record_stage_metric(
            job_id,
            stage=JobStage.RENDER,
            operation="render",
            repair_round=repair_rounds,
            started_at=render_started_at,
            started_clock=render_started_clock,
            outcome=render_outcome,
            route_id=renderer_id,
        )
        if job_id is not None:
            self.store.assert_execution_budget(job_id)

        return (
            rendered_images,
            rendered_pdf,
            rendered_count,
            renderer_id,
            renderer_version,
            render_issues,
        )

    def _inspect_artifact(
        self,
        deck: DeckIR,
        output: Path,
        rendered_images: tuple[Path, ...],
        rendered_pdf: Path | None,
        rendered_count: int | None,
        renderer_id: str | None,
        renderer_version: str | None,
        render_dir: str | Path,
        render_set_id: str | None,
        vision_critique_required: bool,
        job_id: str | None,
        repair_rounds: int,
    ) -> QualityReport:
        inspect_started_at = datetime.now(timezone.utc)
        inspect_started_clock = time.perf_counter()
        report = inspect_pptx(
            deck,
            output,
            rendered_slide_count=rendered_count,
            repair_rounds=repair_rounds,
        )
        with self.limits.rendering():
            glyph_issues = inspect_rendered_glyph_dividers(deck, output, rendered_pdf)
        if glyph_issues:
            report = report.model_copy(update={
                "issues": [*report.issues, *glyph_issues],
                "passed": False,
                "delivery_status": "failed",
            })
        self.record_stage_metric(
            job_id,
            stage=JobStage.QUALITY,
            operation="inspect",
            repair_round=repair_rounds,
            started_at=inspect_started_at,
            started_clock=inspect_started_clock,
            outcome="passed" if report.passed else "failed",
            route_id="lantai-structural-inspector",
        )
        rendered_slides: list[RenderedSlideEvidence] = []
        check_operation()
        for index, (slide, image_path) in enumerate(
            zip(deck.slides, rendered_images, strict=False),
            start=1,
        ):
            check_operation()
            try:
                with Image.open(image_path) as rendered_image:
                    width, height = rendered_image.size
                digest = file_sha256(image_path)
            except (OSError, ValueError):
                continue
            rendered_slides.append(
                RenderedSlideEvidence(
                    slide_id=slide.id,
                    slide_index=index,
                    sha256=digest,
                    width=width,
                    height=height,
                ),
            )
        theme = get_theme(deck.theme_id)
        report = report.model_copy(
            update={
                "renderer_id": renderer_id,
                "renderer_version": renderer_version,
                "font_manifest": sorted(
                    {theme.title_font, theme.body_font},
                ),
                "rendered_slides": rendered_slides,
                "render_set_id": render_set_id or Path(render_dir).name,
                "rendered_glyph_check_version": GLYPH_DIVIDER_RULESET,
                "vision_critique_required": vision_critique_required,
            },
        )

        return report

    @staticmethod
    def _finalize_artifact_report(
        deck: DeckIR,
        report: QualityReport,
        rendered_images: tuple[Path, ...],
        render_issues: list[QualityIssue],
        evidence_issues: list[QualityIssue],
        visual_quality_issues: list[QualityIssue],
        vision_critique_required: bool,
        visual_verified: bool,
        visual_critic_id: str | None,
        visual_issues: list[VisualIssue],
        visual_review_evidence: list[VisualReviewEvidence],
    ) -> QualityReport:
        combined = [
            *report.issues,
            *render_issues,
            *evidence_issues,
            *visual_quality_issues,
        ]
        passed = not any(issue.severity == "error" for issue in combined)
        error_codes = {
            issue.code for issue in combined if issue.severity == "error"
        }
        render_unavailable = "render.unavailable" in error_codes
        render_failed = any(
            code.startswith(("render.", "evidence.render", "evidence.slide"))
            or code in {"layout.glyph_divider_collision", "evidence.glyph_geometry_missing"}
            for code in error_codes
        )
        render_evidence = (
            EvidenceState.UNAVAILABLE
            if render_unavailable
            else EvidenceState.FAILED
            if render_failed
            else EvidenceState.PASSED
            if len(rendered_images) == len(deck.slides)
            else EvidenceState.NOT_RUN
        )
        if not vision_critique_required:
            visual_evidence = EvidenceState.NOT_RUN
        elif any(
            code
            in {
                "vision.critic_unavailable",
                "vision.critic_failed",
                "vision.critic_schema_invalid",
                "vision.source_context_unavailable",
            }
            for code in error_codes
        ):
            visual_evidence = EvidenceState.UNAVAILABLE
        elif any(code.startswith("vision.") for code in error_codes):
            visual_evidence = EvidenceState.FAILED
        elif visual_verified:
            visual_evidence = EvidenceState.PASSED
        else:
            visual_evidence = EvidenceState.NOT_RUN
        blocking_gaps = list(report.blocking_gaps)
        for label, state in (
            ("render evidence", render_evidence),
            ("visual evidence", visual_evidence),
        ):
            if (
                state
                in {
                    EvidenceState.FAILED,
                    EvidenceState.UNAVAILABLE,
                    EvidenceState.STALE,
                }
                and label not in blocking_gaps
            ):
                blocking_gaps.append(label)
        if (
            vision_critique_required
            and visual_evidence != EvidenceState.PASSED
        ):
            if "visual evidence" not in blocking_gaps:
                blocking_gaps.append("visual evidence")
        final_passed = passed and not blocking_gaps
        delivery_status = delivery_status_for(
            combined,
            content_complete=report.content_complete,
            editable=report.editable,
        )
        if not final_passed:
            delivery_status = "failed"
        report = report.model_copy(
            update={
                "issues": combined,
                "passed": final_passed,
                "visual_verified": visual_verified,
                "visual_critic_id": visual_critic_id,
                "visual_issues": visual_issues,
                "visual_review_evidence": visual_review_evidence,
                "render_evidence": render_evidence,
                "visual_evidence": visual_evidence,
                "blocking_gaps": blocking_gaps,
                "delivery_status": delivery_status,
            },
        )

        return report

    def _charged_redesign(
        self,
        designer,
        redesign_request,
        *,
        job_id: str,
        repair_round: int,
    ):
        self.store.assert_execution_budget(job_id)
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        outcome = "failed"
        if hasattr(designer, "last_model_label"):
            designer.last_model_label = None
        # Charge the parent job immediately before every
        # actual model dispatch.  One repair round can contain
        # several failing slides and therefore several calls.
        self.store.consume_cost(job_id, 1)
        try:
            proposal = designer.redesign(redesign_request)
            outcome = "passed"
            return proposal
        finally:
            self.record_stage_metric(
                job_id,
                stage=JobStage.REPAIR,
                operation="repair",
                repair_round=repair_round,
                started_at=started_at,
                started_clock=started_clock,
                outcome=outcome,
                route_id=(
                    getattr(
                        designer,
                        "last_model_label",
                        None,
                    )
                    or type(designer).__name__
                ),
                cost_units=1,
                slide_id=redesign_request.slide.id,
                provider_cost_status=(PresentationProviderCostStatus.UNKNOWN),
            )

    def _checkpoint_accepted_slide(
        self,
        progress_deck: DeckIR,
        progress_plan: DeckPlan | None,
        slide_id: str,
        *,
        job_id: str,
        repair_round: int,
    ) -> None:
        prepared_progress = prepare_deck_for_composition(
            progress_deck,
        )
        progress_path = self.store.write_checkpoint(
            job_id,
            f"deck-ir-redesign-{repair_round + 1}-{slide_id}.json",
            prepared_progress.model_dump_json(indent=2),
        )
        self.store.write_checkpoint(
            job_id,
            "deck-ir.json",
            prepared_progress.model_dump_json(indent=2),
        )
        if progress_plan is not None:
            self.store.write_checkpoint(
                job_id,
                f"deck-plan-redesign-{repair_round + 1}-{slide_id}.json",
                progress_plan.model_dump_json(indent=2),
            )
            self.store.write_checkpoint(
                job_id,
                "deck-plan.json",
                progress_plan.model_dump_json(indent=2),
            )
        self.store.append_event(
            job_id,
            stage=JobStage.REPAIR,
            status=JobStatus.RUNNING,
            slide_id=slide_id,
            message=f"已提交页级修复检查点 {slide_id}",
            checkpoint=str(progress_path),
            recoverable=True,
        )

    def _repair_native_candidate(
        self,
        job_id: str,
        request: PresentationJobRequest,
        working_deck: DeckIR,
        plan: DeckPlan | None,
        report: QualityReport,
        repair_round: int,
        round_path: Path,
    ) -> tuple[bool, DeckIR, DeckPlan | None]:
        if self.repair_designer is not None:
            self.store.assert_execution_budget(job_id)

            charged_redesign = partial(
                self._charged_redesign,
                job_id=job_id,
                repair_round=repair_round,
            )

            try:
                checkpoint_accepted_slide = partial(
                    self._checkpoint_accepted_slide,
                    job_id=job_id,
                    repair_round=repair_round,
                )

                repair = repair_deck_with_designer(
                    working_deck,
                    plan,
                    report.issues,
                    self.repair_designer,
                    usage_session_id=request.conversation_id,
                    allow_structural_repair=True,
                    slide_count_ceiling=request.slide_count,
                    redesign_executor=charged_redesign,
                    accepted_slide_callback=(checkpoint_accepted_slide),
                    proposal_attempt_limit=3,
                )
            except (TypeError, ValueError) as exc:
                self.store.append_event(
                    job_id,
                    stage=JobStage.REPAIR,
                    status=JobStatus.RUNNING,
                    message=f"自动修复提案被拒绝：{exc}",
                    checkpoint=str(round_path),
                    recoverable=True,
                )
                return False, working_deck, plan
            if repair.changed:
                if repair.plan != plan and repair.plan is not None:
                    plan = repair.plan
                    self.store.write_checkpoint(
                        job_id,
                        f"deck-plan-redesign-{repair_round + 1}.json",
                        plan.model_dump_json(indent=2),
                    )
                working_deck = prepare_deck_for_composition(
                    repair.deck,
                )
                repaired_path = self.store.write_checkpoint(
                    job_id,
                    f"deck-ir-redesign-{repair_round + 1}.json",
                    working_deck.model_dump_json(indent=2),
                )
                self.store.append_event(
                    job_id,
                    stage=JobStage.REPAIR,
                    status=JobStatus.RUNNING,
                    message="；".join(repair.actions),
                    checkpoint=str(repaired_path),
                    recoverable=True,
                )
                return True, working_deck, plan
        self.store.append_event(
            job_id,
            stage=JobStage.REPAIR,
            status=JobStatus.RUNNING,
            message="Presentation v3 检出需定向修复的问题；禁止用旧版 fallback layout 冒充重做",
            checkpoint=str(round_path),
            recoverable=True,
        )
        return False, working_deck, plan

    def _repair_legacy_candidate(
        self,
        job_id: str,
        working_deck: DeckIR,
        repair_round: int,
    ) -> DeckIR | None:
        self.store.assert_execution_budget(job_id)
        repair_started_at = datetime.now(timezone.utc)
        repair_started_clock = time.perf_counter()
        repair = repair_deck_once(working_deck)
        self.record_stage_metric(
            job_id,
            stage=JobStage.REPAIR,
            operation="repair",
            repair_round=repair_round,
            started_at=repair_started_at,
            started_clock=repair_started_clock,
            outcome="passed",
            route_id="deterministic-repair",
        )
        if not repair.changed:
            return None
        working_deck = repair.deck
        repaired_path = self.store.write_checkpoint(
            job_id,
            f"deck-ir-repair-{repair_round + 1}.json",
            working_deck.model_dump_json(indent=2),
        )
        self.store.append_event(
            job_id,
            stage=JobStage.REPAIR,
            status=JobStatus.RUNNING,
            message="；".join(repair.actions),
            checkpoint=str(repaired_path),
            recoverable=True,
        )

        return working_deck

    def _compose_candidate_round(
        self,
        job_id: str,
        request: PresentationJobRequest,
        working_deck: DeckIR,
        plan: DeckPlan | None,
        compiler,
        repair_round: int,
    ) -> tuple[CompilationResult, QualityReport, Path, Path]:
        self.store.assert_execution_budget(job_id)
        # Reserve deterministic composition/render/inspection work here.
        # Semantic critic calls are charged individually on cache misses so
        # a 2-page and a 19-page deck no longer report the same model cost.
        round_cost_units = 8
        self.store.consume_cost(job_id, round_cost_units)
        budget_started_at = datetime.now(timezone.utc)
        self.record_stage_metric(
            job_id,
            stage=JobStage.COMPOSE,
            operation="budget_reservation",
            repair_round=repair_round,
            started_at=budget_started_at,
            started_clock=time.perf_counter(),
            outcome="passed",
            route_id=request.composition_engine.value,
            cost_units=round_cost_units,
        )
        candidate_output = (
            self.store.job_dir(job_id)
            / "candidates"
            / f"round-{repair_round}"
            / "presentation.pptx"
        )
        with self.limits.synthesis():
            self.store.append_event(
                job_id,
                stage=JobStage.COMPOSE,
                status=JobStatus.RUNNING,
                message=f"正在用原生对象合成 PPTX（第 {repair_round + 1} 次）",
            )
            compile_started_at = datetime.now(timezone.utc)
            compile_started_clock = time.perf_counter()
            compile_outcome = "passed"
            try:
                result = compiler.compile(working_deck, candidate_output)
            except Exception:
                compile_outcome = "failed"
                raise
            finally:
                self.record_stage_metric(
                    job_id,
                    stage=JobStage.COMPOSE,
                    operation="compile",
                    repair_round=repair_round,
                    started_at=compile_started_at,
                    started_clock=compile_started_clock,
                    outcome=compile_outcome,
                    route_id=request.composition_engine.value,
                )

        self.store.assert_execution_budget(job_id)
        self.store.append_event(
            job_id,
            stage=JobStage.RENDER,
            status=JobStatus.RUNNING,
            message="正在隔离渲染并逐页检查",
            checkpoint=str(candidate_output),
        )
        self.store.append_event(
            job_id,
            stage=JobStage.QUALITY,
            status=JobStatus.RUNNING,
            message="正在执行结构、内容、可编辑性与视觉质量检查",
            checkpoint=str(candidate_output),
        )
        render_dir = (
            self.store.job_dir(job_id) / "renders" / f"round-{repair_round}"
        )
        assert candidate_output is not None
        report, rendered_images = self.verify_artifact(
            working_deck,
            candidate_output,
            render_dir,
            repair_rounds=repair_round,
            visual_required=request.visual_verification_required,
            vision_critique_required=request.vision_critique_required,
            plan=plan,
            render_set_id=f"round-{repair_round}",
            job_id=job_id,
        )
        self.store.assert_execution_budget(job_id)
        for slide, image in zip(
            working_deck.slides,
            rendered_images,
            strict=False,
        ):
            self.store.append_event(
                job_id,
                stage=JobStage.RENDER,
                status=JobStatus.RUNNING,
                slide_id=slide.id,
                message=f"已渲染 {slide.id}",
                checkpoint=str(image),
            )
        round_path = (
            self.store.job_dir(job_id)
            / "quality"
            / f"quality-report-round-{repair_round}.json"
        )
        with self.store.publication_guard(job_id):
            self.store._atomic_text(  # pylint: disable=protected-access
                round_path,
                report.model_dump_json(indent=2),
            )

        return result, report, candidate_output, round_path

    def _publish_composed_candidate(
        self,
        job_id: str,
        request: PresentationJobRequest,
        working_deck: DeckIR,
        output: Path,
        candidate_output: Path,
        report: QualityReport,
        result: CompilationResult,
        version_store: PresentationVersionStore,
        base_version: PresentationVersion | None,
        writer_lease: PresentationWriterLease,
        quality_path: Path,
    ) -> CompilationResult:
        self.store.assert_execution_budget(job_id)
        promotion_started_at = datetime.now(timezone.utc)
        promotion_started_clock = time.perf_counter()
        promotion_outcome = "passed"

        def publish_verified_candidate() -> None:
            previous_snapshot = self._promote_candidate(
                candidate_output,
                output,
            )
            try:
                if file_sha256(output) != report.output_sha256:
                    raise RuntimeError(
                        "promoted PPTX does not match verified candidate",
                    )
                version_store.record(
                    output,
                    working_deck,
                    writer=request.composition_engine.value,
                    parent=base_version,
                    generation=writer_lease.generation,
                    base_pptx_sha256=writer_lease.base_pptx_sha256,
                    expected_pptx_sha256=report.output_sha256,
                    quality_report_path=quality_path,
                    render_dir=(
                        self.store.job_dir(job_id)
                        / "renders"
                        / (report.render_set_id or "")
                    ),
                )
            except Exception:
                self._rollback_promotion(output, previous_snapshot)
                raise

        message = (
            "演示文稿已生成并通过结构、渲染证据与视觉质量检查"
            if report.vision_critique_required
            else "演示文稿已生成并通过结构与逐页渲染证据检查"
        )
        try:
            self.store.complete_after_verified_publication(
                job_id,
                message=message,
                checkpoint=str(output),
                publish=publish_verified_candidate,
            )
        except Exception:
            promotion_outcome = "failed"
            raise
        finally:
            self.record_stage_metric(
                job_id,
                stage=JobStage.COMPLETED,
                operation="promotion",
                repair_round=report.repair_rounds,
                started_at=promotion_started_at,
                started_clock=promotion_started_clock,
                outcome=promotion_outcome,
                route_id="atomic-promotion",
            )
        result = CompilationResult(
            output_path=output,
            deck_id=result.deck_id,
            slide_count=result.slide_count,
            editable_object_count=result.editable_object_count,
            degradations=result.degradations,
            native_repairs=result.native_repairs,
        )

        return result

    def _compose_job_base(
        self,
        job_id: str,
        deck: DeckIR,
        writer_lease: PresentationWriterLease,
    ) -> tuple:
        self.store.assert_execution_budget(job_id)
        request = self.store.get(job_id).request
        if (
            is_native_pptx_engine(request.composition_engine)
            and request.template_path
            and Path(request.template_path).suffix.casefold() == ".pptx"
        ):
            context = deck.design_context
            mode = request.template_mode or (
                context.profile.template_mode if context is not None else None
            )
            if context is not None and mode != context.profile.template_mode:
                raise TemplateBindingError("template_policy_context_mismatch")
            if mode == TemplateMode.STYLE_REFERENCE:
                # Abstract style transfer is never permission to copy source
                # XML or media, even if an older checkpoint contains a binding.
                deck = bind_authorized_template(
                    deck, template_path=request.template_path,
                    source_sha256="0" * 64, template_mode=mode,
                    required_anchors=request.template_anchor_requirements,
                )
            else:
                artifact_id = context.reference_artifact_id if context else None
                if (
                    context is None or not context.reference_input_present
                    or context.profile.source != "reference"
                    or artifact_id is None or not artifact_id.startswith("sha256:")
                    or len(artifact_id) != 71
                    or any(char not in "0123456789abcdef" for char in artifact_id[7:])
                ):
                    raise TemplateBindingError("template_reference_pin_required")
                previous = deck.template_binding
                if previous is not None and (
                    previous.source_sha256 != artifact_id[7:]
                    or Path(previous.source_path).resolve() != Path(request.template_path).resolve()
                ):
                    raise TemplateBindingError("template_binding_request_mismatch")
                deck = bind_authorized_template(
                    deck, template_path=request.template_path,
                    source_sha256=artifact_id[7:], template_mode=mode,
                    required_anchors=request.template_anchor_requirements,
                    source_slide_numbers=(
                        previous.source_slide_numbers if previous is not None else None
                    ),
                )
        working_deck = (
            prepare_deck_for_composition(deck)
            if is_native_pptx_engine(request.composition_engine)
            else deck
        )
        output = self.store.job_dir(job_id) / "output" / "presentation.pptx"
        version_store = PresentationVersionStore(self.store.job_dir(job_id))
        base_version = version_store.current()
        if base_version is not None:
            base_version = version_store.assert_writer_base(
                output,
                working_deck,
            )
            if writer_lease.base_pptx_sha256 != base_version.pptx_sha256:
                raise RuntimeError(
                    "writer lease is not based on the authoritative PPTX",
                )
        elif (
            output.is_file()
            and self.store.get(job_id).status == JobStatus.COMPLETED
        ):
            raise RuntimeError(
                (
                    "completed PPTX has no version ownership record; reconcile"
                    " or re-import before composing"
                ),
            )
        deck_path = self.store.write_checkpoint(
            job_id,
            "deck-ir.json",
            working_deck.model_dump_json(indent=2),
        )
        self.store.append_event(
            job_id,
            stage=JobStage.DECK_IR,
            status=JobStatus.RUNNING,
            message="DeckIR 数据模型已载入",
            checkpoint=str(deck_path),
        )
        plan_path = self.store.checkpoint_path(job_id, "deck-plan.json")
        plan = (
            DeckPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
            if plan_path.is_file()
            else None
        )
        initial_plan = plan

        return (
            request,
            working_deck,
            output,
            version_store,
            base_version,
            deck_path,
            plan,
            initial_plan,
        )

    def _compose_with_new_writer(
        self,
        job_id: str,
        deck: DeckIR,
        runtime_run_id: str | None,
    ) -> tuple[CompilationResult, QualityReport]:
        output = self.store.job_dir(job_id) / "output" / "presentation.pptx"
        version = PresentationVersionStore(
            self.store.job_dir(job_id),
        ).current()
        base_hash = (
            version.pptx_sha256
            if version is not None
            else file_sha256(output)
            if output.is_file()
            else None
        )
        with self.store.writer_lease(
            job_id,
            base_pptx_sha256=base_hash,
            minimum_generation=version.generation if version else 0,
            runtime_run_id=runtime_run_id,
        ) as acquired:
            return self.compose(job_id, deck, writer_lease=acquired)

    def _checkpoint_passed_candidate(
        self,
        job_id: str,
        plan: DeckPlan | None,
        initial_plan: DeckPlan | None,
        working_deck: DeckIR,
        deck: DeckIR,
        deck_path: Path,
    ) -> Path:
        if plan != initial_plan and plan is not None:
            _ = self.store.write_checkpoint(
                job_id,
                "deck-plan.json",
                plan.model_dump_json(indent=2),
            )
        if working_deck != deck:
            deck_path = self.store.write_checkpoint(
                job_id,
                "deck-ir.json",
                working_deck.model_dump_json(indent=2),
            )

        return deck_path

    @job_operation
    def verify_artifact(
        self,
        deck: DeckIR,
        pptx_path: str | Path,
        render_dir: str | Path,
        *,
        repair_rounds: int = 0,
        visual_required: bool = True,
        vision_critique_required: bool = False,
        plan: DeckPlan | None = None,
        render_set_id: str | None = None,
        job_id: str | None = None,
    ) -> tuple[QualityReport, tuple[Path, ...]]:
        """Render and bind a quality report to one exact PPTX candidate."""

        output = Path(pptx_path)
        (
            rendered_images,
            rendered_pdf,
            rendered_count,
            renderer_id,
            renderer_version,
            render_issues,
        ) = self._render_artifact(
            output,
            render_dir,
            job_id,
            repair_rounds,
            visual_required,
        )

        report = self._inspect_artifact(
            deck,
            output,
            rendered_images,
            rendered_pdf,
            rendered_count,
            renderer_id,
            renderer_version,
            render_dir,
            render_set_id,
            vision_critique_required,
            job_id,
            repair_rounds,
        )
        evidence_issues = delivery_evidence_issues(
            deck,
            report,
            output,
            rendered_images,
        )
        correction_issues = []
        if deck.source_fact_corrections:
            try:
                _, source_brief = self._visual_source_context(job_id)
                validate_source_fact_correction_history(deck, source_brief)
            except (_VisualSourceContextError, ValueError):
                correction_issues.append(QualityIssue(
                    code="content.source_fact_correction_invalid", severity="error",
                    message="Caller source-fact correction provenance could not be independently replayed.",
                    repairable=False,
                ))
        evidence_issues.extend(correction_issues)
        (
            visual_issues,
            visual_quality_issues,
            visual_critic_id,
            visual_review_evidence,
            visual_verified,
        ) = ([], [], None, [], False) if correction_issues else self._required_visual_review(
            deck,
            rendered_images,
            plan,
            job_id,
            repair_rounds,
            renderer_id,
            renderer_version,
            vision_critique_required,
        )
        report = self._finalize_artifact_report(
            deck,
            report,
            rendered_images,
            render_issues,
            evidence_issues,
            visual_quality_issues,
            vision_critique_required,
            visual_verified,
            visual_critic_id,
            visual_issues,
            visual_review_evidence,
        )
        return report, rendered_images

    @job_operation
    def compose(
        self,
        job_id: str,
        deck: DeckIR,
        *,
        writer_lease: PresentationWriterLease | None = None,
        runtime_run_id: str | None = None,
    ) -> tuple[CompilationResult, QualityReport]:
        if writer_lease is None:
            return self._compose_with_new_writer(job_id, deck, runtime_run_id)
        (
            request,
            working_deck,
            output,
            version_store,
            base_version,
            deck_path,
            plan,
            initial_plan,
        ) = self._compose_job_base(job_id, deck, writer_lease)
        compiler = self.compiler_for(request.composition_engine)
        result: CompilationResult | None = None
        report: QualityReport | None = None
        candidate_output: Path | None = None
        for repair_round in range(MAX_REPAIR_ROUNDS + 1):
            (
                result,
                report,
                candidate_output,
                round_path,
            ) = self._compose_candidate_round(
                job_id,
                request,
                working_deck,
                plan,
                compiler,
                repair_round,
            )
            if report.passed:
                deck_path = self._checkpoint_passed_candidate(
                    job_id,
                    plan,
                    initial_plan,
                    working_deck,
                    deck,
                    deck_path,
                )
                break
            if repair_round >= MAX_REPAIR_ROUNDS:
                break
            if not any(issue.repairable for issue in report.issues):
                break
            if (
                request.composition_engine
                != CompositionEngine.LEGACY_PYTHON_PPTX
            ):
                changed, working_deck, plan = self._repair_native_candidate(
                    job_id,
                    request,
                    working_deck,
                    plan,
                    report,
                    repair_round,
                    round_path,
                )
                if not changed:
                    break
                continue
            repaired_deck = self._repair_legacy_candidate(
                job_id,
                working_deck,
                repair_round,
            )
            if repaired_deck is None:
                break
            working_deck = repaired_deck

        assert (
            result is not None
            and report is not None
            and candidate_output is not None
        )
        quality_path = (
            self.store.job_dir(job_id) / "quality" / "quality-report.json"
        )
        with self.store.publication_guard(job_id):
            # Writer-owned publication uses the job store's atomic API.
            self.store._atomic_text(  # pylint: disable=protected-access
                quality_path,
                report.model_dump_json(indent=2),
            )
        current = self.store.get(job_id)
        if current.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
            return result, report
        rendered_images = slide_image_paths(
            self.store.job_dir(job_id)
            / "renders"
            / (report.render_set_id or ""),
        )
        deliverable = is_verified_delivery(
            working_deck,
            report,
            candidate_output,
            rendered_images,
        )
        if deliverable:
            result = self._publish_composed_candidate(
                job_id,
                request,
                working_deck,
                output,
                candidate_output,
                report,
                result,
                version_store,
                base_version,
                writer_lease,
                quality_path,
            )
        else:
            message = (
                "质量检查未通过；未发现可自动修复项"
                if report.repair_rounds == 0
                else f"质量检查未通过；已尝试 {report.repair_rounds} 轮自动修复"
            )
            self.store.append_event(
                job_id,
                stage=JobStage.FAILED,
                status=JobStatus.FAILED,
                message=message,
                checkpoint=str(deck_path),
                recoverable=True,
                failure_routes=failure_routes_for_quality_issues(
                    report.issues,
                ),
            )
        return result, report

    @staticmethod
    def _promote_candidate(candidate: Path, output: Path) -> Path | None:
        """Atomically make one already-verified candidate authoritative."""

        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        output.parent.mkdir(parents=True, exist_ok=True)
        snapshot: Path | None = None
        if output.is_file():
            snapshot = candidate.parent / "previous-authoritative.pptx"
            shutil.copy2(output, snapshot)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        shutil.copy2(candidate, temporary)
        os.replace(temporary, output)
        return snapshot

    @staticmethod
    def _rollback_promotion(output: Path, snapshot: Path | None) -> None:
        """Restore the prior authoritative file if version commit fails."""

        if snapshot is None:
            output.unlink(missing_ok=True)
            return
        temporary = output.with_name(f".{output.name}.rollback.tmp")
        shutil.copy2(snapshot, temporary)
        os.replace(temporary, output)
