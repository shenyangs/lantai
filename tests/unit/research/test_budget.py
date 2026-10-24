"""Project budgets stop new work without discarding drafts or review checkpoints."""
import asyncio

import pytest

from lantai.research.projects import ResearchService
from lantai.research.review import review_report


def setup_project(tmp_path, **budget):
    path = tmp_path / 'wiki/raw/sources/budget.txt'
    path.parent.mkdir(parents=True)
    path.write_text('A' * 7000, encoding='utf-8')
    service = ResearchService(tmp_path)
    project = service.create_project('Check reading and review limits',
        ['wiki/raw/sources/budget.txt'], idempotency_key='create', budget=budget)
    return service, project


def test_read_budget_counts_unique_characters_and_preserves_progress(tmp_path):
    service, p = setup_project(tmp_path, reading_chars=6000)
    sid, pid = p['source_versions'][0], p['project_id']
    service.read_source(pid, 1, sid, 0, 1)
    service.read_source(pid, 1, sid, 0, 1)
    state = service.project(pid)
    assert state['budget_usage']['reading_chars'] == 6000
    with pytest.raises(ValueError, match='budget'):
        service.read_source(pid, 1, sid, 1, 1)
    assert service.project(pid)['reading']['read_blocks'] == 1
    assert service.project(pid)['review_status'] == 'not_reviewed'


def test_budget_exhaustion_retains_review_checkpoints_and_draft(tmp_path):
    service, p = setup_project(tmp_path, review_calls=1)
    pid, sid = p['project_id'], p['source_versions'][0]
    service.read_source(pid, 1, sid, 0, 4)
    report = service.save_report(pid, 1, 'First preference.\n\nSecond preference.', [], idempotency_key='report')
    calls = []

    async def reviewer(payload):
        calls.append(payload['range'])
        return {'claims': [{'text': payload['report_text'], 'assessment': 'nonfactual',
                            'reason': 'Preference', 'evidence_indices': []}], 'issues': []}

    async def run():
        await review_report(service, pid, 1, report['id'], reviewer,
            reviewer_identity={'provider': 'fixture', 'model': 'fixture'}, chunk_chars=19)

    with pytest.raises(ValueError, match='budget'):
        asyncio.run(run())
    with pytest.raises(ValueError, match='budget'):
        asyncio.run(run())
    assert len(calls) == 1
    assert len(service.store.list('reviewpart', pid)) == 1
    assert service.project(pid)['report_id'] == report['id']
    assert service.project(pid)['review_status'] == 'not_reviewed'


def test_failed_review_attempt_still_consumes_call_budget(tmp_path):
    service, p = setup_project(tmp_path, review_calls=1)
    report = service.save_report(p['project_id'], 1, 'A preference.', [], idempotency_key='report')
    calls = []

    async def failure(payload):
        calls.append(payload)
        raise RuntimeError('Provider interrupted')

    async def run():
        await review_report(service, p['project_id'], 1, report['id'], failure,
            reviewer_identity={'provider': 'fixture', 'model': 'fixture'})

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    with pytest.raises(ValueError, match='budget'):
        asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize('budget', [{'reading_chars': -1}, {'review_calls': True},
                                  {'review_seconds': 0}, {'arbitrary': 10}])
def test_budget_rejects_invalid_limits(tmp_path, budget):
    with pytest.raises(ValueError, match='budget'):
        setup_project(tmp_path, **budget)


def test_raising_budget_resumes_only_unfinished_review_chunks_after_restart(tmp_path):
    service, p = setup_project(tmp_path, review_calls=1)
    pid, sid = p['project_id'], p['source_versions'][0]
    service.read_source(pid, 1, sid, 0, 4)
    report = service.save_report(pid, 1, 'First preference.\n\nSecond preference.', [], idempotency_key='report')
    calls = []

    async def reviewer(payload):
        calls.append(payload['range'])
        return {'claims': [{'text': payload['report_text'], 'assessment': 'nonfactual',
                            'reason': 'Preference', 'evidence_indices': []}], 'issues': []}

    async def run(revision):
        return await review_report(service, pid, revision, report['id'], reviewer,
            reviewer_identity={'provider': 'fixture', 'model': 'fixture'}, chunk_chars=19)

    with pytest.raises(ValueError, match='budget'):
        asyncio.run(run(1))
    service = ResearchService(tmp_path)
    revised = service.revise_project(pid, 1, budget={'review_calls': 2}, idempotency_key='raise-budget')
    assert revised['report_id'] == report['id']
    assert service.project(pid)['reading']['read_blocks'] == 2
    assert asyncio.run(run(2))['status'] == 'reviewed'
    assert len(calls) == 2
    assert service.project(pid)['budget_usage']['review_calls'] == 2
    assert service.project(pid)['budget_remaining']['review_calls'] == 0


def test_reading_consumption_survives_project_revisions(tmp_path):
    service, p = setup_project(tmp_path, reading_chars=6000)
    pid, sid = p['project_id'], p['source_versions'][0]
    service.read_source(pid, 1, sid, 0, 1)
    service.revise_project(pid, 1, budget={'reading_chars': 7000}, idempotency_key='raise')
    service.read_source(pid, 2, sid, 1, 1)
    assert service.project(pid)['budget_usage']['reading_chars'] == 7000


