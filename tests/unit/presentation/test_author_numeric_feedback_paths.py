"""Synthetic public-runtime lock: numeric repair gets paths, never values."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import lantai.app.structured_utility as structured
from lantai.presentation.intelligence import UtilityPresentationPageAuthor
from lantai.presentation.models import InputMaterial, JobStatus, PresentationJobRequest
from lantai.presentation.planner import build_brief, build_deterministic_fixture_plan
from lantai.presentation.runtime import PresentationJobRuntime


def _accepted_case():
    request = PresentationJobRequest(
        conversation_id="synthetic-numeric-feedback-paths",
        user_request="制作一页证据判断，不增添数字",
        slide_count=1,
        cost_budget_units=10000,
        materials=[InputMaterial(
            name="合成来源.md",
            content="甲方收入为42万元。\n风险尚待核验。",
        )],
    )
    brief = build_brief(request)
    original_plan = build_deterministic_fixture_plan(request, brief)
    planned = original_plan.slides[0].model_copy(update={
        "title": "先核验证据，再判断收入",
        "charts": [],
        "intent": original_plan.slides[0].intent.model_copy(update={
            "required_facts": ["甲方收入为42万元。", "风险尚待核验。"],
            "immutable_facts": ["甲方收入为42万元。"],
        }),
    })
    plan = original_plan.model_copy(update={"slides": [planned]})
    return request, brief, plan


def _scene(extras):
    return {
        "objects": [
            {
                "kind": "text", "id": "title", "role": "title",
                "text_refs": ["storyboard.title"], "font_size": 36,
                "frame": {"x": 0.8, "y": 0.7, "w": 11.5, "h": 0.9},
            },
            {
                "kind": "text", "id": "evidence", "role": "body",
                "text_refs": ["required_facts[0]", "required_facts[1]"],
                "font_size": 24,
                "frame": {"x": 0.8, "y": 2, "w": 11.5, "h": 2.8},
            },
            *extras,
        ],
        "speaker_notes": "", "object_groups": [], "relationships": [],
    }


def _admit(runtime, request, brief, plan):
    record = runtime.service.create_job(request)
    runtime.service.store.write_checkpoint(record.job_id, "brief.json", brief.model_dump_json())
    runtime.service.store.write_checkpoint(record.job_id, "deck-plan.json", plan.model_dump_json())
    runtime.pause(record.job_id, request.conversation_id)
    runtime.resume(record.job_id, request.conversation_id)
    return record.job_id


def _settled_failure(runtime, job_id, conversation_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        record = runtime.get(job_id, conversation_id)
        if record.status == JobStatus.FAILED and record.active_lease_id is None:
            assert not record.resource_settlement_pending
            return record
        time.sleep(0.01)
    raise AssertionError("public Runtime did not settle the synthetic failure")


@pytest.mark.parametrize("object_kind,field,expected_path", [
    ("text", "text", "$.objects[2].text"),
    ("text", "runs", "$.objects[2].runs[0].text"),
    ("text", "decorative", "$.objects[2].text"),
    ("shape", "text", "$.objects[2].text"),
    ("metric", "label", "$.objects[2].label"),
    ("metric", "value", "$.objects[2].value"),
    ("metric", "detail", "$.objects[2].detail"),
    ("table", "headers", "$.objects[2].headers[0]"),
    ("table", "rows", "$.objects[2].rows[0][0]"),
])
def test_repeated_numeric_failure_explains_only_its_safe_object_path(
    tmp_path, monkeypatch, object_kind, field, expected_path,
):
    calls = []
    rejected_marker = "private-rejected-numeric-marker"
    request, brief, plan = _accepted_case()

    async def transport(**kwargs):
        calls.append(kwargs)
        rejected = {"kind": object_kind, "id": "extra",
                    "frame": {"x": 0.8, "y": 5.4, "w": 11.5, "h": 0.9}}
        value = rejected_marker + "：9999万元"
        if object_kind in {"text", "shape"}:
            rejected.update({"text": "待核验", "font_size": 24})
            if object_kind == "text":
                rejected["role"] = "body"
            if field == "runs":
                rejected["runs"] = [{"text": value, "font_size": 24}]
            else:
                rejected["text"] = value
            if field == "decorative":
                rejected["slot"] = "page-number"
        elif object_kind == "metric":
            rejected.update({"label": "收入", "value": "待核验", "detail": "待核验"})
            rejected[field] = value
        else:
            rejected.update({"headers": ["收入"], "rows": [["待核验"]]})
            if field == "headers":
                rejected["headers"][0] = value
            else:
                rejected["rows"][0][0] = value
        return SimpleNamespace(text=json.dumps(_scene([rejected])), model_label="synthetic/test")

    # Only the external text transport is replaced. Admission, budget, schema,
    # content validation, bounded retry, and failure settlement remain real.
    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        job_id = _admit(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        assert failed.generation == failed.attempt_count == 1
        assert failed.recovery_attempt_count == 0
        assert not list(runtime.service.store.job_dir(job_id).rglob("*.pptx"))
        for call in calls[1:]:
            directive = json.loads(call["user_prompt"])["retry_directive"]
            assert expected_path in directive
            assert "page_numeric_contract.allowed_numeric_tokens" in directive
            assert rejected_marker not in directive
            assert "9999" not in directive
            assert "甲方收入为42万元。" not in directive
        feedback = runtime.service.store.checkpoint_path(
            job_id, "page-author-feedback-001.json",
        ).read_text(encoding="utf-8")
        assert json.loads(feedback)["numeric_object_paths"] == [expected_path]
        assert rejected_marker not in feedback
        assert "9999" not in feedback
    finally:
        runtime.shutdown()


def _invalid_metric(fields=("value",)):
    metric = {
        "kind": "metric", "id": "private-numeric-object-id",
        "label": "待核验", "value": "待核验", "detail": "待核验",
        "frame": {"x": 0.8, "y": 5.4, "w": 11.5, "h": 0.9},
    }
    for field in fields:
        metric[field] = "private-numeric-field-content：9999万元"
    return metric


def test_public_numeric_recovery_keeps_all_paths_including_terminal_failure(
    tmp_path, monkeypatch,
):
    calls = []

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        field = ("label", "value", "detail")[(len(calls) - 1) % 3]
        return SimpleNamespace(
            text=json.dumps(_scene([_invalid_metric((field,))])),
            model_label="synthetic/test",
        )

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_case()
    runtime = PresentationJobRuntime(tmp_path, page_author=UtilityPresentationPageAuthor("default"))
    try:
        job_id = _admit(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        assert "$.objects[2].label" in calls[1]["retry_directive"]
        assert "$.objects[2].value" in calls[2]["retry_directive"]
        routes = failed.events[-1].failure_routes
        deadline, original_request, history = failed.deadline_at, failed.request, failed.events
    finally:
        runtime.shutdown()

    restarted = PresentationJobRuntime(tmp_path, page_author=UtilityPresentationPageAuthor("default"))
    try:
        restarted.retry_failed_after_review(
            job_id, request.conversation_id, reviewed_routes=routes,
            review_basis="synthetic-numeric-path-review",
        )
        failed = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 6
        assert failed.deadline_at == deadline
        assert failed.request == original_request
        assert failed.events[:len(history)] == history
        assert failed.generation == 2 and failed.recovery_attempt_count == 1
        directive = calls[3]["retry_directive"]
        assert all("$.objects[2]." + field in directive for field in ("label", "value", "detail"))
        assert "9999" not in directive and "private-numeric" not in directive
        feedback = restarted.service.store.checkpoint_path(job_id, "page-author-feedback-001.json")
        payload = json.loads(feedback.read_text(encoding="utf-8"))
        assert payload["numeric_object_paths"] == [
            "$.objects[2].label", "$.objects[2].value", "$.objects[2].detail",
        ]
        assert "private-numeric" not in feedback.read_text(encoding="utf-8")
        assert not list(restarted.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        restarted.shutdown()


@pytest.mark.parametrize("checkpoint_kind", [
    "legacy", "untrusted_paths", "nonlist", "bounded_paths", "stale", "oversized",
])
def test_public_numeric_recovery_filters_checkpoint_paths_and_owns_instructions(
    tmp_path, monkeypatch, checkpoint_kind,
):
    calls = []
    injection = "SYNTHETIC_UNTRUSTED_NUMERIC_INSTRUCTION"

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        return SimpleNamespace(
            text=json.dumps(_scene([_invalid_metric()])), model_label="synthetic/test",
        )

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_case()
    runtime = PresentationJobRuntime(tmp_path, page_author=UtilityPresentationPageAuthor("default"))
    try:
        job_id = _admit(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        routes = failed.events[-1].failure_routes
        deadline = failed.deadline_at
        feedback = runtime.service.store.checkpoint_path(job_id, "page-author-feedback-001.json")
    finally:
        runtime.shutdown()

    payload = json.loads(feedback.read_text(encoding="utf-8"))
    payload["retry_directive"] = injection
    if checkpoint_kind == "legacy":
        payload.pop("numeric_object_paths")
    elif checkpoint_kind == "untrusted_paths":
        payload["numeric_object_paths"] = [
            "$.objects[2].value", "$.objects[80].text", "$.objects[-1].text",
            "$.objects[02].text", "$.objects[2].font_size", "$.objects[2].id",
            "$.objects[2].headers[8]", "$.objects[2].rows[20][0]",
            "$.objects[2].rows[0][8]", "$.objects[2].runs[1000000].text",
            "$.objects[2].text." + injection, "$.objects[2].value", 123, {"text": injection},
        ]
    elif checkpoint_kind == "nonlist":
        payload["numeric_object_paths"] = {"instruction": injection}
    elif checkpoint_kind == "bounded_paths":
        payload["numeric_object_paths"] = [f"$.objects[{index}].text" for index in range(20)]
    elif checkpoint_kind == "stale":
        payload["dependency_sha256"] = "0" * 64
    serialized = json.dumps(payload)
    if checkpoint_kind == "oversized":
        serialized = " " * 65537 + serialized
    # External synthetic checkpoint corruption; no Runtime collaborator is mocked.
    feedback.write_text(serialized, encoding="utf-8")

    restarted = PresentationJobRuntime(tmp_path, page_author=UtilityPresentationPageAuthor("default"))
    try:
        restarted.retry_failed_after_review(
            job_id, request.conversation_id, reviewed_routes=routes,
            review_basis="synthetic-numeric-checkpoint-review",
        )
        failed = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 6
        assert failed.deadline_at == deadline and failed.recovery_attempt_count == 1
        directive = calls[3].get("retry_directive")
        if checkpoint_kind in {"stale", "oversized"}:
            assert directive is None
        else:
            assert "page_numeric_contract.allowed_numeric_tokens" in directive
            assert injection not in directive and "private-numeric" not in directive
            if checkpoint_kind in {"legacy", "nonlist"}:
                assert "$.objects[" not in directive
            elif checkpoint_kind == "untrusted_paths":
                assert "$.objects[2].value" in directive
                assert all(path not in directive for path in payload["numeric_object_paths"][1:11])
            else:
                assert all(f"$.objects[{index}].text" in directive for index in range(16))
                assert all(f"$.objects[{index}].text" not in directive for index in range(16, 20))
        assert injection not in json.dumps(calls)
        stored = json.loads(feedback.read_text(encoding="utf-8"))
        assert len(stored["numeric_object_paths"]) <= 16
        assert injection not in json.dumps(stored)
        assert not list(restarted.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        restarted.shutdown()


def test_public_numeric_path_collection_is_bounded_and_excludes_supported_values(
    tmp_path, monkeypatch,
):
    calls = []

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        extras = [_invalid_metric(("label", "value", "detail")) for _ in range(20)]
        for index, obj in enumerate(extras):
            obj["id"] += str(index)
        return SimpleNamespace(text=json.dumps(_scene(extras)), model_label="synthetic/test")

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_case()
    runtime = PresentationJobRuntime(tmp_path, page_author=UtilityPresentationPageAuthor("default"))
    try:
        job_id = _admit(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        feedback = runtime.service.store.checkpoint_path(job_id, "page-author-feedback-001.json")
        payload = json.loads(feedback.read_text(encoding="utf-8"))
        expected = [f"$.objects[{index}].{field}" for index in range(2, 22)
                    for field in ("label", "value", "detail")][:16]
        assert payload["numeric_object_paths"] == expected
        for call in calls[1:]:
            assert all(path in call["retry_directive"] for path in expected)
            assert "$.objects[0]" not in call["retry_directive"]
            assert "$.objects[1]" not in call["retry_directive"]
            assert "9999" not in call["retry_directive"]
        assert not list(runtime.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        runtime.shutdown()
