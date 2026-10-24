# -*- coding: utf-8 -*-
"""BRIEF-002: explicit configured-model consent extends, never migrates, BRIEF-001."""
from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.app import utility_model
from lantai.app.daily_brief.activation import (
    DailyBriefActivationService, MemoryTopicCandidate, MemoryTopicDiscovery,
)
from lantai.app.daily_brief.model_task import brief_model_capability
from lantai.app.daily_brief.models import (
    BriefActivationRequest, BriefBudget, BriefCostRecord, BriefGrant,
)
from lantai.app.daily_brief.production_research import (
    BriefSynthesis, ProductionBriefResearchAdapter,
)
from lantai.app.daily_brief.repo import DailyBriefRepository
from lantai.app.structured_utility import StructuredUtilityResult
from tests.unit.app.test_daily_brief_activation import _enable_research, _remember_public_clue
from tests.unit.app.test_daily_brief_production_research import _auto_envelope, _search_response
from lantai.app.daily_brief.research import SafeSourceRead, build_research_envelope


def _configured_cloud(monkeypatch):
    config = SimpleNamespace(mode="dedicated", timeout_seconds=12,
        background_processing_enabled=True)
    monkeypatch.setattr(utility_model, "load_config", lambda: SimpleNamespace(
        utility_intelligence=config))
    resolved = utility_model.ResolvedUtilityModel(model=AsyncMock(),
        provider_id="configured-cloud", model_id="selected-model")
    monkeypatch.setattr(utility_model, "resolve_utility_model", lambda *_: resolved)
    provider = SimpleNamespace(is_local=False,
        get_model_info=lambda _: SimpleNamespace(is_free=False))
    monkeypatch.setattr(utility_model.ProviderManager, "get_instance",
        lambda: SimpleNamespace(get_provider=lambda _: provider))
    return config


def test_capability_accepts_selected_cloud_model_only_under_extended_policy(monkeypatch):
    _configured_cloud(monkeypatch)
    assert not brief_model_capability("agent-1").ready
    capability = brief_model_capability("agent-1", allow_configured_model=True)
    assert capability.ready
    assert capability.model_label == "configured-cloud/selected-model"
    assert "额度" in capability.message


def test_configured_model_preflight_does_not_fall_back_if_selection_is_unavailable(monkeypatch):
    _configured_cloud(monkeypatch)
    monkeypatch.setattr(utility_model, "resolve_utility_model", lambda *_: None)
    fallback = AsyncMock()
    monkeypatch.setattr(utility_model, "resolve_active_model", fallback)
    capability = brief_model_capability("agent-1", allow_configured_model=True)
    assert not capability.ready and capability.code == "model_unavailable"
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_configured_model_execution_does_not_silently_use_active_model(monkeypatch):
    _configured_cloud(monkeypatch)
    monkeypatch.setattr(utility_model, "resolve_utility_model", lambda *_: None)
    fallback = AsyncMock()
    monkeypatch.setattr(utility_model, "resolve_active_model", fallback)
    result = await utility_model.run_utility_text_task(agent_id="agent-1",
        system_prompt="test", user_prompt="test", purpose="daily_brief_synthesis",
        allow_model_fallback=False)
    assert result is None
    fallback.assert_not_called()


def test_existing_grant_and_request_keep_free_only_policy():
    grant = BriefGrant.model_validate({"model_provider": "auto"})
    request = BriefActivationRequest(action_id="legacy")
    assert grant.model_policy == "free_only"
    assert grant.budget.max_cost_cny == 0
    assert request.configured_model_consent is False


