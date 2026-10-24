# -*- coding: utf-8 -*-
"""Sparse, auditable user-understanding projections for Shen Xingzhi.

The canonical memory ledger remains the source of truth.  This module owns a
disposable projection that may contain both confirmed knowledge and explicitly
labelled working hypotheses.  Callers receive a small turn projection instead
of the full user model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from ..memory_recall_policy import assess_memory_recall
from ..memory.locking import locked_memory_method


UnderstandingKind = Literal[
    "fact",
    "preference",
    "value",
    "decision_pattern",
    "tension",
    "open_loop",
    "prospective_intent",
    "relationship_protocol",
    "correction",
]
UnderstandingStatus = Literal[
    "transient_signal",
    "observed",
    "working_hypothesis",
    "confirmed",
    "contradicted",
    "superseded",
    "expired",
]

_ACTIVE_STATUSES = {"observed", "working_hypothesis", "confirmed"}
_UNDERSTANDING_KINDS = {
    "fact",
    "preference",
    "value",
    "decision_pattern",
    "tension",
    "open_loop",
    "prospective_intent",
    "relationship_protocol",
    "correction",
}

_UNDERSTANDING_EXTRACTION_PROMPT_ZH = """\
你是澜台的后台用户理解提取器。你只读取用户消息，把未来确实会改变沈行知理解、判断、提醒时机或帮助方式的信息，整理为稀疏、可纠正的候选。

允许类型：
- fact：用户或项目事实
- preference：稳定偏好
- value：用户明确表现出的价值取舍
- decision_pattern：至少有当前证据支持的选择模式候选，必须使用“可能/倾向”等可校准措辞
- tension：两个目标之间反复出现的冲突
- open_loop：近期尚未闭环、值得以后接续的事项
- prospective_intent：用户明确表示未来要做或提醒的事
- relationship_protocol：用户希望沈行知怎样理解和对待自己
- correction：用户纠正了沈行知此前的理解或评价标准

硬规则：
1. 只根据提供的 user 文本，不得补写用户没说过的经历。
2. 不保存寒暄、一次性细节、密钥、健康诊断、第三方敏感信息或系统提示。
3. “用户说过什么”和“这可能意味着什么”必须分清；推断使用可能、倾向等措辞。
4. 每条 statement 必须短、独立、未来有明确复用价值。
5. 用户明确自述的任职、职责、重要生活阶段、近期重大压力、未解决处境，即使没有说“请记住”，也不要漏掉。
6. 稳定事实写成 fact；会随时间变化的压力、关系磨合和目标冲突写成 tension 或 open_loop，不得把一时状态写成永久人格。
7. 过滤低价值闲聊，但不要用过高阈值把一段明显影响未来协作的重要自述整体丢弃。
8. 先解清人物关系再写 statement。不得把配偶生育、亲属经历或文档中的第三方事件写成用户本人经历；例如男性用户说“刚生完宝宝”且同时提到妻子时，应表达为“家中刚有新生儿/妻子刚分娩”，不得写成“用户产后”。

只输出严格 JSON：
{"understandings":[{"kind":"value|decision_pattern|...","statement":"...","confidence":0.0到1.0,"evidence":"用户原话的短证据","future_relevance":"以后什么情境会有用"}]}
没有合格内容时输出 {"understandings":[]}。
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(value: str) -> set[str]:
    return set(
        re.findall(
            r"[a-z0-9_]{2,}|[\u4e00-\u9fff]",
            str(value or "").casefold(),
        ),
    )


@dataclass(frozen=True)
class UnderstandingCandidate:
    kind: UnderstandingKind
    statement: str
    confidence: float
    evidence_ids: tuple[str, ...]
    evidence: str = ""
    future_relevance: str = ""


