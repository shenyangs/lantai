from __future__ import annotations

from concurrent.futures import Future
import json

import httpx
import openai
import pytest

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.app.utility_model import UtilityTaskIncompleteError, UtilityTaskTimeoutError
from lantai.exceptions import (
    PermissionDeniedException,
    ProviderError,
)
from lantai.presentation.failure import (
    failure_message_for_exception,
    failure_route_for_exception,
    failure_routes_for_quality_issues,
    failure_routes_metadata,
)
from lantai.presentation.jobs import (
    MAX_PRESENTATION_EXTERNAL_RECOVERY_ATTEMPTS,
    MAX_PRESENTATION_IMPLEMENTATION_RECOVERY_ATTEMPTS,
    PresentationJobStore,
)
from lantai.presentation.models import (
    JobStage,
    JobStatus,
    PresentationFailureCategory,
    PresentationFailureRoute,
    PresentationJobRequest,
    PresentationRecoveryAction,
    QualityIssue,
)
from lantai.presentation.page_authoring import (
    PresentationPageAuthorContentError,
    PresentationPageAuthorSceneError,
    PresentationSemanticContentError,
)
from lantai.presentation.runtime import PresentationJobRuntime


def _request() -> PresentationJobRequest:
    return PresentationJobRequest(
        conversation_id="conversation-a",
        user_request="build a deck",
        slide_count=1,
    )


def test_quality_failure_routes_preserve_distinct_causes() -> None:
    routes = failure_routes_for_quality_issues(
        [
            QualityIssue(
                code="content.required_fact_missing",
                severity="error",
                message="required fact is absent",
            ),
            QualityIssue(
                code="layout.out_of_bounds",
                severity="error",
                message="object is outside the slide",
                repairable=True,
            ),
            QualityIssue(
                code="vision.aesthetic_staleness",
                severity="error",
                message="page needs a new visual idea",
                repairable=True,
            ),
            QualityIssue(
                code="pptx.reopen_failed",
                severity="error",
                message="package cannot be reopened",
            ),
            QualityIssue(
                code="vision.critic_unavailable",
                severity="error",
                message="critic provider is unavailable",
            ),
        ]
    )

    assert [route.category for route in routes] == [
        PresentationFailureCategory.CONTENT,
        PresentationFailureCategory.GEOMETRY,
        PresentationFailureCategory.AESTHETIC,
        PresentationFailureCategory.FILE,
        PresentationFailureCategory.MODEL_SERVICE,
    ]
    assert [route.recovery_action for route in routes] == [
        PresentationRecoveryAction.REVISE_CONTENT,
        PresentationRecoveryAction.PATCH_GEOMETRY,
        PresentationRecoveryAction.REDESIGN_SLIDE,
        PresentationRecoveryAction.RETRY_FILE_STAGE,
        PresentationRecoveryAction.RETRY_MODEL_SERVICE,
    ]
    assert routes[1].slide_ids == []
    assert routes[1].issue_codes == ["layout.out_of_bounds"]
    assert routes[4].automatic_retry_allowed is True


def test_exception_failure_routes_distinguish_provider_authorization_and_file() -> None:
    provider = failure_route_for_exception(
        ProviderError("provider unavailable"),
        stage=JobStage.QUALITY,
    )
    authorization = failure_route_for_exception(
        PermissionDeniedException("approval is missing"),
        stage=JobStage.COMPOSE,
    )
    file_failure = failure_route_for_exception(
        OSError("cannot write package"),
        stage=JobStage.COMPOSE,
    )

    assert provider.category == PresentationFailureCategory.MODEL_SERVICE
    assert provider.recovery_action == PresentationRecoveryAction.RETRY_MODEL_SERVICE
    assert provider.retry_stage == JobStage.QUALITY
    assert provider.automatic_retry_allowed is True
    assert authorization.category == PresentationFailureCategory.AUTHORIZATION
    assert authorization.recovery_action == (
        PresentationRecoveryAction.REQUEST_AUTHORIZATION
    )
    assert authorization.requires_human is True
    assert authorization.automatic_retry_allowed is False
    assert file_failure.category == PresentationFailureCategory.FILE
    assert file_failure.recovery_action == (
        PresentationRecoveryAction.RETRY_FILE_STAGE
    )


def test_remote_protocol_failure_routes_to_automatic_model_service_retry() -> None:
    error = httpx.RemoteProtocolError(
        "private upstream disconnect prose",
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )

    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.MODEL_SERVICE
    assert route.recovery_action == PresentationRecoveryAction.RETRY_MODEL_SERVICE
    assert route.issue_codes == ["remoteprotocolerror"]
    assert route.retry_stage == JobStage.COMPOSE
    assert route.automatic_retry_allowed is True
    assert route.requires_human is False
    message = failure_message_for_exception(error, route=route)
    assert message == "当前步骤失败：model_service（remoteprotocolerror）"
    assert "private upstream" not in message


