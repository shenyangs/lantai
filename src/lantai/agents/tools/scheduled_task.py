# -*- coding: utf-8 -*-
"""Tools for preparing user-confirmed scheduled task drafts."""

from __future__ import annotations

import json

from ...runtime.tool_response import TextBlock, ToolResponse


def _build_agent_input(prompt: str) -> list[dict[str, object]]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        },
    ]


def _cron_warnings(cron: str, timezone: str, task_type: str) -> list[str]:
    warnings: list[str] = []
    parts = [part for part in cron.split() if part]
    if len(parts) != 5:
        warnings.append("Cron 表达式需要 5 段，保存前请检查。")
    if not timezone:
        warnings.append("未提供时区，将由前端使用默认时区。")
    if task_type == "text":
        warnings.append("text 任务只会发送固定文本，不会让 AI 执行工作。")
    return warnings


async def prepare_scheduled_task(
    name: str,
    cron: str,
    prompt: str,
    timezone: str = "Asia/Shanghai",
    enabled: bool = True,
    summary: str = "",
    task_type: str = "agent",
) -> ToolResponse:
    """Prepare a scheduled task draft for the chat UI to confirm.

    Use this tool when the user asks in natural language to create, schedule,
    automate, remind, or run a recurring task, for example "每天 9 点帮我做早报",
    "每周一上午 10 点检查项目状态", or "定时提醒我...".

    This tool is deliberately side-effect free: it does NOT create, save, or
    enable a task. It only returns a structured draft. The chat UI will show a
    confirmation card, and the task is created only after the user clicks the
    confirm button.

    Convert the user's schedule into a 5-field cron expression in the provided
    timezone. Use three-letter English day names for weekdays, e.g.
    "0 9 * * mon" for every Monday at 09:00. If the user asks for a one-time
    reminder and the recurring cron behavior is ambiguous, ask a follow-up
    question instead of calling this tool.

    Args:
        name: Short user-facing task name.
        cron: Five-field cron expression, e.g. "0 9 * * *".
        prompt: The instruction the agent should execute on each run.
        timezone: IANA timezone name. Prefer the user's configured timezone.
        enabled: Whether the task should be enabled after confirmation.
        summary: Optional short explanation of the interpreted schedule.
        task_type: "agent" runs the AI with prompt; "text" sends fixed text.

    Returns:
        ToolResponse: A JSON scheduled-task draft for frontend confirmation.
    """
    clean_name = (name or "").strip() or "定时任务"
    clean_cron = (cron or "").strip() or "0 9 * * *"
    clean_prompt = (prompt or "").strip()
    clean_timezone = (timezone or "").strip() or "Asia/Shanghai"
    clean_summary = (summary or "").strip()
    safe_task_type = task_type if task_type in {"agent", "text"} else "agent"

    spec: dict[str, object] = {
        "name": clean_name,
        "enabled": bool(enabled),
        "schedule": {
            "type": "cron",
            "cron": clean_cron,
            "timezone": clean_timezone,
        },
        "task_type": safe_task_type,
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {
                "user_id": "",
                "session_id": "",
            },
            "mode": "stream",
        },
        "runtime": {
            "share_session": True,
            "max_concurrency": 1,
            "timeout_seconds": 120,
            "misfire_grace_seconds": 60,
        },
        "meta": {
            "created_from": "chat_natural_language",
            "draft_tool": "prepare_scheduled_task",
        },
    }

    if safe_task_type == "text":
        spec["text"] = clean_prompt
    else:
        spec["request"] = {
            "input": _build_agent_input(clean_prompt),
            "user_id": "",
            "session_id": "",
        }

    payload = {
        "type": "scheduled_task_draft",
        "summary": clean_summary,
        "prompt": clean_prompt,
        "warnings": _cron_warnings(clean_cron, clean_timezone, safe_task_type),
        "spec": spec,
    }

    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            ),
        ],
    )
