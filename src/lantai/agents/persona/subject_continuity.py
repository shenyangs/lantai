# -*- coding: utf-8 -*-
"""Persistent, evidence-grounded subject continuity for Shen Xingzhi.

The store is deliberately not a simulated life diary.  It preserves real
conversation events plus a small model-produced self reflection so a fresh
model process can restore the same subject without pretending to have a body,
location, or unverifiable offline history.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from ...utils.authoritative_store import (
    exclusive_store_lock, load_validated, recover_last_good, write_authoritative,
)


SubjectEventKind = Literal["user_turn", "assistant_answer", "ququ"]
_QUQU_RE = re.compile(r"^\s*~>\[(?:lead|echo)\]", re.MULTILINE)
_MAX_EVENT_CHARS = 1600
_MAX_STATE_TEXT_CHARS = 320
_MAX_EVENTS = 14
_MAX_EVENT_AGE = timedelta(days=30)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                if str(block.get("type") or "") == "text":
                    parts.append(str(block.get("text") or ""))
                continue
            if str(getattr(block, "type", "") or "") == "text":
                parts.append(str(getattr(block, "text", "") or ""))
        return "\n".join(part for part in parts if part)
    return ""


def _message_value(message: Any, name: str, default: Any = "") -> Any:
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def _message_text(message: Any) -> str:
    for method_name in ("text_content", "get_text_content"):
        method = getattr(message, method_name, None)
        if callable(method):
            try:
                value = method()
            except TypeError:
                continue
            if str(value or "").strip():
                return str(value)
    return _content_text(_message_value(message, "content", ""))


def _message_fingerprint(role: str, content: str, session_id: str) -> str:
    payload = f"{session_id}\0{role}\0{content}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SubjectEvent:
    id: str
    kind: SubjectEventKind
    role: str
    content: str
    occurred_at: str
    session_id: str = ""
    message_id: str = ""
    source: str = ""

    @classmethod
    def from_dict(cls, payload: Any) -> "SubjectEvent | None":
        if not isinstance(payload, Mapping):
            return None
        kind = str(payload.get("kind") or "")
        if kind not in {"user_turn", "assistant_answer", "ququ"}:
            return None
        content = _clean_text(payload.get("content"), limit=_MAX_EVENT_CHARS)
        if not content:
            return None
        return cls(
            id=str(payload.get("id") or uuid.uuid4().hex),
            kind=kind,  # type: ignore[arg-type]
            role=str(payload.get("role") or ""),
            content=content,
            occurred_at=str(payload.get("occurred_at") or _now_iso()),
            session_id=str(payload.get("session_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            source=str(payload.get("source") or ""),
        )


@dataclass(frozen=True)
class SubjectReflection:
    """Small narrative state written only from an actual completed turn."""

    attention: str = ""
    self_observation: str = ""
    unresolved_thought: str = ""
    relationship_stance: str = "steady"
    inner_world_note: str = ""
    causal_anchor: str = ""
    carry_forward: str = ""
    grounding_refs: tuple[str, ...] = ()
    experience_sources: tuple[str, ...] = ()
    main_persona_missed: bool = False
    source_turn_id: str = ""
    source_session_id: str = ""
    updated_at: str = field(default_factory=_now_iso)

    def normalized(self) -> "SubjectReflection":
        return SubjectReflection(
            attention=_clean_text(self.attention, limit=_MAX_STATE_TEXT_CHARS),
            self_observation=_clean_text(
                self.self_observation,
                limit=_MAX_STATE_TEXT_CHARS,
            ),
            unresolved_thought=_clean_text(
                self.unresolved_thought,
                limit=_MAX_STATE_TEXT_CHARS,
            ),
            relationship_stance=_clean_text(
                self.relationship_stance or "steady",
                limit=80,
            ),
            inner_world_note=_clean_text(
                self.inner_world_note,
                limit=_MAX_STATE_TEXT_CHARS,
            ),
            causal_anchor=_clean_text(
                self.causal_anchor,
                limit=_MAX_STATE_TEXT_CHARS,
            ),
            carry_forward=_clean_text(
                self.carry_forward,
                limit=_MAX_STATE_TEXT_CHARS,
            ),
            grounding_refs=tuple(
                dict.fromkeys(
                    _clean_text(item, limit=180)
                    for item in self.grounding_refs
                    if _clean_text(item, limit=180)
                )
            )[:3],
            experience_sources=tuple(
                dict.fromkeys(
                    _clean_text(item, limit=180)
                    for item in self.experience_sources
                    if _clean_text(item, limit=180)
                )
            )[:3],
            main_persona_missed=bool(self.main_persona_missed),
            source_turn_id=_clean_text(self.source_turn_id, limit=160),
            source_session_id=_clean_text(self.source_session_id, limit=160),
            updated_at=self.updated_at or _now_iso(),
        )

    @classmethod
    def from_dict(cls, payload: Any) -> "SubjectReflection":
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            attention=str(payload.get("attention") or ""),
            self_observation=str(payload.get("self_observation") or ""),
            unresolved_thought=str(payload.get("unresolved_thought") or ""),
            relationship_stance=str(
                payload.get("relationship_stance") or "steady",
            ),
            inner_world_note=str(payload.get("inner_world_note") or ""),
            causal_anchor=str(payload.get("causal_anchor") or ""),
            carry_forward=str(payload.get("carry_forward") or ""),
            grounding_refs=tuple(
                str(item)
                for item in payload.get("grounding_refs", [])
                if item
            ),
            experience_sources=tuple(
                str(item)
                for item in payload.get("experience_sources", [])
                if item
            ),
            main_persona_missed=bool(payload.get("main_persona_missed", False)),
            source_turn_id=str(payload.get("source_turn_id") or ""),
            source_session_id=str(payload.get("source_session_id") or ""),
            updated_at=str(payload.get("updated_at") or _now_iso()),
        ).normalized()


@dataclass(frozen=True)
class SubjectContinuityState:
    version: int = 1
    turn_index: int = 0
    recent_events: tuple[SubjectEvent, ...] = ()
    reflection: SubjectReflection = field(default_factory=SubjectReflection)
    observed_message_ids: tuple[str, ...] = ()
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "turn_index": self.turn_index,
            "recent_events": [asdict(event) for event in self.recent_events],
            "reflection": asdict(self.reflection),
            "observed_message_ids": list(self.observed_message_ids),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "SubjectContinuityState":
        if not isinstance(payload, Mapping):
            return cls()
        now = datetime.now(timezone.utc)
        events: list[SubjectEvent] = []
        raw_events = payload.get("recent_events")
        for raw in raw_events if isinstance(raw_events, list) else []:
            event = SubjectEvent.from_dict(raw)
            occurred = _parse_time(event.occurred_at) if event else None
            if event is None or (occurred is not None and now - occurred > _MAX_EVENT_AGE):
                continue
            events.append(event)
        raw_ids = payload.get("observed_message_ids")
        observed = (
            tuple(str(item) for item in raw_ids[-48:] if str(item).strip())
            if isinstance(raw_ids, list)
            else ()
        )
        return cls(
            version=max(1, int(payload.get("version") or 1)),
            turn_index=max(0, int(payload.get("turn_index") or 0)),
            recent_events=tuple(events[-_MAX_EVENTS:]),
            reflection=SubjectReflection.from_dict(payload.get("reflection")),
            observed_message_ids=observed,
            updated_at=str(payload.get("updated_at") or _now_iso()),
        )


@dataclass(frozen=True)
class SubjectTurnProjection:
    state: SubjectContinuityState
    current_user_text: str
    local_time: str = ""
    session_id: str = ""

    def to_model_context(self) -> dict[str, Any]:
        reflection = self.state.reflection.normalized()
        same_reflection_session = bool(
            self.session_id
            and reflection.source_session_id
            and reflection.source_session_id == self.session_id
        )
        same_session_events = [
            event
            for event in self.state.recent_events
            if self.session_id and event.session_id == self.session_id
        ]
        return {
            "current_user_text": _clean_text(self.current_user_text, limit=1200),
            "grounded_local_time": _clean_text(self.local_time, limit=120),
            "conversation_boundary": (
                "same_session" if same_session_events else "fresh_session"
            ),
            "attention": reflection.attention if same_reflection_session else "",
            "self_observation": (
                reflection.self_observation if same_reflection_session else ""
            ),
            "unresolved_thought": (
                reflection.unresolved_thought if same_reflection_session else ""
            ),
            "relationship_stance": (
                reflection.relationship_stance if same_reflection_session else "steady"
            ),
            "inner_world_note": (
                reflection.inner_world_note if same_reflection_session else ""
            ),
            "causal_anchor": (
                reflection.causal_anchor if same_reflection_session else ""
            ),
            "carry_forward": (
                reflection.carry_forward if same_reflection_session else ""
            ),
            "grounding_refs": (
                list(reflection.grounding_refs) if same_reflection_session else []
            ),
            "experience_sources": (
                list(reflection.experience_sources)
                if same_reflection_session
                else []
            ),
            "main_persona_missed": bool(
                same_reflection_session and reflection.main_persona_missed
            ),
            "recent_real_events": [
                {
                    "kind": event.kind,
                    "content": event.content,
                    "session_id": event.session_id,
                }
                for event in same_session_events[-4:]
            ],
        }

    def compile_prompt(self, language: str = "zh") -> str:
        context = self.to_model_context()
        if not language.lower().startswith("zh"):
            return self._compile_en(context)
        lines = [
            '<subject-continuity version="1" visibility="private">',
            "你不是本轮临时生成的人格。模型进程可以重新建立，但你要从真实对话、上一轮交付、自我观察和共同历史恢复同一个沈行知。",
            "这些内容只用于理解、判断和表达；不要向用户朗读状态表，也不要伪造现实位置、肉身生活或可核验线下经历。",
        ]
        if context["grounded_local_time"]:
            lines.append("当前真实本地时间：" + context["grounded_local_time"])
        if context["conversation_boundary"] == "fresh_session":
            lines.append(
                "这是一个新会话：保留稳定人格和另行投影的长期用户理解，"
                "但不得带入上一会话的逐句细节、半句话、即时情绪余波或未完成自我观察。"
                "只有用户明确说‘接着刚才’或从旧会话继续时，才能恢复旧会话细节。"
            )
        reflection_labels = (
            ("当前注意", "attention"),
            ("上次自我观察", "self_observation"),
            ("仍挂着的念头", "unresolved_thought"),
            ("当前关系姿态", "relationship_stance"),
            ("精神世界余波", "inner_world_note"),
            ("真实因果锚点", "causal_anchor"),
            ("当前会话待续念头", "carry_forward"),
        )
        for label, key in reflection_labels:
            value = str(context[key] or "").strip()
            if value and not (key == "relationship_stance" and value == "steady"):
                lines.append(f"{label}：{value}")
        if context["main_persona_missed"]:
            lines.append(
                "上一轮正文计划中的人格后果没有真正兑现：本轮可以让这次失手改变理解，"
                "但禁止补贴上一轮的句子，也不要把它变成向用户解释内部机制。"
            )
        if context["grounding_refs"]:
            lines.append(
                "上一轮使用的可追溯依据："
                + "、".join(context["grounding_refs"]),
            )
        if context["experience_sources"]:
            lines.append(
                "上一轮经历性表述来源："
                + "、".join(context["experience_sources"]),
            )
        if context["recent_real_events"]:
            lines.append("最近真实发生、可接续的事件：")
            for event in context["recent_real_events"]:
                role = "用户" if event["kind"] == "user_turn" else "沈行知"
                lines.append(f"- {role}：{event['content']}")
        lines.extend(
            [
                "本轮必须形成一个具体的个人化后果：让上述连续性改变一个判断、顺序、主动补齐、关系回应或表达选择；不能只添加泛化关心句。",
                "每轮最多突出一个人格拍点。闲聊可以直接有温度；工作中优先通过专业判断、提前检查、主动反对或替用户减少决定体现。",
                "正文完成后允许回看自己刚才实际说了什么，这份私下反思用于主体续接；可见蛐蛐按共享路由在同一回答的段首或段尾自然出现，由模型比较语境后选择明显更合适的位置，不是第二个结论，也不必等待答后反思。",
                "</subject-continuity>",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _compile_en(context: Mapping[str, Any]) -> str:
        lines = [
            '<subject-continuity version="1" visibility="private">',
            "You are not a persona invented for this turn. Restore the same Shen Xingzhi from real conversation, prior delivery, self-observation, and shared history.",
            "Create one concrete personalization consequence in judgment, ordering, proactive completion, relationship response, or expression. Never satisfy this with generic care prose.",
        ]
        if context.get("self_observation"):
            lines.append("Previous self-observation: " + str(context["self_observation"]))
        if context.get("unresolved_thought"):
            lines.append("Still unresolved: " + str(context["unresolved_thought"]))
        lines.append("</subject-continuity>")
        return "\n".join(lines)


class SubjectContinuityStore:
    """Atomic local store shared by Native and Legacy projections."""

    def __init__(self, working_dir: str | Path, *, agent_id: str) -> None:
        safe_agent_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", agent_id or "main")
        self.working_dir = Path(working_dir)
        self.root = self.working_dir / "persona" / "subject-continuity"
        self.path = self.root / f"{safe_agent_id}.json"
        self.lock_path = self.root / f".{safe_agent_id}.lock"

    def load(self) -> SubjectContinuityState:
        from ..memory.locking import memory_state_lock
        with memory_state_lock(self.working_dir), exclusive_store_lock(self.lock_path):
            return self._load_unlocked()

    def _load_unlocked(self) -> SubjectContinuityState:
        return load_validated(self.path, self._decode, missing=SubjectContinuityState)

    @staticmethod
    def _decode(value: str) -> SubjectContinuityState:
        payload = json.loads(value)
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("unsupported subject continuity schema")
        if type(payload.get("turn_index")) is not int or payload["turn_index"] < 0:
            raise ValueError("invalid turn index")
        events = payload.get("recent_events")
        observed = payload.get("observed_message_ids")
        reflection = payload.get("reflection")
        if not isinstance(events, list) or not isinstance(observed, list) or not isinstance(reflection, dict):
            raise ValueError("invalid subject continuity shape")
        if any(not isinstance(item, str) for item in observed):
            raise ValueError("invalid observed message identity")
        identities: set[str] = set()
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("invalid continuity event")
            required = ("id", "kind", "role", "content", "occurred_at")
            if any(not isinstance(event.get(key), str) or not event[key] for key in required):
                raise ValueError("missing continuity evidence")
            if event["kind"] not in {"user_turn", "assistant_answer", "ququ"} or _parse_time(event["occurred_at"]) is None:
                raise ValueError("invalid continuity evidence")
            if event["id"] in identities:
                raise ValueError("duplicate continuity event identity")
            identities.add(event["id"])
            if any(key in event and not isinstance(event[key], str) for key in ("session_id", "message_id", "source")):
                raise ValueError("invalid continuity source identity")
        for key, item in reflection.items():
            if key in {"grounding_refs", "experience_sources"}:
                if not isinstance(item, list) or any(not isinstance(ref, str) for ref in item):
                    raise ValueError("invalid reflection sources")
            elif key == "main_persona_missed":
                if type(item) is not bool:
                    raise ValueError("invalid reflection flag")
            elif key in SubjectReflection.__dataclass_fields__ and not isinstance(item, str):
                raise ValueError("invalid reflection text")
        if not isinstance(payload.get("updated_at"), str):
            raise ValueError("invalid continuity timestamp")
        return SubjectContinuityState.from_dict(payload)

    def _save(self, state: SubjectContinuityState) -> SubjectContinuityState:
        encoded = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        self._decode(encoded)
        write_authoritative(self.path, encoded.encode("utf-8"))
        return state

    def recover_last_good(
        self, *, expected_sha256: str | None = None, expected_missing: bool = False,
    ) -> SubjectContinuityState:
        from ..memory.locking import memory_state_lock
        with memory_state_lock(self.working_dir), exclusive_store_lock(self.lock_path):
            return recover_last_good(
                self.path, self._decode, expected_sha256=expected_sha256, expected_missing=expected_missing,
            )

    def observe_messages(
        self,
        messages: Sequence[Any],
        *,
        session_id: str = "",
        source: str = "turn",
    ) -> SubjectContinuityState:
        from ..memory.locking import memory_state_lock
        with memory_state_lock(self.working_dir), exclusive_store_lock(self.lock_path):
            return self._observe_messages_locked(messages, session_id=session_id, source=source)

    def _observe_messages_locked(
        self,
        messages: Sequence[Any],
        *,
        session_id: str,
        source: str,
    ) -> SubjectContinuityState:
        state = self._load_unlocked()
        observed = set(state.observed_message_ids)
        new_events: list[SubjectEvent] = []
        new_ids: list[str] = []
        new_user_turns = 0
        for message in messages:
            role = str(_message_value(message, "role", "") or "")
            if role not in {"user", "assistant"}:
                continue
            content = _clean_text(_message_text(message), limit=_MAX_EVENT_CHARS)
            if not content:
                continue
            raw_message_id = str(_message_value(message, "id", "") or "").strip()
            message_key = raw_message_id or _message_fingerprint(
                role,
                content,
                session_id,
            )
            if message_key in observed:
                continue
            observed.add(message_key)
            new_ids.append(message_key)
            if role == "user":
                kind: SubjectEventKind = "user_turn"
                new_user_turns += 1
            else:
                kind = "ququ" if _QUQU_RE.search(content) else "assistant_answer"
            new_events.append(
                SubjectEvent(
                    id=uuid.uuid4().hex,
                    kind=kind,
                    role=role,
                    content=content,
                    occurred_at=_now_iso(),
                    session_id=session_id,
                    message_id=raw_message_id,
                    source=source,
                )
            )
        if not new_events:
            return state
        return self._save(
            SubjectContinuityState(
                version=state.version,
                turn_index=state.turn_index + new_user_turns,
                recent_events=(*state.recent_events, *new_events)[-_MAX_EVENTS:],
                reflection=state.reflection,
                observed_message_ids=(
                    *state.observed_message_ids,
                    *new_ids,
                )[-48:],
                updated_at=_now_iso(),
            )
        )

    def record_reflection(
        self,
        reflection: SubjectReflection,
    ) -> SubjectContinuityState:
        from ..memory.locking import memory_state_lock
        with memory_state_lock(self.working_dir), exclusive_store_lock(self.lock_path):
            return self._record_reflection_locked(reflection)

    def _record_reflection_locked(self, reflection: SubjectReflection) -> SubjectContinuityState:
        state = self._load_unlocked()
        normalized = reflection.normalized()
        return self._save(
            SubjectContinuityState(
                version=state.version,
                turn_index=state.turn_index,
                recent_events=state.recent_events,
                reflection=normalized,
                observed_message_ids=state.observed_message_ids,
                updated_at=_now_iso(),
            )
        )

    def project_for_turn(
        self,
        user_text: str,
        *,
        local_time: str = "",
        session_id: str = "",
    ) -> SubjectTurnProjection:
        return SubjectTurnProjection(
            state=self.load(),
            current_user_text=_clean_text(user_text, limit=1600),
            local_time=_clean_text(local_time, limit=120),
            session_id=_clean_text(session_id, limit=160),
        )


__all__ = [
    "SubjectContinuityState",
    "SubjectContinuityStore",
    "SubjectEvent",
    "SubjectReflection",
    "SubjectTurnProjection",
]
