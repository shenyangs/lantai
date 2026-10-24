"""A local work/edition index, separate from full-text evidence and model review."""
from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path
from uuid import uuid4

from .evidence import _digest
from .literature import normalize_doi, search_literature, query_requests
from .store import ResearchStore, RevisionConflict, canonical

MAX_IMPORT_BYTES = 5_000_000


def _labels(value):
    if not isinstance(value, list) or len(value) > 100 or any(
            not isinstance(label, str) or not label.strip() or len(label) > 200 for label in value):
        raise ValueError('Organization labels need at most 100 nonempty strings of at most 200 characters')
    return list(dict.fromkeys(label.strip() for label in value))


def normalize_csl(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get('title'), str) or not value['title'].strip():
        raise ValueError('Every bibliographic item needs a nonempty title')
    result = copy.deepcopy(value)
    result.pop('id', None)
    result['title'] = result['title'].strip()
    result.setdefault('type', 'article-journal')
    if not isinstance(result['type'], str):
        raise ValueError('CSL type must be a string')
    if result.get('DOI') is not None and not isinstance(result['DOI'], str):
        raise ValueError('DOI must be a string')
    if not isinstance(result.get('custom', {}), dict):
        raise ValueError('Custom metadata must be an object')
    doi = normalize_doi(result.get('DOI'))
    if result.get('DOI') and not doi:
        raise ValueError('Invalid DOI')
    if doi:
        result['DOI'] = doi
    names = result.get('author', [])
    if not isinstance(names, list) or any(not isinstance(n, dict) or not (n.get('family') or n.get('literal')) for n in names):
        raise ValueError('Authors must be CSL name objects')
    if any(any(not isinstance(v, (str, bool)) for v in name.values()) for name in names):
        raise ValueError('CSL name values must be text or flags')
    if len(canonical(result).encode()) > 100_000:
        raise ValueError('Bibliographic item exceeds size limit')
    date = result.get('issued', {})
    if not isinstance(date, dict):
        raise ValueError('Issued date must be a CSL date object')
    if 'date-parts' in date:
        parts = date['date-parts']
        if not isinstance(parts, list) or any(not isinstance(p, list) or not 1 <= len(p) <= 3 or
                any(not isinstance(n, int) or isinstance(n, bool) for n in p) for p in parts):
            raise ValueError('Invalid issued date-parts')
    return result


def _identity(csl):
    if csl.get('DOI'):
        return 'doi:' + csl['DOI']
    # Exact normalized metadata only. Similar titles are never silently merged.
    return 'metadata:' + _digest(canonical({k: csl.get(k) for k in ('title', 'author', 'issued', 'type')}).casefold().encode())


def _issues(csl):
    issues = []
    for field, label in (('DOI', 'missing_doi'), ('author', 'missing_authors'), ('issued', 'missing_date')):
        if not csl.get(field):
            issues.append(label)
    if csl.get('custom', {}).get('retracted'):
        issues.append('retracted')
    return issues


