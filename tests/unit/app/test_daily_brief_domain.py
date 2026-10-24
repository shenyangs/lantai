# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone

import pytest
from pydantic import ValidationError

from lantai.app.daily_brief.delivery import (
    BriefDeliveryWorker,
    DeliveryAdapterResult,
    NativeBriefDeliveryDispatcher,
)
from lantai.app.daily_brief.lifecycle import DailyBriefWorkspaceService
from lantai.app.daily_brief.models import (
    BriefBudget,
    BriefCostRecord,
    BriefCoverage,
    BriefDiscussionDraftCreateRequest,
    BriefEvidence,
    BriefFeatureFlags,
    BriefGrant,
    BriefGrantCreateRequest,
    BriefItem,
    BriefPublishRequest,
    BriefSubscriptionCreateRequest,
    BriefSubscriptionUpdateRequest,
    BriefTrend,
)
from lantai.app.daily_brief.repo import DailyBriefRepository
from lantai.app.daily_brief.research import build_research_envelope
from lantai.app.daily_brief.research import (
    BriefResearchResult,
    DailyBriefResearchRunner,
)
from lantai.app.daily_brief.scheduler import DailyBriefScheduler, decide_subscription_tick
from lantai.app.daily_brief.service import (
    BriefConflict,
    BriefNotFound,
    ConsentRequired,
    DailyBriefService,
    ScopeFenceFailed,
)
from lantai.app.reminders.models import (
    RemindersFile,
    WatchTopic,
    WatchTopicUpdateRequest,
)
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.service import ReminderService


NOW = datetime(2026, 9, 20, 1, 15, tzinfo=timezone.utc)


async def _seed_topic(
    tmp_path,
    *,
    topic_id: str = "topic-1",
    evidence_refs: list[str] | None = None,
) -> WatchTopic:
    topic = WatchTopic(
        id=topic_id,
        title="AI 办公产品",
        brief_status="confirmed",
        brief_purpose="跟踪与用户正在做的桌面智能体有关的变化",
        brief_scope_refs=["public:web"],
        brief_evidence_refs=evidence_refs or [],
        brief_exclusions=["rumor-only"],
    )
    await ReminderRepository(tmp_path).save(RemindersFile(watch_topics=[topic]))
    return topic


async def _enable_flags(tmp_path, *, research: bool, native: bool) -> None:
    await DailyBriefRepository(tmp_path).update(
        lambda data: data.model_copy(
            update={
                "feature_flags": BriefFeatureFlags(
                    homepage_entry=True,
                    research=research,
                    native_notification=native,
                )
            }
        )
    )


async def _provision_subscription(
    tmp_path,
    *,
    native_notifications: bool = False,
    research_enabled: bool = True,
    quiet_hours_start: time | None = None,
    quiet_hours_end: time | None = None,
    topic_evidence_refs: list[str] | None = None,
):
    topic = await _seed_topic(tmp_path, evidence_refs=topic_evidence_refs)
    await _enable_flags(
        tmp_path,
        research=research_enabled,
        native=native_notifications,
    )
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            search_provider="fixture-search",
            model_provider="fixture-model",
            action_id="grant-1",
        )
    )
    subscription = await service.create_subscription(
        BriefSubscriptionCreateRequest(
            grant_id=grant.id,
            topic_ids=[topic.id],
            timezone="Asia/Shanghai",
            local_time=time(8, 0),
            native_notifications=native_notifications,
            quiet_hours_start=quiet_hours_start,
            quiet_hours_end=quiet_hours_end,
            action_id="subscription-1",
        )
    )
    return service, topic, grant, subscription


