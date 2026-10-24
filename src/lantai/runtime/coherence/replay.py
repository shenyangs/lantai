# -*- coding: utf-8 -*-
"""Forward-compatible governance for append-only event projections."""

from __future__ import annotations

from typing import Any, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .integrity import canonical_sha256, json_safe


ReplayDisposition = Literal[
    "applied",
    "ignored_duplicate",
    "ignored_unknown_type",
    "quarantined_future_schema",
    "quarantined_invalid",
    "forked_conflict",
]


class ProjectionEventDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    seq: int | None = Field(default=None, ge=0)
    event_type: str
    event_sha256: str
    disposition: ReplayDisposition
    reason: str = ""


class ProjectionReplayReport(BaseModel):
    """Complete replay decision set; ignored bytes remain content-addressed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    projection_id: str
    source_log_sha256: str
    supported_event_schema: int = Field(ge=1)
    decisions: tuple[ProjectionEventDecision, ...]
    applied_event_ids: tuple[str, ...]
    forked: bool = False
    integrity_sha256: str

    def verify_integrity(self) -> bool:
        return (
            canonical_sha256(
                self.model_dump(mode="json", exclude={"integrity_sha256"}),
            )
            == self.integrity_sha256
        )


def govern_projection_replay(
    events: Iterable[Mapping[str, Any] | Any],
    *,
    projection_id: str,
    supported_event_types: set[str] | frozenset[str],
    supported_event_schema: int = 1,
) -> ProjectionReplayReport:
    """Classify events without deleting unknown or conflicting evidence."""

    raw_events = [json_safe(event) for event in events]
    source_log_sha256 = canonical_sha256(raw_events)
    decisions: list[ProjectionEventDecision] = []
    applied_ids: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_sequences: dict[int, tuple[str, str]] = {}
    forked = False
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            raw = {"raw": raw}
        event_sha256 = canonical_sha256(raw)
        event_id = str(raw.get("id") or f"invalid:{index}:{event_sha256[:12]}")
        event_type = str(raw.get("type") or "")
        raw_seq = raw.get("seq")
        seq = raw_seq if isinstance(raw_seq, int) and raw_seq >= 0 else None
        raw_schema = raw.get("schema_version", 1)
        try:
            event_schema = int(raw_schema)
        except (TypeError, ValueError):
            event_schema = -1

        disposition: ReplayDisposition
        reason = ""
        if event_schema > supported_event_schema:
            disposition = "quarantined_future_schema"
            reason = "event schema is newer than this projection"
        elif event_schema < 1 or not event_type or seq is None:
            disposition = "quarantined_invalid"
            reason = "event identity, type, sequence, or schema is invalid"
        elif event_id in seen_ids:
            if seen_ids[event_id] == event_sha256:
                disposition = "ignored_duplicate"
                reason = "exact event replay is idempotent"
            else:
                disposition = "forked_conflict"
                reason = "same event id has different content"
                forked = True
        elif seq in seen_sequences and seen_sequences[seq] != (
            event_id,
            event_sha256,
        ):
            disposition = "forked_conflict"
            reason = "same sequence has different event identity"
            forked = True
        elif event_type not in supported_event_types:
            disposition = "ignored_unknown_type"
            reason = "unknown event is preserved but not projected"
        else:
            disposition = "applied"
            applied_ids.append(event_id)

        seen_ids.setdefault(event_id, event_sha256)
        if seq is not None:
            seen_sequences.setdefault(seq, (event_id, event_sha256))
        decisions.append(
            ProjectionEventDecision(
                event_id=event_id,
                seq=seq,
                event_type=event_type,
                event_sha256=event_sha256,
                disposition=disposition,
                reason=reason,
            ),
        )
    payload = {
        "schema_version": 1,
        "projection_id": projection_id,
        "source_log_sha256": source_log_sha256,
        "supported_event_schema": supported_event_schema,
        "decisions": tuple(decisions),
        "applied_event_ids": tuple(applied_ids),
        "forked": forked,
    }
    return ProjectionReplayReport(
        **payload,
        integrity_sha256=canonical_sha256(payload),
    )


__all__ = [
    "ProjectionEventDecision",
    "ProjectionReplayReport",
    "ReplayDisposition",
    "govern_projection_replay",
]
