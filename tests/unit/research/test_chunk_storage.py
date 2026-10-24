import pytest
from pathlib import Path
from lantai.research.projects import ResearchService


def create(tmp_path, text):
    relative = '.wiki/raw/sources/long.txt'
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    service = ResearchService(tmp_path)
    project = service.create_project('Full reading', [relative], idempotency_key='p')
    return service, project, project['source_versions'][0]


def test_long_source_is_chunked_and_tail_read_without_full_materialization(tmp_path, monkeypatch):
    text = '甲乙丙丁' * 600000 + 'The final negative result.'
    s, p, sid = create(tmp_path, text)
    manifest = s.store.get(sid, kind='source')
    assert 'text' not in manifest
    assert manifest['text_length'] == len(text)
    monkeypatch.setattr(s, 'source', lambda *_: pytest.fail('Full text must not be loaded for block reading'))
    last = manifest['blocks'][-1]['index']
    read = s.read_source(p['project_id'], 1, sid, last)
    assert read['blocks'][0]['text'].endswith('The final negative result.')
    assert s.project(p['project_id'])['reading']['read_blocks'] == 1


def test_more_than_one_hundred_selected_sources_are_not_truncated(tmp_path):
    root = tmp_path / '.wiki/raw/sources'
    root.mkdir(parents=True)
    paths = []
    for i in range(101):
        p = root / f'{i}.txt'
        p.write_text(f'Unique source {i}')
        paths.append(str(p.relative_to(tmp_path)))
    s = ResearchService(tmp_path)
    assert len(s.create_project('All sources', paths, idempotency_key='p')['source_versions']) == 101


def test_corrupt_chunk_cannot_mark_reading_complete(tmp_path):
    s, p, sid = create(tmp_path, 'Original evidence')
    manifest = s.store.get(sid, kind='source')
    with s.store.transaction() as db:
        db.execute('UPDATE source_text_chunks SET text=? WHERE hash=?', ('Corrupted', manifest['text_chunks'][0]['hash']))
    with pytest.raises(ValueError, match='integrity'):
        s.read_source(p['project_id'], 1, sid, 0)
    assert s.project(p['project_id'])['reading']['read_blocks'] == 0


def test_source_extraction_and_freshness_do_not_read_whole_raw_file(tmp_path, monkeypatch):
    def forbidden(*_):
        pytest.fail('Raw sources must be streamed')
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    s, p, sid = create(tmp_path, 'Complete source')
    assert s.project(p['project_id'])['stale_sources'] == []
    assert s.source(sid)['text'] == 'Complete source'


def test_legacy_inline_sources_remain_readable(tmp_path):
    s = ResearchService(tmp_path)
    with s.store.transaction() as db:
        old = s.store.put(db, 'source', {'text': 'Historical evidence', 'text_hash': 'unused'})
    assert s.source(old['id'])['text'] == 'Historical evidence'


def test_purge_removes_only_unreferenced_text_chunks(tmp_path):
    s, p, sid = create(tmp_path, 'Shared block' * 600)
    second = s.create_project('Keep shared evidence', p['source_paths'], idempotency_key='other')
    s.archive_project(p['project_id'], 1)
    s.delete_project(p['project_id'], 1)
    assert s.source(second['source_versions'][0])['text'] == 'Shared block' * 600
    s.archive_project(second['project_id'], 1)
    s.delete_project(second['project_id'], 1)
    with s.store.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM source_text_chunks').fetchone()[0] == 0


@pytest.mark.parametrize('sanitized', [False, True])
def test_backup_restores_chunked_source_and_reading(tmp_path, sanitized):
    import zipfile
    from types import SimpleNamespace
    from lantai.backup._ops.create_helpers import add_agent_workspaces
    workspace = tmp_path / 'workspace'
    text = 'First page\r\n中文 evidence ' * 700
    s, p, sid = create(workspace, text)
    s.read_source(p['project_id'], 1, sid, 0)
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        add_agent_workspaces(output, [('researcher', SimpleNamespace(workspace_dir=workspace))], sanitized=sanitized)
    restored = tmp_path / 'restored'
    (restored / '.lantai/research').mkdir(parents=True)
    with zipfile.ZipFile(archive) as saved:
        index = next(n for n in saved.namelist() if n.endswith('.lantai/research/index.sqlite3'))
        (restored / '.lantai/research/index.sqlite3').write_bytes(saved.read(index))
    service = ResearchService(restored)
    assert service.source(sid)['text'] == text
    assert service.project(p['project_id'])['reading']['read_blocks'] == 1


