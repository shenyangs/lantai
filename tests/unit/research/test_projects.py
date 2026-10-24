"""Research behavior locks: versions, full review, and source reading are distinct."""
import asyncio
from pathlib import Path

import pytest

from lantai.research.projects import ResearchService, RevisionConflict


def test_explicit_abstract_level_survives_refresh_and_rejects_conflicting_duplicates(tmp_path):
    folder = tmp_path / 'wiki/raw/sources'
    folder.mkdir(parents=True)
    first, second = folder / '摘要.txt', folder / '相同内容.txt'
    first.write_text('这里只记录论文摘要，不包含实验细节。', encoding='utf-8')
    second.write_bytes(first.read_bytes())
    a, b = str(first.relative_to(tmp_path)), str(second.relative_to(tmp_path))
    service = ResearchService(tmp_path)
    project = service.create_project('检查摘要证据边界', [a], source_levels={a: 'abstract'}, idempotency_key='abstract')
    assert service.read_source(project['project_id'], 1, project['source_versions'][0], 0)['evidence_level'] == 'abstract'
    refreshed = service.revise_project(project['project_id'], 1, refresh_sources=True, idempotency_key='refresh')
    assert service.source(refreshed['source_versions'][0])['evidence_level'] == 'abstract'
    with pytest.raises(ValueError, match='conflicting'):
        service.create_project('相同文件不能冒充两种证据', [a, b], source_levels={a: 'abstract', b: 'fulltext'}, idempotency_key='conflict')
    with pytest.raises(ValueError, match='outside'):
        service.create_project('拒绝范围外的证据级别', [a], source_levels={b: 'abstract'}, idempotency_key='outside')


@pytest.fixture
def service(tmp_path):
    source = tmp_path / 'wiki/raw/sources/a.txt'
    source.parent.mkdir(parents=True)
    source.write_text('Observational study: treatment and outcome were correlated.\n\n'
                      'A randomized follow-up found no improvement.', encoding='utf-8')
    return ResearchService(tmp_path)


def project(service, key='create'):
    return service.create_project('Does treatment improve outcomes?',
                                  ['wiki/raw/sources/a.txt'], idempotency_key=key)


def test_reading_never_establishes_semantic_support(service):
    p = project(service)
    source = service.source(p['source_versions'][0])
    service.read_source(p['project_id'], 1, source['id'], 0, 1)
    state = service.project(p['project_id'])
    assert state['reading']['read_blocks'] == 1
    assert state['review_status'] == 'not_reviewed'
    assert state['reading']['total_blocks'] >= 1


def test_read_block_citation_preserves_exact_source_without_model_transcription(service):
    p = project(service)
    sid = p['source_versions'][0]
    text = 'The observational result does not prove causation.'
    claims = [{'text': text, 'evidence': [{'source_version_id': sid, 'block_index': 0}]}]
    with pytest.raises(ValueError, match='read'):
        service.save_report(p['project_id'], 1, text, claims, idempotency_key='unread-block')
    reading = service.read_source(p['project_id'], 1, sid, 0, 4)
    assert reading['next_block'] is None
    report = service.save_report(p['project_id'], 1, text, claims, idempotency_key='read-block')
    evidence = report['claims'][0]['evidence'][0]
    block = reading['blocks'][0]
    assert evidence['quote'] == block['text']
    assert (evidence['start'], evidence['end']) == (block['start'], block['end'])
    assert report['review_status'] == 'not_reviewed'
    updated = service.save_report(p['project_id'], 1, text, report['claims'],
        expected_report_id=report['id'], idempotency_key='edit-block-report')
    for index, extra in enumerate(({'quote': 'invented'}, {'start': 0}, {'block_index': -1}, {'block_index': True}, {'block_index': 999})):
        invalid = [{'text': text, 'evidence': [{'source_version_id': sid, 'block_index': 0, **extra}]}]
        with pytest.raises(ValueError):
            service.save_report(p['project_id'], 1, text, invalid,
                expected_report_id=updated['id'], idempotency_key=f'invalid-block-{index}')


