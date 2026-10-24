# -*- coding: utf-8 -*-
"""Versioned public contracts for the presentation generation boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)


# A larger atomic permission envelope is opt-in through caller-supplied IDs.
# This does not widen the independent 40-action or 10-intent limits.
MAX_OBJECT_EDIT_SLIDES = 20


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class GenerationMode(str, Enum):
    STABLE = "stable"
    INTELLIGENT = "intelligent"
    CUSTOM_TEMPLATE = "custom_template"


class PlanningMode(str, Enum):
    """Select semantic planning authority explicitly.

    Production requests default to the isolated model planner.  The
    deterministic planner exists only for fixtures, recovery tests and an
    explicitly requested offline fallback.
    """

    MODEL = "model"
    DETERMINISTIC_FIXTURE = "deterministic_fixture"


class CompositionEngine(str, Enum):
    """One authoritative writer for a single deck mutation."""

    LEGACY_PYTHON_PPTX = "legacy_python_pptx"
    NATIVE_PPTX = "native_pptx"
    # Historical checkpoint value. New requests use the responsibility-based
    # ``native_pptx`` identity while old jobs keep resuming without migration.
    PPTXGENJS_V3 = "pptxgenjs_v3"


class CompositionAuthority(str, Enum):
    """Declare who owns page geometry after the authoring boundary.

    ``AUTO`` exists only for historical checkpoints and programmatic callers
    that predate this contract. Product-facing writers emit ``AUTHOR`` or
    ``DESIGN_SYSTEM`` explicitly.
    """

    AUTO = "auto"
    AUTHOR = "author"
    DESIGN_SYSTEM = "design_system"


def is_native_pptx_engine(engine: CompositionEngine) -> bool:
    """Return whether *engine* routes through the native compiler boundary."""

    return engine in {
        CompositionEngine.NATIVE_PPTX,
        CompositionEngine.PPTXGENJS_V3,
    }


class RelationshipType(str, Enum):
    THESIS = "thesis"
    COMPARISON = "comparison"
    CAUSE_EFFECT = "cause_effect"
    CHRONOLOGY = "chronology"
    PROCESS = "process"
    HIERARCHY = "hierarchy"
    MATRIX = "matrix"
    COMPOSITION = "composition"
    GEOGRAPHY = "geography"
    DATA_TREND = "data_trend"
    BEFORE_AFTER = "before_after"
    QUOTE = "quote"
    HERO_VISUAL = "hero_visual"
    SPECTRUM = "spectrum"
    LAYERS = "layers"
    FUNNEL = "funnel"
    JOURNEY = "journey"
    METRIC_DASHBOARD = "metric_dashboard"
    EVIDENCE_COLLECTION = "evidence_collection"
    TEAM = "team"
    ROADMAP = "roadmap"
    SUMMARY = "summary"
    HERO_STATEMENT = "hero_statement"
    MIXED = "mixed"


class ConstraintLevel(str, Enum):
    """How a learned reference trait may influence a new deck."""

    HARD = "hard"
    MEDIUM = "medium"
    SOFT = "soft"
    REJECT = "reject"


class TemplateMode(str, Enum):
    """User-intent policy for reference/template fidelity."""

    STRICT = "strict"
    BRAND_PRESERVE_OPTIMIZE = "brand_preserve_optimize"
    STYLE_REFERENCE = "style_reference"


class QualityBand(str, Enum):
    """Qualitative evidence band; intentionally not a pseudo-precise score."""

    STRONG = "strong"
    ADEQUATE = "adequate"
    WEAK = "weak"
    UNACCEPTABLE = "unacceptable"
    NOT_APPLICABLE = "not_applicable"


class DeckRhythmRole(str, Enum):
    OPENING = "opening"
    SETUP = "setup"
    ESCALATION = "escalation"
    EVIDENCE = "evidence"
    CONTRAST = "contrast"
    BREATHING = "breathing"
    DENSE = "dense"
    SYNTHESIS = "synthesis"
    CONCLUSION = "conclusion"


class EditabilityMode(str, Enum):
    NATIVE_EDITABLE = "native-editable"
    HYBRID_VISUAL = "hybrid-visual"
    IMAGE_SLIDE = "image-slide"


class EvidenceState(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class JobStage(str, Enum):
    CREATED = "created"
    BRIEF = "brief"
    RESEARCH = "research"
    PLAN = "plan"
    DECK_IR = "deck_ir"
    COMPOSE = "compose"
    RENDER = "render"
    QUALITY = "quality"
    REPAIR = "repair"
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PresentationProviderCostStatus(str, Enum):
    """Billing evidence state for one presentation-stage Provider call.

    ``cost_units`` remains the deterministic parent-job execution budget.  It
    is deliberately not money.  A missing upstream bill therefore stays
    ``UNKNOWN`` instead of being serialized as a zero-cost call.
    """

    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"
    EXACT = "exact"


class PresentationFailureCategory(str, Enum):
    """Stable failure causes used to select a recovery path."""

    CONTENT = "content"
    GEOMETRY = "geometry"
    AESTHETIC = "aesthetic"
    FILE = "file"
    MODEL_SERVICE = "model_service"
    AUTHORIZATION = "authorization"
    UNKNOWN = "unknown"


class PresentationRecoveryAction(str, Enum):
    """The next permitted operation for a classified presentation failure."""

    REVISE_CONTENT = "revise_content"
    PATCH_GEOMETRY = "patch_geometry"
    REDESIGN_SLIDE = "redesign_slide"
    RETRY_FILE_STAGE = "retry_file_stage"
    RETRY_MODEL_SERVICE = "retry_model_service"
    REQUEST_AUTHORIZATION = "request_authorization"
    INSPECT_FAILURE = "inspect_failure"


class PresentationFailureRoute(StrictModel):
    """Durable, machine-readable recovery advice backed by failure evidence."""

    schema_version: Literal["1.0"] = "1.0"
    category: PresentationFailureCategory
    recovery_action: PresentationRecoveryAction
    issue_codes: list[str] = Field(default_factory=list)
    slide_ids: list[str] = Field(default_factory=list)
    retry_stage: JobStage | None = None
    automatic_retry_allowed: bool = False
    requires_human: bool = False


class InputMaterial(StrictModel):
    id: str = Field(default_factory=lambda: f"source-{uuid4().hex}")
    name: str
    path: str | None = None
    content: str | None = Field(default=None, max_length=1_000_000)
    media_type: str | None = None
    sha256: str | None = None

    @model_validator(mode="after")
    def require_one_source(self) -> "InputMaterial":
        if bool(self.path) == bool(self.content):
            raise ValueError(
                "material requires exactly one of path or content",
            )
        return self


class TemplateAnchorRequirement(StrictModel):
    """One explicitly approved identity requirement, never a shape heuristic."""

    source_shape_id: int = Field(ge=1)
    purpose: Literal["title_frame", "static_brand", "page_number"]


class TemplatePageAnchorRequirements(StrictModel):
    target_slide_number: int = Field(ge=1, le=60)
    source_slide_number: int = Field(ge=1, le=60)
    anchors: list[TemplateAnchorRequirement] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_required_shapes(self) -> "TemplatePageAnchorRequirements":
        if len({anchor.source_shape_id for anchor in self.anchors}) != len(self.anchors):
            raise ValueError("required template anchors must not reuse source shapes")
        if sum(anchor.purpose == "title_frame" for anchor in self.anchors) > 1:
            raise ValueError("required template anchors allow only one title frame")
        return self


class TemplateAnchorRequirements(StrictModel):
    """Independent, SHA-pinned requirements supplied before page authoring.

    Requirements are not inferred from names, text, or the author's own uses.
    They remain request data, so legacy DeckIR fingerprints are unchanged.
    """

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: list[TemplatePageAnchorRequirements] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def unique_target_pages(self) -> "TemplateAnchorRequirements":
        if len({page.target_slide_number for page in self.pages}) != len(self.pages):
            raise ValueError("required template anchor target pages must be unique")
        return self


class PresentationJobRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    conversation_id: str
    user_request: str = Field(min_length=1, max_length=20_000)
    audience: str = "通用商务受众"
    language: str = "zh-CN"
    slide_count: int = Field(default=10, ge=1, le=60)
    mode: GenerationMode = GenerationMode.STABLE
    theme_id: str = "lantai-ink"
    template_path: str | None = None
    # ``None`` means the user did not choose a policy explicitly.  The design
    # intelligence layer then derives STRICT / BRAND_PRESERVE_OPTIMIZE /
    # STYLE_REFERENCE from ordinary language.  Keeping the absence observable
    # avoids a default value silently overriding phrases such as “严格按模板”.
    template_mode: TemplateMode | None = None
    template_anchor_requirements: TemplateAnchorRequirements | None = None
    model_id: str | None = None
    planning_mode: PlanningMode = PlanningMode.MODEL
    pipeline_version: str = "lantai-presentation-v3"
    idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=200,
    )
    timeout_seconds: int = Field(default=900, ge=30, le=7200)
    cost_budget_units: int = Field(default=100, ge=1, le=10_000)
    model_config_fingerprint: str | None = None
    input_manifest_hash: str | None = None
    analysis_validation_required: bool = False
    seed: int = 0
    visual_verification_required: bool = True
    vision_critique_required: bool = True
    composition_engine: CompositionEngine = CompositionEngine.NATIVE_PPTX
    materials: list[InputMaterial] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_custom_template(self) -> "PresentationJobRequest":
        if (
            self.mode == GenerationMode.CUSTOM_TEMPLATE
            and not self.template_path
        ):
            raise ValueError("custom_template mode requires template_path")
        return self


    @model_validator(mode="after")
    def validate_template_anchor_requirements(self) -> "PresentationJobRequest":
        required = self.template_anchor_requirements
        if required is None:
            return self
        if not is_native_pptx_engine(self.composition_engine):
            raise ValueError("required template anchors need a native composition engine")
        if not self.template_path or Path(self.template_path).suffix.casefold() != ".pptx":
            raise ValueError("required template anchors need a native PPTX template")
        if self.template_mode == TemplateMode.STYLE_REFERENCE:
            raise ValueError("style reference cannot require native template anchors")
        if {page.target_slide_number for page in required.pages} != set(range(1, self.slide_count + 1)):
            raise ValueError("required template anchors must cover every requested page")
        return self


class SkillExecutionIdentity(StrictModel):
    """Immutable host-supplied identity of the Skill that started a job."""

    name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_priority: int = Field(ge=0)
    version_text: str = ""


class PresentationSourceSection(StrictModel):
    title: str
    body: str
    source_ref: str
    # Document headings belong to this source, including when other materials
    # have different headings. Old checkpoints omit this additive field.
    document_title: str = ""
    # Historical checkpoints retain source_ref. Merged sections carry every
    # contributing source here so a lower slide ceiling cannot erase lineage.
    source_refs: list[str] = Field(default_factory=list)

    @property
    def grounded_text(self) -> str:
        return "\n".join(
            text for text in (self.document_title, self.title, self.body) if text
        )


class PresentationSourceCoverage(StrictModel):
    """What was actually read from one declared source, not a quality claim."""

    material_id: str
    source_name: str
    source_sha256: str
    converted_sha256: str | None = None
    method: str
    status: Literal["supported_content_extracted", "converted_doc_unverified"]
    extracted_chars: int = Field(ge=0)
    limitations: list[str] = Field(default_factory=list)


class PresentationBrief(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    objective: str = Field(min_length=1)
    audience: str
    narrative: str
    required_facts: list[str] = Field(default_factory=list)
    immutable_data: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    source_title: str = ""
    source_sections: list[PresentationSourceSection] = Field(
        default_factory=list,
    )
    source_coverage: list[PresentationSourceCoverage] = Field(default_factory=list)
    style_constraints: list[str] = Field(default_factory=list)
    prohibited_changes: list[str] = Field(default_factory=list)


class SourceCalculationTerm(StrictModel):
    """A model-selected, source-bound signed input; not a new source fact."""

    source_ref: str = Field(min_length=1)
    source_excerpt: str = Field(min_length=1, max_length=2000)
    value: str = Field(pattern=r"^[+-]?[0-9]{1,24}(?:\.[0-9]{1,12})?$")
    unit: str = Field(min_length=1, max_length=64)
    coefficient: Literal[-1, 1]


class SourceCalculationProof(StrictModel):
    """Bounded same-unit addition/subtraction, recomputed at every boundary.

    The model owns metric meaning and authorization selection. Runtime only
    verifies cited excerpts, input values, units and exact Decimal arithmetic.
    A prose formula or a persisted 'verified' flag cannot authorize a result.
    """

    terms: list[SourceCalculationTerm] = Field(min_length=2, max_length=24)
    result: str = Field(pattern=r"^[+-]?[0-9]{1,26}(?:\.[0-9]{1,12})?$")
    unit: str = Field(min_length=1, max_length=64)
    authorization_source_ref: str = Field(min_length=1)
    authorization_excerpt: str = Field(min_length=1, max_length=2000)


class SlideIntent(StrictModel):
    id: str = Field(default_factory=lambda: f"intent-{uuid4().hex}")
    purpose: str
    key_message: str
    evidence: list[str] = Field(default_factory=list)
    relationship_type: RelationshipType = RelationshipType.COMPOSITION
    visual_role: str = "explanation"
    semantic_layout: str = "key_judgment"
    source_refs: list[str] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)
    immutable_facts: list[str] = Field(default_factory=list)
    calculation_proofs: list[SourceCalculationProof] = Field(default_factory=list, max_length=120)
    editability_requirement: EditabilityMode = EditabilityMode.NATIVE_EDITABLE
    density: Literal["low", "medium", "high"] = "medium"


class VisualDirection(StrictModel):
    visual_concept: str
    palette: list[str] = Field(min_length=2, max_length=8)
    typography: str
    density: Literal["low", "medium", "high"] = "medium"
    image_language: str
    chart_language: str
    shape_language: str
    grid: str
    recurring_motif: str


class DesignConstraint(StrictModel):
    """One auditable template/reference constraint.

    ``value`` stays descriptive because a constraint can target a logo, a
    color token, a master object or a prose requirement.  Object-level repair
    enforcement uses ``object_ids`` rather than trying to interpret prose.
    """

    id: str = Field(default_factory=lambda: f"constraint-{uuid4().hex}")
    category: str
    value: str
    level: ConstraintLevel
    source: str = "reference"
    rationale: str
    object_ids: list[str] = Field(default_factory=list)


class TemplateQualityProfile(StrictModel):
    """Qualitative arbitration result for a supplied reference deck."""

    readability: QualityBand = QualityBand.ADEQUATE
    information_hierarchy: QualityBand = QualityBand.ADEQUATE
    grid_consistency: QualityBand = QualityBand.ADEQUATE
    spacing_quality: QualityBand = QualityBand.ADEQUATE
    typography_quality: QualityBand = QualityBand.ADEQUATE
    color_discipline: QualityBand = QualityBand.ADEQUATE
    visual_balance: QualityBand = QualityBand.ADEQUATE
    visual_rhythm: QualityBand = QualityBand.ADEQUATE
    chart_quality: QualityBand = QualityBand.NOT_APPLICABLE
    image_quality: QualityBand = QualityBand.NOT_APPLICABLE
    brand_consistency: QualityBand = QualityBand.ADEQUATE
    composition_variety: QualityBand = QualityBand.ADEQUATE
    overall_quality: QualityBand = QualityBand.ADEQUATE
    strengths: list[str] = Field(default_factory=list)
    major_issues: list[str] = Field(default_factory=list)
    inheritance_recommendations: list[str] = Field(default_factory=list)


class DesignQualityFloor(StrictModel):
    """Functional minimum applied unless STRICT template intent forbids redesign."""

    minimum_body_font_size: float = Field(default=17, ge=12, le=28)
    minimum_caption_font_size: float = Field(default=12, ge=9, le=24)
    require_clear_hierarchy: bool = True
    require_visual_anchor: bool = True
    require_stable_alignment: bool = True
    require_reasonable_whitespace: bool = True
    require_readable_charts: bool = True
    require_correct_image_aspect: bool = True
    maximum_consecutive_same_composition: int = Field(default=2, ge=1, le=6)
    forbidden_patterns: list[str] = Field(
        default_factory=lambda: [
            "tiny_body_text",
            "stretched_image",
            "card_spam",
            "random_heavy_shadow",
            "decorative_wordart",
            "uncontrolled_palette",
        ],
    )


class ReferenceDeckStyleProfile(StrictModel):
    """Portable, data-only design DNA learned from one or more references."""

    schema_version: Literal["1.0"] = "1.0"
    id: str
    source_deck: str = ""
    source_format: Literal["pptx", "pdf", "images", "synthetic", "builtin"] = (
        "builtin"
    )
    visual_personality: list[str] = Field(default_factory=list)
    typography: dict[str, str | float] = Field(default_factory=dict)
    palette: list[str] = Field(default_factory=list)
    color_roles: dict[str, str] = Field(default_factory=dict)
    grid: dict[str, str | float | int] = Field(default_factory=dict)
    spacing_scale: list[float] = Field(default_factory=list)
    margins: dict[str, float] = Field(default_factory=dict)
    hierarchy: dict[str, str | float] = Field(default_factory=dict)
    density: Literal["low", "medium", "high", "adaptive"] = "adaptive"
    shape_language: list[str] = Field(default_factory=list)
    rule_language: list[str] = Field(default_factory=list)
    number_language: list[str] = Field(default_factory=list)
    chart_language: list[str] = Field(default_factory=list)
    image_language: list[str] = Field(default_factory=list)
    header_system: dict[str, str | float | bool] = Field(default_factory=dict)
    footer_system: dict[str, str | float | bool] = Field(default_factory=dict)
    recurring_motifs: list[str] = Field(default_factory=list)
    composition_principles: list[str] = Field(default_factory=list)
    preferred_relationships: list[RelationshipType] = Field(default_factory=list)
    preferred_compositions: list[str] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)
    composition_policy: dict[str, str | float | bool] = Field(
        default_factory=dict,
    )
    variation_seed: int = 0


class ColorProfile(StrictModel):
    """Role-based color policy resolved before slide composition.

    A style describes a visual personality; it is not a fixed color theme.
    This profile records why colors were selected and gives every downstream
    stage the same semantic roles for pages, charts, critique and repair.
    """

    schema_version: Literal["1.0"] = "1.0"
    palette_source: list[str] = Field(default_factory=list)
    brand_anchor: str | None = Field(
        default=None,
        pattern=r"^[0-9A-Fa-f]{6}$",
    )
    semantic_tone: list[str] = Field(default_factory=list)
    background: str = Field(default="F7F7F4", pattern=r"^[0-9A-Fa-f]{6}$")
    surface: str = Field(default="FFFFFF", pattern=r"^[0-9A-Fa-f]{6}$")
    primary_text: str = Field(default="171717", pattern=r"^[0-9A-Fa-f]{6}$")
    secondary_text: str = Field(default="5E625F", pattern=r"^[0-9A-Fa-f]{6}$")
    tertiary_text: str = Field(default="888C88", pattern=r"^[0-9A-Fa-f]{6}$")
    accent_primary: str = Field(default="8E3B46", pattern=r"^[0-9A-Fa-f]{6}$")
    accent_secondary: str = Field(default="C7A76B", pattern=r"^[0-9A-Fa-f]{6}$")
    divider: str = Field(default="D7D8D3", pattern=r"^[0-9A-Fa-f]{6}$")
    border: str = Field(default="C9CBC6", pattern=r"^[0-9A-Fa-f]{6}$")
    chart_primary: str = Field(default="8E3B46", pattern=r"^[0-9A-Fa-f]{6}$")
    chart_secondary: str = Field(default="435A52", pattern=r"^[0-9A-Fa-f]{6}$")
    chart_series: list[str] = Field(default_factory=list, max_length=12)
    positive: str = Field(default="2F7D5B", pattern=r"^[0-9A-Fa-f]{6}$")
    negative: str = Field(default="B14343", pattern=r"^[0-9A-Fa-f]{6}$")
    warning: str = Field(default="B97824", pattern=r"^[0-9A-Fa-f]{6}$")
    neutral_scale: list[str] = Field(default_factory=list, max_length=12)
    usage_rules: list[str] = Field(default_factory=list)
    forbidden_usage: list[str] = Field(default_factory=list)


class PresentationDesignProfile(StrictModel):
    """The single policy object consumed by planning, composition and QA."""

    schema_version: Literal["1.0"] = "1.0"
    identity: str
    source: Literal["autonomous", "reference", "builtin", "custom"] = (
        "autonomous"
    )
    template_mode: TemplateMode = TemplateMode.BRAND_PRESERVE_OPTIMIZE
    brand_constraints: list[DesignConstraint] = Field(default_factory=list)
    master_constraints: list[DesignConstraint] = Field(default_factory=list)
    hard_constraints: list[DesignConstraint] = Field(default_factory=list)
    medium_constraints: list[DesignConstraint] = Field(default_factory=list)
    soft_preferences: list[DesignConstraint] = Field(default_factory=list)
    rejected_traits: list[DesignConstraint] = Field(default_factory=list)
    style_profile: ReferenceDeckStyleProfile
    # Optional only for backward-compatible reading of pre-color-engine
    # checkpoints. Every newly synthesized or learned context populates it.
    color_profile: ColorProfile | None = None
    design_grammar: list[str] = Field(default_factory=list)
    preferred_compositions: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)
    reference_examples: list[str] = Field(default_factory=list)
    template_quality_profile: TemplateQualityProfile = Field(
        default_factory=TemplateQualityProfile,
    )
    quality_floor: DesignQualityFloor = Field(default_factory=DesignQualityFloor)
    flexibility_policy: str = (
        "Preserve identity; adapt composition to meaning and functional quality."
    )

    @model_validator(mode="after")
    def validate_constraint_buckets(self) -> "PresentationDesignProfile":
        buckets = (
            (self.hard_constraints, ConstraintLevel.HARD),
            (self.medium_constraints, ConstraintLevel.MEDIUM),
            (self.soft_preferences, ConstraintLevel.SOFT),
            (self.rejected_traits, ConstraintLevel.REJECT),
        )
        for constraints, expected in buckets:
            if any(item.level != expected for item in constraints):
                raise ValueError(
                    f"constraint bucket requires level={expected.value}",
                )
        return self


class PresentationDesignContext(StrictModel):
    """Authoritative design policy shared by every v3 pipeline stage.

    The context deliberately wraps one profile instead of copying constraint
    buckets into planner/compiler/critic-specific settings.  References are
    optional profile providers; an autonomous profile uses the same contract.
    """

    schema_version: Literal["1.0"] = "1.0"
    profile: PresentationDesignProfile
    reference_input_present: bool = False
    reference_artifact_id: str | None = None
    generated_for_request: str = ""

    @model_validator(mode="after")
    def validate_reference_origin(self) -> "PresentationDesignContext":
        if self.reference_input_present and self.profile.source == "autonomous":
            raise ValueError(
                "reference input cannot silently use an autonomous profile",
            )
        if not self.reference_input_present and self.profile.source == "reference":
            raise ValueError(
                "reference-derived profile requires a declared reference input",
            )
        return self


class NativeCompositionTokens(StrictModel):
    """Resolved low-level values consumed by a native writer backend.

    Style selection and quality-floor arbitration happen before this contract
    is produced.  Backends receive only concrete execution values and never
    inspect a StyleProfile identity or constraint policy.
    """

    canvas: str = Field(pattern=r"^[0-9A-Fa-f]{6}$")
    ink: str = Field(pattern=r"^[0-9A-Fa-f]{6}$")
    muted: str = Field(pattern=r"^[0-9A-Fa-f]{6}$")
    accent: str = Field(pattern=r"^[0-9A-Fa-f]{6}$")
    subtle: str = Field(pattern=r"^[0-9A-Fa-f]{6}$")
    chart_series: list[str] = Field(default_factory=list, max_length=12)
    title_font: str = Field(min_length=1)
    body_font: str = Field(min_length=1)
    title_size: float = Field(ge=24, le=72)
    body_size: float = Field(ge=12, le=36)
    caption_size: float = Field(ge=9, le=24)


class DeckVisualBeat(StrictModel):
    slide_id: str
    role: DeckRhythmRole
    composition_family: str
    dominant: bool = False
    density: Literal["low", "medium", "high"] = "medium"


class DeckVisualPlan(StrictModel):
    """Whole-deck rhythm plan produced before native composition."""

    beats: list[DeckVisualBeat] = Field(default_factory=list)
    narrative_peaks: list[str] = Field(default_factory=list)
    repeated_motifs: list[str] = Field(default_factory=list)
    variation_notes: list[str] = Field(default_factory=list)


class SlideCompositionPlan(StrictModel):
    """Dynamic composition reasoning, not a reusable slide template."""

    relationship_type: RelationshipType
    composition_family: str
    spatial_strategy: str
    reading_path: str
    dominant_role: str
    grouping_logic: str
    whitespace_target: float = Field(default=0.5, ge=0.15, le=0.9)
    asymmetry: float = Field(default=0.5, ge=0, le=1)
    variation_key: str
    rationale: str


class ObjectFrame(StrictModel):
    """Absolute coordinates for expressive native composition on 16:9."""

    x: float = Field(ge=0, le=13.333)
    y: float = Field(ge=0, le=7.5)
    w: float = Field(gt=0, le=13.333)
    h: float = Field(ge=0, le=7.5)

    @model_validator(mode="after")
    def stay_on_canvas(self) -> "ObjectFrame":
        if self.x + self.w > 13.3335 or self.y + self.h > 7.5005:
            raise ValueError("object frame must remain within the 16:9 canvas")
        return self


class DeckPlanSlide(StrictModel):
    id: str = Field(default_factory=lambda: f"plan-slide-{uuid4().hex}")
    order: int = Field(ge=1)
    title: str
    intent: SlideIntent
    content_blocks: list[str] = Field(default_factory=list)
    takeaway: str = ""
    charts: list["ChartObject"] = Field(default_factory=list, max_length=2)
    composition_plan: SlideCompositionPlan | None = None


class DeckPlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str
    narrative_arc: str
    visual_direction: VisualDirection
    design_profile: PresentationDesignProfile | None = None
    design_context: PresentationDesignContext | None = None
    visual_plan: DeckVisualPlan | None = None
    source_coverage: list[PresentationSourceCoverage] = Field(default_factory=list)
    slides: list[DeckPlanSlide] = Field(min_length=1, max_length=60)

    @field_validator("slides")
    @classmethod
    def unique_order_and_ids(
        cls,
        slides: list[DeckPlanSlide],
    ) -> list[DeckPlanSlide]:
        if len({slide.id for slide in slides}) != len(slides):
            raise ValueError("deck plan slide ids must be unique")
        if sorted(slide.order for slide in slides) != list(
            range(1, len(slides) + 1),
        ):
            raise ValueError(
                "deck plan slide order must be contiguous and start at 1",
            )
        return slides


class NativeMetricText(StrictModel):
    """Known metric wording, retained only across an exact native import.

    This is caption provenance, not a font override. Native paragraph text
    must still match the complete authored structure before QA uses it.
    """

    value: str
    label: str
    detail: str

    def caption_paragraphs(self, paragraphs: list[str]) -> frozenset[int]:
        if not self.value.strip() or not self.label.strip() or not self.detail.strip():
            return frozenset()
        # Both native writers have an explicit order. Never infer caption
        # identity from font size, a role string, keywords, or a suffix match.
        for first, second in ((self.value, self.label), (self.label, self.value)):
            body = first.split("\n") + second.split("\n")
            caption = self.detail.split("\n")
            if paragraphs == body + caption:
                return frozenset(range(len(body), len(body) + len(caption)))
        return frozenset()


class TextObject(StrictModel):
    kind: Literal["text"] = "text"
    id: str = Field(default_factory=lambda: f"text-{uuid4().hex}")
    role: str
    text: str
    slot: str = "body"
    priority: int = Field(default=50, ge=0, le=100)
    emphasis: bool = False
    font_size: float | None = Field(default=None, ge=17, le=72)
    color: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{6}$")
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "mid", "bottom"] = "top"
    frame: ObjectFrame | None = None
    runs: list["RichTextRun"] = Field(default_factory=list)
    native_metric_text: NativeMetricText | None = None


class RichTextRun(StrictModel):
    text: str
    paragraph_break: bool = False
    bold: bool = False
    italic: bool = False
    color: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{6}$")
    font_size: float | None = Field(default=None, ge=8, le=144)


class AssetRecord(StrictModel):
    """A local-first, auditable visual asset contract.

    `upload_policy=never` is deliberate: no asset leaves the workspace unless
    a future, separately approved integration records that decision.
    """

    asset_id: str = Field(default_factory=lambda: f"asset-{uuid4().hex}")
    kind: Literal["image", "svg", "video", "font", "other"] = "image"
    local_path: str
    source_uri: str | None = None
    source_ref: str | None = None
    rights_status: Literal[
        "user_provided",
        "generated",
        "licensed",
        "public_domain",
        "review",
    ] = "review"
    license_name: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimensions: tuple[int, int] | None = None
    usage_role: Literal[
        "explanation",
        "background",
        "evidence",
        "decoration",
        "texture",
    ] = "explanation"
    focal_point: tuple[float, float] | None = None
    dedupe_key: str | None = None
    ingestion_status: Literal["ready", "failed", "recovered"] = "ready"
    failure_reason: str | None = None
    recovery_action: str | None = None
    upload_policy: Literal["never", "explicit_user_approval"] = "never"

    @field_validator("focal_point")
    @classmethod
    def validate_focal_point(
        cls,
        value: tuple[float, float] | None,
    ) -> tuple[float, float] | None:
        if value is not None and any(
            point < 0 or point > 1 for point in value
        ):
            raise ValueError("asset focal points must be normalized to 0..1")
        return value

    @model_validator(mode="after")
    def validate_provenance_contract(self) -> "AssetRecord":
        if not self.source_uri and not self.source_ref:
            raise ValueError("asset requires source_uri or source_ref")
        if self.rights_status == "licensed" and not self.license_name:
            raise ValueError("licensed asset requires license_name")
        if self.ingestion_status == "failed" and not self.failure_reason:
            raise ValueError("failed asset requires failure_reason")
        if self.ingestion_status == "recovered" and not self.recovery_action:
            raise ValueError("recovered asset requires recovery_action")
        if self.dimensions is not None and any(
            dimension <= 0 for dimension in self.dimensions
        ):
            raise ValueError("asset dimensions must be positive")
        if not self.dedupe_key:
            object.__setattr__(self, "dedupe_key", self.sha256)
        return self


class ImageObject(StrictModel):
    kind: Literal["image"] = "image"
    id: str = Field(default_factory=lambda: f"image-{uuid4().hex}")
    path: str | None = None
    asset_id: str | None = None
    alt: str
    slot: str = "hero"
    crop: Literal["cover", "contain"] = "cover"
    frame: ObjectFrame | None = None
    focal_point: tuple[float, float] | None = None

    @model_validator(mode="after")
    def require_asset_or_path(self) -> "ImageObject":
        if not self.path and not self.asset_id:
            raise ValueError("image requires asset_id or local path")
        return self


class FactRecord(StrictModel):
    fact_id: str = Field(default_factory=lambda: f"fact-{uuid4().hex}")
    text: str
    source_refs: list[str] = Field(default_factory=list)
    immutable: bool = False


class SourceFactCorrection(StrictModel):
    """Explicit caller authority, never a model-authored planning field."""

    original_fact: str = Field(min_length=1, max_length=4_000)
    corrected_fact: str = Field(min_length=1, max_length=4_000)
    source_ref: str = Field(min_length=1, max_length=400)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_section_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("original_fact", "corrected_fact", "source_ref")
    @classmethod
    def reject_blank_evidence(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source correction evidence must not be blank")
        # Exact source quotations remain byte-preserving, not normalized.
        return value


class SourceFactCorrectionRecord(StrictModel):
    """Replayable one-for-one revision, retaining exact original evidence."""

    correction: SourceFactCorrection
    fact_index: int = Field(ge=0)
    original_fact_texts: dict[str, str] = Field(min_length=1, max_length=120)
    slide_ids: list[str] = Field(min_length=1, max_length=MAX_OBJECT_EDIT_SLIDES)
    base_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corrected_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_required_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corrected_required_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ObjectRelationship(StrictModel):
    relationship_id: str = Field(
        default_factory=lambda: f"relationship-{uuid4().hex}",
    )
    relationship_type: RelationshipType
    source_object_ids: list[str] = Field(min_length=1)
    target_object_ids: list[str] = Field(min_length=1)
    label: str = ""


class ObjectGroup(StrictModel):
    group_id: str = Field(default_factory=lambda: f"group-{uuid4().hex}")
    role: str
    object_ids: list[str] = Field(min_length=1)


class MetricObject(StrictModel):
    kind: Literal["metric"] = "metric"
    id: str = Field(default_factory=lambda: f"metric-{uuid4().hex}")
    label: str
    value: str
    detail: str = ""
    slot: str = "metric_1"
    frame: ObjectFrame | None = None


class TableCellStyle(StrictModel):
    """Bounded native cell typography; absent matrix keeps authored defaults."""

    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "mid", "bottom"] = "top"
    font_size: float | None = Field(default=None, ge=8, le=144)
    fill_color: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{6}$")
    runs: list[RichTextRun] = Field(default_factory=list)
    borders: dict[Literal["lnL", "lnR", "lnT", "lnB"], str] | None = Field(default=None, max_length=4)

    @model_validator(mode="after")
    def validate_borders(self) -> "TableCellStyle":
        from .native_table_borders import parse_table_border
        for side, xml in (self.borders or {}).items():
            parse_table_border(side, xml)
        return self


class TableObject(StrictModel):
    kind: Literal["table"] = "table"
    id: str = Field(default_factory=lambda: f"table-{uuid4().hex}")
    headers: list[str] = Field(min_length=1, max_length=8)
    rows: list[list[str]] = Field(default_factory=list, max_length=20)
    cell_styles: list[list[TableCellStyle]] = Field(default_factory=list, max_length=21)
    slot: str = "table"
    frame: ObjectFrame | None = None

    @model_validator(mode="after")
    def validate_row_width(self) -> "TableObject":
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("every table row must match header width")
        if self.cell_styles and (len(self.cell_styles) != len(self.rows) + 1
                                 or any(len(row) != len(self.headers) for row in self.cell_styles)):
            raise ValueError("native table cell styles must match the complete table grid")
        return self


class ChartAxisStyle(StrictModel):
    """Explicit native scale; None bounds retain Office's automatic scale."""

    orientation: Literal["minMax", "maxMin"] = "minMax"
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    major_unit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    minor_unit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    position: Literal["l", "r", "t", "b"] | None = None
    label_position: Literal["none", "low", "high", "nextTo"] = "nextTo"
    number_format: str = Field(default="General", min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_scale(self) -> "ChartAxisStyle":
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("native chart minimum must be below its maximum")
        return self


class ChartPlotLayout(StrictModel):
    """A native plot rectangle, with absent settings left automatic."""

    layoutTarget: Literal["inner", "outer"] | None = None
    xMode: Literal["edge", "factor"] | None = None
    yMode: Literal["edge", "factor"] | None = None
    wMode: Literal["edge", "factor"] | None = None
    hMode: Literal["edge", "factor"] | None = None
    x: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    w: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    h: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)


