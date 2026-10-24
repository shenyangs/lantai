# -*- coding: utf-8 -*-
"""Universal, profile-driven design reasoning for Presentation Skill v3.

This module owns decisions.  Style profiles are data, references are optional
profile providers, and the compiler never interprets a profile identity.  The
output is explicit slide geometry that can be executed by any native writer.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from .models import (
    ChartObject,
    ColorProfile,
    CompositionAuthority,
    ConstraintLevel,
    DeckIR,
    DeckObject,
    DeckPlan,
    DeckPlanSlide,
    DeckSlide,
    DeckRhythmRole,
    DeckVisualBeat,
    DeckVisualPlan,
    DesignConstraint,
    DesignQualityFloor,
    ImageObject,
    MetricObject,
    ObjectFrame,
    NativeCompositionTokens,
    PresentationBrief,
    PresentationDesignContext,
    PresentationDesignProfile,
    PresentationJobRequest,
    QualityBand,
    ReferenceDeckStyleProfile,
    RelationshipType,
    RichTextRun,
    ObjectRelationship,
    SlideIntent,
    SlideCompositionPlan,
    ShapeObject,
    TableObject,
    TemplateMode,
    TemplateQualityProfile,
    TextObject,
    VisualDirection,
)


_PROFILE_DIR = Path(__file__).with_name("style_profiles")
_BUILTIN_PROFILE_IDS = (
    "editorial-executive",
    "minimal-keynote",
    "product-launch",
    "data-analytical",
)


# Curated color directions are candidate decisions, not style themes.  Their
# tags let context and variation choose among several valid expressions of the
# same design personality without adding profile-id branches to the compiler.
_COLOR_DIRECTIONS = (
    {
        "id": "graphite-burgundy",
        "tags": {"editorial", "executive", "restrained", "warm", "dense"},
        "background": "F4F1EA",
        "surface": "FCFAF6",
        "primary_text": "171615",
        "secondary_text": "5E5A55",
        "tertiary_text": "8B857D",
        "accent_primary": "8B3341",
        "accent_secondary": "B58B52",
        "divider": "D7D0C6",
        "border": "C9C0B5",
        "chart_secondary": "43574F",
        "chart_series": ["8B3341", "43574F", "B58B52", "77736E", "B8AEA1"],
    },
    {
        "id": "ivory-forest",
        "tags": {"editorial", "executive", "credible", "quiet", "analytical"},
        "background": "F3F1E9",
        "surface": "FCFBF7",
        "primary_text": "18211D",
        "secondary_text": "52605A",
        "tertiary_text": "849089",
        "accent_primary": "245B45",
        "accent_secondary": "A87B46",
        "divider": "D2D7D1",
        "border": "C1C8C1",
        "chart_secondary": "586C86",
        "chart_series": ["245B45", "586C86", "A87B46", "7A867F", "B5BDB7"],
    },
    {
        "id": "paper-violet",
        "tags": {"editorial", "executive", "precise", "modern", "high-contrast"},
        "background": "F6F5F2",
        "surface": "FFFFFF",
        "primary_text": "191820",
        "secondary_text": "5E5C67",
        "tertiary_text": "8B8993",
        "accent_primary": "5C4393",
        "accent_secondary": "B28A47",
        "divider": "DAD7DF",
        "border": "CBC7D2",
        "chart_secondary": "397082",
        "chart_series": ["5C4393", "397082", "B28A47", "777482", "B7B1C1"],
    },
    {
        "id": "deep-ink-cyan",
        "tags": {"launch", "technical", "energetic", "sparse", "high-contrast"},
        "background": "101820",
        "surface": "18242D",
        "primary_text": "F4F5F2",
        "secondary_text": "B7C0C6",
        "tertiary_text": "84919A",
        "accent_primary": "48C6B2",
        "accent_secondary": "E4B55F",
        "divider": "34434D",
        "border": "465661",
        "chart_secondary": "E4B55F",
        "chart_series": ["48C6B2", "E4B55F", "7D9AC8", "B7C0C6", "65737C"],
    },
    {
        "id": "stone-cobalt",
        "tags": {"analytical", "quantitative", "clear", "credible", "dense"},
        "background": "F3F6F7",
        "surface": "FFFFFF",
        "primary_text": "17242E",
        "secondary_text": "5B6A74",
        "tertiary_text": "88959D",
        "accent_primary": "315F9A",
        "accent_secondary": "C16F3B",
        "divider": "D4DDE1",
        "border": "C3CFD5",
        "chart_secondary": "C16F3B",
        "chart_series": ["315F9A", "C16F3B", "3E8172", "7C8790", "B4BEC3"],
    },
    {
        "id": "warm-sand-indigo",
        "tags": {"academic", "human", "editorial", "calm", "narrative"},
        "background": "F2EDE3",
        "surface": "FBF8F2",
        "primary_text": "201E1B",
        "secondary_text": "625D55",
        "tertiary_text": "918A80",
        "accent_primary": "3F4C78",
        "accent_secondary": "A65F46",
        "divider": "D7CEC0",
        "border": "C9BEAE",
        "chart_secondary": "A65F46",
        "chart_series": ["3F4C78", "A65F46", "7A8453", "7F786F", "B9AD9D"],
    },
)

_COLOR_TONE_KEYWORDS = {
    "executive": ("管理层", "董事会", "经营", "决策", "executive", "board"),
    "editorial": ("报告", "研究", "汇报", "复盘", "editorial", "report"),
    "analytical": ("数据", "指标", "分析", "趋势", "财务", "kpi", "analysis"),
    "quantitative": ("图表", "测算", "同比", "环比", "占比", "metric"),
    "launch": ("发布", "新品", "上市", "路演", "launch", "release"),
    "technical": ("技术", "平台", "系统", "架构", "technology"),
    "academic": ("学术", "教育", "研究", "论文", "academic"),
    "narrative": ("故事", "文化", "人物", "叙事", "narrative"),
    "dense": ("详细", "深度", "完整", "长篇", "dense"),
    "sparse": ("演讲", "极简", "主旨", "keynote", "minimal"),
    "credible": ("正式", "可靠", "证据", "治理", "credible"),
    "warm": ("温暖", "人文", "关系", "共情", "warm"),
    "modern": ("现代", "创新", "未来", "modern"),
}

_NAMED_ACCENTS = {
    "紫色": "62469B",
    "紫": "62469B",
    "酒红": "8D3442",
    "红色": "A13D45",
    "红": "A13D45",
    "深绿": "245B45",
    "墨绿": "245B45",
    "绿色": "39745B",
    "蓝色": "315F9A",
    "蓝": "315F9A",
    "橙色": "C16F3B",
    "橙": "C16F3B",
    "violet": "62469B",
    "purple": "62469B",
    "burgundy": "8D3442",
    "green": "39745B",
    "blue": "315F9A",
    "orange": "C16F3B",
}


@lru_cache(maxsize=None)
def load_style_profile(profile_id: str) -> ReferenceDeckStyleProfile:
    """Load a data-only profile without adding style branches to execution."""

    if profile_id not in _BUILTIN_PROFILE_IDS:
        raise KeyError(f"unknown built-in presentation style profile: {profile_id}")
    payload = json.loads(
        (_PROFILE_DIR / f"{profile_id}.json").read_text(encoding="utf-8"),
    )
    return ReferenceDeckStyleProfile.model_validate(payload)


@lru_cache(maxsize=1)
def load_universal_design_grammar() -> tuple[str, ...]:
    payload = json.loads(
        (_PROFILE_DIR / "universal-design-grammar.json").read_text(
            encoding="utf-8",
        ),
    )
    return tuple(str(item) for item in payload["principles"])


def infer_template_mode(user_request: str) -> TemplateMode:
    """Interpret ordinary user language as a template-obedience policy."""

    normalized = re.sub(r"\s+", "", user_request).casefold()
    if re.search(r"严格|一处也不要改|一个地方都不要改|集团模板|必须遵循|strict", normalized):
        return TemplateMode.STRICT
    if re.search(r"参考.*(?:感觉|审美|风格)|类似.*风格|style.?reference", normalized):
        return TemplateMode.STYLE_REFERENCE
    return TemplateMode.BRAND_PRESERVE_OPTIMIZE


def resolve_template_mode(request: PresentationJobRequest) -> TemplateMode:
    """Prefer an explicit API policy, otherwise interpret the user's words."""

    return request.template_mode or infer_template_mode(request.user_request)


def _relationship_set(slides: Sequence[DeckPlanSlide] | None) -> set[RelationshipType]:
    return {slide.intent.relationship_type for slide in (slides or [])}