class LibraryService:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ResearchStore(self.workspace)
        with self.store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS work_organization (work TEXT PRIMARY KEY, revision INTEGER, record_id TEXT);
                CREATE TABLE IF NOT EXISTS works (id TEXT PRIMARY KEY, identity TEXT UNIQUE, selected_version_id TEXT);
                CREATE TABLE IF NOT EXISTS work_versions (work TEXT, version TEXT, hash TEXT, PRIMARY KEY(work, hash));
                CREATE TABLE IF NOT EXISTS work_origins (work TEXT, version TEXT, origin TEXT,
                    PRIMARY KEY(work, version, origin));
                CREATE TABLE IF NOT EXISTS work_relations (left_work TEXT, right_work TEXT, relation TEXT,
                    PRIMARY KEY(left_work, right_work, relation));
                CREATE TABLE IF NOT EXISTS source_works (source TEXT PRIMARY KEY, work TEXT, version TEXT);
            ''')

    def import_items(self, values: list[dict], *, origin: dict) -> dict:
        if not isinstance(values, list) or not 1 <= len(values) <= 1000:
            raise ValueError('Import needs 1–1000 items')
        items = [normalize_csl(value) for value in values]  # Validate all before the transaction.
        if len(canonical(items).encode()) > MAX_IMPORT_BYTES:
            raise ValueError('Bibliography exceeds size limit')
        if not isinstance(origin, dict) or not origin.get('provider') or len(canonical(origin)) > 10_000:
            raise ValueError('Import origin is required')
        organizations = []
        for csl in items:
            custom = csl.get('custom', {})
            organization = custom.pop('lantai_organization', None)
            if organization is not None:
                if not isinstance(organization, dict) or set(organization) - {'tags', 'collections', 'metadata_custom_present'}:
                    raise ValueError('Invalid organization labels')
                had_custom = organization.get('metadata_custom_present', False)
                if not isinstance(had_custom, bool):
                    raise ValueError('Invalid organization labels')
                organization = {key: _labels(organization.get(key, [])) for key in ('tags', 'collections')}
                if not custom and not had_custom:
                    csl.pop('custom', None)
            organizations.append(organization)
        results = []
        with self.store.transaction() as db:
            for csl, organization in zip(items, organizations):
                identity = _identity(csl)
                row = db.execute('SELECT * FROM works WHERE identity=?', (identity,)).fetchone()
                work = row['id'] if row else 'work-' + uuid4().hex
                digest = _digest(canonical(csl).encode())
                version = db.execute('SELECT version FROM work_versions WHERE work=? AND hash=?', (work, digest)).fetchone()
                if version:
                    version_id = version['version']
                else:
                    record = self.store.put(db, 'work_version', {'work_id': work, 'csl': csl, 'metadata_hash': digest})
                    version_id = record['id']
                    db.execute('INSERT INTO work_versions VALUES (?,?,?)', (work, version_id, digest))
                if not row:
                    db.execute('INSERT INTO works VALUES (?,?,?)', (work, identity, version_id))
                db.execute('INSERT OR IGNORE INTO work_origins VALUES (?,?,?)', (work, version_id, canonical(origin)))
                if organization is not None and not db.execute('SELECT 1 FROM work_organization WHERE work=?', (work,)).fetchone():
                    self._save_organization(db, work, 1, organization)
                results.append({'work_id': work, 'version_id': version_id, 'duplicate': bool(version),
                                'conflict': bool(row and row['selected_version_id'] != version_id)})
        return {'status': 'ok', 'items': results, 'evidence_level': 'metadata'}

    def work(self, identifier: str, db=None) -> dict:
        if db is None:
            with self.store.transaction() as connection:
                return self.work(identifier, connection)
        row = db.execute('SELECT * FROM works WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise FileNotFoundError('Work not found in this workspace')
        versions = [self.store.get(r['version'], db, 'work_version') for r in
                    db.execute('SELECT version FROM work_versions WHERE work=?', (identifier,)).fetchall()]
        selected = self.store.get(row['selected_version_id'], db, 'work_version')
        origins = [{'version_id': r['version'], **json.loads(r['origin'])} for r in
                   db.execute('SELECT version, origin FROM work_origins WHERE work=?', (identifier,)).fetchall()]
        relations = [dict(r) for r in db.execute('SELECT * FROM work_relations WHERE left_work=? OR right_work=?',
                                               (identifier, identifier)).fetchall()]
        annotations = [v for v in self._records(db, 'library_annotation') if v['work_id'] == identifier]
        links = [dict(r) for r in db.execute('SELECT * FROM source_works WHERE work=?', (identifier,)).fetchall()]
        issues = _issues(selected['csl'])
        if len(versions) > 1:
            issues.append('metadata_conflict')
        organization = self._organization(db, identifier)
        return {'organization': organization, 'work_id': identifier, 'selected_version_id': selected['id'], 'csl': selected['csl'],
                'versions': versions, 'origins': origins, 'relations': relations, 'annotations': annotations,
                'source_versions': links, 'quality_issues': issues,
                'evidence_level': 'fulltext_available' if links else 'metadata', 'fulltext_read': False}

    def _records(self, db, kind):
        return [self.store.get(r['id'], db) for r in db.execute('SELECT id FROM records WHERE kind=?', (kind,)).fetchall()]

    def list(self) -> list[dict]:
        with self.store.transaction() as db:
            return [self.work(r['id'], db) for r in db.execute('SELECT id FROM works ORDER BY rowid DESC').fetchall()]

    def _organization(self, db, work):
        row = db.execute('SELECT revision, record_id FROM work_organization WHERE work=?', (work,)).fetchone()
        if not row:
            return {'revision': 0, 'tags': [], 'collections': []}
        record = self.store.get(row['record_id'], db, 'library_organization')
        return {key: record[key] for key in ('revision', 'tags', 'collections')}

    def _save_organization(self, db, work, revision, labels):
        record = self.store.put(db, 'library_organization', {'work_id': work, 'revision': revision, **labels})
        db.execute('INSERT INTO work_organization VALUES (?,?,?) ON CONFLICT(work) DO UPDATE SET revision=excluded.revision, record_id=excluded.record_id',
                   (work, revision, record['id']))

    def organize(self, work_id: str, expected_revision: int, *, tags: list[str], collections: list[str]) -> dict:
        labels = {'tags': _labels(tags), 'collections': _labels(collections)}
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError('Organization revision must be a nonnegative integer')
        with self.store.transaction() as db:
            work = self.work(work_id, db)
            if work['organization']['revision'] != expected_revision:
                raise RevisionConflict('Library organization changed; reload before editing')
            self._save_organization(db, work_id, expected_revision + 1, labels)
        return self.work(work_id)

    def select_version(self, work_id: str, version_id: str, expected_version_id: str) -> dict:
        with self.store.transaction() as db:
            work = self.work(work_id, db)
            if work['selected_version_id'] != expected_version_id:
                raise RevisionConflict('Selected bibliography version changed; reload')
            if not any(v['id'] == version_id for v in work['versions']):
                raise ValueError('Version does not belong to this work')
            db.execute('UPDATE works SET selected_version_id=? WHERE id=?', (version_id, work_id))
        return self.work(work_id)

    def relate(self, left: str, right: str, relation: str) -> dict:
        if relation not in {'published-version', 'preprint', 'corrects', 'retracts', 'related'} or left == right:
            raise ValueError('Invalid work relation')
        with self.store.transaction() as db:
            self.work(left, db); self.work(right, db)
            db.execute('INSERT OR IGNORE INTO work_relations VALUES (?,?,?)', (left, right, relation))
        return self.work(left)

    def annotate(self, work_id: str, text: str, *, origin: dict, annotation_type='note', anchor=None, region_anchor=None) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise ValueError('Annotation needs 1–100000 characters')
        if anchor is not None and region_anchor is not None:
            raise ValueError('Choose a text anchor or a PDF region, not both')
        with self.store.transaction() as db:
            work = self.work(work_id, db)
            payload = {'work_id': work_id, 'text': text, 'origin': origin, 'annotation_type': annotation_type}
            if origin.get('provider') in {'user', 'assistant-chat', 'user-chat'} or anchor is not None:
                payload['version_id'] = work['selected_version_id']
            if anchor is not None:
                if not isinstance(anchor, dict) or set(anchor) - {'source_version_id', 'quote', 'start', 'end'}:
                    raise ValueError('Invalid annotation anchor')
                link = db.execute('SELECT * FROM source_works WHERE source=? AND work=?',
                                  (anchor.get('source_version_id'), work_id)).fetchone()
                if not link:
                    raise ValueError('Annotation source must be linked to this work')
                source = self.store.get(link['source'], db, 'source')
                start, end = self.store.source_span(source, anchor, db)
                payload['version_id'] = link['version']
                payload['anchor'] = {'source_version_id': source['id'], 'source_hash': source['raw_hash'],
                    'start': start, 'end': end, 'quote': self.store.text_range(source, start, end, db),
                    'pages': [p['page'] for p in source['pages'] if p['start'] < end and p['end'] > start]}
            if region_anchor is not None:
                from .region_annotations import validate_region
                region, version = validate_region(self, db, work_id, region_anchor)
                payload.update(region_anchor=region, version_id=version, annotation_type='region')
            for value in self._records(db, 'library_annotation'):
                if all(value.get(k) == v for k, v in payload.items()):
                    return value
            return self.store.put(db, 'library_annotation', payload)

    def add_note_source(self, annotation_id, project_id, revision, *, idempotency_key):
        from .notes import add_note_source
        return add_note_source(self, annotation_id, project_id, revision, idempotency_key=idempotency_key)

    def link_source(self, work_id: str, source_version_id: str, version_id=None) -> dict:
        with self.store.transaction() as db:
            work = self.work(work_id, db)
            source = self.store.get(source_version_id, db, 'source')
            if source.get('evidence_level') == 'note':
                raise ValueError('A note cannot be linked as the original paper full text')
            version = version_id or work['selected_version_id']
            if not any(v['id'] == version for v in work['versions']):
                raise ValueError('Bibliography version does not belong to work')
            if source.get('evidence_level') == 'abstract':
                raise ValueError('An abstract is not a full-text attachment')
            previous = db.execute('SELECT * FROM source_works WHERE source=?', (source_version_id,)).fetchone()
            if previous and (previous['work'] != work_id or previous['version'] != version):
                raise RevisionConflict('Source already linked to another bibliography version')
            db.execute('INSERT OR IGNORE INTO source_works VALUES (?,?,?)', (source_version_id, work_id, version))
        return self.work(work_id)

    def search(self, project_id, revision, query, limit=5, *, client=None, query_mode='general', author=''):
        requests = query_requests(query, limit, query_mode=query_mode, author=author)
        with self.store.transaction() as db:
            project = self.store.expect(project_id, revision, db)
            if project['policy'] != 'public_search':
                raise ValueError('Project policy does not permit public search')
            if project.get('query_preview_required'):
                value = self.store.put(db, 'query_preview', {
                    'project_id': project_id, 'revision': revision, 'query': query, 'limit': limit,
                    'query_mode': query_mode, 'author': author,
                    'requests': requests, 'status': 'approval_required', 'papers': [], 'sources': {},
                    'message': 'Confirm this exact query in the project literature panel before sending'}, project_id, revision)
                db.execute('INSERT INTO research_query_previews VALUES (?,?,?,NULL)', (value['id'], project_id, 'pending'))
                return value
        return self._search(project_id, revision, query, limit, client=client, query_mode=query_mode, author=author)

    def _search(self, project_id, revision, query, limit, *, client=None, preview_id=None, query_mode='general', author=''):
        with self.store.transaction() as db:
            self.store.expect(project_id, revision, db)
        from .discovery_cache import DiscoveryCache
        result = search_literature(query, limit, client=client, cache=DiscoveryCache(self.store, project_id), query_mode=query_mode, author=author)
        with self.store.transaction() as db:
            self.store.expect(project_id, revision, db)
            value = self.store.put(db, 'search', {**result, 'project_id': project_id, 'revision': revision,
                'query_preview_id': preview_id}, project_id, revision)
            if preview_id:
                db.execute("UPDATE research_query_previews SET status='completed', result_id=? WHERE id=?", (value['id'], preview_id))
            return value

    def search_record(self, search_id):
        return self.store.get(search_id, kind='search')

    def discovery_result(self, search_id, source, capability):
        from .discovery import capability_result
        return capability_result(self.search_record(search_id), source, capability)

    def query_previews(self, project_id):
        with self.store.transaction() as db:
            current = self.store.current(project_id, db)
            items = []
            for row in db.execute('SELECT * FROM research_query_previews WHERE project=? ORDER BY rowid DESC', (project_id,)):
                value = self.store.get(row['id'], db, 'query_preview')
                status = 'stale' if row['status'] == 'pending' and value['revision'] != current['revision'] else row['status']
                items.append({**value, 'status': status, 'result_id': row['result_id']})
            return {'items': items}

    def decide_query(self, preview_id, approve, *, client=None):
        if not isinstance(approve, bool):
            raise ValueError('Query decision must be boolean')
        with self.store.transaction() as db:
            value = self.store.get(preview_id, db, 'query_preview')
            project = self.store.expect(value['project_id'], value['revision'], db)
            if project['policy'] != 'public_search':
                raise ValueError('Project policy does not permit public search')
            if value['requests'] != query_requests(value['query'], value['limit'], query_mode=value.get('query_mode', 'general'), author=value.get('author', '')):
                raise RevisionConflict('Connector changed; create a new query preview')
            row = db.execute('SELECT status FROM research_query_previews WHERE id=?', (preview_id,)).fetchone()
            if row is None or row['status'] != 'pending':
                raise RevisionConflict('Query was already decided; create a new preview to retry')
            status = 'dispatched' if approve else 'cancelled'
            db.execute('UPDATE research_query_previews SET status=? WHERE id=?', (status, preview_id))
            self.store.put(db, 'query_decision', {'preview_id': preview_id, 'approve': approve}, value['project_id'], value['revision'])
        if not approve:
            return {**value, 'status': status}
        # Mark consumed before network I/O. An interrupted request is never silently replayed.
        return self._search(value['project_id'], value['revision'], value['query'], value['limit'], client=client, preview_id=preview_id,
                            query_mode=value.get('query_mode', 'general'), author=value.get('author', ''))

    def import_candidate(self, search_id: str, index: int) -> dict:
        result = self.store.get(search_id, kind='search')
        if not isinstance(index, int) or not 0 <= index < len(result['papers']):
            raise ValueError('Candidate index is out of range')
        paper = result['papers'][index]
        imported = []
        for record in paper['records']:
            csl = record.get('csl') or {'type': 'article-journal', 'title': record['title'], 'DOI': record['doi'],
                'author': [{'literal': n} for n in record['authors'] if n],
                'issued': {'date-parts': [[record['year']]]} if record['year'] else {}}
            csl = {**csl, 'custom': {**csl.get('custom', {}), 'retracted': paper.get('retracted'),
                                   'open_access': record.get('open_access'), 'oa_locations': record.get('oa_locations', [])}}
            imported += self.import_items([csl], origin={'provider': record['source'], 'search_id': search_id,
                                           'retrieved_at': result['sources'][record['source']].get('retrieved_at', result['retrieved_at']),
                                           'cache': result['sources'][record['source']].get('cache', 'unknown'), 'url': record.get('url')})['items']
        return {'status': 'ok', 'items': imported, 'quality_issues': paper['quality_issues']}

    def import_text(self, text: str, format: str) -> dict:
        if not isinstance(text, str) or len(text.encode()) > MAX_IMPORT_BYTES:
            raise ValueError('Import exceeds 5 MB')
        try:
            values = _parse(text, format)
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise ValueError('Invalid bibliography format: ' + str(error)) from error
        return self.import_items(values, origin={'provider': format, 'input_hash': _digest(text.encode())})

    def export(self, format: str, work_ids=None) -> str:
        works = self.list() if work_ids is None else [self.work(w) for w in work_ids]
        values = []
        for work in works:
            value = copy.deepcopy(work['csl'])
            value['id'] = work['work_id']
            if work['organization']['revision']:
                value.setdefault('custom', {})['lantai_organization'] = {
                    'metadata_custom_present': 'custom' in work['csl'],
                    **{key: work['organization'][key] for key in ('tags', 'collections')}}
            values.append(value)
        return _export(values, format)


def _decode_snapshot(value):
    return json.loads(base64.b64decode(value.encode(), validate=True).decode())


def _snapshot(value):
    return base64.b64encode(canonical(value).encode()).decode()


def _parse(text, format):
    if format == 'csl-json':
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError('CSL JSON must be an array')
        return data
    if format == 'bibtex':
        import bibtexparser
        from bibtexparser.customization import splitname
        database = bibtexparser.loads(text)
        if not database.entries:
            raise ValueError('No BibTeX entries')
        values = []
        for entry in database.entries:
            if entry.get('x-lantai-csl'):
                values.append(_decode_snapshot(entry['x-lantai-csl'])); continue
            authors = []
            for name in re.split(r'\s+and\s+', entry.get('author', '')):
                if not name.strip(): continue
                if name.startswith('{') and name.endswith('}'):
                    authors.append({'literal': name[1:-1]}); continue
                parts = splitname(name)
                authors.append({'family': ' '.join(parts['last']), 'given': ' '.join(parts['first']),
                                **({'non-dropping-particle': ' '.join(parts['von'])} if parts['von'] else {}),
                                **({'suffix': ' '.join(parts['jr'])} if parts['jr'] else {})})
            csl = {'title': entry.get('title', ''), 'type': {'article': 'article-journal', 'book': 'book',
                'inproceedings': 'paper-conference', 'incollection': 'chapter', 'phdthesis': 'thesis'}.get(entry['ENTRYTYPE'], 'article'),
                'author': authors}
            for source, target in {'doi': 'DOI', 'journal': 'container-title', 'booktitle': 'container-title',
                    'pages': 'page', 'volume': 'volume', 'number': 'issue', 'publisher': 'publisher',
                    'address': 'publisher-place', 'url': 'URL', 'abstract': 'abstract', 'isbn': 'ISBN', 'edition': 'edition'}.items():
                if entry.get(source): csl[target] = entry[source]
            if re.fullmatch(r'\d{4}', entry.get('year', '')):
                csl['issued'] = {'date-parts': [[int(entry['year'])]]}
            csl['custom'] = {'bibtex_key': entry['ID'], 'unmapped': {k: v for k, v in entry.items() if k not in
                {'ENTRYTYPE', 'ID', 'title', 'author', 'doi', 'journal', 'booktitle', 'pages', 'volume', 'number', 'publisher', 'address', 'url', 'abstract', 'isbn', 'edition', 'year'}}}
            values.append(csl)
        return values
    if format == 'ris':
        import rispy
        values = []
        for entry in rispy.loads(text):
            snapshots = [note[len('LANTAI-CSL:'):] for note in entry.get('notes', []) if note.startswith('LANTAI-CSL:')]
            if snapshots:
                values.append(_decode_snapshot(snapshots[0])); continue
            csl = {'type': {'JOUR': 'article-journal', 'BOOK': 'book', 'CHAP': 'chapter', 'CONF': 'paper-conference',
                           'THES': 'thesis'}.get(entry.get('type_of_reference'), 'article'),
                   'title': entry.get('primary_title') or entry.get('title') or '', 'author': []}
            for name in entry.get('authors', []):
                parts = name.split(',', 1)
                csl['author'].append({'family': parts[0].strip(), **({'given': parts[1].strip()} if len(parts) > 1 else {})})
            for source, target in {'doi': 'DOI', 'secondary_title': 'container-title', 'journal_name': 'container-title',
                'volume': 'volume', 'number': 'issue', 'publisher': 'publisher', 'place_published': 'publisher-place',
                'url': 'URL', 'abstract': 'abstract', 'isbn': 'ISBN'}.items():
                if entry.get(source): csl[target] = entry[source]
            if entry.get('start_page'): csl['page'] = entry['start_page'] + ('-' + entry['end_page'] if entry.get('end_page') else '')
            year = str(entry.get('year', '')).split('/')[0]
            if re.fullmatch(r'\d{4}', year): csl['issued'] = {'date-parts': [[int(year)]]}
            csl['custom'] = {'ris_original': entry}
            values.append(csl)
        if not values: raise ValueError('No RIS entries')
        return values
    raise ValueError('Supported formats: csl-json, bibtex, ris')


def _export(values, format):
    if format == 'csl-json':
        return json.dumps(values, ensure_ascii=False, indent=2)
    if format == 'bibtex':
        import bibtexparser
        database = bibtexparser.bibdatabase.BibDatabase()
        for index, csl in enumerate(values):
            entry = {'ENTRYTYPE': {'article-journal': 'article', 'book': 'book', 'paper-conference': 'inproceedings',
                'chapter': 'incollection', 'thesis': 'phdthesis'}.get(csl['type'], 'misc'),
                'ID': csl.get('id', f'item{index + 1}'), 'title': csl['title'], 'x-lantai-csl': _snapshot(csl)}
            if csl.get('author'):
                entry['author'] = ' and '.join('{' + n['literal'] + '}' if 'literal' in n else
                    ', '.join(filter(None, [n.get('family'), n.get('given')])) for n in csl['author'])
            for target, source in {'doi': 'DOI', 'journal': 'container-title', 'pages': 'page', 'volume': 'volume',
                    'number': 'issue', 'publisher': 'publisher', 'address': 'publisher-place', 'url': 'URL',
                    'abstract': 'abstract', 'isbn': 'ISBN', 'edition': 'edition'}.items():
                if csl.get(source): entry[target] = str(csl[source])
            dates = csl.get('issued', {}).get('date-parts')
            if dates: entry['year'] = str(dates[0][0])
            database.entries.append(entry)
        return bibtexparser.dumps(database)
    if format == 'ris':
        import rispy
        entries = []
        for csl in values:
            entry = {'type_of_reference': {'article-journal': 'JOUR', 'book': 'BOOK', 'chapter': 'CHAP',
                    'paper-conference': 'CONF', 'thesis': 'THES'}.get(csl['type'], 'GEN'),
                'primary_title': csl['title'], 'notes': ['LANTAI-CSL:' + _snapshot(csl)]}
            if csl.get('author'): entry['authors'] = [n.get('literal') or ', '.join(filter(None, [n.get('family'), n.get('given')])) for n in csl['author']]
            for target, source in {'doi': 'DOI', 'secondary_title': 'container-title', 'volume': 'volume',
                    'number': 'issue', 'publisher': 'publisher', 'place_published': 'publisher-place',
                    'url': 'URL', 'abstract': 'abstract', 'isbn': 'ISBN'}.items():
                if csl.get(source): entry[target] = str(csl[source])
            if csl.get('page'): entry['start_page'] = str(csl['page'])
            dates = csl.get('issued', {}).get('date-parts')
            if dates: entry['year'] = str(dates[0][0])
            entries.append(entry)
        return rispy.dumps(entries)
    raise ValueError('Supported formats: csl-json, bibtex, ris')