@pytest.mark.asyncio
async def test_activation_records_consent_and_uses_configured_model_for_memory_discovery(tmp_path):
    await _enable_research(tmp_path)
    atom = _remember_public_clue(tmp_path)
    calls = []
    async def task(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(value=MemoryTopicDiscovery(topics=[
            MemoryTopicCandidate(title="智能体公开变化", purpose="跟踪公开发布",
                source_clue_ids=[atom.id])]), model_label="configured-cloud/selected-model", repaired=False)
    request = BriefActivationRequest(explicit_consent=True, configured_model_consent=True,
        memory_context_processing_consent=True, mode="preview", action_id="configured-activation")
    service = DailyBriefActivationService(tmp_path, agent_id="agent-1", structured_task=task)
    result = await service.activate(request)
    assert result.grant.model_policy == "configured"
    assert result.grant.budget.max_cost_usd > 0 and result.grant.budget.max_cost_cny > 0
    assert calls[0]["require_no_api_cost"] is False
    assert calls[0]["allow_model_fallback"] is False
    assert (await service.activate(request)).grant.id == result.grant.id
    assert len(calls) == 1
    store = await DailyBriefRepository(tmp_path).load()
    assert store.grants[0].model_policy == "configured"
    from lantai.app.reminders.repo import ReminderRepository
    envelope = build_research_envelope(cycle=result.cycle, grant=result.grant,
        topics=(await ReminderRepository(tmp_path).load()).watch_topics)
    assert envelope.model_policy == "configured"


class _Reader:
    async def read(self, url, *, max_chars):
        return SafeSourceRead(original_url=url, final_url=url, title="公开发布",
            text="已核实的公开发布变化。", content_type="text/html", status="usable",
            quality_score=90, can_cite_as_page_body=True, signals=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("currency,amount", [("cost_usd", 0.05), ("cost_cny", 0.72)])
async def test_configured_synthesis_succeeds_and_preserves_actual_cost(currency, amount):
    async def search(_query, *, limit):
        return _search_response(status="ok", results=[{"url": "https://example.com/release"}])
    async def task(**kwargs):
        assert kwargs["require_no_api_cost"] is False
        assert kwargs["allow_model_fallback"] is False
        return StructuredUtilityResult(value=BriefSynthesis(),
            model_label="configured-cloud/selected-model", repaired=False)
    envelope = _auto_envelope().model_copy(update={"model_policy": "configured",
        "budget": BriefBudget(max_searches=1, max_source_reads=2, max_model_tokens=4000,
            max_cost_usd=1, max_cost_cny=10)})
    records = []
    async def record(cost):
        records.append(cost)
    pops = 0
    def usage(_):
        nonlocal pops
        pops += 1
        return {"total_tokens": 300, currency: amount} if pops == 2 else None
    result = await ProductionBriefResearchAdapter(agent_id="agent-1", search=search,
        source_reader=_Reader(), structured_task=task, usage_popper=usage).run_with_usage(envelope, record)
    assert getattr(result.cost, currency) == amount
    assert getattr(records[-1], currency) == amount
    assert result.cost.model_usage_status == "recorded"


@pytest.mark.asyncio
@pytest.mark.parametrize("currency,amount", [("cost_usd", 1.01), ("cost_cny", 10.01)])
async def test_configured_synthesis_still_blocks_cost_overrun(currency, amount):
    async def search(_query, *, limit):
        return _search_response(status="ok", results=[{"url": "https://example.com/release"}])
    async def task(**_):
        return StructuredUtilityResult(value=BriefSynthesis(),
            model_label="configured-cloud/selected-model", repaired=False)
    envelope = _auto_envelope().model_copy(update={"model_policy": "configured",
        "budget": BriefBudget(max_model_tokens=4000, max_cost_usd=1, max_cost_cny=10)})
    with pytest.raises(RuntimeError, match="budget"):
        await ProductionBriefResearchAdapter(agent_id="agent-1", search=search,
            source_reader=_Reader(), structured_task=task,
            usage_popper=lambda _: {"total_tokens": 300, currency: amount}).run(envelope)


def test_remaining_budget_tracks_cny_without_converting_or_renewing_consent():
    from lantai.app.daily_brief.models import BriefCycle, DailyBriefStore
    from lantai.app.daily_brief.service import DailyBriefService
    from datetime import date
    grant = BriefGrant(model_policy="configured", budget=BriefBudget(
        max_model_tokens=4000, max_cost_usd=1, max_cost_cny=10))
    cycle = BriefCycle(logical_key="configured", grant_id=grant.id,
        grant_revision=1, topic_revisions={"topic-1": 1}, occurrence_id="manual:1",
        local_date=date(2026, 10, 3))
    store = DailyBriefStore(grants=[grant], cycles=[cycle], costs=[BriefCostRecord(
        cycle_id=cycle.id, model_tokens=300, model_usage_status="recorded", cost_cny=0.72)])
    budget = DailyBriefService._remaining_budget(store, cycle, grant)
    assert budget.max_model_tokens == 3700
    assert budget.max_cost_cny == pytest.approx(9.28)
    legacy = grant.model_copy(update={"model_policy": "free_only"})
    assert DailyBriefService._remaining_budget(store, cycle, legacy).max_model_tokens == 0


@pytest.mark.parametrize("currency,cap", [("cost_usd", 1), ("cost_cny", 10)])
def test_configured_cost_exhaustion_prevents_another_model_call(currency, cap):
    from lantai.app.daily_brief.models import BriefCycle, DailyBriefStore
    from lantai.app.daily_brief.service import DailyBriefService
    from datetime import date
    grant = BriefGrant(model_policy="configured", budget=BriefBudget(
        max_model_tokens=4000, max_cost_usd=1, max_cost_cny=10))
    cycle = BriefCycle(logical_key="configured", grant_id=grant.id,
        grant_revision=1, topic_revisions={"topic-1": 1}, occurrence_id="manual:1",
        local_date=date(2026, 10, 3))
    records = [BriefCostRecord(cycle_id=cycle.id, model_tokens=100,
        model_usage_status="recorded", **{currency: cap / 2}) for _ in range(2)]
    store = DailyBriefStore(grants=[grant], cycles=[cycle], costs=records)
    assert DailyBriefService._remaining_budget(store, cycle, grant).max_model_tokens == 0


@pytest.mark.asyncio
async def test_legacy_activation_fingerprint_stays_idempotent_after_upgrade(tmp_path):
    await _enable_research(tmp_path)
    atom = _remember_public_clue(tmp_path)
    task = AsyncMock(return_value=StructuredUtilityResult(value=MemoryTopicDiscovery(topics=[
        MemoryTopicCandidate(title="公开发布", purpose="公开变化",
            source_clue_ids=[atom.id])]), model_label="local/test", repaired=False))
    request = BriefActivationRequest(explicit_consent=True,
        memory_context_processing_consent=True, action_id="legacy-retry")
    service = DailyBriefActivationService(tmp_path, agent_id="agent-1", structured_task=task)
    result = await service.activate(request)
    legacy_hash = hashlib.sha256(request.model_dump_json(
        exclude={"configured_model_consent"}).encode()).hexdigest()
    await DailyBriefRepository(tmp_path).update(lambda data: data.model_copy(update={
        "action_fingerprints": {request.action_id: legacy_hash}}))
    assert (await service.activate(request)).grant.id == result.grant.id
    task.assert_awaited_once()
    from lantai.app.daily_brief.service import BriefConflict
    with pytest.raises(BriefConflict, match="选项已改变"):
        await service.activate(request.model_copy(update={"configured_model_consent": True}))
    task.assert_awaited_once()