class ChartObject(StrictModel):
    kind: Literal["chart"] = "chart"
    id: str = Field(default_factory=lambda: f"chart-{uuid4().hex}")
    chart_type: Literal["bar", "column", "line", "pie"] = "bar"
    categories: list[str] = Field(min_length=1, max_length=24)
    series: dict[str, list[float]]
    show_legend: bool | None = None
    category_axis: ChartAxisStyle | None = None
    value_axis: ChartAxisStyle | None = None
    plot_layout: ChartPlotLayout | None = None
    unit: str = ""
    number_format: str = "General"
    derivation: str = ""
    calculation_proofs: list[SourceCalculationProof] = Field(default_factory=list, max_length=120)
    source_refs: list[str] = Field(default_factory=list)
    slot: str = "chart"
    frame: ObjectFrame | None = None

    @model_validator(mode="after")
    def validate_series_lengths(self) -> "ChartObject":
        if not self.series:
            raise ValueError("chart requires at least one data series")
        if any(not isfinite(value) for values in self.series.values() for value in values):
            raise ValueError("chart data must contain only finite numbers")
        if any(
            len(values) != len(self.categories)
            for values in self.series.values()
        ):
            raise ValueError("chart series lengths must match categories")
        return self


class NativeShapeStyle(StrictModel):
    """Bounded paint/caption styles from an authoritative native save.

    Margins are points, as required by the native writer, not canvas inches.
    Absence keeps newly authored shapes on their existing style defaults.
    """

    align: Literal["left", "center", "right"] | None = None
    valign: Literal["top", "mid", "bottom"] | None = None
    margin_top: float | None = Field(default=None, ge=0, le=144, allow_inf_nan=False)
    margin_right: float | None = Field(default=None, ge=0, le=144, allow_inf_nan=False)
    margin_bottom: float | None = Field(default=None, ge=0, le=144, allow_inf_nan=False)
    margin_left: float | None = Field(default=None, ge=0, le=144, allow_inf_nan=False)
    fill_none: bool = False
    line_none: bool = False

    @model_validator(mode="after")
    def complete_point_margins(self) -> "NativeShapeStyle":
        margins = [self.margin_top, self.margin_right, self.margin_bottom, self.margin_left]
        if any(value is not None for value in margins) and any(value is None for value in margins):
            raise ValueError("native shape margins require all four point values")
        return self


