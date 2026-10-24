# -*- coding: utf-8 -*-
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from lantai.app import welcome_greetings


def _request(**overrides):
    payload = {
        "locale": "zh-CN",
        "local_date": "2026-12-25",
        "timezone": "Asia/Shanghai",
        "scenario_keys": ["universal", "time.morning", "festival.christmas"],
        "cultural_calendar": "global",
        "tone": "natural",
        "policy_version": welcome_greetings.WELCOME_POLICY_VERSION,
    }
    payload.update(overrides)
    return welcome_greetings.WelcomeGenerationRequest.model_validate(payload)


def _generation_payload(title: str = "圣诞快乐") -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "id": "ai.christmas.1",
                    "title": title,
                    "subtitle": "愿今天有一点轻松，也有一点好心情。",
                    "scenario_keys": ["festival.christmas"],
                }
            ]
        },
        ensure_ascii=False,
    )


def test_request_rejects_scenario_names_that_only_mimic_an_allowed_prefix():
    with pytest.raises(ValueError, match="unsupported welcome scenario"):
        _request(scenario_keys=["universal.task-history"])


def test_request_rejects_arbitrary_context_hidden_behind_an_allowed_prefix():
    with pytest.raises(ValueError, match="unsupported welcome scenario"):
        _request(scenario_keys=["universal", "time.user_is_sad"])


def test_request_requires_the_expected_universal_and_time_signals():
    with pytest.raises(ValueError, match="universal welcome scenario is required"):
        _request(scenario_keys=["time.morning"])
    with pytest.raises(ValueError, match="exactly one time welcome scenario is required"):
        _request(scenario_keys=["universal"])


def test_request_rejects_festivals_without_the_explicit_matching_calendar():
    with pytest.raises(ValueError, match="explicit cultural calendar"):
        _request(cultural_calendar="none")
    with pytest.raises(ValueError, match="explicit cultural calendar"):
        _request(cultural_calendar="china")


def test_request_rejects_context_fields_outside_the_safe_schema():
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        _request(chat_history=[{"role": "user", "content": "旧任务"}])


@pytest.mark.asyncio
async def test_generate_and_review_uses_only_safe_current_entry_signals(monkeypatch):
    calls = []

    async def fake_task(**kwargs):
        calls.append(kwargs)
        if kwargs["purpose"] == "welcome-greeting-generate":
            return welcome_greetings.UtilityTextResult(
                text=_generation_payload(), model_label="test-model"
            )
        return welcome_greetings.UtilityTextResult(
            text=json.dumps(
                {"approved_ids": ["ai.christmas.1"]}, ensure_ascii=False
            ),
            model_label="test-model",
        )

    monkeypatch.setattr(welcome_greetings, "run_utility_text_task", fake_task)
    response = await welcome_greetings.generate_reviewed_welcome_candidates(
        _request(), agent_id="default"
    )

    assert response.status == "ready"
    assert [item.id for item in response.candidates] == ["ai.christmas.1"]
    assert len(calls) == 2
    joined_prompts = "\n".join(str(call["user_prompt"]) for call in calls)
    assert "festival.christmas" in joined_prompts
    assert "2026-12-25" in joined_prompts
    assert "聊天记录" not in joined_prompts
    assert "最近作品" not in joined_prompts


@pytest.mark.asyncio
async def test_rejects_reviewed_copy_that_implies_an_old_conversation(monkeypatch):
    async def fake_task(**kwargs):
        if kwargs["purpose"] == "welcome-greeting-generate":
            return welcome_greetings.UtilityTextResult(
                text=_generation_payload("上次那份方案，接着来"),
                model_label="test-model",
            )
        return welcome_greetings.UtilityTextResult(
            text=json.dumps(
                {"approved_ids": ["ai.christmas.1"]}, ensure_ascii=False
            ),
            model_label="test-model",
        )

    monkeypatch.setattr(welcome_greetings, "run_utility_text_task", fake_task)
    response = await welcome_greetings.generate_reviewed_welcome_candidates(
        _request(), agent_id="default"
    )

    assert response.status == "no_safe_candidates"
    assert response.candidates == []


@pytest.mark.parametrize(
    "title",
    ["欢迎回来", "今天辛苦了", "看起来你有点累", "这次慢慢来", "沈行知在这里"],
)
@pytest.mark.asyncio
async def test_rejects_copy_that_infers_presence_state_or_continuity(
    monkeypatch,
    title,
):
    calls = 0

    async def fake_task(**kwargs):
        nonlocal calls
        calls += 1
        return welcome_greetings.UtilityTextResult(
            text=_generation_payload(title), model_label="test-model"
        )

    monkeypatch.setattr(welcome_greetings, "run_utility_text_task", fake_task)
    response = await welcome_greetings.generate_reviewed_welcome_candidates(
        _request(), agent_id="default"
    )

    assert response.status == "no_safe_candidates"
    assert response.candidates == []
    assert calls == 1


