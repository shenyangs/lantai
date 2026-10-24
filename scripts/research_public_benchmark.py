#!/usr/bin/env python3
"""Freeze public metadata inputs, then measure the production discovery path.

No model, user content or credentials. Source-selected seed metadata is NOT an
independent expert gold set; relevance and edition labels remain unassigned.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

TOPICS = [
 ('medicine', q) for q in ['randomized blood pressure treatment', 'long covid rehabilitation',
 'antibiotic resistance surveillance', 'diabetes exercise systematic review', 'vaccine effectiveness cohort']
] + [('computing', q) for q in ['retrieval augmented generation evaluation', 'formal verification distributed systems',
 'graph neural networks molecular prediction', 'differential privacy federated learning', 'software supply chain security']
] + [('environment', q) for q in ['urban heat island mitigation', 'climate adaptation agriculture',
 'microplastic marine ecosystem', 'renewable energy storage lifecycle', 'biodiversity conservation land use']
] + [('social', q) for q in ['education randomized tutoring intervention', 'housing affordability urban migration',
 'social media misinformation experiments', 'income inequality health outcomes', 'scientific reproducibility replication']
] + [('chinese', q) for q in ['气候变化与农业适应', '城市热岛缓解', '人工智能教育应用', '老年人慢性病管理', '数字经济与就业',
 '公共卫生风险沟通', '机器学习模型可解释性', '生态系统生物多样性', '新能源储能技术', '科研可重复性']]


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def seed(path):
    import httpx
    if Path(path).exists():
        raise ValueError('Frozen dataset already exists; choose a new version explicitly')
    ids, requests, seen = [], [], set()
    with httpx.Client(timeout=30, follow_redirects=False,
                      headers={'User-Agent': 'LantaiResearchBenchmark/1.0 (public metadata)'}) as client:
        def get(url, params):
            response = client.get(url, params=params)
            response.raise_for_status()  # One attempt; no automatic rate-limit bypass.
            requests.append({'url': str(response.request.url), 'retrieved_at': datetime.now(timezone.utc).isoformat(),
                             'sha256': hashlib.sha256(response.content).hexdigest()})
            time.sleep(1.1)
            return response.json()
        for domain, query, count in [('medicine', 'randomized controlled trial', 20),
                                     ('computing', 'machine learning', 20),
                                     ('environment', 'climate change', 20), ('chinese', '中国', 10)]:
            data = get('https://api.crossref.org/works', {'query': query, 'rows': 100})
            chosen = []
            for item in data['message']['items']:
                doi = item['DOI'].lower()
                if doi in seen or not item.get('title') or not item.get('author'): continue
                seen.add(doi)
                chosen.append({'id': doi, 'title': item['title'][0], 'authors': [
                    ' '.join(filter(None, [a.get('given'), a.get('family')])) or a.get('name', '') for a in item['author']],
                    'domain': domain, 'registry': 'crossref', 'type': item.get('type'),
                    'year': item.get('published', {}).get('date-parts', [[None]])[0][0],
                    'relations': item.get('relation', {}), 'updates': item.get('update-to', []),
                    'seed_request': len(requests) - 1})
                if len(chosen) == count: break
            print(json.dumps({'seed_domain': domain, 'selected': len(chosen)}), flush=True)
            if len(chosen) != count: raise ValueError('Insufficient seed records')
            ids += chosen
        data = get('https://api.datacite.org/dois', {'query': 'climate', 'page[size]': 20})
        for raw in data['data']:
            a = raw['attributes']
            if a['doi'] in seen or not a.get('titles'): continue
            seen.add(a['doi'])
            ids.append({'id': a['doi'], 'title': a['titles'][0]['title'],
                'authors': [v.get('name', '') for v in a.get('creators', [])], 'domain': 'data', 'registry': 'datacite',
                'year': a.get('publicationYear'), 'type': a.get('types', {}).get('resourceTypeGeneral'),
                'relations': a.get('relatedIdentifiers', []), 'seed_request': len(requests) - 1})
            if len(ids) == 80: break
        data = get('https://api.openalex.org/works', {'filter': 'open_access.is_oa:true,has_doi:true', 'per-page': 100})
        fulltext = []
        for raw in data['results']:
            locations = [v for v in raw.get('locations', []) if v.get('pdf_url')]
            if not locations: continue
            loc = locations[0]
            fulltext.append({'id': raw['doi'].removeprefix('https://doi.org/'), 'title': raw['title'],
                'url': loc['pdf_url'], 'license': loc.get('license'), 'is_oa': loc.get('is_oa'),
                'version': loc.get('version'), 'seed_request': len(requests) - 1})
            if len(fulltext) == 30: break
    assert len(ids) == 80 and len(fulltext) == 30
    # Stratify title queries across the first four cohorts; no query is selected after testing.
    titles = [ids[i] for i in list(range(8)) + list(range(20, 28)) + list(range(40, 48)) + list(range(60, 66))]
    save(path, {'schema_version': 1, 'frozen_at': datetime.now(timezone.utc).isoformat(), 'requests': requests,
        'identifiers': ids, 'title_author': [{'id': v['id'], 'query': v['title'] + ' ' + (v['authors'][0] if v['authors'] else ''),
            'domain': v['domain']} for v in titles],
        'topics': [{'domain': d, 'query': q, 'relevance_labels': None} for d, q in TOPICS],
        'fulltext': fulltext, 'limitations': ['Provider-selected input set has selection bias and is not independent gold truth',
            'No expert relevance, edition or retraction labels; missing coverage is explicitly untested',
            'DataCite identifiers are out of Crossref registry coverage', 'Fulltext locations are candidate metadata, not verified files']})


def compact(result):
    return {'status': result['status'], 'sources': result['sources'], 'papers': [
        {'doi': p['doi'], 'records': [{k: r.get(k) for k in ('source', 'doi', 'title', 'authors', 'year',
         'abstract_available', 'retracted', 'updates', 'oa_locations', 'raw_record_hash')} for r in p['records']]}
        for p in result['papers']]}


def summarize(rows):
    out = {}
    for source in ('crossref', 'openalex'):
        out[source] = {}
        for kind in ('identifiers', 'title_author', 'topics'):
            subset = [v for v in rows if v['kind'] == kind]
            good = [v for v in subset if v['result']['sources'][source]['status'] == 'ok']
            covered = [v for v in subset if kind == 'identifiers' and source == 'crossref' and v.get('registry') == 'crossref']
            def found(v):
                return any(r['doi'] == v.get('expected_id') for p in v['result']['papers'] for r in p['records'] if r['source'] == source)
            records = [r for v in good for p in v['result']['papers'] for r in p['records'] if r['source'] == source]
            timings = sorted(v['seconds'] for v in subset)
            out[source][kind] = {'attempts': len(subset), 'source_ok': len(good), 'returned_records': len(records),
                'expected_id_found': sum(found(v) for v in good) if kind != 'topics' else None,
                'known_registry_coverage_attempts': len(covered) if source == 'crossref' and kind == 'identifiers' else None,
                'known_registry_coverage_found': sum(found(v) for v in covered) if source == 'crossref' and kind == 'identifiers' else None,
                'status_counts': {status: sum(v['result']['sources'][source]['status'] == status for v in subset)
                    for status in sorted({v['result']['sources'][source]['status'] for v in subset})},
                'wrong_identifier_accepted': sum(r['doi'] != v['expected_id'] for v in good for p in v['result']['papers']
                    for r in p['records'] if r['source'] == source) if kind == 'identifiers' else None,
                'missing_fields': {f: sum(not r.get(f) for r in records) for f in ('doi', 'title', 'authors', 'year')},
                'query_wall_p50_seconds': statistics.median(timings) if timings else None,
                'query_wall_p95_seconds': timings[min(len(timings)-1, int(len(timings)*.95))] if timings else None,
                'precision_at_10': None, 'ndcg_at_10': None}
    return out


def run(dataset, output):
    from lantai.research.projects import ResearchService
    from lantai.research.library import LibraryService
    from lantai.research.fulltext import fetch_open_pdf, match_pdf_identity, OPEN_LICENSES
    if Path(output).exists():
        raise ValueError('Benchmark output already exists; choose a new run path')
    raw = Path(dataset).read_bytes()
    data = json.loads(raw)
    report = {'dataset_sha256': hashlib.sha256(raw).hexdigest(), 'started_at': datetime.now(timezone.utc).isoformat(),
        'network': 'anonymous public metadata; no API keys; one attempt per query/source; production cache and backoff',
        'rows': [], 'fulltext': [], 'complete': False, 'limitations': data['limitations']}
    with tempfile.TemporaryDirectory(prefix='lantai-public-benchmark-') as directory:
        research = ResearchService(Path(directory))
        library = LibraryService(Path(directory))
        p = research.create_project('Public discovery engineering benchmark', [], policy='public_search', idempotency_key='benchmark')
        for kind in ('identifiers', 'title_author', 'topics'):
            for i, sample in enumerate(data[kind]):
                query = sample['id'] if kind == 'identifiers' else sample['query']
                start = time.perf_counter()
                result = library.search(p['project_id'], 1, query, limit=10)
                report['rows'].append({'kind': kind, 'index': i, 'query': query, 'domain': sample.get('domain'),
                    'expected_id': sample.get('id'), 'registry': sample.get('registry'),
                    'seconds': round(time.perf_counter()-start, 3), 'result': compact(result)})
                save(output, report)
                print(json.dumps({'kind': kind, 'done': i+1, 'status': result['status']}), flush=True)
                time.sleep(1.1)
        for i, sample in enumerate(data['fulltext']):
            start = time.perf_counter()
            result = {'index': i, 'id': sample['id'], 'url': sample['url'], 'license': sample['license'],
                      'download_attempted': False, 'claimed_fulltext_success': False}
            if not sample['is_oa'] or sample['license'] not in OPEN_LICENSES:
                result['status'] = 'license_not_admitted'
            else:
                result['download_attempted'] = True
                try:
                    pdf, final_url = asyncio.run(fetch_open_pdf(sample['url']))
                    identity = match_pdf_identity(pdf, {'DOI': sample['id'], 'title': sample['title']})
                    result.update(status='document_markers_matched', final_url=final_url, bytes=len(pdf), identity=identity)
                except Exception as error:
                    result.update(status='limited', reason=str(error)[:240], error_type=type(error).__name__)
            result['seconds'] = round(time.perf_counter()-start, 3)
            report['fulltext'].append(result)
            save(output, report)
            print(json.dumps({'kind': 'fulltext', 'done': i+1, 'status': result['status']}), flush=True)
            time.sleep(1.1)
    report.update(complete=True, finished_at=datetime.now(timezone.utc).isoformat(), summary=summarize(report['rows']))
    save(output, report)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['seed', 'run'])
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.mode == 'seed': seed(args.dataset)
    elif args.output: run(args.dataset, args.output)
    else: parser.error('--output is required for run')
