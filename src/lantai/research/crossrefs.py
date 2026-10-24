"""Syntactic report target inventory and validation of model reference judgments.

No numbering keywords or local heuristics decide whether a reference is correct.
Raster pixels are deliberately outside this text-only review's scope.
"""
from __future__ import annotations

import re

from .anchors import resolve_span
from .figures import figure_lines

SCOPE = 'table_text_and_figure_captions'


def reference_targets(text):
    # Use the export renderer's cell grammar so the model sees the same tables.
    from .exports import _table_cells
    targets = [{'id': f'figure-{index}', 'kind': 'figure_caption', 'start': a, 'end': b,
                'text': caption, 'context': '', 'figure_id': identifier}
               for index, (a, b, caption, identifier) in enumerate(figure_lines(text), 1)]
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    index, fence, count = 0, None, 0
    while index < len(lines):
        line = lines[index]
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            index += 1
            continue
        cells = _table_cells(line) if fence is None else None
        separators = _table_cells(lines[index + 1]) if cells and index + 1 < len(lines) else None
        if not (separators and len(cells) == len(separators)
                and all(re.fullmatch(r':?-{3,}:?', c) for c in separators)):
            index += 1
            continue
        start = offsets[index]
        index += 2
        while index < len(lines):
            row = _table_cells(lines[index])
            if not row or len(row) != len(cells):
                break
            index += 1
        end = offsets[index] if index < len(lines) else len(text)
        count += 1
        targets.append({'id': f'table-{count}', 'kind': 'table', 'start': start, 'end': end,
                        'text': text[start:end], 'context': text[max(0, start - 500):start]})
    return sorted(targets, key=lambda target: target['start'])


def checked_references(result, report_text, start, end, targets):
    refs = result.get('references')
    if not isinstance(refs, list) or len(refs) > 500:
        raise ValueError('Model review requires structured table/figure references')
    ids = {target['id'] for target in targets}
    checked = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError('Invalid table/figure reference judgment')
        a, b = resolve_span(report_text[start:end], ref, base=start)
        target = ref.get('target_id')
        assessment = ref.get('assessment')
        if (assessment not in {'consistent', 'mismatch', 'missing', 'uncertain'}
                or not isinstance(ref.get('reason'), str) or not ref['reason'].strip()
                or len(ref['reason']) > 5000
                or (target is not None and (not isinstance(target, str) or target not in ids))
                or (assessment in {'consistent', 'mismatch'} and target is None)
                or (assessment == 'missing' and target is not None)):
            raise ValueError('Invalid table/figure reference assessment or target')
        checked.append({'start': a, 'end': b, 'text': report_text[a:b], 'target_id': target,
                        'assessment': assessment, 'reason': ref['reason']})
    return checked


PROMPT = (
    ' Also identify ALL textual references to report tables and figures in report_text, including '
    'implicit references such as "the table above"; ignore literal code examples. '
    'A caption, table title or figure alt text declares a target; it is not itself an outgoing '
    'reference to that same target. Review an actual cross-reference inside a caption only when '
    'it points to another target. Keep target declarations separate from referring passages. '
    'reference_targets is a global syntactic inventory, including targets outside this report chunk. '
    'The inventory IDs are internal handles, NOT printed table/figure numbers. Determine intended '
    'targets semantically from nearby context, caption, numbering and table contents. Return an '
    'additional JSON field "references": [{"text":"exact unique reference passage in report_text",'
    '"target_id":"an actual inventory id or null","assessment":"consistent|mismatch|missing|uncertain",'
    '"reason":"brief reason"}]. Use [] only if this chunk contains no references. '
    'consistent means the intended target exists and the reference agrees with its visible table '
    'text or caption; mismatch means the referenced table values, numbering or topic disagree; '
    'missing means no intended target exists (target_id must be null); uncertain means the target '
    'or agreement is ambiguous. Target_id is required for consistent/mismatch. Inspect table numbers '
    'and values, not just labels. A figure caption supports locating that figure, not confirming '
    'data in its pixels: any assertion requiring unseen raster content is uncertain. Do not claim '
    'visual verification. This check does not turn a report table or caption into independent '
    'empirical evidence: continue the source-based claims review separately. For critical_recheck, '
    'check references in the target passages again; do not assume the earlier check was correct.'
)
