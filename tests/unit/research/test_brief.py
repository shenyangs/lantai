"""Research briefs are versioned inputs, not invented local task classifications."""
import asyncio

import pytest

from lantai.research.projects import ResearchService
from lantai.research.review import review_report


def test_brief_is_persisted_and_passed_to_independent_review(tmp_path):
    service = ResearchService(tmp_path)
    p = service.create_project('What do we know?', [], idempotency_key='create',
        subquestions=['What was measured?', 'What remains uncertain?'],
        outline=['Findings', 'Limitations'])
    report = service.save_report(p['project_id'], 1, 'I prefer further investigation.', [], idempotency_key='save')
    payloads = []

    async def reviewer(payload):
        payloads.append(payload)
        return {'claims': [{'text': payload['report_text'], 'assessment': 'nonfactual',
                           'reason': 'Preference', 'evidence_indices': []}], 'issues': []}

    asyncio.run(review_report(service, p['project_id'], 1, report['id'], reviewer,
                              reviewer_identity={'provider': 'fixture', 'model': 'fixture'}))
    assert payloads[0]['subquestions'] == p['subquestions']
    assert payloads[0]['outline'] == ['Findings', 'Limitations']
    assert ResearchService(tmp_path).project(p['project_id'])['outline'] == p['outline']


def test_changing_brief_invalidates_current_review_and_preserves_history(tmp_path):
    service = ResearchService(tmp_path)
    p = service.create_project('Question', [], idempotency_key='create', subquestions=['Initial question'])
    report = service.save_report(p['project_id'], 1, 'Draft text.', [], idempotency_key='save')
    revised = service.revise_project(p['project_id'], 1, subquestions=['New question'],
                                    outline=['New section'], idempotency_key='revise')
    assert revised['revision'] == 2
    assert revised['report_id'] is None and revised['review_id'] is None
    assert service.report(report['id'])['text'] == 'Draft text.'
    assert service.store.get(p['id'])['subquestions'] == ['Initial question']
    cleared = service.revise_project(p['project_id'], 2, subquestions=[], outline=[], idempotency_key='clear')
    assert cleared['subquestions'] == [] and cleared['outline'] == []


@pytest.mark.parametrize('value', ['not a list', [42], [''], ['x' * 5001], ['item'] * 101])
def test_brief_rejects_invalid_entries_without_partial_project(tmp_path, value):
    service = ResearchService(tmp_path)
    with pytest.raises(ValueError, match='brief'):
        service.create_project('Question', [], subquestions=value, idempotency_key='bad')
    assert service.list_projects() == []


def test_empty_brief_retains_legacy_review_checkpoint_inputs(tmp_path):
    service = ResearchService(tmp_path)
    p = service.create_project('Question', [], idempotency_key='create')
    report = service.save_report(p['project_id'], 1, 'An opinion.', [], idempotency_key='save')
    calls = []

    async def reviewer(payload):
        calls.append(payload)
        assert 'subquestions' not in payload and 'outline' not in payload
        return {'claims': [{'text': payload['report_text'], 'assessment': 'nonfactual',
                           'reason': 'Opinion', 'evidence_indices': []}], 'issues': []}

    for _ in range(2):
        asyncio.run(review_report(service, p['project_id'], 1, report['id'], reviewer,
                                  reviewer_identity={'provider': 'fixture', 'model': 'fixture'}))
    assert len(calls) == 1