def _select_autonomous_profile(
    request: PresentationJobRequest,
    brief: PresentationBrief,
    slides: Sequence[DeckPlanSlide] | None,
    author_direction: VisualDirection | None = None,
) -> str:
    """Choose a profile before composition; the compiler never sees this branch."""

    corpus = " ".join(
        [request.user_request, request.audience, brief.objective, brief.narrative],
    ).casefold()
    relationships = _relationship_set(slides)
    scores = {profile_id: 0 for profile_id in _BUILTIN_PROFILE_IDS}

    # Explicit visual direction is a stronger signal than subject-matter
    # nouns. A deck can discuss a product launch while still asking for a
    # restrained, light editorial treatment; treating every occurrence of
    # "product" or "release" as art direction produced dark launch decks for
    # clearly specified brand-communication briefs.
    explicit_visual_intents = {
        "editorial-executive": (
            "品牌传播风",
            "大面积留白",
            "克制可信",
            "新闻感",
            "编辑风",
            "报刊感",
            "editorial style",
        ),
        "minimal-keynote": (
            "极简演讲风",
            "主题演讲风",
            "keynote style",
        ),
        "product-launch": (
            "新品发布会风格",
            "发布会风格",
            "高对比",
            "动感",
            "舞台感",
            "launch event style",
        ),
        "data-analytical": (
            "数据分析风格",
            "分析报告风",
            "analytical style",
        ),
    }
    for profile_id, phrases in explicit_visual_intents.items():
        scores[profile_id] += sum(
            12 for phrase in phrases if phrase in corpus
        )
        if author_direction is not None:
            # A page author's art direction outranks subject-matter nouns,
            # while an explicit preference in the user's brief remains first.
            concept = author_direction.visual_concept.casefold()
            scores[profile_id] += sum(
                8 for phrase in phrases if phrase in concept
            )

    keyword_weights = {
        "editorial-executive": (
            "汇报", "经营", "管理层", "董事会", "咨询", "报告", "方案", "复盘",
        ),
        "minimal-keynote": (
            "演讲", "主题演讲", "论坛", "愿景", "极简", "keynote", "宣言",
        ),
        "product-launch": (
            "发布", "产品", "新品", "功能", "上市", "路演", "launch", "release",
        ),
        "data-analytical": (
            "数据", "指标", "分析", "趋势", "诊断", "实验", "财务", "增长", "kpi",
        ),
    }
    for profile_id, words in keyword_weights.items():
        scores[profile_id] += sum(3 for word in words if word in corpus)

    analytical = {
        RelationshipType.DATA_TREND,
        RelationshipType.METRIC_DASHBOARD,
        RelationshipType.EVIDENCE_COLLECTION,
    }
    speaker_led = {
        RelationshipType.HERO_STATEMENT,
        RelationshipType.QUOTE,
        RelationshipType.HERO_VISUAL,
    }
    launch = {
        RelationshipType.BEFORE_AFTER,
        RelationshipType.ROADMAP,
        RelationshipType.JOURNEY,
    }
    scores["data-analytical"] += 2 * len(relationships & analytical)
    scores["minimal-keynote"] += 2 * len(relationships & speaker_led)
    scores["product-launch"] += 2 * len(relationships & launch)
    scores["editorial-executive"] += max(1, len(relationships))
    return max(_BUILTIN_PROFILE_IDS, key=lambda item: (scores[item], -_BUILTIN_PROFILE_IDS.index(item)))


class ColorStrategyEngine:
    """Resolve role-based color from identity, intent, evidence and context.

    Reference and brand inputs remain optional.  The engine selects among
    curated, quality-controlled directions using semantic tags, then applies
    higher-priority HARD brand or explicit-user accents.  It never exposes a
    profile-id switch to composition or the native compiler.
    """

    def resolve(
        self,
        *,
        style: ReferenceDeckStyleProfile,
        user_request: str,
        audience: str = "",
        objective: str = "",
        narrative: str = "",
        constraints: Sequence[DesignConstraint] = (),
        quality: TemplateQualityProfile | None = None,
        template_mode: TemplateMode = TemplateMode.BRAND_PRESERVE_OPTIMIZE,
        reference_palette: Sequence[str] = (),
        material_palette: Sequence[str] = (),
        author_palette: Sequence[str] = (),
        seed: int = 0,
    ) -> ColorProfile:
        corpus = " ".join(
            [
                user_request,
                audience,
                objective,
                narrative,
                " ".join(style.visual_personality),
                style.density,
            ],
        ).casefold()
        semantic_tags = {
            tag
            for tag, words in _COLOR_TONE_KEYWORDS.items()
            if any(word.casefold() in corpus for word in words)
        }
        semantic_tags.update(
            token.casefold().replace("_", "-")
            for token in style.visual_personality
            if token.strip()
        )
        scored = [
            (
                len(semantic_tags & set(candidate["tags"])),
                candidate,
            )
            for candidate in _COLOR_DIRECTIONS
        ]
        best_score = max(score for score, _candidate in scored)
        # The style personality narrows the field; the variation seed selects
        # one of the still-plausible directions. This prevents a style from
        # becoming synonymous with a single color skin.
        eligible = [
            candidate
            for score, candidate in scored
            if score >= max(1, best_score - 2)
        ]
        direction = max(
            eligible,
            key=lambda candidate: self._stable_tiebreak(
                str(candidate["id"]),
                seed,
            ),
        )
        resolved = {
            key: value
            for key, value in direction.items()
            if key not in {"id", "tags"}
        }
        sources: list[str] = []

        rejected = {
            constraint.value.casefold()
            for constraint in constraints
            if constraint.level == ConstraintLevel.REJECT
        }
        color_quality = (
            quality.color_discipline
            if quality is not None
            else QualityBand.NOT_APPLICABLE
        )
        reference_allowed = (
            template_mode == TemplateMode.STRICT
            or color_quality
            in {
                QualityBand.STRONG,
                QualityBand.ADEQUATE,
                QualityBand.NOT_APPLICABLE,
            }
        ) and not any("uncontrolled_palette" in value for value in rejected)
        observed_reference = self._valid_palette(
            reference_palette or style.palette,
        )
        if (
            style.source_format not in {"builtin", "synthetic"}
            and observed_reference
            and reference_allowed
        ):
            self._apply_reference_roles(resolved, style, observed_reference)
            sources.append("reference_deck")

        observed_author = self._valid_palette(author_palette)
        observed_material = self._valid_palette(material_palette)
        if observed_author and not sources:
            # VisualDirection uses the same role order as the native writer:
            # canvas, ink, accent, then optional inverse/remaining colors.
            resolved["background"] = observed_author[0]
            if len(observed_author) > 1:
                resolved["primary_text"] = observed_author[1]
            if len(observed_author) > 2:
                resolved["accent_primary"] = observed_author[2]
                resolved["chart_series"] = [
                    observed_author[2],
                    *[
                        value for value in resolved["chart_series"]
                        if value != observed_author[2]
                    ],
                ][:8]
            sources.append("author_direction")
        elif observed_material and not sources:
            resolved["accent_primary"] = observed_material[0]
            if len(observed_material) > 1:
                resolved["accent_secondary"] = observed_material[1]
            sources.append("user_materials")

        explicit_accent = self._explicit_user_accent(user_request)
        hard_brand = self._hard_brand_anchor(constraints)
        accent = hard_brand or explicit_accent
        if accent:
            resolved["accent_primary"] = accent
            resolved["chart_series"] = [
                accent,
                *[
                    value
                    for value in resolved["chart_series"]
                    if value != accent
                ],
            ][:8]
        if explicit_accent:
            sources.insert(0, "explicit_user_preference")
        if hard_brand:
            sources.insert(0, "brand_constraint")
        if not sources:
            sources.append("semantic_synthesis")

        resolved["chart_primary"] = resolved["accent_primary"]
        resolved["chart_secondary"] = resolved.get(
            "chart_secondary",
            resolved["accent_secondary"],
        )
        neutral_scale = list(
            dict.fromkeys(
                [
                    resolved["primary_text"],
                    resolved["secondary_text"],
                    resolved["tertiary_text"],
                    resolved["border"],
                    resolved["divider"],
                    resolved["surface"],
                    resolved["background"],
                ],
            ),
        )
        forbidden = [
            "do not use library-default chart colors",
            "do not use accent color on most objects; emphasis requires scarcity",
            "do not use low-contrast text on colored surfaces",
            "do not add gradients or saturation without semantic purpose",
            *sorted(rejected),
        ]
        return ColorProfile(
            palette_source=list(dict.fromkeys(sources)),
            brand_anchor=hard_brand,
            semantic_tone=sorted(semantic_tags),
            background=str(resolved["background"]),
            surface=str(resolved["surface"]),
            primary_text=str(resolved["primary_text"]),
            secondary_text=str(resolved["secondary_text"]),
            tertiary_text=str(resolved["tertiary_text"]),
            accent_primary=str(resolved["accent_primary"]),
            accent_secondary=str(resolved["accent_secondary"]),
            divider=str(resolved["divider"]),
            border=str(resolved["border"]),
            chart_primary=str(resolved["chart_primary"]),
            chart_secondary=str(resolved["chart_secondary"]),
            chart_series=list(dict.fromkeys(resolved["chart_series"])),
            positive="2F7D5B",
            negative="B14343",
            warning="B97824",
            neutral_scale=neutral_scale,
            usage_rules=[
                "reserve accent_primary for the page anchor, conclusion or one highlighted data series",
                "use neutral text roles to preserve hierarchy on information-dense slides",
                "vary color area by density while keeping the deck palette stable",
                "use chart_series in order and mute non-focus series before adding colors",
                "treat brand_anchor as identity evidence, not permission for full-page saturation",
            ],
            forbidden_usage=list(dict.fromkeys(forbidden)),
        )

    @staticmethod
    def _stable_tiebreak(candidate_id: str, seed: int) -> int:
        digest = hashlib.sha256(f"{seed}:{candidate_id}".encode()).hexdigest()
        return int(digest[:8], 16)

    @staticmethod
    def _valid_palette(values: Sequence[str]) -> list[str]:
        return list(
            dict.fromkeys(
                value
                for raw in values
                if (value := _valid_hex(str(raw))) is not None
            ),
        )

    @staticmethod
    def _constraint_color(value: str) -> str | None:
        match = re.search(r"#?([0-9A-Fa-f]{6})(?![0-9A-Fa-f])", value)
        if match:
            return match.group(1).upper()
        folded = value.casefold()
        for name, color in _NAMED_ACCENTS.items():
            if name.casefold() in folded:
                return color
        return None

    def _hard_brand_anchor(
        self,
        constraints: Sequence[DesignConstraint],
    ) -> str | None:
        for constraint in constraints:
            if constraint.level != ConstraintLevel.HARD:
                continue
            if constraint.category not in {
                "palette",
                "brand_color",
                "color",
                "user_declared",
            }:
                continue
            color = self._constraint_color(
                f"{constraint.value} {constraint.rationale}",
            )
            if color:
                return color
        return None

    @staticmethod
    def _explicit_user_accent(user_request: str) -> str | None:
        match = re.search(r"#([0-9A-Fa-f]{6})(?![0-9A-Fa-f])", user_request)
        if match:
            return match.group(1).upper()
        folded = user_request.casefold()
        for name, color in _NAMED_ACCENTS.items():
            if re.search(
                rf"(?:使用|采用|偏向|喜欢|希望|要|以)[^，。；;]{{0,8}}{re.escape(name.casefold())}",
                folded,
            ):
                return color
        return None

    @staticmethod
    def _apply_reference_roles(
        resolved: dict[str, object],
        style: ReferenceDeckStyleProfile,
        palette: Sequence[str],
    ) -> None:
        roles = {
            key: value
            for key, raw in style.color_roles.items()
            if (value := _valid_hex(str(raw))) is not None
        }
        role_mapping = {
            "background": "canvas",
            "surface": "surface",
            "primary_text": "ink",
            "secondary_text": "muted",
            "tertiary_text": "tertiary",
            "accent_primary": "accent",
            "accent_secondary": "accent_secondary",
            "divider": "subtle",
            "border": "border",
        }
        for target, role in role_mapping.items():
            if role in roles:
                resolved[target] = roles[role]
        fallbacks = (
            "background",
            "primary_text",
            "secondary_text",
            "accent_primary",
            "divider",
        )
        for index, target in enumerate(fallbacks):
            if index < len(palette) and role_mapping[target] not in roles:
                resolved[target] = palette[index]
        resolved["chart_series"] = list(
            dict.fromkeys(
                [
                    str(resolved["accent_primary"]),
                    str(resolved["accent_secondary"]),
                    *palette[1:6],
                ],
            ),
        )


