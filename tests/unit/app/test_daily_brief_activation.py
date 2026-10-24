# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from datetime import time

import pytest

from lantai.agents.persona.user_understanding import UnderstandingAtom, UserUnderstandingStore
from lantai.app.daily_brief.activation import (
    DailyBriefActivationService,
    MemoryTopicCandidate,
    MemoryTopicDiscovery,
)
from lantai.app.daily_brief.models import (
    BriefActivationRequest,
    BriefFeatureFlags,
)
from lantai.app.daily_brief.repo import DailyBriefRepository
from lantai.app.daily_brief.service import ConsentRequired
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.models import WatchTopicCreateRequest
from lantai.app.reminders.service import ReminderService
from lantai.app.structured_utility import StructuredUtilityResult


async def _enable_research(tmp_path) -> None:
    await DailyBriefRepository(tmp_path).update(
        lambda data: data.model_copy(
            update={"feature_flags": BriefFeatureFlags(research=True)}
        )
    )


def _remember_public_clue(tmp_path, *, sensitivity: str = "normal") -> UnderstandingAtom:
    atom = UnderstandingAtom.create(
        kind="preference",
        statement="用户持续关注桌面智能体如何可靠地完成长期工作",
        status="confirmed",
        confidence=0.98,
        evidence_ids=("message-1",),
        future_relevance="评估智能体产品与运行时能力时有用",
        sensitivity=sensitivity,
    )
    UserUnderstandingStore(tmp_path).remember(atom)
    return atom


@pytest.mark.asyncio
async def test_daily_activation_discovers_confirmed_watch_topic_from_memory(tmp_path):
    await _enable_research(tmp_path)
    atom = _remember_public_clue(tmp_path)
    calls: list[dict] = []

    async def structured_task(**kwargs):
        calls.append(kwargs)
        return StructuredUtilityResult(
            value=MemoryTopicDiscovery(
                topics=[
                    MemoryTopicCandidate(
                        title="桌面智能体的可靠持续执行",
                        purpose="跟踪调度、恢复与权限边界的公开变化",
                        source_clue_ids=[atom.id],
                    )
                ]
            ),
            model_label="local/test",
            repaired=False,
        )

    service = DailyBriefActivationService(
        tmp_path,
        agent_id="agent-1",
        structured_task=structured_task,
    )
    result = await service.activate(
        BriefActivationRequest(
            explicit_consent=True,
            memory_context_processing_consent=True,
            mode="daily",
            timezone="Asia/Shanghai",
            local_time=time(8, 30),
            action_id="activate-1",
        )
    )

    assert result.subscription is not None
    assert result.cycle is None
    assert result.topic_ids == result.discovered_topic_ids
    assert result.grant.search_provider == "lantai_auto"
    assert result.grant.model_provider == "auto"
    assert result.grant.budget.max_cost_usd == 0
    assert calls[0]["purpose"] == "daily_brief_topic_discovery"
    assert calls[0]["require_no_api_cost"] is True
    assert calls[0]["usage_session_id"] == "daily-brief:activation:activate-1"
    prompt = json.loads(calls[0]["user_prompt"])
    assert prompt == {
        "clues": [
            {
                "clue_id": atom.id,
                "statement": atom.statement,
                "future_relevance": atom.future_relevance,
            }
        ]
    }

    topics = await ReminderRepository(tmp_path).load()
    topic = topics.watch_topics[0]
    assert topic.origin == "daily_brief_memory_discovery"
    assert topic.brief_status == "confirmed"
    assert topic.brief_evidence_refs == [f"understanding:{atom.id}"]


