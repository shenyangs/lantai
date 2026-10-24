"""Versioned research service shared by tools, API and the workspace UI."""
from __future__ import annotations

from pathlib import Path

from .evidence import _digest, _within
from .sources import extract_source, source_is_current
from .store import ResearchStore, RevisionConflict
from .anchors import resolve_span
from .budget import normalize_budget, usage

POLICIES = {'local_sources', 'public_search', 'offline'}


def _brief_items(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 5000 for item in value):
        raise ValueError('Research brief needs at most 100 nonempty text items, each at most 5000 characters')
    return [item.strip() for item in value]


class ResearchService:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ResearchStore(self.workspace)

    @staticmethod
    def _levels(paths, levels=None):
        if levels is not None and (not isinstance(levels, dict) or set(levels) - set(paths)):
            raise ValueError('source evidence levels are outside the selected paths')
        value = {path: (levels or {}).get(path, 'fulltext') for path in paths}
        if any(level not in {'fulltext', 'abstract', 'excerpt'} for level in value.values()):
            raise ValueError('source evidence level must be fulltext, abstract or excerpt')
        return value

    def _sources(self, db, paths: list[str], levels=None, local_ocr=False) -> list[str]:
        if len(set(paths)) != len(paths):
            raise ValueError('choose distinct source paths; no implicit truncation')
        levels = self._levels(paths, levels)
        ids, seen = [], {}
        for path in paths:
            source = extract_source(self.workspace, path, evidence_level=levels[path], local_ocr=local_ocr,
                                    put_chunk=lambda text: self.store.put_text_chunk(db, text))
            note = db.execute('SELECT record_id FROM library_note_sources WHERE path=?', (path,)).fetchone()
            if note:
                origin = self.store.get(note['record_id'], db, 'note_source_origin')
                if source['raw_hash'] != origin['raw_hash']:
                    raise ValueError('Imported note changed; create a new note version')
                source.update(evidence_level='note', source_kind=origin['source_kind'],
                              source_origin=origin['source_origin'])
            # Identical files are one piece of evidence, even with different filenames.
            if source['raw_hash'] not in seen:
                ids.append(self.store.put(db, 'source', source)['id'])
                seen[source['raw_hash']] = source['evidence_level']
            elif seen[source['raw_hash']] != source['evidence_level']:
                raise ValueError('identical sources have conflicting evidence levels')
        return ids

    def create_project(self, question: str, source_paths: list[str], *, policy='local_sources',
                       idempotency_key: str, scope='', exclusions='', output='report', source_levels=None, budget=None,
                       subquestions=None, outline=None, query_preview_required=None, local_ocr=None) -> dict:
        if not question.strip() or len(question) > 5000 or policy not in POLICIES:
            raise ValueError('question and a supported data policy are required')
        if local_ocr is not None and not isinstance(local_ocr, bool):
            raise ValueError('Local OCR preference must be boolean')
        if query_preview_required is not None and not isinstance(query_preview_required, bool):
            raise ValueError('Query preview preference must be boolean')
        levels = self._levels(source_paths, source_levels)
        payload = dict(question=question, paths=source_paths, policy=policy, scope=scope, source_levels=levels,
                       exclusions=exclusions, output=output)
        if query_preview_required is not None:
            payload['query_preview_required'] = query_preview_required
        if local_ocr is not None:
            payload['local_ocr'] = local_ocr
        limits = normalize_budget(budget)
        if budget is not None:
            payload['budget'] = limits
        brief = {'subquestions': _brief_items(subquestions), 'outline': _brief_items(outline)}
        for field, supplied in (('subquestions', subquestions), ('outline', outline)):
            if supplied is not None:
                payload[field] = brief[field]
        with self.store.transaction() as db:
            existing = self.store.request(db, idempotency_key, payload)
            if existing:
                return existing
            from uuid import uuid4
            project = 'project-' + uuid4().hex
            value = self.store.put(db, 'revision', {
                **brief, 'local_ocr': bool(local_ocr), 'query_preview_required': bool(query_preview_required), 'project_id': project, 'revision': 1, 'question': question, 'scope': scope,
                'exclusions': exclusions, 'output': output, 'policy': policy, 'budget': limits,
                'source_versions': self._sources(db, source_paths, levels, bool(local_ocr)), 'source_paths': source_paths, 'source_levels': levels,
                'report_id': None, 'review_id': None, 'review_status': 'not_reviewed',
            }, project, 1)
            db.execute('INSERT INTO projects VALUES (?,?,?)', (project, 1, value['id']))
            self.store.remember(db, idempotency_key, payload, value)
            return value

    def estimate(self, project: str, revision: int) -> dict:
        from .estimate import estimate
        return estimate(self, project, revision)

    def project(self, project: str) -> dict:
        with self.store.transaction() as db:
            value = self.store.current(project, db)
            sources = [self.store.get(s, db, 'source') for s in value['source_versions']]
            has_library = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_works'").fetchone()
            bibliography = [r['source'] for r in db.execute('SELECT source FROM source_works JOIN works ON '
                'source_works.work=works.id WHERE source_works.version != works.selected_version_id')
                if r['source'] in value['source_versions']] if has_library else []
            read = db.execute('SELECT COUNT(*) FROM reading WHERE project=? AND revision=?',
                              (project, value['revision'])).fetchone()[0]
            used = usage(self.store, db, project)
            from .inference import summary
            inference = summary(self.store, db, project)
            limits = normalize_budget(value.get('budget'))
            archived = bool(db.execute('SELECT 1 FROM archived_projects WHERE project=?', (project,)).fetchone())
        stale = [s['id'] for s in sources if not source_is_current(self.workspace, s)]
        return {'subquestions': [], 'outline': [], **value, 'budget': limits, 'budget_usage': used, 'inference': inference,
                'budget_remaining': {k: max(0, limits[k] - used[k]) for k in limits}, 'archived': archived, 'stale_sources': stale, 'stale_bibliography': bibliography,
                'review_status': ('source_changed' if stale else 'metadata_changed' if bibliography else value['review_status'])
                                if value['review_id'] else value['review_status'],
                'reading': {'read_blocks': read, 'total_blocks': sum(len(s['blocks']) for s in sources),
                            'incomplete_sources': [s['id'] for s in sources if s['coverage'] != 'complete']}}

    def list_projects(self, archived=False) -> list[dict]:
        with self.store.transaction() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM projects WHERE EXISTS '
                '(SELECT 1 FROM archived_projects WHERE project=projects.id)=? ORDER BY rowid DESC', (bool(archived),))]
        return [self.project(p) for p in ids]

    def pending_deletions(self) -> list[dict]:
        with self.store.transaction() as db:
            return [{'project_id': r['project'], 'revision': r['revision']}
                    for r in db.execute('SELECT project, revision FROM project_purges ORDER BY rowid')]

    def _set_archive(self, project, revision, archived):
        import time
        with self.store.transaction() as db:
            value = self.store.current(project, db)
            if value['revision'] != revision:
                raise RevisionConflict('Research revision changed; reload the project')
            if archived:
                db.execute('INSERT OR IGNORE INTO archived_projects VALUES (?,?)', (project, time.time()))
            else:
                db.execute('DELETE FROM archived_projects WHERE project=?', (project,))
        return self.project(project)

    def archive_project(self, project, expected_revision):
        return self._set_archive(project, expected_revision, True)

    def restore_project(self, project, expected_revision):
        return self._set_archive(project, expected_revision, False)

    def delete_project(self, project, expected_revision):
        """Purge an archived project; persist file cleanup intent for safe retries."""
        import json
        from .store import canonical
        with self.store.transaction() as db:
            pending = db.execute('SELECT * FROM project_purges WHERE project=?', (project,)).fetchone()
            if not pending:
                current = self.store.current(project, db)
                if current['revision'] != expected_revision:
                    raise RevisionConflict('Research revision changed; reload the project')
                if not db.execute('SELECT 1 FROM archived_projects WHERE project=?', (project,)).fetchone():
                    raise ValueError('First archive the project before permanent deletion')
                records = [self.store.get(r['id'], db) for r in db.execute('SELECT id FROM records WHERE project=?', (project,))]
                candidates = {s for r in records if r['kind'] == 'revision' for s in r['source_versions']}
                retained = {s for row in db.execute("SELECT id FROM records WHERE kind='revision' AND project!=?", (project,))
                            for s in self.store.get(row['id'], db)['source_versions']}
                removed_sources = [self.store.get(s, db, 'source') for s in candidates - retained]
                ids = [r['id'] for r in records] + [s['id'] for s in removed_sources]
                for identifier in ids:
                    db.execute('DELETE FROM requests WHERE result_id=?', (identifier,))
                    db.execute('DELETE FROM records WHERE id=?', (identifier,))
                # Chunks may be shared across source versions and projects.
                retained_chunks = {chunk['hash'] for row in db.execute("SELECT id FROM records WHERE kind='source'")
                                   for chunk in self.store.get(row['id'], db).get('text_chunks', [])}
                for digest in {c['hash'] for source in removed_sources for c in source.get('text_chunks', [])} - retained_chunks:
                    db.execute('DELETE FROM source_text_chunks WHERE hash=?', (digest,))
                has_library = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_works'").fetchone()
                if has_library:
                    for source in removed_sources:
                        db.execute('DELETE FROM source_works WHERE source=?', (source['id'],))
                retained_blobs = {self.store.get(r['id'], db)['snapshot_path']
                                  for r in db.execute("SELECT id FROM records WHERE kind='source'")}
                files = {r['artifact_path'] for r in records if r['kind'] == 'export'}
                files.update(s['snapshot_path'] for s in removed_sources if s['snapshot_path'] not in retained_blobs)
                # Only managed copies are deleted; original user files remain in the knowledge workspace.
                for relative in files:
                    if not relative.startswith(('.lantai/research/blobs/', '.lantai/research/exports/')):
                        raise ValueError('Research cleanup path is outside managed storage')
                    _within(self.workspace, relative)
                db.execute('INSERT INTO project_purges VALUES (?,?,?)', (project, expected_revision, canonical(sorted(files))))
                db.execute('DELETE FROM reading WHERE project=?', (project,))
                db.execute('DELETE FROM research_reading_usage WHERE project=?', (project,))
                db.execute('DELETE FROM research_review_attempts WHERE project=?', (project,))
                db.execute('DELETE FROM research_model_calls WHERE project=?', (project,))
                db.execute('DELETE FROM research_discovery_cache WHERE project=?', (project,))
                db.execute('DELETE FROM research_query_previews WHERE project=?', (project,))
                db.execute('DELETE FROM projects WHERE id=?', (project,))
                db.execute('DELETE FROM archived_projects WHERE project=?', (project,))
                pending = db.execute('SELECT * FROM project_purges WHERE project=?', (project,)).fetchone()
            if pending['revision'] != expected_revision:
                raise RevisionConflict('Deletion revision changed; use the original deletion request')
            files = json.loads(pending['files'])
        try:
            with self.store.transaction() as db:
                # Import and cleanup share the SQLite write lock. A snapshot reused
                # after an interrupted purge must remain available to its new owner.
                retained_blobs = {self.store.get(r['id'], db)['snapshot_path']
                                  for r in db.execute("SELECT id FROM records WHERE kind='source'")}
                for relative in files:
                    if relative not in retained_blobs:
                        _within(self.workspace, relative).unlink(missing_ok=True)
                db.execute('DELETE FROM project_purges WHERE project=?', (project,))
        except OSError:
            return {'project_id': project, 'status': 'cleanup_pending', 'message': 'Managed file cleanup failed; retry deletion'}
        return {'project_id': project, 'status': 'deleted'}

    def revise_project(self, project: str, expected_revision: int, *, idempotency_key: str,
                       question=None, source_paths=None, policy=None, refresh_sources=False,
                       scope=None, exclusions=None, source_levels=None, budget=None, subquestions=None, outline=None, query_preview_required=None, local_ocr=None) -> dict:
        payload = dict(project=project, revision=expected_revision, question=question, paths=source_paths,
                       policy=policy, refresh=refresh_sources, scope=scope, exclusions=exclusions, source_levels=source_levels)
        if local_ocr is not None:
            if not isinstance(local_ocr, bool):
                raise ValueError('Local OCR preference must be boolean')
            payload['local_ocr'] = local_ocr
        if query_preview_required is not None:
            if not isinstance(query_preview_required, bool):
                raise ValueError('Query preview preference must be boolean')
            payload['query_preview_required'] = query_preview_required
        if budget is not None:
            payload['budget'] = budget
        for field, supplied in (('subquestions', subquestions), ('outline', outline)):
            if supplied is not None:
                payload[field] = _brief_items(supplied)
        with self.store.transaction() as db:
            existing = self.store.request(db, idempotency_key, payload)
            if existing:
                return existing
            old = self.store.expect(project, expected_revision, db)
            value = {k: v for k, v in old.items() if k not in {'id', 'kind', 'created_at'}}
            for key, item in [('question', question), ('policy', policy), ('scope', scope), ('exclusions', exclusions), ('query_preview_required', query_preview_required), ('local_ocr', local_ocr)]:
                if item is not None:
                    value[key] = item
            if value['policy'] not in POLICIES or not value['question'].strip():
                raise ValueError('invalid research scope')
            if source_paths is not None or refresh_sources or source_levels is not None or local_ocr is not None:
                value['source_paths'] = source_paths if source_paths is not None else old['source_paths']
                prior = old.get('source_levels') or {self.store.get(s, db, 'source')['path']:
                    self.store.get(s, db, 'source')['evidence_level'] for s in old['source_versions']}
                if source_levels is not None:
                    self._levels(value['source_paths'], source_levels)
                value['source_levels'] = {path: (source_levels or {}).get(path, prior.get(path, 'fulltext'))
                                          for path in value['source_paths']}
                value['source_versions'] = self._sources(db, value['source_paths'], value['source_levels'], value.get('local_ocr', False))
            value['budget'] = normalize_budget(budget, old.get('budget'))
            for field in ('subquestions', 'outline'):
                value[field] = payload.get(field, old.get(field, []))
            budget_only = budget is not None and not refresh_sources and all(v is None for v in
                (question, source_paths, policy, scope, exclusions, source_levels, subquestions, outline, query_preview_required, local_ocr))
            value['revision'] = expected_revision + 1
            if budget_only:
                db.execute('INSERT INTO reading SELECT project, ?, source, block FROM reading WHERE project=? AND revision=?',
                           (value['revision'], project, expected_revision))
            else:
                value.update(report_id=None, review_id=None, review_status='not_reviewed')
            value = self.store.put(db, 'revision', value, project, value['revision'])
            db.execute('UPDATE projects SET revision=?, record_id=? WHERE id=?',
                       (value['revision'], value['id'], project))
            self.store.remember(db, idempotency_key, payload, value)
            return value

    def source(self, identifier: str) -> dict:
        source = self.source_manifest(identifier)
        return {**source, 'text': self.store.text_range(source)}

    def source_page(self, identifier: str, number: int) -> dict:
        from .pdf_preview import preview_page
        return preview_page(self.workspace, self.source_manifest(identifier), number)

    def source_tables(self, identifier: str, number: int) -> dict:
        from .pdf_tables import page_tables
        return page_tables(self.workspace, self.source_manifest(identifier), number)

    def source_manifest(self, identifier: str) -> dict:
        return self.store.get(identifier, kind='source')

    def adopt_bibliography(self, project: str, expected_revision: int, *, idempotency_key: str) -> dict:
        """Explicitly adopt selected editions without changing historical evidence."""
        payload = {'project': project, 'revision': expected_revision, 'operation': 'adopt_bibliography'}
        with self.store.transaction() as db:
            existing = self.store.request(db, idempotency_key, payload)
            if existing:
                return existing
            old = self.store.expect(project, expected_revision, db)
            sources = {s: self.store.get(s, db, 'source') for s in old['source_versions']}
            if any(not source_is_current(self.workspace, s) for s in sources.values()):
                raise ValueError('Source changed: refresh sources before adopting bibliography')
            has_library = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_works'").fetchone()
            links = db.execute('SELECT source, work, selected_version_id FROM source_works JOIN works '
                               'ON source_works.work=works.id WHERE source_works.version != works.selected_version_id').fetchall() if has_library else []
            mapping = {}
            revision = expected_revision + 1
            for link in links:
                if link['source'] not in sources:
                    continue
                source = self.store.put(db, 'source', {**sources[link['source']],
                    'previous_source_version_id': link['source']})
                mapping[link['source']] = source['id']
                db.execute('INSERT INTO source_works VALUES (?,?,?)',
                           (source['id'], link['work'], link['selected_version_id']))
            if not mapping:
                self.store.remember(db, idempotency_key, payload, old)
                return old
            value = {**old, 'revision': revision, 'source_versions': [mapping.get(s, s) for s in old['source_versions']],
                     'review_id': None, 'review_status': 'not_reviewed'}
            if old['report_id']:
                report = self.store.get(old['report_id'], db, 'report')
                claims = [{**c, 'evidence': [{**e, 'source_version_id': mapping.get(e['source_version_id'], e['source_version_id'])}
                                            for e in c['evidence']]} for c in report['claims']]
                report = self.store.put(db, 'report', {**report, 'revision': revision, 'claims': claims,
                    'previous_report_id': old['report_id'], 'review_status': 'not_reviewed'}, project, revision)
                value['report_id'] = report['id']
            for row in db.execute('SELECT source, block FROM reading WHERE project=? AND revision=?',
                                  (project, expected_revision)).fetchall():
                db.execute('INSERT INTO reading VALUES (?,?,?,?)',
                           (project, revision, mapping.get(row['source'], row['source']), row['block']))
            value = self.store.put(db, 'revision', value, project, revision)
            db.execute('UPDATE projects SET revision=?, record_id=? WHERE id=?', (revision, value['id'], project))
            self.store.remember(db, idempotency_key, payload, value)
            return value

    def read_source(self, project: str, revision: int, source_id: str, first: int, count=1) -> dict:
        if not isinstance(first, int) or first < 0 or not 1 <= count <= 4:
            raise ValueError('read 1–4 blocks at a non-negative index')
        with self.store.transaction() as db:
            current = self.store.expect(project, revision, db)
            if source_id not in current['source_versions']:
                raise ValueError('source is outside this project revision')
            source = self.store.get(source_id, db, 'source')
            blocks = source['blocks'][first:first + count]
            if not blocks:
                raise ValueError('source block out of range; check coverage and scanned pages')
            read_blocks = [{**b, 'text': self.store.text_range(source, b['start'], b['end'], db)} for b in blocks]
            for block in read_blocks:
                block['regions'] = [r for page in source['pages'] for r in page.get('regions', [])
                                    if r['start'] < block['end'] and r['end'] > block['start']]
                block['page_issues'] = [page['issue'] for page in source['pages'] if page['page'] == block['page'] and page['issue']]
            used = usage(self.store, db, project)
            known = {r['block'] for r in db.execute('SELECT block FROM research_reading_usage WHERE project=? AND source=?', (project, source_id))}
            additional = sum(b['end'] - b['start'] for b in blocks if b['index'] not in known)
            if additional and used['reading_chars'] + additional > normalize_budget(current.get('budget'))['reading_chars']:
                raise ValueError('Research reading budget exhausted; raise project limits to continue. Reading progress is preserved.')
            for block in blocks:
                db.execute('INSERT OR IGNORE INTO reading VALUES (?,?,?,?)',
                           (project, revision, source_id, block['index']))
                db.execute('INSERT OR IGNORE INTO research_reading_usage VALUES (?,?,?,?)',
                           (project, source_id, block['index'], block['end'] - block['start']))
            self.store.put(db, 'reading', {'source_version_id': source_id, 'blocks': blocks}, project, revision)
        return {'source_version_id': source_id, 'path': source['path'], 'text_hash': source['text_hash'],
                'coverage': source['coverage'], 'issues': source['issues'], 'evidence_level': source['evidence_level'],
                'blocks': read_blocks,
                'total_blocks': len(source['blocks']),
                'next_block': first + len(blocks) if first + len(blocks) < len(source['blocks']) else None}

    def save_report(self, project: str, revision: int, text: str, claims: list[dict], *,
                    idempotency_key: str, expected_report_id=None, **unsupported) -> dict:
        if unsupported:
            raise ValueError('reviewer assertions cannot establish controlled review')
        if not isinstance(text, str) or not text.strip() or len(text) > 200_000 or len(claims) > 500:
            raise ValueError('report must contain 1–200000 characters and at most 500 citation nodes')
        payload = dict(project=project, revision=revision, text=text, claims=claims, report=expected_report_id)
        with self.store.transaction() as db:
            existing = self.store.request(db, idempotency_key, payload)
            if existing:
                return existing
            current = self.store.expect(project, revision, db)
            if current['report_id'] != expected_report_id:
                raise RevisionConflict('Report version changed; reload before editing')
            normalized = []
            for claim_index, claim in enumerate(claims):
                try:
                    start, end = resolve_span(text, claim)
                except ValueError as exc:
                    raise ValueError(f'Claim {claim_index} report text must be copied exactly from text: {exc}') from exc
                spans = []
                for evidence_index, evidence in enumerate(claim.get('evidence', [])):
                    if evidence['source_version_id'] not in current['source_versions']:
                        raise ValueError('evidence is outside this project revision')
                    source = self.store.get(evidence['source_version_id'], db, 'source')
                    if 'block_index' in evidence:
                        index = evidence['block_index']
                        if type(index) is not int or not 0 <= index < len(source['blocks']):
                            raise ValueError('Invalid source block index')
                        if any(key in evidence for key in ('quote', 'start', 'end')):
                            raise ValueError('Use a block reference OR an exact quote with optional offsets')
                        if not db.execute('SELECT 1 FROM reading WHERE project=? AND revision=? AND source=? AND block=?',
                                (project, revision, source['id'], index)).fetchone():
                            raise ValueError('Please read the source block before citing it')
                        block = source['blocks'][index]
                        a, b = block['start'], block['end']
                        spans.append({'source_version_id': source['id'], 'source_block_index': index,
                                      'quote': self.store.text_range(source, a, b, db), 'start': a, 'end': b})
                        continue
                    try:
                        a, b = self.store.source_span(source, evidence, db)
                    except ValueError as exc:
                        raise ValueError(f'Claim {claim_index} evidence {evidence_index} source quote must be copied exactly from the cited source: {exc}') from exc
                    if self.store.text_range(source, a, b, db) != evidence['quote']:
                        raise ValueError('quote does not match the cited source version')
                    spans.append({**evidence, 'start': a, 'end': b})
                normalized.append({**claim, 'start': start, 'end': end, 'evidence': spans})
            from .figures import report_figures, figure_lines
            for start, end, _, _ in figure_lines(text):
                if any(c["start"] < end and c["end"] > start for c in normalized):
                    raise ValueError("Place citation claims in prose outside figure markup")
            figures = report_figures(self.store, project, text, db)
            report = self.store.put(db, 'report', {'project_id': project, 'revision': revision,
                'text': text, 'text_hash': _digest(text.encode()), 'claims': normalized, 'figures': figures,
                'previous_report_id': current['report_id'], 'review_status': 'not_reviewed'}, project, revision)
            updated = self.store.put(db, 'revision', {**current, 'report_id': report['id'],
                'review_id': None, 'review_status': 'not_reviewed'}, project, revision)
            db.execute('UPDATE projects SET record_id=? WHERE id=?', (updated['id'], project))
            self.store.remember(db, idempotency_key, payload, report)
            return report

    def report(self, identifier: str) -> dict:
        return self.store.get(identifier, kind='report')

    def add_human_review(self, project: str, revision: int, report_id: str, *,
                         expected_review_id: str | None, text: str, verdict: str,
                         idempotency_key: str, anchor=None) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError('Human opinion needs 1–10000 characters')
        if verdict not in {'agree', 'disagree', 'question'}:
            raise ValueError('Choose agree, disagree or question')
        payload = dict(project_id=project, revision=revision, report_id=report_id,
                       review_id=expected_review_id, text=text.strip(), verdict=verdict, anchor=anchor)
        with self.store.transaction() as db:
            previous = self.store.request(db, 'human-review:' + idempotency_key, payload)
            if previous:
                return previous
            current = self.store.expect(project, revision, db)
            if current['report_id'] != report_id or current['review_id'] != expected_review_id:
                raise RevisionConflict('Report or model review changed; reload before recording an opinion')
            report = self.store.get(report_id, db, 'report')
            normalized = None
            if anchor is not None:
                if not isinstance(anchor, dict):
                    raise ValueError('Opinion anchor must identify exact report text')
                a, b = resolve_span(report['text'], anchor)
                normalized = {'start': a, 'end': b, 'text': report['text'][a:b]}
            value = self.store.put(db, 'human_review', {**payload, 'anchor': normalized,
                'report_hash': report['text_hash'], 'source_versions': current['source_versions'],
                'entry_type': 'user_entered_opinion'}, project, revision)
            self.store.remember(db, 'human-review:' + idempotency_key, payload, value)
        return value

    def evidence(self, project: str) -> dict:
        p = self.project(project)
        report = self.report(p['report_id']) if p['report_id'] else None
        review = self.store.get(p['review_id'], kind='review') if p['review_id'] else None
        return {'project': p, 'report': report, 'review': review,
                'human_reviews': self.store.list('human_review', project),
                'sources': [{k: v for k, v in self.source_manifest(s).items() if k != 'text'} for s in p['source_versions']],
                'report_history': [{k: v for k, v in r.items() if k not in {'text', 'claims'}}
                                   for r in self.store.list('report', project)]}
