"""Immutable local evidence packets; deterministic checks never infer meaning.

Only explicitly selected knowledge sources are read. Review assertions remain
attributed input, distinct from byte/quote integrity. No model or network calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_PACKET_CHARS = 2_000_000
# Current knowledge imports live under .wiki; retain the historical roots for
# existing research records, without admitting generated pages or private state.
SOURCE_ROOTS = ('.wiki/raw/sources', 'wiki/raw/sources', 'wiki/sources')


class Citation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_id: str = Field(min_length=1, max_length=80)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=100_000)
    relation: Literal['supports', 'contradicts', 'insufficient', 'pending'] = 'pending'
    reviewer: str = Field(default='', max_length=200)
    rationale: str = Field(default='', max_length=10_000)


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    claim_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=20_000)
    evidence: list[Citation] = Field(default_factory=list, max_length=50)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_hash(data: dict) -> str:
    return _digest(json.dumps({k: v for k, v in data.items() if k != 'record_hash'},
                              ensure_ascii=False, sort_keys=True).encode())


def _within(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('path must be relative to this workspace')
    current = root.resolve()
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink paths are not research sources or stores')
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('path escapes workspace')
    return resolved


def _source(root: Path, relative: str, *, max_bytes=MAX_SOURCE_BYTES) -> Path:
    p = Path(relative)
    if not any(p.is_relative_to(Path(prefix)) and p != Path(prefix) for prefix in SOURCE_ROOTS):
        raise ValueError('select an imported knowledge source file')
    path = _within(root, relative)
    if not path.is_file():
        raise ValueError('source is missing')
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise ValueError('source exceeds research size limit; split it explicitly')
    return path


def _save(root: Path, identifier: str, data: dict) -> dict:
    directory = _within(root, '.lantai/research')
    directory.mkdir(parents=True, exist_ok=True)
    data['record_hash'] = _record_hash(data)
    target = directory / (identifier + '.json')
    temporary = directory / ('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # UUID identifiers are never supplied by a caller and never overwritten.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return data


def load_record(workspace: Path, identifier: str) -> dict:
    if not re.fullmatch(r'(packet|audit)-[0-9a-f]{32}', identifier):
        raise ValueError('invalid research record id')
    path = _within(Path(workspace), '.lantai/research/' + identifier + '.json')
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('research record size limit exceeded')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('record_hash') != _record_hash(data):
        raise ValueError('research record integrity mismatch')
    return data


def create_packet(workspace: Path, source_paths: list[str]) -> dict:
    root = Path(workspace).resolve()
    if not 1 <= len(source_paths) <= 20 or len(set(source_paths)) != len(source_paths):
        raise ValueError('select 1–20 distinct sources; selection is never truncated')
    sources = []
    total = 0
    for relative in source_paths:
        path = _source(root, relative)
        original = path.read_bytes()
        if path.suffix.lower() in {'.txt', '.md', '.markdown', '.csv', '.json', '.jsonl'}:
            text = original.decode('utf-8-sig')
            method, coverage = 'utf8_complete', 'complete'
        else:
            from ..app.wiki.core import extract_source_text_detail
            detail = extract_source_text_detail(path, require_complete_pdf=True, pdf_max_chars=MAX_TEXT_CHARS)
            text = str(detail.get('text') or '')
            method = str(detail.get('method') or '')
            coverage = 'complete' if detail.get('text_complete') else 'unknown'
        if not text.strip():
            raise ValueError('source has no extractable text: ' + relative)
        total += len(text)
        if len(text) > MAX_TEXT_CHARS or total > MAX_PACKET_CHARS:
            raise ValueError('extracted text exceeds research budget; selection is never truncated')
        if path.read_bytes() != original:
            raise ValueError('source changed during extraction; retry')
        sources.append({
            'source_id': 's' + str(len(sources) + 1), 'path': relative,
            'source_hash': _digest(original), 'text_hash': _digest(text.encode()),
            'text': text, 'extraction_method': method, 'coverage': coverage,
            'location_kind': 'extracted_text_character_offsets',
        })
    identifier = 'packet-' + uuid.uuid4().hex
    return _save(root, identifier, {
        'schema_version': 1, 'kind': 'local_evidence_packet', 'packet_id': identifier,
        'created_at': datetime.now(timezone.utc).isoformat(), 'sources': sources,
        'scope': 'selected_local_sources_only', 'network_used': False,
    })


def audit_packet(workspace: Path, packet_id: str, claims: list[dict],
                 open_issues: list[str] | None = None) -> dict:
    root = Path(workspace).resolve()
    packet = load_record(root, packet_id)
    if packet.get('kind') != 'local_evidence_packet':
        raise ValueError('audit requires an evidence packet')
    if len(claims) > 200:
        raise ValueError('at most 200 claims per audit')
    validated = [Claim.model_validate(c) for c in claims]
    if len({c.claim_id for c in validated}) != len(validated):
        raise ValueError('duplicate claim id')
    if open_issues is not None and not isinstance(open_issues, list):
        raise ValueError('open issues must be an array')
    pending = open_issues or []
    if len(pending) > 100 or any(not isinstance(i, str) or len(i) > 10_000 for i in pending):
        raise ValueError('invalid open issues')
    sources = {s['source_id']: s for s in packet['sources']}
    issues = []
    if not validated:
        issues.append('no_claims')
    if pending:
        issues.append('unresolved_questions')
    for s in sources.values():
        try:
            if _digest(_source(root, s['path']).read_bytes()) != s['source_hash']:
                issues.append('source_changed')
        except (ValueError, OSError):
            issues.append('source_unavailable')
        if s['coverage'] != 'complete':
            issues.append('extraction_coverage_unknown')
    results, rendered = [], []
    for c in validated:
        problems, links = [], []
        if not c.evidence:
            problems.append('uncited_claim')
        for e in c.evidence:
            s = sources.get(e.source_id)
            if s is None:
                problems.append('unknown_source')
                continue
            if not 0 <= e.start < e.end <= len(s['text']) or s['text'][e.start:e.end] != e.quote:
                problems.append('quote_mismatch')
            if e.relation == 'pending' or not e.reviewer.strip() or not e.rationale.strip():
                problems.append('review_pending')
            if e.relation == 'contradicts':
                problems.append('conflicting_evidence')
            if e.relation == 'insufficient':
                problems.append('insufficient_evidence')
            url = ('#/workspace?source=' + quote(s['path'], safe='') +
                   f'&rev={s["source_hash"]}')
            links.append(f'[{e.source_id}]({url}) (snapshot characters {e.start}:{e.end})\n' +
                         '\n'.join('> ' + line for line in e.quote.splitlines()))
        issues.extend(problems)
        results.append({**c.model_dump(), 'issues': sorted(set(problems))})
        rendered.append(f'- {c.text} ' + ' '.join(links))
    identifier = 'audit-' + uuid.uuid4().hex
    status = 'blocked' if issues else 'ready_for_review'
    return _save(root, identifier, {
        'schema_version': 1, 'kind': 'research_audit', 'audit_id': identifier,
        'packet_id': packet_id, 'packet_hash': packet['record_hash'],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'status': status, 'scope': 'submitted_claims_only',
        'semantic_validation': 'reviewer_supplied_not_independently_verified',
        'issues': sorted(set(issues)), 'open_issues': pending, 'claims': results,
        'markdown': f'# Research audit: {status}\n\nEvidence snapshot: {packet_id}\n\nOffsets refer to the stored snapshot, not the live preview.\n\n' + '\n'.join(rendered),
    })
