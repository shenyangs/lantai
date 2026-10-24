"""Durable dispatch evidence. Token limits guard subsequent calls, not billing."""
from __future__ import annotations

import asyncio
import math
import time
from uuid import uuid4

from .budget import normalize_budget
from .context import accounted_dispatch
from .evidence import _digest
from .store import canonical

GENERATION_FIELDS = {'temperature', 'top_p', 'seed', 'max_tokens', 'max_output_tokens',
                     'max_completion_tokens', 'reasoning_effort'}


def summary(store, db, project):
    import json
    rows = db.execute('SELECT * FROM research_model_calls WHERE project=? ORDER BY started,id', (project,)).fetchall()
    configs, tokens, cost, unknown_tokens, unknown_cost, seconds = {}, 0, 0.0, 0, 0, 0.0
    for row in rows:
        configs.setdefault(row['config_hash'], {'version': row['config_hash'], **json.loads(row['config'])})
        usage = json.loads(row['usage']) if row['usage'] else {}
        tokens += usage.get('tokens') or 0
        cost += usage.get('cost_cny') or 0
        unknown_tokens += int(usage.get('tokens') is None or row['status'] != 'completed')
        unknown_cost += int(usage.get('cost_cny') is None or row['status'] != 'completed')
        seconds += row['seconds'] if row['seconds'] is not None else row['allowance']
    return {'calls': len(rows), 'known_tokens': tokens, 'unknown_token_calls': unknown_tokens,
            'known_cost_cny': cost if rows and unknown_cost < len(rows) else None,
            'unknown_cost_calls': unknown_cost, 'seconds': seconds, 'configurations': list(configs.values()),
            'token_limit_mode': 'observed_next_call', 'surfaces': sorted({r['surface'] for r in rows})}


def reserve(service, project, revision, configuration, surface):
    config = canonical(configuration)
    with service.store.transaction() as db:
        current = service.store.expect(project, revision, db)
        limits = normalize_budget(current.get('budget'))
        used = summary(service.store, db, project)
        remaining = limits['model_seconds'] - used['seconds']
        if used['calls'] >= limits['model_calls'] or used['known_tokens'] >= limits['model_tokens'] or remaining <= 0:
            raise ValueError('Research model budget exhausted; raise project limits to continue. Draft and checkpoints are preserved.')
        identifier = uuid4().hex
        db.execute('INSERT INTO research_model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (identifier, project, revision, _digest(config.encode()), config, surface,
                    time.time(), remaining, None, 'running', None, None))
    return identifier, remaining


def _number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    if integer and int(value) != value:
        return None
    return int(value) if integer else value


def normalize_usage(value):
    if hasattr(value, 'model_dump'):
        # Defaults on normalized provider objects are not affirmative zero-usage evidence.
        value = value.model_dump(exclude_unset=True)
    value = value if isinstance(value, dict) else {}
    total = _number(value.get('total_tokens'), integer=True)
    if total is None:
        incoming = _number(value.get('input_tokens', value.get('prompt_tokens')), integer=True)
        outgoing = _number(value.get('output_tokens', value.get('completion_tokens')), integer=True)
        if incoming is not None and outgoing is not None:
            total = incoming + outgoing
    cost = _number(value.get('cost_cny'))
    return {'tokens': total, 'cost_cny': cost}


def finish(service, identifier, elapsed, status, usage):
    with service.store.transaction() as db:
        db.execute('UPDATE research_model_calls SET seconds=?,status=?,usage=?,finished=? '
                   'WHERE id=? AND status=?', (elapsed, status, canonical(normalize_usage(usage)), time.time(), identifier, 'running'))


class ResearchModelClient:
    """Wrap one logical model dispatch, including its existing retries/failover."""

    def __init__(self, inner, service, project, revision, *, surface, generation=None, endpoint=None):
        self._inner, self.service, self.project, self.revision = inner, service, project, revision
        self.provider_id, self.model_name = inner.provider_id, inner.model_name
        self.surface = surface
        self.generation = generation or {}
        self.endpoint_hash = _digest(endpoint.encode()) if endpoint else None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def configuration(self, request):
        combined = {**self.generation, **request.generation}
        generation = {key: value for key, value in combined.items()
                      if key in GENERATION_FIELDS and (
                          key == 'reasoning_effort' and value in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'auto')
                          or key != 'reasoning_effort' and type(value) in (int, float) and math.isfinite(value))}
        return {'provider': self.provider_id, 'model': self.model_name, 'generation': generation,
                'endpoint_hash': self.endpoint_hash, 'generation_hash': _digest(canonical(combined).encode())}

    def _reserve(self, request):
        return reserve(self.service, self.project, self.revision, self.configuration(request), self.surface)

    async def complete(self, request):
        identifier, allowance = self._reserve(request)
        started, status, usage = time.monotonic(), 'interrupted', None
        try:
            with accounted_dispatch():
                response = await asyncio.wait_for(self._inner.complete(request), timeout=allowance)
            usage, status = response.usage, 'completed'
            return response
        finally:
            finish(self.service, identifier, time.monotonic() - started, status, usage)

    async def stream(self, request):
        identifier, allowance = self._reserve(request)
        started, status, usage = time.monotonic(), 'interrupted', None
        stream = None
        try:
            with accounted_dispatch():
                stream = self._inner.stream(request).__aiter__()
            while True:
                remaining = allowance - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError('Research model time budget exhausted; progress is preserved')
                try:
                    with accounted_dispatch():
                        event = await asyncio.wait_for(anext(stream), timeout=remaining)
                except StopAsyncIteration:
                    status = 'completed'
                    break
                observed = event.usage or getattr(event.response, 'usage', None)
                if observed is not None:
                    usage = observed
                yield event
        finally:
            try:
                if stream is not None and hasattr(stream, 'aclose'):
                    with accounted_dispatch():
                        await stream.aclose()
            finally:
                finish(self.service, identifier, time.monotonic() - started, status, usage)
