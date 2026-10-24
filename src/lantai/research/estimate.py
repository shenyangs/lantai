"""Read-only preflight: indexed workload and empirical per-call references.

Historical extrema are observations, not prediction intervals. Whole-workflow
cost and time remain unknown until a calibrated end-to-end model exists.
"""
from __future__ import annotations

import json
import time

from .budget import normalize_budget, usage
from .evidence import _digest
from .inference import _number
from .sources import source_is_current
from .store import canonical


def _range(values, *, integer=False):
    known = [n for value in values if (n := _number(value, integer=integer)) is not None]
    return {'min': min(known) if known else None, 'max': max(known) if known else None,
            'known_samples': len(known)}


def estimate(service, project, revision):
    with service.store.transaction() as db:
        current = service.store.expect(project, revision, db)
        sources = [service.store.get(s, db, 'source') for s in current['source_versions']]
        read = {(r['source'], r['block']) for r in db.execute(
            'SELECT source,block FROM reading WHERE project=? AND revision=?', (project, revision))}
        charged = {(r['source'], r['block']) for r in db.execute(
            'SELECT source,block FROM research_reading_usage WHERE project=?', (project,))}
        remaining = [(s['id'], b) for s in sources for b in s['blocks'] if (s['id'], b['index']) not in read]
        chars = sum(b['end'] - b['start'] for _, b in remaining)
        additional = sum(b['end'] - b['start'] for sid, b in remaining if (sid, b['index']) not in charged)
        limits, used = normalize_budget(current.get('budget')), usage(service.store, db, project)
        rows = db.execute('SELECT * FROM research_model_calls WHERE project=? AND revision=? ORDER BY started,id',
                          (project, revision)).fetchall()
        groups = {}
        for row in rows:
            if row['status'] == 'completed':
                groups.setdefault((row['config_hash'], row['surface']), []).append(row)
        history = []
        for (config_hash, surface), samples in groups.items():
            config = json.loads(samples[0]['config'])
            usages = [json.loads(r['usage']) if r['usage'] else {} for r in samples]
            history.append({'config_hash': config_hash, 'surface': surface,
                'provider': config.get('provider'), 'model': config.get('model'), 'samples': len(samples),
                'tokens': _range([u.get('tokens') for u in usages], integer=True),
                'seconds': _range([r['seconds'] for r in samples]),
                'cost_cny': _range([u.get('cost_cny') for u in usages])})
        incomplete = [s['id'] for s in sources if s['coverage'] != 'complete']
        stale = [s['id'] for s in sources if not source_is_current(service.workspace, s)]
        allowance = max(0, limits['reading_chars'] - used['reading_chars'])
        result = {
            'schema_version': 1,
            'binding': {'project_id': project, 'revision': revision, 'report_id': current['report_id'],
                        'source_versions': current['source_versions']},
            'reading': {'status': 'unknown' if not sources else 'partial' if incomplete or stale else 'indexed',
                        'remaining_blocks': len(remaining), 'remaining_chars': chars,
                        'additional_budget_chars': additional, 'budget_remaining_chars': allowance,
                        'fits_budget': additional <= allowance,
                        'incomplete_sources': incomplete, 'stale_sources': stale},
            'workflow': {'status': 'unknown', 'reason': 'no_calibrated_end_to_end_baseline',
                         'model_calls': None, 'tokens': None, 'seconds': None, 'cost_cny': None},
            'historical_calls': history,
            'historical_scope': 'current_revision_completed_calls_only_not_a_forecast',
            'excluded_incomplete_calls': sum(r['status'] != 'completed' for r in rows),
            'exhausted_budgets': [k for k in limits if used[k] >= limits[k]],
        }
        result['fingerprint'] = _digest(canonical(result).encode())
        return {**result, 'generated_at': time.time()}
