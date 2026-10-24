# -*- coding: utf-8 -*-
"""Deprecated import shim for pre-migration presentation integrations.

New code imports :mod:`lantai.presentation.pptx_compiler`. The compatibility
symbols remain so older task checkpoints and external extensions can start,
without making the backend library name part of the public architecture.
"""

from .pptx_compiler import (
    NativePptxCompiler,
    NativePptxCompilerUnavailable,
)

PptxGenJSCompiler = NativePptxCompiler
PptxGenJSUnavailable = NativePptxCompilerUnavailable

__all__ = ["PptxGenJSCompiler", "PptxGenJSUnavailable"]