class ShapeObject(StrictModel):
    """A bounded, editable visual primitive for v3 free composition."""

    kind: Literal["shape"] = "shape"
    id: str = Field(default_factory=lambda: f"shape-{uuid4().hex}")
    shape_type: Literal[
        "rect",
        "round_rect",
        "ellipse",
        "line",
        "chevron",
    ] = "rect"
    slot: str = "canvas"
    frame: ObjectFrame
    text: str = ""
    fill_color: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{6}$")
    line_color: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{6}$")
    line_width: float = Field(default=1, ge=0, le=12)
    text_color: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{6}$")
    font_size: float = Field(default=17, ge=17, le=72)
    runs: list[RichTextRun] = Field(default_factory=list)
    native_style: NativeShapeStyle | None = None
    rotate: float = Field(default=0, ge=-360, le=360)


AuthoredDeckObject = Annotated[
    TextObject
    | ImageObject
    | MetricObject
    | TableObject
    | ChartObject
    | ShapeObject,
    Field(discriminator="kind"),
]


class NativeGroupStyleContext(StrictModel):
    """Pinned inheritance for a relationship-free, imported native group."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    theme_xml: str = Field(min_length=1, max_length=524288)
    master_other_style_xml: str = Field(default="", max_length=524288)
    master_color_map_xml: str = Field(min_length=1, max_length=8192)
    presentation_default_text_style_xml: str = Field(default="", max_length=524288)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_inheritance(self) -> "NativeGroupStyleContext":
        from .native_group import validate_native_context
        validate_native_context(self)
        return self


class NativeGroupObject(StrictModel):
    """Imported immutable XML; never an authoring or AI edit target."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["native_group"] = "native_group"
    id: str = Field(min_length=1)
    slot: Literal["canvas"] = "canvas"
    frame: ObjectFrame
    native_xml: str = Field(min_length=1, max_length=524288)
    native_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_shape_id: int = Field(ge=1)
    native_name: str = Field(min_length=1)
    source_width_emu: Literal[12192000] = 12192000
    source_height_emu: Literal[6858000] = 6858000
    source_ref: str = Field(pattern=r"^genoffice-save:[0-9a-f]{64}$")
    style_context: NativeGroupStyleContext

    @model_validator(mode="after")
    def validate_preserved_group(self) -> "NativeGroupObject":
        from .native_group import validate_native_carrier
        validate_native_carrier(self)
        return self


