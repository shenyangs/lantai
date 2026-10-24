# -*- coding: utf-8 -*-
from __future__ import annotations

from hashlib import sha256
import re


_UNSAFE_PROFILE_CHARS = re.compile(r"[^a-z0-9_-]+")
_WHITESPACE = re.compile(r"\s+")
_HYPHENS = re.compile(r"-+")


def canonicalize_profile_id(profile_id: str | None = None) -> str:
    raw = str(profile_id or "default").strip().lower()
    if not raw:
        return "default"

    slug = _UNSAFE_PROFILE_CHARS.sub("-", raw)
    slug = _HYPHENS.sub("-", slug).strip("-")
    whitespace_normalized = _WHITESPACE.sub("-", raw).strip("-")
    lossy = slug != whitespace_normalized
    if not slug:
        slug = "profile"
        lossy = True
    if lossy:
        digest = sha256(raw.encode("utf-8")).hexdigest()[:12]
        slug = f"{slug}-{digest}"
    return slug
