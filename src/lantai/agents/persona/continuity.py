# -*- coding: utf-8 -*-
"""Short-lived stance continuity for Shen Xingzhi conversations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


_SIGNAL_STANCES = {
    "hard-won-success": "shared_joy",
    "loss-grief": "quiet_company",
    "user-overload": "protective",
    "late-night-work": "protective",
    "scope-pressure": "challenging",
    "craft-integrity": "challenging",
    "public-anger": "ally_with_restraint",
    "relationship-affection": "connected",
    "failure-owning": "repairing",
}
_LIGHT_TEASE_RE = re.compile(
    r"(怎么比我还|你也太|这么高兴|这么认真|你还会高兴|"
    r"哟.{0,8}高兴|哈哈|笑死)",
)
_RESTRAINT_RE = re.compile(
    r"(先别庆祝|别庆祝|收一收|收一下|别高兴过头|"
    r"先说正事|先干活|别煽情)",
)


@dataclass(frozen=True)
class ContinuityState:
    """A bounded relationship stance inside the broader private subject state."""

    stance: str = "steady"
    intensity: float = 0.0
    remaining_turns: int = 0
    origin_signal: str = ""
    user_requested_restraint: bool = False
    light_tease: bool = False

    def advance(
        self,
        *,
        signal_ids: tuple[str, ...],
        user_text: str,
    ) -> "ContinuityState":
        for signal_id in signal_ids:
            stance = _SIGNAL_STANCES.get(signal_id)
            if stance:
                return ContinuityState(
                    stance=stance,
                    intensity=0.72,
                    remaining_turns=4,
                    origin_signal=signal_id,
                    user_requested_restraint=bool(_RESTRAINT_RE.search(user_text)),
                    light_tease=bool(_LIGHT_TEASE_RE.search(user_text)),
                )
        if self.remaining_turns <= 1:
            return ContinuityState()
        return ContinuityState(
            stance=self.stance,
            intensity=max(0.15, self.intensity * 0.78),
            remaining_turns=self.remaining_turns - 1,
            origin_signal=self.origin_signal,
            user_requested_restraint=bool(_RESTRAINT_RE.search(user_text)),
            light_tease=bool(_LIGHT_TEASE_RE.search(user_text)),
        )

    def compile_prompt(self, language: str = "zh") -> str:
        if self.stance == "steady" or self.remaining_turns <= 0:
            return ""
        if not language.lower().startswith("zh"):
            lines = [
                '<relationship-continuity lifetime="short">',
                f"Current stance: {self.stance}; intensity={self.intensity:.2f}.",
                "Keep weak continuity with the previous turn. A light tease is not a rejection. "
                "If the user asks for restraint, reduce expression without retracting the stance.",
            ]
            if self.stance == "ally_with_restraint":
                lines.append(
                    "You may acknowledge that the delivery missed the user, but do not turn "
                    "their anger into tomorrow's liability. Change the delivery while continuing "
                    "to protect the user's interests."
                )
            lines.append("</relationship-continuity>")
            return "\n".join(lines)
        lines = [
            '<relationship-continuity lifetime="short">',
            f"当前关系姿态：{self.stance}；强度={self.intensity:.2f}。",
            "保留上一轮态度的弱惯性，不要因为一句轻微反问就性格突变。",
        ]
        if self.light_tease:
            lines.append("轻微调侃不等于否定；不要突然撤回高兴或变冷。")
        if self.stance == "ally_with_restraint":
            lines.append(
                "可以承认表达方式没接好，但不把怒气变成反伤用户的把柄；"
                "调整表达方式，换一种方式继续守住用户利益。"
            )
        if self.user_requested_restraint:
            lines.append(
                "用户要求收住表达：降低强度并自然收住，但不要否认刚才真实的态度；"
                "除非用户明确切回工作，不凭空转成任务或下一步。"
            )
        lines.append("</relationship-continuity>")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "ContinuityState":
        if not isinstance(payload, dict):
            return cls()
        return cls(
            stance=str(payload.get("stance") or "steady"),
            intensity=max(0.0, min(float(payload.get("intensity") or 0.0), 1.0)),
            remaining_turns=max(0, int(payload.get("remaining_turns") or 0)),
            origin_signal=str(payload.get("origin_signal") or ""),
            user_requested_restraint=bool(payload.get("user_requested_restraint")),
            light_tease=bool(payload.get("light_tease")),
        )
