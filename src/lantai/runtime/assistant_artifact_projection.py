# -*- coding: utf-8 -*-
"""Project trusted tool artifact references into the terminal assistant reply."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from .events import RuntimeEvent
from .messages import RuntimeMessage
from .tools import ToolResult, ToolSpec

_PROJECTION_MARKER = "analysis_chart_reference"
_ANALYSIS_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_ANALYSIS_CHART_FENCE_RE = re.compile(
    r"```analysis-chart[ \t]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)
_REFERENCE_KEYS = frozenset({"analysis_id", "x_column", "y_column", "type"})
_MAX_REFERENCES = 8


def _validated_chart_reference(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != _REFERENCE_KEYS:
        return None
    if any(not isinstance(value.get(key), str) for key in _REFERENCE_KEYS):
        return None
    analysis_id = value["analysis_id"]
    x_column = value["x_column"]
    y_column = value["y_column"]
    chart_type = value["type"]
    if _ANALYSIS_ID_RE.fullmatch(analysis_id) is None:
        return None
    if (
        not x_column
        or not y_column
        or x_column != x_column.strip()
        or y_column != y_column.strip()
        or len(x_column) > 120
        or len(y_column) > 120
        or x_column == y_column
        or chart_type != "bar"
    ):
        return None
    return {
        "analysis_id": analysis_id,
        "x_column": x_column,
        "y_column": y_column,
        "type": chart_type,
    }


def _reference_identity(reference: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        reference["analysis_id"],
        reference["x_column"],
        reference["y_column"],
        reference["type"],
    )


def _references_in_text(text: str) -> set[tuple[str, str, str, str]]:
    references: set[tuple[str, str, str, str]] = set()
    for match in _ANALYSIS_CHART_FENCE_RE.finditer(text):
        try:
            candidate = json.loads(match.group("body"))
        except (TypeError, ValueError):
            continue
        reference = _validated_chart_reference(candidate)
        if reference is not None:
            references.add(_reference_identity(reference))
    return references


def _canonical_fence(reference: dict[str, str]) -> str:
    payload = json.dumps(
        reference,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"```analysis-chart\n{payload}\n```"


class AssistantArtifactProjectionAccumulator:
    """Collect bounded verified references and expose them only at completion."""

    def __init__(self) -> None:
        self._references: list[dict[str, str]] = []
        self._identities: set[tuple[str, str, str, str]] = set()
        self._emitted_identities: set[tuple[str, str, str, str]] = set()

    def observe(self, spec: ToolSpec | None, result: ToolResult) -> None:
        """Observe one settled result without trusting its prose or markdown."""

        if (
            spec is None
            or not result.ok
            or spec.origin != "builtin"
            or spec.name != result.name
            or spec.metadata.get("assistant_artifact_projection")
            != _PROJECTION_MARKER
            or len(self._references) >= _MAX_REFERENCES
        ):
            return
        reference = _validated_chart_reference(
            result.metadata.get("chat_chart_reference"),
        )
        if reference is None:
            return
        identity = _reference_identity(reference)
        if identity in self._identities:
            return
        self._identities.add(identity)
        self._references.append(reference)

    def project(self, event: RuntimeEvent) -> RuntimeEvent:
        """Append missing references to one terminal assistant completion."""

        if (
            not self.has_pending()
            or event.type != "message.completed"
            or event.status != "completed"
            or event.source != "assistant"
        ):
            return event
        raw_message = event.payload.get("message")
        if not isinstance(raw_message, dict) or raw_message.get("role") != "assistant":
            return event
        raw_content = raw_message.get("content")
        if not isinstance(raw_content, list):
            return event
        visible_text = "\n".join(
            str(block.get("text") or "")
            for block in raw_content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        )
        existing = _references_in_text(visible_text)
        observed_existing = existing & self._identities
        additions = [
            reference
            for reference in self._references
            if _reference_identity(reference) not in existing
            and _reference_identity(reference) not in self._emitted_identities
        ]
        self._emitted_identities.update(observed_existing)
        if not additions:
            return event
        suffix = "\n\n".join(_canonical_fence(reference) for reference in additions)
        separator = "\n\n" if visible_text else ""
        updated_text = f"{visible_text}{separator}{suffix}"
        content = [
            dict(block) if isinstance(block, dict) else block
            for block in raw_content
        ]
        text_indexes = [
            index
            for index, block in enumerate(content)
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if text_indexes:
            last_text_index = text_indexes[-1]
            prior_text = str(content[last_text_index].get("text") or "")
            text_separator = "\n\n" if prior_text else ""
            content[last_text_index]["text"] = (
                f"{prior_text}{text_separator}{suffix}"
            )
        else:
            content.append(
                {"type": "text", "text": suffix, "data": {}, "metadata": {}},
            )
        message = dict(raw_message)
        message["content"] = content
        payload: dict[str, Any] = dict(event.payload)
        payload["message"] = message
        self._emitted_identities.update(
            _reference_identity(reference) for reference in additions
        )
        return event.model_copy(
            update={"summary": updated_text, "payload": payload},
        )

    def has_pending(self) -> bool:
        """Return whether verified references still need a public projection."""

        return any(
            _reference_identity(reference) not in self._emitted_identities
            for reference in self._references
        )

    def pending_completion(
        self,
        *,
        seq: int,
        session_id: str | None,
        turn_id: str | None,
    ) -> RuntimeEvent | None:
        """Build one prose-free terminal completion for remaining references."""

        pending = [
            reference
            for reference in self._references
            if _reference_identity(reference) not in self._emitted_identities
        ]
        if not pending:
            return None
        text = "\n\n".join(_canonical_fence(reference) for reference in pending)
        message_id = str(uuid4())
        message = RuntimeMessage.text(
            "assistant",
            text,
            id=message_id,
            metadata={
                "assistant_artifact_projection": _PROJECTION_MARKER,
            },
        )
        self._emitted_identities.update(
            _reference_identity(reference) for reference in pending
        )
        return RuntimeEvent(
            id=str(uuid4()),
            seq=seq,
            type="message.completed",
            status="completed",
            source="assistant",
            session_id=session_id,
            turn_id=turn_id,
            item_id=message_id,
            summary=text,
            payload={"message": message.model_dump(mode="json")},
        )


__all__ = ["AssistantArtifactProjectionAccumulator"]
