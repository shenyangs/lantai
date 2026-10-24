"""Stream source snapshots into immutable text chunks with complete page maps."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from .evidence import _source, _within


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for piece in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(piece)
    return digest.hexdigest()


def extract_source(workspace: Path, relative: str, *, put_chunk, evidence_level='fulltext', local_ocr=False) -> dict:
    if evidence_level not in {'fulltext', 'abstract', 'excerpt'}:
        raise ValueError('source evidence level must be fulltext, abstract or excerpt')
    path = _source(workspace, relative, max_bytes=None)
    suffix = path.suffix.lower()
    directory = _within(workspace, '.lantai/research/blobs')
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ('.' + uuid4().hex + '.tmp')
    digest = hashlib.sha256()
    try:
        with path.open('rb') as original, temporary.open('xb') as snapshot:
            for piece in iter(lambda: original.read(1024 * 1024), b''):
                digest.update(piece)
                snapshot.write(piece)
        raw_hash = digest.hexdigest()
        blob = directory / (raw_hash + suffix)
        try:
            os.link(temporary, blob)
        except FileExistsError:
            if file_hash(blob) != raw_hash:
                raise ValueError('source snapshot integrity mismatch')
    finally:
        temporary.unlink(missing_ok=True)
    issues, blocks, mappings, chunks = [], [], [], []
    text_hash = hashlib.sha256()
    length = 0
    nonempty = False

    def append(text, page, *, block=True):
        nonlocal length, nonempty
        for offset in range(0, len(text), 6000):
            piece = text[offset:offset + 6000]
            start = length
            length += len(piece)
            text_hash.update(piece.encode())
            nonempty = nonempty or bool(piece.strip())
            chunks.append({'start': start, 'end': length, 'hash': put_chunk(piece)})
            if block:
                blocks.append({'index': len(blocks), 'page': page, 'start': start, 'end': length})

    if suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(blob)
        if reader.is_encrypted:
            raise ValueError('Encrypted PDF: supply a readable authorized copy')
        from .pdf_layout import PdfLayout, recognize_page, ocr_text
        with PdfLayout(blob) as layout:
            for number, page in enumerate(reader.pages, 1):
                try:
                    text = page.extract_text(extraction_mode='layout') or ''
                    error = '' if text.strip() else 'no_text_or_scanned_page'
                except Exception:
                    text, error = '', 'page_extraction_failed'
                origin = 'native_text'
                regions = layout.regions(number, text) if text.strip() else []
                if not text.strip() and local_ocr:
                    text, regions = ocr_text(recognize_page(blob, number))
                    origin = 'local_vision_ocr'
                    error = ('ocr_low_confidence' if any(r['confidence'] < .8 for r in regions)
                             else 'ocr_requires_visual_verification') if text.strip() else 'local_ocr_unavailable_or_no_text'
                if length:
                    append('\n\n', None, block=False)
                start = length
                append(text, number)
                mappings.append({'page': number, 'printed_page': None, 'text': None, 'issue': error,
                    'start': start, 'end': length, 'text_origin': origin,
                    'coordinate_system': 'normalized_top_left',
                    'layout_status': 'mapped' if regions else 'unavailable_or_unaligned',
                    'regions': [{**r, 'start': r['start'] + start, 'end': r['end'] + start} for r in regions]})
        method = 'pypdf_layout_all_pages_v3_geometry' + ('_local_ocr' if local_ocr else '')
        issues.append('PDF tables, figures and reading order require visual verification when used as evidence')
    elif suffix in {'.txt', '.md', '.markdown', '.csv', '.json', '.jsonl'}:
        with blob.open('r', encoding='utf-8-sig', newline='') as handle:
            for piece in iter(lambda: handle.read(6000), ''):
                append(piece, None)
        mappings = [{'page': None, 'text': None, 'issue': '', 'start': 0, 'end': length}]
        method = 'utf8_complete_v2_chunked'
    else:
        from ..app.wiki.core import extract_source_text_detail
        detail = extract_source_text_detail(blob)
        if not detail.get('text_complete'):
            issues.append('parser_did_not_establish_complete_coverage')
        append(str(detail.get('text') or ''), None)
        mappings = [{'page': None, 'text': None, 'issue': '', 'start': 0, 'end': length}]
        method = str(detail.get('method') or 'unknown')
    if not nonempty and suffix != '.pdf':
        raise ValueError('source has no extractable text')
    if file_hash(path) != raw_hash:
        raise ValueError('source changed during extraction; retry')
    return {'path': relative, 'raw_hash': raw_hash, 'text_hash': text_hash.hexdigest(),
            'text_length': length, 'text_chunks': chunks,
            'snapshot_path': str(blob.relative_to(workspace)), 'parser': method,
            'pages': mappings, 'blocks': blocks, 'issues': issues,
            'coverage': 'partial' if any(p['issue'] for p in mappings) or
                        'parser_did_not_establish_complete_coverage' in issues else 'complete',
            'evidence_level': evidence_level, 'work_id': None}


def source_is_current(workspace: Path, source: dict) -> bool:
    try:
        return file_hash(_source(workspace, source['path'], max_bytes=None)) == source['raw_hash']
    except (OSError, ValueError):
        return False