def _ready_publish_request(cycle, *, summary: str = "一项有依据的新变化") -> BriefPublishRequest:
    evidence = BriefEvidence(
        id="evidence-1",
        canonical_url="https://example.com/primary",
        original_url="https://example.com/primary",
        source_kind="primary",
        publisher="Example",
        title="Primary document",
        published_at=NOW - timedelta(hours=3),
        event_at=NOW - timedelta(hours=5),
        fetched_at=NOW,
        read_level="primary_document",
        evidence_text="这是测试中已经真实读取的原始来源正文摘要。",
        content_hash="0123456789abcdef",
    )
    item = BriefItem(
        id="item-1",
        topic_id="topic-1",
        title="有依据的变化",
        fact="原始文档发布了一项新能力。",
        relevance="它会影响用户正在做的桌面智能体工作。",
        analysis="目前更像是产品边界的变化。",
        limitations="尚未有长期使用数据。",
        optional_action="可在下一轮方案对比中纳入。",
        evidence_ids=[evidence.id],
    )
    trend = BriefTrend(
        topic_id="topic-1",
        statement="桌面智能体正在将能力向可控的持续任务扩展。",
        support_evidence_ids=[evidence.id],
        falsifiers=["未来两个版本撤回该能力"],
        uncertainty="单一来源只能支持方向性判断。",
    )
    return BriefPublishRequest(
        cycle_id=cycle.id,
        expected_grant_revision=cycle.grant_revision,
        expected_topic_revisions=cycle.topic_revisions,
        outcome="ready",
        local_date=cycle.local_date,
        timezone=cycle.timezone,
        summary=summary,
        coverage=[
            BriefCoverage(
                topic_id="topic-1",
                status="complete",
                checked_at=NOW,
                query_count=2,
                source_read_count=1,
            )
        ],
        evidence=[evidence],
        items=[item],
        trends=[trend],
        checked_started_at=NOW - timedelta(minutes=8),
        checked_finished_at=NOW,
    )


@pytest.mark.asyncio
async def test_explicit_topic_confirmation_and_grant_are_both_required(tmp_path):
    topic = WatchTopic(id="topic-1", title="未确认主题", brief_status="proposed")
    await ReminderRepository(tmp_path).save(RemindersFile(watch_topics=[topic]))
    service = DailyBriefService(tmp_path)

    with pytest.raises(ConsentRequired):
        await service.create_grant(
            BriefGrantCreateRequest(
                explicit_consent=False,
                topic_ids=[topic.id],
                action_id="without-consent",
            )
        )
    with pytest.raises(ConsentRequired):
        await service.create_grant(
            BriefGrantCreateRequest(
                explicit_consent=True,
                topic_ids=[topic.id],
                action_id="unconfirmed-topic",
            )
        )


@pytest.mark.asyncio
async def test_one_time_preview_never_becomes_a_subscription_and_is_consumed(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            action_id="preview-grant",
        )
    )

    with pytest.raises(ConsentRequired):
        await service.create_subscription(
            BriefSubscriptionCreateRequest(
                grant_id=grant.id,
                topic_ids=[topic.id],
                action_id="must-not-subscribe",
            )
        )

    cycle = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        timezone="America/New_York",
        action_id="preview-cycle",
    )
    await service.claim_preview_cycle(cycle.id)
    published = await service.publish(_ready_publish_request(cycle))
    store = await DailyBriefRepository(tmp_path).load()

    assert published.subscription_id is None
    assert published.timezone == "America/New_York"
    assert store.subscriptions == []
    assert next(entry for entry in store.grants if entry.id == grant.id).status == "consumed"


@pytest.mark.asyncio
async def test_one_time_preview_grant_maps_to_one_logical_cycle_across_dates(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            action_id="single-preview-grant",
        )
    )

    first = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        timezone="Asia/Shanghai",
        action_id="single-preview-first",
    )
    repeated_next_day = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 21),
        timezone="America/New_York",
        action_id="single-preview-next-day",
    )

    store = await DailyBriefRepository(tmp_path).load()
    assert repeated_next_day.id == first.id
    assert repeated_next_day.local_date == date(2026, 9, 20)
    assert repeated_next_day.timezone == "Asia/Shanghai"
    assert len([cycle for cycle in store.cycles if cycle.grant_id == grant.id]) == 1
    assert store.action_results["single-preview-next-day"] == first.id


