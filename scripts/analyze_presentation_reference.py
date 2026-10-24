#!/usr/bin/env python3
"""Analyze a PPTX, PDF or slide-image reference into portable design policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from lantai.presentation.models import TemplateMode  # noqa: E402
from lantai.presentation.reference_deck import ReferenceDeckAnalyzer  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure and arbitrate a presentation reference. The output is a "
            "portable StyleProfile and unified PresentationDesignContext, not a slide template."
        ),
    )
    parser.add_argument(
        "sources",
        nargs="+",
        type=Path,
        help="One PPTX/PDF file, or one or more ordered PNG/JPEG/WebP slide images.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile-id", default="custom-reference")
    parser.add_argument(
        "--template-mode",
        choices=[mode.value for mode in TemplateMode],
        default=TemplateMode.BRAND_PRESERVE_OPTIMIZE.value,
    )
    parser.add_argument(
        "--hard",
        action="append",
        default=[],
        help="User-declared immutable constraint; may be repeated.",
    )
    parser.add_argument("--request", default="")
    parser.add_argument("--variation-seed", type=int, default=0)
    return parser


def _markdown_report(payload: dict[str, object]) -> str:
    measurement = payload["measurement"]
    quality = payload["template_quality_profile"]
    style = payload["style_profile"]
    grammar = payload["extracted_grammar"]
    classification = payload["constraint_classification"]
    assert isinstance(measurement, dict)
    assert isinstance(quality, dict)
    assert isinstance(style, dict)
    assert isinstance(grammar, dict)
    assert isinstance(classification, dict)

    constraints = classification.get("constraints", [])
    rows = []
    if isinstance(constraints, list):
        for item in constraints:
            if not isinstance(item, dict):
                continue
            rows.append(
                "| {level} | {category} | {value} | {rationale} |".format(
                    level=item.get("level", ""),
                    category=str(item.get("category", "")).replace("|", "\\|"),
                    value=str(item.get("value", "")).replace("|", "\\|"),
                    rationale=str(item.get("rationale", "")).replace("|", "\\|"),
                ),
            )
    universal = grammar.get("universal_principles", [])
    style_traits = grammar.get("style_specific_traits", [])
    deck_decisions = grammar.get("deck_specific_decisions", [])
    strengths = quality.get("strengths", [])
    issues = quality.get("major_issues", [])

    def bullets(values: object, empty: str) -> str:
        if not isinstance(values, list) or not values:
            return f"- {empty}"
        return "\n".join(f"- {value}" for value in values)

    return f"""# Presentation reference analysis

This report records measurements and selective-inheritance policy. It does not create source-page templates or authorize copying source compositions.

## Source

- Name: `{measurement.get('source_name', '')}`
- Format: `{measurement.get('source_format', '')}`
- SHA-256: `{measurement.get('source_sha256', '')}`
- Canvas: {measurement.get('slide_width', '')} × {measurement.get('slide_height', '')} in
- Slides: {measurement.get('slide_count', '')}

## Template quality arbitration

- Overall: **{quality.get('overall_quality', '')}**
- Readability: {quality.get('readability', '')}
- Information hierarchy: {quality.get('information_hierarchy', '')}
- Grid consistency: {quality.get('grid_consistency', '')}
- Spacing quality: {quality.get('spacing_quality', '')}
- Color discipline: {quality.get('color_discipline', '')}
- Visual rhythm: {quality.get('visual_rhythm', '')}
- Composition variety: {quality.get('composition_variety', '')}

Strengths:

{bullets(strengths, 'No object-level strength was confidently inferred.')}

Major issues:

{bullets(issues, 'No major issue was detected at the current evidence level.')}

## Constraint classification

| level | category | value | rationale |
| --- | --- | --- | --- |
{chr(10).join(rows)}

## Portable style profile

- ID: `{style.get('id', '')}`
- Personality: {', '.join(style.get('visual_personality', [])) if isinstance(style.get('visual_personality'), list) else ''}
- Palette: {', '.join(style.get('palette', [])) if isinstance(style.get('palette'), list) else ''}
- Typography: `{json.dumps(style.get('typography', {}), ensure_ascii=False)}`
- Grid: `{json.dumps(style.get('grid', {}), ensure_ascii=False)}`
- Density: `{style.get('density', '')}`

## Universal design principles

{bullets(universal, 'No additional principle inferred.')}

## Style-specific traits

{bullets(style_traits, 'No style-specific trait inferred.')}

## Deck-specific observations — not reusable templates

{bullets(deck_decisions, 'No deck-specific observation retained.')}

## Boundary

- `Reference Learning` is optional. A no-reference job synthesizes a profile autonomously.
- StyleProfile is data and policy input; it does not introduce compiler `if/else` branches.
- Exact source coordinates remain measurement evidence and are excluded from reusable composition policy.
- Visual critique and repair must honor the same `PresentationDesignContext` constraint buckets and quality floor.
"""


def main() -> int:
    args = _parser().parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source: Path | list[Path] = args.sources[0] if len(args.sources) == 1 else args.sources
    result = ReferenceDeckAnalyzer().analyze(
        source,
        output_dir=output_dir,
        profile_id=args.profile_id,
        template_mode=TemplateMode(args.template_mode),
        explicit_hard_constraints=args.hard,
        generated_for_request=args.request,
        variation_seed=args.variation_seed,
    )
    payload = result.model_dump(mode="json")
    (output_dir / "reference-analysis.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "presentation-design-profile.json").write_text(
        json.dumps(payload["design_context"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "style-profile.json").write_text(
        json.dumps(payload["style_profile"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "reference-analysis.md").write_text(
        _markdown_report(payload),
        encoding="utf-8",
    )
    print(output_dir / "reference-analysis.md")
    print(output_dir / "presentation-design-profile.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
