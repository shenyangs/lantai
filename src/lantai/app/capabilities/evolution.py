# -*- coding: utf-8 -*-
"""Conservative, evidence-backed capability evolution proposals."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .models import Capability, EvolutionEvidence, EvolutionProposal, TaskRun


_LOW_VALUE_CHAT_PATTERNS = (
    re.compile(r"(?:问候|打招呼|寒暄|闲聊|聊聊|讲(?:个|一个)?笑话)"),
    re.compile(
        r"(?:写|作|创作|来).{0,28}诗(?:歌|写作)?|(?:一|1)?(?:首|句).{0,28}诗(?:歌|写作)?"
    ),
    re.compile(r"\b(?:say hello|small talk|tell (?:me )?a joke)\b", re.IGNORECASE),
    re.compile(r"\b(?:write|compose) (?:me )?(?:a )?(?:modern )?poem\b", re.IGNORECASE),
)


def normalize_workflow_intent(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", " ", text)
    text = re.sub(r"\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?", " ", text)
    text = re.sub(r"\d+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())[:160]


def _looks_like_low_value_chat(key: str) -> bool:
    """Exclude repeated conversation prompts that do not describe a workflow.

    These prompts can recur, but they have no stable procedure or operational
    stopping condition. Showing them as reusable-workflow candidates transfers
    a classification mistake to the user.
    """

    return any(pattern.search(key) for pattern in _LOW_VALUE_CHAT_PATTERNS)


def _matching_capability(
    key: str,
    capabilities: list[Capability],
) -> Capability | None:
    for capability in capabilities:
        candidate = normalize_workflow_intent(capability.name)
        if candidate and (candidate == key or candidate in key or key in candidate):
            return capability
    return None


def build_evolution_proposals(
    runs: list[TaskRun],
    capabilities: list[Capability],
) -> list[EvolutionProposal]:
    """Return only high-confidence proposals backed by two distinct runs."""

    grouped: dict[str, dict[str, TaskRun]] = defaultdict(dict)
    for run in runs:
        if run.status != "succeeded":
            continue
        key = normalize_workflow_intent(run.user_intent or run.title)
        if len(key) < 4 or _looks_like_low_value_chat(key):
            continue
        grouped[key][run.id] = run

    proposals: list[EvolutionProposal] = []
    for key, by_id in grouped.items():
        evidence_runs = sorted(
            by_id.values(),
            key=lambda item: item.started_at,
            reverse=True,
        )
        if len(evidence_runs) < 2:
            continue
        matched = _matching_capability(key, capabilities)
        recommended_form = (
            "extend_existing"
            if matched is not None
            else "automation"
            if all(run.source == "cron" for run in evidence_runs)
            else "capability"
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        proposals.append(
            EvolutionProposal(
                id=f"evo-{digest[:16]}",
                fingerprint=digest,
                title=evidence_runs[0].title,
                rationale=(
                    f"近期待办中发现 {len(evidence_runs)} 次独立完成记录；"
                    + (
                        "优先扩展已有能力，避免重复创建。"
                        if matched is not None
                        else "输入、过程和完成条件稳定，可进入人工复核。"
                    )
                ),
                recommended_form=recommended_form,
                repeat_count=len(evidence_runs),
                evidence=[
                    EvolutionEvidence(
                        run_id=run.id,
                        title=run.title,
                        source=run.source,
                        occurred_at=run.started_at,
                    )
                    for run in evidence_runs
                ],
                matched_capability_id=matched.id if matched else None,
            ),
        )
    return sorted(
        proposals,
        key=lambda item: item.evidence[0].occurred_at,
        reverse=True,
    )