def test_research_envelope_isolated_and_private_context_is_reference_fenced():
    topic = WatchTopic(
        id="topic-1",
        title="公开主题",
        brief_status="confirmed",
        brief_purpose="测试范围",
    )
    grant = BriefGrant(
        topic_ids=[topic.id],
        cloud_processing="approved_context",
        allowed_context_refs=["memory:approved"],
    )
    from lantai.app.daily_brief.models import BriefCycle

    cycle = BriefCycle(
        logical_key="preview:test:2026-09-20",
        grant_id=grant.id,
        grant_revision=grant.revision,
        topic_revisions={topic.id: topic.revision},
        occurrence_id="manual:test",
        local_date=date(2026, 9, 20),
        timezone="America/New_York",
        manual=True,
    )

    envelope = build_research_envelope(
        cycle=cycle,
        grant=grant,
        topics=[topic],
        selected_context={"memory:approved": "只允许这一段"},
    )
    assert envelope.share_session is False
    assert envelope.target_session_id is None
    assert envelope.timezone == "America/New_York"
    assert envelope.budget == grant.budget
    assert [entry.ref for entry in envelope.approved_context] == ["memory:approved"]

    with pytest.raises(ScopeFenceFailed):
        build_research_envelope(
            cycle=cycle,
            grant=grant,
            topics=[topic],
            selected_context={"memory:not-approved": "不得出站"},
        )


class _ResearchAdapter:
    def __init__(self, result_factory) -> None:
        self.calls = 0
        self.envelopes = []
        self._result_factory = result_factory

    async def run(self, envelope):
        self.calls += 1
        self.envelopes.append(envelope)
        return await self._result_factory(envelope)


@pytest.mark.asyncio
async def test_preview_runner_isolated_and_claimed_exactly_once(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            search_provider="fixture-search",
            model_provider="fixture-model",
            action_id="runner-grant",
        )
    )
    cycle = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        action_id="runner-preview",
    )

    async def result_factory(envelope):
        return BriefResearchResult(
            publish_request=_ready_publish_request(cycle),
            cost=BriefCostRecord(
                cycle_id=envelope.cycle_id,
                search_count=2,
                source_read_count=1,
                model_tokens=900,
                cost_usd=0.04,
            ),
        )

    adapter = _ResearchAdapter(result_factory)
    runner = DailyBriefResearchRunner(tmp_path, adapter=adapter)
    edition = await runner.run_preview(cycle.id)

    assert edition.cycle_id == cycle.id
    assert adapter.calls == 1
    assert adapter.envelopes[0].share_session is False
    assert adapter.envelopes[0].target_session_id is None
    with pytest.raises(BriefConflict):
        await runner.run_preview(cycle.id)
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_workspace_lifecycle_explicit_opt_out_does_not_claim_preview(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            action_id="lifecycle-off-grant",
        )
    )
    cycle = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        action_id="lifecycle-off-preview",
    )
    lifecycle = DailyBriefWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="test-agent",
        env={"LANTAI_DAILY_BRIEF_WORKER": "0"},
    )

    await lifecycle.start()

    store = await DailyBriefRepository(tmp_path).load()
    saved = next(item for item in store.cycles if item.id == cycle.id)
    assert lifecycle.enabled is False
    assert lifecycle.running is False
    assert saved.status == "queued"


@pytest.mark.asyncio
async def test_workspace_lifecycle_requires_persisted_research_gate(tmp_path):
    lifecycle = DailyBriefWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="test-agent",
        env={},
    )

    tick = await lifecycle.run_once(now=NOW)

    assert lifecycle.enabled is True
    assert tick.claimed_preview_count == 0
    assert tick.claimed_subscription_count == 0
    assert tick.published_count == 0
    assert tick.failed_count == 0


@pytest.mark.asyncio
async def test_controlled_lifecycle_publishes_each_preview_once(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            search_provider="fixture-search",
            model_provider="fixture-model",
            action_id="lifecycle-grant",
        )
    )
    cycle = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        action_id="lifecycle-preview",
    )

    async def result_factory(envelope):
        return BriefResearchResult(
            publish_request=_ready_publish_request(cycle),
            cost=BriefCostRecord(cycle_id=envelope.cycle_id, search_count=1),
        )

    adapter = _ResearchAdapter(result_factory)
    lifecycle = DailyBriefWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="test-agent",
        env={"LANTAI_DAILY_BRIEF_WORKER": "1"},
        adapter=adapter,
    )

    first = await lifecycle.run_once(now=NOW)
    second = await lifecycle.run_once(now=NOW + timedelta(minutes=1))

    assert first.claimed_preview_count == 1
    assert first.published_count == 1
    assert first.failed_count == 0
    assert second.claimed_preview_count == 0
    assert second.published_count == 0
    assert adapter.calls == 1
    assert adapter.envelopes[0].share_session is False
    assert adapter.envelopes[0].target_session_id is None