class AutonomousProfileSynthesizer:
    """Create intrinsic design policy when no reference artifact exists."""

    def synthesize(
        self,
        request: PresentationJobRequest,
        brief: PresentationBrief,
        slides: Sequence[DeckPlanSlide] | None = None,
        author_direction: VisualDirection | None = None,
    ) -> PresentationDesignContext:
        profile_id = _select_autonomous_profile(
            request, brief, slides, author_direction
        )
        style = load_style_profile(profile_id).model_copy(
            update={"variation_seed": request.seed},
        )
        mode = resolve_template_mode(request)
        color_profile = ColorStrategyEngine().resolve(
            style=style,
            user_request=request.user_request,
            audience=request.audience,
            objective=brief.objective,
            narrative=" ".join(
                part for part in (
                    brief.narrative,
                    author_direction.visual_concept if author_direction else "",
                ) if part
            ),
            template_mode=mode,
            author_palette=(
                author_direction.palette if author_direction else ()
            ),
            seed=request.seed,
        )
        design_profile = PresentationDesignProfile(
            identity=f"autonomous:{profile_id}:{request.seed}",
            source="autonomous",
            template_mode=mode,
            style_profile=style,
            color_profile=color_profile,
            design_grammar=list(load_universal_design_grammar()),
            preferred_compositions=list(style.preferred_compositions),
            forbidden_patterns=list(
                dict.fromkeys([*style.anti_patterns, *DesignQualityFloor().forbidden_patterns]),
            ),
            template_quality_profile=TemplateQualityProfile(
                readability=QualityBand.NOT_APPLICABLE,
                information_hierarchy=QualityBand.NOT_APPLICABLE,
                grid_consistency=QualityBand.NOT_APPLICABLE,
                spacing_quality=QualityBand.NOT_APPLICABLE,
                typography_quality=QualityBand.NOT_APPLICABLE,
                color_discipline=QualityBand.NOT_APPLICABLE,
                visual_balance=QualityBand.NOT_APPLICABLE,
                visual_rhythm=QualityBand.NOT_APPLICABLE,
                chart_quality=QualityBand.NOT_APPLICABLE,
                image_quality=QualityBand.NOT_APPLICABLE,
                brand_consistency=QualityBand.NOT_APPLICABLE,
                composition_variety=QualityBand.NOT_APPLICABLE,
                overall_quality=QualityBand.NOT_APPLICABLE,
                strengths=["autonomously synthesized; no reference was required"],
                inheritance_recommendations=[
                    "apply universal quality floor and content-derived composition",
                ],
            ),
        )
        return PresentationDesignContext(
            profile=design_profile,
            reference_input_present=False,
            generated_for_request=request.user_request,
        )


def resolve_design_context(
    request: PresentationJobRequest,
    brief: PresentationBrief,
    slides: Sequence[DeckPlanSlide] | None = None,
    *,
    reference_context: PresentationDesignContext | None = None,
    author_direction: VisualDirection | None = None,
) -> PresentationDesignContext:
    """Return one context for every downstream stage, with or without reference."""

    if reference_context is not None:
        return reference_context
    return AutonomousProfileSynthesizer().synthesize(
        request, brief, slides, author_direction
    )


def visual_direction_from_context(context: PresentationDesignContext) -> VisualDirection:
    """Compatibility projection; this does not become an execution switch."""

    style = context.profile.style_profile
    colors = context.profile.color_profile
    if colors is not None:
        palette = [
            f"#{value}"
            for value in dict.fromkeys(
                [
                    colors.background,
                    colors.primary_text,
                    colors.accent_primary,
                    colors.secondary_text,
                    colors.accent_secondary,
                    colors.divider,
                    *colors.chart_series,
                ],
            )
        ]
    else:
        palette = [f"#{value.lstrip('#')}" for value in style.palette]
    title_font = str(style.typography.get("title_font", "Noto Sans SC"))
    body_font = str(style.typography.get("body_font", title_font))
    return VisualDirection(
        visual_concept=" / ".join(style.visual_personality),
        palette=palette[:8],
        typography=f"{title_font} / {body_font}",
        density=("medium" if style.density == "adaptive" else style.density),
        image_language="; ".join(style.image_language) or "evidence-led imagery",
        chart_language="; ".join(style.chart_language) or "semantic native charts",
        shape_language="; ".join(style.shape_language) or "meaningful native geometry",
        grid=json.dumps(style.grid, ensure_ascii=False, sort_keys=True),
        recurring_motif="; ".join(style.recurring_motifs) or "consistent deck marker",
    )


def resolve_native_composition_tokens(
    context: PresentationDesignContext,
) -> NativeCompositionTokens:
    """Resolve style and quality policy into backend-neutral execution values."""

    style = context.profile.style_profile
    colors = context.profile.color_profile
    roles = style.color_roles
    palette = [
        color
        for value in style.palette
        if (color := _valid_hex(str(value))) is not None
    ]

    def color(role: str, palette_index: int, fallback: str) -> str:
        candidate = _valid_hex(str(roles.get(role, "")))
        if candidate is not None:
            return candidate
        if palette_index < len(palette):
            return palette[palette_index]
        return fallback

    typography = style.typography
    floor = context.profile.quality_floor
    title_size = max(
        24.0,
        min(
            72.0,
            float(
                typography.get(
                    "title_size",
                    typography.get("title_font_size", 34),
                ),
            ),
        ),
    )
    body_size = max(
        floor.minimum_body_font_size,
        min(
            36.0,
            float(
                typography.get(
                    "body_size",
                    typography.get("body_font_size", 18),
                ),
            ),
        ),
    )
    caption_size = max(
        floor.minimum_caption_font_size,
        min(
            24.0,
            float(
                typography.get(
                    "caption_size",
                    typography.get("caption_font_size", 12),
                ),
            ),
        ),
    )
    title_font = str(
        typography.get(
            "title_font",
            typography.get("heading_font", "Noto Sans SC"),
        ),
    ).strip()
    body_font = str(typography.get("body_font", title_font)).strip()
    if colors is not None:
        canvas = colors.background
        ink = colors.primary_text
        muted = colors.secondary_text
        accent = colors.accent_primary
        subtle = colors.divider
        chart_series = list(colors.chart_series)
    else:
        canvas = color("canvas", 0, "F7F7F4")
        ink = color("ink", 1, "171717")
        muted = color("muted", 2, "666666")
        accent = color("accent", 3, "D9583B")
        subtle = color("subtle", 4, "E5E5E0")
        chart_series = [accent, ink, muted, subtle]
    return NativeCompositionTokens(
        canvas=canvas,
        ink=ink,
        muted=muted,
        accent=accent,
        subtle=subtle,
        chart_series=chart_series,
        title_font=title_font or "Noto Sans SC",
        body_font=body_font or "Noto Sans SC",
        title_size=title_size,
        body_size=body_size,
        caption_size=caption_size,
    )


