# -*- coding: utf-8 -*-
"""Public API for optional presentation reference learning."""

from .analyzer import ReferenceDeckAnalyzer
from .models import (
    ConstraintClassification,
    ExtractedDesignGrammar,
    ReferenceDeckMeasurement,
    ReferenceLearningResult,
    ReferenceObjectMeasurement,
    ReferenceSlideMeasurement,
    ReferenceVisualMeasurement,
    VisualSlideMeasurement,
)
from .object_parser import PptxObjectParser
from .profile_builder import ReferenceProfileBuilder
from .quality_arbitration import TemplateQualityArbitrator
from .style_extractor import ReferenceStyleExtractor
from .visual_analyzer import RenderedSlideAnalyzer

__all__ = [
    "ConstraintClassification",
    "ExtractedDesignGrammar",
    "PptxObjectParser",
    "ReferenceDeckAnalyzer",
    "ReferenceDeckMeasurement",
    "ReferenceLearningResult",
    "ReferenceObjectMeasurement",
    "ReferenceProfileBuilder",
    "ReferenceSlideMeasurement",
    "ReferenceStyleExtractor",
    "ReferenceVisualMeasurement",
    "RenderedSlideAnalyzer",
    "TemplateQualityArbitrator",
    "VisualSlideMeasurement",
]
