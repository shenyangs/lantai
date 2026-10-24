# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.agents.persona.user_understanding import (
    UnderstandingAtom,
    UserUnderstandingStore,
)
from lantai.app import utility_model
from lantai.app.daily_brief import api as api_module
from lantai.app.daily_brief.activation import DailyBriefActivationService
from lantai.app.daily_brief.models import BriefFeatureFlags
from lantai.app.daily_brief.repo import DailyBriefRepository
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.structured_utility import StructuredTaskValidationError


@pytest.fixture
def activation_client(monkeypatch, tmp_path):
    asyncio.run(
        DailyBriefRepository(tmp_path).update(
            lambda data: data.model_copy(
                update={"feature_flags": BriefFeatureFlags(research=True)}
            )
        )
    )
    UserUnderstandingStore(tmp_path).remember(
        UnderstandingAtom.create(
            kind="preference",
            statement="用户关注桌面智能体的可靠持续执行",
            status="confirmed",
            confidence=0.98,
            evidence_ids=("test-message-1",),
            future_relevance="评估公开的智能体技术变化",
            sensitivity="normal",
        )
    )
    wake = MagicMock()

    async def workspace(_request):
        return SimpleNamespace(
            workspace_dir=tmp_path,
            agent_id="agent-1",
            daily_brief=SimpleNamespace(wake=wake),
        )

    monkeypatch.setattr(api_module, "get_agent_for_request", workspace)
    app = FastAPI()
    app.include_router(api_module.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, wake


def _activate(client, *, mode="daily"):
    return client.post(
        "/daily-brief/activate",
        json={
            "explicit_consent": True,
            "memory_context_processing_consent": True,
            "mode": mode,
            "action_id": "activation-error-test",
        },
    )


def _assert_no_activation(tmp_path, wake):
    data = asyncio.run(DailyBriefRepository(tmp_path).load())
    assert data.grants == []
    assert data.subscriptions == []
    assert data.cycles == []
    assert asyncio.run(ReminderRepository(tmp_path).load()).watch_topics == []
    wake.assert_not_called()


def test_disabled_worker_rejects_before_creating_activation(monkeypatch, tmp_path, activation_client):
    client, wake = activation_client
    async def workspace(_request):
        return SimpleNamespace(workspace_dir=tmp_path, agent_id="agent-1",
            daily_brief=SimpleNamespace(enabled=False, running=False, wake=wake))
    monkeypatch.setattr(api_module, "get_agent_for_request", workspace)
    response = _activate(client)
    assert response.status_code == 503
    assert "后台执行器" in response.json()["detail"]
    _assert_no_activation(tmp_path, wake)


@pytest.mark.asyncio
async def test_manual_topics_are_preflighted_before_grant(monkeypatch, tmp_path):
    from tests.unit.app.test_daily_brief_domain import _seed_topic
    from lantai.app.daily_brief.models import BriefActivationRequest
    from lantai.app.daily_brief.service import BriefModelUnavailable
    topic = await _seed_topic(tmp_path)
    monkeypatch.setattr(utility_model, "load_config", lambda: SimpleNamespace(
        utility_intelligence=SimpleNamespace(mode="follow_active", background_processing_enabled=False)))
    with pytest.raises(BriefModelUnavailable, match="后台整理"):
        await DailyBriefActivationService(tmp_path, agent_id="agent-1").activate(
            BriefActivationRequest(explicit_consent=True, use_memory_clues=False,
                topic_ids=[topic.id], action_id="manual-preflight"))
    data = await DailyBriefRepository(tmp_path).load()
    assert not data.grants and not data.cycles and not data.subscriptions


@pytest.mark.parametrize("mode", ["daily", "preview"])
def test_configured_paid_model_rejection_is_actionable_and_makes_no_call(
    monkeypatch,
    tmp_path,
    activation_client,
    mode,
):
    client, wake = activation_client
    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: SimpleNamespace(
            utility_intelligence=SimpleNamespace(
                mode="dedicated",
                timeout_seconds=12,
                background_processing_enabled=True,
            )
        ),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent: utility_model.ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="configured-cloud",
            model_id="paid-model",
        ),
    )
    provider = SimpleNamespace(
        is_local=False,
        get_model_info=lambda _model: SimpleNamespace(is_free=False),
    )
    monkeypatch.setattr(
        utility_model.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(get_provider=lambda _provider: provider),
    )
    consume = AsyncMock()
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    response = _activate(client, mode=mode)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "本地或免费" in detail
    assert "设置" in detail
    assert "configured-cloud" not in detail
    consume.assert_not_awaited()
    _assert_no_activation(tmp_path, wake)


@pytest.mark.parametrize(
    "failure", ["unavailable", "timeout", "invalid_output"],
)
def test_topic_discovery_failure_returns_recoverable_error_without_activation(
    monkeypatch,
    tmp_path,
    activation_client,
    failure,
):
    client, wake = activation_client

    async def structured_task(**_kwargs):
        if failure == "timeout":
            raise utility_model.UtilityTaskTimeoutError(
                phase="first_output",
                timeout_seconds=12,
            )
        if failure == "invalid_output":
            raise StructuredTaskValidationError(
                "provider-specific private output"
            )
        return None

    monkeypatch.setattr(
        api_module,
        "DailyBriefActivationService",
        lambda workspace, **kwargs: DailyBriefActivationService(
            workspace,
            **kwargs,
            structured_task=structured_task,
        ),
    )

    response = _activate(client)

    assert response.status_code == 503
    assert "重试" in response.json()["detail"]
    assert "provider-specific private output" not in response.text
    _assert_no_activation(tmp_path, wake)