def test_openai_compatible_quota_403_routes_to_model_service_without_error_prose() -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.PermissionDeniedError(
        "raw upstream quota prose must not be persisted",
        response=response,
        body={
            "error": {
                "type": "access_terminated_error",
                "message": "raw upstream quota prose must not be persisted",
            }
        },
    )

    route = failure_route_for_exception(error, stage=JobStage.PLAN)

    assert route.category == PresentationFailureCategory.MODEL_SERVICE
    assert route.recovery_action == PresentationRecoveryAction.RETRY_MODEL_SERVICE
    assert route.issue_codes == [
        "access_terminated_error.unrecognized_nested_message"
    ]
    assert route.automatic_retry_allowed is True
    assert route.requires_human is False
    message = failure_message_for_exception(error, route=route)
    assert message == (
        "当前步骤失败：model_service（"
        "access_terminated_error.unrecognized_nested_message）"
    )
    assert "raw upstream" not in message


def test_access_terminated_weekly_quota_is_safe_and_not_immediately_retried() -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.PermissionDeniedError(
        "private provider prose",
        response=response,
        body={
            "error": {
                "type": "access_terminated_error",
                "message": (
                    "You have reached the weekly usage limit. "
                    "Your usage limit will reset later."
                ),
            }
        },
    )

    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.MODEL_SERVICE
    assert route.recovery_action == PresentationRecoveryAction.RETRY_MODEL_SERVICE
    assert route.issue_codes == ["access_terminated_error.weekly_quota"]
    assert route.automatic_retry_allowed is False
    assert route.requires_human is True
    message = failure_message_for_exception(error, route=route)
    assert message == (
        "当前步骤失败：model_service（access_terminated_error.weekly_quota）"
    )
    assert "usage limit" not in message
    assert "reset later" not in message


@pytest.mark.parametrize(
    ("provider_message", "issue_code"),
    [
        (
            "You've reached your 5-hour usage limit. Your quota will reset "
            "when the current 5-hour window ends.",
            "access_terminated_error.five_hour_quota",
        ),
        (
            "You've reached your monthly usage limit for this billing cycle. "
            "Your quota will be refreshed in the next cycle.",
            "access_terminated_error.monthly_quota",
        ),
        (
            "You've reached your concurrent request limit. Please wait for "
            "your ongoing requests to finish and try again.",
            "access_terminated_error.concurrent_limit",
        ),
    ],
)
def test_access_terminated_account_limits_are_safe_and_not_automatically_retried(
    provider_message: str,
    issue_code: str,
) -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.PermissionDeniedError(
        "private provider prose",
        response=response,
        body={
            "type": "access_terminated_error",
            "message": provider_message,
        },
    )

    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.MODEL_SERVICE
    assert route.recovery_action == PresentationRecoveryAction.RETRY_MODEL_SERVICE
    assert route.issue_codes == [issue_code]
    assert route.automatic_retry_allowed is False
    assert route.requires_human is True
    message = failure_message_for_exception(error, route=route)
    assert message == f"当前步骤失败：model_service（{issue_code}）"
    assert provider_message not in message


def test_access_terminated_client_policy_routes_to_authorization_safely() -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.PermissionDeniedError(
        "private provider prose",
        response=response,
        body={
            "error": {
                "type": "access_terminated_error",
                "message": "This endpoint only supports approved coding agents.",
            }
        },
    )

    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.AUTHORIZATION
    assert route.recovery_action == PresentationRecoveryAction.REQUEST_AUTHORIZATION
    assert route.issue_codes == ["access_terminated_error.client_policy"]
    assert route.automatic_retry_allowed is False
    assert route.requires_human is True
    message = failure_message_for_exception(error, route=route)
    assert message == (
        "当前步骤失败：authorization（access_terminated_error.client_policy）"
    )
    assert "coding agents" not in message


def test_access_terminated_uses_top_level_message_when_nested_error_has_only_type() -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.PermissionDeniedError(
        "private provider prose",
        response=response,
        body={
            "type": "access_terminated_error",
            "message": (
                "Kimi For Coding is currently only available for Coding Agents."
            ),
            "error": {"type": "access_terminated_error"},
        },
    )

    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.AUTHORIZATION
    assert route.issue_codes == ["access_terminated_error.client_policy"]
    assert route.automatic_retry_allowed is False
    assert route.requires_human is True


def test_access_terminated_records_only_safe_unknown_message_shape() -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.PermissionDeniedError(
        "private provider prose",
        response=response,
        body={
            "error": {
                "type": "access_terminated_error",
                "message": "unrecognized private provider prose",
            }
        },
    )

    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.MODEL_SERVICE
    assert route.issue_codes == [
        "access_terminated_error.unrecognized_nested_message"
    ]
    assert route.automatic_retry_allowed is True
    assert route.requires_human is False
    message = failure_message_for_exception(error, route=route)
    assert message == (
        "当前步骤失败：model_service（"
        "access_terminated_error.unrecognized_nested_message）"
    )
    assert "private provider prose" not in message


def test_access_terminated_records_missing_structured_message_safely() -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.PermissionDeniedError(
        "private provider prose",
        response=response,
        body={"error": {"type": "access_terminated_error"}},
    )

    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.MODEL_SERVICE
    assert route.issue_codes == [
        "access_terminated_error.missing_structured_message"
    ]
    assert "private provider prose" not in failure_message_for_exception(
        error,
        route=route,
    )


def test_openai_compatible_permission_403_stays_authorization_failure() -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.PermissionDeniedError(
        "raw upstream permission prose",
        response=response,
        body={"error": {"type": "permission_error"}},
    )

    route = failure_route_for_exception(error, stage=JobStage.PLAN)

    assert route.category == PresentationFailureCategory.AUTHORIZATION
    assert route.recovery_action == PresentationRecoveryAction.REQUEST_AUTHORIZATION
    assert route.issue_codes == ["permission_error"]
    assert route.automatic_retry_allowed is False
    assert route.requires_human is True


