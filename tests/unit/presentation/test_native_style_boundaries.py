"""Native styles cannot introduce unsafe XML or weaken atomic publication."""

import pytest
from pydantic import ValidationError

from lantai.presentation.models import (
    ChartAxisStyle, ChartPlotLayout, DeckIR, DeckSlide, ObjectFrame,
    TableCellStyle, TableObject, TextObject,
)
from lantai.presentation.pptx_compiler import NativePptxCompiler


@pytest.mark.parametrize("attribute", ['cap="bad"', 'cmpd="bad"', 'algn="bad"'])
def test_native_border_rejects_invalid_line_enums(attribute):
    with pytest.raises(ValueError):
        TableCellStyle(borders={"lnL": f'<a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" {attribute}/>'})


@pytest.mark.parametrize("content", [
    '<a:prstDash val="bad"/>', '<a:headEnd type="bad"/>',
    '<a:headEnd w="bad"/>', '<a:tailEnd len="bad"/>', '<a:miter lim="bad"/>',
    '<a:round/><a:bevel/>', 'unexpected text',
    '<a:solidFill><a:srgbClr val="FFFFFF">unexpected text</a:srgbClr></a:solidFill>',
])
def test_native_border_rejects_invalid_child_styles(content):
    with pytest.raises(ValueError):
        TableCellStyle(borders={"lnL": f'<a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">{content}</a:lnL>'})


def test_native_border_accepts_supported_explicit_line_styles():
    style = TableCellStyle(borders={"lnL": (
        '<a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'w="9525" cap="flat" cmpd="sng" algn="ctr">'
        '<a:solidFill><a:srgbClr val="ABCDEF"/></a:solidFill><a:prstDash val="dashDot"/>'
        '<a:miter lim="800000"/><a:headEnd type="none" w="med" len="lg"/>'
        '<a:tailEnd type="triangle" w="sm" len="sm"/></a:lnL>'
    )})
    assert style.borders["lnL"].endswith('</a:lnL>')


@pytest.mark.parametrize("xml", [
    '<!DOCTYPE x [<!ENTITY x "blocked">]><a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>',
    '<a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" href="https://example.invalid/"/>',
    '<a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></a:lnL>',
    '<a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" w="-1"/>',
    '<a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:solidFill><a:srgbClr val="INVALID"/></a:solidFill></a:lnL>',
])
def test_native_border_rejects_unsafe_or_unresolved_styles(xml):
    with pytest.raises(ValueError):
        TableCellStyle(borders={"lnL": xml})


@pytest.mark.parametrize("axis", [{"minimum": float("nan")}, {"maximum": float("inf")},
                                 {"major_unit": 0}, {"minor_unit": -1}, {"minimum": 2, "maximum": 1}])
def test_native_axis_rejects_invalid_scales(axis):
    with pytest.raises(ValidationError):
        ChartAxisStyle(**axis)


@pytest.mark.parametrize("layout", [{"x": float("nan")}, {"h": 0}, {"w": 2}, {"xMode": "guess"}])
def test_native_plot_rejects_unbounded_layouts(layout):
    with pytest.raises(ValidationError):
        ChartPlotLayout(**layout)


def test_native_cell_styles_require_the_complete_grid():
    with pytest.raises(ValidationError):
        TableObject(headers=["one", "two"], rows=[["a", "b"]], cell_styles=[[TableCellStyle()]])


@pytest.mark.parametrize("repair", ["restore_chart_axes", "restore_table_borders"])
def test_native_style_repair_failure_keeps_previous_candidate(tmp_path, monkeypatch, repair):
    from lantai.presentation import pptx_compiler
    output = tmp_path / "candidate.pptx"
    deck = DeckIR(id="atomic-style-deck", title="合成验证", slides=[DeckSlide(
        id="atomic-style-page", layout_id="freeform", objects=[TextObject(
            id="body", role="body", text="合成验证", font_size=20, frame=ObjectFrame(x=1, y=1, w=4, h=1),
        )],
    )])
    compiler = NativePptxCompiler()
    compiler.compile(deck, output)
    previous = output.read_bytes()

    def fail(*_args):
        raise RuntimeError("synthetic style binding failure")

    monkeypatch.setattr(pptx_compiler, repair, fail)
    with pytest.raises(RuntimeError, match="synthetic style binding failure"):
        compiler.compile(deck, output)
    assert output.read_bytes() == previous
    assert list(tmp_path.glob(".*.pptx")) == []