DeckObject = Annotated[
    TextObject | ImageObject | MetricObject | TableObject | ChartObject | ShapeObject | NativeGroupObject,
    Field(discriminator="kind"),
]


class TemplateAnchorUse(StrictModel):
    """An author's explicit use of one pinned, native template shape."""

    source_shape_id: int = Field(ge=1)
    purpose: Literal["title_frame", "static_brand", "page_number"]
    target_object_id: str = Field(min_length=1)


class TemplateSlideAnchors(StrictModel):
    source_slide_number: int = Field(ge=1, le=60)
    uses: list[TemplateAnchorUse] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_anchor_sources_and_targets(self) -> "TemplateSlideAnchors":
        if len({use.source_shape_id for use in self.uses}) != len(self.uses):
            raise ValueError("template anchors must not reuse source shapes")
        if len({use.target_object_id for use in self.uses}) != len(self.uses):
            raise ValueError("template anchors must not reuse target objects")
        return self


def validate_scene_object_bindings(
    objects: list[DeckObject], anchors: TemplateSlideAnchors | None,
) -> None:
    """Keep authored and runtime scene identity constraints identical."""
    by_id = {obj.id: obj for obj in objects}
    if len(by_id) != len(objects):
        raise ValueError("object ids must be unique within a slide")
    if anchors is None:
        return
    for use in anchors.uses:
        target = by_id.get(use.target_object_id)
        if use.purpose == "title_frame" and (
            not isinstance(target, TextObject) or target.role != "title"
        ):
            raise ValueError("template title anchor requires an existing title object")
        if use.purpose in {"static_brand", "page_number"} and target is not None:
            raise ValueError("template static brand anchor requires an unused object id")


