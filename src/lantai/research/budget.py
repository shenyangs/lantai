"""Durable project limits. These measure work, not provider tokens or money."""
from __future__ import annotations

import time
from uuid import uuid4

DEFAULT_BUDGET = {'reading_chars': 2_000_000, 'review_calls': 100, 'review_seconds': 1800,
                  'model_calls': 1000, 'model_tokens': 1_000_000, 'model_seconds': 3600}


def normalize_budget(value=None, previous=None):
    if value is not None and (not isinstance(value, dict) or set(value) - set(DEFAULT_BUDGET)):
        raise ValueError('Invalid research budget keys')
    result = {**DEFAULT_BUDGET, **(previous or {}), **(value or {})}
    if any(type(v) is not int or v <= 0 or v > 1_000_000_000 for v in result.values()):
        raise ValueError('Research budget limits must be positive integers, at most 1000000000')
    return result


def usage(store, db, project):
    chars = db.execute('SELECT COALESCE(SUM(chars),0) FROM research_reading_usage WHERE project=?', (project,)).fetchone()[0]
    row = db.execute('SELECT COUNT(*), COALESCE(SUM(COALESCE(seconds,allowance)),0) '
                     'FROM research_review_attempts WHERE project=?', (project,)).fetchone()
    # Interrupted processes retain their entire reservation; no invisible refund.
    from .inference import summary
    models = summary(store, db, project)
    return {'reading_chars': chars, 'review_calls': row[0], 'review_seconds': row[1],
            'model_calls': models['calls'], 'model_tokens': models['known_tokens'], 'model_seconds': models['seconds']}


def reserve_review(service, project, revision, report_id):
    with service.store.transaction() as db:
        current = service.store.expect(project, revision, db)
        if current['report_id'] != report_id:
            raise ValueError('Report changed before budget reservation')
        limits = normalize_budget(current.get('budget'))
        used = usage(service.store, db, project)
        seconds = limits['review_seconds'] - used['review_seconds']
        if used['review_calls'] >= limits['review_calls'] or seconds <= 0:
            raise ValueError('Research review budget exhausted; raise project limits to continue. Draft and checkpoints are preserved.')
        identifier = uuid4().hex
        db.execute('INSERT INTO research_review_attempts VALUES (?,?,?,?,?,?,?)',
                   (identifier, project, revision, time.time(), seconds, None, 'running'))
    return identifier, seconds


def finish_review(service, identifier, seconds, status):
    with service.store.transaction() as db:
        db.execute('UPDATE research_review_attempts SET seconds=MIN(allowance,?), status=? WHERE id=?',
                   (seconds, status, identifier))
