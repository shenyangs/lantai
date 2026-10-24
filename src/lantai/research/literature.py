"""Read-only public discovery. Retain provider provenance and metadata conflicts.

A search explicitly sends only the supplied query and optional author to Crossref and OpenAlex.
No local evidence packet, user store, credential, or full text is transmitted.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
from html import unescape
from urllib.parse import quote

import httpx

from .evidence import _digest
from .store import canonical


_CSL_TYPES = {
    'crossref': {
        'journal-article': 'article-journal',
        'book': 'book', 'monograph': 'book', 'edited-book': 'book',
        'reference-book': 'book', 'book-chapter': 'chapter',
        'proceedings-article': 'paper-conference', 'posted-content': 'article',
        'dissertation': 'thesis', 'report': 'report', 'dataset': 'dataset',
        'standard': 'standard',
    },
    'openalex': {
        'article': 'article-journal', 'review': 'article-journal',
        'book': 'book', 'book-chapter': 'chapter', 'dissertation': 'thesis',
        'preprint': 'article', 'report': 'report', 'dataset': 'dataset',
    },
}


def normalize_doi(value: str | None) -> str:
    value = (value or '').strip().lower()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'https://dx.doi.org/', 'doi:'):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    return value if re.fullmatch(r'10\.\d{4,9}/\S+', value) else ''


def _record(source: str, raw: dict) -> dict:
    if source == 'crossref':
        authors = [' '.join(filter(None, [a.get('given'), a.get('family')])) or a.get('name', '')
                   for a in raw.get('author', [])]
        dates = raw.get('published', {}).get('date-parts', [[]])
        year = dates[0][0] if dates and dates[0] else None
        titles = raw.get('title') or []
        record = {'source': source, 'doi': normalize_doi(raw.get('DOI')),
                'title': titles[0] if titles else '', 'authors': [name for name in authors if name.strip()],
                'year': year, 'abstract_available': bool(raw.get('abstract')),
                'retracted': None, 'updates': raw.get('update-to', []),
                'url': raw.get('URL'), 'open_access': None}
        csl = {'type': _CSL_TYPES[source].get(raw.get('type'), 'document'),
            'title': record['title'], 'DOI': record['doi'],
            'author': [{**({'family': a['family']} if a.get('family') else {'literal': a.get('name') or authors[i]}),
                        **({'given': a['given']} if a.get('given') else {})} for i, a in enumerate(raw.get('author', [])) if authors[i].strip()],
            'issued': {'date-parts': dates} if year else {}}
        for key in ('volume', 'issue', 'page', 'publisher', 'ISBN', 'ISSN'):
            if raw.get(key): csl[key] = raw[key]
        if raw.get('container-title'): csl['container-title'] = raw['container-title'][0]
        if raw.get('abstract'): csl['abstract'] = unescape(re.sub(r'<[^>]+>', '', raw['abstract']))
        record.update(csl=csl, oa_locations=[])
        return record
    record = {'source': source, 'doi': normalize_doi(raw.get('doi')),
            'title': raw.get('title') or '', 'year': raw.get('publication_year'),
            'authors': [name for a in raw.get('authorships', [])
                        if (name := a.get('author', {}).get('display_name', '')) and name.strip()],
            'abstract_available': bool(raw.get('abstract_inverted_index')),
            'retracted': raw.get('is_retracted'), 'updates': [],
            'url': raw.get('id'), 'open_access': raw.get('open_access')}
    csl = {'type': _CSL_TYPES['openalex'].get(raw.get('type'), 'document'),
        'title': record['title'], 'DOI': record['doi'], 'author': [{'literal': name} for name in record['authors'] if name],
        'issued': {'date-parts': [[record['year']]]} if record['year'] else {}}
    inverted = raw.get('abstract_inverted_index') or {}
    positions = {int(p): word for word, indexes in inverted.items() for p in indexes}
    if positions: csl['abstract'] = ' '.join(positions[p] for p in sorted(positions))
    best = raw.get('best_oa_location') or {}
    location_source = best.get('source') or {}
    if location_source.get('display_name'): csl['container-title'] = location_source['display_name']
    biblio = raw.get('biblio') or {}
    for key in ('volume', 'issue'):
        if biblio.get(key): csl[key] = biblio[key]
    if biblio.get('first_page'): csl['page'] = biblio['first_page'] + ('-' + biblio['last_page'] if biblio.get('last_page') else '')
    locations, urls = [], set()
    for location in [best, *(raw.get('locations') or [])]:
        url = location.get('pdf_url')
        if location.get('is_oa') and url and url not in urls:
            urls.add(url); locations.append({'url': url, 'license': location.get('license'), 'version': location.get('version'),
                'is_oa': True, 'landing_page_url': location.get('landing_page_url'), 'origin': 'openalex'})
    record.update(csl=csl, oa_locations=locations)
    return record


CONNECTOR_VERSION = 'public-metadata-v5'


def query_requests(query, limit=5, *, query_mode='general', author=''):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ValueError('query must contain 1–1000 characters; limit must be 1–20')
    if query_mode not in ('general', 'title_author'):
        raise ValueError('query_mode must be general or title_author')
    if not isinstance(author, str) or len(author) > 200 or any(
        not (c.isalnum() or c in " .'-’") for c in author
    ):
        raise ValueError('author must be a name of at most 200 characters; use spaces instead of search operators or commas')
    if author and query_mode != 'title_author':
        raise ValueError('author requires title_author query mode')
    doi = normalize_doi(query) if query_mode == 'general' else ''
    requests = {}
    for source in ('crossref', 'openalex'):
        endpoint, params = (
            ('https://api.crossref.org/works', {'query.bibliographic': query, 'rows': limit})
            if source == 'crossref' else
            ('https://api.openalex.org/works', {'search': query, 'per-page': limit})
        )
        if query_mode == 'title_author':
            if source == 'crossref':
                if author.strip():
                    params['query.author'] = author
            else:
                params = {'search.title': query, 'per-page': limit}
                if author.strip():
                    params['filter'] = f'raw_author_name.search:"{author}"'
        if doi:
            endpoint = ('https://api.crossref.org/works/' + quote(doi, safe='') if source == 'crossref'
                        else 'https://api.openalex.org/works/https://doi.org/' + quote(doi, safe='/'))
            params = {}
        requests[source] = {'endpoint': endpoint, 'params': params, 'version': CONNECTOR_VERSION}
    return requests


def search_literature(query: str, limit: int = 5, *, client: httpx.Client | None = None, cache=None, query_mode='general', author='') -> dict:
    plan = query_requests(query, limit, query_mode=query_mode, author=author)
    if client is None:
        with httpx.Client(timeout=20, follow_redirects=False,
                          headers={'User-Agent': 'LantaiResearch/1.0 (public metadata)'}) as owned:
            return search_literature(query, limit, client=owned, cache=cache, query_mode=query_mode, author=author)

    doi = normalize_doi(query) if query_mode == 'general' else ''
    cache_query = [query, query_mode, author]

    def fetch(source):
        if cache is not None:
            cached = cache.begin(source, cache_query, limit)
            if cached is not None:
                return source, *cached
        retry_after = None
        def done(state, records):
            state = {**state, 'request': plan[source], 'pagination': 'identifier_lookup' if doi else 'first_page_only'}
            if cache is not None:
                state, records = cache.complete(source, cache_query, limit, state, records, retry_after)
            return source, state, records
        endpoint, params = plan[source]['endpoint'], plan[source]['params']
        try:
            response = client.get(endpoint, params=params)
            if doi and response.status_code == 404:
                return done({'status': 'ok', 'count': 0, 'lookup': 'not_found'}, [])
            retry_after = response.headers.get('Retry-After')
            if response.status_code != 200:
                return done( {'status': 'rate_limited' if response.status_code == 429 else 'error',
                                'http_status': response.status_code}, [])
            data = response.json()
            items = ([data['message'] if source == 'crossref' else data] if doi else
                     data['message']['items'] if source == 'crossref' else data['results'])
            if not isinstance(items, list):
                raise ValueError('invalid results')
            records = [{**_record(source, item), 'raw_record': item,
                        'raw_record_hash': _digest(canonical(item).encode())} for item in items[:limit]]
            if doi and any(r['doi'] != doi for r in records):
                raise ValueError('DOI identity mismatch')
            return done({'status': 'ok', 'count': len(records)}, records)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError):
            return done({'status': 'error', 'reason': 'transport_or_invalid_response'}, [])

    states, papers = {}, {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for source, state, records in pool.map(fetch, ('crossref', 'openalex')):
            states[source] = state
            for i, record in enumerate(records):
                key = record['doi'] or f'{source}:{i}'
                papers.setdefault(key, {'doi': record['doi'], 'records': [], 'fulltext_read': False})['records'].append(record)
    for paper in papers.values():
        records = paper['records']
        primary = records[0]
        paper.update(title=primary['title'], authors=primary['authors'], year=primary['year'])
        issues = []
        if not paper['doi']:
            issues.append('missing_doi')
        if any(not r['authors'] for r in records):
            issues.append('missing_authors')
        if any(not r['title'] for r in records):
            issues.append('missing_title')
        if len({r['title'].strip().casefold() for r in records if r['title']}) > 1:
            issues.append('title_conflict')
        if len({r['year'] for r in records if r['year']}) > 1:
            issues.append('year_conflict')
        paper['retracted'] = True if any(r['retracted'] is True for r in records) else None
        if paper['retracted']:
            issues.append('retracted')
        paper['quality_issues'] = issues
    successes = sum(s['status'] == 'ok' for s in states.values())
    return {'schema_version': 1, 'status': 'ok' if successes == 2 else 'partial' if successes else 'error',
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'query': query, 'query_mode': query_mode, 'author': author,
            'sources': states, 'papers': list(papers.values()), 'scope': 'metadata_only'}
