"""Synthetic public opportunity feedback concurrent with memory adoption."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import threading

import pytest

from lantai.agents.memory import memory_pack
from lantai.agents.persona.intervention_router import InterventionCandidate
from lantai.agents.persona.opportunity_queue import OpportunityStore


@pytest.mark.parametrize('feedback', ['completed', 'dismissed'])
def test_adoption_preserves_completed_opportunity_feedback(tmp_path, monkeypatch, feedback):
    target = tmp_path / 'target'
    target.mkdir()
    # The real memory manager keeps an OpportunityStore instance, and the
    # public feedback endpoint reuses it. This is the same live object here.
    store = OpportunityStore(target)
    item = store.enqueue_candidate(
        InterventionCandidate(
            kind='future_follow_up', content='synthetic deferred opportunity',
            relevance=0.9, urgency=0.2, expected_utility=0.9,
            confidence=0.9, relationship_resonance=0.7, novelty=0.8,
            intrusion_cost=0.35,
        ),
        source_ids=('source',),
    )
    assert item is not None and item.status == 'queued'
    source = tmp_path / 'source'
    shutil.copytree(target, source)
    pack = tmp_path / 'old-memory.zip'
    memory_pack.export_memory_pack(source, pack, agent_id='fixture')
    entered, release = threading.Event(), threading.Event()
    original_export = memory_pack.export_memory_pack

    def held_export(working_dir, destination, *, agent_id):
        if Path(working_dir).resolve() == target.resolve():
            entered.set()
            assert release.wait(5), 'fixture adoption release timed out'
        return original_export(working_dir, destination, agent_id=agent_id)

    monkeypatch.setattr(memory_pack, 'export_memory_pack', held_export)

    def adopt():
        return memory_pack.adopt_memory_pack(
            pack, target, agent_id='fixture', dry_run=False,
            confirmation=memory_pack.MEMORY_PACK_ADOPT_CONFIRMATION,
        )

    def decide():
        updated = store.apply_feedback(item.id, feedback)
        assert updated is not None and updated.status == feedback

    with ThreadPoolExecutor(max_workers=2) as workers:
        adoption = workers.submit(adopt)
        try:
            assert entered.wait(5), 'fixture adoption did not reach recovery export'
            decision = workers.submit(decide)
            # Correct writer fencing can leave the feedback pending until
            # publication completes; the test accepts that serialization.
            threading.Event().wait(0.2)
        finally:
            release.set()
        report = adoption.result(timeout=8)
        decision.result(timeout=8)
    assert report.adopted is True
    persisted = {value.id: value for value in OpportunityStore(target).list_all()}
    assert persisted[item.id].status == feedback, 'adoption revived an opportunity after acknowledged explicit feedback'
