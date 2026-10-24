import sqlite3
import zipfile
from types import SimpleNamespace

import pytest

from lantai.backup._ops.create_helpers import add_agent_workspaces
from lantai.research.projects import ResearchService


@pytest.mark.parametrize('sanitized', [False, True])
def test_backup_preserves_library_organization_and_conflict_revision(tmp_path, sanitized):
    from lantai.research.library import LibraryService
    from lantai.research.store import RevisionConflict
    workspace = tmp_path / 'workspace'
    library = LibraryService(workspace)
    work = library.import_items([{'title': 'Study'}], origin={'provider': 'fixture'})['items'][0]['work_id']
    library.organize(work, 0, tags=['Methods'], collections=['Review'])
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=workspace))], sanitized=sanitized)
    restored_workspace = tmp_path / 'restored'
    with zipfile.ZipFile(archive) as saved:
        index = next(name for name in saved.namelist() if name.endswith('.lantai/research/index.sqlite3'))
        restored_index = restored_workspace / '.lantai/research/index.sqlite3'
        restored_index.parent.mkdir(parents=True)
        restored_index.write_bytes(saved.read(index))
    restored = LibraryService(restored_workspace)
    assert restored.work(work)['organization'] == {'revision': 1, 'tags': ['Methods'], 'collections': ['Review']}
    with pytest.raises(RevisionConflict):
        restored.organize(work, 0, tags=[], collections=[])


@pytest.mark.parametrize('sanitized', [False, True])
def test_research_backup_restores_committed_wal_records_without_journals(tmp_path, sanitized):
    workspace = tmp_path / 'workspace'
    service = ResearchService(workspace)
    project = service.create_project('备份研究记录', [], idempotency_key='create')
    writer = sqlite3.connect(service.store.path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('UPDATE projects SET revision=2 WHERE id=?', (project['project_id'],))
        writer.commit()
        archive = tmp_path / 'backup.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=workspace))],
                                        sanitized=sanitized)
        with zipfile.ZipFile(archive) as saved:
            names = saved.namelist()
            assert len(names) == 1 and names[0].endswith('.lantai/research/index.sqlite3')
            restored = tmp_path / 'restored.sqlite3'
            restored.write_bytes(saved.read(names[0]))
        with sqlite3.connect(restored) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert db.execute('SELECT revision FROM projects WHERE id=?', (project['project_id'],)).fetchone() == (2,)
            assert db.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 1
    finally:
        writer.close()


@pytest.mark.parametrize('sanitized', [False, True])
def test_research_backup_preserves_consumption_and_unsettled_attempts(tmp_path, sanitized):
    from lantai.research.budget import reserve_review
    workspace = tmp_path / 'workspace'
    source = workspace / 'wiki/raw/sources/budget.txt'
    source.parent.mkdir(parents=True)
    source.write_text('Budget evidence.', encoding='utf-8')
    service = ResearchService(workspace)
    project = service.create_project('Preserve limits', ['wiki/raw/sources/budget.txt'],
        idempotency_key='create', budget={'review_calls': 1, 'review_seconds': 10})
    pid = project['project_id']
    service.read_source(pid, 1, project['source_versions'][0], 0, 1)
    report = service.save_report(pid, 1, 'A preference.', [], idempotency_key='report')
    reserve_review(service, pid, 1, report['id'])
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        assert add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=workspace))],
                                    sanitized=sanitized)
    restored_workspace = tmp_path / 'restored'
    with zipfile.ZipFile(archive) as saved:
        index = next(name for name in saved.namelist() if name.endswith('.lantai/research/index.sqlite3'))
        restored_index = restored_workspace / '.lantai/research/index.sqlite3'
        restored_index.parent.mkdir(parents=True)
        restored_index.write_bytes(saved.read(index))
    restored = ResearchService(restored_workspace)
    state = restored.project(pid)
    assert state['budget_usage'] == {'reading_chars': 16, 'review_calls': 1, 'review_seconds': 10,
        'model_calls': 0, 'model_tokens': 0, 'model_seconds': 0}
    assert state['budget_remaining']['review_calls'] == 0
    assert state['report_id'] == report['id']
    with pytest.raises(ValueError, match='budget'):
        reserve_review(restored, pid, 1, report['id'])
