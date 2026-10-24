# -*- coding: utf-8 -*-
"""Merge memory channels into the latest answer's auditable usage trace."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .persona.perceptible_presence import (
    PersonaDeliveryReport,
    PersonaMemoryReference,
    PersonaTurnPlan,
)

_TRACE_LOCK = threading.Lock()


def _normalized_query(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _same_turn_query(stored: str, current: str) -> bool:
    """Match the recall seam's bounded query to the user message."""

    left = _normalized_query(stored)
    right = _normalized_query(current)
    if not left or not right:
        return False
    return left == right or left == right[: len(left)] or right == left[: len(right)]


def _read_trace(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def merge_last_recall_usage(
    working_dir: str | Path,
    *,
    query: str,
    references: Iterable[PersonaMemoryReference],
) -> bool:
    """Append actually delivered user-memory evidence to the latest trace.

    A query guard prevents a late response from contaminating a newer turn's
    trace. Existing canonical recall items are retained and citations are
    deduplicated across channels.
    """

    incoming = tuple(references)
    if not incoming or not str(query or "").strip():
        return False
    path = Path(working_dir) / "memory-ledger" / "last-recall.json"
    with _TRACE_LOCK:
        payload = _read_trace(path)
        stored_query = str(payload.get("query") or "")
        if stored_query and not _same_turn_query(stored_query, query):
            return False

        existing = [
            dict(item)
            for item in payload.get("items", [])
            if isinstance(item, dict)
        ]
        citation_indexes = {
            str(item.get("citation") or ""): index
            for index, item in enumerate(existing)
            if str(item.get("citation") or "")
        }
        changed = False
        for reference in incoming:
            item = reference.to_dict()
            citation = item["citation"]
            duplicate_index = citation_indexes.get(citation)
            if duplicate_index is None:
                citation_indexes[citation] = len(existing)
                existing.append(item)
                changed = True
                continue
            previous = existing[duplicate_index]
            if float(item["score"]) > float(previous.get("score") or 0.0):
                existing[duplicate_index] = item
                changed = True

        if not changed:
            return False
        _write_atomic(
            path,
            {
                **payload,
                "query": stored_query or str(query).strip()[:240],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "items": existing,
            },
        )
    return True


def record_delivered_persona_memory(
    working_dir: str | Path,
    *,
    query: str,
    plan: PersonaTurnPlan,
    report: PersonaDeliveryReport,
) -> bool:
    """Record only a relationship memory verified in the visible answer."""

    if not (
        report.delivered
        and report.signal == "grounded_memory_echo"
        and plan.beat == "relationship_echo"
    ):
        return False
    return merge_last_recall_usage(
        working_dir,
        query=query,
        references=plan.memory_references,
    )