class DeckSlide(StrictModel):
    id: str = Field(default_factory=lambda: f"slide-{uuid4().hex}")
    layout_id: str
    # Runtime-owned page purpose survives promotion to explicit geometry.
    # ``freeform`` is a compiler layout, not evidence that a cover is a body page.
    semantic_layout: str | None = None
    intent_id: str | None = None
    composition_authority: CompositionAuthority = CompositionAuthority.AUTO
    objects: list[DeckObject] = Field(default_factory=list, max_length=80)
    speaker_notes: str = ""
    revision: int = Field(default=1, ge=1)
    editability_mode: EditabilityMode = EditabilityMode.NATIVE_EDITABLE
    source_fact_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    scene_hash: str | None = None
    object_groups: list[ObjectGroup] = Field(default_factory=list)
    relationships: list[ObjectRelationship] = Field(default_factory=list)
    template_anchors: TemplateSlideAnchors | None = None

    @property
    def provenance_layout(self) -> str:
        if self.layout_id != "freeform":
            return self.layout_id
        return self.semantic_layout or self.layout_id

    @field_validator("objects")
    @classmethod
    def unique_object_ids(cls, objects: list[DeckObject]) -> list[DeckObject]:
        if len({obj.id for obj in objects}) != len(objects):
            raise ValueError("object ids must be unique within a slide")
        return objects

    @model_validator(mode="after")
    def validate_author_composition(self) -> "DeckSlide":
        if self.composition_authority == CompositionAuthority.AUTHOR:
            missing = [obj.id for obj in self.objects if obj.frame is None]
            if missing:
                raise ValueError(
                    "author-owned composition requires explicit frames for "
                    f"every object: {', '.join(missing)}"
                )
        validate_scene_object_bindings(self.objects, self.template_anchors)
        return self