@pytest.mark.asyncio
async def test_rejects_candidate_with_scenario_outside_the_request(monkeypatch):
    calls = 0

    async def fake_task(**kwargs):
        nonlocal calls
        calls += 1
        return welcome_greetings.UtilityTextResult(
            text=_generation_payload(), model_label="test-model"
        )

    monkeypatch.setattr(welcome_greetings, "run_utility_text_task", fake_task)
    response = await welcome_greetings.generate_reviewed_welcome_candidates(
        _request(
            scenario_keys=["universal", "time.morning"],
            cultural_calendar="none",
        ),
        agent_id="default",
    )

    assert response.status == "no_safe_candidates"
    assert response.candidates == []
    assert calls == 1


@pytest.mark.asyncio
async def test_rejects_festival_copy_mislabeled_as_universal(monkeypatch):
    calls = 0

    async def fake_task(**kwargs):
        nonlocal calls
        calls += 1
        return welcome_greetings.UtilityTextResult(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "id": "ai.mislabeled",
                            "title": "圣诞快乐",
                            "subtitle": "愿今天有一点轻松。",
                            "scenario_keys": ["universal"],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            model_label="test-model",
        )

    monkeypatch.setattr(welcome_greetings, "run_utility_text_task", fake_task)
    response = await welcome_greetings.generate_reviewed_welcome_candidates(
        _request(
            scenario_keys=["universal", "time.morning"],
            cultural_calendar="none",
        ),
        agent_id="default",
    )

    assert response.status == "no_safe_candidates"
    assert response.candidates == []
    assert calls == 1


@pytest.mark.asyncio
async def test_accepts_new_year_copy_only_with_an_explicit_new_year_scenario(monkeypatch):
    calls = []

    async def fake_task(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "welcome-greeting-generate":
            return welcome_greetings.UtilityTextResult(
                text=json.dumps(
                    {
                        "candidates": [
                            {
                                "id": "ai.new-year",
                                "title": "新年好",
                                "subtitle": "愿今天有个轻松的开头。",
                                "scenario_keys": ["festival.new_year"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                model_label="test-model",
            )
        return welcome_greetings.UtilityTextResult(
            text=json.dumps({"approved_ids": ["ai.new-year"]}, ensure_ascii=False),
            model_label="test-model",
        )

    monkeypatch.setattr(welcome_greetings, "run_utility_text_task", fake_task)
    response = await welcome_greetings.generate_reviewed_welcome_candidates(
        _request(
            local_date="2027-01-01",
            scenario_keys=["universal", "time.morning", "festival.new_year"],
            cultural_calendar="global",
        ),
        agent_id="default",
    )

    assert response.status == "ready"
    assert [candidate.id for candidate in response.candidates] == ["ai.new-year"]
    assert calls == ["welcome-greeting-generate", "welcome-greeting-review"]


@pytest.mark.asyncio
async def test_background_off_makes_zero_model_calls(monkeypatch):
    calls = 0

    async def fake_task(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("model must stay off")

    monkeypatch.setattr(welcome_greetings, "run_utility_text_task", fake_task)
    monkeypatch.setattr(
        welcome_greetings,
        "background_processing_enabled",
        lambda: False,
    )

    response = await welcome_greetings.generate_reviewed_welcome_candidates(
        _request(), agent_id="default"
    )

    assert response.status == "disabled"
    assert response.candidates == []
    assert calls == 0


def test_daily_budget_is_atomic_and_limited_to_two_batches(tmp_path):
    budget = welcome_greetings.WelcomeGenerationBudgetStore(
        tmp_path / "welcome-budget.sqlite3"
    )

    assert budget.reserve("default", "2026-12-25", "batch-a") is True
    assert budget.reserve("default", "2026-12-25", "batch-a") is True
    assert budget.reserve("default", "2026-12-25", "batch-b") is True
    assert budget.reserve("default", "2026-12-25", "batch-c") is False
    assert budget.used("default", "2026-12-25") == 2
    budget.close()


def test_daily_budget_is_atomic_across_store_connections(tmp_path):
    path = tmp_path / "welcome-budget.sqlite3"

    def reserve(index: int) -> bool:
        budget = welcome_greetings.WelcomeGenerationBudgetStore(path)
        try:
            return budget.reserve("default", "2026-12-25", f"batch-{index}")
        finally:
            budget.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(8)))

    audit = welcome_greetings.WelcomeGenerationBudgetStore(path)
    try:
        assert sum(results) == 2
        assert audit.used("default", "2026-12-25") == 2
    finally:
        audit.close()
