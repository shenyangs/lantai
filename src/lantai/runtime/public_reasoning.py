# -*- coding: utf-8 -*-
"""Provider-visible reasoning and final-answer projection.

Lantai treats provider-visible thinking as a display surface, not as a promise
to expose a model's private raw chain of thought.  Runtime preserves the
provider's visible reasoning summary and event order while narrowly removing
malformed transport placeholders and exact prompt/secret fingerprints.  It
must never replace real reasoning with a fixed status or an invented summary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Any

from .events import RuntimeEvent
from .input import RuntimeInput
from .messages import RuntimeMessage
from .prompt_leak_guard import PromptLeakGuard


SUBAGENT_INTERNAL_ACTIVITY_STATUS = "正在处理当前任务。"
_VALID_QUQU_LINE_RE = re.compile(r"^\s*~>\[(?:lead|echo)\]\s+\S")
_VALID_QUQU_TAGS = ("~>[lead]", "~>[echo]")
_PLACEHOLDER_TOKEN_RE = re.compile(r"\bundefined\b", re.IGNORECASE)
_PLACEHOLDER_ONLY_LINE_RE = re.compile(
    r"^\s*(?:undefined(?:\s+undefined)*)\s*$",
    re.IGNORECASE,
)
_PLACEHOLDER_SECTION_LINE_RE = re.compile(
    r"^\s*#{1,6}\s*(?:task\s*:\s*undefined|skill\s*:\s*\S+|\u5f53\u524d\u72b6\u6001)\s*$",
    re.IGNORECASE,
)
_PUBLIC_TEXT_BLOCK_TYPES = {"text", "thinking", "reasoning"}
_PUBLIC_PROGRESS_AUTHORITY_KEYS = {
    "_lantai_runtime_public_progress_attestation",
    "public_progress",
    "public_projection",
    "public_progress_id",
    "public_progress_revision",
    "private_reasoning_exposed",
    "runtime_presentation_only",
}
_PUBLIC_PROGRESS_ATTESTATION_KEY = secrets.token_bytes(32)
RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD = (
    "_lantai_runtime_public_progress_attestation"
)


def is_runtime_public_progress(value: Any) -> bool:
    """Return whether metadata is an explicit Runtime-authored progress fact.

    Merely carrying an ``id`` is not an authority boundary: provider metadata
    and older envelopes can contain arbitrary dictionaries.  Every projection
    surface must validate the version and provenance before it labels model
    text as safe compact persona copy.
    """

    revision = value.get("revision") if isinstance(value, dict) else None
    tool_call_ids = value.get("tool_call_ids") if isinstance(value, dict) else None
    return bool(
        isinstance(value, dict)
        and value.get("schema_version") == 2
        and isinstance(value.get("id"), str)
        and str(value.get("id") or "").strip()
        and value.get("source") == "runtime_facts"
        and value.get("text_origin") == "assistant_public_text"
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision > 0
        and (
            tool_call_ids is None
            or (
                isinstance(tool_call_ids, list)
                and bool(tool_call_ids)
                and all(
                    isinstance(call_id, str) and bool(call_id.strip())
                    for call_id in tool_call_ids
                )
            )
        )
    )


def without_runtime_public_progress_claims(value: Any) -> Any:
    """Recursively remove unverified public-progress authority metadata."""

    if isinstance(value, dict):
        return {
            key: without_runtime_public_progress_claims(item)
            for key, item in value.items()
            if key not in _PUBLIC_PROGRESS_AUTHORITY_KEYS
        }
    if isinstance(value, list):
        return [without_runtime_public_progress_claims(item) for item in value]
    return value


def _runtime_public_progress_signature(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        _PUBLIC_PROGRESS_ATTESTATION_KEY,
        canonical,
        hashlib.sha256,
    ).hexdigest()


def attest_runtime_public_progress(
    value: Any,
    *,
    run_id: str,
    wire_message_id: str,
    message_type: str,
    text: str,
) -> dict[str, Any] | None:
    """Seal one Runtime-authored public tool preamble for the SSE bridge.

    The legacy Console stream is also a provider boundary.  A plain metadata
    marker is therefore insufficient: an adapter could mint the same keys.
    This short-lived, process-local seal lets the converter attest a trusted
    RuntimeEvent and lets the Console boundary reject lookalike provider data.
    """

    if not is_runtime_public_progress(value):
        return None
    normalized_type = str(message_type or "").strip().lower()
    public_text = str(text or "")
    payload = {
        "progress": dict(value),
        "run_id": str(run_id or "").strip(),
        "wire_message_id": str(wire_message_id or "").strip(),
        "message_type": normalized_type,
        "content_sha256": hashlib.sha256(public_text.encode("utf-8")).hexdigest(),
    }
    if (
        not payload["run_id"]
        or not payload["wire_message_id"]
        or normalized_type not in {"message", "reasoning"}
        or not public_text.strip()
    ):
        return None
    return {
        **payload,
        "signature": _runtime_public_progress_signature(payload),
    }


def verify_runtime_public_progress_attestation(
    value: Any,
    *,
    run_id: str,
    wire_message_id: str,
    message_type: str,
    text: str,
) -> dict[str, Any] | None:
    """Return sealed progress only when its exact public wire frame matches.

    Binding the visible text, semantic message type and final wire id prevents
    a valid Runtime seal from being replayed onto another same-run provider
    frame with different content.
    """

    if not isinstance(value, dict):
        return None
    payload = {
        "progress": value.get("progress"),
        "run_id": str(value.get("run_id") or "").strip(),
        "wire_message_id": str(value.get("wire_message_id") or "").strip(),
        "message_type": str(value.get("message_type") or "").strip().lower(),
        "content_sha256": str(value.get("content_sha256") or "").strip(),
    }
    signature = str(value.get("signature") or "").strip()
    if (
        payload["run_id"] != str(run_id or "").strip()
        or payload["wire_message_id"] != str(wire_message_id or "").strip()
        or payload["message_type"] != str(message_type or "").strip().lower()
        or payload["content_sha256"]
        != hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
        or not signature
    ):
        return None
    try:
        expected = _runtime_public_progress_signature(payload)
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(signature, expected):
        return None
    progress = payload["progress"]
    if not is_runtime_public_progress(progress):
        return None
    return dict(progress)


def _unfenced_lines(text: str) -> list[str]:
    lines: list[str] = []
    fenced = False
    for line in str(text or "").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def has_degenerate_placeholder_flood(text: str) -> bool:
    """Detect provider placeholder floods without rejecting code examples."""

    lines = _unfenced_lines(text)
    placeholder_count = sum(len(_PLACEHOLDER_TOKEN_RE.findall(line)) for line in lines)
    placeholder_lines = sum(
        1
        for line in lines
        if _PLACEHOLDER_ONLY_LINE_RE.fullmatch(line)
        or (
            "undefined" in line.lower()
            and _PLACEHOLDER_SECTION_LINE_RE.fullmatch(line)
        )
    )
    return placeholder_count >= 24 or (
        placeholder_count >= 12 and placeholder_lines >= 3
    )


def sanitize_public_assistant_text(text: str) -> str:
    """Remove malformed aside-like lines outside fenced deliverable content."""

    kept: list[str] = []
    fenced = False
    placeholder_flood = has_degenerate_placeholder_flood(text)
    for line in str(text or "").splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            kept.append(line)
            continue
        stripped = line.lstrip()
        if not fenced and stripped.startswith("~>") and not _VALID_QUQU_LINE_RE.match(
            line
        ):
            continue
        if placeholder_flood and not fenced and (
            _PLACEHOLDER_ONLY_LINE_RE.fullmatch(line.rstrip("\r\n"))
            or _PLACEHOLDER_SECTION_LINE_RE.fullmatch(line.rstrip("\r\n"))
        ):
            continue
        kept.append(line)
    return "".join(kept)


def _could_be_incomplete_valid_ququ(stripped: str) -> bool:
    if any(tag.startswith(stripped) for tag in _VALID_QUQU_TAGS):
        return True
    return any(
        stripped.startswith(tag) and not stripped[len(tag) :].strip()
        for tag in _VALID_QUQU_TAGS
    )


def _could_be_incomplete_placeholder_line(stripped: str) -> bool:
    folded = stripped.strip().lower()
    if not folded:
        return False
    if "undefined".startswith(folded):
        return True
    return bool(
        re.match(r"^#{1,6}\s*(?:task|skill|\u5f53\u524d\u72b6\u6001)(?:\s*:.*)?$", folded)
    )


def _stable_stream_projection(text: str) -> str:
    """Project assistant text safely across arbitrary stream boundaries."""

    kept: list[str] = []
    fenced = False
    stream_lines = str(text or "").splitlines(keepends=True)
    placeholder_flood = has_degenerate_placeholder_flood(text)
    for index, line in enumerate(stream_lines):
        complete = line.endswith(("\n", "\r"))
        stripped = line.lstrip()
        if stripped.startswith("```"):
            fenced = not fenced
            kept.append(line)
            continue
        if fenced or _VALID_QUQU_LINE_RE.match(line):
            kept.append(line)
            continue
        if stripped.startswith("~>"):
            if not complete and _could_be_incomplete_valid_ququ(stripped):
                continue
            continue
        placeholder_only = _PLACEHOLDER_ONLY_LINE_RE.fullmatch(
            line.rstrip("\r\n")
        )
        section_scaffold = _PLACEHOLDER_SECTION_LINE_RE.fullmatch(
            line.rstrip("\r\n")
        )
        if not fenced and complete and placeholder_only:
            continue
        if not fenced and complete and section_scaffold:
            # Hold an ambiguous scaffold heading until later ordinary content
            # proves it is intentional. A detected placeholder flood makes it
            # permanently invalid; otherwise the cumulative stream projection
            # releases the heading together with the following real content.
            later_text = "".join(stream_lines[index + 1 :]).strip()
            if (
                placeholder_flood
                or "undefined" in line.lower()
                or not later_text
                or _could_be_incomplete_placeholder_line(later_text)
            ):
                continue
        if not fenced and not complete and (
            placeholder_only
            or _could_be_incomplete_placeholder_line(stripped)
        ):
            continue
        if not complete and (not stripped or stripped == "~"):
            continue
        kept.append(line)
    return "".join(kept)


def prompt_leak_guard_for_runtime_input(runtime_input: RuntimeInput) -> PromptLeakGuard:
    """Build one exact leak guard for ordinary public assistant output.

    Exact fingerprints protect literal prompt/secret material while retaining
    the surrounding provider reasoning instead of replacing the whole block.
    """

    metadata: dict[str, Any] = dict(runtime_input.request.metadata or {})
    metadata.update(dict(runtime_input.metadata or {}))
    leak_canaries = metadata.get("prompt_leak_canaries") or ()
    leak_fingerprints = metadata.get("prompt_leak_fingerprints") or ()
    if isinstance(leak_canaries, str):
        leak_canaries = (leak_canaries,)
    if isinstance(leak_fingerprints, str):
        leak_fingerprints = (leak_fingerprints,)
    automatic_prompt_fingerprints: list[str] = []
    for runtime_message in runtime_input.messages:
        if runtime_message.role != "system":
            continue
        system_text = runtime_message.text_content().strip()
        if len(system_text) >= 24:
            automatic_prompt_fingerprints.append(system_text)
        automatic_prompt_fingerprints.extend(
            line.strip()
            for line in system_text.splitlines()
            if len(line.strip()) >= 24
        )
    return PromptLeakGuard(
        canaries=tuple(leak_canaries)
        if isinstance(leak_canaries, (list, tuple))
        else (),
        fingerprints=tuple(
            dict.fromkeys(
                [
                    *automatic_prompt_fingerprints,
                    *(
                        leak_fingerprints
                        if isinstance(leak_fingerprints, (list, tuple))
                        else ()
                    ),
                ]
            )
        ),
    )


class PublicReasoningProjector:
    """Preserve visible reasoning semantics while filtering narrow corruption."""

    def __init__(self, leak_guard: PromptLeakGuard | None = None) -> None:
        self._leak_guard = leak_guard
        self._assistant_streams: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _stream_key(event: RuntimeEvent) -> str:
        return str(event.item_id or event.turn_id or event.id)

    @staticmethod
    def _summary(event: RuntimeEvent) -> str:
        """Read summaries from both native and persisted public event schemas."""

        summary = getattr(event, "summary", "")
        return summary if isinstance(summary, str) else ""

    @staticmethod
    def _copy_event(
        event: RuntimeEvent,
        *,
        summary: str,
        payload: dict[str, Any],
    ) -> RuntimeEvent:
        """Update an event without inventing fields absent from replay schemas."""

        update: dict[str, Any] = {"payload": payload}
        if "summary" in type(event).model_fields:
            update["summary"] = summary
        return event.model_copy(update=update)

    def _project_delta(self, event: RuntimeEvent, delta: str) -> str:
        key = self._stream_key(event)
        raw, emitted = self._assistant_streams.get(key, ("", ""))
        raw += delta
        projected = _stable_stream_projection(raw)
        if not projected.startswith(emitted):
            self._assistant_streams[key] = (raw, projected)
            return ""
        visible_delta = projected[len(emitted) :]
        self._assistant_streams[key] = (raw, projected)
        return visible_delta

    def _guard(self, event: RuntimeEvent) -> RuntimeEvent:
        if event.type in {"message.delta", "message.completed"}:
            payload = dict(event.payload)
            delta = payload.get("delta")
            if isinstance(delta, str):
                payload["delta"] = (
                    self._project_delta(event, delta)
                    if event.type == "message.delta"
                    else sanitize_public_assistant_text(delta)
                )
            raw_message = payload.get("message")
            if isinstance(raw_message, dict):
                try:
                    message = RuntimeMessage.model_validate(raw_message)
                except Exception:
                    message = None
                if message is not None:
                    message.content = [
                        block.model_copy(
                            update={
                                "text": (
                                    _stable_stream_projection(block.text)
                                    if event.type == "message.delta"
                                    else sanitize_public_assistant_text(block.text)
                                )
                            }
                        )
                        if block.type in _PUBLIC_TEXT_BLOCK_TYPES
                        and isinstance(block.text, str)
                        else block
                        for block in message.content
                    ]
                    payload["message"] = message.model_dump(mode="json")
            event = self._copy_event(
                event,
                summary=(
                    str(payload.get("delta") or "")
                    if event.type == "message.delta" and isinstance(delta, str)
                    else sanitize_public_assistant_text(self._summary(event))
                ),
                payload=payload,
            )
            if event.type == "message.completed":
                self._assistant_streams.pop(self._stream_key(event), None)
        if self._leak_guard is None:
            return event
        if event.type == "message.delta":
            payload = dict(event.payload)
            delta = payload.get("delta")
            if isinstance(delta, str):
                stream_id = self._stream_key(event)
                guarded_delta = self._leak_guard.project_stream_text(
                    delta,
                    surface="public_runtime_event",
                    stream_id=stream_id,
                )
                payload = self._leak_guard.project_value(
                    {key: value for key, value in payload.items() if key != "delta"},
                    surface="public_runtime_event",
                ) | {"delta": guarded_delta}
                return self._copy_event(
                    event,
                    summary=guarded_delta,
                    payload=payload,
                )
        if event.type == "message.completed":
            self._leak_guard.finalize_stream(
                surface="public_runtime_event",
                stream_id=self._stream_key(event),
            )
        guarded_summary = self._leak_guard.project_value(
            self._summary(event),
            surface="public_runtime_event",
        )
        guarded_payload = self._leak_guard.project_value(
            event.payload,
            surface="public_runtime_event",
        )
        return self._copy_event(
            event,
            summary=guarded_summary if isinstance(guarded_summary, str) else "",
            payload=guarded_payload if isinstance(guarded_payload, dict) else {},
        )

    def project(self, event: RuntimeEvent) -> RuntimeEvent | None:
        """Project public content without fabricating or re-summarising it."""
        return self._guard(event)


__all__ = [
    "SUBAGENT_INTERNAL_ACTIVITY_STATUS",
    "PublicReasoningProjector",
    "has_degenerate_placeholder_flood",
    "is_runtime_public_progress",
    "prompt_leak_guard_for_runtime_input",
    "sanitize_public_assistant_text",
    "without_runtime_public_progress_claims",
]