class TemplatePackageBinding(StrictModel):
    """Pinned native template context, separate from learned design profiles.

    The authoring layer chooses source pages explicitly. The compiler only
    executes their native layout/master relationships; it never chooses a
    style or imports source page content.
    """

    source_path: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_slide_numbers: list[Annotated[int, Field(ge=1)]] = Field(
        min_length=1, max_length=60,
    )


class DeckIR(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    id: str = Field(default_factory=lambda: f"deck-{uuid4().hex}")
    title: str
    theme_id: str = "lantai-ink"
    language: str = "zh-CN"
    aspect_ratio: Literal["16:9"] = "16:9"
    seed: int = 0
    slides: list[DeckSlide] = Field(min_length=1, max_length=60)
    source_refs: list[str] = Field(default_factory=list)
    source_coverage: list[PresentationSourceCoverage] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)
    immutable_data: list[str] = Field(default_factory=list)
    visual_direction: VisualDirection | None = None
    design_profile: PresentationDesignProfile | None = None
    design_context: PresentationDesignContext | None = None
    composition_tokens: NativeCompositionTokens | None = None
    visual_plan: DeckVisualPlan | None = None
    facts: list[FactRecord] = Field(default_factory=list)
    source_fact_corrections: list[SourceFactCorrectionRecord] = Field(
        default_factory=list, max_length=40,
    )
    assets: list[AssetRecord] = Field(default_factory=list)
    template_binding: TemplatePackageBinding | None = None

    @model_serializer(mode="wrap")
    def preserve_neutral_correction_checkpoint(self, handler) -> dict:
        payload = handler(self)
        # A new optional history field must not change old owned checkpoints,
        # including JSON reloads and the historical fingerprint reconstructions.
        # Actual corrections always retain their complete replayable provenance.
        if not self.source_fact_corrections:
            payload.pop("source_fact_corrections", None)
        return payload

    @model_validator(mode="before")
    @classmethod
    def migrate_v1(cls, value):
        """Read legacy 1.0 checkpoints without preserving their limitations."""

        if (
            isinstance(value, dict)
            and value.get("schema_version", "1.0") == "1.0"
        ):
            migrated = dict(value)
            migrated["schema_version"] = "2.0"
            migrated.setdefault("facts", [])
            migrated.setdefault("assets", [])
            normalized_slides = []
            for raw_slide in migrated.get("slides", []):
                slide = (
                    raw_slide.model_dump(mode="python")
                    if isinstance(raw_slide, BaseModel)
                    else dict(raw_slide)
                )
                normalized_slides.append(
                    {
                        **slide,
                        "revision": slide.get("revision", 1),
                        "editability_mode": slide.get(
                            "editability_mode",
                            EditabilityMode.NATIVE_EDITABLE.value,
                        ),
                        "source_fact_ids": slide.get("source_fact_ids", []),
                        "asset_ids": slide.get("asset_ids", []),
                        "scene_hash": slide.get("scene_hash"),
                        "object_groups": slide.get("object_groups", []),
                        "relationships": slide.get("relationships", []),
                    },
                )
            migrated["slides"] = normalized_slides
            return migrated
        return value

    @field_validator("slides")
    @classmethod
    def unique_slide_ids(cls, slides: list[DeckSlide]) -> list[DeckSlide]:
        if len({slide.id for slide in slides}) != len(slides):
            raise ValueError("slide ids must be unique")
        object_ids = [obj.id for slide in slides for obj in slide.objects]
        if len(set(object_ids)) != len(object_ids):
            raise ValueError("object ids must be unique across the deck")
        return slides

    @model_validator(mode="after")
    def validate_v2_references(self) -> "DeckIR":
        if self.template_binding is not None and len(
            self.template_binding.source_slide_numbers,
        ) != len(self.slides):
            raise ValueError("template binding must map every target slide")
        fact_ids = {fact.fact_id for fact in self.facts}
        asset_ids = {asset.asset_id for asset in self.assets}
        for slide in self.slides:
            object_ids = {obj.id for obj in slide.objects}
            if set(slide.source_fact_ids) - fact_ids:
                raise ValueError("slide references unknown facts")
            if set(slide.asset_ids) - asset_ids:
                raise ValueError("slide references unknown assets")
            for obj in slide.objects:
                if isinstance(obj, ImageObject) and obj.asset_id:
                    if obj.asset_id not in asset_ids:
                        raise ValueError("image references unknown asset")
            for group in slide.object_groups:
                if set(group.object_ids) - object_ids:
                    raise ValueError("object group references unknown objects")
            for relation in slide.relationships:
                referenced = set(relation.source_object_ids) | set(
                    relation.target_object_ids,
                )
                if referenced - object_ids:
                    raise ValueError(
                        "object relationship references unknown objects",
                    )
        return self


class LayoutSlot(StrictModel):
    id: str
    accepts: set[Literal["text", "image", "metric", "table", "chart", "shape", "native_group"]]
    min_items: int = Field(default=0, ge=0)
    max_items: int = Field(default=1, ge=1)
    max_chars: int | None = Field(default=None, ge=1)