def test_runtime_evidence_projection_only_consumes_current_controlled_review(service):
    from lantai.runtime.research_delivery import project_research_evidence_graph
    from lantai.research.review import review_report
    p = project(service)
    source = service.source(p['source_versions'][0])
    service.read_source(p['project_id'], 1, source['id'], 0, 4)
    text = 'The outcome was correlated with treatment.'
    report = service.save_report(p['project_id'], 1, text,
        [{'text': text, 'evidence': [{'source_version_id': source['id'], 'quote': source['text']}]}],
        idempotency_key='projection-report')
    graph = project_research_evidence_graph(service, p['project_id'])
    node = next(iter(graph.nodes.values()))
    assert node.citation_status == 'verified_original'
    assert node.support_status == 'unreviewed'
    async def reviewer(data):
        return {'claims': [{'text': text, 'assessment': 'supports', 'reason': 'Bounded association.',
                            'evidence_indices': [0]}], 'issues': []}
    review = asyncio.run(review_report(service, p['project_id'], 1, report['id'], reviewer,
        reviewer_identity={'provider': 'controlled-fixture', 'model': 'semantic-test'}))
    reviewed = project_research_evidence_graph(service, p['project_id'])
    checked = next(iter(reviewed.nodes.values()))
    assert checked.support_status == 'supports'
    assert checked.controlled_review_id == review['id']
    assert checked.report_hash == report['text_hash']
    assert reviewed.graph_hash != graph.graph_hash
    Path(service.workspace, source['path']).write_text('Changed source.', encoding='utf-8')
    stale = project_research_evidence_graph(service, p['project_id'])
    assert next(iter(stale.nodes.values())).support_status == 'unreviewed'
    assert stale.open_issues


def test_idempotency_and_revision_conflict(service):
    p = project(service)
    assert project(service)['project_id'] == p['project_id']
    with pytest.raises(ValueError, match='idempotency'):
        service.create_project('different', [], idempotency_key='create')
    updated = service.revise_project(p['project_id'], 1, question='New question',
                                     idempotency_key='revision')
    assert updated['revision'] == 2
    with pytest.raises(RevisionConflict):
        service.revise_project(p['project_id'], 1, question='Stale edit',
                               idempotency_key='stale')
    reopened = ResearchService(service.workspace)
    assert reopened.project(p['project_id'])['revision'] == 2


def test_quote_integrity_and_report_edit_invalidate(service):
    p = project(service)
    source = service.source(p['source_versions'][0])
    citation = {'source_version_id': source['id'], 'start': 0, 'end': 19,
                'quote': source['text'][:19]}
    r = service.save_report(p['project_id'], 1, 'The study is observational.',
                            [{'start': 0, 'end': 26, 'evidence': [citation]}],
                            idempotency_key='draft')
    assert r['review_status'] == 'not_reviewed'
    with pytest.raises(ValueError, match='quote'):
        service.save_report(p['project_id'], 1, 'Changed.',
                            [{'start': 0, 'end': 8, 'evidence': [{**citation, 'quote': 'forged'}]}],
                            expected_report_id=r['id'], idempotency_key='forged')
    r2 = service.save_report(p['project_id'], 1, 'A new unreferenced claim.', [],
                             expected_report_id=r['id'], idempotency_key='edit')
    assert r2['id'] != r['id']
    assert service.report(r['id'])['text'] == r['text']
    assert service.project(p['project_id'])['review_status'] == 'not_reviewed'


def test_invalid_report_anchor_identifies_the_field_to_repair(service):
    p = project(service)
    source = service.source(p['source_versions'][0])
    with pytest.raises(ValueError, match='Claim 0 report text'):
        service.save_report(p['project_id'], 1, 'Bounded conclusion.',
            [{'text': 'Unrelated claim.', 'evidence': []}], idempotency_key='bad-claim')
    with pytest.raises(ValueError, match='Claim 0 evidence 0 source quote'):
        service.save_report(p['project_id'], 1, 'Bounded conclusion.',
            [{'text': 'Bounded conclusion.', 'evidence': [{'source_version_id': source['id'],
                'quote': 'Invented source wording.'}]}], idempotency_key='bad-evidence')
    assert service.project(p['project_id'])['report_id'] is None


