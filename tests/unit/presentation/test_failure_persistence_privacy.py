"""Actual Runtime failure paths never persist rejected model keys or values."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import lantai.app.structured_utility as structured
import lantai.presentation.intelligence as intelligence
from lantai.app.utility_model import UtilityTaskTimeoutError
from lantai.presentation.failure import failure_message_for_exception, failure_route_for_exception
from lantai.presentation.models import InputMaterial, JobStage, JobStatus, PresentationJobRequest
from lantai.presentation.page_authoring import (
    PresentationPageAuthorContentError, PresentationPageAuthorSceneError,
    PresentationSemanticContentError,
)
from lantai.presentation.planner import build_brief, build_deterministic_fixture_plan
from lantai.presentation.runtime import PresentationJobRuntime


PRIVATE_KEY = "SYNTHETIC_MODEL_PRIVATE_KEY"
PRIVATE_NUMBER = "876543210987654321"
PRIVATE_PATH = "/private/synthetic/" + PRIVATE_KEY


def _request():
    return PresentationJobRequest(
        conversation_id="synthetic-failure-privacy", user_request="合成安全校验",
        slide_count=1, cost_budget_units=10000,
        materials=[InputMaterial(
            id="source-synthetic-public", name="public-synthetic.md",
            content="# 合成事实\n本材料只用于公开合成诊断。",
        )],
    )


def _assert_private_markers_absent(job_dir):
    checked = 0
    for path in job_dir.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".txt", ".md"}:
            text = path.read_text(encoding="utf-8")
            assert PRIVATE_KEY not in text, path.name
            assert PRIVATE_PATH not in text, path.name
            assert PRIVATE_NUMBER not in text, path.name
            checked += 1
    assert checked > 0


@pytest.mark.parametrize(("case", "failure_kind", "issue_code"), [
    ("schema", "schema_validation", "structuredtaskvalidationerror"),
    ("numeric", "numeric_grounding", "content.unsupported_number"),
    ("source_ref", "ledger_known_source_refs", "content.claim_contract"),
    ("calculation_authorization", "calculation_authorization", "content.claim_contract"),
    ("timeout", None, "utilitytasktimeouterror"),
])
def test_actual_semantic_planner_runtime_failure_is_private_and_bounded(
    tmp_path, monkeypatch, case, failure_kind, issue_code,
):
    planner = intelligence.UtilityPresentationSemanticPlanner("synthetic-runtime-agent")
    runtime = PresentationJobRuntime(tmp_path, semantic_planner=planner)
    calls = []

    async def transport(**kwargs):
        calls.append(kwargs)
        if case == "timeout":
            raise UtilityTaskTimeoutError(
                phase="first_output", timeout_seconds=int(PRIVATE_NUMBER),
            )
        if case == "schema":
            payload = {"claims": [], PRIVATE_KEY: PRIVATE_PATH + PRIVATE_NUMBER}
        else:
            source_refs = json.loads(kwargs["user_prompt"])["allowed_source_refs"]
            assert source_refs
            payload = {"claims": [{
                "claim_id": "claim-001",
                "statement": "合成未获批准数字" + PRIVATE_NUMBER if case == "numeric" else "公开合成事实",
                "source_refs": ["SRC-SYNTH-UNKNOWN"] if case == "source_ref" else [source_refs[0]],
                "delivery_contract_ids": [],
            }]}
            if case == "calculation_authorization":
                claim = payload["claims"][0]
                claim["kind"] = "calculation"
                claim["calculation"] = "公开合成计算"
                claim["calculation_proofs"] = [{
                    "terms": [
                        {"source_ref": source_refs[0], "source_excerpt": "合成事实",
                         "value": "1", "unit": "万元", "coefficient": coefficient}
                        for coefficient in (1, -1)
                    ],
                    "result": "0", "unit": "万元",
                    "authorization_source_ref": source_refs[0],
                    "authorization_excerpt": PRIVATE_PATH + PRIVATE_NUMBER,
                }]
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), model_label="synthetic/test")

    monkeypatch.setattr(structured, "run_utility_text_task", transport)
    try:
        record = runtime.service.create_job(_request())
        runtime._run(record.job_id)
        saved = runtime.service.store.get(record.job_id)
        assert saved.status == JobStatus.FAILED
        assert saved.events[-1].failure_routes[0].issue_codes == [issue_code]
        assert len(calls) == planner.last_utility_call_count == 3
        assert planner.last_stage_attempt_counts == {"claims": 3}
        assert len({call["max_output_tokens"] for call in calls}) == 1
        assert len({call["timeout_seconds"] for call in calls}) == 1
        checkpoint = json.loads(runtime.service.store.checkpoint_path(
            record.job_id, "semantic-plan-diagnostics-recovery-000.json",
        ).read_text(encoding="utf-8"))
        assert checkpoint["total_output_token_budget"] == calls[0]["max_output_tokens"] * 3
        failures = checkpoint.get("stage_contract_failures", [])
        assert len(failures) == (0 if failure_kind is None else 3)
        if failure_kind is not None:
            assert [item["failure_kind"] for item in failures] == [failure_kind] * 3
        assert not runtime.service.store.checkpoint_path(record.job_id, "deck-plan.json").exists()
        assert not list(runtime.service.store.job_dir(record.job_id).rglob("*.pptx"))
        _assert_private_markers_absent(runtime.service.store.job_dir(record.job_id))
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("location", ["root", "nested", "code", "index"])
def test_page_author_retry_and_final_failure_never_persist_unowned_schema_paths(
    tmp_path, location,
):
    private_issue = {
        "root": f"extra_forbidden@$.{PRIVATE_KEY}",
        "nested": f"extra_forbidden@$.objects[0].shape.{PRIVATE_KEY}",
        "code": f"{PRIVATE_KEY}@$",
        "index": f"missing@$.objects[{PRIVATE_NUMBER}].frame",
    }[location]

    class RejectedAuthor:
        def __init__(self):
            self.calls = 0
            self.directives = []

        def author(self, *args, retry_directive=None):
            self.calls += 1
            self.directives.append(retry_directive)
            raise structured.StructuredTaskValidationError(
                PRIVATE_KEY + PRIVATE_PATH + PRIVATE_NUMBER,
                validation_issues=(private_issue, "missing@$.objects[0].frame"),
            )

    author = RejectedAuthor()
    runtime = PresentationJobRuntime(tmp_path, page_author=author)
    try:
        request = _request()
        record = runtime.service.create_job(request)
        brief = build_brief(request)
        plan = build_deterministic_fixture_plan(request, brief)
        runtime.service.store.write_checkpoint(record.job_id, "brief.json", brief.model_dump_json())
        runtime.service.store.write_checkpoint(record.job_id, "deck-plan.json", plan.model_dump_json())
        runtime._run(record.job_id)
        saved = runtime.service.store.get(record.job_id)
        assert saved.status == JobStatus.FAILED
        assert author.calls == saved.cost_used_units == 3
        assert saved.events[-1].failure_routes[0].issue_codes == ["structuredtaskvalidationerror"]
        assert author.directives[0] is None
        assert "missing@$.objects[0].frame" in author.directives[1]
        assert PRIVATE_KEY not in str(author.directives)
        assert PRIVATE_NUMBER not in str(author.directives)
        retries = [event for event in saved.events if "仅重试当前页" in event.message]
        assert len(retries) == 2
        assert all("missing@$.objects[0].frame" in event.message for event in retries)
        assert not runtime.service.store.checkpoint_path(record.job_id, "page-scene-001.json").exists()
        assert not list(runtime.service.store.job_dir(record.job_id).rglob("*.pptx"))
        _assert_private_markers_absent(runtime.service.store.job_dir(record.job_id))
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("error", [
    structured.StructuredTaskValidationError(PRIVATE_KEY, validation_issues=(f"extra_forbidden@$.{PRIVATE_KEY}",)),
    PresentationSemanticContentError(PRIVATE_NUMBER, violation_code="unsupported_number"),
    PresentationPageAuthorContentError(PRIVATE_NUMBER, violation_code="unsupported_number"),
    PresentationPageAuthorSceneError(PRIVATE_KEY, violation_code="dangling_object_reference"),
])
def test_model_contract_failure_formatter_keeps_route_not_rejected_prose(error):
    before = (str(error), error.__cause__)
    route = failure_route_for_exception(error, stage=JobStage.COMPOSE)
    assert failure_message_for_exception(error, route=route) == (
        f"当前步骤失败：{route.category.value}（{route.issue_codes[0]}）"
    )
    assert (str(error), error.__cause__) == before


def test_owned_file_failure_message_is_still_actionable():
    assert failure_message_for_exception(OSError("cannot write candidate.pptx")) == (
        "当前步骤失败：cannot write candidate.pptx"
    )
