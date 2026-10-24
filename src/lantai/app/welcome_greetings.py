# -*- coding: utf-8 -*-
"""Safe, isolated AI candidates for the new-chat empty welcome surface.

This module intentionally accepts only current clock/calendar signals and an
explicit style preference.  It never reads chat history, memory, works, or
task state, and its output is not part of the conversation transcript.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import load_config
from .utility_model import UtilityTextResult, run_utility_text_task


WELCOME_POLICY_VERSION = "welcome-v1.2026-09-27"
_TIME_SCENARIO_KEYS = frozenset(
    {
        "time.dawn",
        "time.morning",
        "time.noon",
        "time.afternoon",
        "time.evening",
        "time.late",
    }
)
_RHYTHM_SCENARIO_KEYS = frozenset(
    {
        "rhythm.week_start",
        "rhythm.week_middle",
        "rhythm.weekend",
        "rhythm.month_start",
        "rhythm.month_end",
    }
)
_CHINA_FESTIVAL_KEYS = frozenset(
    {
        "festival.spring_festival",
        "festival.lantern",
        "festival.dragon_boat",
        "festival.qixi",
        "festival.mid_autumn",
        "festival.lunar_eve",
        "festival.national_day",
        "festival.winter_solstice",
    }
)
_GLOBAL_FESTIVAL_KEYS = frozenset(
    {
        "festival.new_year",
        "festival.christmas",
    }
)
_FESTIVAL_SCENARIO_KEYS = _CHINA_FESTIVAL_KEYS | _GLOBAL_FESTIVAL_KEYS
_ALLOWED_SCENARIO_KEYS = (
    frozenset({"universal"})
    | _TIME_SCENARIO_KEYS
    | _RHYTHM_SCENARIO_KEYS
    | _FESTIVAL_SCENARIO_KEYS
)
_FORBIDDEN_CONTEXT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"上次",
        r"刚才",
        r"接着",
        r"继续.{0,8}(?:做|聊|改|写|推进)",
        r"没(?:有)?做完",
        r"还记得",
        r"你最近",
        r"你这几天",
        r"那份(?:方案|文档|作品|任务)",
        r"此前(?:的|那)",
        r"欢迎回来",
        r"(?:今天|这会儿)?(?:还在|又在)(?:忙|工作)",
        r"(?:熬夜|凌晨|早点休息|辛苦了)",
        r"(?:看起来|似乎|感觉)(?:你|今天)",
        r"你的?(?:状态|心情|进度)",
        r"这次",
        r"沈行知",
    )
)
_FESTIVAL_TEXT_REQUIREMENTS = (
    (re.compile(r"(?:春节|新春)"), frozenset({"festival.spring_festival"})),
    (re.compile(r"除夕"), frozenset({"festival.lunar_eve"})),
    (re.compile(r"元宵"), frozenset({"festival.lantern"})),
    (re.compile(r"端午"), frozenset({"festival.dragon_boat"})),
    (re.compile(r"七夕"), frozenset({"festival.qixi"})),
    (re.compile(r"中秋"), frozenset({"festival.mid_autumn"})),
    (re.compile(r"(?:国庆|十月一日)"), frozenset({"festival.national_day"})),
    (re.compile(r"圣诞"), frozenset({"festival.christmas"})),
    (re.compile(r"冬至"), frozenset({"festival.winter_solstice"})),
    (re.compile(r"元旦"), frozenset({"festival.new_year"})),
    (
        re.compile(r"新年"),
        frozenset({"festival.new_year", "festival.spring_festival"}),
    ),
)


class WelcomeGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locale: Literal["zh-CN"] = "zh-CN"
    local_date: date
    timezone: str = Field(min_length=1, max_length=80)
    scenario_keys: list[str] = Field(min_length=1, max_length=12)
    cultural_calendar: Literal["none", "china", "global", "all"] = "none"
    tone: Literal["natural", "concise", "restrained"] = "natural"
    policy_version: Literal[WELCOME_POLICY_VERSION] = WELCOME_POLICY_VERSION

    @field_validator("scenario_keys")
    @classmethod
    def validate_scenario_keys(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(str(value).strip() for value in values))
        if any(value not in _ALLOWED_SCENARIO_KEYS for value in cleaned):
            raise ValueError("unsupported welcome scenario")
        if "universal" not in cleaned:
            raise ValueError("universal welcome scenario is required")
        if len(_TIME_SCENARIO_KEYS.intersection(cleaned)) != 1:
            raise ValueError("exactly one time welcome scenario is required")
        return cleaned

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_cultural_calendar(self) -> WelcomeGenerationRequest:
        festival_keys = _FESTIVAL_SCENARIO_KEYS.intersection(self.scenario_keys)
        allowed_festival_keys: frozenset[str]
        if self.cultural_calendar == "china":
            allowed_festival_keys = _CHINA_FESTIVAL_KEYS
        elif self.cultural_calendar == "global":
            allowed_festival_keys = _GLOBAL_FESTIVAL_KEYS
        elif self.cultural_calendar == "all":
            allowed_festival_keys = _FESTIVAL_SCENARIO_KEYS
        else:
            allowed_festival_keys = frozenset()
        if not festival_keys.issubset(allowed_festival_keys):
            raise ValueError("festival scenario requires its explicit cultural calendar")
        return self


class AiWelcomeCandidate(BaseModel):
    id: str
    locale: Literal["zh-CN"] = "zh-CN"
    group: str
    title: str
    subtitle: str
    source: Literal["ai"] = "ai"
    version: int = 1
    scope: Literal["new_chat_empty_welcome"] = "new_chat_empty_welcome"
    tone: Literal["natural", "concise", "restrained"]
    canEnterChatHistory: Literal[False] = False
    validFrom: str
    validUntil: str
    scenarioKeys: list[str]
    reviewStatus: Literal["approved"] = "approved"
    policyVersion: Literal[WELCOME_POLICY_VERSION] = WELCOME_POLICY_VERSION


class WelcomeGenerationResponse(BaseModel):
    status: Literal[
        "ready",
        "disabled",
        "unavailable",
        "budget_exhausted",
        "no_safe_candidates",
    ]
    candidates: list[AiWelcomeCandidate] = Field(default_factory=list)
    policy_version: str = WELCOME_POLICY_VERSION


class WelcomeGenerationBudgetStore:
    """Atomic two-batch daily budget, with idempotent batch reservations."""

    _initialization_lock = threading.Lock()

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=30,
        )
        self._connection.execute("PRAGMA busy_timeout=30000")
        # WAL negotiation takes a database-wide lock. Background jobs may
        # construct separate stores at the same time, so serialize same-process
        # initialization and retain SQLite's busy timeout for other processes.
        with self._initialization_lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS welcome_generation_budget (
                    agent_id TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    batch_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (agent_id, local_date, batch_key)
                )
                """
            )

    def reserve(
        self,
        agent_id: str,
        local_date: str,
        batch_key: str,
        *,
        daily_limit: int = 2,
    ) -> bool:
        with self._lock:
            try:
                # A store is opened per background job. BEGIN IMMEDIATE makes
                # the read-count-insert sequence atomic across those separate
                # SQLite connections, not merely within this Python object.
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT 1 FROM welcome_generation_budget "
                    "WHERE agent_id = ? AND local_date = ? AND batch_key = ?",
                    (agent_id, local_date, batch_key),
                ).fetchone()
                if existing is not None:
                    self._connection.commit()
                    return True
                count = self._connection.execute(
                    "SELECT COUNT(*) FROM welcome_generation_budget "
                    "WHERE agent_id = ? AND local_date = ?",
                    (agent_id, local_date),
                ).fetchone()[0]
                if int(count) >= max(0, daily_limit):
                    self._connection.commit()
                    return False
                self._connection.execute(
                    "INSERT INTO welcome_generation_budget "
                    "(agent_id, local_date, batch_key, created_at) VALUES (?, ?, ?, ?)",
                    (agent_id, local_date, batch_key, datetime.now().isoformat()),
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def used(self, agent_id: str, local_date: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM welcome_generation_budget "
                "WHERE agent_id = ? AND local_date = ?",
                (agent_id, local_date),
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def background_processing_enabled() -> bool:
    return bool(
        getattr(load_config().utility_intelligence, "background_processing_enabled", True)
    )


def _json_object(text: str) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _safe_text(title: str, subtitle: str) -> bool:
    title = title.strip()
    subtitle = subtitle.strip()
    if not title or not subtitle or len(title) > 24 or len(subtitle) > 48:
        return False
    joined = f"{title}\n{subtitle}"
    return not any(pattern.search(joined) for pattern in _FORBIDDEN_CONTEXT_PATTERNS)


def _validity_window(request: WelcomeGenerationRequest) -> tuple[str, str]:
    zone = ZoneInfo(request.timezone)
    start = datetime.combine(request.local_date, datetime.min.time(), tzinfo=zone)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _candidate_from_raw(
    raw: Any,
    request: WelcomeGenerationRequest,
) -> AiWelcomeCandidate | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    subtitle = str(raw.get("subtitle") or "").strip()
    if not _safe_text(title, subtitle):
        return None
    raw_scenario_keys = raw.get("scenario_keys", [])
    if not isinstance(raw_scenario_keys, list):
        return None
    scenario_keys = list(dict.fromkeys(str(key) for key in raw_scenario_keys))
    if not scenario_keys or any(key not in request.scenario_keys for key in scenario_keys):
        return None
    text = f"{title}\n{subtitle}"
    if any(
        pattern.search(text) and not allowed_keys.intersection(scenario_keys)
        for pattern, allowed_keys in _FESTIVAL_TEXT_REQUIREMENTS
    ):
        return None
    identifier = str(raw.get("id") or "").strip()
    if not re.fullmatch(r"ai\.[a-zA-Z0-9_.-]{1,80}", identifier):
        digest = hashlib.sha256(f"{title}\0{subtitle}".encode()).hexdigest()[:16]
        identifier = f"ai.generated.{digest}"
    valid_from, valid_until = _validity_window(request)
    return AiWelcomeCandidate(
        id=identifier,
        group=scenario_keys[0],
        title=title,
        subtitle=subtitle,
        tone=request.tone,
        validFrom=valid_from,
        validUntil=valid_until,
        scenarioKeys=scenario_keys,
    )


async def generate_reviewed_welcome_candidates(
    request: WelcomeGenerationRequest,
    *,
    agent_id: str,
) -> WelcomeGenerationResponse:
    """Generate then independently review welcome copy in an isolated channel."""
    if not background_processing_enabled():
        return WelcomeGenerationResponse(status="disabled")

    safe_input = {
        "locale": request.locale,
        "local_date": request.local_date.isoformat(),
        "timezone": request.timezone,
        "scenario_keys": request.scenario_keys,
        "cultural_calendar": request.cultural_calendar,
        "tone": request.tone,
    }
    generated = await run_utility_text_task(
        agent_id=agent_id,
        purpose="welcome-greeting-generate",
        system_prompt=(
            "你为全新的空白对话页写简短开场。它必须让人感到自然、有人味，"
            "同时不得暗示用户的状态、意图、旧任务或任何共同历史。只使用输入中的"
            "日期、时间场景、用户明确选择的文化日历和语气。输出严格 JSON："
            '{"candidates":[{"id":"ai.xxx","title":"不超过24字",'
            '"subtitle":"不超过48字","scenario_keys":["..."]}]}。最多4组。'
        ),
        user_prompt=json.dumps(safe_input, ensure_ascii=False, sort_keys=True),
        max_output_tokens=600,
        usage_session_id=f"welcome:{agent_id}:{request.local_date.isoformat()}",
    )
    if generated is None:
        return WelcomeGenerationResponse(status="unavailable")
    generated_payload = _json_object(generated.text)
    raw_candidates = (
        generated_payload.get("candidates", []) if generated_payload else []
    )
    provisional: list[AiWelcomeCandidate] = []
    seen_ids: set[str] = set()
    seen_copy: set[tuple[str, str]] = set()
    for raw in raw_candidates[:8]:
        candidate = _candidate_from_raw(raw, request)
        if candidate is None:
            continue
        copy_key = (candidate.title, candidate.subtitle)
        if candidate.id in seen_ids or copy_key in seen_copy:
            continue
        seen_ids.add(candidate.id)
        seen_copy.add(copy_key)
        provisional.append(candidate)
    if not provisional:
        return WelcomeGenerationResponse(status="no_safe_candidates")

    review_input = {
        "allowed_signals": safe_input,
        "candidates": [
            {
                "id": candidate.id,
                "title": candidate.title,
                "subtitle": candidate.subtitle,
                "scenario_keys": candidate.scenarioKeys,
            }
            for candidate in provisional
        ],
    }
    reviewed = await run_utility_text_task(
        agent_id=agent_id,
        purpose="welcome-greeting-review",
        system_prompt=(
            "你是开场文案审校员。只批准适合全新空白对话的文案。拒绝任何对旧会话、"
            "未完成事项、作品、近期状态、情绪、熬夜或用户意图的猜测；拒绝催促、"
            "评判、虚构关心和强行续聊。节日只在 allowed_signals 明确包含时可用。"
            '输出严格 JSON：{"approved_ids":["ai.xxx"]}。'
        ),
        user_prompt=json.dumps(review_input, ensure_ascii=False, sort_keys=True),
        max_output_tokens=250,
        usage_session_id=f"welcome-review:{agent_id}:{request.local_date.isoformat()}",
    )
    if reviewed is None:
        return WelcomeGenerationResponse(status="unavailable")
    reviewed_payload = _json_object(reviewed.text) or {}
    approved = {
        str(value) for value in reviewed_payload.get("approved_ids", [])
    }
    candidates = [candidate for candidate in provisional if candidate.id in approved]
    return WelcomeGenerationResponse(
        status="ready" if candidates else "no_safe_candidates",
        candidates=candidates,
    )


__all__ = [
    "AiWelcomeCandidate",
    "UtilityTextResult",
    "WELCOME_POLICY_VERSION",
    "WelcomeGenerationBudgetStore",
    "WelcomeGenerationRequest",
    "WelcomeGenerationResponse",
    "background_processing_enabled",
    "generate_reviewed_welcome_candidates",
]
