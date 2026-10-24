"""Lossless source-window projection for semantic review, without selection or summarization."""
from __future__ import annotations


def evidence_contexts(store, sources, evidence):
    """Share only overlapping/adjacent windows within the same immutable source.

    Evidence order, quotes and claim mappings stay intact. Each item retains its
    original +/-1500-character window as absolute source offsets into a group.
    The union is budgeted before reading text; disjoint gaps are never filled.
    """
    windows = []
    for item in evidence:
        source = sources[item['source_version_id']]
        length = len(source['text']) if 'text' in source else source['text_length']
        item['context_start'] = max(0, item['start'] - 1500)
        item['context_end'] = min(length, item['end'] + 1500)
        windows.append(item)
    groups = []
    for item in sorted(windows, key=lambda e: (e['source_version_id'], e['context_start'], e['context_end'])):
        if (groups and groups[-1]['source_version_id'] == item['source_version_id']
                and item['context_start'] <= groups[-1]['end']):
            groups[-1]['end'] = max(groups[-1]['end'], item['context_end'])
        else:
            groups.append({'source_version_id': item['source_version_id'],
                'start': item['context_start'], 'end': item['context_end']})
        item['context_index'] = len(groups) - 1
    if sum(g['end'] - g['start'] for g in groups) > 120_000:
        raise ValueError('Review evidence exceeds context budget; divide the report explicitly')
    return [{**group, 'text': store.text_range(sources[group['source_version_id']], group['start'], group['end'])}
        for group in groups]