@pytest.mark.asyncio
async def test_controlled_lifecycle_times_out_one_cycle_and_fails_visibly(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            search_provider="fixture-search",
            model_provider="fixture-model",
            action_id="timeout-grant",
        )
    )
    cycle = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        action_id="timeout-preview",
    )

    async def never_finishes(_envelope):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    lifecycle = DailyBriefWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="test-agent",
        env={
            "LANTAI_DAILY_BRIEF_WORKER": "1",
            "LANTAI_DAILY_BRIEF_TIMEOUT_SECONDS": "0.01",
        },
        adapter=_ResearchAdapter(never_finishes),
    )

    tick = await lifecycle.run_once(now=NOW)

    store = await DailyBriefRepository(tmp_path).load()
    saved = next(item for item in store.cycles if item.id == cycle.id)
    assert tick.failed_count == 1
    assert tick.results[0].status == "failed"
    assert "TimeoutError" in tick.results[0].error
    assert saved.status == "failed"
    assert "TimeoutError" in saved.failure_reason


@pytest.mark.asyncio
async def test_runner_rejects_result_with_another_timezone(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            search_provider="fixture-search",
            model_provider="fixture-model",
            action_id="timezone-grant",
        )
    )
    cycle = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        timezone="Asia/Shanghai",
        action_id="timezone-preview",
    )

    async def wrong_timezone(envelope):
        return BriefResearchResult(
            publish_request=_ready_publish_request(cycle).model_copy(
                update={"timezone": "UTC"}
            ),
            cost=BriefCostRecord(cycle_id=envelope.cycle_id),
        )

    runner = DailyBriefResearchRunner(
        tmp_path,
        adapter=_ResearchAdapter(wrong_timezone),
    )

    with pytest.raises(ScopeFenceFailed, match="claimed cycle"):
        await runner.run_preview(cycle.id)


@pytest.mark.asyncio
async def test_controlled_lifecycle_claims_due_subscription_once(tmp_path):
    _service, _topic, _grant, subscription = await _provision_subscription(tmp_path)
    repo = DailyBriefRepository(tmp_path)

    async def result_factory(envelope):
        store = await repo.load()
        cycle = next(item for item in store.cycles if item.id == envelope.cycle_id)
        return BriefResearchResult(
            publish_request=_ready_publish_request(cycle),
            cost=BriefCostRecord(cycle_id=envelope.cycle_id, search_count=1),
        )

    adapter = _ResearchAdapter(result_factory)
    lifecycle = DailyBriefWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="test-agent",
        env={"LANTAI_DAILY_BRIEF_WORKER": "1"},
        adapter=adapter,
    )
    due = datetime(2026, 9, 20, 0, 5, tzinfo=timezone.utc)

    first = await lifecycle.run_once(now=due)
    second = await lifecycle.run_once(now=due + timedelta(minutes=5))

    store = await repo.load()
    assert first.claimed_subscription_count == 1
    assert first.published_count == 1
    assert second.claimed_subscription_count == 0
    assert adapter.calls == 1
    assert len(store.editions) == 1
    assert store.editions[0].subscription_id == subscription.id


@pytest.mark.asyncio
async def test_default_lifecycle_fails_visibly_when_authorized_provider_is_unsupported(
    tmp_path,
):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            action_id="lifecycle-unconfigured-grant",
        )
    )
    cycle = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        action_id="lifecycle-unconfigured-preview",
    )
    lifecycle = DailyBriefWorkspaceService(
        workspace_dir=tmp_path,
        agent_id="test-agent",
        env={"LANTAI_DAILY_BRIEF_WORKER": "1"},
    )

    tick = await lifecycle.run_once(now=NOW)

    store = await DailyBriefRepository(tmp_path).load()
    saved = next(item for item in store.cycles if item.id == cycle.id)
    assert tick.failed_count == 1
    assert tick.published_count == 0
    assert saved.status == "failed"
    assert "search provider is unsupported" in saved.failure_reason


