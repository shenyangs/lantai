"""Recovery keeps safe, page-scoped feedback through the public Runtime API."""

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


def _accepted_plan_case():
    request = PresentationJobRequest(
        conversation_id="synthetic-page-feedback-recovery",
        user_request="制作一页收入判断，不增添数据",
        slide_count=1,
        cost_budget_units=10000,
        materials=[InputMaterial(
            name="合成事实.md",
            content="甲方收入为42万元。\n风险尚待核验。",
        )],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    planned = plan.slides[0].model_copy(update={
        "title": "先核验证据，再判断收入",
        "charts": [],
        "intent": plan.slides[0].intent.model_copy(update={
            "required_facts": ["甲方收入为42万元。", "风险尚待核验。"],
            "immutable_facts": ["甲方收入为42万元。"],
        }),
    })
    return request, brief, plan.model_copy(update={"slides": [planned]})


def _incomplete_scene(refs):
    return {
        "objects": [
            {
                "kind": "text", "id": "title", "role": "title",
                "text_refs": ["storyboard.title"], "font_size": 36,
                "frame": {"x": 0.8, "y": 0.7, "w": 11.5, "h": 0.9},
            },
            {
                "kind": "text", "id": "evidence", "role": "body",
                "text_refs": refs, "font_size": 24,
                "frame": {"x": 0.8, "y": 2, "w": 11.5, "h": 3.8},
            },
        ],
        "speaker_notes": "", "object_groups": [], "relationships": [],
    }


def _resume_accepted_plan(runtime, request, brief, plan):
    # A previously accepted plan is fixture input. Admission, writers, budget,
    # author parsing, failure routing and recovery are all production behavior.
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
    raise AssertionError("synthetic public Runtime did not settle its failure")


def test_public_author_explains_numeric_json_encoding_without_rewriting_rejected_values(
    tmp_path, monkeypatch,
):
    calls = []
    rejected_marker = "private-wire-value-must-not-be-repeated"

    async def transport(**kwargs):
        calls.append(kwargs)
        scene = _incomplete_scene(["required_facts[0]", "required_facts[1]"])
        scene["objects"][1]["font_size"] = {
            "value": 24, "unit": "pt", "private": rejected_marker,
        }
        return SimpleNamespace(
            text=json.dumps(scene), model_label="synthetic/test",
        )

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        assert "JSON number" in calls[0]["system_prompt"]
        assert "不得附加 pt、px 或英寸单位" in calls[0]["system_prompt"]
        for call in calls[1:]:
            directive = json.loads(call["user_prompt"])["retry_directive"]
            assert "float_type@$.objects[1].font_size" in directive
            assert "JSON number" in directive
            assert "不得附加 pt、px 或英寸单位" in directive
            assert rejected_marker not in directive
        feedback = runtime.service.store.checkpoint_path(
            job_id, "page-author-feedback-001.json",
        ).read_text(encoding="utf-8")
        assert json.loads(feedback)["schema_issues"] == [
            "float_type@$.objects[1].font_size",
        ]
        assert rejected_marker not in feedback
        assert not list(runtime.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("defect, expected_code", [
    ("missing_kind", "union_tag_not_found"),
    ("non_object", "model_attributes_type"),
])
def test_public_author_explains_object_json_encoding_and_required_discriminator(
    tmp_path, monkeypatch, defect, expected_code,
):
    calls = []
    rejected_marker = "private-object-value-must-not-be-repeated"

    async def transport(**kwargs):
        calls.append(kwargs)
        scene = _incomplete_scene(["required_facts[0]", "required_facts[1]"])
        if defect == "missing_kind":
            scene["objects"][1].pop("kind")
            scene["objects"][1]["text"] = rejected_marker
        else:
            scene["objects"][1] = [rejected_marker]
        return SimpleNamespace(
            text=json.dumps(scene), model_label="synthetic/test",
        )

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        for guidance in [
            calls[0]["system_prompt"],
            *(json.loads(call["user_prompt"])["retry_directive"] for call in calls[1:]),
        ]:
            assert "每个条目必须是 JSON object" in guidance
            assert "每个条目都显式填写 kind" in guidance
            assert "native_scene_contract.object_types_by_kind" in guidance
            assert rejected_marker not in guidance
        for call in calls[1:]:
            assert (
                expected_code + "@$.objects[1]"
                in json.loads(call["user_prompt"])["retry_directive"]
            )
        feedback = runtime.service.store.checkpoint_path(
            job_id, "page-author-feedback-001.json",
        ).read_text(encoding="utf-8")
        assert json.loads(feedback)["schema_issues"] == [
            expected_code + "@$.objects[1]",
        ]
        assert rejected_marker not in feedback
        assert not list(runtime.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        runtime.shutdown()


def test_public_reviewed_recovery_keeps_feedback_including_terminal_attempt(
    tmp_path, monkeypatch,
):
    calls = []

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        # The terminal attempt introduces a different missing fact. It must be
        # carried too, even though no fourth in-generation retry is allowed.
        refs = ["required_facts[0]", "immutable_facts[0]"] if len(calls) == 3 else ["required_facts[1]"]
        return SimpleNamespace(
            text=json.dumps(_incomplete_scene(refs), ensure_ascii=False),
            model_label="synthetic/test",
        )

    # Only the external text transport is replaced; no Runtime/author helpers.
    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(tmp_path, page_author=UtilityPresentationPageAuthor("default"))
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        assert calls[0].get("retry_directive") is None
        assert "required_facts[0]" in calls[1]["retry_directive"]
        assert "required_facts[1]" not in calls[2]["retry_directive"]
        routes = failed.events[-1].failure_routes
        assert routes[0].issue_codes == ["content.required_visible_fact_omitted"]
        original_deadline = failed.deadline_at
        original_request = failed.request
        historical_events = failed.events
    finally:
        runtime.shutdown()

    restarted = PresentationJobRuntime(tmp_path, page_author=UtilityPresentationPageAuthor("default"))
    try:
        restarted.retry_failed_after_review(
            job_id, request.conversation_id, reviewed_routes=routes,
            review_basis="synthetic-missing-fact-feedback-review",
        )
        recovered = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == recovered.cost_used_units == 6
        assert recovered.deadline_at == original_deadline
        assert recovered.request == original_request
        assert recovered.events[:len(historical_events)] == historical_events
        assert recovered.recovery_attempt_count == 1
        directive = calls[3].get("retry_directive")
        assert directive is not None, "reviewed recovery discarded page feedback"
        assert all(path in directive for path in (
            "required_facts[0]", "immutable_facts[0]", "required_facts[1]",
        ))
        assert "甲方收入为42万元。" not in directive
        assert "风险尚待核验。" not in directive
        assert not list(restarted.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        restarted.shutdown()


@pytest.mark.parametrize("kind, fields", [
    ("text", {"role": "body", "text": "合成证据", "font_size": 24}),
    ("shape", {"shape_type": "rect"}),
    ("metric", {"label": "合成证据", "value": "待核验"}),
    ("table", {"headers": ["合成证据"], "rows": [["待核验"]]}),
    ("chart", {"chart_ref": "synthetic-same-page-chart"}),
    ("image", {"asset_id": "synthetic-asset", "alt": "合成证据"}),
])
def test_public_schema_feedback_uses_json_fields_not_union_branch_names(
    tmp_path, monkeypatch, kind, fields,
):
    calls = []

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        scene = _incomplete_scene([
            "required_facts[0]", "required_facts[1]", "immutable_facts[0]",
        ])
        scene["objects"].append({
            "kind": kind, "id": "synthetic-invalid-frame", **fields,
            "frame": {"x": 1, "y": 1, "w": 0, "h": 1},
        })
        return SimpleNamespace(
            text=json.dumps(scene, ensure_ascii=False), model_label="synthetic/test",
        )

    # Only replace the external model transport. Real parsing, schema validation,
    # feedback persistence and both public recovery APIs remain in the loop.
    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    canonical = "greater_than@$.objects[2].frame.w"
    branch_path = f"greater_than@$.objects[2].{kind}.frame.w"
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        directive = calls[1]["retry_directive"]
        assert canonical in directive
        assert branch_path not in directive
        original_deadline = failed.deadline_at
        original_request = failed.request
        history = failed.events
    finally:
        runtime.shutdown()

    restarted = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        restarted.retry_failed(job_id, request.conversation_id)
        failed = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 6
        assert failed.deadline_at == original_deadline
        assert failed.request == original_request
        assert failed.events[:len(history)] == history
        assert failed.recovery_attempt_count == 1
        directive = calls[3]["retry_directive"]
        assert canonical in directive
        assert branch_path not in directive
        assert not list(restarted.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        restarted.shutdown()


def test_public_schema_feedback_keeps_real_text_field_after_restart(
    tmp_path, monkeypatch,
):
    calls = []

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        scene = _incomplete_scene([
            "required_facts[0]", "required_facts[1]", "immutable_facts[0]",
        ])
        scene["objects"][1].pop("text_refs")
        scene["objects"][1]["text"] = ["synthetic-invalid-string"]
        return SimpleNamespace(
            text=json.dumps(scene, ensure_ascii=False), model_label="synthetic/test",
        )

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    canonical = "string_type@$.objects[1].text"
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        assert canonical in calls[1]["retry_directive"]
        assert canonical + ".text" not in calls[1]["retry_directive"]
        deadline = failed.deadline_at
        original_request = failed.request
        history = failed.events
    finally:
        runtime.shutdown()

    restarted = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        restarted.retry_failed(job_id, request.conversation_id)
        failed = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 6
        assert failed.deadline_at == deadline
        assert failed.request == original_request
        assert failed.events[:len(history)] == history
        assert failed.recovery_attempt_count == 1
        directive = calls[3]["retry_directive"]
        assert canonical in directive
        assert canonical + ".text" not in directive
        assert "string_type@$.objects[1]；" not in directive
        assert not list(restarted.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        restarted.shutdown()


@pytest.mark.parametrize(("defect", "code", "repair_hint"), [
    (
        "representation", "authored_text_representation",
        "text 或 text_refs 只能选一种",
    ),
    (
        "reference_runs", "authored_text_reference_runs",
        "选择 text_refs 时 runs 必须为空",
    ),
])
def test_public_text_representation_feedback_is_safe_bounded_and_survives_restart(
    tmp_path, monkeypatch, defect, code, repair_hint,
):
    calls = []
    private_marker = "SYNTHETIC_REJECTED_TEXT_REPRESENTATION_DO_NOT_ECHO"

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        scene = _incomplete_scene([
            "required_facts[0]", "required_facts[1]", "immutable_facts[0]",
        ])
        scene["objects"][1]["id"] = private_marker
        if defect == "representation":
            scene["objects"][1]["text"] = private_marker
        else:
            scene["objects"][1]["runs"] = [{
                "text": private_marker, "font_size": 24,
            }]
        return SimpleNamespace(
            text=json.dumps(scene, ensure_ascii=False), model_label="synthetic/test",
        )

    # Only replace the external text transport. The production schema, retry
    # budget, checkpoint writer and public recovery API remain in the loop.
    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    canonical = f"{code}@$.objects[1]"
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        directive = calls[1]["retry_directive"]
        assert canonical in directive
        assert repair_hint in directive
        assert canonical + ".text" not in directive
        assert private_marker not in json.dumps(calls)
        deadline = failed.deadline_at
        original_request = failed.request
        history = failed.events
    finally:
        runtime.shutdown()

    restarted = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        restarted.retry_failed(job_id, request.conversation_id)
        failed = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 6
        assert failed.deadline_at == deadline
        assert failed.request == original_request
        assert failed.events[:len(history)] == history
        assert failed.recovery_attempt_count == 1
        directive = calls[3]["retry_directive"]
        assert canonical in directive
        assert repair_hint in directive
        assert private_marker not in json.dumps(calls)
        checked = 0
        for path in restarted.service.store.job_dir(job_id).rglob("*"):
            if path.is_file() and path.suffix in {".json", ".jsonl", ".txt", ".md"}:
                assert private_marker not in path.read_text(encoding="utf-8"), path.name
                checked += 1
        assert checked > 0
        assert not list(restarted.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        restarted.shutdown()


def test_public_recovery_rebuilds_feedback_without_checkpoint_prompt_injection(
    tmp_path, monkeypatch,
):
    calls = []
    private_marker = "SYNTHETIC_UNTRUSTED_FEEDBACK_INSTRUCTION"

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        return SimpleNamespace(
            text=json.dumps(_incomplete_scene(["required_facts[1]"]), ensure_ascii=False),
            model_label="synthetic/test",
        )

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        routes = failed.events[-1].failure_routes
        feedback_path = runtime.service.store.checkpoint_path(
            job_id, "page-author-feedback-001.json",
        )
    finally:
        runtime.shutdown()

    payload = json.loads(feedback_path.read_text(encoding="utf-8"))
    payload.update({
        "parts": ["fact_visibility", "schema"],
        "retry_directive": private_marker,
        "missing_fact_paths": [
            "required_facts[0]", "required_facts[999999]",
            "immutable_facts[999999]", private_marker,
        ],
        "schema_issues": [
            "missing@$.objects[0].frame",
            "missing@$.objects[876543210987654321].frame",
            "extra_forbidden@$." + private_marker,
        ],
    })
    feedback_path.write_text(json.dumps(payload), encoding="utf-8")

    restarted = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        restarted.retry_failed_after_review(
            job_id, request.conversation_id, reviewed_routes=routes,
            review_basis="synthetic-untrusted-feedback-review",
        )
        failed = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 6
        directive = calls[3].get("retry_directive")
        assert directive is not None
        assert "required_facts[0]" in directive
        assert "missing@$.objects[0].frame" in directive
        assert all(marker not in directive for marker in (
            private_marker, "999999", "876543210987654321",
        ))
    finally:
        restarted.shutdown()


def test_public_reviewed_recovery_keeps_safe_schema_feedback_not_model_keys(
    tmp_path, monkeypatch,
):
    calls = []
    private_markers = (
        "SYNTHETIC_FEEDBACK_PRIVATE_KEY",
        "/private/synthetic/rejected-response",
        "876543210987654321",
    )

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        scene = _incomplete_scene([
            "required_facts[0]", "required_facts[1]", "immutable_facts[0]",
        ])
        # Both owned paths must survive, including the terminal new path.
        scene["objects"][1 if len(calls) == 3 else 0].pop("kind")
        scene[private_markers[0]] = " ".join(private_markers[1:])
        return SimpleNamespace(
            text=json.dumps(scene, ensure_ascii=False),
            model_label="synthetic/test",
        )

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        routes = failed.events[-1].failure_routes
        assert routes[0].issue_codes == ["structuredtaskvalidationerror"]
        original_deadline = failed.deadline_at
        history = failed.events
    finally:
        runtime.shutdown()

    restarted = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        restarted.retry_failed_after_review(
            job_id, request.conversation_id, reviewed_routes=routes,
            review_basis="synthetic-schema-feedback-review",
        )
        failed = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 6
        assert failed.deadline_at == original_deadline
        assert failed.events[:len(history)] == history
        directive = calls[3].get("retry_directive")
        assert directive is not None
        assert "union_tag_not_found@$.objects[0]" in directive
        assert "union_tag_not_found@$.objects[1]" in directive
        assert all(marker not in json.dumps(calls) for marker in private_markers)
        checked = 0
        for path in restarted.service.store.job_dir(job_id).rglob("*"):
            if path.is_file() and path.suffix in {".json", ".jsonl", ".txt", ".md"}:
                text = path.read_text(encoding="utf-8")
                assert all(marker not in text for marker in private_markers), path.name
                checked += 1
        assert checked > 0
        assert not list(restarted.service.store.job_dir(job_id).rglob("*.pptx"))
    finally:
        restarted.shutdown()


@pytest.mark.parametrize("corruption", [
    "dependency", "version", "json", "nonobject", "unknown_part",
    "nonstring_part", "oversized", "encoding",
])
def test_public_reviewed_recovery_ignores_untrusted_feedback_files(
    tmp_path, monkeypatch, corruption,
):
    calls = []

    async def transport(**kwargs):
        calls.append(json.loads(kwargs["user_prompt"]))
        return SimpleNamespace(
            text=json.dumps(_incomplete_scene(["required_facts[1]"]), ensure_ascii=False),
            model_label="synthetic/test",
        )

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    request, brief, plan = _accepted_plan_case()
    runtime = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        job_id = _resume_accepted_plan(runtime, request, brief, plan)
        failed = _settled_failure(runtime, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 3
        routes = failed.events[-1].failure_routes
        original_deadline = failed.deadline_at
        feedback_path = runtime.service.store.checkpoint_path(
            job_id, "page-author-feedback-001.json",
        )
    finally:
        runtime.shutdown()

    # Deliberate external corruption of a synthetic checkpoint, not a mocked
    # Runtime collaborator. Recovered calls remain the observable assertion.
    payload = json.loads(feedback_path.read_text(encoding="utf-8"))
    if corruption == "dependency":
        payload["dependency_sha256"] = "0" * 64
    elif corruption == "version":
        payload["schema_version"] = "unsupported"
    elif corruption == "nonobject":
        payload = []
    elif corruption == "unknown_part":
        payload["parts"] = ["UNTRUSTED_PROMPT_TEXT"]
    elif corruption == "nonstring_part":
        payload["parts"] = [{"untrusted": "instruction"}]
    serialized = json.dumps(payload)
    if corruption == "json":
        serialized = "{broken"
    elif corruption == "oversized":
        serialized = " " * 65537 + serialized
    feedback_path.write_bytes(
        b"\xff\xfe" if corruption == "encoding" else serialized.encode("utf-8"),
    )

    restarted = PresentationJobRuntime(
        tmp_path, page_author=UtilityPresentationPageAuthor("default"),
    )
    try:
        restarted.retry_failed_after_review(
            job_id, request.conversation_id, reviewed_routes=routes,
            review_basis="synthetic-corrupt-feedback-review",
        )
        failed = _settled_failure(restarted, job_id, request.conversation_id)
        assert len(calls) == failed.cost_used_units == 6
        assert failed.deadline_at == original_deadline
        assert failed.recovery_attempt_count == 1
        assert calls[3].get("retry_directive") is None
        assert "required_facts[0]" in calls[4]["retry_directive"]
        assert "UNTRUSTED_PROMPT_TEXT" not in json.dumps(calls)
    finally:
        restarted.shutdown()
