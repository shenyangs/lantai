# -*- coding: utf-8 -*-
"""Governed background consolidation for canonical long-term memory.

Dreaming is deliberately downstream of the canonical ledger.  It may rank
evidence and stage review candidates, but it never edits ``MEMORY.md`` and it
never turns inferred content into confirmed memory.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical_ledger import CanonicalMemoryLedger, ConfirmedFact, MemoryEvent
from .locking import memory_state_lock


_ELIGIBLE_TYPES = {"identity", "workflow", "style", "instruction"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", (value or "").casefold(), flags=re.UNICODE)


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


class MemoryDreamingService:
    """Small public interface around ranking, audit, diary, and rollback state."""

    def __init__(
        self,
        working_dir: str | Path,
        *,
        agent_id: str,
        ledger: CanonicalMemoryLedger | None = None,
    ) -> None:
        self.working_dir = Path(working_dir)
        self.agent_id = agent_id
        self.ledger = ledger or CanonicalMemoryLedger(self.working_dir)
        self.root = self.working_dir / "memory-dreaming"
        self.runs_dir = self.root / "runs"
        self.diary_path = self.root / "DREAMS.md"
        self.lock_path = self.root / ".dreaming.lock"

    @contextmanager
    def _locked(self):
        with memory_state_lock(self.working_dir):
            self.root.mkdir(parents=True, exist_ok=True)
            yield

    def run(
        self,
        *,
        trigger: str = "manual",
        shadow: bool = False,
        min_score: float = 0.68,
        rem_enabled: bool = True,
        max_candidates: int = 20,
    ) -> dict[str, Any]:
        """Run Light/REM/Deep without ever confirming inferred memory."""

        with self._locked():
            started_at = _now()
            run_id = f"dream_{uuid.uuid4().hex}"
            facts = self.ledger.list_facts(
                statuses={"observed", "pending", "conflict"},
            )
            event_ids = {
                event_id for fact in facts for event_id in fact.event_ids
            }
            events = {
                event.id: event
                for event in self.ledger.list_events(event_ids=event_ids)
            }
            candidates = [
                self._score_fact(fact, events, min_score=_bounded(min_score))
                for fact in facts
            ]
            candidates.sort(
                key=lambda item: (
                    bool(item["eligible"]),
                    float(item["score"]),
                    int(item["occurrence_count"]),
                ),
                reverse=True,
            )
            candidates = candidates[: max(1, min(max_candidates, 100))]
            eligible = [item for item in candidates if item["eligible"]]
            mutations: list[dict[str, Any]] = []
            if not shadow:
                for candidate in eligible:
                    before = self.ledger.get_fact(candidate["fact_id"])
                    if before is None:
                        continue
                    staged = self.ledger.stage_for_review(
                        before.id,
                        run_id=run_id,
                        score=float(candidate["score"]),
                        reason=str(candidate["reason"]),
                        source_event_ids=list(candidate["source_event_ids"]),
                    )
                    if staged is None:
                        continue
                    candidate["status"] = staged.status
                    candidate["staged"] = True
                    mutations.append(
                        {
                            "fact_id": before.id,
                            "before": self.serialize_fact(before),
                            "after": self.serialize_fact(staged),
                        },
                    )
            result = {
                "version": 1,
                "run_id": run_id,
                "agent_id": self.agent_id,
                "trigger": trigger,
                "shadow": bool(shadow),
                "status": "completed",
                "started_at": started_at,
                "completed_at": _now(),
                "phases": {
                    "light": "completed",
                    "rem": "completed" if rem_enabled else "disabled",
                    "deep": "completed",
                },
                "counts": {
                    "reviewed": len(candidates),
                    "eligible": len(eligible),
                    "staged": len(mutations),
                    "excluded": len(candidates) - len(eligible),
                },
                "candidates": candidates,
                "mutations": mutations,
                "can_rollback": bool(mutations),
                "rolled_back_at": "",
            }
            self._write_run(result)
            self._append_diary(result, rem_enabled=rem_enabled)
            return result

    def _score_fact(
        self,
        fact: ConfirmedFact,
        events: dict[str, MemoryEvent],
        *,
        min_score: float,
    ) -> dict[str, Any]:
        evidence = [
            events[event_id]
            for event_id in fact.event_ids
            if event_id in events
        ]
        grounded_events = [
            event
            for event in evidence
            if self.ledger._is_verified_evidence_ref(fact, event)
        ]
        age_days = max(
            0.0,
            (datetime.now(timezone.utc) - _parse_time(fact.updated_at)).total_seconds()
            / 86_400,
        )
        signals = {
            "relevance": _bounded(float(fact.utility_score)),
            "frequency": _bounded(fact.occurrence_count / 3),
            "session_diversity": _bounded(len(set(fact.session_ids)) / 3),
            "recency": _bounded(1 - age_days / 90),
            "evidence": _bounded(len(grounded_events) / 3),
            "richness": self._richness(fact.content),
        }
        score = (
            signals["relevance"] * 0.30
            + signals["frequency"] * 0.24
            + signals["session_diversity"] * 0.15
            + signals["recency"] * 0.15
            + signals["evidence"] * 0.10
            + signals["richness"] * 0.06
        )
        reasons: list[str] = []
        if not grounded_events:
            reasons.append("没有通过来源复核的用户证据")
        if fact.memory_type not in _ELIGIBLE_TYPES:
            reasons.append("记忆类型不进入长期沉淀")
        if fact.sensitivity != "normal":
            reasons.append("涉及敏感信息，只能人工确认")
        if fact.subject not in {"user", "project"}:
            reasons.append("主体不是用户或项目")
        if fact.occurrence_count < 2 or len(set(fact.session_ids)) < 2:
            reasons.append("尚未在两个独立会话中重复出现")
        if score < min_score:
            reasons.append(f"综合评分低于 {min_score:.2f}")
        eligible = not reasons
        if eligible:
            reasons.append("证据、重复度和长期价值均达到待确认门槛")
        return {
            "fact_id": fact.id,
            "content": fact.content,
            "memory_type": fact.memory_type,
            "status": fact.status,
            "scope": fact.scope,
            "subject": fact.subject,
            "sensitivity": fact.sensitivity,
            "score": round(score, 4),
            "signals": {key: round(value, 4) for key, value in signals.items()},
            "occurrence_count": fact.occurrence_count,
            "session_count": len(set(fact.session_ids)),
            "evidence_count": len(evidence),
            "grounded_evidence_count": len(grounded_events),
            "grounded": bool(grounded_events),
            "eligible": eligible,
            "reason": "；".join(reasons),
            "source_event_ids": [event.id for event in grounded_events],
        }

    @staticmethod
    def _richness(content: str) -> float:
        length = len(_normalize(content))
        if length < 8:
            return 0.2
        if length <= 160:
            return 1.0
        return _bounded(1 - (length - 160) / 640)

    def _write_run(self, payload: dict[str, Any]) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.runs_dir / f"{payload['run_id']}.json"
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, path)

    def _append_diary(
        self,
        payload: dict[str, Any],
        *,
        rem_enabled: bool,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        existing = ""
        if self.diary_path.exists():
            existing = self.diary_path.read_text(encoding="utf-8").rstrip()
        if not existing:
            existing = (
                "# 澜台梦境日记\n\n"
                "> 这是供用户复核的整理报告，永远不能作为长期记忆的晋升来源。"
            )
        lines = [
            existing,
            "",
            f"## {payload['completed_at']} · {payload['run_id']}",
            "",
            f"- 触发：{payload['trigger']}",
            f"- 模式：{'影子整理' if payload['shadow'] else '受治理整理'}",
            f"- REM：{'已生成反思摘要' if rem_enabled else '已关闭'}",
            f"- 审阅：{payload['counts']['reviewed']} 条",
            f"- 达标：{payload['counts']['eligible']} 条",
            "",
            "### 候选",
        ]
        eligible = [
            item for item in payload["candidates"] if item.get("eligible")
        ]
        if eligible:
            for item in eligible[:10]:
                lines.append(
                    f"- [{item['score']:.2f}] {item['content']} "
                    f"（{item['reason']}）",
                )
        else:
            lines.append("- 本轮没有达到晋升门槛的候选。")
        self.diary_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def read_run(self, run_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"dream_[a-f0-9]{32}", run_id or ""):
            return None
        path = self.runs_dir / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def list_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent auditable runs, newest first."""

        if not self.runs_dir.exists():
            return []
        runs: list[dict[str, Any]] = []
        for path in self.runs_dir.glob("dream_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                runs.append(payload)
        runs.sort(
            key=lambda item: str(
                item.get("completed_at") or item.get("started_at") or "",
            ),
            reverse=True,
        )
        return runs[: max(1, min(limit, 50))]

    def attach_reflection(
        self,
        run_id: str,
        *,
        text: str,
        model_label: str,
    ) -> dict[str, Any] | None:
        """Attach quarantined REM prose; it is never read by scoring code."""

        reflection = (text or "").strip()[:4000]
        if not reflection:
            return self.read_run(run_id)
        with self._locked():
            payload = self.read_run(run_id)
            if payload is None:
                return None
            payload["reflection"] = {
                "text": reflection,
                "model_label": (model_label or "")[:120],
                "created_at": _now(),
                "promotion_source": False,
            }
            self._write_run(payload)
            with self.diary_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    "\n### REM 反思（隔离内容）\n\n"
                    "> 以下内容只供用户浏览，不参与评分、检索或记忆晋升。\n\n"
                    f"{reflection}\n",
                )
            return payload

    def rollback(self, run_id: str) -> dict[str, Any]:
        """Undo untouched staging mutations from one run, preserving user edits."""

        with self._locked():
            payload = self.read_run(run_id)
            if payload is None:
                raise ValueError("Dreaming run not found")
            if not payload.get("can_rollback") or payload.get("rolled_back_at"):
                return {
                    "run_id": run_id,
                    "rolled_back": False,
                    "restored_count": 0,
                    "skipped_count": len(payload.get("mutations", [])),
                    "reason": "run_not_rollbackable",
                }
            restored = 0
            skipped = 0
            for mutation in payload.get("mutations", []):
                if not isinstance(mutation, dict):
                    skipped += 1
                    continue
                fact = self.ledger.rollback_dream_stage(
                    str(mutation.get("fact_id") or ""),
                    run_id=run_id,
                    before=mutation.get("before")
                    if isinstance(mutation.get("before"), dict)
                    else {},
                    after=mutation.get("after")
                    if isinstance(mutation.get("after"), dict)
                    else {},
                )
                if fact is None:
                    skipped += 1
                else:
                    restored += 1
            payload["can_rollback"] = False
            payload["rolled_back_at"] = _now()
            payload["rollback"] = {
                "restored_count": restored,
                "skipped_count": skipped,
            }
            self._write_run(payload)
            return {
                "run_id": run_id,
                "rolled_back": bool(restored),
                "restored_count": restored,
                "skipped_count": skipped,
                "rolled_back_at": payload["rolled_back_at"],
            }

    @staticmethod
    def serialize_fact(fact: ConfirmedFact) -> dict[str, Any]:
        """Expose a stable snapshot helper for later rollback mutations."""

        return asdict(fact)
