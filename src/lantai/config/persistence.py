"""Persist configuration without exposing a truncated authoritative file."""

import json
from pathlib import Path
from typing import Any

from ..utils.authoritative_store import atomic_write_bytes


def atomic_write_config_json(path: Path, payload: dict[str, Any]) -> None:
    """Preserve JSON formatting and publish only a complete private file."""
    value = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, value)
