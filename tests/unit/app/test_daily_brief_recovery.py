# -*- coding: utf-8 -*-
"""Behavior locks for the brief generation incidents of 2026-10-03."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from tests.unit.app.test_daily_brief_domain import (
    NOW, _enable_flags, _provision_subscription, _ready_publish_request, _seed_topic,
)
from lantai.app.daily_brief.models import BriefGrantCreateRequest, utc_now
from lantai.app.daily_brief.repo import DailyBriefRepository
from lantai.app.daily_brief.research import DailyBriefResearchRunner
from lantai.app.daily_brief.scheduler import DailyBriefScheduler
from lantai.app.daily_brief.service import BriefConflict, DailyBriefService, ScopeFenceFailed


async def _preview(tmp_path, action="preview"):
    await _seed_topic(tmp_path)
    await _enable_flags(tmp_path, research=True, native=False)
    service = DailyBriefService(tmp_path)
    grant = await service.create_grant(BriefGrantCreateRequest(
        explicit_consent=True, one_time=True, topic_ids=["topic-1"],
        public_source_discovery=True, public_source_read=True,
        cloud_processing="public_only", action_id=action,
    ))
    cycle = await service.create_preview_cycle(
        grant_id=grant.id, local_date=date(2026, 9, 20), action_id=action + ":cycle",
    )
    return service, cycle


@pytest.mark.asyncio
async def test_second_occurrence_claim_cannot_run_same_attempt(tmp_path):
    service, _, _, subscription = await _provision_subscription(tmp_path)
    args = dict(subscription_id=subscription.id, occurrence_id="occurrence", local_date=NOW.date())
    await service.claim_occurrence(**args)
    with pytest.raises(BriefConflict):
        await service.claim_occurrence(**args)


@pytest.mark.asyncio
async def test_distinct_preview_grants_on_same_day_keep_both_editions(tmp_path):
    service, first = await _preview(tmp_path, "first")
    await service.claim_preview_cycle(first.id)
    edition = await service.publish(_ready_publish_request(first))
    service, second = await _preview(tmp_path, "second")
    await service.claim_preview_cycle(second.id)
    other = await service.publish(_ready_publish_request(second))
    assert other.id != edition.id
    store = await DailyBriefRepository(tmp_path).load()
    assert len([entry for entry in store.editions if entry.status == "published"]) == 2


@pytest.mark.asyncio
async def test_v1_preview_collision_migrates_with_exact_revision_links_and_backup(tmp_path):
    from lantai.app.daily_brief.models import BriefDiscussionDraftCreateRequest
    service, first = await _preview(tmp_path, "first")
    await service.claim_preview_cycle(first.id)
    earlier = await service.publish(_ready_publish_request(first))
    draft = await service.create_discussion_draft(earlier.id, BriefDiscussionDraftCreateRequest(
        edition_revision=1, item_id=earlier.items[0].id, text="继续", action_id="old-draft"))
    service, second = await _preview(tmp_path, "second")
    await service.claim_preview_cycle(second.id)
    later = await service.publish(_ready_publish_request(second))
    repo = DailyBriefRepository(tmp_path)
    data = (await repo.load()).model_copy(update={"version": 1, "editions": [
        later.model_copy(update={"id": earlier.id, "revision": 2}),
        earlier.model_copy(update={"status": "superseded"})]})
    original = data.model_dump_json()
    repo.path.write_text(original)
    await repo.update(lambda current: current)
    assert len(await service.list_editions()) == 2
    restored = await service.get_edition(earlier.id, 1)
    assert restored.grant_id == earlier.grant_id
    assert restored.id != (await service.get_edition(earlier.id, 2)).id
    assert (await service.get_discussion_draft(draft.id)).context_ref.edition_id == restored.id
    assert (repo.root / "daily_brief.v1.backup.json").read_text() == original


@pytest.mark.asyncio
async def test_cancelled_generation_has_recoverable_terminal_state(tmp_path):
    service, cycle = await _preview(tmp_path)
    started = asyncio.Event()

    class BlockingAdapter:
        async def run(self, envelope):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(DailyBriefResearchRunner(tmp_path, adapter=BlockingAdapter()).run_preview(cycle.id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store = await DailyBriefRepository(tmp_path).load()
    assert store.cycles[0].status == "failed"
    assert store.cycles[0].failure_reason == "interrupted"


@pytest.mark.asyncio
async def test_stale_subscription_does_not_strand_healthy_claim(tmp_path):
    service, _, _, subscription = await _provision_subscription(tmp_path)
    repository = DailyBriefRepository(tmp_path)
    await repository.update(lambda data: data.model_copy(update={"subscriptions": [
        subscription.model_copy(update={"id": "stale", "grant_revision": 99}), subscription,
    ]}))
    claimed = await DailyBriefScheduler(tmp_path).tick(now=NOW)
    assert [cycle.subscription_id for cycle in claimed] == [subscription.id]


@pytest.mark.asyncio
async def test_worker_starts_subscription_before_claiming_the_next_waiter(tmp_path):
    from lantai.app.daily_brief.lifecycle import DailyBriefWorker
    from lantai.app.daily_brief.models import BriefCostRecord
    from lantai.app.daily_brief.research import BriefResearchResult
    service, _, _, subscription = await _provision_subscription(tmp_path)
    repository = DailyBriefRepository(tmp_path)
    await repository.update(lambda data: data.model_copy(update={"subscriptions": [
        subscription, subscription.model_copy(update={"id": "second-subscription"}),
    ]}))
    calls = []
    class Adapter:
        async def run(self, envelope):
            store = await repository.load()
            running = [cycle for cycle in store.cycles if cycle.status == "running"]
            assert len(running) == 1, "waiting subscriptions must not spend their lease before execution"
            calls.append(envelope.cycle_id)
            return BriefResearchResult(publish_request=_ready_publish_request(running[0]),
                cost=BriefCostRecord(cycle_id=envelope.cycle_id))
    tick = await DailyBriefWorker(tmp_path, adapter=Adapter()).run_once(now=NOW)
    assert tick.published_count == 2 and tick.failed_count == 0
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_attempt_retry_requires_consent_and_keeps_history(tmp_path):
    service, cycle = await _preview(tmp_path)
    await service.claim_preview_cycle(cycle.id)
    await service.fail_cycle(cycle.id, "interrupted")
    with pytest.raises(Exception, match="consent"):
        await service.retry_cycle(cycle.id, action_id="retry", explicit_consent=False)
    retried = await service.retry_cycle(cycle.id, action_id="retry", explicit_consent=True)
    assert retried.id != cycle.id
    assert retried.logical_key == cycle.logical_key
    assert retried.status == "queued"
    assert (await service.retry_cycle(cycle.id, action_id="retry", explicit_consent=True)).id == retried.id
    store = await DailyBriefRepository(tmp_path).load()
    assert next(entry for entry in store.cycles if entry.id == cycle.id).status == "failed"


@pytest.mark.asyncio
async def test_expired_execution_recovery_never_replays_research(tmp_path):
    service, cycle = await _preview(tmp_path)
    claimed = await service.claim_preview_cycle(cycle.id)
    await DailyBriefRepository(tmp_path).update(lambda data: data.model_copy(update={"cycles": [
        claimed.model_copy(update={"lease_expires_at": utc_now() - timedelta(seconds=1)}),
    ]}))
    assert await service.recover_interrupted_cycles() == 1
    store = await DailyBriefRepository(tmp_path).load()
    assert store.cycles[0].status == "failed"
    assert store.cycles[0].failure_reason == "interrupted"


@pytest.mark.asyncio
async def test_expired_execution_cannot_renew_before_recovery_runs(tmp_path):
    service, queued = await _preview(tmp_path)
    claimed = await service.claim_preview_cycle(queued.id)
    cycle = await service.begin_execution(claimed.id, claimed.execution_token)
    expired_at = utc_now() - timedelta(seconds=1)
    await DailyBriefRepository(tmp_path).update(lambda data: data.model_copy(update={"cycles": [
        cycle.model_copy(update={"lease_expires_at": expired_at}),
    ]}))

    with pytest.raises(ScopeFenceFailed):
        await service.heartbeat(cycle.id, cycle.execution_token)
    with pytest.raises(ScopeFenceFailed):
        await service.publish(_ready_publish_request(cycle).model_copy(update={
            "execution_token": cycle.execution_token,
        }))
