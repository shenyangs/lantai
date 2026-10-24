# -*- coding: utf-8 -*-
"""Language support levels and canonical prompt fallbacks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentLanguageSupport:
    requested: str
    locale: str
    level: str
    canonical: str
    reason: str


def agent_language_support(language: str) -> AgentLanguageSupport:
    """Return explicit support instead of silently mixing partial prompts."""
    requested = str(language or "").strip().lower().replace("_", "-")
    locale = requested.split("-", 1)[0]
    if locale == "zh":
        return AgentLanguageSupport(requested, "zh", "full", "zh", "complete")
    if locale == "en":
        return AgentLanguageSupport(requested, "en", "full", "en", "complete")
    if locale == "ru":
        return AgentLanguageSupport(
            requested,
            "ru",
            "partial",
            "en",
            "Russian workspace copy exists, but the complete protected canon is English",
        )
    return AgentLanguageSupport(
        requested,
        locale or "unknown",
        "unsupported",
        "en",
        "unsupported locales use the complete English canon",
    )


def canonical_prompt_language(language: str) -> str:
    """Select the complete canonical language for model-facing contracts."""
    return agent_language_support(language).canonical
