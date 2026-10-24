# -*- coding: utf-8 -*-
"""Turn a point-in-time chat prefix into reviewable reusable assets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from ...runtime.converters.legacy_messages import (
    legacy_memory_state_to_chat_messages,
)
from ..experience.events import _iter_events_for_workspace
from ..experience.schemas import ChatExperienceEvent
from ..structured_utility import run_structured_utility_task
from .models import (
    Capability,
    CapabilityDraft,
    CapabilityParameter,
    CapabilityStep,
    CollaborationDraftRequest,
    CollaborationInsights,
    CollaborationScope,
    SuggestedSkillDraft,
    TaskRun,
    TaskStep,
)
from .task_run_materializer import materialize_task_runs


MAX_MESSAGE_CHARS = 12_000
MAX_EVIDENCE_CHARS = 60_000
UTILITY_HEARTBEAT_SECONDS = 1.5
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc


class VisibleCollaborationMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    original_id: str | None = None
    turn_id: str | None = None
    created_at: datetime | None = None


class CollaborationEvidence(BaseModel):
    agent_id: str
    chat_id: str
    chat_title: str
    anchor_run_id: str
    messages: list[VisibleCollaborationMessage] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    artifact_titles: list[str] = Field(default_factory=list)
    turn_count: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    truncated: bool = False
    redacted_count: int = 0
    source_workspace_dir: str | None = Field(default=None, exclude=True, repr=False)


class DistillationBlueprint(BaseModel):
    title: str
    summary: str
    skill_name: str
    skill_description: str
    reusable_goal: str
    use_when: list[str] = Field(default_factory=list)
    variable_inputs: list[str] = Field(default_factory=list)
    workflow_steps: list[str] = Field(default_factory=list)
    decision_rules: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)
    tool_strategy: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    output_expectations: list[str] = Field(default_factory=list)
    asset_kind: Literal["skill", "both"] = "skill"
    capability_reason: str = ""

    @field_validator("skill_name")
    @classmethod
    def validate_skill_name(cls, value: str) -> str:
        name = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", name):
            raise ValueError("skill_name must be a lowercase kebab-case identifier")
        return name


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)"
            r"(\s*[:=]\s*)[\"']?[^\s,;\"']{8,}[\"']?",
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        "[REDACTED_TOKEN]",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z ]*PRIVATE KEY-----",
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
)


def _parse_datetime(value: Any) -> datetime | None:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def redact_sensitive_text(value: str) -> tuple[str, int]:
    """Remove credentials and normalize user-home paths before model use."""

    text = value.replace("\r\n", "\n")
    replacements = 0
    for pattern, replacement in _SECRET_PATTERNS:
        text, count = pattern.subn(replacement, text)
        replacements += count
    text = re.sub(r"/Users/[^/\s]+/", "~/", text)
    text = re.sub(
        r"(?i)[A-Z]:\\Users\\[^\\\s]+\\",
        lambda _match: "~\\",
        text,
    )
    return text.strip(), replacements


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            part for item in value if (part := _text_from_content(item))
        )
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("text"), str):
        return str(value["text"])
    if isinstance(value.get("content"), (str, list, dict)):
        return _text_from_content(value["content"])
    return ""


def _metadata_records(message: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    queue: list[Any] = [message.get("metadata")]
    seen: set[int] = set()
    while queue:
        candidate = queue.pop(0)
        if not isinstance(candidate, dict) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        result.append(candidate)
        for key in ("metadata", "legacy_request_context", "runtime"):
            queue.append(candidate.get(key))
    return result


def _message_turn_ids(message: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for metadata in _metadata_records(message):
        for key in (
            "native_runtime_turn_id",
            "runtime_turn_id",
            "turn_id",
            "run_id",
            "last_turn_run_id",
            "client_turn_id",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                result.add(value)
    return result


def _message_original_id(message: dict[str, Any]) -> str | None:
    for metadata in _metadata_records(message):
        value = metadata.get("original_id") or metadata.get("runtime_item_id")
        if isinstance(value, str) and value:
            return value
    value = message.get("id")
    return value if isinstance(value, str) and value else None


def _event_identities(event: ChatExperienceEvent) -> set[str]:
    result = {value for value in (event.run_id, event.item_id) if value}
    for key in (
        "native_runtime_turn_id",
        "runtime_turn_id",
        "turn_id",
        "run_id",
        "client_turn_id",
    ):
        value = event.payload.get(key)
        if isinstance(value, str) and value:
            result.add(value)
    return result


def _visible_messages_until_anchor(
    messages: list[Any],
    *,
    anchor_run_id: str,
    anchor_ended_at: datetime | None,
    assistant_turn_limit: int,
) -> tuple[list[VisibleCollaborationMessage], bool, int]:
    visible: list[VisibleCollaborationMessage] = []
    redacted_count = 0
    exact_anchor_found = False
    assistant_original_ids: set[str] = set()

    for raw in messages:
        message = _record(raw)
        role = str(message.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            continue
        if str(message.get("type") or "message").lower() != "message":
            continue
        created_at = _parse_datetime(
            message.get("created_at") or message.get("createdAt")
        )
        if (
            not exact_anchor_found
            and anchor_ended_at is not None
            and created_at is not None
            and created_at > anchor_ended_at
        ):
            break
        text = _text_from_content(message.get("content")).strip()
        if not text:
            continue
        text, count = redact_sensitive_text(text[:MAX_MESSAGE_CHARS])
        redacted_count += count
        if not text:
            continue
        turn_ids = _message_turn_ids(message)
        original_id = _message_original_id(message)
        visible.append(
            VisibleCollaborationMessage(
                role=role,
                text=text,
                original_id=original_id,
                turn_id=next(iter(turn_ids), None),
                created_at=created_at,
            )
        )
        if role == "assistant" and original_id:
            assistant_original_ids.add(original_id)
        if role == "assistant" and anchor_run_id in turn_ids:
            exact_anchor_found = True
            break
        if (
            role == "assistant"
            and not turn_ids
            and len(assistant_original_ids) >= assistant_turn_limit
        ):
            break

    return visible, exact_anchor_found, redacted_count


def _truncate_messages(
    messages: list[VisibleCollaborationMessage],
) -> tuple[list[VisibleCollaborationMessage], bool]:
    total = sum(len(item.text) for item in messages)
    if total <= MAX_EVIDENCE_CHARS:
        return messages, False

    kept: list[VisibleCollaborationMessage] = []
    budget = MAX_EVIDENCE_CHARS
    if messages:
        first = messages[0]
        kept.append(first)
        budget -= len(first.text)
    tail: list[VisibleCollaborationMessage] = []
    for item in reversed(messages[1:]):
        if len(item.text) > budget:
            continue
        tail.append(item)
        budget -= len(item.text)
    kept.extend(reversed(tail))
    return kept, True


def build_collaboration_evidence(
    *,
    agent_id: str,
    chat_id: str,
    chat_title: str,
    anchor_run_id: str,
    events: list[ChatExperienceEvent],
    messages: list[Any],
    source_workspace_dir: str | None = None,
) -> CollaborationEvidence:
    relevant = sorted(
        (
            event
            for event in events
            if event.agent_id == agent_id and event.chat_id == chat_id
        ),
        key=lambda event: (event.seq, event.ts, event.id),
    )
    anchor_events = [
        event for event in relevant if anchor_run_id in _event_identities(event)
    ]
    if not anchor_events:
        raise ValueError("所点回复已无法与这个会话的运行记录匹配。")
    if not any(
        event.kind in {"chat.final_answer", "chat.turn.settled"}
        for event in anchor_events
    ):
        raise ValueError("所点这一轮还没有完成，暂时不能沉淀。")

    anchor_seq = max(event.seq for event in anchor_events)
    prefix = [event for event in relevant if event.seq <= anchor_seq]
    turn_ids = {
        event.run_id
        for event in prefix
        if event.kind == "chat.turn.started" and event.run_id
    }
    assistant_turn_limit = max(
        1,
        len(
            {
                event.run_id
                for event in prefix
                if event.kind == "chat.final_answer" and event.run_id
            }
        ),
    )
    anchor_timestamps = [
        parsed
        for event in anchor_events
        if (parsed := _parse_datetime(event.ts)) is not None
    ]
    anchor_ended_at = max(anchor_timestamps, default=None)
    visible, _, redacted_count = _visible_messages_until_anchor(
        messages,
        anchor_run_id=anchor_run_id,
        anchor_ended_at=anchor_ended_at,
        assistant_turn_limit=assistant_turn_limit,
    )
    visible_roles = {message.role for message in visible}
    if not {"user", "assistant"}.issubset(visible_roles):
        raise ValueError("截止所点回复的可见协作内容不完整，无法安全沉淀。")
    visible, truncated = _truncate_messages(visible)

    tool_names: list[str] = []
    artifacts: list[str] = []
    for event in prefix:
        if event.kind.startswith("chat.tool_call."):
            name = str(
                event.payload.get("tool_name") or event.payload.get("name") or ""
            ).strip()
            if name and name not in tool_names:
                tool_names.append(name)
        elif event.kind == "chat.artifact.created":
            title = str(
                event.payload.get("title")
                or event.payload.get("path")
                or event.payload.get("url")
                or ""
            ).strip()
            title, count = redact_sensitive_text(title)
            redacted_count += count
            if title and title not in artifacts:
                artifacts.append(title)

    return CollaborationEvidence(
        agent_id=agent_id,
        chat_id=chat_id,
        chat_title=chat_title or "会话协作",
        anchor_run_id=anchor_run_id,
        messages=visible,
        tool_names=tool_names,
        artifact_titles=artifacts,
        turn_count=max(len(turn_ids), assistant_turn_limit),
        started_at=_parse_datetime(prefix[0].ts) if prefix else None,
        ended_at=anchor_ended_at,
        truncated=truncated,
        redacted_count=redacted_count,
        source_workspace_dir=source_workspace_dir,
    )


def _short_line(value: str, limit: int = 180) -> str:
    normalized = " ".join(value.split()).strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def _fallback_skill_name(evidence: CollaborationEvidence) -> str:
    digest = hashlib.sha1(
        f"{evidence.chat_id}:{evidence.anchor_run_id}:{evidence.chat_title}".encode(
            "utf-8"
        )
    ).hexdigest()[:8]
    return f"collaboration-method-{digest}"


def _explicit_corrections(evidence: CollaborationEvidence) -> list[str]:
    corrections: list[str] = []
    user_messages = [item.text for item in evidence.messages if item.role == "user"]
    for text in user_messages[1:]:
        if re.search(
            r"(?:不要|不是|不对|应该|需要|改成|补上|删掉|保留|更注重|我想)",
            text,
        ):
            corrections.append(_short_line(text))
    return corrections[:8]


def build_fallback_blueprint(
    evidence: CollaborationEvidence,
) -> DistillationBlueprint:
    user_messages = [item.text for item in evidence.messages if item.role == "user"]
    first_goal = _short_line(user_messages[0]) if user_messages else evidence.chat_title
    corrections = _explicit_corrections(evidence)
    has_execution = bool(evidence.tool_names or evidence.artifact_titles)
    tool_strategy = [
        f"在确认输入和交付标准后使用 {name}，并核对返回结果。"
        for name in evidence.tool_names[:8]
    ]
    return DistillationBlueprint(
        title=f"{evidence.chat_title}的可复用方法",
        summary=(
            f"将“{first_goal}”这类任务从需求澄清、迭代修正到交付复核的协作方法。"
        ),
        skill_name=_fallback_skill_name(evidence),
        skill_description=f"复用“{evidence.chat_title}”中经验证的协作步骤与交付标准。",
        reusable_goal=first_goal,
        use_when=[
            "面对目标相似、需要多轮校准后交付的任务。",
            "需要保留用户明确纠正和验收标准，避免重复试错。",
        ],
        variable_inputs=["本次任务目标", "业务上下文与输入材料", "交付格式与验收条件"],
        workflow_steps=[
            "复述目标、约束、可用资料和期望交付物，补齐影响结果的缺口。",
            "先产出可检查的中间结构，再根据用户的明确纠正调整方向。",
            "对完成结果做必要核验，确认格式、范围和验收条件后交付。",
        ],
        decision_rules=[
            "优先采用用户后续明确的纠正，不沿用被否定的早期做法。",
            "只沉淀可复用方法，不固化本次人名、路径、密钥或项目专有事实。",
        ],
        corrections=corrections,
        tool_strategy=tool_strategy,
        failure_modes=[
            "只复制最后答案，丢失了中间的纠正和取舍。",
            "把一次性素材或本地路径写成通用规则。",
        ],
        output_expectations=[
            "交付物直接回应当前目标，结构清楚且可复核。",
            "显式说明已做什么、关键改动和验证结果。",
        ],
        asset_kind="both" if has_execution else "skill",
        capability_reason=(
            "对话中包含可重复的工具执行或交付产物。"
            if has_execution
            else ""
        ),
    )


def _prompt_for_evidence(evidence: CollaborationEvidence) -> str:
    transcript = "\n\n".join(
        f"[{item.role}]\n{item.text}" for item in evidence.messages
    )
    tools = ", ".join(evidence.tool_names) or "无"
    artifacts = "\n".join(f"- {item}" for item in evidence.artifact_titles) or "无"
    return (
        f"会话标题：{evidence.chat_title}\n"
        f"截止轮次：{evidence.anchor_run_id}\n"
        f"已完成轮数：{evidence.turn_count}\n"
        f"可见工具：{tools}\n"
        f"交付产物：\n{artifacts}\n\n"
        f"可见对话：\n{transcript}"
    )


async def distill_blueprint(
    evidence: CollaborationEvidence,
) -> tuple[DistillationBlueprint, bool, str | None]:
    system_prompt = """
