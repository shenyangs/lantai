#!/usr/bin/env python3
"""Render synthetic long research exports; requires local LibreOffice and PDFium.

Run with PYTHONPATH=src and --output pointing to a new directory outside the repo.
No provider, user documents, application data or shared office profile is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def run(output: Path, office: str) -> dict:
    from pypdf import PdfReader
    import pypdfium2 as pdfium
    from lantai.research.exports import export_file, export_report
    from lantai.research.projects import ResearchService

    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='lantai-research-layout-') as temporary:
        root = Path(temporary)
        source_path = root / 'wiki/raw/sources/版式测试资料.txt'
        source_path.parent.mkdir(parents=True)
        quote = '样本量为 80。研究仅发现相关性，未证明因果关系。'
        source_path.write_text(quote, encoding='utf-8')
        service = ResearchService(root)
        project = service.create_project('长报告分页与跨页表格验收', [source_path.relative_to(root).as_posix()],
                                         policy='offline', idempotency_key='project')
        body = '## 研究结论\n\n观察到相关性，不能推断因果关系。\n\n'
        body += '\n\n'.join(f'### 第 {i + 1} 项讨论\n\n' +
                          '这是一项合成版式检查。正文说明方法、证据和适用边界，不能当作真实研究结论。' * 5
                          for i in range(20))
        body += '\n\n## 跨页比较表\n\n| 编号 | 观察结果 | 解释边界 |\n| --- | --- | --- |\n'
        body += '\n'.join(f'| {i + 1} | 第 {i + 1} 项观察，保留原始描述 | 未排除混杂，不能据此推断因果。 |' for i in range(100))
        body += '\n\n## 表后讨论\n\n最终段落不可丢失。\n'
        claim = '观察到相关性，不能推断因果关系。'
        start = body.index(claim)
        report = service.save_report(project['project_id'], 1, body, [{
            'start': start, 'end': start + len(claim), 'evidence': [{
                'source_version_id': project['source_versions'][0], 'start': 0, 'end': len(quote), 'quote': quote,
            }],
        }], idempotency_key='report')
        exports = {}
        for format in ('docx', 'pdf'):
            receipt = export_report(service, project['project_id'], 1, report['id'], format, 'gb7714', format)
            target = output / ('research.' + format)
            shutil.copyfile(export_file(service, receipt['id']), target)
            exports[format] = {'sha256': receipt['artifact_hash'], 'status': receipt['status']}
        converted = output / 'word-rendered'
        converted.mkdir()
        process = subprocess.run([office, '-env:UserInstallation=' + (root / 'office-profile').as_uri(),
                                  '--headless', '--convert-to', 'pdf', '--outdir', str(converted),
                                  str(output / 'research.docx')], capture_output=True, text=True, timeout=120)
        if process.returncode or not (converted / 'research.pdf').is_file():
            raise RuntimeError('Isolated Word rendering failed: ' + process.stderr + process.stdout)
        for label, path in (('pdf', output / 'research.pdf'), ('word', converted / 'research.pdf')):
            reader = PdfReader(path)
            texts = [page.extract_text() for page in reader.pages]
            all_text = '\n'.join(texts)
            assert len(texts) >= 5 and '最终段落不可丢失' in all_text
            assert '第 100 项观察' in all_text and '待核验' in all_text and quote in all_text
            table_pages = [index for index, text in enumerate(texts) if '未排除混杂' in text]
            assert len(table_pages) >= 3 and all('观察结果' in texts[index] for index in table_pages)
            if label == 'word':
                assert '引用为静态文本' in all_text
            # Render representative first, middle/table transition and final pages.
            selected = sorted({0, len(texts) - 1, table_pages[0], table_pages[1], table_pages[-1]})
            pdf = pdfium.PdfDocument(path)
            images = []
            try:
                for index in selected:
                    page = pdf[index]
                    bitmap = page.render(scale=1.25)
                    name = f'{label}-page-{index + 1}.png'
                    bitmap.to_pil().save(output / name)
                    bitmap.close(); page.close()
                    images.append(name)
            finally:
                pdf.close()
            exports[label + '_render'] = {'pages': len(texts), 'table_pages': [i + 1 for i in table_pages],
                                        'rendered_pages': images, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    result = {'status': 'ok', 'fixture': 'synthetic-long-report-100-row-table', 'model_calls': 0,
              'exports': exports, 'visual_inspection': 'pending'}
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--office', default=shutil.which('soffice'))
    arguments = parser.parse_args()
    if not arguments.office:
        parser.error('LibreOffice is required to verify the rendered Word layout')
    print(json.dumps(run(arguments.output.resolve(), arguments.office), ensure_ascii=False))
