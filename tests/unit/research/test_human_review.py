import pytest
from lantai.research.projects import ResearchService
from lantai.research.store import RevisionConflict
from lantai.research.exports import export_report, export_file


@pytest.fixture
def draft(tmp_path):
    s = ResearchService(tmp_path)
    p = s.create_project('Review', [], idempotency_key='p')
    r = s.save_report(p['project_id'], 1, 'An uncertain finding.', [], idempotency_key='r')
    return s, p, r


def add(s, p, r, **extra):
    return s.add_human_review(p['project_id'], 1, r['id'], expected_review_id=None,
        text='Please distinguish the two populations.', verdict='question',
        idempotency_key='opinion', **extra)


def test_opinion_is_immutable_idempotent_and_does_not_promote_draft(draft):
    s, p, r = draft
    note = add(s, p, r, anchor={'text': 'uncertain'})
    assert add(s, p, r, anchor={'text': 'uncertain'}) == note
    restored = ResearchService(s.workspace).evidence(p['project_id'])
    assert restored['human_reviews'] == [note]
    assert note['anchor'] == {'start': 3, 'end': 12, 'text': 'uncertain'}
    assert note['report_hash'] == r['text_hash']
    assert restored['project']['review_status'] == 'not_reviewed'
    assert restored['review'] is None


def test_changed_report_preserves_opinion_as_history(draft):
    s, p, r = draft
    note = add(s, p, r)
    s.save_report(p['project_id'], 1, 'A revised finding.', [], expected_report_id=r['id'], idempotency_key='r2')
    detail = s.evidence(p['project_id'])
    assert detail['human_reviews'] == [note]
    assert note['report_id'] != detail['report']['id']
    with pytest.raises(RevisionConflict):
        s.add_human_review(p['project_id'], 1, r['id'], expected_review_id=None,
            text='Stale', verdict='agree', idempotency_key='stale')


@pytest.mark.parametrize('extra', [{'anchor': {'text': 'not in report'}}, {'anchor': {'start': True, 'end': 4}}])
def test_invalid_anchor_cannot_become_a_human_opinion(draft, extra):
    s, p, r = draft
    with pytest.raises(ValueError):
        add(s, p, r, **extra)
    assert s.evidence(p['project_id'])['human_reviews'] == []


def test_export_binds_opinions_without_upgrading_review_status(draft):
    s, p, r = draft
    note = add(s, p, r)
    receipt = export_report(s, p['project_id'], 1, r['id'], 'markdown', 'apa', 'export')
    text = export_file(s, receipt['id']).read_text()
    assert '人工复核意见' in text and note['text'] in text
    assert receipt['human_review_ids'] == [note['id']]
    assert receipt['status'] == 'not_reviewed'


def test_agreement_preserves_negative_model_record_and_structural_failure(draft):
    import asyncio
    from lantai.research.review import review_report
    s, p, r = draft
    async def model(payload):
        return {'claims': [{'text': payload['report_text'], 'assessment': 'insufficient',
            'reason': 'No supporting evidence', 'evidence_indices': []}], 'issues': []}
    review = asyncio.run(review_report(s, p['project_id'], 1, r['id'], model,
        reviewer_identity={'provider': 'fixture', 'model': 'fixture'}))
    s.add_human_review(p['project_id'], 1, r['id'], expected_review_id=review['id'],
        text='I agree with the prose.', verdict='agree', idempotency_key='agree')
    assert s.evidence(p['project_id'])['review'] == review
    assert s.project(p['project_id'])['review_status'] == 'needs_revision'
    with s.store.transaction() as db:
        db.execute('UPDATE records SET payload=? WHERE id=?', ('{}', r['id']))
    with pytest.raises(ValueError, match='integrity'):
        s.add_human_review(p['project_id'], 1, r['id'], expected_review_id=review['id'],
            text='Override corruption', verdict='agree', idempotency_key='corrupt')


@pytest.mark.parametrize('sanitized', [False, True])
def test_backup_preserves_independent_opinions(draft, tmp_path, sanitized):
    import zipfile
    from types import SimpleNamespace
    from lantai.backup._ops.create_helpers import add_agent_workspaces
    s, p, r = draft
    note = add(s, p, r)
    archive = tmp_path.parent / (tmp_path.name + '-backup.zip')
    with zipfile.ZipFile(archive, 'w') as output:
        assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=s.workspace))], sanitized=sanitized)
    restored_path = tmp_path / 'restored/.lantai/research/index.sqlite3'
    restored_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive) as saved:
        index = next(name for name in saved.namelist() if name.endswith('.lantai/research/index.sqlite3'))
        restored_path.write_bytes(saved.read(index))
    restored = ResearchService(tmp_path / 'restored')
    assert restored.evidence(p['project_id'])['human_reviews'] == [note]
