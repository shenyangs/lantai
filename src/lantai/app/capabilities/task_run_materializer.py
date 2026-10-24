# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..experience.events import _iter_events_for_workspace
from ..experience.schemas import ChatExperienceEvent
from ...runtime.runs import runtime_run_status_projection_rank
from .models import Artifact, TaskRun, TaskRunSource, TaskRunStatus, TaskStep

def _parse_dt(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _short(value: Any, limit: int = 4000) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _tool_input_detail(payload: dict[str, Any], fallback: str) -> str:
    """Project canonical or legacy tool input into stable human-readable text."""

    for key in ("tool_input", "arguments", "input", "detail"):
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if isinstance(value, (dict, list)):
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return str(value)
    return fallback


def _source_for_event(event: ChatExperienceEvent) -> TaskRunSource:
    if event.kind.startswith("cron."):
        return "cron"
    if event.kind.startswith("image."):
        return "image"
    return "chat"


def _event_group_id(event: ChatExperienceEvent) -> str:
    if event.run_id:
        return event.run_id
    if event.chat_id:
        return event.chat_id
    return event.id


def _normalize_task_status(value: Any) -> TaskRunStatus | None:
    status = str(value or "").strip().lower()
    if not status:
        return None
    if status in {"finished", "completed", "complete", "success", "succeeded", "ok"}:
        return "succeeded"
    if status in {"failed", "failure", "error", "rejected"}:
        return "failed"
    if status in {"stalled"}:
        return "stalled"
    if status in {"timeout", "timed_out"}:
        return "timed_out"
    if status in {"blocked"}:
        return "blocked"
    if status in {"cancelled", "canceled", "interrupted"}:
        return "cancelled"
    return None


def _event_terminal_status(event: ChatExperienceEvent) -> TaskRunStatus | None:
    if event.kind == "chat.error":
        return "failed"
    if event.kind == "chat.final_answer":
        return "succeeded"
    if not event.kind.endswith(".settled"):
        return None

    payload = event.payload or {}
    return (
        _normalize_task_status(payload.get("final_task_status"))
        or _normalize_task_status(payload.get("final_status"))
        or _normalize_task_status(event.status)
    )


def _step_status(event: ChatExperienceEvent, *, terminal: bool) -> str:
    if event.kind == "chat.turn.stop_requested":
        return "succeeded"
    terminal_status = _event_terminal_status(event)
    if terminal_status in {"failed", "timed_out", "cancelled"}:
        return "failed"
    if terminal_status in {"blocked", "stalled", "succeeded"}:
        return terminal_status
    if event.kind.endswith(".started") and not terminal:
        return "running"
    if event.kind.endswith(".started"):
        return "succeeded"
    if event.kind.endswith(".error") or event.kind == "chat.error":
        return "failed"
    if event.status in {"failed", "stalled"}:
        return event.status
    if event.status == "interrupted":
        return "failed"
    return "succeeded"


def _run_status(events: list[ChatExperienceEvent]) -> TaskRunStatus:
    terminal_status: TaskRunStatus | None = None
    terminal_rank = runtime_run_status_projection_rank("running")
    for event in events:
        status = _event_terminal_status(event)
        if not status:
            continue
        rank = runtime_run_status_projection_rank(status)
        if rank >= terminal_rank:
            terminal_status = status
            terminal_rank = rank
    return terminal_status or "running"


def _artifact_from_event(event: ChatExperienceEvent) -> Artifact | None:
    payload = event.payload or {}
    path = payload.get("path") or payload.get("file_path")
    url = payload.get("url")
    title = payload.get("title") or payload.get("name") or path or url
    if not title:
        return None
    artifact_type = str(payload.get("type") or "file")
    if artifact_type not in {
        "file",
        "image",
        "markdown",
        "html",
        "docx",
        "xlsx",
        "wiki_page",
        "email_draft",
    }:
        artifact_type = "image" if str(payload.get("mime_type") or "").startswith("image/") else "file"
    return Artifact(
        type=artifact_type,  # type: ignore[arg-type]
        title=str(title),
        path=str(path) if path else None,
        url=str(url) if url else None,
        mime_type=str(payload.get("mime_type")) if payload.get("mime_type") else None,
        size=int(payload["size"]) if isinstance(payload.get("size"), int) else None,
        created_at=_parse_dt(event.ts),
    )


def _step_from_event(
    event: ChatExperienceEvent,
    *,
    index: int,
    terminal: bool,
) -> TaskStep:
    payload = event.payload or {}
    kind = "message"
    title = event.summary or event.kind
    detail = event.summary
    tool_name = None
    metadata: dict[str, Any] = {
        "experience_event_id": event.id,
        "experience_kind": event.kind,
    }

    if event.kind == "chat.turn.started":
        title = "用户请求"
        detail = payload.get("text_preview") or event.summary
        metadata["workspace_dir"] = payload.get("workspace_dir")
    elif event.kind == "chat.turn.settled":
        title = "本轮结束"
        detail = f"流式事件 {payload.get('active_run_event_count') or 0} 条"
    elif event.kind.startswith("chat.tool_call."):
        kind = "tool_call"
        title = str(payload.get("title") or payload.get("tool_name") or "工具调用")
        detail = _tool_input_detail(payload, event.summary)
        tool_name = str(payload.get("tool_name") or payload.get("name") or "")
    elif event.kind == "chat.tool_result":
        kind = "tool_result"
        title = str(payload.get("title") or payload.get("tool_name") or "工具结果")
        detail = payload.get("result") or payload.get("detail") or event.summary
        tool_name = str(payload.get("tool_name") or payload.get("name") or "")
    elif event.kind.startswith("chat.approval."):
        kind = "approval"
        title = "风险确认"
        detail = payload.get("detail") or payload.get("decision") or event.summary
        metadata["approval_id"] = payload.get("approval_id")
    elif event.kind == "chat.artifact.created":
        kind = "artifact"
        title = str(payload.get("title") or payload.get("path") or "产物")
        detail = payload.get("path") or payload.get("url") or event.summary
    elif event.kind == "chat.final_answer":
        title = "最终回复"
        detail = payload.get("text") or event.summary
    elif event.kind == "chat.error":
        kind = "error"
        title = "运行错误"
        detail = payload.get("error") or event.summary
    elif event.kind == "cron.job.started":
        title = "定时任务开始"
        detail = payload.get("job_name") or payload.get("job_id") or event.summary
    elif event.kind == "cron.job.settled":
        title = "定时任务结束"
        detail = payload.get("error_text") or payload.get("final_status") or event.summary
    elif event.kind == "image.task.started":
        title = "已退役图片任务"
        detail = payload.get("prompt") or event.summary
    elif event.kind == "image.task.settled":
        title = "已退役图片任务"
        detail = payload.get("final_status") or event.summary

    return TaskStep(
        index=index,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        detail=_short(detail),
        status=_step_status(event, terminal=terminal),  # type: ignore[arg-type]
        tool_name=tool_name or None,
        started_at=_parse_dt(event.ts),
        ended_at=_parse_dt(event.ts) if not event.kind.endswith(".started") else None,
        metadata={key: value for key, value in metadata.items() if value is not None},
    )


def _title_for_events(events: list[ChatExperienceEvent]) -> str:
    for event in events:
        payload = event.payload or {}
        if event.kind == "chat.turn.started" and payload.get("text_preview"):
            return _short(payload.get("text_preview"), 48)
        if payload.get("job_name"):
            return str(payload["job_name"])
        if payload.get("title"):
            return str(payload["title"])
    source = _source_for_event(events[0])
    return {
        "chat": "聊天任务",
        "cron": "定时任务",
        "image": "已退役图片任务",
    }.get(source, "任务")


def _intent_for_events(events: list[ChatExperienceEvent]) -> str | None:
    for event in events:
        payload = event.payload or {}
        for key in ("text_preview", "prompt", "job_name", "summary"):
            if payload.get(key):
                return str(payload[key])
    return events[0].summary if events else None


def materialize_task_runs(
    workspace_dir: Path | str,
    *,
    agent_id: str | None = None,
    limit: int = 120,
) -> list[TaskRun]:
    """Build TaskRun views from the append-only experience ledger."""

    grouped: dict[str, list[ChatExperienceEvent]] = defaultdict(list)
    for event in _iter_events_for_workspace(workspace_dir):
        if agent_id and event.agent_id != agent_id:
            continue
        grouped[_event_group_id(event)].append(event)

    runs: list[TaskRun] = []
    for group_id, events in grouped.items():
        events.sort(key=lambda item: item.ts)
        if not events:
            continue
        status = _run_status(events)
        terminal = status != "running"
        artifacts = [
            artifact
            for event in events
            if event.kind in {"chat.artifact.created", "image.task.settled"}
            if (artifact := _artifact_from_event(event)) is not None
        ]
        steps = [
            _step_from_event(event, index=index, terminal=terminal)
            for index, event in enumerate(events)
        ]
        started_at = _parse_dt(events[0].ts)
        ended_at = _parse_dt(events[-1].ts) if terminal else None
        first = events[0]
        runs.append(
            TaskRun(
                id=f"experience:{group_id}",
                agent_id=first.agent_id,
                source=_source_for_event(first),
                chat_id=first.chat_id,
                session_id=first.session_id,
                title=_title_for_events(events),
                user_intent=_intent_for_events(events),
                status=status,
                started_at=started_at,
                ended_at=ended_at,
                steps=steps,
                artifacts=artifacts,
            )
        )

    return sorted(runs, key=lambda item: item.started_at, reverse=True)[:limit]