@pytest.mark.asyncio
async def test_runner_does_not_resurrect_cycle_invalidated_during_research(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="public_only",
            action_id="revoked-runner-grant",
        )
    )
    cycle = await service.create_preview_cycle(
        grant_id=grant.id,
        local_date=date(2026, 9, 20),
        action_id="revoked-runner-preview",
    )

    async def revoke_then_return(_envelope):
        await service.revoke_grant(
            grant.id,
            expected_revision=grant.revision,
            action_id="revoke-during-research",
        )
        return BriefResearchResult(
            publish_request=_ready_publish_request(cycle),
            cost=BriefCostRecord(cycle_id=cycle.id),
        )

    runner = DailyBriefResearchRunner(
        tmp_path,
        adapter=_ResearchAdapter(revoke_then_return),
    )
    with pytest.raises(ScopeFenceFailed):
        await runner.run_preview(cycle.id)

    store = await DailyBriefRepository(tmp_path).load()
    saved = next(item for item in store.cycles if item.id == cycle.id)
    assert saved.status == "invalidated"
    assert saved.failure_reason == "grant_revoked"


def test_no_update_cannot_hide_a_partial_or_failed_check():
    with pytest.raises(ValidationError):
        BriefPublishRequest(
            cycle_id="cycle-1",
            expected_grant_revision=1,
            expected_topic_revisions={"topic-1": 1},
            outcome="no_material_update",
            local_date=date(2026, 9, 20),
            timezone="Asia/Shanghai",
            coverage=[BriefCoverage(topic_id="topic-1", status="failed")],
            checked_started_at=NOW - timedelta(minutes=2),
            checked_finished_at=NOW,
        )


def test_scheduler_uses_iana_local_date_across_dst_boundaries():
    from lantai.app.daily_brief.models import BriefSubscription

    subscription = BriefSubscription(
        grant_id="grant-1",
        grant_revision=1,
        topic_revisions={"topic-1": 1},
        timezone="America/New_York",
        local_time=time(1, 30),
    )
    before = decide_subscription_tick(
        subscription,
        now=datetime(2026, 11, 1, 5, 20, tzinfo=timezone.utc),
    )
    after = decide_subscription_tick(
        subscription,
        now=datetime(2026, 11, 1, 6, 40, tzinfo=timezone.utc),
    )

    assert before.local_date == after.local_date == "2026-11-01"
    assert before.due is False
    assert after.due is True
    assert before.occurrence_id == after.occurrence_id


@pytest.mark.asyncio
async def test_scheduler_claims_one_logical_cycle_and_never_retries_on_tick(tmp_path):
    service, _topic, _grant, subscription = await _provision_subscription(tmp_path)
    scheduler = DailyBriefScheduler(tmp_path)
    due = datetime(2026, 9, 20, 0, 5, tzinfo=timezone.utc)

    first = await scheduler.tick(now=due)
    second = await scheduler.tick(now=due + timedelta(minutes=15))
    await service.fail_cycle(first[0].id, "provider unavailable")
    third = await scheduler.tick(now=due + timedelta(hours=2))

    assert len(first) == 1
    assert first[0].subscription_id == subscription.id
    assert second == []
    assert third == []


@pytest.mark.asyncio
async def test_publish_is_revisioned_deduped_and_watermark_only_advances_on_success(tmp_path):
    service, _topic, _grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
    )
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="occurrence-1",
        local_date=date(2026, 9, 20),
    )
    first_request = _ready_publish_request(cycle)
    first = await service.publish(first_request)
    duplicate = await service.publish(first_request)
    revised = await service.publish(
        _ready_publish_request(cycle, summary="经更正的摘要")
    )
    store = await DailyBriefRepository(tmp_path).load()
    saved_subscription = next(item for item in store.subscriptions if item.id == subscription.id)

    assert first.id == duplicate.id == revised.id
    assert duplicate.revision == 1
    assert revised.revision == 2
    assert saved_subscription.last_successful_local_date == date(2026, 9, 20)
    assert [(item.channel, item.edition_revision, item.state) for item in store.delivery_receipts] == [
        ("native", 2, "pending"),
        ("home", 2, "submitted"),
        ("native", 1, "suppressed"),
        ("home", 1, "submitted"),
    ]


@pytest.mark.asyncio
async def test_budget_and_topic_revision_are_commit_time_fences(tmp_path):
    service, topic, grant, subscription = await _provision_subscription(tmp_path)
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="occurrence-budget",
        local_date=date(2026, 9, 20),
    )
    await service.record_cost(
        BriefCostRecord(cycle_id=cycle.id, search_count=grant.budget.max_searches)
    )
    with pytest.raises(ScopeFenceFailed):
        await service.record_cost(BriefCostRecord(cycle_id=cycle.id, search_count=1))

    changed = topic.model_copy(update={"revision": topic.revision + 1})
    await ReminderRepository(tmp_path).save(RemindersFile(watch_topics=[changed]))
    with pytest.raises(ScopeFenceFailed):
        await service.publish(_ready_publish_request(cycle))