class DeckRhythmPlanner:
    """Plan whole-deck pacing before any coordinates are assigned."""

    def plan(
        self,
        slides: Sequence[DeckPlanSlide],
        context: PresentationDesignContext,
    ) -> DeckVisualPlan:
        beats: list[DeckVisualBeat] = []
        families: list[str] = []
        for index, slide in enumerate(slides):
            relationship = slide.intent.relationship_type
            if index == 0:
                role = DeckRhythmRole.OPENING
            elif index == len(slides) - 1:
                role = DeckRhythmRole.CONCLUSION
            elif relationship in {
                RelationshipType.DATA_TREND,
                RelationshipType.METRIC_DASHBOARD,
                RelationshipType.EVIDENCE_COLLECTION,
                RelationshipType.MATRIX,
            }:
                role = DeckRhythmRole.EVIDENCE if index % 3 else DeckRhythmRole.DENSE
            elif relationship in {
                RelationshipType.HERO_STATEMENT,
                RelationshipType.QUOTE,
                RelationshipType.HERO_VISUAL,
            }:
                role = DeckRhythmRole.BREATHING
            elif index % 5 == 0:
                role = DeckRhythmRole.ESCALATION
            else:
                role = DeckRhythmRole.EVIDENCE
            family = _composition_family(relationship, slide.intent.visual_role)
            families.append(family)
            beats.append(
                DeckVisualBeat(
                    slide_id=slide.id,
                    role=role,
                    composition_family=family,
                    dominant=role in {DeckRhythmRole.OPENING, DeckRhythmRole.CONCLUSION}
                    or index in {max(0, len(slides) // 2)},
                    density=(
                        "low"
                        if role
                        in {
                            DeckRhythmRole.OPENING,
                            DeckRhythmRole.BREATHING,
                            DeckRhythmRole.CONCLUSION,
                        }
                        else slide.intent.density
                    ),
                ),
            )
        return DeckVisualPlan(
            beats=beats,
            narrative_peaks=[beat.slide_id for beat in beats if beat.dominant],
            repeated_motifs=list(context.profile.style_profile.recurring_motifs[:3]),
            variation_notes=[
                f"{len(set(families))} semantic composition families across {len(slides)} slides",
                "composition follows relationships; profile identity never selects compiler code",
            ],
        )


def _composition_family(relationship: RelationshipType, visual_role: str) -> str:
    role = visual_role.casefold()
    if "opening" in role or "closing" in role:
        return "statement-field"
    families = {
        RelationshipType.COMPARISON: "comparison-field",
        RelationshipType.BEFORE_AFTER: "transformation-field",
        RelationshipType.CAUSE_EFFECT: "causal-flow",
        RelationshipType.CHRONOLOGY: "progression",
        RelationshipType.PROCESS: "progression",
        RelationshipType.JOURNEY: "progression",
        RelationshipType.ROADMAP: "progression",
        RelationshipType.HIERARCHY: "hierarchy-field",
        RelationshipType.LAYERS: "layered-system",
        RelationshipType.FUNNEL: "layered-system",
        RelationshipType.MATRIX: "matrix-field",
        RelationshipType.SPECTRUM: "spectrum-field",
        RelationshipType.DATA_TREND: "annotated-data",
        RelationshipType.METRIC_DASHBOARD: "metric-field",
        RelationshipType.EVIDENCE_COLLECTION: "evidence-field",
        RelationshipType.QUOTE: "quote-field",
        RelationshipType.HERO_VISUAL: "hero-field",
        RelationshipType.HERO_STATEMENT: "statement-field",
        RelationshipType.THESIS: "statement-field",
        RelationshipType.SUMMARY: "synthesis-field",
        RelationshipType.TEAM: "people-field",
        RelationshipType.GEOGRAPHY: "spatial-field",
        RelationshipType.COMPOSITION: "editorial-flow",
        RelationshipType.MIXED: "editorial-flow",
    }
    return families.get(relationship, "editorial-flow")


def _is_numeric_table_cell(value: str) -> bool:
    compact = re.sub(r"\s+", "", str(value or ""))
    return bool(
        compact
        and re.fullmatch(
            r"[+−-]?[$¥￥€£]?[\d,]+(?:\.\d+)?(?:%|[kKmMbB]|千|万|亿)?",
            compact,
        )
    )


def _is_presentational_comparison_table(table: TableObject) -> bool:
    """Recognize a bounded qualitative matrix as a comparison relationship.

    The relationship guides page geometry, but the table stays one native,
    editable evidence object.  Editorial restructuring belongs to the
    upstream author; deterministic composition must not repeat every header in
    one generated card per row.
    """

    column_count = len(table.headers)
    if not 3 <= column_count <= 5 or not 2 <= len(table.rows) <= 6:
        return False
    cells = [cell for row in table.rows for cell in row]
    if not cells:
        return False
    textual_count = sum(not _is_numeric_table_cell(cell) for cell in cells)
    return textual_count / len(cells) >= 0.6


class CompositionPlanner:
    """Convert slide meaning plus policy into explicit, non-template geometry."""

    def plan_slide(
        self,
        slide: DeckPlanSlide,
        context: PresentationDesignContext,
        beat: DeckVisualBeat,
    ) -> SlideCompositionPlan:
        style = context.profile.style_profile
        policy = style.composition_policy
        whitespace = float(policy.get("whitespace_bias", 0.56))
        asymmetry = float(policy.get("asymmetry", 0.56))
        digest = hashlib.sha256(
            f"{style.variation_seed}:{slide.id}:{slide.intent.key_message}".encode(),
        ).hexdigest()[:10]
        family = _composition_family(
            slide.intent.relationship_type,
            slide.intent.visual_role,
        )
        reading = (
            "left-to-right progression"
            if slide.intent.relationship_type
            in {
                RelationshipType.CHRONOLOGY,
                RelationshipType.PROCESS,
                RelationshipType.JOURNEY,
                RelationshipType.ROADMAP,
                RelationshipType.BEFORE_AFTER,
            }
            else "anchor then supporting evidence"
        )
        return SlideCompositionPlan(
            relationship_type=slide.intent.relationship_type,
            composition_family=family,
            spatial_strategy=(
                f"algorithmic {family}; {len(slide.content_blocks)} evidence units "
                f"at {beat.density} density"
            ),
            reading_path=reading,
            dominant_role=slide.intent.visual_role,
            grouping_logic="group by semantic evidence role, never by paragraph count alone",
            whitespace_target=max(0.15, min(0.9, whitespace)),
            asymmetry=max(0.0, min(1.0, asymmetry)),
            variation_key=digest,
            rationale=(
                f"relationship={slide.intent.relationship_type.value}; "
                f"rhythm={beat.role.value}; style policy is data input"
            ),
        )

    def compose_objects(
        self,
        slide: DeckPlanSlide,
        objects: Sequence[DeckObject],
        context: PresentationDesignContext,
        composition: SlideCompositionPlan,
    ) -> list[DeckObject]:
        """Assign every native object an explicit frame using semantic algorithms."""

        style = context.profile.style_profile
        tokens = resolve_native_composition_tokens(context)
        # A table is one native editable evidence object. Expanding a
        # qualitative table into one text block per row repeats every header
        # and creates a ledger-like slide. Editorial restructuring belongs to
        # the upstream author; deterministic composition only measures and
        # places the chosen native object.
        margin_left = float(style.margins.get("left", style.grid.get("margin_x", 0.72)))
        margin_right = float(style.margins.get("right", style.grid.get("margin_x", 0.72)))
        margin_top = float(style.margins.get("top", style.grid.get("margin_y", 0.42)))
        margin_bottom = float(style.margins.get("bottom", 0.44))
        title_scale = float(style.composition_policy.get("title_scale", 1.0))
        title_size = max(
            32.0,
            min(62.0, tokens.title_size * title_scale),
        )
        body_size = max(
            context.profile.quality_floor.minimum_body_font_size,
            min(26.0, tokens.body_size),
        )
        paper_width = 13.333
        content_width = paper_width - margin_left - margin_right
        is_statement = composition.relationship_type in {
            RelationshipType.HERO_STATEMENT,
            RelationshipType.THESIS,
            RelationshipType.QUOTE,
            RelationshipType.SUMMARY,
        }
        role = slide.intent.visual_role.casefold()
        is_opening = "opening" in role
        is_closing = "closing" in role
        is_opening_or_closing = is_opening or is_closing
        title_height = min(1.7 if is_statement else 1.25, 0.74 + title_size / 78)
        content_top = max(1.62, margin_top + title_height + 0.38)
        content_bottom = 7.5 - margin_bottom
        content_height = content_bottom - content_top

        title_objects = [
            obj for obj in objects if isinstance(obj, TextObject) and obj.role == "title"
        ]
        takeaway_objects = [
            obj for obj in objects if isinstance(obj, TextObject) and obj.slot == "takeaway"
        ]
        authored_shapes = [
            obj for obj in objects if isinstance(obj, ShapeObject) and obj not in title_objects
        ]
        content_objects = [
            obj
            for obj in objects
            if obj not in title_objects and obj not in authored_shapes
            and obj not in takeaway_objects
        ]
        # A slide that already carries an explicit native diagram plus explicit
        # coordinates is constrained slide code, not an unfinished legacy
        # layout.  Its geometry is an upstream composition decision.  The
        # planner may apply deck-level typography and scaffolding, but must not
        # silently stretch text across the authored diagram or reinterpret it
        # as a generic evidence rail.
        has_authored_composition = bool(authored_shapes) and all(
            obj.frame is not None for obj in objects
        )

        if is_opening:
            title_y = 1.28
            title_height = 1.92
            title_w = content_width * (0.66 + 0.12 * composition.asymmetry)
            content_top = 3.62
            content_height = max(2.22, content_bottom - content_top)
        elif is_closing:
            title_y = 1.58
            title_height = 1.72
            title_w = content_width * (0.68 + 0.12 * composition.asymmetry)
            content_top = 4.08
            content_height = max(1.25, content_bottom - content_top)
        elif is_statement:
            content_top = max(3.32, content_top)
            content_height = max(1.2, content_bottom - content_top)
            title_y = margin_top + 0.62
            title_w = content_width * (0.66 + 0.14 * composition.asymmetry)
        else:
            title_y = margin_top + 0.24
            title_w = content_width * (0.72 + 0.16 * composition.asymmetry)

        takeaway_text = slide.takeaway.strip() or "\n".join(obj.text for obj in takeaway_objects)
        takeaway_height = 0.86 if takeaway_text else 0.0
        if takeaway_height:
            content_height -= takeaway_height + 0.24

        frames = (
            [obj.frame for obj in content_objects]
            if has_authored_composition
            else self._content_frames(
                relationship=composition.relationship_type,
                visual_role=slide.intent.visual_role,
                objects=content_objects,
                x=margin_left,
                y=content_top,
                w=content_width,
                h=content_height,
                asymmetry=composition.asymmetry,
                variation=int(composition.variation_key[:4], 16),
            )
        )
        placed: list[DeckObject] = self._decorations(
            slide=slide,
            context=context,
            composition=composition,
            # Authored diagrams already carry their relationship grammar.  Only
            # add the global header/footer system around them.
            frames=() if has_authored_composition else frames,
            x=margin_left,
            y=content_top,
            w=content_width,
            h=content_height,
        )
        placed.extend(authored_shapes)
        if takeaway_height:
            placed.append(TextObject(
                id=takeaway_objects[0].id if takeaway_objects else f"{slide.id}-takeaway",
                role="body",
                slot="takeaway",
                text=takeaway_text,
                font_size=body_size,
                color=tokens.ink,
                frame=ObjectFrame(
                    x=margin_left, y=content_bottom - takeaway_height,
                    w=content_width, h=takeaway_height,
                ),
            ))
        for title in title_objects:
            placed.append(
                title.model_copy(
                    update={
                        "frame": (
                            title.frame
                            if has_authored_composition
                            else ObjectFrame(
                                x=margin_left,
                                y=title_y,
                                w=title_w,
                                h=title_height,
                            )
                        ),
                        "font_size": (
                            min(72.0, max(54.0, title_size * 1.45))
                            if is_opening
                            else min(64.0, max(46.0, title_size * 1.26))
                            if is_closing
                            else title_size
                        ),
                        "color": title.color or tokens.ink,
                    },
                ),
            )
        if not content_objects:
            return placed

        for index, (obj, frame) in enumerate(zip(content_objects, frames, strict=True)):
            update: dict[str, object] = {"frame": frame}
            if isinstance(obj, TextObject):
                relationship = composition.relationship_type
                is_anchor = index == 0 and composition.relationship_type in {
                    RelationshipType.HERO_STATEMENT,
                    RelationshipType.THESIS,
                    RelationshipType.QUOTE,
                    RelationshipType.SUMMARY,
                    RelationshipType.HIERARCHY,
                    RelationshipType.LAYERS,
                    RelationshipType.FUNNEL,
                    RelationshipType.COMPOSITION,
                    RelationshipType.MIXED,
                }
                requested_size = obj.font_size or body_size
                is_progression_stage = relationship in {
                    RelationshipType.CHRONOLOGY,
                    RelationshipType.PROCESS,
                    RelationshipType.JOURNEY,
                    RelationshipType.ROADMAP,
                    RelationshipType.SPECTRUM,
                    RelationshipType.CAUSE_EFFECT,
                }
                is_comparison_field = relationship in {
                    RelationshipType.COMPARISON,
                    RelationshipType.BEFORE_AFTER,
                }
                is_data_annotation = relationship in {
                    RelationshipType.DATA_TREND,
                    RelationshipType.METRIC_DASHBOARD,
                    RelationshipType.EVIDENCE_COLLECTION,
                }
                is_structural_cell = relationship in {
                    RelationshipType.MATRIX,
                    RelationshipType.HIERARCHY,
                    RelationshipType.LAYERS,
                    RelationshipType.FUNNEL,
                }
                is_evidence_rail = relationship in {
                    RelationshipType.COMPOSITION,
                    RelationshipType.MIXED,
                } and index > 0
                update["font_size"] = min(
                    46.0,
                    max(
                        body_size,
                        requested_size,
                        title_size * 0.78 if is_anchor else body_size,
                        22.0 if is_progression_stage else body_size,
                        max(24.0, title_size * 0.5) if is_comparison_field else body_size,
                        22.0 if is_data_annotation else body_size,
                        20.0 if is_structural_cell else body_size,
                        20.0 if is_evidence_rail else body_size,
                        20.0 if is_opening and index == 0 else body_size,
                    ),
                )
                has_semantic_weight = (
                    is_anchor
                    or is_progression_stage
                    or is_comparison_field
                    or is_data_annotation
                    or is_structural_cell
                )
                update["color"] = obj.color or (
                    tokens.ink if has_semantic_weight else tokens.muted
                )
                update["emphasis"] = obj.emphasis or has_semantic_weight
                if is_comparison_field and not obj.runs:
                    lines = [line.strip() for line in obj.text.splitlines() if line.strip()]
                    if len(lines) > 1:
                        heading_size = min(34.0, max(26.0, title_size * 0.52))
                        detail_size = min(23.0, max(body_size, title_size * 0.38))
                        update["runs"] = [
                            RichTextRun(
                                text=f"{lines[0]}\n",
                                bold=True,
                                color=tokens.accent,
                                font_size=heading_size,
                            ),
                            RichTextRun(
                                text="\n".join(lines[1:]),
                                color=tokens.ink,
                                font_size=detail_size,
                            ),
                        ]
                if is_opening_or_closing:
                    update["color"] = obj.color or tokens.muted
            placed.append(obj.model_copy(update=update))
        return placed

    @staticmethod
    def _content_frames(
        *,
        relationship: RelationshipType,
        visual_role: str,
        objects: Sequence[DeckObject],
        x: float,
        y: float,
        w: float,
        h: float,
        asymmetry: float,
        variation: int,
    ) -> list[ObjectFrame]:
        """Lay out semantic relationships; only a matrix creates a matrix."""

        count = len(objects)
        if count == 0:
            return []
        gap = 0.24 + (variation % 3) * 0.035
        # Refuse an unreadable scene before constructing negative-height
        # frames. The semantic planner must consolidate related evidence or
        # divide the material; neither dropping content nor tiny type is safe.
        rows = max(1, count - 1)
        if not any(isinstance(obj, ImageObject) for obj in objects) and relationship in {
            RelationshipType.MATRIX, RelationshipType.COMPARISON,
            RelationshipType.BEFORE_AFTER,
        }:
            rows = (count + 1) // 2
        if (h - gap * (rows - 1)) / rows < 0.28:
            raise ValueError(
                "composition capacity exceeded: consolidate related evidence "
                "or split the slide before rendering; content was not discarded"
            )

        image_index = next(
            (index for index, obj in enumerate(objects) if isinstance(obj, ImageObject)),
            None,
        )
        if image_index is not None:
            image_left = variation % 2 == 0
            image_w = w * (0.54 + 0.12 * asymmetry)
            text_w = w - image_w - gap
            image_x = x if image_left else x + text_w + gap
            text_x = x + image_w + gap if image_left else x
            support_count = count - 1
            support_h = (
                (h - gap * max(0, support_count - 1)) / max(1, support_count)
            )
            frames: list[ObjectFrame] = []
            support_index = 0
            for index in range(count):
                if index == image_index:
                    frames.append(ObjectFrame(x=image_x, y=y, w=image_w, h=h))
                else:
                    frames.append(
                        ObjectFrame(
                            x=text_x,
                            y=y + support_index * (support_h + gap),
                            w=text_w,
                            h=support_h,
                        ),
                    )
                    support_index += 1
            return frames

        if relationship in {
            RelationshipType.DATA_TREND,
            RelationshipType.METRIC_DASHBOARD,
            RelationshipType.EVIDENCE_COLLECTION,
        }:
            primary_index = next(
                (
                    index
                    for index, obj in enumerate(objects)
                    if isinstance(obj, (ChartObject, TableObject))
                ),
                0,
            )
            if count == 1:
                return [ObjectFrame(x=x, y=y + 0.12, w=w * 0.84, h=h * 0.82)]
            lead_w = w * (0.58 + 0.1 * asymmetry)
            rail_w = w - lead_w - gap
            support_count = count - 1
            support_h = (h - gap * max(0, support_count - 1)) / support_count
            frames = []
            support_index = 0
            for index in range(count):
                if index == primary_index:
                    frames.append(ObjectFrame(x=x, y=y, w=lead_w, h=h))
                else:
                    frames.append(
                        ObjectFrame(
                            x=x + lead_w + gap,
                            y=y + support_index * (support_h + gap),
                            w=rail_w,
                            h=support_h,
                        ),
                    )
                    support_index += 1
            return frames

        if relationship in {RelationshipType.COMPARISON, RelationshipType.BEFORE_AFTER}:
            # Each authored object is one comparison unit, never half of a
            # paragraph list. Larger sets use a grid; two or three use columns.
            columns = count if count <= 3 else 2 if count == 4 else 3
            rows = (count + columns - 1) // columns
            cell_w = (w - gap * (columns - 1)) / columns
            cell_h = (h - gap * (rows - 1)) / rows
            return [ObjectFrame(
                x=x + (index % columns) * (cell_w + gap),
                y=y + (index // columns) * (cell_h + gap),
                w=cell_w, h=cell_h,
            ) for index in range(count)]

        progression = {
            RelationshipType.CHRONOLOGY,
            RelationshipType.PROCESS,
            RelationshipType.JOURNEY,
            RelationshipType.ROADMAP,
            RelationshipType.SPECTRUM,
            RelationshipType.CAUSE_EFFECT,
        }
        if relationship in progression and count <= 6:
            stage_gap = max(0.18, gap * 0.8)
            stage_w = (w - stage_gap * (count - 1)) / count
            stage_y = y + min(1.36, h * 0.27)
            stage_h = min(2.32, h * 0.48)
            return [
                ObjectFrame(
                    x=x + index * (stage_w + stage_gap) + 0.08,
                    y=stage_y + (0.1 if (index + variation) % 2 else 0),
                    w=max(0.35, stage_w - 0.16),
                    h=stage_h,
                )
                for index in range(count)
            ]

        if relationship in {
            RelationshipType.HIERARCHY,
            RelationshipType.LAYERS,
            RelationshipType.FUNNEL,
        }:
            if count == 1:
                return [ObjectFrame(x=x, y=y + h * 0.12, w=w * 0.64, h=h * 0.7)]
            root_w = w * (0.42 + 0.1 * asymmetry)
            rail_w = w - root_w - gap * 1.6
            rail_count = count - 1
            rail_h = (h - gap * max(0, rail_count - 1)) / rail_count
            return [
                ObjectFrame(x=x, y=y + h * 0.12, w=root_w, h=h * 0.7),
                *[
                    ObjectFrame(
                        x=x + root_w + gap * 1.6,
                        y=y + index * (rail_h + gap),
                        w=rail_w,
                        h=rail_h,
                    )
                    for index in range(rail_count)
                ],
            ]

        if relationship == RelationshipType.MATRIX:
            columns = 2
            rows = (count + columns - 1) // columns
            cell_w = (w - gap) / columns
            cell_h = (h - gap * max(0, rows - 1)) / rows
            return [
                ObjectFrame(
                    x=x + (index % columns) * (cell_w + gap),
                    y=y + (index // columns) * (cell_h + gap),
                    w=cell_w,
                    h=cell_h,
                )
                for index in range(count)
            ]

        if relationship in {
            RelationshipType.HERO_STATEMENT,
            RelationshipType.QUOTE,
            RelationshipType.THESIS,
            RelationshipType.HERO_VISUAL,
            RelationshipType.SUMMARY,
        }:
            lead_w = w * (0.6 + 0.18 * asymmetry)
            if count == 1:
                return [ObjectFrame(x=x, y=y + h * 0.06, w=lead_w, h=h * 0.78)]
            frames = [ObjectFrame(x=x, y=y + h * 0.04, w=lead_w, h=h * 0.62)]
            support_count = count - 1
            support_h = (h - gap * max(0, support_count - 1)) / support_count
            for index in range(count - 1):
                frames.append(
                    ObjectFrame(
                        x=x + lead_w + gap,
                        y=y + index * (support_h + gap),
                        w=max(0.4, w - lead_w - gap),
                        h=max(0.2, support_h),
                    ),
                )
            return frames

        # Editorial flow is deliberately open and asymmetric.  A generic
        # square-root grid here would recreate the old "N blocks = N cards"
        # template logic under a new module name.
        if count == 1:
            return [ObjectFrame(x=x, y=y + h * 0.1, w=w * 0.76, h=h * 0.7)]
        lead_w = w * (0.5 + 0.14 * asymmetry)
        rail_w = w - lead_w - gap * 1.6
        rail_count = count - 1
        rail_h = (h - gap * max(0, rail_count - 1)) / rail_count
        lead_y = y + (0.18 if variation % 2 else 0)
        label_gutter = min(0.56, rail_w * 0.16)
        return [
            ObjectFrame(
                x=x + 0.2,
                y=lead_y,
                w=max(0.4, lead_w - 0.2),
                h=max(0.4, h - (lead_y - y)),
            ),
            *[
                ObjectFrame(
                    x=x + lead_w + gap * 1.6 + label_gutter,
                    y=y + index * (rail_h + gap),
                    w=max(0.4, rail_w - label_gutter),
                    h=rail_h,
                )
                for index in range(rail_count)
            ],
        ]

    @staticmethod
    def _decorations(
        *,
        slide: DeckPlanSlide,
        context: PresentationDesignContext,
        composition: SlideCompositionPlan,
        frames: Sequence[ObjectFrame],
        x: float,
        y: float,
        w: float,
        h: float,
    ) -> list[DeckObject]:
        """Create sparse native scaffolding from policy and information meaning."""

        tokens = resolve_native_composition_tokens(context)
        style = context.profile.style_profile
        prefix = f"{slide.id}-grammar"
        objects: list[DeckObject] = []
        if bool(style.header_system.get("enabled", True)):
            objects.extend(
                [
                    ShapeObject(
                        id=f"{prefix}-header-rule",
                        shape_type="line",
                        slot="design-scaffold",
                        frame=ObjectFrame(x=x, y=0.31, w=w, h=0.01),
                        line_color=tokens.subtle,
                        line_width=0.8,
                    ),
                    ShapeObject(
                        id=f"{prefix}-header-accent",
                        shape_type="line",
                        slot="design-scaffold",
                        frame=ObjectFrame(x=x, y=0.31, w=min(1.18, w * 0.12), h=0.01),
                        line_color=tokens.accent,
                        line_width=2.2,
                    ),
                ],
            )

        if bool(style.footer_system.get("enabled", True)) and bool(
            style.footer_system.get("page_number", True),
        ):
            objects.append(
                TextObject(
                    id=f"{prefix}-page-marker",
                    role="meta",
                    slot="page-marker",
                    text=f"{slide.order:02d}",
                    font_size=17,
                    color=tokens.muted,
                    align="right",
                    frame=ObjectFrame(x=x + w - 0.55, y=7.08, w=0.55, h=0.24),
                ),
            )

        relationship = composition.relationship_type
        role = slide.intent.visual_role.casefold()
        is_opening = "opening" in role
        is_closing = "closing" in role
        if is_opening:
            objects.extend(
                [
                    ShapeObject(
                        id=f"{prefix}-opening-spine",
                        shape_type="rect",
                        slot="design-scaffold",
                        frame=ObjectFrame(x=max(0.12, x - 0.18), y=1.3, w=0.08, h=1.72),
                        fill_color=tokens.accent,
                        line_color=tokens.accent,
                        line_width=0,
                    ),
                    ShapeObject(
                        id=f"{prefix}-opening-field",
                        shape_type="rect",
                        slot="design-scaffold",
                        frame=ObjectFrame(
                            x=x + w - max(0.18, 0.38 * composition.asymmetry),
                            y=0.92,
                            w=max(0.18, 0.38 * composition.asymmetry),
                            h=5.88,
                        ),
                        fill_color=tokens.accent,
                        line_color=tokens.accent,
                        line_width=0,
                    ),
                ],
            )
        elif is_closing:
            objects.extend(
                [
                    ShapeObject(
                        id=f"{prefix}-closing-rule",
                        shape_type="line",
                        slot="design-scaffold",
                        frame=ObjectFrame(x=x, y=3.56, w=min(2.2, w * 0.2), h=0.01),
                        line_color=tokens.accent,
                        line_width=4.0,
                    ),
                    ShapeObject(
                        id=f"{prefix}-closing-stop",
                        shape_type="rect",
                        slot="design-scaffold",
                        frame=ObjectFrame(x=x + w - 0.38, y=6.24, w=0.38, h=0.38),
                        fill_color=tokens.accent,
                        line_color=tokens.accent,
                        line_width=0,
                    ),
                ],
            )

        if relationship in {
            RelationshipType.PROCESS,
            RelationshipType.CHRONOLOGY,
            RelationshipType.JOURNEY,
            RelationshipType.ROADMAP,
            RelationshipType.SPECTRUM,
            RelationshipType.CAUSE_EFFECT,
        } and frames:
            # The lead-and-rail fallback has no reserved area for stage
            # markers. Keep its content intact instead of overlaying numbers
            # on adjacent rows. Horizontal progression reserves this space.
            horizontal_stages = max(frame.y for frame in frames) - min(frame.y for frame in frames) <= 0.11
            for index, frame in enumerate(frames, start=1):
                if not horizontal_stages:
                    continue
                stage_left = frame.x - 0.08
                objects.extend(
                    [
                        TextObject(
                            id=f"{prefix}-stage-number-{index}",
                            role="meta",
                            slot="relationship-marker",
                            text=f"{index:02d}",
                            emphasis=True,
                            font_size=32,
                            color=tokens.accent,
                            frame=ObjectFrame(
                                x=stage_left,
                                y=max(y + 0.18, frame.y - 0.72),
                                w=min(0.72, frame.w),
                                h=0.52,
                            ),
                        ),
                        ShapeObject(
                            id=f"{prefix}-stage-rule-{index}",
                            shape_type="line",
                            slot="relationship-path",
                            frame=ObjectFrame(
                                x=stage_left,
                                y=frame.y - 0.08,
                                w=frame.w,
                                h=0.01,
                            ),
                            line_color=tokens.accent if index == 1 else tokens.subtle,
                            line_width=2.4 if index == 1 else 1.1,
                        ),
                    ],
                )
                # Only adjacent stages in the same horizontal band have a
                # vertical separator. Dense timelines use the evidence rail;
                # extending a fixed-height separator there can leave the page.
                if index < len(frames) and abs(frame.y - frames[index].y) <= 0.11:
                    separator_x = (frame.x + frame.w + frames[index].x) / 2
                    objects.append(
                        ShapeObject(
                            id=f"{prefix}-stage-separator-{index}",
                            shape_type="line",
                            slot="relationship-path",
                            frame=ObjectFrame(
                                x=separator_x,
                                y=frame.y - 0.68,
                                w=0.01,
                                h=min(2.9, h * 0.58, y + h - (frame.y - 0.68)),
                            ),
                            line_color=tokens.subtle,
                            line_width=0.8,
                        ),
                    )

        if relationship in {RelationshipType.COMPOSITION, RelationshipType.MIXED} and len(frames) > 1:
            lead = frames[0]
            objects.append(
                ShapeObject(
                    id=f"{prefix}-lead-axis",
                    shape_type="line",
                    slot="relationship-axis",
                    frame=ObjectFrame(x=lead.x - 0.2, y=lead.y + 0.02, w=0.01, h=min(1.06, lead.h)),
                    line_color=tokens.accent,
                    line_width=3.0,
                ),
            )
            for index, frame in enumerate(frames[1:], start=1):
                objects.extend(
                    [
                        TextObject(
                            id=f"{prefix}-evidence-number-{index}",
                            role="meta",
                            slot="relationship-marker",
                            text=f"{index:02d}",
                            font_size=17,
                            emphasis=True,
                            color=tokens.accent,
                            frame=ObjectFrame(
                                x=frame.x - 0.52,
                                y=frame.y + 0.04,
                                w=0.38,
                                h=0.3,
                            ),
                        ),
                        ShapeObject(
                            id=f"{prefix}-evidence-rule-{index}",
                            shape_type="line",
                            slot="relationship-axis",
                            frame=ObjectFrame(
                                x=frame.x - 0.52,
                                y=frame.y,
                                w=frame.w + 0.52,
                                h=0.01,
                            ),
                            line_color=tokens.subtle,
                            line_width=0.9,
                        ),
                    ],
                )

        if relationship in {RelationshipType.COMPARISON, RelationshipType.BEFORE_AFTER}:
            first_row = [frame for frame in frames if abs(frame.y - y) < 0.01]
            # A single comparison band benefits from a shared axis. Multi-row
            # comparisons instead use short editorial rules per unit; extending
            # column dividers through multiple rows makes the slide read like a
            # spreadsheet again.
            if len(first_row) == len(frames):
                for index, (left, right) in enumerate(zip(first_row, first_row[1:])):
                    divider_x = (left.x + left.w + right.x) / 2
                    objects.append(
                        ShapeObject(
                            id=f"{prefix}-comparison-axis-{index}",
                            shape_type="line",
                            slot="relationship-axis",
                            frame=ObjectFrame(x=divider_x, y=y, w=0.01, h=h),
                            line_color=tokens.subtle,
                            line_width=1.2,
                        ),
                    )
            for index, frame in enumerate(frames):
                objects.append(
                    ShapeObject(
                        id=f"{prefix}-comparison-rule-{index}",
                        shape_type="line",
                        slot="relationship-axis",
                        frame=ObjectFrame(x=frame.x, y=frame.y - 0.06, w=frame.w, h=0.01),
                        line_color=tokens.accent if index == 0 else tokens.subtle,
                        line_width=2.2 if index == 0 else 1.0,
                    ),
                )
            if relationship == RelationshipType.BEFORE_AFTER and len(first_row) == 2:
                divider_x = (first_row[0].x + first_row[0].w + first_row[1].x) / 2
                objects.append(
                    ShapeObject(
                        id=f"{prefix}-transition",
                        shape_type="chevron",
                        slot="relationship-marker",
                        frame=ObjectFrame(x=divider_x - 0.16, y=y + h * 0.42, w=0.32, h=0.52),
                        fill_color=tokens.accent,
                        line_color=tokens.accent,
                        line_width=0,
                    ),
                )

        if relationship == RelationshipType.MATRIX:
            objects.extend(
                [
                    ShapeObject(
                        id=f"{prefix}-matrix-x",
                        shape_type="line",
                        slot="relationship-axis",
                        frame=ObjectFrame(x=x + w / 2, y=y, w=0.01, h=h),
                        line_color=tokens.subtle,
                        line_width=1.0,
                    ),
                    ShapeObject(
                        id=f"{prefix}-matrix-y",
                        shape_type="line",
                        slot="relationship-axis",
                        frame=ObjectFrame(x=x, y=y + h / 2, w=w, h=0.01),
                        line_color=tokens.subtle,
                        line_width=1.0,
                    ),
                ],
            )

        if relationship in {
            RelationshipType.HIERARCHY,
            RelationshipType.LAYERS,
            RelationshipType.FUNNEL,
        } and len(frames) > 1:
            root = frames[0]
            rail_x = root.x + root.w + 0.2
            objects.append(
                ShapeObject(
                    id=f"{prefix}-hierarchy-rail",
                    shape_type="line",
                    slot="relationship-axis",
                    frame=ObjectFrame(x=rail_x, y=y + 0.18, w=0.01, h=max(0.1, h - 0.36)),
                    line_color=tokens.accent,
                    line_width=1.4,
                ),
            )
        return objects


def enrich_plan_with_design_context(
    request: PresentationJobRequest,
    brief: PresentationBrief,
    plan: DeckPlan,
    *,
    reference_context: PresentationDesignContext | None = None,
) -> DeckPlan:
    """Attach the single context, deck rhythm and dynamic page decisions."""

    context = resolve_design_context(
        request,
        brief,
        plan.slides,
        reference_context=reference_context,
    )
    rhythm = DeckRhythmPlanner().plan(plan.slides, context)
    beats = {beat.slide_id: beat for beat in rhythm.beats}
    planner = CompositionPlanner()
    slides = [
        slide.model_copy(
            update={
                "composition_plan": planner.plan_slide(
                    slide,
                    context,
                    beats[slide.id],
                ),
            },
        )
        for slide in plan.slides
    ]
    return plan.model_copy(
        update={
            "visual_direction": visual_direction_from_context(context),
            "design_profile": context.profile,
            "design_context": context,
            "visual_plan": rhythm,
            "slides": slides,
        },
    )


def _relationship_for_existing_slide(slide: DeckSlide) -> RelationshipType:
    """Recover semantic intent for an old or newly redesigned DeckIR slide.

    This is a compatibility/revision boundary, not a layout lookup.  It reads
    semantic object roles and kinds so the composition planner can make a new
    decision before the style-agnostic compiler runs.
    """

    roles = {
        str(getattr(obj, "role", "")).casefold()
        for obj in slide.objects
    }
    slots = {
        str(getattr(obj, "slot", "")).casefold()
        for obj in slide.objects
    }
    layout_id = slide.layout_id.casefold()
    if "quote" in roles or "quote" in slots:
        return RelationshipType.QUOTE
    if any(isinstance(obj, ChartObject) for obj in slide.objects):
        return RelationshipType.DATA_TREND
    if any(getattr(obj, "kind", "") == "metric" for obj in slide.objects):
        return RelationshipType.METRIC_DASHBOARD
    if (
        layout_id == "comparison"
        or {"left", "right"}.issubset(slots)
        or any(
            isinstance(obj, TableObject)
            and _is_presentational_comparison_table(obj)
            for obj in slide.objects
        )
    ):
        return RelationshipType.COMPARISON
    if any(slot.startswith("step_") for slot in slots):
        return RelationshipType.PROCESS
    if any(slot.startswith("node_") for slot in slots):
        return RelationshipType.HIERARCHY
    return RelationshipType.COMPOSITION


def _textual_evidence(slide: DeckSlide) -> list[str]:
    evidence: list[str] = []
    for obj in slide.objects:
        value = getattr(obj, "text", None)
        if value:
            evidence.append(str(value))
            continue
        if isinstance(obj, TableObject):
            evidence.extend(obj.headers)
            evidence.extend(cell for row in obj.rows for cell in row)
            continue
        label = getattr(obj, "label", None)
        metric_value = getattr(obj, "value", None)
        if label or metric_value is not None:
            evidence.append(f"{label or ''} {metric_value or ''}".strip())
    return evidence


def compose_deck_ir_with_design_context(
    deck: DeckIR,
    request: PresentationJobRequest,
    brief: PresentationBrief,
    *,
    plan: DeckPlan | None = None,
    slide_ids: Iterable[str] | None = None,
    author_direction: VisualDirection | None = None,
) -> DeckIR:
    """Give selected DeckIR slides explicit geometry before compilation.

    Normal generation receives geometry while building DeckIR from the plan.
    This companion path is for a resumed legacy checkpoint or a whole-slide
    redesign whose objects were produced after the original plan.  It keeps
    semantic layout recovery in Design Intelligence and prevents the native
    compiler/backend from becoming a hidden template engine.
    """

    selected_ids = set(slide_ids or ()) or {slide.id for slide in deck.slides}
    context = deck.design_context
    if context is None and deck.design_profile is not None:
        context = PresentationDesignContext(
            profile=deck.design_profile,
            reference_input_present=deck.design_profile.source == "reference",
            generated_for_request=request.user_request,
        )

    planned_by_intent = {
        slide.intent.id: slide
        for slide in (plan.slides if plan is not None else [])
    }
    planned_by_id = {
        slide.id: slide
        for slide in (plan.slides if plan is not None else [])
    }
    semantic_slides: list[DeckPlanSlide] = []
    for order, deck_slide in enumerate(deck.slides, start=1):
        planned = (
            planned_by_intent.get(deck_slide.intent_id or "")
            or planned_by_id.get(deck_slide.id)
        )
        evidence = _textual_evidence(deck_slide)
        if planned is not None:
            semantic_slides.append(
                planned.model_copy(
                    update={
                        "id": deck_slide.id,
                        "order": order,
                        "content_blocks": evidence or planned.content_blocks,
                    },
                ),
            )
            continue
        title = next(
            (
                obj.text
                for obj in deck_slide.objects
                if isinstance(obj, TextObject) and obj.role == "title" and obj.text
            ),
            evidence[0] if evidence else deck.title,
        )
        relationship = _relationship_for_existing_slide(deck_slide)
        semantic_slides.append(
            DeckPlanSlide(
                id=deck_slide.id,
                order=order,
                title=title,
                intent=SlideIntent(
                    id=deck_slide.intent_id or f"intent-{deck_slide.id}",
                    purpose="重新组织已有页面的信息关系",
                    key_message=title,
                    evidence=evidence,
                    semantic_layout=deck_slide.provenance_layout,
                    relationship_type=relationship,
                    visual_role=(
                        "quote"
                        if relationship == RelationshipType.QUOTE
                        else "explanation"
                    ),
                    density=(
                        "high"
                        if sum(len(item) for item in evidence) > 360
                        else "low"
                        if sum(len(item) for item in evidence) < 80
                        else "medium"
                    ),
                ),
                content_blocks=evidence,
            ),
        )

    if context is None:
        context = resolve_design_context(
            request,
            brief,
            semantic_slides,
            author_direction=author_direction,
        )
    rhythm = DeckRhythmPlanner().plan(semantic_slides, context)
    beats = {beat.slide_id: beat for beat in rhythm.beats}
    planner_engine = CompositionPlanner()
    semantic_by_id = {slide.id: slide for slide in semantic_slides}
    composed_slides: list[DeckSlide] = []
    for deck_slide in deck.slides:
        semantic_slide = semantic_by_id[deck_slide.id]
        semantic_layout = deck_slide.provenance_layout
        if semantic_layout == "freeform":
            semantic_layout = semantic_slide.intent.semantic_layout
        if deck_slide.semantic_layout != semantic_layout:
            deck_slide = deck_slide.model_copy(update={"semantic_layout": semantic_layout})
        if deck_slide.composition_authority == CompositionAuthority.AUTHOR:
            missing_frames = [
                obj.id for obj in deck_slide.objects if obj.frame is None
            ]
            if missing_frames:
                raise ValueError(
                    "author-owned composition requires explicit frames for "
                    f"every object: {', '.join(missing_frames)}"
                )
            # An author-owned scene is already page code. Attach shared deck
            # context below, but do not add decorations, rewrite typography,
            # or reinterpret the authored relationship.
            composed_slides.append(deck_slide)
            continue
        # Attaching a PresentationDesignContext promotes the whole checkpoint
        # to the explicit-geometry contract.  A targeted redo changes design
        # intent only for ``selected_ids``; untouched historical slides are
        # materialized once so the low-level compiler never has to infer a
        # layout.  Already-composed slides remain byte-for-byte equivalent at
        # the object-model level.
        needs_geometry_promotion = any(
            obj.frame is None for obj in deck_slide.objects
        )
        if deck_slide.id not in selected_ids and not needs_geometry_promotion:
            composed_slides.append(deck_slide)
            continue
        composition = planner_engine.plan_slide(
            semantic_slide,
            context,
            beats[deck_slide.id],
        )
        composed_slides.append(
            deck_slide.model_copy(
                update={
                    # Explicit geometry is the compiler contract.  Semantic
                    # intent remains in semantic_layout, relationships and
                    # the plan; a legacy name must never make the compiler
                    # infer placement during a targeted redesign.
                    "layout_id": "freeform",
                    "objects": planner_engine.compose_objects(
                        semantic_slide,
                        deck_slide.objects,
                        context,
                        composition,
                    ),
                    "relationships": [
                        *deck_slide.relationships,
                        *(
                            []
                            if any(
                                item.relationship_type == composition.relationship_type
                                for item in deck_slide.relationships
                            )
                            or not deck_slide.objects
                            else [
                                ObjectRelationship(
                                    relationship_id=(
                                        f"{deck_slide.id}-page-relationship"
                                    ),
                                    relationship_type=composition.relationship_type,
                                    source_object_ids=[deck_slide.objects[0].id],
                                    target_object_ids=[
                                        obj.id for obj in deck_slide.objects[1:]
                                    ]
                                    or [deck_slide.objects[0].id],
                                    label="page composition relationship",
                                ),
                            ]
                        ),
                    ],
                },
            ),
        )

    return deck.model_copy(
        update={
            "slides": composed_slides,
            "design_profile": context.profile,
            "design_context": context,
            "composition_tokens": resolve_native_composition_tokens(context),
            "visual_direction": (
                author_direction or visual_direction_from_context(context)
            ),
            "visual_plan": rhythm,
        },
    )


def _valid_hex(value: str) -> str | None:
    normalized = value.lstrip("#").upper()
    return normalized if re.fullmatch(r"[0-9A-F]{6}", normalized) else None


def constraint_levels_by_object(
    context: PresentationDesignContext,
    object_ids: Iterable[str],
) -> dict[str, ConstraintLevel]:
    allowed = set(object_ids)
    result: dict[str, ConstraintLevel] = {}
    profile = context.profile
    for constraint in [
        *profile.hard_constraints,
        *profile.medium_constraints,
        *profile.soft_preferences,
        *profile.rejected_traits,
    ]:
        for object_id in constraint.object_ids:
            if object_id in allowed:
                result[object_id] = constraint.level
    return result
