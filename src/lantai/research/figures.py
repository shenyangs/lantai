"""Immutable project-owned raster figures; never resolve report text as a path/URL."""
from __future__ import annotations

import base64
import binascii
from io import BytesIO
import re
import warnings

from .evidence import _digest
from .store import ResearchStore

MAX_BYTES = 6 * 1024 * 1024
PROJECT_BYTES = 30 * 1024 * 1024
FIGURE_LINE = re.compile(r'^ {0,3}!\[([^\]\n]*)\]\(research-figure:(figure-[0-9a-f]{32})\)\s*$')


def figure_lines(text, *, strict=True):
    """Yield source offsets and explicit standalone references outside fenced code."""
    fence, offset = None, 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None and 'research-figure:' in line:
            match = FIGURE_LINE.fullmatch(line.rstrip('\r\n'))
            if not match:
                if strict:
                    raise ValueError('Use a standalone ![caption](research-figure:figure-ID) line for each figure')
                offset += len(line)
                continue
            if len(match.group(1)) > 300:
                raise ValueError('Figure caption must not exceed 300 characters')
            yield offset, offset + len(line.rstrip('\r\n')), match.group(1), match.group(2)
        offset += len(line)


def metadata(record):
    return {key: record[key] for key in ('id', 'project_id', 'sha256', 'media_type', 'bytes', 'width', 'height')}


class FigureService:
    def __init__(self, workspace, *, store=None):
        self.store = store if store is not None else ResearchStore(workspace)

    def upload(self, project, revision, data, *, idempotency_key):
        if not isinstance(data, str) or not data or len(data) > 4 * ((MAX_BYTES + 2) // 3):
            raise ValueError('Figure must be a PNG or JPEG no larger than 6 MiB')
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('Figure must be base64 PNG or JPEG') from exc
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError('Figure must be a PNG or JPEG no larger than 6 MiB')
        from PIL import Image, UnidentifiedImageError
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as image:
                    if image.format not in {'PNG', 'JPEG'} or getattr(image, 'n_frames', 1) != 1:
                        raise ValueError('Only static PNG or JPEG figures are supported')
                    width, height = image.size
                    if width * height > 16_000_000:
                        raise ValueError('Figure exceeds 16 million pixels')
                    media_type = Image.MIME[image.format]
                    image.verify()
                with Image.open(BytesIO(raw)) as image:
                    image.load()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError('Invalid PNG or JPEG figure') from exc
        payload = dict(project_id=project, revision=revision, sha256=_digest(raw))
        with self.store.transaction() as db:
            existing = self.store.request(db, 'figure:' + idempotency_key, payload)
            if existing:
                return metadata(existing)
            self.store.expect(project, revision, db)
            used = sum(self.store.get(row['id'], db, 'figure')['bytes'] for row in db.execute(
                "SELECT id FROM records WHERE project=? AND kind='figure'", (project,)))
            if used + len(raw) > PROJECT_BYTES:
                raise ValueError('Project figure storage exceeds 30 MiB')
            record = self.store.put(db, 'figure', {**payload, 'data': data, 'bytes': len(raw),
                'width': width, 'height': height, 'media_type': media_type}, project, revision)
            self.store.remember(db, 'figure:' + idempotency_key, payload, record)
            return metadata(record)

    def load(self, project, identifier, db=None):
        record = self.store.get(identifier, db, 'figure')
        if record['project_id'] != project:
            raise ValueError('Figure is outside this project')
        raw = base64.b64decode(record['data'], validate=True)
        if len(raw) != record['bytes'] or _digest(raw) != record['sha256']:
            raise ValueError('Figure integrity mismatch')
        return record


def report_figures(store, project, text, db):
    service = FigureService(store.workspace, store=store)
    records = {}
    total = 0
    for _, _, _, identifier in figure_lines(text):
        if identifier not in records:
            records[identifier] = metadata(service.load(project, identifier, db))
        total += records[identifier]['bytes']
        if total > PROJECT_BYTES:
            raise ValueError('Report figures exceed 30 MiB, including repeated references')
    return list(records.values())


def portable_markdown(text, figures):
    for start, end, caption, identifier in reversed(list(figure_lines(text, strict=False))):
        record = figures.get(identifier)
        if record is None:
            continue
        text = text[:start] + f"![{caption}](data:{record['media_type']};base64,{record['data']})" + text[end:]
    return text