@pytest.mark.asyncio
async def test_revoke_withdraws_editions_invalidates_drafts_and_suppresses_pending(tmp_path):
    service, _topic, grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
    )
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="occurrence-revoke",
        local_date=date(2026, 9, 20),
    )
    edition = await service.publish(_ready_publish_request(cycle))
    draft = await service.create_discussion_draft(
        edition.id,
        BriefDiscussionDraftCreateRequest(
            edition_revision=edition.revision,
            item_id="item-1",
            text="这是未发送的讨论草稿",
            action_id="draft-before-revoke",
        ),
    )

    await service.revoke_grant(
        grant.id,
        expected_revision=grant.revision,
        action_id="revoke-1",
    )
    store = await DailyBriefRepository(tmp_path).load()
    saved_draft = next(item for item in store.discussion_drafts if item.id == draft.id)

    assert saved_draft.status == "invalidated"
    assert saved_draft.text == ""
    withdrawn = next(item for item in store.editions if item.id == edition.id)
    assert withdrawn.status == "withdrawn"
    assert withdrawn.summary == ""
    assert withdrawn.evidence == withdrawn.items == withdrawn.trends == []
    assert withdrawn.content_hash == ""
    assert all(
        item.state != "pending"
        for item in store.delivery_receipts
        if item.channel == "native"
    )
    with pytest.raises(BriefNotFound):
        await service.get_edition(edition.id)


@pytest.mark.asyncio
async def test_private_derivatives_require_exact_context_grant_and_are_erased(tmp_path):
    topic = await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=True)
    service = DailyBriefService(tmp_path)

    public_grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            one_time=True,
            topic_ids=[topic.id],
            cloud_processing="public_only",
            action_id="public-grant",
        )
    )
    public_cycle = await service.create_preview_cycle(
        grant_id=public_grant.id,
        local_date=date(2026, 9, 20),
        action_id="public-preview",
    )
    await service.claim_preview_cycle(public_cycle.id)
    with pytest.raises(ScopeFenceFailed):
        await service.publish(
            _ready_publish_request(public_cycle).model_copy(
                update={"private_dependency_refs": ["memory:approved"]}
            )
        )

    grant = await service.create_grant(
        BriefGrantCreateRequest(
            explicit_consent=True,
            topic_ids=[topic.id],
            public_source_discovery=True,
            public_source_read=True,
            cloud_processing="approved_context",
            allowed_context_refs=["memory:approved"],
            action_id="private-grant",
        )
    )
    subscription = await service.create_subscription(
        BriefSubscriptionCreateRequest(
            grant_id=grant.id,
            topic_ids=[topic.id],
            native_notifications=True,
            action_id="private-subscription",
        )
    )
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="private-occurrence",
        local_date=date(2026, 9, 20),
    )
    edition = await service.publish(
        _ready_publish_request(cycle).model_copy(
            update={"private_dependency_refs": ["memory:approved"]}
        )
    )
    draft = await service.create_discussion_draft(
        edition.id,
        BriefDiscussionDraftCreateRequest(
            edition_revision=edition.revision,
            text="这是一段依赖私人上下文的未发送草稿",
            action_id="private-draft",
        ),
    )

    await service.invalidate_dependency("memory:approved")
    store = await DailyBriefRepository(tmp_path).load()
    withdrawn = next(item for item in store.editions if item.id == edition.id)
    invalidated = next(item for item in store.discussion_drafts if item.id == draft.id)
    saved_grant = next(item for item in store.grants if item.id == grant.id)

    assert withdrawn.status == "withdrawn"
    assert withdrawn.summary == ""
    assert withdrawn.evidence == withdrawn.items == withdrawn.trends == []
    assert invalidated.status == "invalidated"
    assert invalidated.text == ""
    assert saved_grant.allowed_context_refs == []
    assert saved_grant.revision == grant.revision + 1
    assert all(
        receipt.state != "pending"
        for receipt in store.delivery_receipts
        if receipt.channel == "native"
    )


