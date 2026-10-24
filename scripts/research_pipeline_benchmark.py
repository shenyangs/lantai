#!/usr/bin/env python3
"""Reproducible local 20/100-source processing baseline; no inference or network.

Generated distributable fixtures mix Chinese/English text and four-page PDFs.
One blank PDF page per twenty sources represents an explicit extraction gap.
Measurements describe processing only, not research quality or user acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def dataset(root: Path, count: int):
    from reportlab.pdfgen import canvas
    paths, pdf_pages, blank_pages = [], 0, 0
    folder = root / 'wiki/raw/sources'
    folder.mkdir(parents=True)
    for i in range(count):
        if i % 2:
            path = folder / f'fixture-{i:03d}.txt'
            path.write_text((f'资料 {i}：此固定材料用于处理性能测量。Original source {i}; '
                'observational association alone cannot establish causality.\n\n') * 60, encoding='utf-8')
        else:
            path = folder / f'fixture-{i:03d}.pdf'
            document = canvas.Canvas(str(path))
            for page in range(4):
                pdf_pages += 1
                if i % 20 == 0 and page == 2:
                    blank_pages += 1
                else:
                    for line in range(36):
                        document.drawString(40, 780 - line * 18,
                            f'Source {i}; page {page + 1}; line {line + 1}. Correlation does not establish causation.')
                document.showPage()
            document.save()
        paths.append(path.relative_to(root).as_posix())
    return paths, pdf_pages, blank_pages


def measure(count: int):
    from lantai.research.projects import ResearchService
    from lantai.research.exports import export_report, export_file
    with tempfile.TemporaryDirectory(prefix=f'lantai-research-baseline-{count}-') as temporary:
        root = Path(temporary)
        paths, pdf_pages, blank_pages = dataset(root, count)
        service = ResearchService(root)
        start = time.perf_counter()
        p = service.create_project('Processing baseline with explicit extraction gaps', paths, idempotency_key='baseline')
        imported = time.perf_counter()
        sources = [service.source_manifest(s) for s in p['source_versions']]
        for source in sources:
            for first in range(0, len(source['blocks']), 4):
                service.read_source(p['project_id'], 1, source['id'], first, 4)
        read = time.perf_counter()
        report = service.save_report(p['project_id'], 1,
            '# 未核验性能测试稿\n\n' + ('此文本仅验证处理与导出性能，不代表研究结论。\n\n' * 300), [],
            idempotency_key='report')
        exports = []
        for format in ('markdown', 'docx', 'pdf'):
            before = time.perf_counter()
            record = export_report(service, p['project_id'], 1, report['id'], format, 'gb7714', format)
            path = export_file(service, record['id'])
            exports.append({'format': format, 'seconds': round(time.perf_counter() - before, 4),
                'bytes': path.stat().st_size, 'integrity': hashlib.sha256(path.read_bytes()).hexdigest() == record['artifact_hash'],
                'review_status': record['status']})
        state = service.project(p['project_id'])
        return {'source_count': count, 'pdf_count': count // 2, 'pdf_pages': pdf_pages,
            'blank_pdf_pages': blank_pages, 'blank_page_ratio': blank_pages / pdf_pages,
            'indexed_characters': sum(s.get('text_length', len(s.get('text', ''))) for s in sources), 'reading': state['reading'],
            'source_import_seconds': round(imported - start, 4), 'all_blocks_read_seconds': round(read - imported, 4),
            'exports': exports, 'network_calls': 0, 'model_calls': 0, 'model_cost': None,
            'passed': state['reading']['read_blocks'] == state['reading']['total_blocks']
                and len(state['reading']['incomplete_sources']) == count // 20
                and all(x['integrity'] and x['review_status'] == 'not_reviewed' for x in exports)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = {'schema_version': 1, 'fixture_version': 'mixed-text-pdf-v1',
        'host': {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine(),
                 'python': platform.python_version()}, 'scope': 'local_processing_only',
        'runs': [measure(n) for n in (20, 100)], 'quality_evaluation': False,
        'user_acceptance': False, 'budget_frozen': False}
    result['passed'] = all(r['passed'] for r in result['runs'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