def test_source_changes_do_not_replace_history(service):
    p = project(service)
    old = service.source(p['source_versions'][0])
    Path(service.workspace, old['path']).write_text('New result.', encoding='utf-8')
    state = service.project(p['project_id'])
    assert state['stale_sources'] == [old['id']]
    assert service.source(old['id'])['text'].startswith('Observational')
    new = service.revise_project(p['project_id'], 1, refresh_sources=True,
                                 idempotency_key='refresh')
    assert new['source_versions'] != p['source_versions']


def test_scope_isolation_and_paths(service, tmp_path):
    p = project(service)
    other = ResearchService(tmp_path / 'other')
    with pytest.raises(FileNotFoundError):
        other.project(p['project_id'])
    with pytest.raises(ValueError):
        service.create_project('question', ['../../secret'], idempotency_key='bad')


def test_full_report_scan_includes_unsubmitted_tail(service):
    from lantai.research.review import review_report
    p = project(service)
    text = 'Introductory discussion.\n\nTreatment causes a 90% improvement.'
    report = service.save_report(p['project_id'], 1, text, [], idempotency_key='report')
    inputs = []

    async def reviewer(payload):
        inputs.append(payload)
        return {'claims': [{'start': text.index('Treatment'), 'end': len(text),
                            'assessment': 'insufficient', 'reason': 'No causal evidence.',
                            'evidence_indices': []}], 'issues': []}

    review = asyncio.run(review_report(service, p['project_id'], 1, report['id'],
                                      reviewer, reviewer_identity={'provider': 'test', 'model': 'reviewer'}))
    assert ''.join(x['report_text'] for x in inputs) == text
    assert review['coverage'] == [[0, len(text)]]
    assert review['status'] == 'needs_revision'
    assert review['claims'][0]['assessment'] == 'insufficient'
    assert service.project(p['project_id'])['review_status'] == 'needs_revision'


def test_forged_reviewer_or_incomplete_review_not_accepted(service):
    from lantai.research.review import review_report
    p = project(service)
    report = service.save_report(p['project_id'], 1, 'Claim.', [], idempotency_key='r')

    async def bad(_):
        return {'claims': [{'start': 0, 'end': 100, 'assessment': 'supports',
                            'reason': 'trust me', 'evidence_indices': []}], 'issues': []}

    with pytest.raises(ValueError):
        asyncio.run(review_report(service, p['project_id'], 1, report['id'], bad,
                                  reviewer_identity={'provider': 'test', 'model': 'reviewer'}))
    assert service.project(p['project_id'])['review_status'] == 'not_reviewed'
    with pytest.raises(ValueError):
        service.save_report(p['project_id'], 1, 'Claim.', [],
                            reviewer='self-reported', idempotency_key='forged')


def test_stop_preserves_checkpoints_and_never_starts_next_call(service):
    from lantai.research.review import review_report
    p = project(service)
    report = service.save_report(p['project_id'], 1, 'A' * 9000, [], idempotency_key='r')
    calls = []

    async def stopped(payload):
        calls.append(payload)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(review_report(service, p['project_id'], 1, report['id'], stopped,
                                  reviewer_identity={'provider': 'test', 'model': 'reviewer'}, chunk_chars=4000))
    assert len(calls) == 1
    assert service.project(p['project_id'])['review_status'] == 'not_reviewed'


def test_retry_reuses_completed_review_checkpoint(service):
    from lantai.research.review import review_report
    p = project(service)
    report = service.save_report(p['project_id'], 1, 'A' * 9000, [], idempotency_key='r')
    calls = []

    async def interrupted(payload):
        calls.append(payload['range'])
        if len(calls) == 2:
            raise asyncio.CancelledError()
        return {'claims': [], 'issues': []}

    identity = {'provider': 'test', 'model': 'reviewer'}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(review_report(service, p['project_id'], 1, report['id'], interrupted,
                                  reviewer_identity=identity, chunk_chars=4000))
    resumed = []

    async def continuation(payload):
        resumed.append(payload['range'])
        return {'claims': [], 'issues': []}

    result = asyncio.run(review_report(service, p['project_id'], 1, report['id'], continuation,
                                      reviewer_identity=identity, chunk_chars=4000))
    assert resumed == [[4000, 8000], [8000, 9000]]
    assert result['coverage'] == [[0, 4000], [4000, 8000], [8000, 9000]]


