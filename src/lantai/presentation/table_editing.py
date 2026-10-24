"""Text-only native table edits preserve the existing grid and cell styles."""

from copy import deepcopy

from .models import TableObject

_DML = "http://schemas.openxmlformats.org/drawingml/2006/main"


def replace_native_table_text(
    obj: TableObject, headers: list[str], rows: list[list[str]],
) -> TableObject:
    if (len(headers) != len(obj.headers) or len(rows) != len(obj.rows)
            or any(len(row) != len(obj.headers) for row in rows)):
        raise ValueError("table text edit must preserve the existing table grid")
    old_grid, new_grid = [obj.headers, *obj.rows], [headers, *rows]
    styles = []
    for ri, row in enumerate(obj.cell_styles):
        cells = []
        for ci, style in enumerate(row):
            text = new_grid[ri][ci]
            if text == old_grid[ri][ci] or not style.runs:
                cells.append(style)
            elif len(style.runs) == 1 and not style.runs[0].paragraph_break:
                cells.append(style.model_copy(update={
                    "runs": [style.runs[0].model_copy(update={"text": text})],
                }))
            else:
                raise ValueError("table text edit cannot preserve mixed native cell styles")
        styles.append(cells)
    return obj.model_copy(update={"headers": headers, "rows": rows, "cell_styles": styles})


def is_table_text_only_edit(before, after) -> bool:
    """Derive eligibility from the approved IR, never a model-supplied flag."""
    if not isinstance(before, TableObject) or not isinstance(after, TableObject):
        return False
    if before == after or before.frame is None:
        return False
    try:
        return replace_native_table_text(before, after.headers, after.rows) == after
    except ValueError:
        return False


def replace_native_table_carrier(carrier, before: TableObject, after: TableObject):
    """Change only native cell text after checking the exact imported grid.

    Recompiling a table can normalize default properties and drop properties
    not represented in IR. Keep the authoritative carrier instead, refusing
    stale or ambiguous cell text rather than guessing its run formatting.
    """
    if not is_table_text_only_edit(before, after):
        raise ValueError("native table edit is not text-only")
    result = deepcopy(carrier)
    tables = result.findall(f".//{{{_DML}}}tbl")
    if len(tables) != 1:
        raise ValueError("native table carrier is missing or ambiguous")
    native_rows = tables[0].findall(f"{{{_DML}}}tr")
    old_grid, new_grid = [before.headers, *before.rows], [after.headers, *after.rows]
    if len(native_rows) != len(old_grid):
        raise ValueError("native table edit has a stale grid")
    for native_row, old_row, new_row in zip(native_rows, old_grid, new_grid, strict=True):
        cells = native_row.findall(f"{{{_DML}}}tc")
        if len(cells) != len(old_row):
            raise ValueError("native table edit has a stale grid")
        for cell, old_text, new_text in zip(cells, old_row, new_row, strict=True):
            paragraphs = cell.findall(f"{{{_DML}}}txBody/{{{_DML}}}p")
            text = "\n".join("".join(
                "\v" if node.tag == f"{{{_DML}}}br" else
                (node.findtext(f"{{{_DML}}}t", default="")
                 if node.tag in {f"{{{_DML}}}r", f"{{{_DML}}}fld"} else "")
                for node in paragraph
            ) for paragraph in paragraphs)
            if text != old_text:
                raise ValueError("native table edit has stale cell text")
            if old_text == new_text:
                continue
            runs = paragraphs[0].findall(f"{{{_DML}}}r") if paragraphs else []
            if (len(paragraphs) != 1 or len(runs) != 1
                    or any(node.tag not in {f"{{{_DML}}}pPr", f"{{{_DML}}}r", f"{{{_DML}}}endParaRPr"}
                           for node in paragraphs[0])
                    or len(runs[0].findall(f"{{{_DML}}}t")) != 1):
                raise ValueError("native table edit cannot preserve ambiguous cell formatting")
            runs[0].find(f"{{{_DML}}}t").text = new_text
    return result
