import pytest
from pathlib import Path

from lantai.research.projects import ResearchService, RevisionConflict
from lantai.research.exports import export_report, export_file


def test_archive_restore_and_permanent_deletion_preserve_other_projects_and_original_files(tmp_path):
    original = tmp_path / 'wiki/raw/sources/研究资料.txt'
    original.parent.mkdir(parents=True)
    original.write_text('同一原始资料可以用于多个项目。')
    service = ResearchService(tmp_path)
    p = service.create_project('删除测试', ['wiki/raw/sources/研究资料.txt'], idempotency_key='one')
    other = service.create_project('保留测试', ['wiki/raw/sources/研究资料.txt'], idempotency_key='two')
    report = service.save_report(p['project_id'], 1, '待核验草稿', [], idempotency_key='report')
    exported = export_report(service, p['project_id'], 1, report['id'], 'markdown', 'numeric', 'export')
    artifact = export_file(service, exported['id'])
    with pytest.raises(ValueError, match='archive'):
        service.delete_project(p['project_id'], 1)
    service.archive_project(p['project_id'], 1)
    assert [x['project_id'] for x in service.list_projects()] == [other['project_id']]
    assert service.list_projects(archived=True)[0]['project_id'] == p['project_id']
    with pytest.raises(ValueError, match='archived'):
        service.save_report(p['project_id'], 1, '不能修改归档项目', [], expected_report_id=report['id'], idempotency_key='blocked')
    service.restore_project(p['project_id'], 1)
    assert service.project(p['project_id'])['report_id'] == report['id']
    assert artifact.is_file()
    service.archive_project(p['project_id'], 1)
    with pytest.raises(RevisionConflict):
        service.delete_project(p['project_id'], 2)
    deleted = service.delete_project(p['project_id'], 1)
    assert deleted['status'] == 'deleted' and not artifact.exists()
    with pytest.raises(FileNotFoundError):
        service.project(p['project_id'])
    with pytest.raises(FileNotFoundError):
        service.report(report['id'])
    with pytest.raises(FileNotFoundError):
        service.source(p['source_versions'][0])
    assert service.source(other['source_versions'][0])['text'] == original.read_text()
    assert original.read_text() == '同一原始资料可以用于多个项目。'


def test_failed_file_cleanup_is_visible_and_can_be_retried_without_deleting_new_references(tmp_path, monkeypatch):
    original = tmp_path / 'wiki/raw/sources/retry.txt'
    original.parent.mkdir(parents=True)
    original.write_text('待删除的托管快照。')
    service = ResearchService(tmp_path)
    p = service.create_project('清理重试', ['wiki/raw/sources/retry.txt'], idempotency_key='one')
    blob = tmp_path / service.source(p['source_versions'][0])['snapshot_path']
    service.archive_project(p['project_id'], 1)
    unlink = Path.unlink

    def fail_managed_file(path, *args, **kwargs):
        if path == blob:
            raise PermissionError('injected cleanup failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail_managed_file)
    result = service.delete_project(p['project_id'], 1)
    assert result['status'] == 'cleanup_pending' and blob.exists()
    assert service.pending_deletions() == [{'project_id': p['project_id'], 'revision': 1}]
    assert service.list_projects() == service.list_projects(archived=True) == []
    with pytest.raises(RevisionConflict):
        service.delete_project(p['project_id'], 2)
    # A new project may legitimately import the same file during the retry window.
    other = service.create_project('新引用', ['wiki/raw/sources/retry.txt'], idempotency_key='two')
    monkeypatch.setattr(Path, 'unlink', unlink)
    assert service.delete_project(p['project_id'], 1)['status'] == 'deleted'
    assert service.pending_deletions() == [] and blob.is_file()
    assert service.source(other['source_versions'][0])['text'] == original.read_text()
    service.archive_project(other['project_id'], 1)
    assert service.delete_project(other['project_id'], 1)['status'] == 'deleted'
    assert not blob.exists() and original.is_file()