def test_long_pdf_tail_and_scanned_page_are_explicit(service):
    from reportlab.pdfgen.canvas import Canvas
    path = Path(service.workspace, 'wiki/raw/sources/long.pdf')
    canvas = Canvas(str(path))
    for page in range(50):
        if page != 20:
            canvas.drawString(40, 740, 'Critical negative result.' if page == 49 else 'Page context.')
        canvas.showPage()
    canvas.save()
    p = service.create_project('question', ['wiki/raw/sources/long.pdf'], idempotency_key='pdf')
    source = service.source(p['source_versions'][0])
    assert len(source['pages']) == 50
    assert source['pages'][20]['issue'] == 'no_text_or_scanned_page'
    assert source['coverage'] == 'partial'
    tail = service.read_source(p['project_id'], 1, source['id'], len(source['blocks']) - 1)
    assert tail['blocks'][0]['page'] == 50
    assert 'Critical negative result.' in tail['blocks'][0]['text']


def test_exact_text_anchors_avoid_model_character_counting_without_fuzzy_quotes(service):
    from lantai.research.review import review_report
    p = project(service)
    source = service.source(p['source_versions'][0])
    text = '相关性不证明因果。\n\n后续随机试验未发现改善。'
    report = service.save_report(p['project_id'], 1, text, [{'text': text.split('\n')[0],
        'evidence': [{'source_version_id': source['id'], 'quote': source['text'].split('\n')[0]}]}],
        idempotency_key='anchors')
    assert report['claims'][0]['start'] == 0
    assert report['claims'][0]['evidence'][0]['end'] == source['text'].index('\n')

    async def reviewer(payload):
        return {'claims': [{'text': text.split('\n')[-1], 'assessment': 'insufficient',
            'reason': 'A claim omitted from the submitted citation nodes is still scanned.', 'evidence_indices': []}], 'issues': []}
    review = asyncio.run(review_report(service, p['project_id'], 1, report['id'], reviewer,
        reviewer_identity={'provider': 'test', 'model': 'anchors'}))
    assert review['claims'][0]['start'] == text.index('后续')
    assert review['status'] == 'needs_revision'
    with pytest.raises(ValueError, match='quote|anchor'):
        service.save_report(p['project_id'], 1, text, [{'text': text, 'evidence': [
            {'source_version_id': source['id'], 'quote': 'Randomized treatment significantly improved everything.'}]}],
            expected_report_id=report['id'], idempotency_key='false-anchor')
    with pytest.raises(ValueError, match='ambiguous|anchor'):
        service.save_report(p['project_id'], 1, 'Same. Same.', [{'text': 'Same.', 'evidence': []}],
            expected_report_id=report['id'], idempotency_key='ambiguous-anchor')


def test_review_reuses_exact_evidence_for_many_claims_without_repeating_context(service):
    from lantai.research.review import review_report
    path = Path(service.workspace, 'wiki/raw/sources/a.txt')
    path.write_text('Observed association; no causal conclusion. ' * 40, encoding='utf-8')
    p = project(service)
    sid = p['source_versions'][0]
    service.read_source(p['project_id'], 1, sid, 0, 4)
    sentences = [f'Bounded observation {i}.' for i in range(80)]
    text = '\n'.join(sentences)
    report = service.save_report(p['project_id'], 1, text,
        [{'text': sentence, 'evidence': [{'source_version_id': sid, 'block_index': 0}]}
         for sentence in sentences], idempotency_key='many-claims')
    async def reviewer(payload):
        assert len(payload['evidence']) == 1
        evidence = payload['evidence'][0]
        assert evidence['quote'] == service.source(sid)['text']
        assert evidence['claim_ranges'] == [[c['start'], c['end']] for c in report['claims']]
        return {'claims': [{'text': sentence, 'assessment': 'supports',
            'reason': 'Controlled fixture', 'evidence_indices': [0]} for sentence in sentences], 'issues': []}
    review = asyncio.run(review_report(service, p['project_id'], 1, report['id'], reviewer,
        reviewer_identity={'provider': 'test', 'model': 'reviewer'}))
    assert review['status'] == 'reviewed'
    assert len(review['claims']) == 80