def test_explicit_quote_and_review_context_load_only_required_range(tmp_path, monkeypatch):
    import asyncio
    from lantai.research.review import review_report
    s, p, sid = create(tmp_path, 'Prefix. ' * 1000 + 'Target evidence.' + ' Suffix.' * 1000)
    start = 8000
    calls = []
    original = s.store.text_range
    def bounded(source, a=0, b=None, db=None):
        assert b is not None, 'Range-based citation and review must not hydrate full text'
        calls.append((a, b))
        return original(source, a, b, db)
    monkeypatch.setattr(s.store, 'text_range', bounded)
    report = s.save_report(p['project_id'], 1, 'Target evidence.', [{'text': 'Target evidence.',
        'evidence': [{'source_version_id': sid, 'quote': 'Target evidence.', 'start': start, 'end': start + 16}]}], idempotency_key='r')
    async def reviewer(payload):
        evidence = payload['evidence'][0]
        context = payload['evidence_contexts'][evidence['context_index']]
        assert context['source_version_id'] == sid
        assert context['start'] == start - 1500 and context['end'] == start + 16 + 1500
        assert context['text'] == original(s.source_manifest(sid), context['start'], context['end'])
        assert 'Target evidence.' in context['text']
        return {'claims': [{'text': payload['report_text'], 'assessment': 'supports', 'reason': 'Fixture', 'evidence_indices': [0]}], 'issues': []}
    asyncio.run(review_report(s, p['project_id'], 1, report['id'], reviewer, reviewer_identity={'provider':'fixture','model':'fixture'}))
    assert max(b - a for a, b in calls) <= 3016


def test_quote_without_offsets_streams_across_chunk_boundary_and_rejects_duplicates(tmp_path, monkeypatch):
    text = 'a' * 5997 + 'Unique boundary evidence' + 'b' * 6500
    s, p, sid = create(tmp_path, text)
    original = s.store.text_range
    def bounded(source, a=0, b=None, db=None):
        assert b is not None, 'Quote lookup must stream chunks'
        return original(source, a, b, db)
    monkeypatch.setattr(s.store, 'text_range', bounded)
    report = s.save_report(p['project_id'], 1, 'Claim', [{'text': 'Claim', 'evidence': [
        {'source_version_id': sid, 'quote': 'Unique boundary evidence'}]}], idempotency_key='r')
    assert report['claims'][0]['evidence'][0]['start'] == 5997
    with pytest.raises(ValueError, match='ambiguous'):
        s.save_report(p['project_id'], 1, 'New claim', [{'text': 'New claim', 'evidence': [
            {'source_version_id': sid, 'quote': 'aa'}]}], expected_report_id=report['id'], idempotency_key='r2')


def test_large_raw_source_is_imported_incrementally_with_complete_digest(tmp_path):
    import hashlib
    root = tmp_path / '.wiki/raw/sources'
    root.mkdir(parents=True)
    path = root / 'large.txt'
    digest = hashlib.sha256()
    piece = ('abcd中文\r\n' * 10000).encode()
    with path.open('wb') as stream:
        for _ in range(180):
            stream.write(piece)
            digest.update(piece)
    assert path.stat().st_size > 20 * 1024 * 1024
    s = ResearchService(tmp_path)
    p = s.create_project('Large source', [str(path.relative_to(tmp_path))], idempotency_key='large')
    manifest = s.source_manifest(p['source_versions'][0])
    assert manifest['raw_hash'] == digest.hexdigest()
    assert manifest['text_length'] == len(piece.decode()) * 180
    assert s.read_source(p['project_id'], 1, manifest['id'], len(manifest['blocks']) - 1)['blocks'][-1]['text'].endswith('abcd中文\r\n')
