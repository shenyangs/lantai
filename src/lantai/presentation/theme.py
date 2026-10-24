"""Original Lantai presentation themes; no third-party template assets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemeTokens:
    id: str
    name: str
    background: str
    surface: str
    primary: str
    accent: str
    text: str
    muted: str
    title_font: str
    body_font: str


THEMES: dict[str, ThemeTokens] = {
    "lantai-ink": ThemeTokens(
        id="lantai-ink",
        name="澜台·墨蓝",
        background="F7F8FA",
        surface="FFFFFF",
        primary="203A5F",
        accent="D7A64A",
        text="172033",
        muted="667085",
        title_font="Noto Sans SC",
        body_font="Noto Sans SC",
    ),
    "lantai-slate": ThemeTokens(
        id="lantai-slate",
        name="澜台·岩灰",
        background="111827",
        surface="1F2937",
        primary="E5E7EB",
        accent="67E8F9",
        text="F9FAFB",
        muted="9CA3AF",
        title_font="Noto Sans SC",
        body_font="Noto Sans SC",
    ),
    "lantai-warm": ThemeTokens(
        id="lantai-warm",
        name="澜台·纸页",
        background="F5F0E8",
        surface="FFFDF8",
        primary="6B3E2E",
        accent="C85C3A",
        text="2F2A26",
        muted="7D726A",
        title_font="Noto Sans SC",
        body_font="Noto Sans SC",
    ),
}

THEME_ALIASES: dict[str, str] = {
    "business-blue": "lantai-ink",
    "corporate-blue": "lantai-ink",
    "corporate": "lantai-ink",
    "ink": "lantai-ink",
    "slate": "lantai-slate",
    "warm": "lantai-warm",
}


def normalize_theme_id(theme_id: str | None) -> str:
    """Resolve model-authored theme labels to a supported native theme.

    Theme is an optional visual hint. An invented or stale label must not abort
    an otherwise valid presentation request, so unknown labels use the stable
    default while well-known descriptive aliases retain their intended style.
    """

    normalized = str(theme_id or "").strip().lower().replace("_", "-")
    if normalized in THEMES:
        return normalized
    return THEME_ALIASES.get(normalized, "lantai-ink")


def get_theme(theme_id: str) -> ThemeTokens:
    try:
        return THEMES[theme_id]
    except KeyError as exc:
        raise ValueError(f"unknown presentation theme: {theme_id}") from exc