你是澜台的协作方法提炼器。你会看到从某个会话开始到用户点击的助手回复为止的可见对话和工具摘要。
任务是把这段协作抽象成可复用 Skill，而不是摘录原对话或仅复制最后答案。

必须：
1. 从用户后续轮次中提炼明确纠正、否定的做法、判断规则和交付标准。
2. 区分稳定方法与一次性人名、项目名、文件路径、数值、秘密和私密事实；后者不得进入 Skill。
3. 不推测或输出模型私有思维链；只用可见消息、工具行为和交付结果。
4. Skill 名使用 3-64 位小写英文、数字和连字符。
5. 无论如何都产出 Skill。只有同时存在稳定的多步工具执行或可参数化交付流程时，asset_kind 才是 both；否则是 skill。
6. 步骤、规则和输出标准要可操作、可验证，不写空泛口号。
"""
    try:
        result = await run_structured_utility_task(
            agent_id=evidence.agent_id,
            system_prompt=system_prompt,
            user_prompt=_prompt_for_evidence(evidence),
            purpose="collaboration_distillation",
            result_model=DistillationBlueprint,
            timeout_seconds=90,
        )
    except Exception as exc:
        return build_fallback_blueprint(evidence), False, str(exc)
    if result is None:
        return build_fallback_blueprint(evidence), False, None
    return result.value, True, None


def _markdown_list(items: list[str], fallback: str) -> str:
    values = [item.strip() for item in items if item.strip()]
    if not values:
        values = [fallback]
    return "\n".join(f"- {item}" for item in values)


def _numbered_list(items: list[str], fallback: str) -> str:
    values = [item.strip() for item in items if item.strip()]
    if not values:
        values = [fallback]
    return "\n".join(f"{index}. {item}" for index, item in enumerate(values, 1))


def _skill_content(blueprint: DistillationBlueprint) -> str:
    return f"""---