@pytest.mark.parametrize(
    "error",
    [
        UtilityTaskTimeoutError(phase="total", timeout_seconds=120),
        StructuredTaskValidationError(
            "structured utility task output failed schema validation",
        ),
    ],
)
def test_utility_model_failures_never_route_as_files(error: Exception) -> None:
    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.MODEL_SERVICE
    assert route.recovery_action == (
        PresentationRecoveryAction.RETRY_MODEL_SERVICE
    )
    assert route.retry_stage == JobStage.COMPOSE
    assert route.automatic_retry_allowed is True
    assert route.requires_human is False


@pytest.mark.parametrize("stage", [JobStage.PLAN, JobStage.COMPOSE])
@pytest.mark.parametrize("reason,code", [
    ("length", "model_output_truncated"),
    ("content_filter", "model_output_filtered"),
])
def test_incomplete_utility_output_has_honest_route_without_blind_retry(
    stage, reason, code,
) -> None:
    error = UtilityTaskIncompleteError(finish_reason=reason)
    route = failure_route_for_exception(error, stage=stage)
    assert route.category == PresentationFailureCategory.MODEL_SERVICE
    assert route.recovery_action == PresentationRecoveryAction.RETRY_MODEL_SERVICE
    assert route.issue_codes == [code]
    assert route.retry_stage == stage
    assert route.automatic_retry_allowed is False
    assert route.requires_human is False


def test_page_author_content_failure_routes_to_content_repair() -> None:
    route = failure_route_for_exception(
        PresentationPageAuthorContentError(
            "safe content-gate summary",
            violation_code="unsupported_number",
        ),
        stage=JobStage.COMPOSE,
    )

    assert route.category == PresentationFailureCategory.CONTENT
    assert route.recovery_action == PresentationRecoveryAction.REVISE_CONTENT
    assert route.retry_stage == JobStage.COMPOSE
    assert route.automatic_retry_allowed is False
    assert route.requires_human is False
    assert route.issue_codes == ["content.unsupported_number"]


def test_page_author_content_failure_detected_at_quality_rewinds_to_compose(
) -> None:
    route = failure_route_for_exception(
        PresentationPageAuthorContentError(
            "speaker notes contain unsupported numeric token(s)",
            violation_code="numeric_speaker_notes",
        ),
        stage=JobStage.QUALITY,
    )

    assert route.category == PresentationFailureCategory.CONTENT
    assert route.recovery_action == PresentationRecoveryAction.REVISE_CONTENT
    assert route.retry_stage == JobStage.COMPOSE
    assert route.automatic_retry_allowed is False
    assert route.requires_human is False
    assert route.issue_codes == ["content.numeric_speaker_notes"]


def test_page_author_missing_fact_failure_routes_to_safe_slide_id() -> None:
    error = PresentationPageAuthorContentError(
        "page author omitted one or more required visible facts",
        violation_code="required_visible_fact_omitted",
    )
    error.presentation_missing_fact_paths = ["required_facts[0]"]
    error.presentation_detail_target_orders = [1]

    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)

    assert route.category == PresentationFailureCategory.CONTENT
    assert route.recovery_action == PresentationRecoveryAction.REVISE_CONTENT
    assert route.retry_stage == JobStage.COMPOSE
    assert route.automatic_retry_allowed is False
    assert route.requires_human is False
    assert route.issue_codes == ["content.required_visible_fact_omitted"]
    assert route.slide_ids == ["slide-001"]


def test_semantic_content_failure_routes_to_plan_content_repair() -> None:
    error = PresentationSemanticContentError(
        "safe semantic contract summary",
        violation_code="before_after_cardinality",
    )
    error.presentation_detail_target_orders = [3, 3, 0, 61, True, "4"]

    route = failure_route_for_exception(
        error,
        stage=JobStage.PLAN,
    )

    assert route.category == PresentationFailureCategory.CONTENT
    assert route.recovery_action == PresentationRecoveryAction.REVISE_CONTENT
    assert route.retry_stage == JobStage.PLAN
    assert route.automatic_retry_allowed is False
    assert route.requires_human is False
    assert route.issue_codes == ["content.before_after_cardinality"]
    assert route.slide_ids == ["slide-003"]


def test_page_author_scene_failure_routes_to_geometry_repair() -> None:
    route = failure_route_for_exception(
        PresentationPageAuthorSceneError(
            "safe scene-gate summary",
            violation_code="dangling_object_reference",
        ),
        stage=JobStage.COMPOSE,
    )

    assert route.category == PresentationFailureCategory.GEOMETRY
    assert route.recovery_action == PresentationRecoveryAction.PATCH_GEOMETRY
    assert route.retry_stage == JobStage.COMPOSE
    assert route.automatic_retry_allowed is False
    assert route.requires_human is False


def test_mixed_failure_routes_never_advertise_automatic_retry() -> None:
    provider = failure_route_for_exception(
        ProviderError("provider unavailable"),
        stage=JobStage.QUALITY,
    )
    authorization = failure_route_for_exception(
        PermissionDeniedException("approval is missing"),
        stage=JobStage.COMPOSE,
    )

    metadata = failure_routes_metadata([provider, authorization])

    assert metadata["automatic_retry_allowed"] is False
    assert metadata["requires_human"] is True


