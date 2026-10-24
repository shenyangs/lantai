"""Project-scoped metadata cache and workspace-local per-source admission.

One network attempt per source and request; subsequent requests honor cooldowns.
No sleeps or automatic retry loops, and failed responses are never zero hits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
from uuid import uuid4
import time

from .evidence import _digest
from .literature import CONNECTOR_VERSION
from .store import canonical

VERSION = CONNECTOR_VERSION


def retry_delay(value, now):
    try:
        delay = float(value) if str(value).strip().isdigit() else parsedate_to_datetime(value).timestamp() - now
        return max(0.0, delay) if math.isfinite(delay) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


class DiscoveryCache:
    def __init__(self, store, project):
        self.store, self.project, self.clock = store, project, time.time
        self._claims = {}

    def key(self, source, query, limit):
        return _digest(canonical([VERSION, source, query, limit]).encode())

    def begin(self, source, query, limit):
        now = self.clock()
        key = self.key(source, query, limit)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM research_discovery_cache WHERE project=? AND key=?', (self.project, key)).fetchone()
            if row and row['expires'] > now:
                if _digest(row['payload'].encode()) != row['hash']:
                    raise ValueError('discovery cache integrity mismatch')
                value = json.loads(row['payload'])
                return {**value['state'], 'cache': 'hit', 'network_sent': False}, value['records']
            state = db.execute('SELECT * FROM research_discovery_sources WHERE source=?', (source,)).fetchone()
            if state and state['retry_at'] > now:
                return {'status': state['status'], 'retry_at': state['retry_at'], 'network_sent': False,
                        'cache': 'miss', 'reason': 'source_cooldown'}, []
            failures = state['failures'] if state else 0
            token = uuid4().hex
            self._claims[source] = token
            db.execute('INSERT OR REPLACE INTO research_discovery_sources VALUES (?,?,?,?,?)',
                       (source, now + 120, failures, 'busy', token))
            # Expired entries contain no useful cache value; keep immutable search receipts separately.
            db.execute('DELETE FROM research_discovery_cache WHERE expires<=?', (now,))
        return None

    def complete(self, source, query, limit, state, records, retry_after=None):
        now = self.clock()
        state = {**state, 'retrieved_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                 'cache': 'miss', 'network_sent': True, 'connector_version': VERSION}
        with self.store.transaction() as db:
            row = db.execute('SELECT failures, token FROM research_discovery_sources WHERE source=?', (source,)).fetchone()
            if row is None or row['token'] != self._claims.get(source):
                return state, records
            failures = 0 if state['status'] == 'ok' else (row['failures'] if row else 0) + 1
            wait = 1 if not failures else max(retry_delay(retry_after, now), 300 if failures >= 3 else 2 ** failures)
            next_status = 'rate_limited' if state['status'] == 'rate_limited' else 'circuit_open' if failures >= 3 else 'backoff' if failures else 'busy'
            db.execute('INSERT OR REPLACE INTO research_discovery_sources VALUES (?,?,?,?,?)', (source, now + wait, failures, next_status, row['token']))
            if failures:
                state['retry_at'] = now + wait
            elif db.execute('SELECT 1 FROM projects WHERE id=?', (self.project,)).fetchone():
                state['negative_cache'] = not records
                expires = now + (3600 if not records else 86400)
                state['expires_at'] = datetime.fromtimestamp(expires, timezone.utc).isoformat()
                payload = canonical({'state': state, 'records': records})
                db.execute('INSERT OR REPLACE INTO research_discovery_cache VALUES (?,?,?,?,?,?)',
                           (self.project, self.key(source, query, limit), payload, _digest(payload.encode()), now, expires))
        return state, records