name: {blueprint.skill_name}
description: {blueprint.skill_description.strip()}
---

# {blueprint.title.strip()}

## Use when
{_markdown_list(blueprint.use_when, '遇到需要多轮校准后交付的同类任务。')}

## Variable inputs
{_markdown_list(blueprint.variable_inputs, '当前任务目标、输入材料和验收条件。')}

## Workflow
{_numbered_list(blueprint.workflow_steps, '澄清目标，执行任务，核验后交付。')}

## Decision rules
{_markdown_list(blueprint.decision_rules, '优先遵循用户最新的明确约束。')}

## 用户纠正
{_markdown_list(blueprint.corrections, '本次没有需要固化的额外纠正。')}

## Tool strategy
{_markdown_list(blueprint.tool_strategy, '按当前环境选择工具，不固化一次性路径和账号。')}

## Failure modes
{_markdown_list(blueprint.failure_modes, '不要只复制上一次结果，应根据新输入重新验证。')}

## Output expectations
{_markdown_list(blueprint.output_expectations, '交付可直接使用、结构清楚且可复核的结果。')}

## Boundaries
- 不复用原对话中的密钥、账号、私密资料和绝对本地路径。
- 不把一次性项目事实写成通用规则。
- 不输出或伪造模型私有思维链。
"""


def _capability_instruction(blueprint: DistillationBlueprint) -> str:
    workflow = _numbered_list(blueprint.workflow_steps, "完成 {{task_goal}}。")
    decisions = _markdown_list(blueprint.decision_rules, "遵循用户当前约束。")
    outputs = _markdown_list(blueprint.output_expectations, "交付可复核的结果。")
    return (
        "任务目标：{{task_goal}}\n\n"
        f"可复用流程：\n{workflow}\n\n"
        f"判断规则：\n{decisions}\n\n"
        f"交付标准：\n{outputs}"
    )


def build_draft_from_blueprint(
    *,
    evidence: CollaborationEvidence,
    blueprint: DistillationBlueprint,
    source_run: TaskRun,
    used_utility_model: bool,
    utility_error: str | None = None,
    anchor_response_id: str | None = None,
) -> CapabilityDraft:
    now = datetime.now(timezone.utc)
    capability = Capability(
        id=str(uuid4()),
        name=blueprint.title.strip() or "协作沉淀能力",
        description=blueprint.summary.strip(),
        enabled=False,
        tags=["协作沉淀", "聊天"],
        source_run_id=source_run.id,
        parameters=[
            CapabilityParameter(
                name="task_goal",
                label="本次任务目标",
                description="运行时替换为当前需要完成的同类任务。",
            )
        ],
        steps=[
            CapabilityStep(
                index=0,
                title="按沉淀方法执行",
                mode="dynamic",
                kind="agent_prompt",
                instruction_template=_capability_instruction(blueprint),
                required_parameters=["task_goal"],
                risk_level="low",
            ),
            CapabilityStep(
                index=1,
                title="核验并交付",
                mode="fixed",
                kind="delivery",
                instruction_template="核对验收条件，把结果与必要的验证说明交付给用户。",
                required_parameters=[],
                risk_level="low",
            ),
        ],
        created_at=now,
        updated_at=now,
    )
    warnings: list[str] = []
    if not used_utility_model:
        warnings.append("辅助智能未可用，已使用本地规则生成可继续编辑的草稿。")
    if utility_error:
        warnings.append("智能提炼未通过结构校验，已安全回退到本地规则。")
    if evidence.redacted_count:
        warnings.append(
            f"生成前已过滤 {evidence.redacted_count} 处可能的密钥或敏感字段。"
        )
    if evidence.truncated:
        warnings.append("会话较长，已保留开头目标和截止前的关键轮次。")

    scope = CollaborationScope(
        chat_id=evidence.chat_id,
        anchor_run_id=evidence.anchor_run_id,
        anchor_response_id=anchor_response_id,
        message_count=len(evidence.messages),
        turn_count=evidence.turn_count,
        tool_call_count=len(evidence.tool_names),
        started_at=evidence.started_at,
        ended_at=evidence.ended_at,
        truncated=evidence.truncated,
        redacted_count=evidence.redacted_count,
    )
    insights = CollaborationInsights(
        summary=blueprint.summary,
        reusable_goal=blueprint.reusable_goal,
        corrections=blueprint.corrections,
        decision_rules=blueprint.decision_rules,
        failure_modes=blueprint.failure_modes,
        tool_strategy=blueprint.tool_strategy,
        output_expectations=blueprint.output_expectations,
    )
    suggested_skill = SuggestedSkillDraft(
        name=blueprint.skill_name,
        description=blueprint.skill_description,
        content=_skill_content(blueprint),
        tags=["协作沉淀", "聊天"],
        reason=(
            f"吸收了截至所点回复的 {evidence.turn_count} 轮协作，"
            "并将用户纠正、稳定步骤和交付标准抽象为 Skill。"
        ),
        attach_to_step_index=0 if blueprint.asset_kind == "both" else None,
    )
    return CapabilityDraft(
        capability=capability,
        source_run=source_run,
        primary_asset=blueprint.asset_kind,
        scope=scope,
        insights=insights,
        suggested_skills=[suggested_skill],
        evidence=[
            f"范围：会话开始 → 所点回复（{scope.message_count} 条可见消息，{scope.turn_count} 轮）",
            f"用户纠正：识别到 {len(blueprint.corrections)} 条可复用的明确修正",
            f"稳定流程：提炼为 {len(blueprint.workflow_steps)} 个步骤",
            f"工具与交付：{len(evidence.tool_names)} 类工具，{len(evidence.artifact_titles)} 个产物记录",
        ],
        warnings=warnings,
    )


def _source_run_for_evidence(workspace: Any, evidence: CollaborationEvidence) -> TaskRun:
    for run in materialize_task_runs(
        evidence.source_workspace_dir or workspace.workspace_dir,
        agent_id=evidence.agent_id,
        limit=500,
    ):
        if run.id == evidence.anchor_run_id:
            return run
    return TaskRun(
        id=evidence.anchor_run_id,
        agent_id=evidence.agent_id,
        source="chat",
        chat_id=evidence.chat_id,
        title=evidence.chat_title,
        user_intent=next(
            (item.text for item in reversed(evidence.messages) if item.role == "user"),
            evidence.chat_title,
        ),
        status="succeeded",
        started_at=evidence.started_at or datetime.now(timezone.utc),
        ended_at=evidence.ended_at,
        steps=[
            TaskStep(
                index=0,
                kind="message",
                title="截止所点回复的协作",
                detail=f"{len(evidence.messages)} 条可见消息",
                status="succeeded",
                started_at=evidence.started_at,
                ended_at=evidence.ended_at,
            )
        ],
    )


def _runtime_workspace_for_chat(workspace: Any, chat: Any) -> Path:
    agent_root = Path(workspace.workspace_dir).expanduser().resolve()
    meta = getattr(chat, "meta", None)
    candidate_value = meta.get("topic_workspace_dir") if isinstance(meta, dict) else None
    if not isinstance(candidate_value, str) or not candidate_value.strip():
        return agent_root
    candidate = Path(candidate_value).expanduser().resolve()
    try:
        candidate.relative_to(agent_root)
    except ValueError:
        return agent_root
    return candidate if candidate.is_dir() else agent_root


async def collect_workspace_evidence(
    workspace: Any,
    payload: CollaborationDraftRequest,
) -> CollaborationEvidence:
    chat_manager = getattr(workspace, "chat_manager", None)
    runner = getattr(workspace, "runner", None)
    session = getattr(runner, "session", None)
    if chat_manager is None or session is None:
        raise RuntimeError("会话存储尚未就绪。")
    chat = await chat_manager.get_chat(payload.chat_id)
    if chat is None:
        raise ValueError("没有找到要沉淀的会话。")
    state = await session.get_session_state_dict(
        chat.session_id,
        chat.user_id,
        chat.channel,
    )
    converted = await legacy_memory_state_to_chat_messages(
        state.get("agent", {}).get("memory", {}) if state else {}
    )
    runtime_workspace = _runtime_workspace_for_chat(workspace, chat)
    events = list(_iter_events_for_workspace(runtime_workspace))
    return build_collaboration_evidence(
        agent_id=workspace.agent_id,
        chat_id=payload.chat_id,
        chat_title=chat.name or "会话协作",
        anchor_run_id=payload.anchor_run_id,
        events=events,
        messages=converted,
        source_workspace_dir=str(runtime_workspace),
    )


def _stage(
    step: str,
    title: str,
    detail: str,
    *,
    status: Literal["running", "completed", "failed"] = "running",
    elapsed_seconds: int | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "stage",
        "step": step,
        "status": status,
        "title": title,
        "detail": detail,
    }
    if elapsed_seconds is not None:
        event["elapsed_seconds"] = elapsed_seconds
    return event


async def stream_collaboration_draft_events(
    workspace: Any,
    payload: CollaborationDraftRequest,
) -> AsyncIterator[dict[str, Any]]:
    yield _stage(
        "collect",
        "读取会话范围",
        "正在定位从第一句到所点回复的完整协作。",
    )
    try:
        evidence = await collect_workspace_evidence(workspace, payload)
        yield _stage(
            "collect",
            "读取会话范围",
            f"已收集 {len(evidence.messages)} 条可见消息、{evidence.turn_count} 轮协作。",
            status="completed",
        )
        yield _stage(
            "sanitize",
            "过滤敏感与一次性信息",
            (
                f"已处理 {evidence.redacted_count} 处敏感字段。"
                if evidence.redacted_count
                else "未发现需要移除的密钥形式。"
            ),
            status="completed",
        )
        yield _stage(
            "distill",
            "提炼纠正与稳定方法",
            "正在识别哪些调整值得下次直接复用。",
        )
        task = asyncio.create_task(distill_blueprint(evidence))
        elapsed = 0.0
        while not task.done():
            done, _ = await asyncio.wait(
                {task},
                timeout=UTILITY_HEARTBEAT_SECONDS,
            )
            if done:
                break
            elapsed += UTILITY_HEARTBEAT_SECONDS
            visible_elapsed = round(elapsed)
            yield _stage(
                "distill",
                "提炼纠正与稳定方法",
                f"沈行知正在整理协作方法，已经过 {visible_elapsed} 秒。",
                elapsed_seconds=visible_elapsed,
            )
        blueprint, used_utility_model, utility_error = await task
        yield _stage(
            "distill",
            "提炼纠正与稳定方法",
            f"已找到 {len(blueprint.corrections)} 条纠正、{len(blueprint.workflow_steps)} 个稳定步骤。",
            status="completed",
        )
        yield _stage(
            "classify",
            "判断沉淀形态",
            (
                "这段协作同时包含可执行流程，建议生成 Skill + 能力。"
                if blueprint.asset_kind == "both"
                else "这段协作更适合沉淀为 Skill。"
            ),
            status="completed",
        )
        yield _stage(
            "draft",
            "生成可复核草稿",
            "正在组装 Skill 结构、边界和交付标准。",
        )
        source_run = _source_run_for_evidence(workspace, evidence)
        draft = build_draft_from_blueprint(
            evidence=evidence,
            blueprint=blueprint,
            source_run=source_run,
            used_utility_model=used_utility_model,
            utility_error=utility_error,
            anchor_response_id=payload.anchor_response_id,
        )
        yield _stage(
            "draft",
            "生成可复核草稿",
            "草稿已完成，等待你确认后再保存。",
            status="completed",
        )
        yield {
            "type": "result",
            "operation_id": str(uuid4()),
            "draft": draft.model_dump(mode="json"),
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        yield {
            "type": "error",
            "message": str(exc) or "沉淀过程未完成。",
        }


def encode_sse_event(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
