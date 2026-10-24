"""Workspace-local transactional index and immutable, hash-checked records."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .evidence import _digest, _within


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class RevisionConflict(ValueError):
    """The current project/report changed; reload before applying the edit."""


class ResearchStore:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.directory = _within(self.workspace, '.lantai/research')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = _within(self.workspace, '.lantai/research/index.sqlite3')
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, project TEXT,
                    revision INTEGER, payload TEXT NOT NULL, hash TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS records_project ON records(project, kind, created);
                CREATE TABLE IF NOT EXISTS source_text_chunks (hash TEXT PRIMARY KEY, text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS library_note_sources (path TEXT PRIMARY KEY, record_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, revision INTEGER, record_id TEXT);
                CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, hash TEXT, result_id TEXT);
                CREATE TABLE IF NOT EXISTS reading (
                    project TEXT, revision INTEGER, source TEXT, block INTEGER,
                    PRIMARY KEY(project, revision, source, block));
                CREATE TABLE IF NOT EXISTS archived_projects (project TEXT PRIMARY KEY, archived REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS research_model_calls (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, revision INTEGER NOT NULL,
                    config_hash TEXT NOT NULL, config TEXT NOT NULL, surface TEXT NOT NULL,
                    started REAL NOT NULL, allowance REAL NOT NULL, seconds REAL,
                    status TEXT NOT NULL, usage TEXT, finished REAL);
                CREATE INDEX IF NOT EXISTS research_model_calls_project ON research_model_calls(project);
                CREATE TABLE IF NOT EXISTS research_review_attempts (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, revision INTEGER NOT NULL,
                    started REAL NOT NULL, allowance REAL NOT NULL, seconds REAL, status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS research_review_attempts_project ON research_review_attempts(project);
                CREATE TABLE IF NOT EXISTS research_reading_usage (
                    project TEXT, source TEXT, block INTEGER, chars INTEGER NOT NULL,
                    PRIMARY KEY(project, source, block));
                CREATE TABLE IF NOT EXISTS research_discovery_cache (
                    project TEXT, key TEXT, payload TEXT NOT NULL, hash TEXT NOT NULL,
                    retrieved REAL NOT NULL, expires REAL NOT NULL, PRIMARY KEY(project, key));
                CREATE TABLE IF NOT EXISTS research_discovery_sources (
                    source TEXT PRIMARY KEY, retry_at REAL NOT NULL, failures INTEGER NOT NULL, status TEXT NOT NULL, token TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_query_previews (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, status TEXT NOT NULL, result_id TEXT);
                CREATE TABLE IF NOT EXISTS project_purges (project TEXT PRIMARY KEY, revision INTEGER, files TEXT NOT NULL);
            ''')
            # executescript ends its transaction. Serialize the one-time migration
            # before reading old records so concurrent initializers cannot race.
            db.execute('BEGIN IMMEDIATE')
            if db.execute('PRAGMA user_version').fetchone()[0] < 1:
                last_source = None
                source = None
                for row in db.execute('SELECT DISTINCT project, source, block FROM reading ORDER BY source'):
                    if row['source'] != last_source:
                        source = self.get(row['source'], db, 'source')
                        last_source = row['source']
                    block = source['blocks'][row['block']]
                    db.execute('INSERT OR IGNORE INTO research_reading_usage VALUES (?,?,?,?)',
                               (row['project'], row['source'], row['block'], block['end'] - block['start']))
                db.execute('PRAGMA user_version=1')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, db, kind: str, value: dict, project=None, revision=None) -> dict:
        value = {**value, 'id': kind + '-' + uuid4().hex, 'schema_version': 1,
                 'kind': kind, 'created_at': time.time()}
        payload = canonical(value)
        db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?)',
                   (value['id'], kind, project, revision, payload, _digest(payload.encode()), time.time()))
        return value

    def get(self, identifier: str, db=None, kind=None) -> dict:
        if db is None:
            with self.transaction() as connection:
                return self.get(identifier, connection, kind)
        row = db.execute('SELECT payload, hash FROM records WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise FileNotFoundError('Research record not found in this workspace')
        if _digest(row['payload'].encode()) != row['hash']:
            raise ValueError('research record integrity mismatch')
        value = json.loads(row['payload'])
        if kind and value['kind'] != kind:
            raise ValueError('wrong research record type')
        return value

    def current(self, project: str, db) -> dict:
        row = db.execute('SELECT record_id FROM projects WHERE id=?', (project,)).fetchone()
        if row is None:
            raise FileNotFoundError('Research project not found in this workspace')
        return self.get(row['record_id'], db, 'revision')

    def expect(self, project: str, revision: int, db) -> dict:
        value = self.current(project, db)
        if value['revision'] != revision:
            raise RevisionConflict('Research revision changed; reload the project')
        if db.execute('SELECT 1 FROM archived_projects WHERE project=?', (project,)).fetchone():
            raise ValueError('Research project is archived; restore it before making changes')
        return value

    def request(self, db, key: str, payload: dict) -> dict | None:
        if not isinstance(key, str) or not 1 <= len(key) <= 160:
            raise ValueError('idempotency key is required (1–160 characters)')
        digest = _digest(canonical(payload).encode())
        row = db.execute('SELECT hash, result_id FROM requests WHERE key=?', (key,)).fetchone()
        if row:
            if row['hash'] != digest:
                raise ValueError('idempotency key was reused for different input')
            return self.get(row['result_id'], db)
        return None

    def remember(self, db, key: str, payload: dict, result: dict):
        db.execute('INSERT INTO requests VALUES (?,?,?)',
                   (key, _digest(canonical(payload).encode()), result['id']))

    def list(self, kind: str, project=None) -> list[dict]:
        with self.transaction() as db:
            rows = db.execute('SELECT id FROM records WHERE kind=? AND (? IS NULL OR project=?) '
                              'ORDER BY created DESC', (kind, project, project)).fetchall()
            return [self.get(row['id'], db) for row in rows]

    def put_text_chunk(self, db, text: str) -> str:
        digest = _digest(text.encode())
        db.execute('INSERT OR IGNORE INTO source_text_chunks VALUES (?,?)', (digest, text))
        row = db.execute('SELECT text FROM source_text_chunks WHERE hash=?', (digest,)).fetchone()
        if row['text'] != text:
            raise ValueError('source chunk integrity mismatch')
        return digest

    def text_range(self, source, start=0, end=None, db=None):
        length = len(source['text']) if 'text' in source else source['text_length']
        end = length if end is None else min(end, length)
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= length:
            raise ValueError('Invalid source text range')
        if 'text' in source:
            return source['text'][start:end]
        if db is None:
            with self.transaction() as connection:
                return self.text_range(source, start, end, connection)
        parts = []
        position = start
        for chunk in source['text_chunks']:
            if chunk['end'] <= start or chunk['start'] >= end:
                continue
            row = db.execute('SELECT text FROM source_text_chunks WHERE hash=?', (chunk['hash'],)).fetchone()
            if row is None or _digest(row['text'].encode()) != chunk['hash'] or len(row['text']) != chunk['end'] - chunk['start']:
                raise ValueError('source chunk integrity mismatch')
            a, b = max(start, chunk['start']), min(end, chunk['end'])
            if a != position:
                raise ValueError('source chunk range integrity mismatch')
            parts.append(row['text'][a - chunk['start']:b - chunk['start']])
            position = b
        if position != end:
            raise ValueError('source chunk range integrity mismatch')
        text = ''.join(parts)
        if start == 0 and end == length and _digest(text.encode()) != source['text_hash']:
            raise ValueError('source text integrity mismatch')
        return text

    def source_span(self, source, evidence, db):
        from .anchors import resolve_span
        if evidence.get('start') is not None or evidence.get('end') is not None:
            a, b = evidence.get('start'), evidence.get('end')
            if type(a) is not int or type(b) is not int:
                raise ValueError('Supply both offsets or an exact text anchor')
            return resolve_span(self.text_range(source, a, b, db), evidence, text_key='quote', base=a)
        quote = evidence.get('quote')
        if not isinstance(quote, str) or not quote:
            raise ValueError('A nonempty exact text quote/anchor is required')
        if 'text' in source:
            return resolve_span(source['text'], evidence, text_key='quote')
        tail, position = '', 0
        found = None
        for chunk in source['text_chunks']:
            piece = self.text_range(source, chunk['start'], chunk['end'], db)
            window = tail + piece
            base = position - len(tail)
            at = window.find(quote)
            while at >= 0:
                match = base + at
                if found is not None and match != found:
                    raise ValueError('Exact quote/anchor is ambiguous; supply verified offsets')
                found = match
                at = window.find(quote, at + 1)
            position += len(piece)
            tail = window[-(len(quote) - 1):] if len(quote) > 1 else ''
        if found is None:
            raise ValueError('Exact quote/anchor is absent from the supplied version')
        return found, found + len(quote)
