# -*- coding: utf-8 -*-
"""Evidence-based collaboration calibration without emotional gamification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


_CALIBRATION_TYPES = {"workflow", "style", "instruction"}


def _field(entry: Any, name: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


@dataclass(frozen=True)
class CollaborationCalibration:
    """Derived collaboration familiarity that never changes authority."""

    stage: str
    evidence_count: int
    evidence_types: tuple[str, ...]

    def compile_prompt(self, language: str = "zh") -> str:
        """Return a compact prompt projection for the current calibration."""

        if language.lower().startswith("zh"):
            guidance = {
                "new": (
                    "少假设用户偏好；说明关键前提，"
                    "只在会改变结果的歧义处确认。"
                ),
                "calibrating": (
                    "使用已确认偏好，"
                    "但对尚未形成证据的工作习惯仍保持谨慎。"
                ),
                "established": (
                    "自然应用已确认工作约定，减少重复交代，"
                    "并更早指出与稳定偏好的冲突。当前情境与已确认历史"
                    "的回声明显时，可以自然说出‘又是……’或点出共同经历；"
                    "只能引用本提示中真实存在的记忆，不要每轮翻旧账。"
                ),
            }[self.stage]
            return (
                f'<collaboration-calibration stage="{self.stage}">\n'
                "这不是好感度或亲密度，"
                "只表示系统拥有多少用户已确认的协作证据。\n"
                f"当前指引：{guidance}\n"
                "当前消息优先于历史偏好；"
                "协作校准不能放宽权限、事实标准或关系边界。\n"
                f"证据计数：{self.evidence_count}；"
                f"证据类型：{','.join(self.evidence_types) or 'none'}\n"
                "</collaboration-calibration>"
            )
        guidance = {
            "new": "Assume fewer preferences and confirm only ambiguities that change the result.",
            "calibrating": (
                "Use confirmed preferences while remaining cautious "
                "about unproven working habits."
            ),
            "established": (
                "Apply confirmed working agreements naturally, "
                "reduce repetition, and surface conflicts earlier. When the current "
                "moment clearly echoes confirmed history, it is fine to say 'this "
                "again' or name the shared pattern naturally. Cite only memories "
                "actually present in this prompt and do not bring up old history on "
                "every turn."
            ),
        }[self.stage]
        return (
            f'<collaboration-calibration stage="{self.stage}">\n'
            "This is not affinity or intimacy; it only reflects confirmed collaboration evidence.\n"
            f"Current guidance: {guidance}\n"
            "The current message overrides history. Calibration never relaxes "
            "permissions, factual standards, or relationship boundaries.\n"
            f"Evidence count: {self.evidence_count}; "
            f"types: {','.join(self.evidence_types) or 'none'}\n"
            "</collaboration-calibration>"
        )


def derive_collaboration_calibration(
    entries: Iterable[Any],
) -> CollaborationCalibration:
    """Derive a non-emotional stage from active, confirmed memory evidence."""

    evidence = [
        entry
        for entry in entries
        if _field(entry, "status") == "active"
        and bool(_field(entry, "confirmed", False))
        and _field(entry, "type") in _CALIBRATION_TYPES
    ]
    evidence_types = tuple(sorted({_field(entry, "type") for entry in evidence}))
    if not evidence:
        stage = "new"
    elif len(evidence) >= 3 and len(evidence_types) >= 2:
        stage = "established"
    else:
        stage = "calibrating"
    return CollaborationCalibration(
        stage=stage,
        evidence_count=len(evidence),
        evidence_types=evidence_types,
    )
