# -*- coding: utf-8 -*-
"""One recall gate for every automatic long-term-memory injection path.

Confirmation answers whether a memory is trustworthy enough to retain.
Recall answers whether that retained memory is applicable to the current turn.
Those decisions are deliberately independent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_LATIN_TOKEN_RE = re.compile(r"[a-z0-9_][a-z0-9_.+-]{1,}", re.I)
_CJK_SEQUENCE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_STANDING_RULE_RE = re.compile(
    r"(?:^|[\s：:])(?:以后|今后|每次|始终|默认|长期规则|务必|必须|不要|不能)|"
    r"\b(?:always|never|by\s+default|every\s+time|must)\b",
    re.I,
)
_PROFILE_QUERY_RE = re.compile(
    r"(?:你了解我|你知道我|关于我|用户画像|我的记忆|"
    r"what\s+do\s+you\s+know\s+about\s+me|my\s+profile)",
    re.I,
)
_STYLE_QUERY_RE = re.compile(r"(?:风格|语气|口吻|表达|怎么写|写法|style|tone)", re.I)
_INSTRUCTION_QUERY_RE = re.compile(
    r"(?:长期规则|默认规则|我的要求|我要求你|我说过不要|"
    r"standing\s+instruction|default\s+rule)",
    re.I,
)
_GENERIC_FEATURES = {
    "一个",
    "这个",
    "那个",
    "用户",
    "当前",
    "现在",
    "需要",
    "应该",
    "可以",
    "进行",
    "回答",
    "输出",
    "事情",
    "什么",
    "怎么",
    "the",
    "and",
    "for",
    "with",
    "user",
}

_CONCEPT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("concept:concern", re.compile(r"(?:担心|顾虑|不放心|心里没底|反复修改|再改|concern|worr)", re.I)),
    ("concept:implicit", re.compile(r"(?:半句话|没说|未说|没有明说|含蓄|言外之意|implicit|unsaid)", re.I)),
    ("concept:family", re.compile(r"(?:家里|家庭|家人|宝宝|新生儿|配偶|family|baby)", re.I)),
    ("concept:work", re.compile(r"(?:工作|岗位|管理|团队|职责|出差|项目|work|team|role)", re.I)),
    ("concept:pressure", re.compile(r"(?:很累|疲惫|压力|迷茫|焦虑|承受|stress|tired|pressure)", re.I)),
    ("concept:release", re.compile(r"(?:发布|上线|发版|公开|release|launch)", re.I)),
    ("concept:quality", re.compile(r"(?:质量|标准|不满意|重新做|重做|quality)", re.I)),
    ("concept:game", re.compile(r"(?:游戏|马里奥|关卡|game|mario)", re.I)),
)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", "", str(value or "").casefold())


def _features(value: str) -> set[str]:
    text = str(value or "").casefold()
    features = {
        token
        for token in _LATIN_TOKEN_RE.findall(text)
        if token not in _GENERIC_FEATURES
    }
    for sequence in _CJK_SEQUENCE_RE.findall(text):
        if len(sequence) == 1:
            continue
        for width in (2, 3):
            features.update(
                sequence[index : index + width]
                for index in range(max(0, len(sequence) - width + 1))
            )
    for concept, pattern in _CONCEPT_PATTERNS:
        if pattern.search(text):
            features.add(concept)
    return {item for item in features if item and item not in _GENERIC_FEATURES}


def is_explicit_standing_rule(content: str, memory_kind: str = "") -> bool:
    """Return true only for text that explicitly says it applies by default."""

    if memory_kind not in {
        "instruction",
        "relationship_protocol",
        "style",
        "preference",
        "workflow",
    }:
        return False
    return bool(_STANDING_RULE_RE.search(str(content or "")))


@dataclass(frozen=True)
class RecallDecision:
    eligible: bool
    score: float
    reason: str
    standing: bool = False


class MemoryRecallPolicy:
    """Deep module separating retained knowledge from turn applicability."""

    def assess(
        self,
        *,
        query: str,
        content: str,
        memory_kind: str = "",
        scope: str = "global",
        semantic_score: float | None = None,
    ) -> RecallDecision:
        clean_query = str(query or "").strip()
        clean_content = str(content or "").strip()
        if not clean_query or not clean_content:
            return RecallDecision(False, 0.0, "empty_query_or_memory")

        normalized_scope = str(scope or "global")
        if normalized_scope == "thread":
            # The live thread already carries its own conversation context.  A
            # persisted thread fact must never follow the user into a new case.
            return RecallDecision(False, 0.0, "thread_scope_requires_live_thread")

        durable_scope = normalized_scope in {"global", "project"}
        standing = durable_scope and is_explicit_standing_rule(
            clean_content,
            memory_kind,
        )
        if standing:
            return RecallDecision(True, 1.0, "explicit_standing_rule", standing=True)

        if durable_scope and _PROFILE_QUERY_RE.search(clean_query) and memory_kind in {
            "identity",
            "fact",
            "style",
            "preference",
            "value",
            "decision_pattern",
            "tension",
        }:
            return RecallDecision(True, 0.9, "explicit_profile_recall")
        if durable_scope and _STYLE_QUERY_RE.search(clean_query) and memory_kind in {"style", "preference"}:
            return RecallDecision(True, 0.85, "explicit_style_recall")
        if durable_scope and _INSTRUCTION_QUERY_RE.search(clean_query) and memory_kind in {
            "instruction",
            "relationship_protocol",
        }:
            return RecallDecision(True, 0.85, "explicit_instruction_recall")
        query_norm = _normalize(clean_query)
        content_norm = _normalize(clean_content)
        if min(len(query_norm), len(content_norm)) >= 4 and (
            query_norm in content_norm or content_norm in query_norm
        ):
            return RecallDecision(True, 1.0, "phrase_match")

        # Semantic recall is evidence of applicability, not authority.  It is
        # evaluated inside the same policy seam and only after scope checks;
        # a vector hit therefore cannot turn raw dialogue into a standing rule.
        if semantic_score is not None and float(semantic_score) >= 0.50:
            return RecallDecision(
                True,
                min(1.0, float(semantic_score)),
                "semantic_relevance",
            )

        query_features = _features(clean_query)
        content_features = _features(clean_content)
        overlap = query_features & content_features
        if not overlap:
            return RecallDecision(False, 0.0, "no_current_turn_relevance")

        coverage = len(overlap) / max(len(query_features), 1)
        precision = len(overlap) / max(len(content_features), 1)
        score = min(1.0, coverage * 0.72 + precision * 0.28 + min(len(overlap), 3) * 0.08)
        return RecallDecision(True, score, "lexical_relevance")


DEFAULT_MEMORY_RECALL_POLICY = MemoryRecallPolicy()


def assess_memory_recall(
    *,
    query: str,
    content: str,
    memory_kind: str = "",
    scope: str = "global",
    semantic_score: float | None = None,
) -> RecallDecision:
    return DEFAULT_MEMORY_RECALL_POLICY.assess(
        query=query,
        content=content,
        memory_kind=memory_kind,
        scope=scope,
        semantic_score=semantic_score,
    )
