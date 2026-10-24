"""Overlapping citations must not multiply the review's source context budget."""
import asyncio

import pytest

from lantai.research.projects import ResearchService
from lantai.research.review import review_report
from tests.unit.research.test_review_v2 import IDENTITY, result, setup


def project_with_citations(tmp_path, texts, quotes):
    root = tmp_path / 'wiki/raw/sources'
    root.mkdir(parents=True)
    paths = []
    for i, text in enumerate(texts):
        path = root / f'{i}.txt'
        path.write_text(text, encoding='utf-8')
        paths.append(str(path.relative_to(tmp_path)))
    service = ResearchService(tmp_path)
    p = service.create_project('Compare all observations', paths, idempotency_key='create')
    for sid in p['source_versions']:
        next_block = 0
        while next_block is not None:
            next_block = service.read_source(p['project_id'], 1, sid, next_block, 4)['next_block']
    claims = [{'text': f'Synthetic claim {i:04d}.', 'evidence': [
        {'source_version_id': p['source_versions'][source], 'quote': quote}]}
        for i, (source, quote) in enumerate(quotes)]
    report = service.save_report(p['project_id'], 1, '\n'.join(c['text'] for c in claims),
        claims, idempotency_key='report')
    return service, p, report


def invoke(service, p, report, reviewer, **kwargs):
    return asyncio.run(review_report(service, p['project_id'], 1, report['id'], reviewer,
        reviewer_identity=IDENTITY, **kwargs))


def assert_lossless(payload, texts_by_source):
    groups = payload['evidence_contexts']
    assert payload['evidence_context_contract'] == 1
    for group in groups:
        assert group['text'] == texts_by_source[group['source_version_id']][group['start']:group['end']]
    for item in payload['evidence']:
        text = texts_by_source[item['source_version_id']]
        group = groups[item['context_index']]
        assert group['source_version_id'] == item['source_version_id']
        assert item['context_start'] == max(0, item['start'] - 1500)
        assert item['context_end'] == min(len(text), item['end'] + 1500)
        assert group['start'] <= item['context_start'] < item['context_end'] <= group['end']
        a, b = item['context_start'] - group['start'], item['context_end'] - group['start']
        assert group['text'][a:b] == text[item['context_start']:item['context_end']]
        assert item['quote'] == text[item['start']:item['end']]
        assert 'context' not in item  # The repeated original text must leave the model payload.


def test_dense_citations_preserve_all_context_full_scan_and_critical_recheck(tmp_path):
    text = ''.join(f'Synthetic observation {i:04d}: measured value is {i:04d}; no causal inference.\n'
        for i in range(100))
    service, p, report = project_with_citations(tmp_path, [text],
        [(0, f'Synthetic observation {i:04d}') for i in range(60)])
    calls = []
    async def reviewer(payload):
        calls.append(payload)
        assert_lossless(payload, {p['source_versions'][0]: text})
        assert len(payload['evidence']) == 60
        assert len(payload['evidence_contexts']) == 1
        assert len(payload['evidence_contexts'][0]['text']) == 5833
        assert [e['index'] for e in payload['evidence']] == list(range(60))
        for i, item in enumerate(payload['evidence']):
            claim = report['claims'][i]
            assert item['claim_ranges'] == [[claim['start'], claim['end']]]
        # An independent recheck may disagree; transport compression must not hide it.
        if payload.get('phase') == 'critical_recheck':
            return result(payload, assessment='insufficient', importance='critical', disposition='unresolved')
        return result(payload, importance='critical')
    review = invoke(service, p, report, reviewer, chunk_chars=700)
    scans = [c for c in calls if c.get('phase') != 'critical_recheck']
    assert ''.join(c['report_text'] for c in scans) == report['text']
    assert len(scans) == 2 and len(calls) == 4
    assert review['status'] == 'needs_revision'
    assert review['critical_review']['required_claims'] == 2
    assert service.report(report['id']) == report


def test_context_union_never_merges_sources_or_fills_uncited_gaps(tmp_path):
    first = 'FIRST' + 'x' * 4000 + 'MIDDLE' + 'y' * 4000 + 'LAST'
    second = 'SECOND' + first
    service, p, report = project_with_citations(tmp_path, [first, second],
        [(0, 'LAST'), (1, 'FIRST'), (0, 'FIRST'), (0, 'MIDDLE')])
    async def reviewer(payload):
        assert_lossless(payload, dict(zip(p['source_versions'], [first, second])))
        assert len(payload['evidence_contexts']) == 4
        return result(payload)
    invoke(service, p, report, reviewer)


def test_genuinely_large_context_still_rejected_before_review_reservation(tmp_path):
    text = ''.join(f'Unique evidence {i:04d}.' + 'x' * 4000 for i in range(45))
    service, p, report = project_with_citations(tmp_path, [text],
        [(0, f'Unique evidence {i:04d}.') for i in range(45)])
    async def forbidden(payload):
        pytest.fail('Oversized context must not reach the reviewer')
    with pytest.raises(ValueError, match='exceeds context budget'):
        invoke(service, p, report, forbidden)
    assert service.project(p['project_id'])['budget_usage']['review_calls'] == 0
    assert service.project(p['project_id'])['review_id'] is None


def test_context_contract_change_invalidates_review_and_checkpoints(tmp_path, monkeypatch):
    from lantai.research import review as module
    service, p, report = setup(tmp_path)
    contracts = []
    async def reviewer(payload):
        contracts.append(payload['evidence_context_contract'])
        return result(payload)
    monkeypatch.setattr(module, 'EVIDENCE_CONTEXT_CONTRACT', 0)
    old = invoke(service, p, report, reviewer)
    monkeypatch.setattr(module, 'EVIDENCE_CONTEXT_CONTRACT', 1)
    current = invoke(service, p, report, reviewer)
    repeated = invoke(service, p, report, reviewer)
    assert old['id'] != current['id'] == repeated['id']
    assert old['checkpoint_ids'] != current['checkpoint_ids']
    assert contracts == [0, 1]
    assert 'evidence_context_contract' not in IDENTITY