@pytest.mark.asyncio
async def test_pausing_subscription_invalidates_work_and_suppresses_notification(tmp_path):
    service, _topic, _grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
    )
    completed = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="pause-completed",
        local_date=date(2026, 9, 20),
    )
    await service.publish(_ready_publish_request(completed))
    running = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="pause-running",
        local_date=date(2026, 9, 21),
    )

    paused = await service.update_subscription(
        subscription.id,
        BriefSubscriptionUpdateRequest(
            expected_revision=subscription.revision,
            status="paused",
            action_id="pause-subscription",
        ),
    )
    store = await DailyBriefRepository(tmp_path).load()
    saved_cycle = next(item for item in store.cycles if item.id == running.id)

    assert paused.status == "paused"
    assert saved_cycle.status == "invalidated"
    assert all(
        receipt.state != "pending"
        for receipt in store.delivery_receipts
        if receipt.channel == "native"
    )


@pytest.mark.asyncio
async def test_watch_topic_check_watermark_preserves_revision_and_published_brief(tmp_path):
    service, topic, _grant, subscription = await _provision_subscription(tmp_path)
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="topic-watermark",
        local_date=date(2026, 9, 20),
    )
    edition = await service.publish(_ready_publish_request(cycle))
    checked_at = datetime(2026, 9, 20, 8, 30, tzinfo=timezone.utc)

    updated = await ReminderService(ReminderRepository(tmp_path)).update_watch_topic(
        topic.id,
        WatchTopicUpdateRequest(
            last_checked_at=checked_at,
            expected_revision=topic.revision,
        ),
    )
    stored = await service.get_edition(edition.id)

    assert updated is not None
    assert updated.revision == topic.revision
    assert updated.last_checked_at == checked_at
    assert stored.status == "published"


@pytest.mark.asyncio
async def test_watch_topic_revision_tombstones_old_brief_derivatives(tmp_path):
    service, topic, _grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
    )
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="topic-revision",
        local_date=date(2026, 9, 20),
    )
    edition = await service.publish(_ready_publish_request(cycle))
    draft = await service.create_discussion_draft(
        edition.id,
        BriefDiscussionDraftCreateRequest(
            edition_revision=edition.revision,
            text="旧主题范围下的讨论草稿",
            action_id="topic-revision-draft",
        ),
    )

    updated = await ReminderService(ReminderRepository(tmp_path)).update_watch_topic(
        topic.id,
        WatchTopicUpdateRequest(
            title="AI 办公产品（修订范围）",
            expected_revision=topic.revision,
        ),
    )
    store = await DailyBriefRepository(tmp_path).load()
    withdrawn = next(item for item in store.editions if item.id == edition.id)
    invalidated = next(item for item in store.discussion_drafts if item.id == draft.id)

    assert updated is not None and updated.revision == topic.revision + 1
    assert withdrawn.status == "withdrawn"
    assert withdrawn.summary == ""
    assert invalidated.status == "invalidated"
    assert invalidated.text == ""
    assert all(
        receipt.state != "pending"
        for receipt in store.delivery_receipts
        if receipt.channel == "native"
    )
    with pytest.raises(BriefNotFound):
        await service.get_edition(edition.id)


@pytest.mark.asyncio
async def test_invalidated_understanding_pauses_topic_and_tombstones_derivatives(
    tmp_path,
):
    dependency_ref = "understanding:ua_deleted"
    service, topic, _grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
        topic_evidence_refs=[dependency_ref],
    )
    reminder_service = ReminderService(ReminderRepository(tmp_path))
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="understanding-invalidated",
        local_date=date(2026, 9, 20),
    )
    edition = await service.publish(_ready_publish_request(cycle))
    draft = await service.create_discussion_draft(
        edition.id,
        BriefDiscussionDraftCreateRequest(
            edition_revision=edition.revision,
            text="依赖已删除理解的讨论草稿",
            action_id="understanding-invalidated-draft",
        ),
    )

    invalidated_count = await reminder_service.invalidate_watch_topics_by_evidence_ref(
        dependency_ref,
    )
    reminders = await ReminderRepository(tmp_path).load()
    store = await DailyBriefRepository(tmp_path).load()
    saved_topic = next(item for item in reminders.watch_topics if item.id == topic.id)
    withdrawn = next(item for item in store.editions if item.id == edition.id)
    invalidated = next(item for item in store.discussion_drafts if item.id == draft.id)

    assert invalidated_count == 1
    assert saved_topic.brief_status == "paused"
    assert saved_topic.revision == topic.revision + 1
    assert saved_topic.metadata["brief_dependency_invalidated"] == dependency_ref
    assert withdrawn.status == "withdrawn"
    assert withdrawn.summary == ""
    assert invalidated.status == "invalidated"
    assert invalidated.text == ""
    assert all(
        receipt.state != "pending"
        for receipt in store.delivery_receipts
        if receipt.channel == "native"
    )


