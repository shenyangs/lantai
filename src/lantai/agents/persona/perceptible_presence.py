# -*- coding: utf-8 -*-
"""Turn contracts for personality that is observable in the final answer."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..memory_recall_policy import assess_memory_recall
from .user_understanding import TurnUnderstandingProjection, UnderstandingAtom
from .subject_continuity import SubjectTurnProjection

ConversationMode = Literal["casual", "mixed", "work", "high_stakes"]
PersonaBeat = Literal[
    "relationship_echo",
    "subjective_notice",
    "professional_judgment",
    "quiet",
]
PersonaMode = Literal["full", "balanced", "quiet", "off"]
ExpressionSurface = Literal[
    "main_answer",
    "work_method",
    "relationship_reply",
    "ququ",
    "multiple",
    "quiet",
]

_HIGH_STAKES_RE = re.compile(
    r"(线上|生产环境|数据库).{0,16}(宕机|故障|丢数据|泄露|崩|止血)|"
    r"(紧急止血|数据泄露|事故|sev[- ]?[01]|production.{0,12}(down|outage))",
    re.I,
)
_WORK_RE = re.compile(
    r"((?:帮我|请).{0,12}(改|做|查|修|写|整理|总结|分析|评估|比较|设计|规划|实现|排查|调试)|"
    r"把.{0,16}(改|做|查|修|写|整理|总结)|"
    r"方案|架构|代码|项目|任务|文件|测试|部署|发布|优化|实现|"
    r"分析|评估|比较|选择|建议|排查|调试|风险|上线|"
    r"\b(?:debug|fix|build|write|analyse|analyze|review|deploy|release)\b)",
    re.I,
)
_JUDGMENT_WORK_RE = re.compile(
    r"(方案|架构|风险|取舍|选择|应该|值不值得|合理|优先|"
    r"为什么|分析|评估|建议|strategy|trade-?off|risk|review)",
    re.I,
)
_PERSONAL_RE = re.compile(
    r"(我|你|今天|昨晚|凌晨|半夜|累|困|难过|开心|生气|怀念|"
    r"喜欢|讨厌|下雨|音乐|歌|大学|年轻|生活|睡|吃|喝|"
    r"I\b|you\b|today|tonight|tired|sad|happy|miss|remember|music|rain)",
    re.I,
)
_SUBJECTIVE_SIGNAL_RE = re.compile(
    r"(我在|我来|我会|我先|我想|我觉得|我更|我很|我有点|"
    r"我不|我倒|我忽然|我还|我替你|我听|我看|让我|我这边|"
    r"我也|说真的|坦白说|你.{0,10}(是不是|还好吗|先别|别再|要不要|愿意)|"
    r"\bI\s+(?:am|think|feel|want|would|will|can|cannot|can't|don't|do not)|"
    r"\blet me\b|\bI(?:'m| am) here\b)",
    re.I,
)
_PROFESSIONAL_SIGNAL_RE = re.compile(
    r"(我(认为|判断|建议|更倾向|支持|赞成|认同|反对|坚持|"
    r"不赞成|不认同|不同意|不能接受|接手|担心|会先|先|来|替你|主张|选择)|"
    r"我的建议|我的判断|我的结论|"
    r"\bI\s+(?:think|recommend|would|will|disagree|object|prefer|judge)|"
    r"\bmy\s+(?:recommendation|judgment|call|view)\b)",
    re.I,
)
_RELATIONAL_SIGNAL_RE = re.compile(
    r"(我|你|我们|咱们|记得|想到|想起|上次|以前|那时|会不会|"
    r"是不是|吗|？|\bI\b|\byou\b|\bwe\b|remember|remind)",
    re.I,
)
_QUQU_VISIBLE_RE = re.compile(r"^\s*~>\[(?:lead|echo)\].*$", re.MULTILINE)
_GENERIC_MEMORY_ANCHORS = frozenset(
    {
        "用户",
        "喜欢",
        "偏好",
        "认为",
        "经常",
        "曾经",
        "这个",
        "那个",
        "的歌",
        "听歌",
        "事情",
        "user",
        "likes",
        "prefers",
    }
)


@dataclass(frozen=True)
class PresenceState:
    """Small session state that bounds personality-silent streaks."""

    turn_index: int = 0
    turns_since_visible: int = 0

    @classmethod
    def from_dict(cls, payload: Any) -> "PresenceState":
        if not isinstance(payload, dict):
            return cls()
        return cls(
            turn_index=max(0, int(payload.get("turn_index") or 0)),
            turns_since_visible=max(0, int(payload.get("turns_since_visible") or 0)),
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class PersonaMemoryReference:
    """Traceable user-understanding evidence approved for one persona beat."""

    source: str
    citation: str
    content: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "citation": self.citation,
            "content": self.content,
            "score": round(max(0.0, min(float(self.score), 1.0)), 4),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "PersonaMemoryReference | None":
        if not isinstance(payload, dict):
            return None
        content = str(payload.get("content") or "").strip()
        citation = str(payload.get("citation") or "").strip()
        if not content or not citation:
            return None
        try:
            score = float(payload.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        return cls(
            source=str(payload.get("source") or "user-understanding").strip(),
            citation=citation,
            content=content,
            score=max(0.0, min(score, 1.0)),
        )


@dataclass(frozen=True)
class PersonaTurnPlan:
    conversation_mode: ConversationMode
    beat: PersonaBeat
    require_visible: bool
    memory_statements: tuple[str, ...]
    association_hints: tuple[str, ...]
    reason: str
    next_state: PresenceState
    memory_references: tuple[PersonaMemoryReference, ...] = ()
    ququ_independent: bool = True
    latent_need: str = ""
    subjective_attention: str = ""
    relationship_posture: str = ""
    personalization_consequence: str = ""
    subject_context: dict[str, Any] = field(default_factory=dict)
    ququ_bypass_worthy: bool = False
    causal_anchor: str = ""
    selected_memory_ids: tuple[str, ...] = ()
    expression_surface: ExpressionSurface = "quiet"
    self_implication: str = ""
    carry_forward: str = ""
    experience_sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["memory_statements"] = list(self.memory_statements)
        payload["association_hints"] = list(self.association_hints)
        payload["selected_memory_ids"] = list(self.selected_memory_ids)
        payload["experience_sources"] = list(self.experience_sources)
        payload["memory_references"] = [
            reference.to_dict() for reference in self.memory_references
        ]
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "PersonaTurnPlan | None":
        if not isinstance(payload, dict):
            return None
        mode = str(payload.get("conversation_mode") or "work")
        beat = str(payload.get("beat") or "quiet")
        if mode not in {"casual", "mixed", "work", "high_stakes"}:
            return None
        if beat not in {
            "relationship_echo",
            "subjective_notice",
            "professional_judgment",
            "quiet",
        }:
            return None
        memory_references = tuple(
            reference
            for reference in (
                PersonaMemoryReference.from_dict(item)
                for item in payload.get("memory_references", [])
            )
            if reference is not None
        )
        return cls(
            conversation_mode=mode,  # type: ignore[arg-type]
            beat=beat,  # type: ignore[arg-type]
            require_visible=bool(payload.get("require_visible")),
            memory_statements=tuple(
                str(item) for item in payload.get("memory_statements", []) if item
            ),
            association_hints=tuple(
                str(item) for item in payload.get("association_hints", []) if item
            ),
            reason=str(payload.get("reason") or ""),
            next_state=PresenceState.from_dict(payload.get("next_state")),
            memory_references=memory_references,
            ququ_independent=bool(payload.get("ququ_independent", True)),
            latent_need=str(payload.get("latent_need") or "")[:400],
            subjective_attention=str(
                payload.get("subjective_attention") or "",
            )[:400],
            relationship_posture=str(
                payload.get("relationship_posture") or "",
            )[:120],
            personalization_consequence=str(
                payload.get("personalization_consequence") or "",
            )[:500],
            subject_context=(
                dict(payload.get("subject_context") or {})
                if isinstance(payload.get("subject_context"), dict)
                else {}
            ),
            ququ_bypass_worthy=bool(payload.get("ququ_bypass_worthy", False)),
            causal_anchor=str(payload.get("causal_anchor") or "")[:500],
            selected_memory_ids=tuple(
                str(item)
                for item in payload.get("selected_memory_ids", [])
                if item
            )[:1],
            expression_surface=(
                str(payload.get("expression_surface") or "quiet")
                if str(payload.get("expression_surface") or "quiet")
                in {
                    "main_answer",
                    "work_method",
                    "relationship_reply",
                    "ququ",
                    "multiple",
                    "quiet",
                }
                else "quiet"
            ),  # type: ignore[arg-type]
            self_implication=str(payload.get("self_implication") or "")[:400],
            carry_forward=str(payload.get("carry_forward") or "")[:400],
            experience_sources=tuple(
                str(item)
                for item in payload.get("experience_sources", [])
                if item
            )[:3],
        )

    def compile_prompt(self, language: str = "zh") -> str:
        # A per-turn contract is useful only when the model selected a concrete
        # consequence. Full mode normally requires one; quieter modes may rely
        # on the static canon without manufacturing lore or generic care prose.
        if not self.require_visible and not self.memory_statements:
            return ""
        if not language.lower().startswith("zh"):
            return self._compile_en()
        requirement = "本轮必须兑现" if self.require_visible else "本轮在自然时兑现"
        lines = [
            '<perceptible-presence version="3.0" '
            f'mode="{self.conversation_mode}" beat="{self.beat}" '
            f'required="{str(self.require_visible).lower()}">',
            f"{requirement}一个正文人格动作。它属于正常回答，不加标签，不写成套话，也不能只放进蛐蛐。",
        ]
        if self.latent_need:
            lines.append(
                "本轮最可能改变帮助方式的未言明需要（是模型判断，不是用户事实）："
                + self.latent_need,
            )
        if self.subjective_attention:
            lines.append("沈行知此刻真正注意到：" + self.subjective_attention)
        if self.relationship_posture:
            lines.append("本轮关系位置：" + self.relationship_posture)
        if self.personalization_consequence:
            lines.append(
                "本轮必须真正发生的个人化后果："
                + self.personalization_consequence,
            )
        if self.causal_anchor:
            lines.append("这次人格动作的真实因果锚点：" + self.causal_anchor)
        lines.append("最合适的显现出口：" + self.expression_surface)
        if self.self_implication:
            lines.append("这件事对沈行知自己的牵动：" + self.self_implication)
        if self.carry_forward:
            lines.append("只在当前会话内部延续的未完念头：" + self.carry_forward)
        if self.experience_sources:
            lines.append(
                "经历性表述只可依据这些来源："
                + "、".join(self.experience_sources),
            )
        if self.beat == "relationship_echo":
            lines.extend(
                [
                    "把当前情境与至多一条可信关系记忆自然接起来，让用户感到这是延续中的同一段关系。",
                    "允许调用自己确信的可靠常识形成文化或生活联想；不确定时可以用问题试探，不得把猜测说成共同经历或用户事实。",
                    "关系回声最多一两句：点到具体联系就收住，不引用歌词，不继续替用户虚构校园、人物或心理故事；把谈话还给用户。",
                ]
            )
        elif self.beat == "subjective_notice":
            lines.append(
                "主动注意当前话语里最有人的那个细节，用一两句真实的好奇、感受、关心或轻微联想回应，再继续谈正事。",
            )
        elif self.beat == "professional_judgment":
            lines.append(
                "人格放在专业判断里：明确说出你认为最重要的取舍、风险或下一步，并替用户减少至少一个本可避免的决定。",
            )
        else:
            lines.append(
                "本轮可以安静，以准确、克制和不打扰体现人格；如果当前不是任务，"
                "不要用‘我判断’、复盘、收尾、测试或下一步把聊天硬转成工作。"
            )
        if self.conversation_mode == "high_stakes":
            lines.append("这是高风险工作：不要插入生活闲聊；一句接手或判断后立刻先恢复、止血或保护证据。")
        elif self.conversation_mode == "work":
            lines.append("不要拿音乐、天气或泛化关心装饰任务；沈行知的存在感应来自判断、担当和贴合请求的回应。")
        if self.memory_statements:
            lines.append("本轮获准使用的可信记忆（最多选一条）：")
            lines.extend(f"- {item}" for item in self.memory_statements[:1])
        if self.association_hints:
            lines.append("这条记忆过去标注的适用情境（只是检索钩子，不是用户事实）：")
            lines.extend(f"- {item}" for item in self.association_hints[:1])
        lines.extend(
            [
                "聊天本身是正当用途：生活、玩笑、喜欢、难过或分享日常时，"
                "不要强行制造任务、产物、复盘或行动建议；用户没有要求继续时允许自然收住。",
                "蛐蛐是独立通道；若本轮蛐蛐路由命中，必须另行遵守，不得拿正文人格动作抵扣蛐蛐。",
                "主体答案、事实标准与回应完整度不因人格动作而降低。",
                "</perceptible-presence>",
            ]
        )
        return "\n".join(lines)

    def _compile_en(self) -> str:
        requirement = "must" if self.require_visible else "may"
        lines = [
            '<perceptible-presence version="3.0" '
            f'mode="{self.conversation_mode}" beat="{self.beat}" '
            f'required="{str(self.require_visible).lower()}">',
            f"This turn {requirement} contain one natural persona action in the main answer, without a label or stock phrase.",
            "In casual conversation, connect one grounded memory only when it fits. In work, show personality through judgment, ownership, and reduced user effort.",
            "Conversation is itself a valid use: do not force ordinary life, humor, affection, grief, or an already complete exchange into a task, retrospective, test, or next action.",
        ]
        if self.memory_statements:
            lines.append("Approved relevant memory: " + self.memory_statements[0])
        lines.extend(
            [
                "Ququ is independent and cannot be replaced by this main-answer action.",
            "Do not reduce factual accuracy or response quality.",
                "</perceptible-presence>",
            ]
        )
        return "\n".join(lines)

    def fallback_lead(self, language: str = "zh") -> str:
        """Compatibility seam; fixed local persona prose is forbidden in V4."""

        del language
        return ""


@dataclass(frozen=True)
class PersonaDeliveryReport:
    """Auditable result of checking only the final user-visible answer."""

    beat: PersonaBeat
    required: bool
    delivered: bool
    signal: str

    def to_dict(
        self,
        *,
        attempts: int,
        runtime: str,
        fallback_guard: bool = False,
    ) -> dict[str, Any]:
        return {
            "beat": self.beat,
            "required": self.required,
            "delivered": self.delivered,
            "signal": self.signal,
            "attempts": max(0, int(attempts)),
            "runtime": runtime,
            "fallback_guard": bool(fallback_guard),
        }


def _memory_anchors(statement: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", statement).casefold()
    anchors: set[str] = set()
    for word in re.findall(r"[a-z0-9][a-z0-9._+-]{1,}", normalized):
        if word not in _GENERIC_MEMORY_ANCHORS:
            anchors.add(word)
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        for width in range(2, min(6, len(segment)) + 1):
            for start in range(0, len(segment) - width + 1):
                candidate = segment[start : start + width]
                if candidate not in _GENERIC_MEMORY_ANCHORS:
                    anchors.add(candidate)
    return tuple(sorted(anchors, key=lambda item: (-len(item), item)))


def evaluate_persona_delivery(
    plan: PersonaTurnPlan,
    final_text: str,
) -> PersonaDeliveryReport:
    """Check the required beat in visible prose; Ququ never counts."""

    body = _QUQU_VISIBLE_RE.sub("", str(final_text or "")).strip()
    if not plan.require_visible:
        return PersonaDeliveryReport(
            beat=plan.beat,
            required=False,
            delivered=True,
            signal="not_required",
        )
    delivered = False
    signal = f"missing_{plan.beat}"
    if plan.beat == "relationship_echo":
        normalized_body = re.sub(r"\s+", "", body).casefold()
        anchors = {
            anchor
            for statement in plan.memory_statements
            for anchor in _memory_anchors(statement)
        }
        grounded = bool(
            anchors
            and any(anchor in normalized_body for anchor in anchors)
        )
        delivered = grounded and bool(_RELATIONAL_SIGNAL_RE.search(body))
        if delivered:
            signal = "grounded_memory_echo"
    elif plan.beat == "subjective_notice":
        delivered = bool(_SUBJECTIVE_SIGNAL_RE.search(body))
        if delivered:
            signal = "subjective_notice"
    elif plan.beat == "professional_judgment":
        delivered = bool(_PROFESSIONAL_SIGNAL_RE.search(body))
        if delivered:
            signal = "professional_judgment"
    else:
        delivered = True
        signal = "intentional_quiet"
    return PersonaDeliveryReport(
        beat=plan.beat,
        required=True,
        delivered=delivered,
        signal=signal,
    )


def _relationship_memory(
    projection: TurnUnderstandingProjection,
    query: str,
) -> UnderstandingAtom | None:
    for item in projection.confirmed:
        if item.kind not in {"preference", "value", "fact", "relationship_protocol"}:
            continue
        recall = assess_memory_recall(
            query=query,
            content=" ".join(
                part for part in (item.statement, item.future_relevance) if part
            ),
            memory_kind=item.kind,
            scope=item.scope,
        )
        if recall.eligible:
            return item
    return None


def plan_persona_turn(
    user_text: str,
    *,
    projection: TurnUnderstandingProjection,
    state: PresenceState,
    persona_mode: PersonaMode = "full",
    intent_decision: Any | None = None,
    subject_projection: SubjectTurnProjection | None = None,
) -> PersonaTurnPlan:
    """Choose one visible action, or intentional silence, for the turn."""

    text = re.sub(r"\s+", " ", str(user_text or "")).strip()
    model_classified = bool(
        intent_decision is not None
        and getattr(intent_decision, "status", "") == "classified"
    )
    model_unavailable = bool(
        intent_decision is not None and not model_classified
    )
    high_stakes = bool(
        getattr(intent_decision, "conversation_mode", "") == "high_stakes"
        if model_classified
        else (_HIGH_STAKES_RE.search(text) if not model_unavailable else False)
    )
    work = bool(
        getattr(intent_decision, "conversation_mode", "")
        in {"work", "mixed", "high_stakes"}
        if model_classified
        else (_WORK_RE.search(text) if not model_unavailable else False)
    )
    personal = bool(
        getattr(intent_decision, "conversation_mode", "")
        in {"casual", "mixed"}
        if model_classified
        else (_PERSONAL_RE.search(text) if not model_unavailable else False)
    )
    if model_classified:
        conversation_mode = getattr(intent_decision, "conversation_mode", "mixed")
    elif model_unavailable:
        conversation_mode = "mixed"
    elif high_stakes:
        conversation_mode: ConversationMode = "high_stakes"
    elif work and personal:
        conversation_mode = "mixed"
    elif work:
        conversation_mode = "work"
    else:
        conversation_mode = "casual"
    selected_memory_ids = tuple(
        str(item)
        for item in getattr(intent_decision, "selected_memory_ids", ())
        if str(item).strip()
    )[:1]
    selected_memory = (
        projection.candidate_by_id(selected_memory_ids[0])
        if selected_memory_ids
        else None
    )
    relationship_memory = (
        selected_memory
        if model_classified and not high_stakes
        else (
            _relationship_memory(projection, text)
            if not model_classified
            and not high_stakes
            and persona_mode in {"full", "balanced"}
            else None
        )
    )
    beat: PersonaBeat = "quiet"
    required = False
    reason = "intentional_silence"
    if persona_mode == "off":
        reason = "persona_disabled"
    elif model_classified:
        candidate_beat = str(getattr(intent_decision, "persona_beat", "quiet"))
        if candidate_beat == "relationship_echo" and relationship_memory is None:
            candidate_beat = "subjective_notice"
        beat = candidate_beat  # type: ignore[assignment]
        expression_surface = str(
            getattr(intent_decision, "expression_surface", "quiet") or "quiet",
        )
        required = bool(
            getattr(intent_decision, "persona_required", False)
            and expression_surface != "quiet"
        )
        reason = "auxiliary_model_turn_judgment"
    elif model_unavailable:
        reason = "primary_model_discretion"
    elif high_stakes:
        beat = "professional_judgment"
        required = True
        reason = "high_stakes_requires_grounded_takeover"
    elif relationship_memory:
        beat = "relationship_echo"
        required = persona_mode in {"full", "balanced"}
        reason = "relevant_memory_requires_relationship_echo"
    elif persona_mode == "quiet":
        reason = "quiet_mode"
    elif work:
        beat = "professional_judgment"
        required = persona_mode == "full" or bool(_JUDGMENT_WORK_RE.search(text))
        reason = (
            "work_requires_visible_judgment"
            if required
            else "balanced_work_allows_quiet_execution"
        )
    elif personal:
        beat = "subjective_notice"
        required = persona_mode == "full" or state.turns_since_visible >= 3
        reason = (
            "personal_chat_requires_subjective_notice"
            if required
            else "balanced_personal_chat_allows_brief_silence"
        )
    elif state.turns_since_visible >= 3:
        beat = "subjective_notice"
        required = True
        reason = "silence_streak_requires_visible_presence"
    else:
        reason = "plain_turn_allows_intentional_silence"
    next_state = PresenceState(
        turn_index=state.turn_index + 1,
        turns_since_visible=0 if required else min(state.turns_since_visible + 1, 99),
    )
    return PersonaTurnPlan(
        conversation_mode=conversation_mode,
        beat=beat,
        require_visible=required,
        memory_statements=(relationship_memory.statement,) if relationship_memory else (),
        association_hints=(relationship_memory.future_relevance,)
        if relationship_memory and relationship_memory.future_relevance
        else (),
        reason=reason,
        next_state=next_state,
        memory_references=(
            PersonaMemoryReference(
                source="user-understanding",
                citation=(
                    "user-understanding/projection.json#"
                    f"{relationship_memory.id}"
                ),
                content=relationship_memory.statement,
                score=relationship_memory.confidence,
            ),
        )
        if relationship_memory
        else (),
        latent_need=str(getattr(intent_decision, "latent_need", "") or "")[:400],
        subjective_attention=str(
            getattr(intent_decision, "subjective_attention", "") or "",
        )[:400],
        relationship_posture=str(
            getattr(intent_decision, "relationship_posture", "") or "",
        )[:120],
        personalization_consequence=(
            str(
                getattr(
                    intent_decision,
                    "personalization_consequence",
                    "",
                )
                or "",
            )[:500]
        ),
        subject_context=(
            subject_projection.to_model_context()
            if subject_projection is not None
            else {}
        ),
        ququ_bypass_worthy=bool(
            getattr(intent_decision, "ququ_bypass_worthy", False),
        ),
        causal_anchor=str(
            getattr(intent_decision, "causal_anchor", "") or "",
        )[:500],
        selected_memory_ids=(relationship_memory.id,) if relationship_memory else (),
        expression_surface=(
            str(getattr(intent_decision, "expression_surface", "quiet") or "quiet")
            if model_classified
            else ("main_answer" if required else "quiet")
        ),  # type: ignore[arg-type]
        self_implication=str(
            getattr(intent_decision, "self_implication", "") or "",
        )[:400],
        carry_forward=str(
            getattr(intent_decision, "carry_forward", "") or "",
        )[:400],
        experience_sources=tuple(
            str(item)
            for item in getattr(intent_decision, "experience_sources", ())
            if str(item).strip()
        )[:3],
    )