def test_job_event_persists_failure_routes_for_read_only_status(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    route = failure_route_for_exception(
        ProviderError("provider unavailable"),
        stage=JobStage.PLAN,
    )

    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="provider unavailable",
        recoverable=True,
        failure_routes=[route],
    )

    restored = PresentationJobStore(tmp_path).get(record.job_id)
    assert restored.events[-1].failure_routes == [route]
    assert restored.events[-1].failure_routes[0].retry_stage == JobStage.PLAN


def test_job_store_reads_legacy_event_without_failure_routes(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="legacy failure",
        recoverable=True,
    )
    manifest_path = store.job_dir(record.job_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("recovery_attempt_count", None)
    manifest.pop("external_recovery_attempt_count", None)
    manifest.pop("implementation_recovery_attempt_count", None)
    manifest.pop("implementation_recovery_fixes", None)
    for event in manifest["events"]:
        event.pop("failure_routes", None)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    restored = PresentationJobStore(tmp_path).get(record.job_id)

    assert restored.events[-1].failure_routes == []
    assert restored.recovery_attempt_count == 0
    assert restored.external_recovery_attempt_count == 0
    assert restored.implementation_recovery_attempt_count == 0
    assert restored.implementation_recovery_fixes == []


def test_failed_job_retry_reuses_job_and_persists_bounded_attempt(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    route = failure_route_for_exception(
        ProviderError("provider unavailable"),
        stage=JobStage.QUALITY,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="provider unavailable",
        recoverable=True,
        failure_routes=[route],
    )

    event = store.retry_failed(record.job_id, "conversation-a")
    retried = store.get(record.job_id)

    assert event.job_id == record.job_id
    assert event.stage == JobStage.QUALITY
    assert event.status == JobStatus.RUNNING
    assert retried.recovery_attempt_count == 1
    assert retried.cost_used_units == 0
    assert len(store.list_records("conversation-a")) == 1


def test_failed_job_retry_normalizes_known_pre_fix_utility_route(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    pre_fix_route = PresentationFailureRoute(
        category=PresentationFailureCategory.UNKNOWN,
        recovery_action=PresentationRecoveryAction.INSPECT_FAILURE,
        issue_codes=["structuredtaskvalidationerror"],
        retry_stage=JobStage.COMPOSE,
        automatic_retry_allowed=False,
        requires_human=True,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="pre-fix structured utility failure",
        recoverable=True,
        failure_routes=[pre_fix_route],
    )

    event = store.retry_failed(record.job_id, "conversation-a")

    assert event.job_id == record.job_id
    assert event.stage == JobStage.COMPOSE
    assert event.status == JobStatus.RUNNING
    assert store.get(record.job_id).recovery_attempt_count == 1


def test_failed_job_retry_normalizes_pre_fix_remote_protocol_route(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    pre_fix_route = PresentationFailureRoute(
        category=PresentationFailureCategory.UNKNOWN,
        recovery_action=PresentationRecoveryAction.INSPECT_FAILURE,
        issue_codes=["remoteprotocolerror"],
        retry_stage=JobStage.COMPOSE,
        automatic_retry_allowed=False,
        requires_human=True,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="pre-fix remote protocol failure",
        recoverable=True,
        failure_routes=[pre_fix_route],
    )

    event = store.retry_failed(record.job_id, "conversation-a")

    assert event.job_id == record.job_id
    assert event.stage == JobStage.COMPOSE
    assert event.status == JobStatus.RUNNING
    assert store.get(record.job_id).recovery_attempt_count == 1


def test_failed_job_retry_rejects_human_or_unclassified_route(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    authorization = store.create(_request())
    authorization_route = failure_route_for_exception(
        PermissionDeniedException("approval is missing"),
        stage=JobStage.COMPOSE,
    )
    store.append_event(
        authorization.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="approval is missing",
        recoverable=True,
        failure_routes=[authorization_route],
    )
    legacy = store.create(_request())
    store.append_event(
        legacy.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="legacy failure",
        recoverable=True,
    )

    with pytest.raises(ValueError, match="human review"):
        store.retry_failed(authorization.job_id, "conversation-a")
    with pytest.raises(ValueError, match="classified recovery route"):
        store.retry_failed(legacy.job_id, "conversation-a")

    assert store.get(authorization.job_id).status == JobStatus.FAILED
    assert store.get(authorization.job_id).recovery_attempt_count == 0
    assert store.get(legacy.job_id).status == JobStatus.FAILED


def test_reviewed_failed_job_retry_is_append_only_and_reuses_original_job(
    tmp_path,
) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    persisted_route = PresentationFailureRoute(
        category=PresentationFailureCategory.UNKNOWN,
        recovery_action=PresentationRecoveryAction.INSPECT_FAILURE,
        issue_codes=["valueerror"],
        retry_stage=JobStage.COMPOSE,
        automatic_retry_allowed=False,
        requires_human=True,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="legacy generic failure",
        recoverable=True,
        failure_routes=[persisted_route],
    )
    reviewed_route = PresentationFailureRoute(
        category=PresentationFailureCategory.CONTENT,
        recovery_action=PresentationRecoveryAction.REVISE_CONTENT,
        issue_codes=["unsupported_number"],
        retry_stage=JobStage.COMPOSE,
        automatic_retry_allowed=False,
        requires_human=False,
    )

    with pytest.raises(ValueError, match="human review"):
        store.retry_failed(record.job_id, "conversation-a")

    event = store.retry_failed_after_review(
        record.job_id,
        "conversation-a",
        reviewed_routes=[reviewed_route],
        review_basis="runtime_stack_and_deterministic_grounding_gate",
    )
    retried = store.get(record.job_id)

    assert event.job_id == record.job_id
    assert event.stage == JobStage.COMPOSE
    assert event.status == JobStatus.RUNNING
    assert event.failure_routes == [reviewed_route]
    assert retried.recovery_attempt_count == 1
    assert retried.events[-2].failure_routes == [persisted_route]
    assert retried.events[-2].status == JobStatus.FAILED
    assert len(store.list_records("conversation-a")) == 1


def test_plan_retry_archives_stale_plan_and_deck_without_deleting_history(
    tmp_path,
) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    brief_path = store.write_checkpoint(
        record.job_id, "brief.json", '{"brief":"kept"}',
    )
    plan_path = store.write_checkpoint(
        record.job_id, "deck-plan.json", '{"plan":"old"}',
    )
    deck_path = store.write_checkpoint(
        record.job_id, "deck-ir.json", '{"deck":"old"}',
    )
    store.append_event(
        record.job_id,
        stage=JobStage.BRIEF,
        status=JobStatus.RUNNING,
        message="brief ready",
        checkpoint=str(brief_path),
    )
    store.append_event(
        record.job_id,
        stage=JobStage.PLAN,
        status=JobStatus.RUNNING,
        message="plan ready",
        checkpoint=str(plan_path),
    )
    store.append_event(
        record.job_id,
        stage=JobStage.DECK_IR,
        status=JobStatus.RUNNING,
        message="deck ready",
        checkpoint=str(deck_path),
    )
    persisted_route = PresentationFailureRoute(
        category=PresentationFailureCategory.UNKNOWN,
        recovery_action=PresentationRecoveryAction.INSPECT_FAILURE,
        issue_codes=["content_contract_mismatch"],
        retry_stage=JobStage.PLAN,
        automatic_retry_allowed=False,
        requires_human=True,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="plan contract needs reviewed regeneration",
        recoverable=True,
        failure_routes=[persisted_route],
    )
    reviewed_route = persisted_route.model_copy(update={
        "category": PresentationFailureCategory.CONTENT,
        "recovery_action": PresentationRecoveryAction.REVISE_CONTENT,
        "issue_codes": ["content.required_fact_contract"],
        "requires_human": False,
    })

    event = store.retry_failed_after_review(
        record.job_id,
        "conversation-a",
        reviewed_routes=[reviewed_route],
        review_basis="required_fact_contract.v2",
    )

    history = (
        store.job_dir(record.job_id)
        / "checkpoints"
        / "history"
        / "recovery-001-from-plan"
    )
    retried = store.get(record.job_id)
    assert event.checkpoint is None
    assert "已归档 2 个过期下游检查点" in event.message
    assert store.checkpoint_path(record.job_id, "brief.json").is_file()
    assert not store.checkpoint_path(record.job_id, "deck-plan.json").exists()
    assert not store.checkpoint_path(record.job_id, "deck-ir.json").exists()
    assert (history / "deck-plan.json").read_text(encoding="utf-8") == (
        '{"plan":"old"}'
    )
    assert (history / "deck-ir.json").read_text(encoding="utf-8") == (
        '{"deck":"old"}'
    )
    assert JobStage.BRIEF.value in retried.checkpoints
    assert JobStage.PLAN.value not in retried.checkpoints
    assert JobStage.DECK_IR.value not in retried.checkpoints
    assert retried.events[-2].failure_routes == [persisted_route]


def test_reviewed_failed_job_retry_cannot_bypass_authorization(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    route = failure_route_for_exception(
        PermissionDeniedException("approval is missing"),
        stage=JobStage.COMPOSE,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="approval is missing",
        recoverable=True,
        failure_routes=[route],
    )

    with pytest.raises(ValueError, match="authorization"):
        store.retry_failed_after_review(
            record.job_id,
            "conversation-a",
            reviewed_routes=[route],
            review_basis="operator_review",
        )

    failed = store.get(record.job_id)
    assert failed.status == JobStatus.FAILED
    assert failed.recovery_attempt_count == 0


def test_failed_job_retry_is_bounded_and_keeps_original_budget(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(
        PresentationJobRequest(
            conversation_id="conversation-a",
            user_request="build a deck",
            slide_count=1,
            cost_budget_units=1,
        )
    )
    route = failure_route_for_exception(
        OSError("temporary file failure"),
        stage=JobStage.COMPOSE,
    )

    for expected_attempt in (1, 2, 3):
        store.append_event(
            record.job_id,
            stage=JobStage.FAILED,
            status=JobStatus.FAILED,
            message="temporary file failure",
            recoverable=True,
            failure_routes=[route],
        )
        store.retry_failed(record.job_id, "conversation-a")
        assert (
            store.get(record.job_id).recovery_attempt_count
            == expected_attempt
        )

    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="temporary file failure",
        recoverable=True,
        failure_routes=[route],
    )
    with pytest.raises(RuntimeError, match="recovery attempt limit"):
        store.retry_failed(record.job_id, "conversation-a")

    exhausted = store.get(record.job_id)
    assert exhausted.status == JobStatus.FAILED
    assert exhausted.recovery_attempt_count == 3
    exhausted.cost_used_units = exhausted.cost_budget_units
    exhausted.recovery_attempt_count = 0
    store._write(exhausted)
    with pytest.raises(RuntimeError, match="cost budget"):
        store.retry_failed(record.job_id, "conversation-a")


def test_verified_external_model_recovery_extends_exhausted_job_without_reset(
    tmp_path,
) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    exhausted = store.get(record.job_id)
    exhausted.recovery_attempt_count = 3
    store._write(exhausted)
    historical_route = PresentationFailureRoute(
        category=PresentationFailureCategory.UNKNOWN,
        recovery_action=PresentationRecoveryAction.INSPECT_FAILURE,
        issue_codes=["permissiondeniederror"],
        retry_stage=JobStage.PLAN,
        automatic_retry_allowed=False,
        requires_human=True,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="historical provider quota failure",
        recoverable=True,
        failure_routes=[historical_route],
    )
    reviewed_route = PresentationFailureRoute(
        category=PresentationFailureCategory.MODEL_SERVICE,
        recovery_action=PresentationRecoveryAction.RETRY_MODEL_SERVICE,
        issue_codes=["access_terminated_error"],
        retry_stage=JobStage.PLAN,
        automatic_retry_allowed=True,
        requires_human=False,
    )

    event = store.retry_failed_after_external_recovery(
        record.job_id,
        "conversation-a",
        reviewed_routes=[reviewed_route],
        review_basis="same_route_provider_smoke_passed",
    )
    retried = store.get(record.job_id)

    assert event.job_id == record.job_id
    assert event.stage == JobStage.PLAN
    assert event.failure_routes == [reviewed_route]
    assert retried.recovery_attempt_count == 3
    assert retried.external_recovery_attempt_count == 1
    assert retried.events[-2].failure_routes == [historical_route]
    assert len(store.list_records("conversation-a")) == 1


def test_external_model_recovery_is_bounded_and_keeps_original_budget(
    tmp_path,
) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(
        PresentationJobRequest(
            conversation_id="conversation-a",
            user_request="build a deck",
            slide_count=1,
            cost_budget_units=1,
        )
    )
    route = PresentationFailureRoute(
        category=PresentationFailureCategory.MODEL_SERVICE,
        recovery_action=PresentationRecoveryAction.RETRY_MODEL_SERVICE,
        issue_codes=["access_terminated_error"],
        retry_stage=JobStage.PLAN,
        automatic_retry_allowed=True,
        requires_human=False,
    )

    for expected_attempt in range(
        1,
        MAX_PRESENTATION_EXTERNAL_RECOVERY_ATTEMPTS + 1,
    ):
        store.append_event(
            record.job_id,
            stage=JobStage.FAILED,
            status=JobStatus.FAILED,
            message="provider quota window not ready",
            recoverable=True,
            failure_routes=[route],
        )
        store.retry_failed_after_external_recovery(
            record.job_id,
            "conversation-a",
            reviewed_routes=[route],
            review_basis="same_route_provider_smoke_passed",
        )
        assert (
            store.get(record.job_id).external_recovery_attempt_count
            == expected_attempt
        )

    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="provider quota window not ready",
        recoverable=True,
        failure_routes=[route],
    )
    with pytest.raises(RuntimeError, match="external recovery attempt limit"):
        store.retry_failed_after_external_recovery(
            record.job_id,
            "conversation-a",
            reviewed_routes=[route],
            review_basis="same_route_provider_smoke_passed",
        )

    exhausted = store.get(record.job_id)
    exhausted.external_recovery_attempt_count = 0
    exhausted.cost_used_units = exhausted.cost_budget_units
    store._write(exhausted)
    with pytest.raises(RuntimeError, match="cost budget"):
        store.retry_failed_after_external_recovery(
            record.job_id,
            "conversation-a",
            reviewed_routes=[route],
            review_basis="same_route_provider_smoke_passed",
        )


def test_external_model_recovery_cannot_bypass_authorization(tmp_path) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    authorization_route = failure_route_for_exception(
        PermissionDeniedException("approval is missing"),
        stage=JobStage.PLAN,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="approval is missing",
        recoverable=True,
        failure_routes=[authorization_route],
    )
    reviewed_route = PresentationFailureRoute(
        category=PresentationFailureCategory.MODEL_SERVICE,
        recovery_action=PresentationRecoveryAction.RETRY_MODEL_SERVICE,
        issue_codes=["access_terminated_error"],
        retry_stage=JobStage.PLAN,
        automatic_retry_allowed=True,
        requires_human=False,
    )

    with pytest.raises(ValueError, match="authorization"):
        store.retry_failed_after_external_recovery(
            record.job_id,
            "conversation-a",
            reviewed_routes=[reviewed_route],
            review_basis="same_route_provider_smoke_passed",
        )

    failed = store.get(record.job_id)
    assert failed.status == JobStatus.FAILED
    assert failed.external_recovery_attempt_count == 0


def test_verified_implementation_fix_reuses_page_checkpoints_without_reset(
    tmp_path,
) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    exhausted = store.get(record.job_id)
    exhausted.recovery_attempt_count = 3
    store._write(exhausted)
    page_checkpoint = store.write_checkpoint(
        record.job_id,
        "page-scene-001.json",
        '{"dependency_hash":"kept"}',
    )
    deck_checkpoint = store.write_checkpoint(
        record.job_id,
        "deck-ir.json",
        '{"id":"stale-composed-deck"}',
    )
    route = PresentationFailureRoute(
        category=PresentationFailureCategory.CONTENT,
        recovery_action=PresentationRecoveryAction.REVISE_CONTENT,
        issue_codes=["content.unsupported_number"],
        retry_stage=JobStage.COMPOSE,
        automatic_retry_allowed=False,
        requires_human=False,
    )
    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="page author introduced unsupported number(s): 95, 150",
        recoverable=True,
        failure_routes=[route],
    )

    event = store.retry_failed_after_implementation_fix(
        record.job_id,
        "conversation-a",
        reviewed_routes=[route],
        review_basis="numeric_contract_diff.v1",
        fix_identity="binary_subtraction_contract.v1",
    )
    retried = store.get(record.job_id)

    assert event.job_id == record.job_id
    assert event.stage == JobStage.COMPOSE
    assert page_checkpoint.is_file()
    assert not deck_checkpoint.exists()
    assert (
        store.job_dir(record.job_id)
        / "checkpoints"
        / "history"
        / "implementation-recovery-001-from-compose"
        / "deck-ir.json"
    ).read_text(encoding="utf-8") == '{"id":"stale-composed-deck"}'
    assert retried.recovery_attempt_count == 3
    assert retried.implementation_recovery_attempt_count == 1
    assert retried.implementation_recovery_fixes == [
        "binary_subtraction_contract.v1",
    ]
    assert len(store.list_records("conversation-a")) == 1

    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="same deterministic defect",
        recoverable=True,
        failure_routes=[route],
    )
    with pytest.raises(ValueError, match="same presentation implementation fix"):
        store.retry_failed_after_implementation_fix(
            record.job_id,
            "conversation-a",
            reviewed_routes=[route],
            review_basis="numeric_contract_diff.v1",
            fix_identity="binary_subtraction_contract.v1",
        )


def test_implementation_recovery_is_bounded_and_keeps_original_budget(
    tmp_path,
) -> None:
    store = PresentationJobStore(tmp_path)
    record = store.create(_request())
    route = PresentationFailureRoute(
        category=PresentationFailureCategory.CONTENT,
        recovery_action=PresentationRecoveryAction.REVISE_CONTENT,
        issue_codes=["content.contract_mismatch"],
        retry_stage=JobStage.COMPOSE,
        automatic_retry_allowed=False,
        requires_human=False,
    )

    for attempt in range(1, MAX_PRESENTATION_IMPLEMENTATION_RECOVERY_ATTEMPTS + 1):
        store.append_event(
            record.job_id,
            stage=JobStage.FAILED,
            status=JobStatus.FAILED,
            message="deterministic contract mismatch",
            recoverable=True,
            failure_routes=[route],
        )
        store.retry_failed_after_implementation_fix(
            record.job_id,
            "conversation-a",
            reviewed_routes=[route],
            review_basis=f"deterministic_contract_evidence.v{attempt}",
            fix_identity=f"deterministic_contract_fix.v{attempt}",
        )

    store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="another deterministic contract mismatch",
        recoverable=True,
        failure_routes=[route],
    )
    with pytest.raises(RuntimeError, match="implementation recovery attempt limit"):
        store.retry_failed_after_implementation_fix(
            record.job_id,
            "conversation-a",
            reviewed_routes=[route],
            review_basis="deterministic_contract_evidence.v3",
            fix_identity="deterministic_contract_fix.v3",
        )

    exhausted = store.get(record.job_id)
    exhausted.implementation_recovery_attempt_count = 0
    exhausted.cost_used_units = exhausted.cost_budget_units
    store._write(exhausted)
    with pytest.raises(RuntimeError, match="cost budget"):
        store.retry_failed_after_implementation_fix(
            record.job_id,
            "conversation-a",
            reviewed_routes=[route],
            review_basis="deterministic_contract_evidence.v3",
            fix_identity="deterministic_contract_fix.v3",
        )


def test_runtime_retry_submits_the_original_failed_job(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = PresentationJobRuntime(tmp_path)
    record = runtime.service.create_job(_request())
    route = failure_route_for_exception(
        ProviderError("provider unavailable"),
        stage=JobStage.QUALITY,
    )
    runtime.service.store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="provider unavailable",
        recoverable=True,
        failure_routes=[route],
    )
    submitted: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_submit",
        lambda job_id, **_kwargs: submitted.append(job_id),
    )

    event = runtime.retry_failed(record.job_id, "conversation-a")

    assert event.job_id == record.job_id
    assert submitted == [record.job_id]
    assert runtime.service.store.get(record.job_id).recovery_attempt_count == 1
    runtime.shutdown()


def test_runtime_reviewed_retry_submits_the_original_failed_job(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = PresentationJobRuntime(tmp_path)
    record = runtime.service.create_job(_request())
    persisted_route = PresentationFailureRoute(
        category=PresentationFailureCategory.UNKNOWN,
        recovery_action=PresentationRecoveryAction.INSPECT_FAILURE,
        issue_codes=["valueerror"],
        retry_stage=JobStage.COMPOSE,
        automatic_retry_allowed=False,
        requires_human=True,
    )
    runtime.service.store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="legacy generic failure",
        recoverable=True,
        failure_routes=[persisted_route],
    )
    reviewed_route = persisted_route.model_copy(update={
        "category": PresentationFailureCategory.CONTENT,
        "recovery_action": PresentationRecoveryAction.REVISE_CONTENT,
        "issue_codes": ["unsupported_number"],
        "requires_human": False,
    })
    submitted: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_submit",
        lambda job_id, **_kwargs: submitted.append(job_id),
    )

    event = runtime.retry_failed_after_review(
        record.job_id,
        "conversation-a",
        reviewed_routes=[reviewed_route],
        review_basis="runtime_stack_and_deterministic_grounding_gate",
    )

    assert event.job_id == record.job_id
    assert submitted == [record.job_id]
    assert runtime.service.store.get(record.job_id).recovery_attempt_count == 1
    runtime.shutdown()


