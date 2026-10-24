"""Explicit, licensed OA PDF import with pinned public HTTPS and revision checks."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import unicodedata
from io import BytesIO
from urllib.parse import urljoin, urlparse

from ..app.mcp.network_security import resolve_mcp_endpoint, create_pinned_mcp_http_client
from .evidence import _digest, _source

OPEN_LICENSES = {'cc0', 'cc-by', 'cc-by-sa', 'cc-by-nc', 'cc-by-nc-sa', 'cc-by-nd', 'cc-by-nc-nd', 'public-domain'}
MAX_PDF_BYTES = 20_000_000


async def _public_endpoint(url):
    if urlparse(url).scheme != 'https':
        raise ValueError('Full text needs public HTTPS')
    endpoint = await resolve_mcp_endpoint(url)
    if not ipaddress.ip_address(endpoint.pinned_ip).is_global:
        raise ValueError('Private address rejected for full-text download')
    return endpoint


async def fetch_open_pdf(url: str) -> tuple[bytes, str]:
    for _ in range(6):
        endpoint = await _public_endpoint(url)
        async with create_pinned_mcp_http_client(endpoint) as client:
            async with client.stream('GET', url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get('location')
                    if not location: raise ValueError('PDF redirect has no target')
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise ValueError(f'Full-text server returned HTTP {response.status_code}')
                length = response.headers.get('content-length')
                if length and (not length.isdigit() or int(length) > MAX_PDF_BYTES):
                    raise ValueError('Full text exceeds the 20 MB limit')
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_PDF_BYTES: raise ValueError('Full text exceeds the 20 MB limit')
                    chunks.append(chunk)
        data = b''.join(chunks)
        if not data.startswith(b'%PDF-'):
            raise ValueError('Server did not return a PDF; import an authorized copy manually')
        from pypdf import PdfReader
        pdf = PdfReader(BytesIO(data))
        if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 2000:
            raise ValueError('PDF is encrypted, empty, or exceeds the page budget')
        return data, url
    raise ValueError('Full text has too many redirects')


def match_pdf_identity(data: bytes, csl: dict) -> dict:
    """Conservative document-marker match, not semantic verification or full reading."""
    from pypdf import PdfReader
    from .literature import normalize_doi

    def title_key(value):
        return ''.join(c for c in unicodedata.normalize('NFKC', str(value)).casefold() if c.isalnum())

    failure = 'PDF identity could not be established; import and associate an authorized copy manually'
    expected_doi = normalize_doi(csl.get('DOI'))
    expected_title = title_key(csl.get('title') or '')
    if not expected_doi or len(expected_title) < 12:
        raise ValueError(failure)
    try:
        pdf = PdfReader(BytesIO(data))
        if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 2000:
            raise ValueError(failure)
        first_page = pdf.pages[0].extract_text() or ''
        declared_title = title_key((pdf.metadata or {}).get('/Title') or '')
    except Exception as exc:
        raise ValueError(failure) from exc
    # Generic PDF generator metadata is unknown, never positive identity evidence.
    if declared_title not in {'', 'untitled', 'document', 'microsoftword'} and declared_title != expected_title:
        raise ValueError(failure)
    identifiers = {normalize_doi(value.rstrip('.,;:')) for value in
                   re.findall(r'10\.\d{4,9}/[^\s<>"\[\]]+', first_page, re.IGNORECASE)}
    if expected_title not in title_key(first_page) or expected_doi not in identifiers:
        raise ValueError(failure)
    return {'method': 'pdf_document_markers_v1', 'doi': expected_doi,
            'title_hash': _digest(expected_title.encode()), 'raw_hash': _digest(data),
            'first_page_hash': _digest(first_page.encode()),
            'scope': 'first_page_title_and_exact_doi; not semantic or edition verification'}


async def import_open_fulltext(research, library, project_id: str, revision: int, work_id: str,
                               location_index: int, idempotency_key: str, agent_id=None):
    # Check policy and the exact metadata version before any outbound request.
    payload = {'project': project_id, 'revision': revision, 'work': work_id, 'location': location_index}
    with research.store.transaction() as db:
        completed = research.store.request(db, 'fulltext:' + idempotency_key, payload)
        if completed: return completed
        checkpoint = research.store.request(db, 'fulltext-source:' + idempotency_key, payload)
        if not checkpoint:
            project = research.store.expect(project_id, revision, db)
            if project['policy'] != 'public_search':
                raise ValueError('Project policy does not permit public full-text retrieval')
            work = library.work(work_id, db)
    if not checkpoint:
        locations = work['csl'].get('custom', {}).get('oa_locations', [])
        if not isinstance(location_index, int) or not 0 <= location_index < len(locations):
            raise ValueError('No recorded open-access PDF at this index')
        location = locations[location_index]
        license = (location.get('license') or '').lower()
        if not location.get('is_oa') or license not in OPEN_LICENSES:
            raise ValueError('A known open license is required; import an authorized copy manually')
        data, final_url = await fetch_open_pdf(location['url'])
        identity = await asyncio.to_thread(match_pdf_identity, data, work['csl'])
        with research.store.transaction() as db:
            research.store.expect(project_id, revision, db)
            if library.work(work_id, db)['selected_version_id'] != work['selected_version_id']:
                raise ValueError('Bibliography version changed while downloading; retry')
        from ..app.wiki.core import import_single_wiki_source
        info, _, _ = await asyncio.to_thread(import_single_wiki_source, research.workspace,
            'research/' + _digest(data)[:24] + '.pdf', data, agent_id)
        with research.store.transaction() as db:
            research.store.expect(project_id, revision, db)
            checkpoint = research.store.put(db, 'fulltext_checkpoint', {
                'path': info.path, 'paths': list(dict.fromkeys([*project['source_paths'], info.path])),
                'version_id': work['selected_version_id'], 'requested_url': location['url'],
                'final_url': final_url, 'license': license, 'raw_hash': _digest(data), 'identity': identity,
                'imported_hash': _digest(_source(research.workspace, info.path).read_bytes())}, project_id, revision)
            research.store.remember(db, 'fulltext-source:' + idempotency_key, payload, checkpoint)
    identity = checkpoint.get('identity') or {}
    if identity.get('method') != 'pdf_document_markers_v1' or identity.get('raw_hash') != checkpoint['raw_hash']:
        raise ValueError('PDF identity is missing from the checkpoint; restart with a new import request')
    with research.store.transaction() as db:
        if library.work(work_id, db)['selected_version_id'] != checkpoint['version_id']:
            raise ValueError('Bibliography version changed; select the imported edition before resuming')
    if _digest(_source(research.workspace, checkpoint['path']).read_bytes()) != checkpoint['imported_hash']:
        raise ValueError('Imported full text changed before completion')
    updated = await asyncio.to_thread(research.revise_project, project_id, revision,
        source_paths=checkpoint['paths'], idempotency_key='fulltext-revision:' + idempotency_key)
    with research.store.transaction() as db:
        research.store.expect(project_id, updated['revision'], db)
    source = next(research.source(s) for s in updated['source_versions'] if research.source(s)['path'] == checkpoint['path'])
    await asyncio.to_thread(library.link_source, work_id, source['id'], checkpoint['version_id'])
    with research.store.transaction() as db:
        research.store.expect(project_id, updated['revision'], db)
        receipt = research.store.put(db, 'fulltext_import', {'project_id': project_id, 'revision': updated['revision'],
            'work_id': work_id, 'version_id': checkpoint['version_id'], 'source_version_id': source['id'],
            **{k: checkpoint[k] for k in ('requested_url', 'final_url', 'license', 'raw_hash', 'identity')},
            'fulltext_read': False}, project_id, updated['revision'])
    result = {'project': research.project(project_id), 'receipt': receipt, 'work': library.work(work_id)}
    with research.store.transaction() as db:
        result = research.store.put(db, 'fulltext_result', result, project_id, updated['revision'])
        research.store.remember(db, 'fulltext:' + idempotency_key, payload, result)
    return result
