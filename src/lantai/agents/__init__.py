# -*- coding: utf-8 -*-
"""Lantai Agents Module.

This module provides the main agent implementation and supporting utilities
for building AI agents with tools, skills, and memory management.

Public API:
- LegacyLantaiAgent: Main agent class
- create_model_and_formatter: Factory for creating models and formatters

Example:
    >>> from lantai.agents import LegacyLantaiAgent, create_model_and_formatter
    >>> agent = LegacyLantaiAgent()
    >>> # Or with custom model
    >>> model, formatter = create_model_and_formatter()
"""

# LegacyLantaiAgent is lazy-loaded so that importing agents.skills_manager
# (e.g. from CLI init_cmd/skills_cmd) does not pull heavy runtime tools.
# pylint: disable=undefined-all-variable
__all__ = ["LegacyLantaiAgent", "create_model_and_formatter"]


def __getattr__(name: str):
    """Lazy load heavy imports."""
    if name == "LegacyLantaiAgent":
        from .legacy_lantai_agent import LegacyLantaiAgent

        return LegacyLantaiAgent
    if name == "create_model_and_formatter":
        from .model_factory import create_model_and_formatter

        return create_model_and_formatter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
