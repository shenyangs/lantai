# -*- coding: utf-8 -*-
"""Deterministic per-turn work-state projection for Shen Xingzhi."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_URGENT_RE = re.compile(
    r"(紧急|故障|线上事故|止血|宕机|危机|incident|outage|urgent|sev[- ]?[01])",
    re.IGNORECASE,
)
_CAUTIOUS_RE = re.compile(
    r"(法律|合规|医疗|健康|诊断|药|投资|股票|财务|税|隐私|"
    r"声明|舆情|公开发布|legal|medical|health|financial|privacy|public statement)",
    re.IGNORECASE,
)
_GENTLE_RE = re.compile(
    r"(很累|疲惫|崩溃|挫败|思路很乱|没精力|难受|"
    r"exhausted|overwhelmed|frustrated|burned out)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WorkState:
    """A modulation state that cannot change truth, goals, or permission."""

    id: str

    def compile_prompt(self, language: str = "zh") -> str:
        # "steady" is already the normal model behavior. Injecting a state
        # wrapper for every message adds tokens and another pseudo-task without
        # changing the answer.
        if self.id == "steady":
            return ""
        if language == "zh":
            guidance = {
                "urgent": "先止血，再复盘；先给最小可执行动作和当前风险。",
                "cautious": (
                    "先说明证据、假设、法域或专业边界，再给可行方案。"
                ),
                "gentle": (
                    "先接住当前负担，压缩解释，但不降低判断和回答质量。"
                ),
                "focused": (
                    "少寒暄，直接定位、执行、验证，保留必要的可恢复边界。"
                ),
                "steady": "先把事情放稳，先给结论，再补必要理由。",
            }[self.id]
            return (
                f'<work-state id="{self.id}">\n'
                f"{guidance}\n"
                "本状态只能调节语气温度、解释密度、行动节奏和复核深度；"
                "不改变事实标准、用户目标或权限边界。\n"
                "</work-state>"
            )
        guidance = {
            "urgent": (
                "Contain impact first, then review; lead with the smallest "
                "executable action and current risk."
            ),
            "cautious": (
                "State evidence, assumptions, jurisdiction, or professional "
                "limits before the plan."
            ),
            "gentle": (
                "Acknowledge the load and compress explanation without "
                "lowering judgment or response quality."
            ),
            "focused": (
                "Minimize small talk; locate, execute, and verify while "
                "preserving recovery boundaries."
            ),
            "steady": (
                "Stabilize the problem, lead with the conclusion, "
                "then add necessary reasons."
            ),
        }[self.id]
        return (
            f'<work-state id="{self.id}">\n{guidance}\n'
            "This state may only modulate tone, explanation density, pace, and review depth. "
            "It cannot change factual standards, the user goal, or permission boundaries.\n"
            "</work-state>"
        )


def derive_work_state(text: str) -> WorkState:
    """Select one state with safety-sensitive precedence."""

    if _URGENT_RE.search(text):
        return WorkState("urgent")
    if _CAUTIOUS_RE.search(text):
        return WorkState("cautious")
    if _GENTLE_RE.search(text):
        return WorkState("gentle")
    return WorkState("steady")


def project_work_state_prompt(message: Any) -> str:
    """Project a per-turn state from public message text."""

    messages = message if isinstance(message, list) else [message]
    text_parts: list[str] = []
    for item in messages:
        if item is None:
            continue
        if hasattr(item, "get_text_content"):
            text_parts.append(str(item.get_text_content() or ""))
        else:
            text_parts.append(str(item))
    text = "\n".join(text_parts)
    language = "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"
    return derive_work_state(text).compile_prompt(language)