def test_concurrent_review_cannot_overspend_and_cancellation_is_durable(tmp_path):
    service, p = setup_project(tmp_path, review_calls=1)
    report = service.save_report(p['project_id'], 1, 'Preference.', [], idempotency_key='report')
    calls = []

    async def run():
        entered = asyncio.Event()

        async def reviewer(payload):
            calls.append(payload)
            entered.set()
            await asyncio.Event().wait()

        async def review():
            return await review_report(service, p['project_id'], 1, report['id'], reviewer,
                reviewer_identity={'provider': 'fixture', 'model': 'fixture'})
        task = asyncio.create_task(review())
        await entered.wait()
        try:
            with pytest.raises(ValueError, match='budget'):
                await review()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert len(calls) == 1
    assert ResearchService(tmp_path).project(p['project_id'])['budget_usage']['review_calls'] == 1


def test_review_time_budget_cancels_provider_and_retains_draft(tmp_path):
    service, p = setup_project(tmp_path, review_seconds=1)
    report = service.save_report(p['project_id'], 1, 'Preference.', [], idempotency_key='report')
    cancelled = []

    async def reviewer(payload):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)

    with pytest.raises(ValueError, match='time budget'):
        asyncio.run(review_report(service, p['project_id'], 1, report['id'], reviewer,
            reviewer_identity={'provider': 'fixture', 'model': 'fixture'}))
    state = service.project(p['project_id'])
    assert cancelled == [True]
    assert state['report_id'] == report['id']
    assert state['budget_remaining']['review_seconds'] == 0


def test_budget_usage_does_not_reload_previously_read_source_text(tmp_path, monkeypatch):
    from lantai.research.budget import usage
    service, p = setup_project(tmp_path)
    service.read_source(p['project_id'], 1, p['source_versions'][0], 0, 1)

    def unexpected_load(*args, **kwargs):
        raise AssertionError('Budget accounting must not reload source payloads')

    monkeypatch.setattr(service.store, 'get', unexpected_load)
    with service.store.transaction() as db:
        assert usage(service.store, db, p['project_id'])['reading_chars'] == 6000


def test_prebudget_database_migration_counts_reading_once_across_revisions(tmp_path):
    service, p = setup_project(tmp_path)
    pid, sid = p['project_id'], p['source_versions'][0]
    service.read_source(pid, 1, sid, 0, 1)
    service.revise_project(pid, 1, budget={'reading_chars': 7000}, idempotency_key='raise')
    with service.store.transaction() as db:
        db.execute('DROP TABLE IF EXISTS research_reading_usage')
        db.execute('PRAGMA user_version=0')
    restarted = ResearchService(tmp_path)
    assert restarted.project(pid)['budget_usage']['reading_chars'] == 6000
    restarted.read_source(pid, 2, sid, 1, 1)
    assert ResearchService(tmp_path).project(pid)['budget_usage']['reading_chars'] == 7000


def test_unsettled_review_reservation_survives_restart(tmp_path):
    from lantai.research.budget import reserve_review
    service, p = setup_project(tmp_path, review_calls=2, review_seconds=10)
    pid = p['project_id']
    report = service.save_report(pid, 1, 'A preference.', [], idempotency_key='report')
    reserve_review(service, pid, 1, report['id'])
    restarted = ResearchService(tmp_path)
    assert restarted.project(pid)['budget_usage'] == {
        'reading_chars': 0, 'review_calls': 1, 'review_seconds': 10,
        'model_calls': 0, 'model_tokens': 0, 'model_seconds': 0}
    with pytest.raises(ValueError, match='budget'):
        reserve_review(restarted, pid, 1, report['id'])


def test_prebudget_idempotency_receipts_remain_retryable(tmp_path):
    from lantai.research.evidence import _digest
    from lantai.research.store import canonical
    service = ResearchService(tmp_path)
    p = service.create_project('Legacy project', [], idempotency_key='create')
    legacy_create = dict(question='Legacy project', paths=[], policy='local_sources',
                         scope='', source_levels={}, exclusions='', output='report')
    with service.store.transaction() as db:
        db.execute('UPDATE requests SET hash=? WHERE key=?',
                   (_digest(canonical(legacy_create).encode()), 'create'))
    assert service.create_project('Legacy project', [], idempotency_key='create')['id'] == p['id']
    revised = service.revise_project(p['project_id'], 1, question='Revised question', idempotency_key='revise')
    legacy_revision = dict(project=p['project_id'], revision=1, question='Revised question',
                           paths=None, policy=None, refresh=False, scope=None, exclusions=None, source_levels=None)
    with service.store.transaction() as db:
        db.execute('UPDATE requests SET hash=? WHERE key=?',
                   (_digest(canonical(legacy_revision).encode()), 'revise'))
    assert service.revise_project(p['project_id'], 1, question='Revised question', idempotency_key='revise')['id'] == revised['id']