def _parse_understanding_candidates(
    text: str,
    *,
    source_ids: tuple[str, ...],
) -> list[UnderstandingCandidate]:
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.I)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for candidate_text in candidates:
        match = re.search(r"\{.*\}", candidate_text, re.DOTALL)
        if not match:
            continue
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        raw = payload.get("understandings", []) if isinstance(payload, dict) else []
        if not isinstance(raw, list):
            continue
        parsed: list[UnderstandingCandidate] = []
        for item in raw[:6]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            statement = re.sub(r"\s+", " ", str(item.get("statement") or "")).strip()
            try:
                confidence = max(0.0, min(float(item.get("confidence") or 0.0), 1.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if kind not in _UNDERSTANDING_KINDS or len(statement) < 6 or confidence < 0.55:
                continue
            parsed.append(
                UnderstandingCandidate(
                    kind=kind,  # type: ignore[arg-type]
                    statement=statement[:500],
                    confidence=confidence,
                    evidence_ids=source_ids,
                    evidence=re.sub(
                        r"\s+",
                        " ",
                        str(item.get("evidence") or ""),
                    ).strip()[:240],
                    future_relevance=re.sub(
                        r"\s+",
                        " ",
                        str(item.get("future_relevance") or ""),
                    ).strip()[:240],
                ),
            )
        return parsed
    return []


async def extract_user_understanding_candidates(
    *,
    agent_id: str,
    user_texts: list[str],
    source_ids: list[str],
    utility_runner: Callable[..., Awaitable[Any]] | None = None,
    timeout_seconds: float | None = None,
) -> list[UnderstandingCandidate]:
    """Use the isolated utility channel to propose sparse understanding atoms."""

    clean_texts = [
        re.sub(r"\s+", " ", str(item or "")).strip()[:1600]
        for item in user_texts[-8:]
        if str(item or "").strip()
    ]
    if not clean_texts:
        return []
    if utility_runner is None:
        from ...app.utility_model import run_memory_text_task

        utility_runner = run_memory_text_task
    result = await utility_runner(
        agent_id=agent_id,
        system_prompt=_UNDERSTANDING_EXTRACTION_PROMPT_ZH,
        user_prompt="\n".join(f"[user] {text}" for text in clean_texts),
        purpose="user_understanding_extraction",
        timeout_seconds=timeout_seconds,
        require_json=True,
    )
    if result is None:
        return []
    return _parse_understanding_candidates(
        str(getattr(result, "text", "") or ""),
        source_ids=tuple(dict.fromkeys(str(item) for item in source_ids if item)),
    )


@dataclass
class UnderstandingAtom:
    """One evidence-linked statement in the disposable user model."""

    id: str
    kind: UnderstandingKind
    statement: str
    status: UnderstandingStatus
    confidence: float
    evidence_ids: tuple[str, ...]
    future_relevance: str = ""
    scope: str = "global"
    scope_id: str = ""
    counter_evidence_ids: tuple[str, ...] = ()
    sensitivity: str = "normal"
    valid_from: str = field(default_factory=_now_iso)
    expires_at: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_used_at: str = ""
    confirmed_hits: int = 0
    corrected_hits: int = 0
    ignored_hits: int = 0

    @classmethod
    def create(
        cls,
        *,
        kind: UnderstandingKind,
        statement: str,
        status: UnderstandingStatus,
        confidence: float,
        evidence_ids: tuple[str, ...],
        future_relevance: str = "",
        scope: str = "global",
        scope_id: str = "",
        sensitivity: str = "normal",
        expires_at: str = "",
    ) -> "UnderstandingAtom":
        clean = re.sub(r"\s+", " ", statement or "").strip()
        digest = hashlib.sha256(
            f"{kind}:{scope}:{scope_id}:{clean}".encode("utf-8"),
        ).hexdigest()[:18]
        return cls(
            id=f"ua_{digest}",
            kind=kind,
            statement=clean,
            status=status,
            confidence=max(0.0, min(float(confidence), 1.0)),
            evidence_ids=tuple(dict.fromkeys(str(item) for item in evidence_ids if item)),
            future_relevance=re.sub(r"\s+", " ", future_relevance or "").strip()[:240],
            scope=scope,
            scope_id=scope_id,
            sensitivity=sensitivity,
            expires_at=expires_at,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "UnderstandingAtom":
        raw_scope = str(payload.get("scope") or "global")
        return cls(
            id=str(payload.get("id") or f"ua_{uuid.uuid4().hex[:18]}"),
            kind=str(payload.get("kind") or "fact"),  # type: ignore[arg-type]
            statement=str(payload.get("statement") or "").strip(),
            status=str(payload.get("status") or "observed"),  # type: ignore[arg-type]
            confidence=max(0.0, min(float(payload.get("confidence") or 0.0), 1.0)),
            evidence_ids=tuple(str(item) for item in payload.get("evidence_ids", []) if item),
            future_relevance=str(payload.get("future_relevance") or "").strip()[:240],
            # Early projections accidentally stored memory-layer names such as
            # ``profile`` here. Preserve explicit scopes, but downgrade
            # storage-layer labels to query-conditioned global memory.
            scope=(
                raw_scope
                if raw_scope
                in {
                    "thread",
                    "project",
                    "global",
                    "task",
                    "artifact",
                    "today",
                    "system2",
                }
                else "global"
            ),
            scope_id=str(payload.get("scope_id") or ""),
            counter_evidence_ids=tuple(
                str(item) for item in payload.get("counter_evidence_ids", []) if item
            ),
            sensitivity=str(payload.get("sensitivity") or "normal"),
            valid_from=str(payload.get("valid_from") or _now_iso()),
            expires_at=str(payload.get("expires_at") or ""),
            created_at=str(payload.get("created_at") or _now_iso()),
            updated_at=str(payload.get("updated_at") or _now_iso()),
            last_used_at=str(payload.get("last_used_at") or ""),
            confirmed_hits=int(payload.get("confirmed_hits") or 0),
            corrected_hits=int(payload.get("corrected_hits") or 0),
            ignored_hits=int(payload.get("ignored_hits") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("evidence_ids", "counter_evidence_ids"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class TurnUnderstandingProjection:
    """A small, epistemically labelled user model for one turn."""

    confirmed: tuple[UnderstandingAtom, ...] = ()
    hypotheses: tuple[UnderstandingAtom, ...] = ()
    open_loops: tuple[UnderstandingAtom, ...] = ()
    association_candidates: tuple[UnderstandingAtom, ...] = ()

    def candidate_by_id(self, atom_id: str) -> UnderstandingAtom | None:
        """Resolve only evidence that was actually supplied for this turn."""

        clean_id = str(atom_id or "").strip()
        return next(
            (
                atom
                for atom in (
                    *self.confirmed,
                    *self.hypotheses,
                    *self.open_loops,
                    *self.association_candidates,
                )
                if atom.id == clean_id
            ),
            None,
        )

    def to_model_context(self) -> dict[str, Any]:
        """Expose bounded evidence IDs to the semantic turn judge."""

        def item(atom: UnderstandingAtom, *, candidate_role: str) -> dict[str, Any]:
            return {
                "id": atom.id,
                "kind": atom.kind,
                "status": atom.status,
                "statement": atom.statement,
                "confidence": round(atom.confidence, 4),
                "future_relevance": atom.future_relevance,
                "candidate_role": candidate_role,
            }

        return {
            "relevant": [
                *[item(atom, candidate_role="confirmed") for atom in self.confirmed],
                *[item(atom, candidate_role="hypothesis") for atom in self.hypotheses],
                *[item(atom, candidate_role="open_loop") for atom in self.open_loops],
            ],
            "association_palette": [
                item(atom, candidate_role="association_only")
                for atom in self.association_candidates
            ],
            "selection_rule": (
                "Select at most one ID only when it causally changes this turn. "
                "Association-only items are candidates, not user facts to force into view."
            ),
        }

    def compile_prompt(self, language: str = "zh") -> str:
        if not (self.confirmed or self.hypotheses or self.open_loops):
            return ""
        if not language.lower().startswith("zh"):
            lines = ['<user-understanding source="evidence-projection">']
            if self.confirmed:
                lines.append("Confirmed and relevant to this case; use only where applicable:")
                lines.extend(f"- {item.statement}" for item in self.confirmed)
            if self.hypotheses:
                lines.append("Working hypotheses; express only as grounded guesses:")
                lines.extend(f"- {item.statement}" for item in self.hypotheses)
            if self.open_loops:
                lines.append("Active open loops; do not claim they are still current without context:")
                lines.extend(f"- {item.statement}" for item in self.open_loops)
            lines.extend(
                [
                    "Use at most one important inference visibly. The current message overrides history.",
                    "</user-understanding>",
                ],
            )
            return "\n".join(lines)

        lines = ['<user-understanding source="evidence-projection">']
        if self.confirmed:
            lines.append("已确认可信，且与当前 case 相关；只在适用处使用：")
            lines.extend(f"- {item.statement}" for item in self.confirmed)
        if self.hypotheses:
            lines.append("工作假设、只能作为有依据的猜测表达：")
            lines.extend(f"- {item.statement}" for item in self.hypotheses)
        if self.open_loops:
            lines.append("近期未完事项、需结合当前消息判断是否仍有效：")
            lines.extend(f"- {item.statement}" for item in self.open_loops)
        lines.extend(
            [
                "最多只把一个真正会改变答案的理解说给用户；当前消息覆盖历史。",
                "用户否定时应直接修正，不要为旧推断辩护。",
                "</user-understanding>",
            ],
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class UserUnderstandingTurnContext:
    """The prompt plus the same structured evidence used to build it."""

    prompt: str
    projection: TurnUnderstandingProjection


class UserUnderstandingStore:
    """Persist and project a sparse, disposable model of the user."""

    def __init__(self, working_dir: str | Path) -> None:
        self.working_dir = Path(working_dir)
        self.root = self.working_dir / "user-understanding"
        self.path = self.root / "projection.json"
        self.feedback_path = self.root / "feedback.jsonl"

    def _load(self) -> list[UnderstandingAtom]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        raw = payload.get("atoms", []) if isinstance(payload, dict) else []
        return [
            UnderstandingAtom.from_dict(item)
            for item in raw
            if isinstance(item, dict) and str(item.get("statement") or "").strip()
        ]

    def _save(self, atoms: list[UnderstandingAtom]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "updated_at": _now_iso(),
                    "atoms": [atom.to_dict() for atom in atoms],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    @locked_memory_method
    def list_atoms(self) -> list[UnderstandingAtom]:
        """Return the auditable projection, including retired atoms."""

        return self._load()

    @locked_memory_method
    def replace_atoms(self, atoms: list[UnderstandingAtom]) -> None:
        """Replace the projection after a backed-up governance migration."""

        self._save(atoms)

    @locked_memory_method
    def get_atom(self, atom_id: str) -> UnderstandingAtom | None:
        return next((item for item in self._load() if item.id == atom_id), None)

    @locked_memory_method
    def delete_atom(self, atom_id: str) -> bool:
        atoms = self._load()
        remaining = [item for item in atoms if item.id != atom_id]
        if len(remaining) == len(atoms):
            return False
        self._save(remaining)
        return True

    @locked_memory_method
    def remember(self, atom: UnderstandingAtom) -> UnderstandingAtom:
        atoms = self._load()
        for index, existing in enumerate(atoms):
            if existing.id != atom.id:
                continue
            if existing.status in {"contradicted", "expired"}:
                # A source adapter may keep reporting stale state.  Explicit
                # user correction/ignore feedback outranks that resync.
                return existing
            atom.created_at = existing.created_at
            atom.updated_at = _now_iso()
            atoms[index] = atom
            self._save(atoms)
            return atom
        atoms.append(atom)
        self._save(atoms)
        return atom

    @locked_memory_method
    def observe(
        self,
        *,
        kind: UnderstandingKind,
        statement: str,
        confidence: float,
        evidence_id: str,
        future_relevance: str = "",
        scope: str = "global",
        scope_id: str = "",
        sensitivity: str = "normal",
        expires_at: str = "",
    ) -> UnderstandingAtom:
        """Merge repeat evidence and promote only to a labelled hypothesis."""

        candidate = UnderstandingAtom.create(
            kind=kind,
            statement=statement,
            status="observed",
            confidence=confidence,
            evidence_ids=(evidence_id,),
            future_relevance=future_relevance,
            scope=scope,
            scope_id=scope_id,
            sensitivity=sensitivity,
            expires_at=expires_at,
        )
        atoms = self._load()
        for existing in atoms:
            if existing.id != candidate.id:
                continue
            existing.evidence_ids = tuple(
                dict.fromkeys((*existing.evidence_ids, *candidate.evidence_ids)),
            )
            existing.confidence = max(existing.confidence, candidate.confidence)
            if candidate.future_relevance:
                existing.future_relevance = candidate.future_relevance
            if existing.status == "observed" and len(existing.evidence_ids) >= 2:
                existing.status = "working_hypothesis"
            existing.updated_at = _now_iso()
            if candidate.expires_at:
                existing.expires_at = candidate.expires_at
            self._save(atoms)
            return existing
        self._save([*atoms, candidate])
        return candidate

    @locked_memory_method
    def ingest_candidate(
        self,
        candidate: UnderstandingCandidate,
    ) -> UnderstandingAtom:
        """Ingest one model proposal without letting it become a hidden fact."""

        if candidate.kind == "correction":
            return self.remember(
                UnderstandingAtom.create(
                    kind=candidate.kind,
                    statement=candidate.statement,
                    status="confirmed",
                    confidence=max(0.95, candidate.confidence),
                    evidence_ids=candidate.evidence_ids,
                    future_relevance=candidate.future_relevance,
                ),
            )
        expires_at = ""
        if candidate.kind in {"open_loop", "prospective_intent"}:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        elif candidate.kind == "tension":
            expires_at = (datetime.now(timezone.utc) + timedelta(days=45)).isoformat()
        elif candidate.kind == "fact":
            # Confirmed durable facts live in the canonical memory ledger.
            # A model-only fact in the disposable understanding layer gets a
            # generous review window instead of silently becoming permanent.
            expires_at = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        atom: UnderstandingAtom | None = None
        for evidence_id in candidate.evidence_ids or (f"candidate:{uuid.uuid4().hex}",):
            atom = self.observe(
                kind=candidate.kind,
                statement=candidate.statement,
                confidence=candidate.confidence,
                evidence_id=evidence_id,
                future_relevance=candidate.future_relevance,
                expires_at=expires_at,
            )
        assert atom is not None
        if (
            atom.status == "observed"
            and candidate.confidence >= 0.85
            and candidate.kind
            in {
                "preference",
                "value",
                "tension",
                "relationship_protocol",
                "open_loop",
                "prospective_intent",
            }
        ):
            atoms = self._load()
            for existing in atoms:
                if existing.id != atom.id:
                    continue
                existing.status = "working_hypothesis"
                existing.updated_at = _now_iso()
                atom = existing
                break
            self._save(atoms)
        return atom

    @locked_memory_method
    def apply_feedback(
        self,
        atom_id: str,
        verdict: Literal["confirmed", "corrected", "ignored"],
    ) -> UnderstandingAtom | None:
        """Apply explicit or interaction feedback through one auditable seam."""

        atoms = self._load()
        updated: UnderstandingAtom | None = None
        for atom in atoms:
            if atom.id != atom_id:
                continue
            if verdict == "confirmed":
                atom.confirmed_hits += 1
                atom.status = "confirmed"
                atom.confidence = max(atom.confidence, 0.95)
            elif verdict == "corrected":
                atom.corrected_hits += 1
                atom.status = "contradicted"
                atom.confidence = 0.0
            else:
                atom.ignored_hits += 1
                atom.confidence = max(0.0, atom.confidence - 0.08)
                if atom.ignored_hits >= 3 and atom.status != "confirmed":
                    atom.status = "expired"
            atom.updated_at = _now_iso()
            updated = atom
            break
        if updated is None:
            return None
        self._save(atoms)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.feedback_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "atom_id": atom_id,
                        "verdict": verdict,
                        "created_at": _now_iso(),
                    },
                    ensure_ascii=False,
                )
                + "\n",
            )
        return updated

    @locked_memory_method
    def invalidate_source(self, source_id: str) -> int:
        """Remove a forgotten source and retire atoms left without evidence."""

        clean_source_id = str(source_id or "").strip()
        if not clean_source_id:
            return 0
        atoms = self._load()
        changed = 0
        for atom in atoms:
            if clean_source_id not in atom.evidence_ids:
                continue
            atom.evidence_ids = tuple(
                item for item in atom.evidence_ids if item != clean_source_id
            )
            if not atom.evidence_ids:
                atom.status = "superseded"
            atom.updated_at = _now_iso()
            changed += 1
        if changed:
            self._save(atoms)
        return changed

    @locked_memory_method
    def association_palette(
        self,
        *,
        max_items: int = 12,
        now: datetime | None = None,
    ) -> tuple[UnderstandingAtom, ...]:
        """Return a bounded private palette for live semantic judgment.

        These items are not authorized for the main answer.  A separate
        association judge must select at most one before it may be surfaced.
        """

        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        eligible: list[UnderstandingAtom] = []
        for atom in self._load():
            if (
                atom.status not in {"confirmed", "working_hypothesis"}
                or atom.sensitivity == "sensitive"
                or atom.kind
                not in {"preference", "value", "fact", "relationship_protocol"}
            ):
                continue
            if atom.expires_at:
                try:
                    expiry = datetime.fromisoformat(
                        atom.expires_at.replace("Z", "+00:00"),
                    )
                except ValueError:
                    expiry = None
                if expiry is not None:
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    if expiry <= current_time:
                        continue
            eligible.append(atom)
        eligible.sort(
            key=lambda atom: (
                atom.status == "confirmed",
                atom.confidence,
                atom.updated_at,
            ),
            reverse=True,
        )
        return tuple(eligible[: max(0, max_items)])

    @locked_memory_method
    def project_for_turn(
        self,
        query: str,
        *,
        now: datetime | None = None,
        max_confirmed: int = 4,
        max_hypotheses: int = 2,
        max_open_loops: int = 3,
    ) -> TurnUnderstandingProjection:
        query_tokens = _tokens(query)
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        def is_expired(atom: UnderstandingAtom) -> bool:
            if not atom.expires_at:
                return False
            try:
                expiry = datetime.fromisoformat(atom.expires_at.replace("Z", "+00:00"))
            except ValueError:
                return False
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry <= current_time

        def score(atom: UnderstandingAtom, relevance: float) -> float:
            atom_tokens = _tokens(atom.statement)
            overlap = len(query_tokens & atom_tokens) / max(len(query_tokens), 1)
            status_bonus = 0.30 if atom.status == "confirmed" else 0.15
            kind_bonus = 0.12 if atom.kind in {"relationship_protocol", "correction"} else 0.0
            return relevance + overlap * 0.15 + status_bonus + kind_bonus + atom.confidence * 0.20

        active: list[tuple[UnderstandingAtom, float]] = []
        for atom in self._load():
            if (
                atom.status not in _ACTIVE_STATUSES
                or atom.sensitivity == "sensitive"
                or is_expired(atom)
            ):
                continue
            recall = assess_memory_recall(
                query=query,
                content=atom.statement,
                memory_kind=atom.kind,
                scope=atom.scope,
            )
            if recall.eligible:
                active.append((atom, score(atom, recall.score)))
        active.sort(key=lambda item: item[1], reverse=True)
        ranked = [atom for atom, _score in active]
        confirmed = tuple(
            atom
            for atom in ranked
            if atom.status == "confirmed" and atom.kind not in {"open_loop", "prospective_intent"}
        )[:max_confirmed]
        hypotheses = tuple(
            atom
            for atom in ranked
            if atom.status == "working_hypothesis"
            and atom.kind not in {"open_loop", "prospective_intent"}
        )[:max_hypotheses]
        open_loops = tuple(
            atom
            for atom in ranked
            if atom.kind in {"open_loop", "prospective_intent"}
        )[:max_open_loops]
        return TurnUnderstandingProjection(
            confirmed=confirmed,
            hypotheses=hypotheses,
            open_loops=open_loops,
        )
