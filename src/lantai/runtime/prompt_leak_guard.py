# -*- coding: utf-8 -*-
"""Exact canary/fingerprint guard for public runtime projections."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .events import RuntimeEvent

_REDACTION = "[REDACTED_PROMPT_FINGERPRINT]"


@dataclass(frozen=True, slots=True)
class PromptLeakFinding:
    surface: str
    kind: str
    value_sha256: str
    occurrences: int


class PromptLeakGuard:
    """Redact exact test canaries without retaining raw values in logs."""

    def __init__(
        self,
        *,
        canaries: tuple[str, ...] | list[str] = (),
        fingerprints: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._values = tuple(
            (kind, value)
            for kind, values in (
                ("canary", canaries),
                ("fingerprint", fingerprints),
            )
            for raw in values
            for value in (str(raw or "").strip(),)
            if value
        )
        self._findings: dict[tuple[str, str], PromptLeakFinding] = {}
        self._stream_pending: dict[tuple[str, str], str] = {}

    @property
    def findings(self) -> tuple[PromptLeakFinding, ...]:
        return tuple(self._findings.values())

    def _project_text(self, text: str, *, surface: str) -> str:
        projected = text
        for kind, value in self._values:
            count = projected.count(value)
            if not count:
                continue
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            key = (kind, digest)
            previous = self._findings.get(key)
            self._findings[key] = PromptLeakFinding(
                surface=surface,
                kind=kind,
                value_sha256=digest,
                occurrences=count + (previous.occurrences if previous else 0),
            )
            projected = projected.replace(value, _REDACTION)
        return projected

    def _record(self, *, kind: str, value: str, surface: str, count: int) -> None:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        key = (kind, digest)
        previous = self._findings.get(key)
        self._findings[key] = PromptLeakFinding(
            surface=surface,
            kind=kind,
            value_sha256=digest,
            occurrences=count + (previous.occurrences if previous else 0),
        )

    def project_stream_text(
        self,
        text: str,
        *,
        surface: str,
        stream_id: str,
        final: bool = False,
    ) -> str:
        """Redact exact values across arbitrary stream chunk boundaries.

        Only the suffix that could still become a protected value is retained.
        Raw prompt material is kept in memory for the lifetime of one stream and
        is never written to findings or logs.
        """
        key = (surface, stream_id)
        pending = self._stream_pending.pop(key, "") + str(text or "")
        projected: list[str] = []
        index = 0
        while index < len(pending):
            matches = [
                (kind, value)
                for kind, value in self._values
                if pending.startswith(value, index)
            ]
            if matches:
                kind, value = max(matches, key=lambda item: len(item[1]))
                self._record(kind=kind, value=value, surface=surface, count=1)
                projected.append(_REDACTION)
                index += len(value)
                continue
            tail = pending[index:]
            if not final and any(
                value.startswith(tail) for _kind, value in self._values
            ):
                break
            projected.append(pending[index])
            index += 1
        if not final and index < len(pending):
            self._stream_pending[key] = pending[index:]
        return "".join(projected)

    def discard_stream(self, *, surface: str, stream_id: str) -> None:
        """Forget an incomplete stream suffix without exposing it."""
        self._stream_pending.pop((surface, stream_id), None)

    def finalize_stream(self, *, surface: str, stream_id: str) -> str:
        """Finish one stream and return any safe suffix held at its boundary.

        A harmless terminal character can also be the opening prefix of a
        protected value.  It therefore has to remain buffered while more
        deltas may arrive, but it must not be silently discarded once the
        authoritative completed message closes the stream.
        """
        return self.project_stream_text(
            "",
            surface=surface,
            stream_id=stream_id,
            final=True,
        )

    def project_value(self, value: Any, *, surface: str) -> Any:
        """Return a redacted deep projection while leaving the input untouched."""
        if isinstance(value, str):
            return self._project_text(value, surface=surface)
        if isinstance(value, dict):
            return {
                self._project_text(str(key), surface=surface): self.project_value(
                    item,
                    surface=surface,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.project_value(item, surface=surface) for item in value]
        if isinstance(value, tuple):
            return tuple(self.project_value(item, surface=surface) for item in value)
        return value

    def project_event(
        self,
        event: RuntimeEvent,
        *,
        surface: str,
    ) -> RuntimeEvent:
        """Return a public-safe event copy without mutating private model state."""
        return event.model_copy(
            update={
                "summary": self._project_text(event.summary, surface=surface),
                "payload": self.project_value(event.payload, surface=surface),
            }
        )


__all__ = ["PromptLeakFinding", "PromptLeakGuard"]
