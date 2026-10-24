"""Pinned, offline CSL processor. No hand-built author/date or numeric rules."""
from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .evidence import _digest
from .library import normalize_csl

DATA = Path(__file__).with_name('data')
STYLES = {'apa': 'apa.csl', 'numeric': 'ieee.csl', 'gb7714': 'gb7714.csl'}


def _xml_object(text):
    # Use the processor's documented XmlJSON input. The CSL logic stays
    # entirely in the unmodified, pinned processor.
    def node(element):
        children = [element.text.strip()] if element.text and element.text.strip() else []
        for child in element:
            children.append(node(child))
            if child.tail and child.tail.strip():
                children.append(child.tail.strip())
        if not children and element.tag.rsplit('}', 1)[-1] in {'term', 'single', 'multiple'}:
            children = ['']
        attributes = {key.replace('{http://www.w3.org/XML/1998/namespace}', 'xml:'): value
                      for key, value in element.attrib.items()}
        return {'name': element.tag.rsplit('}', 1)[-1], 'attrs': attributes, 'children': children}
    return node(ET.fromstring(text))


def validate_citation_output(result, clusters):
    """Check processor IDs, never infer identities from rendered citation text."""
    expected = {cite['id'] for cluster in clusters for cite in cluster}
    entries = result.get('bibliography_parameters', {}).get('entry_ids', [])
    actual = [identifier for entry in entries for identifier in entry]
    if (len(result.get('citations', [])) != len(clusters)
            or any(not isinstance(value, str) or not value.strip() for value in result.get('citations', []))
            or len(entries) != len(result.get('bibliography', []))
            or any(not value.strip() for value in result.get('bibliography', []))
            or any(not entry for entry in entries)
            or len(actual) != len(set(actual)) or set(actual) != expected):
        raise ValueError('Citation consistency check failed; bibliography and body must refer to the same versions')
    return actual


def format_citations(items: list[dict], clusters: list[list[dict]], style='gb7714') -> dict:
    if style not in STYLES or not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise ValueError('Choose apa, numeric or gb7714 and supply 1–1000 items')
    values = {}
    for value in items:
        identifier = value.get('id') if isinstance(value, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in values:
            raise ValueError('Bibliography IDs must be unique nonempty strings')
        values[identifier] = {**normalize_csl(value), 'id': identifier}
    if not isinstance(clusters, list) or not 1 <= len(clusters) <= 1000:
        raise ValueError('Supply 1–1000 citation clusters')
    for cluster in clusters:
        if not isinstance(cluster, list) or not 1 <= len(cluster) <= 100:
            raise ValueError('Citation cluster needs 1–100 items')
        for cite in cluster:
            if not isinstance(cite, dict) or cite.get('id') not in values:
                raise ValueError('Citation refers to an unknown bibliographic version')
            if set(cite) - {'id', 'locator', 'label', 'prefix', 'suffix', 'suppress-author', 'author-only'}:
                raise ValueError('Unsupported citation option')
    manifest = json.loads((DATA / 'manifest.json').read_text())

    def checked(name):
        data = (DATA / name).read_bytes()
        expected = manifest['engine']['sha256'] if name == 'citeproc.js' else manifest['files'][name]['sha256']
        if _digest(data) != expected:
            raise ValueError('Bundled citation data failed integrity check')
        return data.decode('utf-8')

    import quickjs
    context = quickjs.Context()
    context.set_memory_limit(256 * 1024 * 1024)
    context.set_max_stack_size(8 * 1024 * 1024)
    context.set_time_limit(30)
    context.eval('var module = {exports:{}}; var console = {log:function(){}};')
    context.eval(checked('citeproc.js'))
    locales = {name: _xml_object(checked('locales-' + name + '.xml')) for name in ('en-US', 'en-GB', 'zh-CN', 'zh-TW')}
    payload = {'items': values, 'clusters': clusters, 'style': _xml_object(checked(STYLES[style])), 'locales': locales}
    context.eval('var input = ' + json.dumps(payload, ensure_ascii=False) + ';')
    try:
        output = context.eval('''
            var CSL = module.exports;
            var processor = new CSL.Engine({
                retrieveLocale:function(lang) {return input.locales[lang] || input.locales['en-US'];},
                retrieveItem:function(id) {return input.items[id];}
            }, input.style);
            processor.setOutputFormat('text');
            var pre = [], strings = [];
            input.clusters.forEach(function(items, index) {
                var citation = {citationID:'c-'+index, citationItems:items, properties:{noteIndex:0}};
                var result = processor.processCitationCluster(citation, pre, []);
                result[1].forEach(function(update) {strings[update[0]] = update[1];});
                pre.push([citation.citationID, 0]);
            });
            var bib = processor.makeBibliography();
            JSON.stringify({citations:strings, bibliography:bib ? bib[1] : [],
                bibliography_parameters:bib ? bib[0] : {}});
        ''')
        result = json.loads(output)
    except (quickjs.JSException, ValueError) as error:
        raise ValueError('CSL formatting failed: ' + str(error)) from error
    result['bibliography'] = [line.strip() for line in result['bibliography']]
    bibliography_ids = validate_citation_output(result, clusters)
    warnings = []
    for identifier in bibliography_ids:
        item = values[identifier]
        fields = ['author', 'issued']
        if item.get('type') == 'article-journal':
            fields.append('container-title')
        date = item.get('issued', {})
        present = {'author': bool(item.get('author')),
                   'issued': bool(date.get('date-parts')) or any(
                       isinstance(date.get(key), str) and date[key].strip() for key in ('raw', 'literal')),
                   'container-title': bool(str(item.get('container-title', '')).strip())}
        missing = [field for field in fields if not present[field]]
        if missing:
            warnings.append({'id': identifier, 'missing_fields': missing})
    result['audit'] = {'schema_version': 1, 'status': 'consistent',
        'scope': 'structured_citations_only', 'clusters': [[cite['id'] for cite in cluster] for cluster in clusters],
        'bibliography_ids': bibliography_ids, 'metadata_warnings': warnings}
    result['provenance'] = {'engine': 'citeproc-js', 'engine_version': manifest['engine']['version'],
        'engine_sha256': manifest['engine']['sha256'], 'style': style,
        'style_sha256': manifest['files'][STYLES[style]]['sha256'],
        'metadata_hash': _digest(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()),
        'attribution': 'Citations formatted with citeproc-js, © Frank Bennett; CSL styles/locales by the Citation Style Language project.'}
    return result