def test_runtime_external_recovery_submits_the_original_failed_job(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = PresentationJobRuntime(tmp_path)
    record = runtime.service.create_job(_request())
    historical_route = PresentationFailureRoute(
        category=PresentationFailureCategory.UNKNOWN,
        recovery_action=PresentationRecoveryAction.INSPECT_FAILURE,
        issue_codes=["permissiondeniederror"],
        retry_stage=JobStage.PLAN,
        automatic_retry_allowed=False,
        requires_human=True,
    )
    runtime.service.store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="historical provider quota failure",
        recoverable=True,
        failure_routes=[historical_route],
    )
    reviewed_route = PresentationFailureRoute(
        category=PresentationFailureCategory.MODEL_SERVICE,
        recovery_action=PresentationRecoveryAction.RETRY_MODEL_SERVICE,
        issue_codes=["access_terminated_error"],
        retry_stage=JobStage.PLAN,
        automatic_retry_allowed=True,
        requires_human=False,
    )
    submitted: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_submit",
        lambda job_id, **_kwargs: submitted.append(job_id),
    )

    event = runtime.retry_failed_after_external_recovery(
        record.job_id,
        "conversation-a",
        reviewed_routes=[reviewed_route],
        review_basis="same_route_provider_smoke_passed",
        max_attempts=3,
    )

    assert event.job_id == record.job_id
    assert "（1/3）" in event.message
    assert submitted == [record.job_id]
    saved = runtime.service.store.get(record.job_id)
    assert saved.recovery_attempt_count == 0
    assert saved.external_recovery_attempt_count == 1
    runtime.shutdown()


