#!/usr/bin/env python3
"""Exercise real research exports inside the frozen backend without a model."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
from zipfile import ZipFile


def run_probe() -> dict:
    from docx import Document
    from pypdf import PdfReader
    from lantai.research.exports import export_file, export_report
    from lantai.research.projects import ResearchService

    with tempfile.TemporaryDirectory(prefix='lantai-research-export-probe-') as temporary:
        workspace = Path(temporary)
        path = workspace / 'wiki/raw/sources/观察研究.txt'
        path.parent.mkdir(parents=True)
        quote = '样本量为 80。仅发现相关性，未证明因果关系。'
        path.write_text(quote, encoding='utf-8')
        service = ResearchService(workspace)
        project = service.create_project('包态科研导出验收', [path.relative_to(workspace).as_posix()],
                                         policy='offline', idempotency_key='project')
        source = service.source(project['source_versions'][0])
        claim = '观察研究发现相关性，不能据此推断因果关系。'
        report = service.save_report(project['project_id'], 1, claim, [{
            'start': 0, 'end': len(claim), 'evidence': [{
                'source_version_id': source['id'], 'start': 0, 'end': len(quote), 'quote': quote,
            }],
        }], idempotency_key='report')
        checks = {}
        for format in ('markdown', 'docx', 'pdf'):
            receipt = export_report(service, project['project_id'], 1, report['id'],
                                    format, 'gb7714', format)
            data = export_file(service, receipt['id']).read_bytes()
            assert hashlib.sha256(data).hexdigest() == receipt['artifact_hash']
            assert receipt['status'] == 'not_reviewed'
            assert receipt['citation_provenance']['engine'] == 'citeproc-js'
            embedded_font = None
            if format == 'markdown':
                text = data.decode('utf-8')
            elif format == 'docx':
                document = Document(BytesIO(data))
                text = '\n'.join(p.text for p in document.paragraphs)
                with ZipFile(BytesIO(data)) as archive:
                    embedded_font = any(name.endswith('.odttf') for name in archive.namelist())
                    assert 'word/footer1.xml' in archive.namelist()
                    assert b'PAGE' in archive.read('word/footer1.xml')
            else:
                document = PdfReader(BytesIO(data))
                text = '\n'.join(page.extract_text() for page in document.pages)
                descriptors = [font.get_object().get('/FontDescriptor')
                               for page in document.pages for font in page['/Resources']['/Font'].values()]
                embedded_font = any(value and '/FontFile2' in value.get_object() for value in descriptors)
            assert claim in text and quote in text and '待核验' in text
            assert format == 'markdown' or embedded_font
            checks[format] = {'bytes': len(data), 'hash_verified': True,
                              'draft_label': True, 'embedded_font': embedded_font}
        return {'status': 'ok', 'model_calls': 0, 'exports': checks}


if __name__ == '__main__':
    print(json.dumps(run_probe(), ensure_ascii=False, sort_keys=True))
