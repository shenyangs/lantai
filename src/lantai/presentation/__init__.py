"""Deterministic, editable presentation generation for Lantai."""

from .compiler import CompilationResult, PresentationCompiler
from .jobs import PresentationJobStore
from .editing import PresentationEditTransactionService
from .models import (
    DeckIR,
    DeckPlan,
    LayoutTemplate,
    PresentationBrief,
    PresentationEditOperation,
    PresentationEditPreview,
    PresentationEditReceipt,
    PresentationJobEvent,
    PresentationStageMetric,
    PresentationJobRequest,
    PresentationSourceSection,
    QualityReport,
    SlideIntent,
    TextReplacement,
)

__all__ = [
    "CompilationResult",
    "DeckIR",
    "DeckPlan",
    "LayoutTemplate",
    "PresentationBrief",
    "PresentationCompiler",
    "PresentationEditOperation",
    "PresentationEditPreview",
    "PresentationEditReceipt",
    "PresentationEditTransactionService",
    "PresentationJobEvent",
    "PresentationStageMetric",
    "PresentationJobRequest",
    "PresentationSourceSection",
    "PresentationJobStore",
    "QualityReport",
    "SlideIntent",
    "TextReplacement",
]
