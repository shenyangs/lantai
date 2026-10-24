# -*- coding: utf-8 -*-
"""Evidence-backed failure classification for presentation recovery."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import re
from typing import Any

from lantai.app.utility_model import UtilityTaskIncompleteError
from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.exceptions import (
    AppBaseException,
    ArtifactDeliveryException,
    BadGatewayException,
    ForbiddenException,
    GatewayTimeoutException,
    ModelConfigurationIncompatibleException,
    ModelContextLengthExceededException,
    ModelExecutionException,
    ModelFormatterError,
    ModelNotFoundException,
    ModelProtocolException,
    ModelQuotaExceededException,
    ModelTimeoutException,
    NetworkException,
    ProviderError,
    RateLimitExceededException,
    ServiceUnavailableException,
    SystemCommandException,
    TimeoutException,
    TooManyRequestsException,
    UnauthorizedException,
    UnauthorizedModelAccessException,
)

from .models import (
    JobStage,
    PresentationFailureCategory,
    PresentationFailureRoute,
    PresentationRecoveryAction,
    QualityIssue,
)
from .page_authoring import (
    PresentationPageAuthorContentError,
    PresentationPageAuthorSceneError,
    PresentationSemanticContentError,
)

_MODEL_SERVICE_EXCEPTIONS = (
    ProviderError,
    ModelExecutionException,
    ModelProtocolException,
    ModelConfigurationIncompatibleException,
    ModelTimeoutException,
    ModelNotFoundException,
    ModelQuotaExceededException,
    ModelContextLengthExceededException,
    ModelFormatterError,
    NetworkException,
    RateLimitExceededException,
    ServiceUnavailableException,
    GatewayTimeoutException,
    TimeoutException,
    TooManyRequestsException,
    BadGatewayException,
)
_AUTHORIZATION_EXCEPTIONS = (
    PermissionError,
    UnauthorizedException,
    ForbiddenException,
    UnauthorizedModelAccessException,
)
_FILE_EXCEPTIONS = (
    OSError,
    ArtifactDeliveryException,
    SystemCommandException,
)

_CONTENT_CODES = {
    "design.repetition",
    "design.repetitive_text_dump",
    "design.text_wall",
    "vision.chart_semantics",
    "vision.narrative_transition",
    "vision.repetition",
}
_GEOMETRY_CODES = {
    "design.font_too_small",
    "vision.alignment",
    "vision.chart_readability",
    "vision.crop",
    "vision.density",
    "vision.image_distortion",
    "vision.typography",
    "vision.whitespace",
}
_MODEL_SERVICE_CODES = {
    "vision.critic_failed",
    "vision.critic_schema_invalid",
    "vision.critic_unavailable",
}
_FILE_CODES = {
    "deck.slide_count",
    "vision.render_set_incomplete",
}
_RETRYABLE_UTILITY_EXCEPTION_CODES = {
    "structuredtaskvalidationerror",
    "utilitytasktimeouterror",
}
_MODEL_SERVICE_STRUCTURED_ERROR_TYPES = {
    "access_terminated_error",
    "insufficient_quota",
    "overloaded_error",
    "quota_exceeded",
    "rate_limit_error",
    "rate_limit_exceeded",
}
_AUTHORIZATION_STRUCTURED_ERROR_TYPES = {
    "authentication_error",
    "forbidden",
    "invalid_api_key",
    "permission_error",
    "unauthorized",
}
_MODEL_SERVICE_EXCEPTION_NAMES = {
    "apiconnectionerror",
    "apitimeouterror",
    "internalservererror",
    "ratelimiterror",
    "remoteprotocolerror",
}
_RETRYABLE_PERSISTED_MODEL_SERVICE_CODES = (
    _RETRYABLE_UTILITY_EXCEPTION_CODES | _MODEL_SERVICE_EXCEPTION_NAMES
)
_AUTHORIZATION_EXCEPTION_NAMES = {
    "authenticationerror",
    "permissiondeniederror",
}
_ACCESS_TERMINATED_ACCOUNT_LIMIT_CODES = {
    "access_terminated_error.five_hour_quota",
    "access_terminated_error.weekly_quota",
    "access_terminated_error.monthly_quota",
    "access_terminated_error.concurrent_limit",
}
_SAFE_EXTERNAL_ERROR_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _structured_external_error_type(exc: Exception) -> str | None:
    """Read a stable SDK error type without inspecting provider prose."""

    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    candidates: list[Any] = [body.get("type")]
    nested = body.get("error")
    if isinstance(nested, dict):
        candidates.insert(0, nested.get("type"))
    for candidate in candidates:
        normalized = str(candidate or "").strip().casefold()
        if _SAFE_EXTERNAL_ERROR_TYPE_RE.fullmatch(normalized):
            return normalized
    return None


def _access_terminated_issue_code(
    exc: Exception,
    *,
    structured_type: str | None,
) -> str | None:
    """Disambiguate an overloaded Provider type without persisting prose.

    Kimi-compatible endpoints use ``access_terminated_error`` for both a
    resettable weekly quota and a client-policy rejection.  Those conditions
    need different recovery behavior.  Inspect only the structured error
    message in memory and return a fixed safe token; never return the message.
    Unknown variants retain the historical recovery behavior under a fixed
    message-shape token that never includes Provider prose.
    """

    if structured_type != "access_terminated_error":
        return None
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return structured_type
    nested = body.get("error")
    message_candidates: list[tuple[str, Any]] = []
    if isinstance(nested, dict):
        message_candidates.append(("nested", nested.get("message")))
    message_candidates.append(("top_level", body.get("message")))
    present_sources: list[str] = []
    for source, message in message_candidates:
        if not isinstance(message, str):
            continue
        present_sources.append(source)
        normalized = " ".join(message.casefold().split())
        if "5-hour usage limit" in normalized:
            return "access_terminated_error.five_hour_quota"
        if (
            "weekly" in normalized
            and ("usage limit" in normalized or "quota" in normalized)
        ) or "usage limit will reset" in normalized:
            return "access_terminated_error.weekly_quota"
        if "monthly usage limit" in normalized and "billing cycle" in normalized:
            return "access_terminated_error.monthly_quota"
        if "concurrent request limit" in normalized:
            return "access_terminated_error.concurrent_limit"
        if "coding agent" in normalized and any(
            marker in normalized
            for marker in ("only", "supported", "approved", "allowlist")
        ):
            return "access_terminated_error.client_policy"
    if present_sources == ["nested", "top_level"]:
        return "access_terminated_error.unrecognized_nested_and_top_level_message"
    if present_sources:
        return f"access_terminated_error.unrecognized_{present_sources[0]}_message"
    return "access_terminated_error.missing_structured_message"


def _external_provider_failure_category(
    exc: Exception,
    *,
    structured_type: str | None,
) -> PresentationFailureCategory | None:
    """Classify OpenAI-compatible SDK errors from stable fields only."""

    exception_name = type(exc).__name__.casefold()
    status_code = getattr(exc, "status_code", None)
    if (
        structured_type in _MODEL_SERVICE_STRUCTURED_ERROR_TYPES
        or exception_name in _MODEL_SERVICE_EXCEPTION_NAMES
        or status_code == 429
        or (isinstance(status_code, int) and status_code >= 500)
    ):
        return PresentationFailureCategory.MODEL_SERVICE
    if (
        structured_type in _AUTHORIZATION_STRUCTURED_ERROR_TYPES
        or exception_name in _AUTHORIZATION_EXCEPTION_NAMES
        or status_code in {401, 403}
    ):
        return PresentationFailureCategory.AUTHORIZATION
    return None


@dataclass
class _FailureRouteAccumulator:
    category: PresentationFailureCategory
    action: PresentationRecoveryAction
    retry_stage: JobStage | None
    automatic_retry_allowed: bool
    requires_human: bool
    issue_codes: list[str] = field(default_factory=list)
    slide_ids: list[str] = field(default_factory=list)

    def add(self, issue: QualityIssue) -> None:
        if issue.code not in self.issue_codes:
            self.issue_codes.append(issue.code)
        if issue.slide_id and issue.slide_id not in self.slide_ids:
            self.slide_ids.append(issue.slide_id)

    def build(self) -> PresentationFailureRoute:
        return PresentationFailureRoute(
            category=self.category,
            recovery_action=self.action,
            issue_codes=self.issue_codes,
            slide_ids=self.slide_ids,
            retry_stage=self.retry_stage,
            automatic_retry_allowed=self.automatic_retry_allowed,
            requires_human=self.requires_human,
        )


def _quality_issue_route(
    code: str,
) -> tuple[
    PresentationFailureCategory,
    PresentationRecoveryAction,
    JobStage,
    bool,
    bool,
]:
    if code in _MODEL_SERVICE_CODES:
        return (
            PresentationFailureCategory.MODEL_SERVICE,
            PresentationRecoveryAction.RETRY_MODEL_SERVICE,
            JobStage.QUALITY,
            True,
            False,
        )
    if (
        code in _FILE_CODES
        or code.startswith("pptx.")
        or code.startswith("render.")
        or code.startswith("evidence.")
        or code.startswith("editability.")
    ):
        retry_stage = (
            JobStage.RENDER
            if code.startswith("render.")
            or code == "vision.render_set_incomplete"
            else JobStage.COMPOSE
        )
        return (
            PresentationFailureCategory.FILE,
            PresentationRecoveryAction.RETRY_FILE_STAGE,
            retry_stage,
            True,
            False,
        )
    if (
        code in _CONTENT_CODES
        or code.startswith("content.")
        or code.startswith("chart.")
        or code.startswith("material.")
        or code.startswith("provenance.")
    ):
        return (
            PresentationFailureCategory.CONTENT,
            PresentationRecoveryAction.REVISE_CONTENT,
            JobStage.REPAIR,
            False,
            False,
        )
    if code in _GEOMETRY_CODES or code.startswith("layout."):
        return (
            PresentationFailureCategory.GEOMETRY,
            PresentationRecoveryAction.PATCH_GEOMETRY,
            JobStage.REPAIR,
            False,
            False,
        )
    if (
        code.startswith("design.")
        or code.startswith("vision.")
        or code == "user.redesign"
    ):
        return (
            PresentationFailureCategory.AESTHETIC,
            PresentationRecoveryAction.REDESIGN_SLIDE,
            JobStage.REPAIR,
            False,
            False,
        )
    return (
        PresentationFailureCategory.UNKNOWN,
        PresentationRecoveryAction.INSPECT_FAILURE,
        JobStage.QUALITY,
        False,
        True,
    )


def failure_routes_for_quality_issues(
    issues: Iterable[QualityIssue],
) -> list[PresentationFailureRoute]:
    """Group blocking QA evidence into stable, stage-specific recovery routes."""

    grouped: dict[
        tuple[
            PresentationFailureCategory,
            PresentationRecoveryAction,
            JobStage,
        ],
        _FailureRouteAccumulator,
    ] = {}
    for issue in issues:
        if issue.severity != "error":
            continue
        category, action, retry_stage, retry_allowed, requires_human = (
            _quality_issue_route(issue.code)
        )
        key = (category, action, retry_stage)
        accumulator = grouped.setdefault(
            key,
            _FailureRouteAccumulator(
                category=category,
                action=action,
                retry_stage=retry_stage,
                automatic_retry_allowed=retry_allowed,
                requires_human=requires_human,
            ),
        )
        accumulator.add(issue)
    return [accumulator.build() for accumulator in grouped.values()]


def failure_route_for_exception(
    exc: Exception,
    *,
    stage: JobStage,
) -> PresentationFailureRoute:
    """Classify an execution exception without inferring facts from its prose."""

    structured_type = _structured_external_error_type(exc)
    access_terminated_issue_code = _access_terminated_issue_code(
        exc,
        structured_type=structured_type,
    )
    if isinstance(
        exc,
        (PresentationPageAuthorContentError, PresentationSemanticContentError),
    ):
        issue_code = f"content.{exc.violation_code}"
    elif isinstance(exc, PresentationPageAuthorSceneError):
        issue_code = f"layout.{exc.violation_code}"
    elif isinstance(exc, UtilityTaskIncompleteError):
        issue_code = {
            "length": "model_output_truncated",
            "content_filter": "model_output_filtered",
        }.get(exc.finish_reason, "model_output_incomplete")
    elif access_terminated_issue_code:
        issue_code = access_terminated_issue_code
    elif structured_type:
        issue_code = structured_type
    elif isinstance(exc, AppBaseException):
        issue_code = str(exc.code).lower()
    else:
        issue_code = type(exc).__name__.lower()
    external_category = _external_provider_failure_category(
        exc,
        structured_type=structured_type,
    )
    if issue_code in _ACCESS_TERMINATED_ACCOUNT_LIMIT_CODES:
        category = PresentationFailureCategory.MODEL_SERVICE
        action = PresentationRecoveryAction.RETRY_MODEL_SERVICE
        retry_allowed = False
        requires_human = True
    elif issue_code == "access_terminated_error.client_policy":
        category = PresentationFailureCategory.AUTHORIZATION
        action = PresentationRecoveryAction.REQUEST_AUTHORIZATION
        retry_allowed = False
        requires_human = True
    elif isinstance(exc, UtilityTaskIncompleteError):
        category = PresentationFailureCategory.MODEL_SERVICE
        action = PresentationRecoveryAction.RETRY_MODEL_SERVICE
        # Replaying the same token limit or filtered input is not a repair.
        # Keep the checkpoint, but require a changed recovery request.
        retry_allowed = False
        requires_human = False
    elif issue_code in _RETRYABLE_UTILITY_EXCEPTION_CODES:
        category = PresentationFailureCategory.MODEL_SERVICE
        action = PresentationRecoveryAction.RETRY_MODEL_SERVICE
        retry_allowed = True
        requires_human = False
    elif isinstance(exc, PresentationPageAuthorSceneError):
        category = PresentationFailureCategory.GEOMETRY
        action = PresentationRecoveryAction.PATCH_GEOMETRY
        retry_allowed = False
        requires_human = False
    elif isinstance(
        exc,
        (PresentationPageAuthorContentError, PresentationSemanticContentError),
    ):
        category = PresentationFailureCategory.CONTENT
        action = PresentationRecoveryAction.REVISE_CONTENT
        retry_allowed = False
        requires_human = False
    elif isinstance(exc, _AUTHORIZATION_EXCEPTIONS):
        category = PresentationFailureCategory.AUTHORIZATION
        action = PresentationRecoveryAction.REQUEST_AUTHORIZATION
        retry_allowed = False
        requires_human = True
    elif external_category == PresentationFailureCategory.MODEL_SERVICE:
        category = PresentationFailureCategory.MODEL_SERVICE
        action = PresentationRecoveryAction.RETRY_MODEL_SERVICE
        retry_allowed = True
        requires_human = False
    elif external_category == PresentationFailureCategory.AUTHORIZATION:
        category = PresentationFailureCategory.AUTHORIZATION
        action = PresentationRecoveryAction.REQUEST_AUTHORIZATION
        retry_allowed = False
        requires_human = True
    elif isinstance(exc, _MODEL_SERVICE_EXCEPTIONS):
        category = PresentationFailureCategory.MODEL_SERVICE
        action = PresentationRecoveryAction.RETRY_MODEL_SERVICE
        retry_allowed = True
        requires_human = False
    elif isinstance(exc, _FILE_EXCEPTIONS):
        category = PresentationFailureCategory.FILE
        action = PresentationRecoveryAction.RETRY_FILE_STAGE
        retry_allowed = True
        requires_human = False
    else:
        category = PresentationFailureCategory.UNKNOWN
        action = PresentationRecoveryAction.INSPECT_FAILURE
        retry_allowed = False
        requires_human = True
    slide_ids = []
    for order in getattr(exc, "presentation_detail_target_orders", []):
        if (
            isinstance(order, int)
            and not isinstance(order, bool)
            and 1 <= order <= 60
        ):
            slide_ids.append(f"slide-{order:03d}")
    retry_stage = stage
    if isinstance(
        exc,
        (PresentationPageAuthorContentError, PresentationPageAuthorSceneError),
    ):
        # A page-author contract can surface while reloading an already
        # repaired DeckIR during a later quality retry.  Rewind to compose so
        # the stale merged DeckIR is archived and clean page-scene checkpoints
        # can be reused, instead of reopening the same invalid checkpoint.
        retry_stage = JobStage.COMPOSE
    elif isinstance(exc, PresentationSemanticContentError):
        retry_stage = JobStage.PLAN
    return PresentationFailureRoute(
        category=category,
        recovery_action=action,
        issue_codes=[issue_code],
        slide_ids=list(dict.fromkeys(slide_ids)),
        retry_stage=retry_stage,
        automatic_retry_allowed=retry_allowed,
        requires_human=requires_human,
    )


def failure_message_for_exception(
    exc: Exception,
    *,
    route: PresentationFailureRoute | None = None,
) -> str:
    """Keep engineering diagnostics, never rejected model keys or prose."""

    resolved = route or failure_route_for_exception(exc, stage=JobStage.FAILED)
    structured_type = _structured_external_error_type(exc)
    external_category = _external_provider_failure_category(
        exc,
        structured_type=structured_type,
    )
    if external_category is not None or isinstance(
        exc,
        (
            StructuredTaskValidationError,
            PresentationSemanticContentError,
            PresentationPageAuthorContentError,
            PresentationPageAuthorSceneError,
        ),
    ):
        issue_code = (
            resolved.issue_codes[0] if resolved.issue_codes else "provider_error"
        )
        return f"当前步骤失败：{resolved.category.value}（{issue_code}）"
    return f"当前步骤失败：{exc}"


def normalize_persisted_failure_routes(
    routes: Iterable[PresentationFailureRoute],
) -> list[PresentationFailureRoute]:
    """Read pre-fix model-service failures through the current stable type map.

    Historical events stay append-only.  This dual-read adapter only corrects
    routes whose persisted exception type is an exact known retryable failure;
    unknown legacy failures remain human-reviewed.
    """

    normalized: list[PresentationFailureRoute] = []
    for route in routes:
        if any(
            code in _RETRYABLE_PERSISTED_MODEL_SERVICE_CODES
            for code in route.issue_codes
        ):
            route = route.model_copy(update={
                "category": PresentationFailureCategory.MODEL_SERVICE,
                "recovery_action": PresentationRecoveryAction.RETRY_MODEL_SERVICE,
                "automatic_retry_allowed": True,
                "requires_human": False,
            })
        normalized.append(route)
    return normalized


def failure_routes_metadata(
    routes: Iterable[PresentationFailureRoute],
) -> dict[str, Any]:
    """Serialize recovery routes once for tool responses and status probes."""

    materialized = list(routes)
    return {
        "failure_routes": [
            route.model_dump(mode="json") for route in materialized
        ],
        "failure_categories": list(
            dict.fromkeys(route.category.value for route in materialized)
        ),
        "recovery_actions": list(
            dict.fromkeys(route.recovery_action.value for route in materialized)
        ),
        "automatic_retry_allowed": bool(materialized)
        and all(
            route.automatic_retry_allowed and not route.requires_human
            for route in materialized
        ),
        "requires_human": any(route.requires_human for route in materialized),
    }
