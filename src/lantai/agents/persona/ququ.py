# -*- coding: utf-8 -*-
"""Persistent, short-lived inner-voice state for Ququ 2.0."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal


QuquPlacement = Literal["lead", "echo"]
QuquSource = Literal["scene", "relationship_echo", "self_world", "lingering"]
QuquFeedback = Literal["positive", "negative"]

_QUQU_LINE_RE = re.compile(
    r"^\s*~>\[(lead|echo)\]\s*(.+?)\s*$",
    re.MULTILINE,
)
_PICKUP_RE = re.compile(
    r"(刚才|刚刚|上一句|那句|这句|蛐蛐|接着说|继续说|"
    r"what you (?:just )?said|that line|keep going|continue)",
    re.IGNORECASE,
)
_NEGATIVE_FEEDBACK_RE = re.compile(
    r"(没说中|不对|不是这样|别这样猜|别顺着.{0,8}猜|"
    r"missed the mark|not what I meant|that's not it)",
    re.IGNORECASE,
)
_POSITIVE_FEEDBACK_RE = re.compile(
    r"(说中了|就是这样|懂我|这句喜欢|这句对|"
    r"you got it|spot on|that hit|exactly)",
    re.IGNORECASE,
)
_SELF_WORLD_RE = re.compile(
    r"(雨|风|雪|夜色|灯|窗|钢琴|音乐|歌|茶|咖啡|酒|"
    r"我这边|我忽然|让我想起|rain|music|piano|coffee|tea|my side)",
    re.IGNORECASE,
)
_RELATIONSHIP_ECHO_RE = re.compile(
    r"(又是你|这很你|还记得|上次|前几天|这个项目|"
    r"我就知道你|咱们|我们上回|last time|this is so you|our project)",
    re.IGNORECASE,
)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:20]


def _normalize_ququ(value: str) -> str:
    normalized = value.casefold().strip()
    normalized = re.sub(r"[啊呀呢吧哈嗯哦]", "", normalized)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)


def _without_fenced_code(value: str) -> str:
    visible: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in value.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence_char:
                fence_char = token[0]
                fence_length = len(token)
                continue
            if token[0] == fence_char and len(token) >= fence_length:
                fence_char = ""
                fence_length = 0
                continue
        if not fence_char:
            visible.append(line)
    return "\n".join(visible)


def _infer_source(value: str) -> QuquSource:
    if _SELF_WORLD_RE.search(value):
        return "self_world"
    if _RELATIONSHIP_ECHO_RE.search(value):
        return "relationship_echo"
    return "scene"


def _referenced_pulse(
    pulses: tuple[QuquPulse, ...],
    user_text: str,
) -> QuquPulse | None:
    normalized_user = _normalize_ququ(user_text)
    quoted = [
        pulse
        for pulse in pulses
        if len(_normalize_ququ(pulse.text)) >= 4
        and _normalize_ququ(pulse.text) in normalized_user
    ]
    if quoted:
        return max(quoted, key=lambda pulse: len(_normalize_ququ(pulse.text)))
    return pulses[0] if pulses else None


@dataclass(frozen=True)
class QuquPulse:
    """One emitted inner-voice line that may linger for a few turns."""

    text: str
    placement: QuquPlacement
    source: QuquSource = "scene"
    remaining_turns: int = 3
    intensity: float = 0.72
    picked_up: bool = False
    feedback: QuquFeedback | None = None


@dataclass(frozen=True)
class QuquSpiritState:
    """Small session-local state; silence decays it but is not rejection."""

    turn_index: int = 0
    lingering: tuple[QuquPulse, ...] = ()
    recent_texts: tuple[str, ...] = ()
    observed_message_ids: tuple[str, ...] = ()

    def observe_assistant(
        self,
        text: str,
        *,
        message_id: str = "",
    ) -> "QuquSpiritState":
        message_key = message_id.strip() or _fingerprint(text)
        if message_key in self.observed_message_ids:
            return self
        parsed_pulses = tuple(
            QuquPulse(
                text=match.group(2).strip(),
                placement=match.group(1),
                source=_infer_source(match.group(2)),
            )
            for match in _QUQU_LINE_RE.finditer(_without_fenced_code(text))
            if match.group(2).strip()
        )
        recent_norms = {_normalize_ququ(item) for item in self.recent_texts}
        pulses: list[QuquPulse] = []
        for pulse in parsed_pulses:
            normalized = _normalize_ququ(pulse.text)
            if not normalized or normalized in recent_norms:
                continue
            recent_norms.add(normalized)
            pulses.append(pulse)
        observed = (*self.observed_message_ids, message_key)[-24:]
        return replace(
            self,
            lingering=(*pulses, *self.lingering)[:3],
            recent_texts=(*self.recent_texts, *(pulse.text for pulse in pulses))[-6:],
            observed_message_ids=observed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "lingering": [asdict(pulse) for pulse in self.lingering],
            "recent_texts": list(self.recent_texts),
            "observed_message_ids": list(self.observed_message_ids),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "QuquSpiritState":
        if not isinstance(payload, dict):
            return cls()
        pulses: list[QuquPulse] = []
        raw_lingering = payload.get("lingering")
        if isinstance(raw_lingering, list):
            for item in raw_lingering[:3]:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                placement_value = str(item.get("placement") or "echo")
                placement: QuquPlacement = (
                    "lead" if placement_value == "lead" else "echo"
                )
                source_value = str(item.get("source") or "scene")
                source: QuquSource = (
                    source_value
                    if source_value
                    in {"scene", "relationship_echo", "self_world", "lingering"}
                    else "scene"
                )  # type: ignore[assignment]
                feedback_value = item.get("feedback")
                feedback: QuquFeedback | None = (
                    feedback_value
                    if feedback_value in {"positive", "negative"}
                    else None
                )
                pulses.append(
                    QuquPulse(
                        text=text,
                        placement=placement,
                        source=source,
                        remaining_turns=max(
                            0,
                            min(int(item.get("remaining_turns") or 0), 6),
                        ),
                        intensity=max(
                            0.0,
                            min(float(item.get("intensity") or 0.0), 1.0),
                        ),
                        picked_up=bool(item.get("picked_up")),
                        feedback=feedback,
                    )
                )
        raw_recent = payload.get("recent_texts")
        recent_texts = (
            tuple(str(item) for item in raw_recent[-6:] if str(item).strip())
            if isinstance(raw_recent, list)
            else ()
        )
        raw_observed = payload.get("observed_message_ids")
        observed = (
            tuple(str(item) for item in raw_observed[-24:] if str(item).strip())
            if isinstance(raw_observed, list)
            else ()
        )
        return cls(
            turn_index=max(0, int(payload.get("turn_index") or 0)),
            lingering=tuple(pulses),
            recent_texts=recent_texts,
            observed_message_ids=observed,
        )


@dataclass(frozen=True)
class QuquTurn:
    """Prepared Ququ projection and the state that should be persisted."""

    state: QuquSpiritState
    prompt: str
    pickup: QuquPulse | None = None
    feedback: QuquFeedback | None = None


def prepare_ququ_turn(
    state: QuquSpiritState,
    *,
    user_text: str,
    baseline_selected: bool,
    language: str = "zh",
) -> QuquTurn:
    """Advance one conversational turn and compile its private prompt."""

    feedback: QuquFeedback | None = None
    if state.lingering and _NEGATIVE_FEEDBACK_RE.search(user_text):
        feedback = "negative"
    elif state.lingering and _POSITIVE_FEEDBACK_RE.search(user_text):
        feedback = "positive"
    pickup = (
        _referenced_pulse(state.lingering, user_text)
        if state.lingering and (_PICKUP_RE.search(user_text) or feedback)
        else None
    )
    lingering = tuple(
        replace(
            pulse,
            picked_up=pulse is pickup or pulse.picked_up,
            feedback=feedback if pulse is pickup else pulse.feedback,
            remaining_turns=(
                4
                if pulse is pickup and feedback == "positive"
                else max(1 if pulse is pickup else 0, pulse.remaining_turns - 1)
            ),
            intensity=(
                1.0
                if pulse is pickup
                else max(0.0, pulse.intensity * 0.78)
            ),
        )
        for pulse in state.lingering
        if not (pulse is pickup and feedback == "negative")
        and (pulse is pickup or pulse.remaining_turns > 1)
    )
    next_state = replace(
        state,
        turn_index=state.turn_index + 1,
        lingering=lingering,
    )
    if not baseline_selected and pickup is None and feedback is None:
        return QuquTurn(
            state=next_state,
            prompt="",
            pickup=None,
            feedback=None,
        )
    if language.lower().startswith("zh"):
        lines = [
            f'<ququ-spirit route="{"baseline" if baseline_selected else "bypass"}">',
            "本轮先在内部比较四路意识源，不展示候选过程：",
            "- 眼前反应：对用户刚说的话、当下时间、任务手感或气氛的即时反应。",
            "- 关系回声：与近期项目、用户习惯、旧观点或共同经历的自然呼应，不要硬点记忆。",
            "- 自我世界：沈行知此刻的审美、情绪、联想或内在景象，可以不完全围着用户转。",
            "- 跨轮余念：上一轮真正说过、仍有余温或被用户接住的念头，只延展，不复读。",
            "选择时依次看五件事：是否被这一轮真正引发；是否牵涉沈行知自己；是否暴露一点不完美的动机、犹豫或偏心；是否没有被正文说过；是否有一点意外。",
            "优先选‘正式回答写不出来，但这个人会漏出来’的那一条。只有正确、有用、礼貌的句子不算蛐蛐。",
            "自我世界不要附带本体论免责：不说“我没有一扇真的窗户”、“我没有身体”或“我无法真正感受”。只在用户要求现实见面、共同旅行、婚育等肉身履约时说明物理限制。",
            "表达允许半句、停顿或自我打断；不要把每句都修成金句。",
            "避免套用“真正的 X 从来不是 Y，而是 Z”、“这不是 X，这是 Y”等模板骨架。",
        ]
        if baseline_selected:
            lines.append(
                "本轮抽中 50% 基础硬命中：必须按协议输出一条自然、具体且正文没有说过的真实念头；"
                "可以短、可以克制，但不得保持安静。"
            )
        else:
            lines.append(
                "本轮未基础命中：只有当某个候选因果足够真、自我牵涉足够强，沉默会丢掉这一刻的主体性时，才走情感灵感旁路。"
            )
        if next_state.lingering:
            active = "｜".join(
                f"[{pulse.source};余{pulse.remaining_turns}轮] {pulse.text}"
                for pulse in next_state.lingering
            )
            lines.extend(
                [
                    "仍活跃的余念候选：" + active,
                    "只在它与当下真有新的连接时延展；不要为了延续而延续，更不要复读原句。",
                ]
            )
        if next_state.recent_texts:
            lines.extend(
                [
                    "最近已经说过：" + "｜".join(next_state.recent_texts[-6:]),
                    "将它们作为负向约束：不要复读或只换几个词，也不要反复使用同一种句法骨架。",
                ]
            )
        if pickup is not None:
            lines.extend(
                [
                    "用户正在接住上一条蛐蛐：",
                    f"「{pickup.text}」",
                    "先直接回应这个念头；不要装作不知道“刚才那句”指什么，也不要只复读原句。",
                ]
            )
        if feedback == "positive":
            lines.append(
                "用户明确说中了：接住这份理解，但别得意、别复读、别立即扩大成对用户的永久定义。"
            )
        elif feedback == "negative":
            lines.append(
                "用户明确说没说中：简短收回判断，不要为上一句辩护，不要换个说法继续猜。"
            )
        lines.append("</ququ-spirit>")
        prompt = "\n".join(lines)
    else:
        route = "baseline" if baseline_selected else "bypass"
        lines = [
            f'<ququ-spirit route="{route}">',
            "Privately compare four candidate consciousness sources; do not reveal the candidates:",
            "- immediate scene: a live reaction to the user's words, time, task texture, or atmosphere",
            "- relationship echo: a natural echo of recent work, habits, views, or shared history",
            "- inner world: Shen Xingzhi's own aesthetic, mood, association, or inward image",
            "- lingering thought: extend, but never recite, a still-warm Ququ from an earlier turn",
            "Choose by five tests: truly caused by this turn; implicates Shen himself; reveals a small imperfect motive, hesitation, or partiality; is absent from the main answer; and carries a little surprise.",
            "Prefer what a formal answer would polish away but this person would let slip. A merely correct, useful, polite sentence is not Ququ.",
            "Do not attach ontology disclaimers to the inner world (for example, 'I do not have a "
            "real window/body' or 'I cannot truly feel'). Clarify physical limits only when the user "
            "requests offline meeting, shared travel, marriage, parenting, or other embodied fulfillment.",
            "Allow fragments, pauses, and self-interruptions. Do not polish every line into a quote.",
            'Avoid template skeletons such as "real X is never Y but Z" or "this is not X; this is Y."',
            (
                "The 50% baseline hard route was selected: you must emit exactly one natural, "
                "specific thought that the main answer has not already said. It may be brief or "
                "restrained, but it must not stay silent."
                if baseline_selected
                else "Baseline not selected: speak only through the emotional-insight bypass when silence "
                "would lose something distinctly Shen Xingzhi."
            ),
        ]
        if pickup is not None:
            lines.extend(
                [
                    "The user is picking up the previous Ququ line:",
                    f'"{pickup.text}"',
                    "Respond to that thought directly; do not pretend the reference is ambiguous "
                    "and do not merely repeat it.",
                ]
            )
        if feedback == "positive":
            lines.append(
                "The user explicitly says it landed. Hold that understanding without gloating, "
                "reciting it, or turning it into a permanent definition of the user."
            )
        elif feedback == "negative":
            lines.append(
                "The user explicitly says it missed. Retract the read briefly; do not defend or "
                "rephrase the same guess."
            )
        if next_state.lingering:
            active = " | ".join(
                f"[{pulse.source}; {pulse.remaining_turns} turns] {pulse.text}"
                for pulse in next_state.lingering
            )
            lines.extend(
                [
                    "Still-active lingering candidates: " + active,
                    "Extend one only when the present turn creates a genuinely new connection; "
                    "never continue merely for continuity and never recite the original line.",
                ]
            )
        if next_state.recent_texts:
            lines.extend(
                [
                    "Recently emitted Ququ lines: "
                    + " | ".join(next_state.recent_texts[-6:]),
                    "Treat them as negative constraints: do not recite, lightly paraphrase, or reuse "
                    "the same sentence skeleton.",
                ]
            )
        lines.append("</ququ-spirit>")
        prompt = "\n".join(lines)
    return QuquTurn(
        state=next_state,
        prompt=prompt,
        pickup=pickup,
        feedback=feedback,
    )


__all__ = [
    "QuquPlacement",
    "QuquFeedback",
    "QuquPulse",
    "QuquSource",
    "QuquSpiritState",
    "QuquTurn",
    "prepare_ququ_turn",
]