@pytest.mark.asyncio
async def test_activation_is_idempotent_across_topics_grant_and_subscription(tmp_path):
    await _enable_research(tmp_path)
    atom = _remember_public_clue(tmp_path)
    model_calls = 0

    async def structured_task(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        return StructuredUtilityResult(
            value=MemoryTopicDiscovery(
                topics=[
                    MemoryTopicCandidate(
                        title="智能体长期任务",
                        purpose="跟踪可靠执行的公开变化",
                        source_clue_ids=[atom.id],
                    )
                ]
            ),
            model_label="local/test",
            repaired=False,
        )

    service = DailyBriefActivationService(
        tmp_path,
        agent_id="agent-1",
        structured_task=structured_task,
    )
    request = BriefActivationRequest(
        explicit_consent=True,
        memory_context_processing_consent=True,
        action_id="same-action",
    )
    first = await service.activate(request)
    second = await service.activate(request)

    assert second.grant.id == first.grant.id
    assert second.subscription is not None and first.subscription is not None
    assert second.subscription.id == first.subscription.id
    assert second.topic_ids == first.topic_ids
    data = await ReminderRepository(tmp_path).load()
    assert len(data.watch_topics) == 1
    assert model_calls == 1


@pytest.mark.asyncio
async def test_same_activation_action_serializes_before_memory_model_call(tmp_path):
    await _enable_research(tmp_path)
    atom = _remember_public_clue(tmp_path)
    entered = asyncio.Event()
    finish = asyncio.Event()
    model_calls = 0

    async def structured_task(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        entered.set()
        await finish.wait()
        return StructuredUtilityResult(
            value=MemoryTopicDiscovery(topics=[MemoryTopicCandidate(
                title="智能体长期任务",
                purpose="跟踪可靠执行的公开变化",
                source_clue_ids=[atom.id],
            )]),
            model_label="local/test",
            repaired=False,
        )

    request = BriefActivationRequest(
        explicit_consent=True,
        memory_context_processing_consent=True,
        action_id="concurrent-activation",
    )
    first_service = DailyBriefActivationService(
        tmp_path, agent_id="agent-1", structured_task=structured_task,
    )
    second_service = DailyBriefActivationService(
        tmp_path, agent_id="agent-1", structured_task=structured_task,
    )
    first = asyncio.create_task(first_service.activate(request))
    await entered.wait()
    second = asyncio.create_task(second_service.activate(request))
    await asyncio.sleep(0)
    finish.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert model_calls == 1
    assert first_result.grant.id == second_result.grant.id
    assert first_result.topic_ids == second_result.topic_ids
    assert len((await ReminderRepository(tmp_path).load()).watch_topics) == 1


@pytest.mark.asyncio
async def test_activation_recovers_partially_created_topic_without_model_retry(tmp_path):
    await _enable_research(tmp_path)
    atom = _remember_public_clue(tmp_path)
    reminders = ReminderService(ReminderRepository(tmp_path))
    partial = await reminders.create_watch_topic(
        WatchTopicCreateRequest(
            title="智能体长期任务",
            description="跟踪可靠执行的公开变化",
            origin="daily_brief_memory_discovery",
            metadata={
                "daily_brief_activation_key": "recover-action:0",
                "daily_brief_activation_id": "recover-action",
                "memory_topic_policy": "v1",
                "source_clue_ids": [atom.id],
            },
        )
    )

    async def structured_task(**_kwargs):
        raise AssertionError("a partial activation must not rediscover topics")

    result = await DailyBriefActivationService(
        tmp_path,
        agent_id="agent-1",
        structured_task=structured_task,
    ).activate(
        BriefActivationRequest(
            explicit_consent=True,
            memory_context_processing_consent=True,
            action_id="recover-action",
        )
    )

    saved = await ReminderRepository(tmp_path).load()
    recovered = next(topic for topic in saved.watch_topics if topic.id == partial.id)
    assert recovered.brief_status == "confirmed"
    assert recovered.brief_evidence_refs == [f"understanding:{atom.id}"]
    assert result.topic_ids == [partial.id]


@pytest.mark.asyncio
async def test_activation_without_consent_or_eligible_memory_has_no_side_effect(tmp_path):
    await _enable_research(tmp_path)
    _remember_public_clue(tmp_path, sensitivity="sensitive")
    model_calls = 0

    async def structured_task(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("ineligible memory must not be sent to a model")

    service = DailyBriefActivationService(
        tmp_path,
        agent_id="agent-1",
        structured_task=structured_task,
    )
    with pytest.raises(ConsentRequired):
        await service.activate(
            BriefActivationRequest(explicit_consent=False, action_id="no-consent")
        )
    with pytest.raises(ConsentRequired, match="没有可用的记忆线索"):
        await service.activate(
            BriefActivationRequest(
                explicit_consent=True,
                memory_context_processing_consent=True,
                action_id="no-memory",
            )
        )

    assert model_calls == 0
    assert (await ReminderRepository(tmp_path).load()).watch_topics == []


@pytest.mark.asyncio
async def test_memory_clues_require_separate_model_processing_consent(tmp_path):
    await _enable_research(tmp_path)
    _remember_public_clue(tmp_path)
    model_calls = 0

    async def structured_task(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("memory must not leave the store without separate consent")

    service = DailyBriefActivationService(
        tmp_path,
        agent_id="agent-1",
        structured_task=structured_task,
    )
    with pytest.raises(ConsentRequired, match="记忆线索交给当前模型"):
        await service.activate(
            BriefActivationRequest(
                explicit_consent=True,
                use_memory_clues=True,
                memory_context_processing_consent=False,
                action_id="no-memory-outbound-consent",
            )
        )

    assert model_calls == 0
    assert (await ReminderRepository(tmp_path).load()).watch_topics == []
