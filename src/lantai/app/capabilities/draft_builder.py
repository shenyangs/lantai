# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib

from .models import (
    Capability,
    CapabilityDraft,
    CapabilityParameter,
    CapabilityStep,
    DeliveryTarget,
    SuggestedSkillDraft,
    TaskRun,
    utc_now,
)


def _clean_title(value: str | None, fallback: str = "可复用任务") -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    return text[:28]


def _normalize_skill_name(input_text: str, *, prefix: str = "capability-skill") -> str:
    ascii_name = (
        input_text.lower()
        .replace("_", "-")
    )
    ascii_name = "".join(ch if ch.isalnum() else "-" for ch in ascii_name if ord(ch) < 128)
    ascii_name = "-".join(part for part in ascii_name.split("-") if part)
    if ascii_name:
        return ascii_name[:48]
    digest = hashlib.sha1(input_text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def _extract_final_response(task_run: TaskRun) -> str:
    for step in reversed(task_run.steps):
        if step.title == "最终回复" and step.detail:
            return str(step.detail).strip()
        if step.metadata.get("experience_kind") == "chat.final_answer" and step.detail:
            return str(step.detail).strip()
    return ""


def _build_suggested_skill(task_run: TaskRun) -> SuggestedSkillDraft | None:
    if task_run.source != "chat" or task_run.status != "succeeded":
        return None
    intent = task_run.user_intent or task_run.title
    final_response = _extract_final_response(task_run)
    if not intent or not final_response:
        return None

    title = _clean_title(intent, "聊天任务")
    name = _normalize_skill_name(str(intent))
    description = f"处理“{title}”这类任务的可复用技能片段。"
    content = f"""---
name: {name}
description: {description}
---

# {name}

## Use when
- 用户的目标与“原始需求”接近，需要复用同类处理流程。
- 某个能力需要复用这次对话沉淀出的写法、判断顺序或交付结构。

## What to do
1. 先识别任务目标、约束和希望的输出形态。
2. 参考下方“成功回复参考”，抽取稳定步骤、结构和判断顺序。
3. 输出时保留清晰结构，避免把一次性上下文原样照搬。

## Output expectations
- 给出可直接交付给用户的结果。
- 结构清楚，必要时包含步骤、结论和后续建议。

## Original request
{str(intent).strip()}

## Successful response reference
{final_response}

## Notes
- 这是从能力沉淀流程自动生成的技能片段草稿。
- 保存后会被当前能力引用；如需共享给其它智能体，可在技能页手动同步到技能池。
"""
    return SuggestedSkillDraft(
        name=name,
        description=description,
        content=content,
        tags=["能力沉淀", task_run.source],
        reason="这轮对话包含可复用的处理结构，可作为能力内部 Skill 步骤复用。",
        attach_to_step_index=0,
    )


def build_capability_draft(task_run: TaskRun) -> CapabilityDraft:
    """Build a conservative draft from a task run.

    v1 intentionally avoids auto-enabling or over-inferring tool calls. It turns
    the successful task into one editable agent step and keeps evidence visible.
    """
    intent = task_run.user_intent or task_run.title
    title = _clean_title(task_run.title or intent)
    evidence = [
        f"{step.index + 1}. {step.title}: {step.status}"
        for step in task_run.steps[:8]
    ]
    if task_run.artifacts:
        evidence.extend(f"产物：{artifact.title}" for artifact in task_run.artifacts[:5])

    parameter = CapabilityParameter(
        name="task_goal",
        label="任务目标",
        type="text",
        required=True,
        default=intent,
        description="从原任务抽取出的目标，可在复用时改写。",
    )
    step = CapabilityStep(
        index=0,
        title="执行任务",
        mode="dynamic",
        kind="agent_prompt",
        instruction_template="{{task_goal}}",
        required_parameters=["task_goal"],
        risk_level="low",
    )
    delivery = CapabilityStep(
        index=1,
        title="交付结果",
        mode="fixed",
        kind="delivery",
        instruction_template="把本次执行结果交付给用户。",
        risk_level="low",
    )
    capability = Capability(
        name=f"{title}能力",
        description=f"从任务「{task_run.title}」生成的可复用草稿。",
        enabled=False,
        tags=["草稿", task_run.source],
        source_run_id=task_run.id,
        parameters=[parameter],
        steps=[step, delivery],
        delivery_defaults=[DeliveryTarget(type="chat")],
        updated_at=utc_now(),
    )
    warnings = [
        "这是草稿：请复核参数、步骤和风险等级后再启用。",
        "v1 不会固化裸工具调用或 GUI 操作，只保留可编辑的 Agent 指令。",
    ]
    suggested_skill = _build_suggested_skill(task_run)
    return CapabilityDraft(
        capability=capability,
        source_run=task_run,
        evidence=evidence,
        warnings=warnings,
        suggested_skills=[suggested_skill] if suggested_skill else [],
    )
