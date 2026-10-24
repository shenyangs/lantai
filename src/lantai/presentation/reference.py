"""Clean-room presentation reference pack and release gates.

The pack stores research metadata and abstract design grammar only.  It never
embeds third-party slides, screenshots, templates, logos, fonts, or copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product


@dataclass(frozen=True)
class ReferenceSource:
    id: str
    name: str
    category: str
    url: str
    license: str
    usage: str = "pattern-study-only"
    distributable_asset: bool = False


REFERENCE_SOURCES: tuple[ReferenceSource, ...] = (
    ReferenceSource("microsoft-presentations", "Microsoft Presentations", "open-source", "https://github.com/microsoft/presentations", "MIT"),
    ReferenceSource("presenton", "Presenton", "open-source", "https://github.com/presenton/presenton", "Apache-2.0"),
    ReferenceSource("pptagent", "PPTAgent / DeepPresenter", "research-code", "https://github.com/icip-cas/pptagent", "repository-license"),
    ReferenceSource("if-powerpoint-generator", "Microsoft IF PowerPoint Generator", "open-source", "https://github.com/microsoft/IF-PowerPoint-Generator", "MIT"),
    ReferenceSource("pptxgenjs", "PptxGenJS", "open-source", "https://github.com/gitbrent/PptxGenJS", "MIT"),
    ReferenceSource("python-pptx", "python-pptx", "open-source", "https://github.com/scanny/python-pptx", "MIT"),
    ReferenceSource("pptx-skills", "pptx-skills", "open-source", "https://github.com/jiandong01/pptx-skills", "MIT"),
    ReferenceSource("ppt-master", "PPT Master", "open-source", "https://github.com/smallooo/ppt-master", "repository-license"),
    ReferenceSource("pptx-gen", "pptx-gen", "open-source", "https://github.com/alfonsograziano/pptx-gen", "repository-license"),
    ReferenceSource("copilot-powerpoint", "Microsoft Copilot for PowerPoint", "product", "https://support.microsoft.com/en-gb/copilot-powerpoint", "proprietary"),
    ReferenceSource("copilot-generator", "Microsoft AI PowerPoint Generator", "product", "https://www.microsoft.com/en-us/microsoft-365/powerpoint/ai-powerpoint-generator", "proprietary"),
    ReferenceSource("beautiful-ai", "Beautiful.ai", "product", "https://www.beautiful.ai/", "proprietary"),
    ReferenceSource("gamma", "Gamma", "product", "https://gamma.app/", "proprietary"),
    ReferenceSource("canva-presentations", "Canva Presentations", "product", "https://www.canva.com/presentations/", "proprietary"),
    ReferenceSource("kimi-slides", "Kimi Slides", "product", "https://www.kimi.com/zh-cn/features/slides", "proprietary"),
    ReferenceSource("presentations-ai", "Presentations.AI", "product", "https://www.presentations.ai/", "proprietary"),
    ReferenceSource("pitch", "Pitch", "product", "https://pitch.com/", "proprietary"),
    ReferenceSource("tome", "Tome", "product", "https://tome.app/", "proprietary"),
    ReferenceSource("slidesgo-ai", "Slidesgo AI Presentation Maker", "product", "https://slidesgo.com/ai-presentations", "proprietary"),
    ReferenceSource("adobe-express", "Adobe Express Presentations", "product", "https://www.adobe.com/express/create/presentation", "proprietary"),
    ReferenceSource("material-design", "Material Design", "design-system", "https://m3.material.io/", "Apache-2.0"),
    ReferenceSource("fluent-2", "Fluent 2", "design-system", "https://fluent2.microsoft.design/", "MIT"),
    ReferenceSource("carbon", "Carbon Design System", "design-system", "https://carbondesignsystem.com/", "Apache-2.0"),
    ReferenceSource("spectrum", "Adobe Spectrum", "design-system", "https://spectrum.adobe.com/", "Apache-2.0"),
    ReferenceSource("ibm-plex", "IBM Plex", "typography", "https://github.com/IBM/plex", "OFL-1.1"),
    ReferenceSource("noto-cjk", "Noto CJK", "typography", "https://github.com/notofonts/noto-cjk", "OFL-1.1"),
    ReferenceSource("datawrapper", "Datawrapper Academy", "data-visualization", "https://www.datawrapper.de/academy", "proprietary"),
    ReferenceSource("ft-vocabulary", "Financial Times Visual Vocabulary", "data-visualization", "https://github.com/Financial-Times/chart-doctor/tree/main/visual-vocabulary", "MIT"),
    ReferenceSource("dataviz-catalogue", "Data Visualisation Catalogue", "data-visualization", "https://datavizcatalogue.com/", "proprietary"),
    ReferenceSource("unsplash", "Unsplash", "image-guidance", "https://unsplash.com/license", "Unsplash-License"),
    ReferenceSource("pexels", "Pexels", "image-guidance", "https://www.pexels.com/license/", "Pexels-License"),
    ReferenceSource("openmoji", "OpenMoji", "icon-guidance", "https://github.com/hfg-gmuend/openmoji", "CC-BY-SA-4.0"),
)


SEMANTIC_ROLES = (
    "cover", "agenda", "section", "key_judgment", "two_column", "three_cards",
    "comparison", "process", "timeline", "data_conclusion", "matrix", "hierarchy",
    "table", "chart", "image_led", "closing",
)


@dataclass(frozen=True)
class PatternCell:
    id: str
    semantic_role: str
    density: str
    media_ratio: str
    grid: str
    title_level: str
    whitespace: str
    note: str


def build_pattern_cells() -> tuple[PatternCell, ...]:
    """Return 128 clean-room pattern annotations (16 roles × 2 × 4)."""
    cells: list[PatternCell] = []
    for index, (role, density, media_ratio) in enumerate(
        product(SEMANTIC_ROLES, ("low", "medium"), ("none", "quarter", "half", "dominant")),
        start=1,
    ):
        cells.append(
            PatternCell(
                id=f"pattern-{index:03d}",
                semantic_role=role,
                density=density,
                media_ratio=media_ratio,
                grid="12-column / 8pt baseline",
                title_level="40/28/18" if density == "low" else "32/22/15",
                whitespace="generous" if density == "low" else "balanced",
                note="Abstract grammar only; no third-party slide geometry or copy retained.",
            )
        )
    return tuple(cells)


PATTERN_CELLS = build_pattern_cells()


@dataclass(frozen=True)
class GoldenTask:
    id: str
    family: str
    input_kind: str
    difficulty: str
    expected_slides: int
    must_check: tuple[str, ...]


def build_golden_tasks() -> tuple[GoldenTask, ...]:
    families = (
        ("business-report", "prompt", 8),
        ("product-launch", "docx", 10),
        ("research-summary", "pdf", 12),
        ("data-review", "xlsx", 10),
        ("image-story", "images", 8),
        ("custom-template", "pptx-template", 10),
    )
    tasks: list[GoldenTask] = []
    for family_index, (family, input_kind, slide_count) in enumerate(families):
        for variant in range(10):
            difficulty = ("standard", "stress", "edge")[variant % 3]
            tasks.append(
                GoldenTask(
                    id=f"golden-{family_index * 10 + variant + 1:02d}",
                    family=family,
                    input_kind=input_kind,
                    difficulty=difficulty,
                    expected_slides=slide_count + (variant % 3) - 1,
                    must_check=(
                        "native_editable", "no_hard_overflow", "immutable_data",
                        "reopen", "render", "conversation_isolation",
                    ),
                )
            )
    return tuple(tasks)


GOLDEN_TASKS = build_golden_tasks()


SCORING_RUBRIC = {
    "content_completeness": 25,
    "narrative_clarity": 15,
    "visual_hierarchy": 15,
    "layout_integrity": 20,
    "native_editability": 15,
    "source_and_data_fidelity": 10,
}


def validate_reference_pack() -> None:
    if not 30 <= len(REFERENCE_SOURCES) <= 50:
        raise ValueError("reference pack must contain 30–50 sources")
    if not 120 <= len(PATTERN_CELLS) <= 180:
        raise ValueError("reference pack must contain 120–180 pattern cells")
    if len(GOLDEN_TASKS) != 60:
        raise ValueError("golden suite must contain exactly 60 tasks")
    if any(source.distributable_asset for source in REFERENCE_SOURCES):
        raise ValueError("v1 reference sources must never ship third-party assets")
    if sum(SCORING_RUBRIC.values()) != 100:
        raise ValueError("scoring rubric must total 100")