class _SuccessfulAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, _notification):
        self.calls += 1
        return DeliveryAdapterResult(
            state="submitted",
            adapter_message_id="native-message-1",
        )


class _UncertainAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, _notification):
        self.calls += 1
        raise TimeoutError("adapter response was lost")


@pytest.mark.asyncio
async def test_notification_disabled_quiet_unknown_and_submitted_are_distinct(tmp_path):
    service, _topic, _grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
    )
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="occurrence-delivery",
        local_date=date(2026, 9, 20),
    )
    await service.publish(_ready_publish_request(cycle))

    quiet = await BriefDeliveryWorker(
        tmp_path,
        adapter=_SuccessfulAdapter(),
    ).run_once(now=datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc))
    assert quiet.deferred_for_quiet_hours == 1

    uncertain = _UncertainAdapter()
    first = await BriefDeliveryWorker(tmp_path, adapter=uncertain).run_once(
        now=datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)
    )
    second = await BriefDeliveryWorker(tmp_path, adapter=uncertain).run_once(
        now=datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)
    )
    assert first.unknown == 1
    assert second.unknown == 0
    assert uncertain.calls == 1


@pytest.mark.asyncio
async def test_notification_does_not_cross_adapter_boundary_if_edition_withdraws_after_claim(
    tmp_path,
):
    service, _topic, _grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
    )
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="occurrence-withdraw-after-claim",
        local_date=date(2026, 9, 20),
    )
    await service.publish(_ready_publish_request(cycle))

    adapter = _SuccessfulAdapter()
    worker = BriefDeliveryWorker(tmp_path, adapter=adapter)

    async def withdrawn_edition(_edition_id):
        raise BriefNotFound("edition withdrawn")

    worker._service.get_edition = withdrawn_edition
    first = await worker.run_once(now=NOW)
    second = await worker.run_once(now=NOW)

    assert first.unknown == 1
    assert second.unknown == 0
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_missing_notification_adapter_is_suppressed_not_success(tmp_path):
    service, _topic, _grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
    )
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="occurrence-disabled-adapter",
        local_date=date(2026, 9, 20),
    )
    await service.publish(_ready_publish_request(cycle))

    result = await BriefDeliveryWorker(tmp_path).run_once(now=NOW)
    store = await DailyBriefRepository(tmp_path).load()
    native = next(item for item in store.delivery_receipts if item.channel == "native")

    assert result.suppressed == 1
    assert result.submitted == result.acknowledged == 0
    assert native.state == "suppressed"


@pytest.mark.asyncio
async def test_native_dispatch_claim_is_unknown_until_renderer_resolves_and_reads(
    tmp_path,
):
    service, _topic, _grant, subscription = await _provision_subscription(
        tmp_path,
        native_notifications=True,
    )
    cycle = await service.claim_occurrence(
        subscription_id=subscription.id,
        occurrence_id="occurrence-renderer-bridge",
        local_date=date(2026, 9, 20),
    )
    await service.publish(_ready_publish_request(cycle))

    dispatcher = NativeBriefDeliveryDispatcher(tmp_path)
    notification = await dispatcher.claim_next(now=NOW)
    assert notification is not None

    store = await DailyBriefRepository(tmp_path).load()
    claimed = next(
        item for item in store.delivery_receipts if item.id == notification.receipt_id
    )
    assert claimed.state == "unknown"
    assert await dispatcher.claim_next(now=NOW) is None

    submitted = await service.resolve_delivery(
        notification.receipt_id,
        state="submitted",
        adapter_message_id="electron-local",
    )
    assert submitted.state == "submitted"
    assert submitted.acknowledged_at is None
    assert submitted.read_at is None

    read = await service.mark_delivery_read(notification.receipt_id)
    assert read.state == "read"
    assert read.acknowledged_at is not None
    assert read.read_at is not None
