"""Six evidence relations and independent critical-claim review behavior locks."""
import asyncio

import pytest

from lantai.research.projects import ResearchService
from lantai.research.review import review_report

IDENTITY = {'provider': 'fixture', 'model': 'fixture', 'method': 'full_report_review_v2'}


def setup(tmp_path, **budget):
    source = tmp_path / 'wiki/raw/sources/a.txt'
    source.parent.mkdir(parents=True)
    source.write_text('The first trial improved outcomes. The second found no improvement.')
    service = ResearchService(tmp_path)
    p = service.create_project('Compare results', ['wiki/raw/sources/a.txt'],
        idempotency_key='create', budget=budget or None)
    sid = p['source_versions'][0]
    service.read_source(p['project_id'], 1, sid, 0, 4)
    text = 'The first trial improved outcomes. Results disagree across trials.'
    report = service.save_report(p['project_id'], 1, text, [
        {'text': part, 'evidence': [{'source_version_id': sid, 'block_index': 0}]}
        for part in ('The first trial improved outcomes.', 'Results disagree across trials.')], idempotency_key='report')
    return service, p, report


def result(payload, assessment='supports', importance='normal', disposition='accepted'):
    return {'claims': [{'text': payload['report_text'], 'assessment': assessment,
        'importance': importance, 'disposition': disposition, 'reason': 'Test judgment',
        'evidence_indices': [0]}], 'issues': []}


def run(service, p, report, reviewer):
    return asyncio.run(review_report(service, p['project_id'], p['revision'], report['id'], reviewer,
        reviewer_identity=IDENTITY))


@pytest.mark.parametrize('assessment', ['partial_support', 'unrelated', 'uncertain'])
def test_new_relations_remain_unresolved(tmp_path, assessment):
    service, p, report = setup(tmp_path)
    async def reviewer(payload):
        return result(payload, assessment=assessment, disposition='unresolved')
    review = run(service, p, report, reviewer)
    assert review['status'] == 'needs_revision'
    assert review['claims'][0]['assessment'] == assessment


def test_disclosed_conflict_keeps_relation_and_exact_disclosure(tmp_path):
    service, p, report = setup(tmp_path)
    async def reviewer(payload):
        value = result(payload, assessment='contradicts', disposition='disclosed')
        value['claims'][0]['disclosure'] = {'text': 'Results disagree across trials.'}
        return value
    review = run(service, p, report, reviewer)
    assert review['status'] == 'reviewed'
    assert review['claims'][0]['assessment'] == 'contradicts'
    assert review['claims'][0]['disclosure']['start'] == report['text'].index('Results')


@pytest.mark.parametrize('alteration', ['missing_disclosure', 'invented_disclosure', 'accepted_contradiction', 'bool_index', 'missing_importance'])
def test_malformed_v2_cannot_pass(tmp_path, alteration):
    service, p, report = setup(tmp_path)
    async def reviewer(payload):
        value = result(payload)
        claim = value['claims'][0]
        if alteration in {'missing_disclosure', 'invented_disclosure'}:
            claim.update(assessment='contradicts', disposition='disclosed')
            if alteration == 'invented_disclosure':
                claim['disclosure'] = {'text': 'Invented caveat'}
        elif alteration == 'accepted_contradiction':
            claim['assessment'] = 'contradicts'
        elif alteration == 'bool_index':
            claim['evidence_indices'] = [False]
        else:
            del claim['importance']
        return value
    with pytest.raises(ValueError):
        run(service, p, report, reviewer)
    assert service.project(p['project_id'])['review_status'] == 'not_reviewed'


def test_critical_claim_receives_independent_call_and_disagreement_blocks(tmp_path):
    service, p, report = setup(tmp_path)
    calls = []
    async def reviewer(payload):
        calls.append(payload)
        if len(calls) == 1:
            return result(payload, importance='critical')
        assert payload['phase'] == 'critical_recheck'
        assert 'primary_assessment' not in payload
        assert payload['critical_claims'] == [{'start': 0, 'end': len(report['text']), 'text': report['text']}]
        return result(payload, assessment='insufficient', importance='critical', disposition='unresolved')
    review = run(service, p, report, reviewer)
    assert len(calls) == 2
    assert review['status'] == 'needs_revision'
    assert review['critical_review']['claims'][0]['assessment'] == 'insufficient'
    assert service.project(p['project_id'])['budget_usage']['review_calls'] == 2


def test_second_review_budget_failure_resumes_primary_checkpoint(tmp_path):
    service, p, report = setup(tmp_path, review_calls=1)
    calls = []
    async def reviewer(payload):
        calls.append(payload)
        return result(payload, importance='critical')
    with pytest.raises(ValueError, match='budget'):
        run(service, p, report, reviewer)
    assert len(service.store.list('reviewpart', p['project_id'])) == 1
    service = ResearchService(tmp_path)
    p = service.revise_project(p['project_id'], 1, budget={'review_calls': 2}, idempotency_key='raise')
    review = run(service, p, report, reviewer)
    assert review['status'] == 'reviewed'
    assert len(calls) == 2
    assert calls[-1]['phase'] == 'critical_recheck'


def test_reused_evidence_span_projects_both_claim_assessments(tmp_path):
    from lantai.runtime.research_delivery import project_research_evidence_graph
    service, p, report = setup(tmp_path)
    async def reviewer(payload):
        return {'claims': [{**result(payload)['claims'][0], 'text': c['text']} for c in report['claims']], 'issues': []}
    run(service, p, report, reviewer)
    graph = project_research_evidence_graph(service, p['project_id'])
    assert [node.support_status for node in graph.nodes.values()] == ['supports', 'supports']


def test_missing_critical_recheck_cannot_be_saved(tmp_path):
    service, p, report = setup(tmp_path)
    async def reviewer(payload):
        if payload.get('phase') == 'critical_recheck':
            return {'claims': [], 'issues': []}
        return result(payload, importance='critical')
    with pytest.raises(ValueError, match='omitted'):
        run(service, p, report, reviewer)
    assert service.project(p['project_id'])['review_status'] == 'not_reviewed'


def test_export_distinguishes_disclosed_conflict_from_unresolved_claim(tmp_path):
    from lantai.research.exports import export_report, export_file
    service, p, report = setup(tmp_path)
    async def reviewer(payload):
        value = result(payload, assessment='contradicts', disposition='disclosed')
        value['claims'][0]['disclosure'] = {'text': 'Results disagree across trials.'}
        return value
    run(service, p, report, reviewer)
    artifact = export_report(service, p['project_id'], 1, report['id'], 'markdown', 'numeric', 'export')
    text = export_file(service, artifact['id']).read_text()
    assert '已披露的证据分歧' in text
    assert '待处理结论' not in text
