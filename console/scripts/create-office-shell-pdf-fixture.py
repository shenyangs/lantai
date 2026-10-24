from pathlib import Path
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def build_fixture(output_path: Path, case_id: str = "case-01") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "FixtureTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=24,
        leading=30,
        textColor=colors.HexColor("#1F2A37"),
        alignment=TA_LEFT,
        spaceAfter=10 * mm,
    )
    eyebrow = ParagraphStyle(
        "FixtureEyebrow",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#536F90"),
        tracking=1.2,
        spaceAfter=4 * mm,
    )
    body = ParagraphStyle(
        "FixtureBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=11,
        leading=17,
        textColor=colors.HexColor("#435064"),
        spaceAfter=5 * mm,
    )
    heading = ParagraphStyle(
        "FixtureHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=20,
        textColor=colors.HexColor("#1F2A37"),
        spaceBefore=5 * mm,
        spaceAfter=4 * mm,
    )

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#DCE3EB"))
        canvas.line(20 * mm, 15 * mm, 190 * mm, 15 * mm)
        canvas.setFillColor(colors.HexColor("#7A8796"))
        canvas.setFont("Helvetica", 8.5)
        canvas.drawString(20 * mm, 10 * mm, "Lantai local PDF editor - visual QA fixture")
        canvas.drawRightString(190 * mm, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=22 * mm,
        title="Lantai Office Shell Visual QA",
        author="Lantai",
    )

    story = [
        Paragraph("LOCAL OFFICE / VISUAL ACCEPTANCE", eyebrow),
        Paragraph(f"A healthy PDF editing surface / {case_id}", title),
        Paragraph(
            "This document exists to verify the complete local PDF experience: page rendering, "
            "thumbnail navigation, ribbon commands, zoom and status feedback, and the Lantai "
            "co-creation panel. An empty-state screenshot is not sufficient evidence.",
            body,
        ),
        Paragraph("Acceptance matrix", heading),
    ]
    table_data = [
        ["Surface", "Expected evidence", "Result"],
        ["Document canvas", "Sharp text, borders, and page shadow", "Visible"],
        ["Ribbon", "File and PDF editing commands", "Visible"],
        ["Navigation", "Page thumbnails and current page", "Visible"],
        ["Co-creation", "Single Lantai entry and right panel", "Visible"],
        ["Status", "Page count and zoom feedback", "Visible"],
    ]
    table = Table(table_data, colWidths=[42 * mm, 92 * mm, 30 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#536F90")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("LEADING", (0, 0), (-1, -1), 13),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#334155")),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F7F9FC")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FB")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE3EB")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 8 * mm),
            Paragraph("What this fixture catches", heading),
            Paragraph(
                "A valid capture must show actual PDF page pixels inside the editor, not merely "
                "the application's loading or no-file state. The second page also confirms that "
                "navigation and page count are connected to a real document.",
                body,
            ),
            PageBreak(),
            Paragraph("PAGE TWO / NAVIGATION CHECK", eyebrow),
            Paragraph("A second rendered page", title),
            Paragraph(
                "The local editor should report two pages, display a second thumbnail, and keep "
                "the same product shell while navigating. The content is intentionally simple so "
                "clipping, font substitution, and scaling defects remain obvious.",
                body,
            ),
            Paragraph("Visual invariants", heading),
            Paragraph(
                "1. The page remains white against a neutral canvas.<br/>"
                "2. Text stays crisp at the default zoom.<br/>"
                "3. The Lantai entry remains in the title row.<br/>"
                "4. The right collaboration panel does not cover the document.<br/>"
                "5. Status and zoom controls remain reachable.",
                body,
            ),
        ]
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "output/pdf/office-shell-visual-fixture.pdf")
    case_id = sys.argv[2] if len(sys.argv) > 2 else "case-01"
    build_fixture(target.resolve(), case_id)
    print(target.resolve())
