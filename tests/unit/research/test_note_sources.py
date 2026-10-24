import pytest

from lantai.research.library import LibraryService
from lantai.research.projects import ResearchService


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'wiki/raw/sources/paper.txt'
    path.parent.mkdir(parents=True)
    path.write_text('An observation. No causation established.')
    research = ResearchService(tmp_path)
    project = research.create_project('What is supported?', [str(path.relative_to(tmp_path))], idempotency_key='p')
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Study', 'DOI': '10.1234/study'}], origin={'provider': 'manual'})['items'][0]
    library.link_source(work['work_id'], project['source_versions'][0])
    return research, library, project, work


def test_annotation_binds_exact_linked_source_and_bibliographic_version(setup):
    research, library, project, work = setup
    anchor = {'source_version_id': project['source_versions'][0], 'quote': 'No causation established.'}
    note = library.annotate(work['work_id'], 'Need a trial.', origin={'provider': 'user'}, anchor=anchor)
    assert note['version_id'] == work['version_id']
    assert note['anchor']['quote'] == anchor['quote']
    assert note['anchor']['start'] == 16
    assert note['anchor']['source_hash'] == research.source_manifest(anchor['source_version_id'])['raw_hash']
    changed = library.import_items([{'title': 'New edition', 'DOI': '10.1234/study'}], origin={'provider': 'manual'})['items'][0]
    library.select_version(work['work_id'], changed['version_id'], work['version_id'])
    assert library.work(work['work_id'])['annotations'][0]['version_id'] == work['version_id']
    with pytest.raises(ValueError, match='quote|anchor'):
        library.annotate(work['work_id'], 'Forged', origin={'provider': 'user'}, anchor={**anchor, 'quote': 'Trial proves it.'})
    other = library.import_items([{'title': 'Other'}], origin={'provider': 'manual'})['items'][0]
    with pytest.raises(ValueError, match='linked'):
        library.annotate(other['work_id'], 'Wrong work', origin={'provider': 'user'}, anchor=anchor)


def test_note_is_independently_readable_citable_and_cannot_be_paper_attachment(setup):
    research, library, project, work = setup
    note = library.annotate(work['work_id'], 'Need a trial.', origin={'provider': 'user'})
    result = library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='note')
    assert result['project']['revision'] == 2
    source = research.source_manifest(result['source_version_id'])
    assert source['evidence_level'] == 'note'
    assert source['source_origin']['annotation_id'] == note['id']
    assert source['source_origin']['version_id'] == work['version_id']
    assert source['source_kind'] == 'user_note'
    text = research.source(source['id'])['text']
    assert 'Need a trial.' in text and '不是论文原文' in text
    research.read_source(project['project_id'], 2, source['id'], 0, 1)
    research.save_report(project['project_id'], 2, 'The note proposes a trial.', [{
        'text': 'The note proposes a trial.', 'evidence': [{'source_version_id': source['id'], 'quote': 'Need a trial.'}]
    }], idempotency_key='report')
    with pytest.raises(ValueError, match='note|Note'):
        library.link_source(work['work_id'], source['id'])
    assert library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='note') == result
    refreshed = research.revise_project(project['project_id'], 2, refresh_sources=True, idempotency_key='refresh')
    assert any(research.source_manifest(s).get('source_kind') == 'user_note' for s in refreshed['source_versions'])


def test_model_written_note_is_never_labeled_user_original(setup):
    research, library, project, work = setup
    note = library.annotate(work['work_id'], 'Summary by assistant.', origin={'provider': 'assistant-chat'})
    result = library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='model')
    source = research.source_manifest(result['source_version_id'])
    assert source['source_kind'] == 'model_summary'
    assert '模型生成' in research.source(source['id'])['text']


def test_note_import_rejects_cross_workspace_and_stale_revision_before_file_write(setup, tmp_path):
    research, library, project, work = setup
    note = library.annotate(work['work_id'], 'Need a trial.', origin={'provider': 'user'})
    with pytest.raises(FileNotFoundError):
        LibraryService(tmp_path / 'other').add_note_source(note['id'], project['project_id'], 1, idempotency_key='x')
    research.revise_project(project['project_id'], 1, question='Changed?', idempotency_key='r')
    with pytest.raises(ValueError):
        library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='stale')
    assert not (tmp_path / '.wiki/raw/sources').exists()


def test_review_receives_note_identity_without_paper_authority(setup):
    import asyncio
    from lantai.research.review import review_report
    research, library, project, work = setup
    note = library.annotate(work['work_id'], 'Need a trial.', origin={'provider': 'assistant-chat'})
    result = library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='note')
    sid = result['source_version_id']
    report = research.save_report(project['project_id'], 2, 'A trial is needed.', [{
        'text': 'A trial is needed.', 'evidence': [{'source_version_id': sid, 'quote': 'Need a trial.'}]
    }], idempotency_key='r')
    async def reviewer(payload):
        evidence = payload['evidence'][0]
        assert evidence['evidence_level'] == 'note'
        assert evidence['source_kind'] == 'model_summary'
        assert evidence['source_origin']['annotation_id'] == note['id']
        return {'claims': [{'text': payload['report_text'], 'assessment': 'insufficient', 'reason': 'Only a note', 'evidence_indices': [0]}], 'issues': []}
    value = asyncio.run(review_report(research, project['project_id'], 2, report['id'], reviewer,
        reviewer_identity={'provider': 'controlled-test', 'model': 'test'}))
    assert value['status'] == 'needs_revision'


def test_note_import_resumes_after_project_revision_without_duplicate_source(setup, monkeypatch):
    research, library, project, work = setup
    note = library.annotate(work['work_id'], 'Need a trial.', origin={'provider': 'user'})
    original = ResearchService.source_manifest
    def interrupted(*args, **kwargs):
        raise RuntimeError('After revision checkpoint')
    monkeypatch.setattr(ResearchService, 'source_manifest', interrupted)
    with pytest.raises(RuntimeError):
        library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='recover')
    monkeypatch.setattr(ResearchService, 'source_manifest', original)
    value = library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='recover')
    assert value['project']['revision'] == 2
    assert len(value['project']['source_versions']) == 2


@pytest.mark.parametrize('sanitized', [False, True])
def test_backup_preserves_note_binding_and_anchor(setup, tmp_path, sanitized):
    import zipfile
    from types import SimpleNamespace
    from lantai.backup._ops.create_helpers import add_agent_workspaces
    research, library, project, work = setup
    note = library.annotate(work['work_id'], 'Need a trial.', origin={'provider': 'user'},
        anchor={'source_version_id': project['source_versions'][0], 'quote': 'An observation.'})
    value = library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='backup')
    archive = tmp_path.parent / (tmp_path.name + '-backup.zip')
    with zipfile.ZipFile(archive, 'w') as output:
        assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=research.workspace))], sanitized=sanitized)
    target = tmp_path / 'restored/.lantai/research/index.sqlite3'
    target.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive) as saved:
        name = next(name for name in saved.namelist() if name.endswith('.lantai/research/index.sqlite3'))
        target.write_bytes(saved.read(name))
    restored = LibraryService(tmp_path / 'restored')
    assert restored.work(work['work_id'])['annotations'] == [note]
    source = ResearchService(tmp_path / 'restored').source(value['source_version_id'])
    assert source['source_origin']['anchor'] == note['anchor']
    with restored.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM library_note_sources').fetchone()[0] == 1
