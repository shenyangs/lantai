# -*- coding: utf-8 -*-
"""Public job locks for bounded page-author scheduling; synthetic transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.app.utility_model import UtilityTaskTimeoutError
from lantai.exceptions import ModelExecutionException
from lantai.utils.operation_control import current_operation
from lantai.presentation.intelligence import UtilityPresentationPageAuthor
from lantai.presentation.models import (
    CompositionAuthority,
    DeckIR,
    InputMaterial,
    JobStatus,
    PresentationJobRequest,
)
from lantai.presentation.planner import build_deterministic_fixture_plan
from lantai.presentation.runtime import PresentationJobRuntime


class FixturePlanner:
    """Synthetic planner seam; the real author and native compiler still run."""

    def plan(self, request, brief, **_kwargs):
        return build_deterministic_fixture_plan(request, brief)


def synthetic_request(**updates):
    request = PresentationJobRequest(
        conversation_id="synthetic-page-author-batch",
        user_request="制作四页合成流程说明，严格保留原始事实。",
        slide_count=4,
        timeout_seconds=30,
        cost_budget_units=1000,
        visual_verification_required=False,
        vision_critique_required=False,
        materials=[InputMaterial(
            name="synthetic.md",
            content=(
                "# 合成流程\n\n## 立项\n核实用户给出的目标。\n\n"
                "## 设计\n保留来源中已经确认的事实。\n\n"
                "## 验证\n使用独立证据检查交付内容。\n\n"
                "## 总结\n汇总已经完成的工作。"
            ),
        )],
    )
    return request.model_copy(update=updates)


def scene_for(payload):
    storyboard = payload["storyboard"]
    intent = storyboard["intent"]
    return {
        "objects": [
            {
                "kind": "text", "id": "title", "role": "title",
                "text": storyboard["title"], "font_size": 36,
                "frame": {"x": 1, "y": 0.5, "w": 10, "h": 1},
            },
            {
                "kind": "text", "id": "body", "role": "body",
                "text": "\n".join(
                    intent["required_facts"] + intent["immutable_facts"],
                ),
                "font_size": 24,
                "frame": {"x": 1, "y": 2, "w": 10, "h": 4},
            },
        ],
    }


def wait_for_settled(runtime, record, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = runtime.get(record.job_id, record.conversation_id)
        if (
            current.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.PAUSED}
            and current.active_lease_id is None
            and not current.resource_settlement_pending
        ):
            return current
        time.sleep(0.01)
    raise AssertionError("public job did not settle its writer and resources")


def runtime_with_transport(tmp_path, monkeypatch, transport):
    monkeypatch.setattr("lantai.app.structured_utility.run_utility_text_task", transport)
    monkeypatch.setattr(
        "lantai.presentation.intelligence._utility_route_cache_identity",
        lambda _agent_id: "synthetic-provider/approved-route",
    )
    return PresentationJobRuntime(
        tmp_path,
        semantic_planner=FixturePlanner(),
        page_author=UtilityPresentationPageAuthor("synthetic-agent"),
    )


def test_public_page_transport_disconnect_recovers_inside_original_job(
    tmp_path, monkeypatch,
):
    calls = []
    private_error = "synthetic-private-transport-disconnect-sentinel"

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(payload)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError(private_error)
        return SimpleNamespace(
            text=json.dumps(scene_for(payload)),
            model_label="synthetic-provider/approved-route",
        )

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request(slide_count=1))
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.COMPLETED
        assert len(calls) == 2
        assert saved.job_id == record.job_id
        assert saved.generation == saved.attempt_count == 1
        assert saved.deadline_at == record.deadline_at
        metrics = [
            metric for metric in saved.stage_metrics
            if metric.operation == "author" and metric.slide_id
        ]
        assert [metric.outcome for metric in metrics] == ["failed", "passed"]
        assert sum(metric.cost_units for metric in metrics) == 2
        assert any(
            "模型服务连接中断；仅重试当前页（2/5）" in event.message
            and event.recoverable for event in saved.events
        )
        assert all("retry_directive" not in payload for payload in calls)
        assert private_error not in saved.model_dump_json()
        assert runtime.output_path(record.job_id, record.conversation_id).is_file()
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("cost_budget,expected_calls", [(1000, 3), (11, 1)])
def test_public_page_transport_disconnect_respects_retry_and_cost_caps(
    tmp_path, monkeypatch, cost_budget, expected_calls,
):
    calls = []
    private_error = "synthetic-private-repeated-disconnect-sentinel"

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        raise httpx.RemoteProtocolError(private_error)

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request(
            slide_count=1, cost_budget_units=cost_budget,
        ))
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.FAILED
        assert len(calls) == expected_calls
        assert saved.cost_used_units == 10 + expected_calls
        assert saved.cost_budget_units == record.cost_budget_units == cost_budget
        assert saved.deadline_at == record.deadline_at
        assert saved.generation == saved.attempt_count == 1
        assert saved.active_lease_id is None
        assert not saved.resource_settlement_pending
        assert private_error not in saved.model_dump_json()
        assert not runtime.service.store.checkpoint_path(
            record.job_id, "page-scene-001.json",
        ).exists()
        assert not list(tmp_path.rglob("presentation.pptx"))
        if cost_budget == 1000:
            route = saved.events[-1].failure_routes[0]
            assert route.issue_codes == ["remoteprotocolerror"]
            assert route.category.value == "model_service"
            assert len([
                event for event in saved.events if "模型服务连接中断" in event.message
            ]) == 2
    finally:
        runtime.shutdown()


def test_public_page_disconnect_does_not_reauthor_accepted_siblings(
    tmp_path, monkeypatch,
):
    calls = []

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        order = payload["storyboard"]["order"]
        calls.append(order)
        if order == 2 and calls.count(order) == 1:
            raise httpx.RemoteProtocolError("synthetic-private-page-two-sentinel")
        return SimpleNamespace(
            text=json.dumps(scene_for(payload)),
            model_label="synthetic-provider/approved-route",
        )

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request())
        saved = wait_for_settled(runtime, record)
        assert sorted(calls) == [1, 2, 2, 3, 4]
        assert saved.generation == saved.attempt_count == 1
        assert saved.deadline_at == record.deadline_at
        assert saved.active_lease_id is None
        assert not saved.resource_settlement_pending
        deck_path = runtime.service.store.checkpoint_path(record.job_id, "deck-ir.json")
        authored = DeckIR.model_validate_json(deck_path.read_text(encoding="utf-8"))
        assert [slide.id for slide in authored.slides] == [
            f"slide-{order:03d}" for order in range(1, 5)
        ]
        assert all(
            slide.composition_authority == CompositionAuthority.AUTHOR
            for slide in authored.slides
        )
        assert len([
            event for event in saved.events if "模型服务连接中断" in event.message
        ]) == 1
    finally:
        runtime.shutdown()


def test_public_stop_during_page_disconnect_does_not_admit_retry(
    tmp_path, monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()
    calls = []

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        entered.set()
        try:
            while not release.is_set():
                await asyncio.sleep(0.005)
            raise httpx.RemoteProtocolError("synthetic-private-stop-disconnect-sentinel")
        finally:
            cleaned.set()

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request(slide_count=1))
        assert entered.wait(5), "synthetic transport did not start"
        runtime.cancel(record.job_id, record.conversation_id)
        release.set()
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.CANCELLED
        assert len(calls) == 1
        assert cleaned.is_set()
        assert saved.cost_used_units == 11
        assert saved.generation == saved.attempt_count == 1
        assert saved.deadline_at == record.deadline_at
        assert not any("模型服务连接中断" in event.message for event in saved.events)
        assert not list(tmp_path.rglob("presentation.pptx"))
    finally:
        release.set()
        runtime.shutdown()


def test_public_page_schema_feedback_survives_a_transport_disconnect(
    tmp_path, monkeypatch,
):
    calls = []

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(payload)
        if len(calls) == 2:
            raise httpx.RemoteProtocolError("synthetic-private-feedback-disconnect-sentinel")
        scene = scene_for(payload)
        if len(calls) == 1:
            scene["objects"][0]["text_refs"] = ["storyboard.title"]
        return SimpleNamespace(
            text=json.dumps(scene), model_label="synthetic-provider/approved-route",
        )

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request(slide_count=1))
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.COMPLETED
        assert len(calls) == 3
        assert "retry_directive" not in calls[0]
        assert calls[1]["retry_directive"] == calls[2]["retry_directive"]
        assert "authored_text_representation@$.objects[0]" in calls[2]["retry_directive"]
        assert "text 或 text_refs 只能选一种" in calls[2]["retry_directive"]
        assert saved.generation == saved.attempt_count == 1
        assert saved.deadline_at == record.deadline_at
    finally:
        runtime.shutdown()


def test_public_page_mixed_failures_keep_the_total_five_attempt_ceiling(
    tmp_path, monkeypatch,
):
    calls = []
    failures = [
        httpx.RemoteProtocolError("synthetic-private-mixed-disconnect-sentinel"),
        StructuredTaskValidationError(
            "synthetic-private-schema-sentinel", validation_issues=("missing@$.objects[0].frame",),
        ),
        UtilityTaskTimeoutError(phase="idle", timeout_seconds=1),
        httpx.RemoteProtocolError("synthetic-private-mixed-disconnect-sentinel"),
        StructuredTaskValidationError(
            "synthetic-private-schema-sentinel", validation_issues=("missing@$.objects[0].frame",),
        ),
    ]

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(payload)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return SimpleNamespace(
            text=json.dumps(scene_for(payload)), model_label="synthetic-provider/approved-route",
        )

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request(slide_count=1))
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.FAILED
        assert len(calls) == 5
        assert saved.cost_used_units == 15
        assert saved.generation == saved.attempt_count == 1
        assert saved.deadline_at == record.deadline_at
        assert len([event for event in saved.events if "仅重试当前页" in event.message]) == 4
        assert "synthetic-private-" not in saved.model_dump_json()
        assert not list(tmp_path.rglob("presentation.pptx"))
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("error_type", ["permission_error", "access_terminated_error"])
def test_public_page_account_or_permission_failure_is_not_a_disconnect_retry(
    tmp_path, monkeypatch, error_type,
):
    calls = []
    error = openai.PermissionDeniedError(
        "synthetic-private-permission-sentinel",
        response=httpx.Response(403, request=httpx.Request("POST", "https://provider.invalid/v1/chat")),
        body={"error": {
            "type": error_type,
            "message": "synthetic-private-permission-sentinel" if error_type == "permission_error" else
            "weekly usage limit will reset synthetic-private-permission-sentinel",
        }},
    )

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        raise error

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request(slide_count=1))
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.FAILED
        assert len(calls) == 1
        assert saved.cost_used_units == 11
        route = saved.events[-1].failure_routes[0]
        assert route.requires_human is True
        assert route.automatic_retry_allowed is False
        assert not any("模型服务连接中断" in event.message for event in saved.events)
        assert "synthetic-private-permission-sentinel" not in saved.model_dump_json()
        assert not list(tmp_path.rglob("presentation.pptx"))
    finally:
        runtime.shutdown()


def test_public_page_disconnect_cannot_extend_the_original_deadline(
    tmp_path, monkeypatch,
):
    calls = []
    cleaned = threading.Event()

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        try:
            await asyncio.sleep(30.1)
            raise httpx.RemoteProtocolError("synthetic-private-expired-disconnect-sentinel")
        finally:
            cleaned.set()

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request(slide_count=1, timeout_seconds=30))
        saved = wait_for_settled(runtime, record, timeout=35)
        assert saved.status == JobStatus.FAILED
        assert len(calls) == 1
        assert cleaned.is_set()
        assert saved.cost_used_units == 11
        assert saved.deadline_at == record.deadline_at
        assert saved.generation == saved.attempt_count == 1
        assert not list(tmp_path.rglob("presentation.pptx"))
    finally:
        runtime.shutdown()


def test_public_model_job_overlaps_three_authors_and_assembles_in_page_order(
    tmp_path, monkeypatch,
):
    lock = threading.Lock()
    first_batch_entered = threading.Event()
    entered, finished, stalled = [], [], []
    active = peak = 0

    async def transport(**kwargs):
        nonlocal active, peak
        payload = json.loads(kwargs["user_prompt"])
        order = payload["storyboard"]["order"]
        with lock:
            entered.append(order)
            active += 1
            peak = max(peak, active)
            if len(entered) >= 3:
                first_batch_entered.set()
        try:
            deadline = time.monotonic() + 1
            while not first_batch_entered.is_set():
                if time.monotonic() >= deadline:
                    stalled.append(order)
                    raise RuntimeError("three independent authors did not overlap")
                await asyncio.sleep(0.005)
            await asyncio.sleep({1: 0.09, 2: 0.06, 3: 0.03}.get(order, 0.01))
            with lock:
                finished.append(order)
            return SimpleNamespace(
                text=json.dumps(scene_for(payload)),
                model_label=f"synthetic-provider/page-{order}",
            )
        finally:
            with lock:
                active -= 1

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request())
        saved = wait_for_settled(runtime, record)
        assert not stalled, "page authors are still serialized"
        assert peak == 3 and active == 0
        assert sorted(entered) == [1, 2, 3, 4]
        assert finished != [1, 2, 3, 4], "completion order was not challenged"
        deck_path = runtime.service.store.checkpoint_path(record.job_id, "deck-ir.json")
        authored = DeckIR.model_validate_json(deck_path.read_text(encoding="utf-8"))
        assert [slide.id for slide in authored.slides] == [
            f"slide-{order:03d}" for order in range(1, 5)
        ]
        assert all(
            slide.composition_authority == CompositionAuthority.AUTHOR
            for slide in authored.slides
        )
        metrics = [metric for metric in saved.stage_metrics if metric.operation == "author" and metric.slide_id]
        assert len(metrics) == 4 and all(metric.cost_units == 1 for metric in metrics)
        for order, slide in enumerate(authored.slides, 1):
            path = runtime.service.store.checkpoint_path(record.job_id, f"page-scene-{order:03d}.json")
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            assert checkpoint["slide"]["id"] == slide.id
            assert checkpoint["model_route"] == f"synthetic-provider/page-{order}"
            metric = next(metric for metric in metrics if metric.slide_id == slide.id)
            assert metric.route_id == checkpoint["model_route"]
    finally:
        runtime.shutdown()


def test_one_author_failure_cancels_and_joins_siblings_without_new_admission(
    tmp_path, monkeypatch,
):
    lock = threading.Lock()
    entered, cleaning = [], []
    all_entered, all_cleaning, release_cleanup = (
        threading.Event(), threading.Event(), threading.Event()
    )
    private_failure = "synthetic-private-author-failure-sentinel"

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        order = payload["storyboard"]["order"]
        control = current_operation()
        assert control is not None
        control.resource_started()
        with lock:
            entered.append(order)
            if len(entered) == 3:
                all_entered.set()
        try:
            while not all_entered.is_set():
                await asyncio.sleep(0.005)
            if order == 1:
                raise ModelExecutionException(
                    "synthetic-provider/page-1",
                    details={"response": private_failure},
                )
            while True:
                await asyncio.sleep(0.01)
        finally:
            if order != 1:
                with lock:
                    cleaning.append(order)
                    if len(cleaning) == 2:
                        all_cleaning.set()
                while not release_cleanup.is_set():
                    await asyncio.sleep(0.01)
            control.resource_settled(True)

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request())
        assert all_cleaning.wait(3)
        during_cleanup = runtime.get(record.job_id, record.conversation_id)
        assert during_cleanup.active_lease_id is not None
        assert during_cleanup.status == JobStatus.RUNNING
        assert sorted(entered) == [1, 2, 3]
        release_cleanup.set()
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.FAILED, "a sibling cancellation hid the real author failure"
        assert saved.events[-1].failure_routes[0].category.value == "model_service"
        assert saved.cost_used_units == 13
        assert private_failure not in saved.model_dump_json()
        assert not runtime.service.store.checkpoint_path(record.job_id, "deck-ir.json").exists()
        assert not list(tmp_path.rglob("presentation.pptx"))
    finally:
        release_cleanup.set()
        runtime.shutdown()


def test_parallel_author_admission_never_overruns_the_original_cost_cap(
    tmp_path, monkeypatch,
):
    entered, cleaned = [], []

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        order = payload["storyboard"]["order"]
        entered.append(order)
        try:
            while True:
                await asyncio.sleep(0.01)
        finally:
            cleaned.append(order)

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request(cost_budget_units=12))
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.FAILED
        assert saved.cost_used_units == saved.cost_budget_units == 12
        assert len(entered) <= 2 and sorted(cleaned) == sorted(entered)
        assert saved.deadline_at == record.deadline_at
        assert saved.attempt_count == 1 and saved.generation == 1
        assert not runtime.service.store.checkpoint_path(record.job_id, "deck-ir.json").exists()
        assert not list(tmp_path.rglob("presentation.pptx"))
    finally:
        runtime.shutdown()


def test_public_pause_resume_reuses_accepted_page_and_original_deadline(
    tmp_path, monkeypatch,
):
    calls = []
    first_batch_entered = threading.Event()
    resuming = threading.Event()
    lock = threading.Lock()

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        order = payload["storyboard"]["order"]
        with lock:
            calls.append(order)
            if len(calls) == 3:
                first_batch_entered.set()
        if not resuming.is_set():
            while not first_batch_entered.is_set():
                await asyncio.sleep(0.005)
            if order != 1:
                while True:
                    await asyncio.sleep(0.01)
        return SimpleNamespace(
            text=json.dumps(scene_for(payload)),
            model_label=f"synthetic-provider/page-{order}",
        )

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request())
        accepted = runtime.service.store.checkpoint_path(record.job_id, "page-scene-001.json")
        deadline = time.monotonic() + 3
        while not accepted.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert accepted.exists()
        original_sha256 = hashlib.sha256(accepted.read_bytes()).hexdigest()
        runtime.pause(record.job_id, record.conversation_id)
        paused = wait_for_settled(runtime, record)
        assert paused.status == JobStatus.PAUSED
        assert calls.count(1) == 1
        assert not runtime.service.store.checkpoint_path(record.job_id, "deck-ir.json").exists()
        resuming.set()
        runtime.resume(record.job_id, record.conversation_id)
        saved = wait_for_settled(runtime, record)
        assert saved.deadline_at == record.deadline_at == paused.deadline_at
        assert saved.cost_budget_units == paused.cost_budget_units == 1000
        assert hashlib.sha256(accepted.read_bytes()).hexdigest() == original_sha256
        assert calls.count(1) == 1
        assert all(calls.count(order) >= 1 for order in range(2, 5))
        deck_path = runtime.service.store.checkpoint_path(record.job_id, "deck-ir.json")
        deck = DeckIR.model_validate_json(deck_path.read_text(encoding="utf-8"))
        assert [slide.id for slide in deck.slides] == [f"slide-{order:03d}" for order in range(1, 5)]
        author_metrics = [
            metric for metric in saved.stage_metrics
            if metric.operation == "author" and metric.slide_id
        ]
        # Cost units are reservations, not API billing: Stop may happen
        # between admission and the transport's first turn.
        assert sum(metric.cost_units for metric in author_metrics) >= len(calls)
        assert sorted(
            metric.slide_id for metric in author_metrics if metric.outcome == "passed"
        ) == [slide.id for slide in deck.slides]
    finally:
        resuming.set()
        runtime.shutdown()


def test_public_stop_joins_all_author_cleanup_before_retiring_the_writer(
    tmp_path, monkeypatch,
):
    lock = threading.Lock()
    entered, cleaning, controls = [], [], []
    all_entered, all_cleaning, release_cleanup = (
        threading.Event(), threading.Event(), threading.Event()
    )

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        order = payload["storyboard"]["order"]
        control = current_operation()
        assert control is not None
        assert kwargs["usage_session_id"] == "synthetic-page-author-batch"
        assert 0 < kwargs["timeout_seconds"] <= 30
        control.resource_started()
        with lock:
            entered.append(order)
            controls.append(control)
            if len(entered) == 3:
                all_entered.set()
        try:
            while True:
                await asyncio.sleep(0.01)
        finally:
            with lock:
                cleaning.append(order)
                if len(cleaning) == 3:
                    all_cleaning.set()
            while not release_cleanup.is_set():
                await asyncio.sleep(0.01)
            control.resource_settled(True)

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request())
        assert all_entered.wait(3)
        admitted = runtime.get(record.job_id, record.conversation_id)
        runtime.cancel(record.job_id, record.conversation_id)
        assert all_cleaning.wait(3)
        runtime.cancel(record.job_id, record.conversation_id)
        during_cleanup = runtime.get(record.job_id, record.conversation_id)
        assert during_cleanup.status == JobStatus.CANCELLED
        assert during_cleanup.active_lease_id == admitted.active_lease_id
        assert len(entered) == 3 and sorted(cleaning) == [1, 2, 3]
        assert len({control.identity for control in controls}) == 1
        assert len({id(control.resources) for control in controls}) == 1
        assert all(control.unsettled.is_set() for control in controls)
        release_cleanup.set()
        saved = wait_for_settled(runtime, record)
        assert saved.status == JobStatus.CANCELLED
        assert saved.cost_used_units == admitted.cost_used_units == 13
        assert saved.deadline_at == admitted.deadline_at
        assert saved.attempt_count == admitted.attempt_count == 1
        assert saved.generation == admitted.generation == 1
        assert all(not control.unsettled.is_set() for control in controls)
        assert not runtime.service.store.checkpoint_path(record.job_id, "deck-ir.json").exists()
        assert not list(tmp_path.rglob("presentation.pptx"))
    finally:
        release_cleanup.set()
        runtime.shutdown()


def test_overridden_utility_author_stays_serial_without_explicit_opt_in(
    tmp_path, monkeypatch,
):
    lock = threading.Lock()
    active = peak = 0
    calls = []

    class CustomAuthor(UtilityPresentationPageAuthor):
        def author(self, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                return super().author(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(payload["storyboard"]["order"])
        return SimpleNamespace(
            text=json.dumps(scene_for(payload)),
            model_label="synthetic-provider/custom",
        )

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    runtime.page_author = CustomAuthor("synthetic-agent")
    try:
        record = runtime.create_and_start(synthetic_request())
        wait_for_settled(runtime, record)
        assert peak == 1 and active == 0, "custom mutable author was made concurrent implicitly"
        assert calls == [1, 2, 3, 4]
    finally:
        runtime.shutdown()


def test_parallel_author_retries_only_the_rejected_page_with_owned_feedback(
    tmp_path, monkeypatch,
):
    lock = threading.Lock()
    payloads = {}

    async def transport(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        order = payload["storyboard"]["order"]
        with lock:
            attempts = payloads.setdefault(order, [])
            attempts.append(payload)
            attempt = len(attempts)
        scene = scene_for(payload)
        if order == 2 and attempt == 1:
            scene["objects"] = scene["objects"][:1]
        await asyncio.sleep(0.01)
        return SimpleNamespace(
            text=json.dumps(scene), model_label=f"synthetic-provider/page-{order}",
        )

    runtime = runtime_with_transport(tmp_path, monkeypatch, transport)
    try:
        record = runtime.create_and_start(synthetic_request())
        saved = wait_for_settled(runtime, record)
        assert {order: len(attempts) for order, attempts in payloads.items()} == {
            1: 1, 2: 2, 3: 1, 4: 1,
        }
        assert "retry_directive" not in payloads[2][0]
        assert "required_facts[0]" in payloads[2][1]["retry_directive"]
        assert all(
            "retry_directive" not in payloads[order][0] for order in (1, 3, 4)
        )
        metrics = [metric for metric in saved.stage_metrics if metric.operation == "author" and metric.slide_id]
        assert sum(metric.cost_units for metric in metrics) == 5
        assert sum(metric.outcome == "passed" for metric in metrics) == 4
        assert sum(metric.outcome == "failed" for metric in metrics) == 1
        assert runtime.service.store.checkpoint_path(record.job_id, "page-author-feedback-002.json").is_file()
        deck_path = runtime.service.store.checkpoint_path(record.job_id, "deck-ir.json")
        deck = DeckIR.model_validate_json(deck_path.read_text(encoding="utf-8"))
        assert [slide.id for slide in deck.slides] == [f"slide-{order:03d}" for order in range(1, 5)]
    finally:
        runtime.shutdown()
