"""On-demand ruled-table geometry from a hash-verified PDF page, without inference."""
from __future__ import annotations

from .pdf_preview import verified_pdf_page

# Bound response size for a single page; never silently truncate a table.
MAX_CELLS = 10000
MAX_TEXT = 200000


def page_tables(workspace, source, number):
    result = dict(source_version_id=source['id'], raw_hash=source['raw_hash'], page=number,
                  parser='pdfplumber_ruled_lines_v1', status='unavailable', tables=[],
                  page_text='', coordinate_system='pdfplumber_page_top_left_points',
                  verified_against_local_source=False)
    # Integrity errors must remain errors, rather than masquerading as parser failures.
    with verified_pdf_page(workspace, source, number) as (fixed, _):
        try:
            import pdfplumber
            with pdfplumber.open(fixed) as document:
                page = document.pages[number - 1]
                try:
                    result['page_width'], result['page_height'] = float(page.width), float(page.height)
                    page_text = page.extract_text() or ''
                    tables, cells, text_size = [], 0, len(page_text)
                    if text_size > MAX_TEXT:
                        result['status'] = 'size_limit'; return result
                    for table in page.find_tables({'vertical_strategy': 'lines', 'horizontal_strategy': 'lines'}):
                        cells += sum(len(row.cells) for row in table.rows)
                        if cells > MAX_CELLS:
                            result['status'] = 'size_limit'; return result
                        values = table.extract()
                        rows = []
                        for index, row in enumerate(table.rows):
                            extracted = []
                            for column, box in enumerate(row.cells):
                                if box is None:
                                    extracted.append(None)  # Missing/merged slots are not empty cells.
                                    continue
                                text = values[index][column] or ''
                                text_size += len(text)
                                if text_size > MAX_TEXT:
                                    result['status'] = 'size_limit'; return result
                                extracted.append(dict(text=text, bbox=list(box)))
                            rows.append(extracted)
                        tables.append(dict(bbox=list(table.bbox), rows=rows,
                                           header_status='unreviewed', footnote_status='unresolved'))
                    result.update(tables=tables, page_text=page_text,
                                  status='needs_visual_review' if tables else 'no_ruled_tables')
                finally:
                    page.close()
        except Exception:
            # Parsing is optional. Keep the original-page reader usable and the failure explicit.
            result.update(status='unavailable', tables=[], page_text='')
    return result
