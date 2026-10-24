# -*- coding: utf-8 -*-
"""Evidence-linked queue for useful, non-urgent proactive interventions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from .intervention_router import InterventionCandidate, route_intervention
from ..memory.locking import locked_memory_method, memory_state_lock
from ...utils.authoritative_store import atomic_write_bytes


OpportunityStatus = Literal["queued", "completed", "dismissed", "expired"]
OpportunityFeedback = Literal["presented", "ignored", "completed", "dismissed"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class Opportunity:
    id: str
    kind: str
    content: str
    score: float
    source_ids: tuple[str, ...]
    status: OpportunityStatus = "queued"
    created_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())
    expires_at: str = ""
    cooldown_until: str = ""
    ignored_hits: int = 0
    presented_hits: int = 0

    @classmethod
    def create(
        cls,
        candidate: InterventionCandidate,
        *,
        score: float,
        source_ids: tuple[str, ...],
        now: datetime,
        expires_at: str,
    ) -> "Opportunity":
        clean = " ".join(str(candidate.content or "").split()).strip()
        digest = hashlib.sha256(
            f"{candidate.kind}:{clean}:{'|'.join(source_ids)}".encode("utf-8"),
        ).hexdigest()[:18]
        stamp = now.astimezone(timezone.utc).isoformat()
        return cls(
            id=f"op_{digest}",
            kind=candidate.kind,
            content=clean,
            score=max(-1.0, min(float(score), 1.0)),
            source_ids=source_ids,
            created_at=stamp,
            updated_at=stamp,
            expires_at=expires_at,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Opportunity":
        return cls(
            id=str(payload.get("id") or ""),
            kind=str(payload.get("kind") or "future_follow_up"),
            content=str(payload.get("content") or "").strip(),
            score=max(-1.0, min(float(payload.get("score") or 0.0), 1.0)),
            source_ids=tuple(
                str(item) for item in payload.get("source_ids", []) if item
            ),
            status=str(payload.get("status") or "queued"),  # type: ignore[arg-type]
            created_at=str(payload.get("created_at") or _now().isoformat()),
            updated_at=str(payload.get("updated_at") or _now().isoformat()),
            expires_at=str(payload.get("expires_at") or ""),
            cooldown_until=str(payload.get("cooldown_until") or ""),
            ignored_hits=int(payload.get("ignored_hits") or 0),
            presented_hits=int(payload.get("presented_hits") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_ids"] = list(self.source_ids)
        return payload


class OpportunityStore:
    """Persistent queue that is allowed to contain no eligible opportunity."""

    def __init__(self, working_dir: str | Path) -> None:
        self.working_dir = Path(working_dir).expanduser()
        self.root = self.working_dir / "user-understanding"
        self.path = self.root / "opportunities.json"
        self._items: dict[str, Opportunity] = {}
        with memory_state_lock(self.working_dir):
            self._load()

    def _load(self) -> None:
        # A long-lived memory manager can outlive a category replacement.
        # Re-read under the stable workspace fence before each operation.
        self._items = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        items = raw.get("opportunities", []) if isinstance(raw, dict) else []
        for payload in items:
            if isinstance(payload, dict):
                item = Opportunity.from_dict(payload)
                if item.id and item.content:
                    self._items[item.id] = item

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "opportunities": [
                item.to_dict()
                for item in sorted(self._items.values(), key=lambda value: value.created_at)
            ],
        }
        atomic_write_bytes(self.path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    @locked_memory_method
    def enqueue_candidate(
        self,
        candidate: InterventionCandidate,
        *,
        source_ids: tuple[str, ...],
        now: datetime | None = None,
        expires_at: str = "",
    ) -> Opportunity | None:
        """Queue only candidates semantically routed for a later opportunity."""

        self._load()
        decision = route_intervention(candidate)
        if decision.route != "opportunity" or not candidate.content.strip():
            return None
        instant = (now or _now()).astimezone(timezone.utc)
        sources = tuple(dict.fromkeys(str(item) for item in source_ids if item))
        expiry = expires_at or (instant + timedelta(days=30)).isoformat()
        item = Opportunity.create(
            candidate,
            score=decision.score,
            source_ids=sources,
            now=instant,
            expires_at=expiry,
        )
        existing = self._items.get(item.id)
        if existing and existing.status == "queued":
            existing.score = max(existing.score, item.score)
            existing.updated_at = instant.isoformat()
            existing.expires_at = expiry
            item = existing
        else:
            self._items[item.id] = item
        self._save()
        return item

    @locked_memory_method
    def next_eligible(self, *, now: datetime | None = None) -> Opportunity | None:
        self._load()
        instant = (now or _now()).astimezone(timezone.utc)
        changed = False
        eligible: list[Opportunity] = []
        for item in self._items.values():
            if item.status != "queued":
                continue
            expiry = _as_utc(item.expires_at)
            if expiry is not None and expiry <= instant:
                item.status = "expired"
                item.updated_at = instant.isoformat()
                changed = True
                continue
            cooldown = _as_utc(item.cooldown_until)
            if cooldown is not None and cooldown > instant:
                continue
            eligible.append(item)
        if changed:
            self._save()
        if not eligible:
            return None
        return max(
            eligible,
            key=lambda item: (item.score - 0.12 * item.ignored_hits, item.updated_at),
        )

    @locked_memory_method
    def apply_feedback(
        self,
        opportunity_id: str,
        feedback: OpportunityFeedback,
        *,
        now: datetime | None = None,
    ) -> Opportunity | None:
        self._load()
        item = self._items.get(opportunity_id)
        if item is None:
            return None
        instant = (now or _now()).astimezone(timezone.utc)
        item.updated_at = instant.isoformat()
        if feedback == "presented":
            item.presented_hits += 1
            item.cooldown_until = (instant + timedelta(days=1)).isoformat()
        elif feedback == "ignored":
            item.ignored_hits += 1
            if item.ignored_hits >= 3:
                item.status = "dismissed"
            else:
                item.cooldown_until = (
                    instant + timedelta(days=2 * item.ignored_hits)
                ).isoformat()
        elif feedback == "completed":
            item.status = "completed"
        elif feedback == "dismissed":
            item.status = "dismissed"
        self._save()
        return item

    @locked_memory_method
    def invalidate_source(self, source_id: str) -> int:
        """Retire opportunities whose evidence source is no longer valid."""

        self._load()
        changed = 0
        for item in self._items.values():
            if item.status == "queued" and source_id in item.source_ids:
                item.status = "dismissed"
                item.updated_at = _now().isoformat()
                changed += 1
        if changed:
            self._save()
        return changed

    @locked_memory_method
    def list_all(self) -> list[Opportunity]:
        self._load()
        return list(self._items.values())
