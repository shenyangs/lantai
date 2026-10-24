from __future__ import annotations

import re
import unicodedata
from pathlib import Path


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*]+')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def presentation_filename(
    title: str | None,
    *,
    fallback: str = "澜台演示文稿",
    max_stem_length: int = 80,
) -> str:
    """Convert a deck title into a portable, human-readable PPTX filename."""

    value = unicodedata.normalize("NFKC", title or "")
    value = _CONTROL_CHARACTERS.sub("", value)
    value = _INVALID_FILENAME_CHARACTERS.sub("-", value)
    value = _WHITESPACE.sub(" ", value).strip(" .-_")
    if value.casefold().endswith(".pptx"):
        value = value[:-5].rstrip(" .-_")
    if not value:
        value = fallback
    value = value[:max_stem_length].rstrip(" .-_") or fallback
    if value.upper() in _WINDOWS_RESERVED_STEMS:
        value = f"{fallback}-{value}"
    return f"{value}.pptx"


def unique_presentation_path(directory: Path, title: str | None) -> Path:
    """Choose a collision-safe destination without overwriting an earlier deck."""

    desired = directory / presentation_filename(title)
    if not desired.exists():
        return desired
    for index in range(2, 10_000):
        candidate = desired.with_name(f"{desired.stem} ({index}){desired.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为演示文稿选择唯一文件名: {desired}")
