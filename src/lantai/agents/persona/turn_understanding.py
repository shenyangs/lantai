# -*- coding: utf-8 -*-
"""Compact conclusions for anticipatory turn understanding.

This is not a reasoning trace.  It exposes only the evidence-labelled result
that the visible-response model may use or ignore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .user_understanding import TurnUnderstandingProjection


@dataclass(frozen=True)
class TurnUnderstandingFrame:
    explicit_request: str
    unsaid_need: str = ""
    confidence: float = 0.0
    evidence_ids: tuple[str, ...] = ()

    def compile_prompt(self, language: str = "zh") -> str:
        # The visible model already has the user's explicit request. Injecting
        # a generic instruction to search for an "unsaid need" on every turn
        # makes ordinary conversation do unnecessary meta-reasoning and can
        # manufacture work the user never requested. Only a separately
        # authorized, evidence-backed candidate merits extra prompt context.
        if not self.explicit_request or not self.unsaid_need:
            return ""
        if not language.lower().startswith("zh"):
            lines = [
                '<anticipatory-understanding mode="conclusion-only">',
                "Before answering, check whether the user needs something beyond the literal request.",
            ]
            if self.unsaid_need:
                lines.append(
                    "Evidence-grounded candidate, not a fact: " + self.unsaid_need,
                )
            lines.extend(
                [
                    "Surface at most one inference when it materially changes the answer; calibrate it as a guess.",
                    "Then reduce decisions or take the next useful action. Do not expose hidden reasoning.",
                    "</anticipatory-understanding>",
                ],
            )
            return "\n".join(lines)

        lines = [
            '<anticipatory-understanding mode="conclusion-only">',
            "回答前判断表面请求之外，用户是否还有一个会改变答案的潜在需要、顾虑或没说完的后半句。",
        ]
        if self.unsaid_need:
            lines.append(
                "当前候选（不是事实，只能作为有依据的判断表达）："
                + self.unsaid_need,
            )
        lines.extend(
            [
                "只有真正会改变答案且信心足够时才说出来；最多说出一个，不要连续分析用户。",
                "说中以后立刻减轻选择、接走判断或推进下一步；猜错时直接修正。",
                "这里只提供结论性标签，不得向用户暴露隐藏推理过程。",
                "</anticipatory-understanding>",
            ],
        )
        return "\n".join(lines)


def build_turn_understanding(
    user_text: str,
    *,
    projection: TurnUnderstandingProjection,
) -> TurnUnderstandingFrame:
    """Build one compact turn frame from an already relevance-ranked projection."""

    explicit = re.sub(r"\s+", " ", str(user_text or "")).strip()[:600]
    hypothesis = projection.hypotheses[0] if projection.hypotheses else None
    return TurnUnderstandingFrame(
        explicit_request=explicit,
        unsaid_need=hypothesis.statement if hypothesis is not None else "",
        confidence=hypothesis.confidence if hypothesis is not None else 0.0,
        evidence_ids=hypothesis.evidence_ids if hypothesis is not None else (),
    )
