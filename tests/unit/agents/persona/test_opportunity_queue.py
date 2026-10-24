from datetime import datetime, timedelta, timezone

from lantai.agents.persona.intervention_router import InterventionCandidate
from lantai.agents.persona.opportunity_queue import OpportunityStore


def _candidate(*, urgency: float = 0.2) -> InterventionCandidate:
    return InterventionCandidate(
        kind="recommendation",
        content="把上次没做完的记忆纠错入口补上",
        relevance=0.9,
        urgency=urgency,
        expected_utility=0.8,
        confidence=0.9,
        relationship_resonance=0.7,
        novelty=0.7,
        intrusion_cost=0.35,
    )


def test_queue_allows_zero_and_only_accepts_opportunity_route(tmp_path) -> None:
    store = OpportunityStore(tmp_path)

    assert store.next_eligible() is None
    queued = store.enqueue_candidate(
        _candidate(),
        source_ids=("task:memory-ui",),
    )
    direct = store.enqueue_candidate(
        _candidate(urgency=0.9),
        source_ids=("task:urgent",),
    )

    assert queued is not None
    assert direct is None
    assert store.next_eligible().content == queued.content


def test_expired_completed_and_source_invalidated_opportunities_disappear(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    store = OpportunityStore(tmp_path)
    expired = store.enqueue_candidate(
        _candidate(),
        source_ids=("task:expired",),
        now=now,
        expires_at=(now - timedelta(seconds=1)).isoformat(),
    )
    active = store.enqueue_candidate(
        InterventionCandidate(**{**_candidate().__dict__, "content": "跟进发行检查"}),
        source_ids=("task:release",),
        now=now,
    )

    assert expired is not None and active is not None
    assert store.next_eligible(now=now).id == active.id
    store.apply_feedback(active.id, "completed", now=now)
    assert store.next_eligible(now=now) is None

    replacement = store.enqueue_candidate(
        InterventionCandidate(**{**_candidate().__dict__, "content": "重看用户测评"}),
        source_ids=("artifact:eval",),
        now=now,
    )
    assert replacement is not None
    store.invalidate_source("artifact:eval")
    assert store.next_eligible(now=now) is None


def test_repeated_ignore_creates_cooldown_then_retires_opportunity(tmp_path) -> None:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    store = OpportunityStore(tmp_path)
    item = store.enqueue_candidate(
        _candidate(),
        source_ids=("task:memory-ui",),
        now=now,
    )
    assert item is not None

    store.apply_feedback(item.id, "ignored", now=now)
    assert store.next_eligible(now=now + timedelta(hours=1)) is None
    assert store.next_eligible(now=now + timedelta(days=2)) is not None

    store.apply_feedback(item.id, "ignored", now=now + timedelta(days=2))
    store.apply_feedback(item.id, "ignored", now=now + timedelta(days=5))
    assert store.next_eligible(now=now + timedelta(days=20)) is None