class LayoutTemplate(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str
    semantic_role: str
    slots: list[LayoutSlot]
    fallback_layout_id: str | None = None
    hard_constraints: list[str] = Field(default_factory=list)
    soft_constraints: list[str] = Field(default_factory=list)


class QualityIssue(StrictModel):
    code: str
    severity: Literal["info", "warning", "error"]
    slide_id: str | None = None
    object_id: str | None = None
    message: str
    repairable: bool = False


class VisualIssue(StrictModel):
    slide_id: str
    severity: Literal["info", "warning", "error"]
    issue_type: Literal[
        "hierarchy",
        "composition",
        "alignment",
        "whitespace",
        "density",
        "typography",
        "image_quality",
        "crop",
        "chart_readability",
        "chart_semantics",
        "repetition",
        "visual_consistency",
        "narrative_transition",
        "visual_anchor",
        "color_discipline",
        "card_overuse",
        "image_distortion",
        "template_fidelity",
        "brand_constraint",
        "aesthetic_staleness",
        "content_accuracy",
        "source_coverage",
    ]
    object_ids: list[str] = Field(default_factory=list)
    description: str
    proposed_action: str


class VisualCriticSlideInput(StrictModel):
    """Evidence package shown to a semantic visual critic for one slide."""

    slide_id: str
    slide_index: int = Field(ge=1)
    rendered_path: str
    rendered_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    usage_session_id: str | None = None
    intent: SlideIntent | None = None
    key_message: str
    # Candidate native notes are not visible slide content or original evidence.
    # The default preserves compatibility without inventing historical notes.
    speaker_notes: str = ""
    factual_constraints: list[str] = Field(default_factory=list)
    # Keep the legacy combined validation context, but do not mistake every
    # deck-wide fact for an obligation to repeat it on each individual page.
    deck_factual_constraints: list[str] = Field(default_factory=list)
    page_factual_constraints: list[str] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)
    full_deck_context: list[str] = Field(default_factory=list)
    original_user_request: str = ""
    source_brief: PresentationBrief | None = None
    source_facts: list[FactRecord] = Field(default_factory=list)
    slide_objects: list[DeckObject] = Field(default_factory=list)
    object_relationships: list[ObjectRelationship] = Field(default_factory=list)
    design_profile: PresentationDesignProfile | None = None
    design_context: PresentationDesignContext | None = None
    template_mode: TemplateMode = TemplateMode.BRAND_PRESERVE_OPTIMIZE
    constraint_levels_by_object: dict[str, ConstraintLevel] = Field(
        default_factory=dict,
    )


class VisualCriticResult(StrictModel):
    critic_id: str = Field(min_length=1)
    issues: list[VisualIssue] = Field(default_factory=list)


class VisualReviewEvidence(StrictModel):
    """Auditable page/deck critic evidence bound to exact dependencies."""

    scope: Literal["slide", "deck"]
    critic_id: str = Field(min_length=1)
    dependency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_hit: bool = False
    slide_id: str | None = None
    rendered_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    duration_ms: float = Field(default=0, ge=0)
    cost_units: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_scope(self) -> "VisualReviewEvidence":
        if self.scope == "slide" and not self.slide_id:
            raise ValueError("slide review evidence requires slide_id")
        if self.scope == "deck" and self.slide_id is not None:
            raise ValueError("deck review evidence cannot bind one slide_id")
        return self


class VisualReviewCheckpoint(StrictModel):
    """Recoverable semantic-review result for one dependency fingerprint."""

    schema_version: Literal["1.0"] = "1.0"
    scope: Literal["slide", "deck"]
    dependency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    critic_cache_identity: str = Field(min_length=1)
    result: VisualCriticResult
    rendered_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class RenderedSlideEvidence(StrictModel):
    slide_id: str
    slide_index: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class QualityReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    deck_id: str
    passed: bool
    delivery_status: Literal["passed", "degraded", "failed"] = "failed"
    issues: list[QualityIssue] = Field(default_factory=list)
    content_complete: bool = True
    editable: bool = True
    output_sha256: str | None = None
    rendered_slide_count: int = 0
    render_verified: bool = False
    vision_critique_required: bool = False
    visual_verified: bool = False
    visual_critic_id: str | None = None
    visual_issues: list[VisualIssue] = Field(default_factory=list)
    visual_review_evidence: list[VisualReviewEvidence] = Field(
        default_factory=list,
    )
    renderer_id: str | None = None
    renderer_version: str | None = None
    font_manifest: list[str] = Field(default_factory=list)
    rendered_slides: list[RenderedSlideEvidence] = Field(default_factory=list)
    render_set_id: str | None = None
    rendered_glyph_check_version: str | None = None
    repair_rounds: int = Field(default=0, ge=0, le=3)
    content_evidence: EvidenceState = EvidenceState.NOT_RUN
    structure_evidence: EvidenceState = EvidenceState.NOT_RUN
    render_evidence: EvidenceState = EvidenceState.NOT_RUN
    visual_evidence: EvidenceState = EvidenceState.NOT_RUN
    editability_evidence: EvidenceState = EvidenceState.NOT_RUN
    provenance_evidence: EvidenceState = EvidenceState.NOT_RUN
    blocking_gaps: list[str] = Field(default_factory=list)


