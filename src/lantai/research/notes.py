"""Import immutable library notes as explicit secondary evidence, never paper text."""
from .evidence import _digest, _source
from .projects import ResearchService


def add_note_source(library, annotation_id, project_id, revision, *, idempotency_key):
    research = ResearchService(library.workspace)
    payload = {'annotation': annotation_id, 'project': project_id, 'revision': revision}
    key = 'note-import:' + idempotency_key
    with library.store.transaction() as db:
        done = library.store.request(db, key, payload)
        if done:
            return done
        checkpoint = library.store.request(db, 'note-source:' + idempotency_key, payload)
        note = library.store.get(annotation_id, db, 'library_annotation')
        if not checkpoint:
            project = library.store.expect(project_id, revision, db)
            work = library.work(note['work_id'], db)
    if not checkpoint:
        provider = note['origin'].get('provider')
        kind, label = ('user_note', '用户笔记') if provider == 'user' else (
            ('model_summary', '模型生成的笔记') if provider in {'assistant-chat', 'user-chat'} else ('imported_note', '导入笔记'))
        # Legacy annotations without a version stay explicitly unbound.
        origin = {'annotation_id': note['id'], 'work_id': note['work_id'],
                  'version_id': note.get('version_id'), 'provider': provider,
                  'anchor': note.get('anchor'), 'external_origin': note['origin']}
        if note.get('region_anchor') is not None:
            origin['region_anchor'] = note['region_anchor']
        version = next((v for v in work['versions'] if v['id'] == note.get('version_id')), None)
        title = version['csl']['title'] if version else '未绑定文献版本'
        text = f'# {label}：{title}\n\n这是{label}，不是论文原文；不能替代对原始资料的核验。\n\n'
        text += f'笔记记录：{note["id"]}\n文献版本：{note.get("version_id") or "未绑定"}\n\n## 笔记正文\n\n{note["text"]}\n'
        from ..app.wiki.core import import_single_wiki_source
        info, _, _ = import_single_wiki_source(library.workspace, 'research-notes/' + note['id'] + '.md', text.encode())
        digest = _digest(_source(library.workspace, info.path).read_bytes())
        with library.store.transaction() as db:
            library.store.expect(project_id, revision, db)
            binding = library.store.put(db, 'note_source_origin', {'source_origin': origin,
                'source_kind': kind, 'raw_hash': digest, 'path': info.path})
            previous = db.execute('SELECT record_id FROM library_note_sources WHERE path=?', (info.path,)).fetchone()
            if previous:
                existing = library.store.get(previous['record_id'], db, 'note_source_origin')
                if any(existing[k] != binding[k] for k in ('source_origin', 'source_kind', 'raw_hash')):
                    raise ValueError('Note source provenance conflicts with existing import')
            else:
                db.execute('INSERT INTO library_note_sources VALUES (?,?)', (info.path, binding['id']))
            checkpoint = library.store.put(db, 'note_import_checkpoint', {'path': info.path,
                'raw_hash': digest, 'paths': list(dict.fromkeys([*project['source_paths'], info.path]))})
            library.store.remember(db, 'note-source:' + idempotency_key, payload, checkpoint)
    if _digest(_source(library.workspace, checkpoint['path']).read_bytes()) != checkpoint['raw_hash']:
        raise ValueError('Imported note changed before completion')
    updated = research.revise_project(project_id, revision, source_paths=checkpoint['paths'],
                                      idempotency_key='note-revision:' + idempotency_key)
    source = next(research.source_manifest(s) for s in updated['source_versions']
                  if research.source_manifest(s)['path'] == checkpoint['path'])
    with library.store.transaction() as db:
        library.store.expect(project_id, updated['revision'], db)
        result = library.store.put(db, 'note_import', {'project': research.store.current(project_id, db),
            'source_version_id': source['id'], 'annotation_id': note['id'], 'fulltext_read': False})
        library.store.remember(db, key, payload, result)
        return result
