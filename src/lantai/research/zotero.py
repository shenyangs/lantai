"""Read-only Zotero local API adapter; never modifies the source library."""
from __future__ import annotations

from html.parser import HTMLParser
import re
import json

import httpx

from .evidence import _digest
from .library import LibraryService, _labels
from .store import canonical

BASE = 'http://127.0.0.1:23119/api/'


class _PlainNote(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []
    def handle_data(self, data): self.parts.append(data)
    def handle_endtag(self, tag):
        if tag in {'p', 'div', 'br', 'li'}: self.parts.append('\n')


def zotero_csl(data):
    value = {'type': {'journalArticle': 'article-journal', 'book': 'book', 'bookSection': 'chapter',
                     'conferencePaper': 'paper-conference', 'thesis': 'thesis', 'report': 'report',
                     'webpage': 'webpage', 'preprint': 'article'}.get(data['itemType'], 'article'),
             'title': data.get('title', ''), 'author': []}
    for creator in data.get('creators', []):
        role = {'author': 'author', 'editor': 'editor', 'translator': 'translator'}.get(creator.get('creatorType'))
        if role:
            name = {'literal': creator['name']} if creator.get('name') else {
                'family': creator.get('lastName', ''), 'given': creator.get('firstName', '')}
            if name.get('family') or name.get('literal'): value.setdefault(role, []).append(name)
    for source, target in {'DOI': 'DOI', 'publicationTitle': 'container-title', 'bookTitle': 'container-title',
            'publisher': 'publisher', 'place': 'publisher-place', 'volume': 'volume', 'issue': 'issue',
            'pages': 'page', 'edition': 'edition', 'url': 'URL', 'abstractNote': 'abstract',
            'ISBN': 'ISBN', 'ISSN': 'ISSN', 'language': 'language'}.items():
        if data.get(source): value[target] = data[source]
    date = re.match(r'^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?', data.get('date', ''))
    if date: value['issued'] = {'date-parts': [[int(n) for n in date.groups() if n]]}
    elif data.get('date'): value['issued'] = {'raw': data['date']}
    value['custom'] = {'zotero_key': data.get('key'), 'zotero_version': data.get('version'),
                       'zotero_item_type': data['itemType'], 'tags': data.get('tags', []),
                       'collections': data.get('collections', []), 'extra': data.get('extra', '')}
    return value


def import_local_zotero(library: LibraryService, *, client: httpx.Client | None = None) -> dict:
    if client is None:
        with httpx.Client(timeout=httpx.Timeout(20, connect=2), follow_redirects=False, trust_env=False,
                          headers={'Zotero-API-Version': '3'}) as owned:
            return import_local_zotero(library, client=owned)
    try:
        root = client.get(BASE)
        if root.status_code == 403:
            return {'status': 'permission_required', 'reason': 'Enable Zotero local API in Advanced settings', 'readonly': True}
        if root.status_code != 200:
            return {'status': 'unavailable', 'http_status': root.status_code, 'readonly': True}
        instance = root.headers.get('Zotero-Server-ID')
        revision = None
        records = []
        for start in range(0, 10_001, 100):
            response = client.get(BASE + 'users/0/items', params={'limit': 100, 'start': start, 'format': 'json'})
            if response.status_code == 403:
                return {'status': 'permission_required', 'reason': 'Zotero local API access is disabled', 'readonly': True}
            response.raise_for_status()
            current_instance = response.headers.get('Zotero-Server-ID')
            current_revision = response.headers.get('Last-Modified-Version')
            if instance and current_instance and current_instance != instance or revision and current_revision and current_revision != revision:
                return {'status': 'retry_required', 'reason': 'Zotero database changed during import', 'readonly': True}
            instance = instance or current_instance
            revision = revision or current_revision
            batch = response.json()
            if not isinstance(batch, list) or any(not isinstance(item, dict) or not isinstance(item.get('data'), dict) for item in batch):
                raise ValueError('Invalid Zotero response')
            if start == 10_000 and batch:
                return {'status': 'limit_exceeded', 'reason': 'Select a smaller library (maximum 10000 items)', 'readonly': True}
            records.extend(batch)
            if len(batch) < 100: break
        warnings = [] if instance else ['instance_identity_unavailable: pre-Zotero-10 records use content hashes']
        collections = {}
        if any(r['data'].get('collections') for r in records):
            for start in range(0, 10_001, 100):
                response = client.get(BASE + 'users/0/collections', params={'limit': 100, 'start': start, 'format': 'json'})
                if response.status_code in {403, 404, 501}:
                    warnings.append('collections_unavailable'); collections = {}; break
                response.raise_for_status()
                current_instance = response.headers.get('Zotero-Server-ID')
                current_revision = response.headers.get('Last-Modified-Version')
                if (instance and current_instance and current_instance != instance or
                        revision and current_revision and current_revision != revision):
                    return {'status': 'retry_required', 'reason': 'Zotero database changed during import', 'readonly': True}
                batch = response.json()
                if not isinstance(batch, list) or any(not isinstance(c, dict) or not isinstance(c.get('data'), dict) or not isinstance(c['data'].get('name'), str) for c in batch):
                    raise ValueError('Invalid Zotero collections')
                if start == 10_000 and batch:
                    return {'status': 'limit_exceeded', 'reason': 'More than 10000 collections', 'readonly': True}
                collections.update({c['key']: c for c in batch})
                if len(batch) < 100: break

        def mapped_csl(data):
            csl = zotero_csl(data)
            mappings, labels = [], []
            for key in data.get('collections', []):
                path, visited, current = [], set(), key
                while current:
                    if current in visited or current not in collections:
                        warnings.append('collection_path_unresolved:' + str(key)); path = []; break
                    visited.add(current)
                    record = collections[current]
                    path.insert(0, record['data']['name'])
                    current = record['data'].get('parentCollection')
                if path:
                    mappings.append({'key': key, 'version': collections[key].get('version'), 'path': path})
                    label = ' / '.join(path)
                    if len(label) <= 200: labels.append(label)
                    else: warnings.append('collection_path_too_long:' + str(key))
            csl['custom']['collection_mapping'] = mappings
            tags = _labels([tag['tag'] for tag in data.get('tags', [])])
            csl['custom']['lantai_organization'] = {'tags': tags, 'collections': _labels(labels), 'metadata_custom_present': True}
            return csl

        origin = {'provider': 'zotero-local', 'instance_id': instance or 'unidentified-local-database',
                  'library_id': 0, 'local_api_version': 3}
        bibliographic = [record for record in records if record['data'].get('itemType') not in {'attachment', 'note', 'annotation'}]
        # All primary records validate together, so a malformed source cannot half-import.
        from .library import normalize_csl
        for record in bibliographic: normalize_csl(mapped_csl({**record['data'], 'key': record['key'], 'version': record.get('version', 0)}))
        mapping = {}
        imported = []
        for record in bibliographic:
            data = {**record['data'], 'key': record['key'], 'version': record.get('version', 0)}
            identity = {**origin, 'item_key': record['key'], 'item_version': record.get('version', 0),
                        'content_hash': _digest(canonical(data).encode())}
            result = library.import_items([mapped_csl(data)], origin=identity)['items'][0]
            mapping[record['key']] = result['work_id']; imported.append(result)
        attachments = {r['key']: r['data'].get('parentItem') for r in records if r['data'].get('itemType') == 'attachment'}
        attachment_records = {r['key']: r for r in records if r['data'].get('itemType') == 'attachment'}
        annotation_count = 0
        for record in records:
            data = record['data']; type = data.get('itemType')
            if type not in {'attachment', 'note', 'annotation'}: continue
            parent = data.get('parentItem')
            work_id = mapping.get(parent) or mapping.get(attachments.get(parent))
            if not work_id: continue
            if type == 'note':
                parser = _PlainNote(); parser.feed(data.get('note', '')); text = ''.join(parser.parts).strip()
            elif type == 'annotation':
                text = '\n'.join(filter(None, [data.get('annotationText'), data.get('annotationComment')]))
            else:
                text = 'Attachment available in Zotero (not read): ' + (data.get('title') or record['key'])
            external_anchor = None
            if type == 'annotation':
                attachment = attachment_records.get(parent, {})
                position = data.get('annotationPosition')
                try:
                    position = json.loads(position) if isinstance(position, str) else position
                except ValueError:
                    warnings.append('annotation_position_unparsed:' + record['key'])
                external_anchor = {'attachment_key': parent, 'attachment_version': attachment.get('version'),
                    'attachment_content_hash': _digest(canonical(attachment.get('data', {})).encode()),
                    'page_label': data.get('annotationPageLabel'), 'position': position,
                    'quote': data.get('annotationText'), 'annotation_type': data.get('annotationType'),
                    'verified_against_local_source': False}
            if text:
                library.annotate(work_id, text, origin={**origin, 'item_key': record['key'],
                    'parent_item': parent, 'item_version': record.get('version', 0),
                    'content_hash': _digest(canonical(data).encode()), 'attachment_unread': type == 'attachment',
                    **({'external_anchor': external_anchor} if external_anchor else {})}, annotation_type=type)
                annotation_count += 1
        return {'status': 'partial' if warnings else 'ok', 'origin': origin, 'items': imported,
                'annotations': annotation_count, 'readonly': True,
                'warnings': list(dict.fromkeys(warnings)),
                'fulltext_read': False}
    except httpx.ConnectError:
        return {'status': 'unavailable', 'reason': 'Zotero local API is not running', 'readonly': True}
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
        return {'status': 'error', 'reason': type(error).__name__, 'readonly': True}
