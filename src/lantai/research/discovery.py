"""Uniform capability views over an immutable, policy-admitted search snapshot.

This module never sends requests. New remote data goes through Library.search,
including the project's query preview, policy, cache and rate limits.
"""
from __future__ import annotations

from .literature import normalize_doi

CAPABILITIES = ('search', 'resolve_identifier', 'get_metadata', 'get_relations', 'locate_fulltext')
SOURCES = ('crossref', 'openalex')


def capabilities(source):
    if source not in SOURCES:
        raise ValueError('Unknown research discovery source')
    return {name: name != 'locate_fulltext' or source == 'openalex' for name in CAPABILITIES}


def capability_result(snapshot, source, capability):
    supported = capabilities(source)
    if capability not in CAPABILITIES:
        raise ValueError('Unknown research discovery capability')
    state = snapshot.get('sources', {}).get(source, {})
    receipt = {key: state[key] for key in ('status', 'reason', 'http_status', 'retry_at',
        'cache', 'negative_cache', 'network_sent', 'request') if key in state}
    receipt.update(source=source, capability=capability, capabilities=supported,
        connector_version=state.get('connector_version') or state.get('request', {}).get('version') or 'unknown',
        query=snapshot['query'], retrieved_at=state.get('retrieved_at', snapshot.get('retrieved_at')),
        search_id=snapshot.get('id'), pagination=state.get('pagination', 'unknown'),
        completeness='source_snapshot_only', fulltext_fetched=False,
        status=state.get('status', 'unavailable'), items=[], raw_record_refs=[], count=0)
    if not supported[capability]:
        receipt.update(status='unavailable', reason='connector_capability_not_supported')
        return receipt
    if capability == 'resolve_identifier' and not normalize_doi(snapshot['query']):
        receipt.update(status='unavailable', reason='requires_exact_doi_search_snapshot')
        return receipt
    if receipt['status'] != 'ok':
        return receipt
    for paper_index, paper in enumerate(snapshot.get('papers', [])):
        for record_index, record in enumerate(paper.get('records', [])):
            if record.get('source') != source:
                continue
            raw = record.get('raw_record') or {}
            receipt['raw_record_refs'].append({'search_id': snapshot.get('id'),
                'paper_index': paper_index, 'record_index': record_index,
                'sha256': record.get('raw_record_hash')})
            item = {'doi': record.get('doi', ''), 'title': record.get('title', '')}
            if capability == 'get_relations':
                if source == 'crossref':
                    item.update(relations=raw.get('relation', {}), updates=raw.get('update-to', []))
                else:
                    item.update(referenced_works=raw.get('referenced_works', []),
                                related_works=raw.get('related_works', []))
                item['retracted'] = record.get('retracted')
            elif capability == 'locate_fulltext':
                item['locations'] = record.get('oa_locations', [])
            else:
                item = record
            receipt['items'].append(item)
    receipt['count'] = len(receipt['items'])
    return receipt