class PresentationVersion(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    version_id: str = Field(default_factory=lambda: f"version-{uuid4().hex}")
    parent_version_id: str | None = None
    pptx_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deck_ir_version: str
    writer: Literal[
        "native_pptx",
        "pptxgenjs_v3",
        "legacy_python_pptx",
        "lantai_ai_edit",
        "genoffice_human",
        "office_cli",
        "external_reconcile",
        "version_rollback",
    ]
    reconciled: bool = True
    generation: int = Field(default=0, ge=0)
    base_pptx_sha256: str | None = None
    pptx_snapshot: str | None = None
    deck_ir_snapshot: str | None = None
    quality_snapshot: str | None = None
    render_snapshot_dir: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class PresentationJobEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: str
    sequence: int = Field(ge=1)
    stage: JobStage
    status: JobStatus
    slide_id: str | None = None
    message: str
    checkpoint: str | None = None
    recoverable: bool = False
    failure_routes: list[PresentationFailureRoute] = Field(default_factory=list)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class PresentationStageMetric(StrictModel):
    """One append-only timing/cost observation for a presentation attempt."""

    schema_version: Literal["1.0", "1.1"] = "1.1"
    metric_id: str = Field(default_factory=lambda: f"metric-{uuid4().hex}")
    stage: JobStage
    operation: Literal[
        "brief",
        "design_context",
        "plan",
        "author",
        "budget_reservation",
        "compile",
        "render",
        "inspect",
        "critic",
        "repair",
        "promotion",
    ]
    repair_round: int = Field(default=0, ge=0)
    slide_id: str | None = None
    route_id: str | None = None
    outcome: Literal[
        "passed",
        "failed",
        "cancelled",
        "unavailable",
    ]
    duration_ms: float = Field(ge=0)
    cost_units: int = Field(default=0, ge=0)
    provider_cost_status: PresentationProviderCostStatus = (
        PresentationProviderCostStatus.NOT_APPLICABLE
    )
    provider_cost_micro_cny: int | None = Field(default=None, ge=0)
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_provider_cost_evidence(self) -> "PresentationStageMetric":
        status = self.provider_cost_status
        amount = self.provider_cost_micro_cny
        if status is PresentationProviderCostStatus.EXACT:
            if amount is None:
                raise ValueError(
                    "exact provider cost requires provider_cost_micro_cny",
                )
            if not self.route_id:
                raise ValueError("provider cost evidence requires route_id")
        elif amount is not None:
            raise ValueError(
                f"{status.value} provider cost cannot have an amount",
            )
        elif (
            status is PresentationProviderCostStatus.UNKNOWN
            and not self.route_id
        ):
            raise ValueError("provider cost evidence requires route_id")
        return self


class PresentationEditOperation(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    operation_id: str = Field(default_factory=lambda: f"edit-{uuid4().hex}")
    deck_id: str
    slide_ids: list[str] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)
    allowed_content_types: set[
        Literal["text", "image", "metric", "table", "chart", "shape", "speaker_notes"]
    ]
    instruction: str
    requires_preview: bool = True
    snapshot_id: str | None = None

    @model_validator(mode="after")
    def require_explicit_scope(self) -> "PresentationEditOperation":
        if not self.slide_ids and not self.object_ids:
            raise ValueError(
                "presentation edits require an explicit slide or object scope",
            )
        return self


class TextReplacement(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["replace_text"] = "replace_text"
    object_id: str
    text: str


class ReplaceTextAction(StrictModel):
    kind: Literal["replace_text"] = "replace_text"
    object_id: str
    text: str


class SetShapeTextAction(StrictModel):
    """Change an existing compatible native shape's label, not its style."""

    kind: Literal["set_shape_text"] = "set_shape_text"
    object_id: str
    text: str


class UpdateTableAction(StrictModel):
    """Change an existing native table's text, never its grid or style."""

    kind: Literal["update_table"] = "update_table"
    object_id: str
    headers: list[str] = Field(min_length=1, max_length=8)
    rows: list[list[str]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_row_width(self) -> "UpdateTableAction":
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("every table row must match header width")
        return self


class UpdateChartAction(StrictModel):
    kind: Literal["update_chart"] = "update_chart"
    object_id: str
    chart_type: Literal["bar", "column", "line", "pie"] | None = None
    categories: list[str] = Field(min_length=1, max_length=24)
    series: dict[str, list[float]]
    show_legend: bool | None = None
    unit: str | None = None
    number_format: str | None = None
    derivation: str | None = None
    source_refs: list[str] | None = None

    @model_validator(mode="after")
    def validate_chart(self) -> "UpdateChartAction":
        if any(
            len(values) != len(self.categories)
            for values in self.series.values()
        ):
            raise ValueError("chart series lengths must match categories")
        return self


class ReplaceImageAction(StrictModel):
    kind: Literal["replace_image"] = "replace_image"
    object_id: str
    asset_id: str
    alt: str | None = None
    crop: Literal["cover", "contain"] | None = None
    focal_point: tuple[float, float] | None = None


class MoveResizeAction(StrictModel):
    kind: Literal["move_resize"] = "move_resize"
    object_id: str
    frame: ObjectFrame


class AddObjectAction(StrictModel):
    kind: Literal["add_object"] = "add_object"
    object: AuthoredDeckObject


class RemoveObjectAction(StrictModel):
    kind: Literal["remove_object"] = "remove_object"
    object_id: str


ObjectEditAction = Annotated[
    ReplaceTextAction
    | SetShapeTextAction
    | UpdateTableAction
    | UpdateChartAction
    | ReplaceImageAction
    | MoveResizeAction
    | AddObjectAction
    | RemoveObjectAction,
    Field(discriminator="kind"),
]


class SlideIntentPatch(StrictModel):
    """Auditable correction to the authored claim behind one native slide.

    Repair the page's main judgment without replacing source evidence,
    immutable facts or the visual scene. New derivations may only append
    existing source IDs and source-bound calculation proofs; Runtime checks
    them against the original brief before the whole-deck semantic review.
    """

    # Omit for the object-edit target.  A controlled repair may name another
    # slide so a dependent geometry fix and its already-known intent
    # correction can pass one all-or-nothing whole-deck review.
    slide_id: str | None = None
    key_message: str = Field(min_length=1, max_length=4_000)
    source_ref_additions: list[str] = Field(default_factory=list, max_length=24)
    calculation_proof_additions: list[SourceCalculationProof] = Field(
        default_factory=list, max_length=120,
    )


class SpeakerNotesOutputContract(StrictModel):
    """Caller-supplied lexical limits; never inferred from free-form intent.

    Length counts only basic Han codepoints (U+4E00..U+9FFF) in changed
    note values. Existing unchanged notes and JSON identifiers are exempt.
    The ordinary numeric-free contract remains independently required.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    min_han_characters: int | None = Field(default=None, ge=0, le=6000, strict=True)
    max_han_characters: int | None = Field(default=None, ge=0, le=6000, strict=True)
    forbidden_characters: list[Annotated[str, Field(min_length=1, max_length=1, strict=True)]] = Field(
        default_factory=list, max_length=128,
    )

    @field_validator("forbidden_characters")
    @classmethod
    def require_unique_printable_characters(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not item.isprintable() for item in value):
            raise ValueError("forbidden characters must be unique printable codepoints")
        return value

    @model_validator(mode="after")
    def require_consistent_bounds(self) -> "SpeakerNotesOutputContract":
        if (self.min_han_characters is not None and self.max_han_characters is not None
                and self.min_han_characters > self.max_han_characters):
            raise ValueError("minimum note length must not exceed maximum note length")
        return self


class SlideSpeakerNotesPatch(StrictModel):
    """An explicit page-level note replacement, never a visible object action."""

    slide_id: str | None = None
    speaker_notes: str = Field(max_length=6000)


class PresentationObjectEditPlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    slide_id: str
    additional_slide_ids: list[str] = Field(
        default_factory=list, max_length=MAX_OBJECT_EDIT_SLIDES - 1,
    )
    actions: list[ObjectEditAction] = Field(default_factory=list, max_length=40)
    intent_patch: SlideIntentPatch | None = None
    intent_patches: list[SlideIntentPatch] = Field(default_factory=list, max_length=10)
    speaker_notes_patches: list[SlideSpeakerNotesPatch] = Field(
        default_factory=list, max_length=MAX_OBJECT_EDIT_SLIDES,
    )
    rationale: str = ""

    @model_validator(mode="after")
    def require_material_edit(self) -> "PresentationObjectEditPlan":
        if not self.actions and self.intent_patch is None and not self.intent_patches and not self.speaker_notes_patches:
            raise ValueError(
                "presentation object edit plan requires an object action or intent patch or notes patch",
            )
        if self.slide_id in self.additional_slide_ids:
            raise ValueError("primary slide cannot also be an additional slide")
        if len(set(self.additional_slide_ids)) != len(self.additional_slide_ids):
            raise ValueError("additional slide ids must be unique")
        if self.intent_patch is not None and self.intent_patches:
            raise ValueError("cannot combine intent_patch and intent_patches")
        targets = [patch.slide_id if patch.slide_id is not None else self.slide_id
                   for patch in self.resolved_intent_patches()]
        if any(not target for target in targets):
            raise ValueError("intent patch slide id cannot be empty")
        if len(set(targets)) != len(targets):
            raise ValueError("intent patch slide ids must be unique")
        if self.speaker_notes_patches and (self.actions or self.intent_patch is not None or self.intent_patches):
            raise ValueError("notes-only edits cannot combine object actions or intent patches")
        note_targets = [patch.slide_id for patch in self.resolved_speaker_notes_patches()]
        if any(not target for target in note_targets):
            raise ValueError("speaker notes patch slide id cannot be empty")
        if len(set(note_targets)) != len(note_targets):
            raise ValueError("speaker notes patch slide ids must be unique")
        return self

    def resolved_intent_patches(self) -> list[SlideIntentPatch]:
        """One bounded representation; omitted targets mean the primary page."""
        patches = [self.intent_patch] if self.intent_patch is not None else self.intent_patches
        return [patch.model_copy(update={
            "slide_id": self.slide_id if patch.slide_id is None else patch.slide_id,
        }) for patch in patches]

    def resolved_speaker_notes_patches(self) -> list[SlideSpeakerNotesPatch]:
        return [patch.model_copy(update={
            "slide_id": self.slide_id if patch.slide_id is None else patch.slide_id,
        }) for patch in self.speaker_notes_patches]


class PresentationEditPreview(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    preview_id: str = Field(default_factory=lambda: f"preview-{uuid4().hex}")
    operation: PresentationEditOperation
    source_path: str
    candidate_path: str
    snapshot_path: str
    source_sha256: str
    changed_object_ids: list[str]
    change_summary: list[str]
    verified: bool = False


class PresentationEditReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    operation_id: str
    source_path: str
    snapshot_path: str
    committed_sha256: str
    changed_object_ids: list[str]
    verified: bool = False


# Storyboards reference the native chart schema declared below their own
# definition. Resolve it explicitly before structured model schemas are used.
DeckPlanSlide.model_rebuild()
DeckPlan.model_rebuild()