def test_runtime_implementation_recovery_submits_original_failed_job(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = PresentationJobRuntime(tmp_path)
    record = runtime.service.create_job(_request())
    exhausted_default = runtime.service.store.get(record.job_id)
    exhausted_default.implementation_recovery_attempt_count = 2
    runtime.service.store._write(exhausted_default)
    route = PresentationFailureRoute(
        category=PresentationFailureCategory.CONTENT,
        recovery_action=PresentationRecoveryAction.REVISE_CONTENT,
        issue_codes=["content.unsupported_number"],
        retry_stage=JobStage.COMPOSE,
        automatic_retry_allowed=False,
        requires_human=False,
    )
    runtime.service.store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="page author introduced unsupported number(s): 95, 150",
        recoverable=True,
        failure_routes=[route],
    )
    submitted: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_submit",
        lambda job_id, **_kwargs: submitted.append(job_id),
    )

    event = runtime.retry_failed_after_implementation_fix(
        record.job_id,
        "conversation-a",
        reviewed_routes=[route],
        review_basis="numeric_contract_diff.v1",
        fix_identity="binary_subtraction_contract.v1",
        max_attempts=3,
    )

    assert event.job_id == record.job_id
    assert submitted == [record.job_id]
    saved = runtime.service.store.get(record.job_id)
    assert saved.implementation_recovery_attempt_count == 3
    runtime.shutdown()


def test_runtime_retry_queues_after_failed_worker_finishes(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = PresentationJobRuntime(tmp_path)
    record = runtime.service.create_job(_request())
    route = failure_route_for_exception(
        ProviderError("provider unavailable"),
        stage=JobStage.QUALITY,
    )
    runtime.service.store.append_event(
        record.job_id,
        stage=JobStage.FAILED,
        status=JobStatus.FAILED,
        message="provider unavailable",
        recoverable=True,
        failure_routes=[route],
    )
    finishing_worker: Future[None] = Future()
    runtime._futures[record.job_id] = finishing_worker
    submitted: list[tuple[object, tuple[object, ...]]] = []
    replacement: Future[None] = Future()

    def capture_submit(function, *args):
        submitted.append((function, args))
        return replacement

    monkeypatch.setattr(runtime._executor, "submit", capture_submit)

    runtime.retry_failed(record.job_id, "conversation-a")

    assert submitted == []
    finishing_worker.set_result(None)
    assert submitted == [(runtime._run, (record.job_id, False))]
    assert runtime.service.store.get(record.job_id).status == JobStatus.RUNNING
    runtime.shutdown()
