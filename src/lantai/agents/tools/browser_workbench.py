# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import Any

from ...app.browser_workbench import BrowserActionBroker
from ...app.browser_workbench.models import (
    BrowserAction,
    BrowserTaskConfirmation,
    BrowserTaskRequest,
)
from ...runtime.tool_response import TextBlock, ToolResponse


def _tool_response(payload: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, indent=2),
            )
        ],
    )


def _parse_metadata(metadata_json: str) -> tuple[dict[str, Any], str]:
    metadata: dict[str, Any] = {}
    if not metadata_json.strip():
        return metadata, ""
    try:
        parsed = json.loads(metadata_json)
    except json.JSONDecodeError:
        return {}, "metadata_json must be a JSON object"
    if not isinstance(parsed, dict):
        return {}, "metadata_json must be a JSON object"
    metadata = parsed
    return metadata, ""


def _parse_domains(value: str) -> list[str]:
    raw = value.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _browser_task_runtime():
    from ...app.routers.browser_workbench import get_browser_task_runtime

    return get_browser_task_runtime()


async def browser_workbench(
    action: str,
    profile_id: str = "default",
    url: str = "",
    page_id: str = "default",
    selector: str = "",
    ref: str = "",
    text: str = "",
    code: str = "",
    path: str = "",
    access: str = "",
    reason: str = "",
    run_id: str = "",
    confirmed: bool = False,
    metadata_json: str = "",
) -> ToolResponse:
    """Run an action in Lantai's built-in Browser Workbench.

    Use this whenever the user asks for the built-in/internal browser,
    login-state browsing, the current browser page, replay evidence, profile
    policy, or LantaiCLI adapters. Write actions normally require user
    confirmation.
    """

    metadata, metadata_error = _parse_metadata(metadata_json)
    if metadata_error:
        return _tool_response({"ok": False, "error": metadata_error})
    payload: dict[str, Any] = {
        "action": action,
        "profile_id": profile_id,
        "url": url,
        "page_id": page_id,
        "selector": selector,
        "ref": ref,
        "text": text,
        "code": code,
        "path": path,
        "reason": reason,
        "confirmed": confirmed,
        "metadata": metadata,
    }
    if access:
        payload["access"] = access
    if run_id:
        payload["run_id"] = run_id
    try:
        observation = await BrowserActionBroker().execute(
            BrowserAction.model_validate(payload),
        )
    except Exception as exc:  # pragma: no cover - defensive tool boundary
        return _tool_response({"ok": False, "error": str(exc)})
    return _tool_response(observation.model_dump(mode="json"))


async def browser_task(
    operation: str = "create",
    run_id: str = "",
    profile_id: str = "default",
    target_domains: str = "",
    access: str = "read",
    mode: str = "background_observable",
    instruction: str = "",
    url: str = "",
    adapter_id: str = "",
    metadata_json: str = "",
    max_actions: int = 200,
    max_duration_seconds: float = 1800.0,
    approved: bool = False,
    reason: str = "",
) -> ToolResponse:
    """Create or control an observable background Browser Workbench task.

    This is the required tool for multi-step reading, research, pagination,
    feed scrolling, collection, or summarization. Use `create`/`run` with
    `access=read`; navigation clicks, scrolling, opening details, search, and
    filtering then run silently under task-level read authority. Use `get`,
    `list`, `events`, `artifacts`, `pause`, `resume`, `cancel`, and `confirm` to
    monitor or control a task. External side effects such as like, follow,
    comment, publish, upload, payment, authorization, or profile changes still
    stop at BrowserActionBroker. Never claim the requested task is complete
    unless task.status is `completed`; otherwise disclose
    `completed_action_count/planned_action_count`, preserved artifacts, and the
    exact stop or recovery reason.
    """

    op = operation.strip().lower() or "create"
    metadata, metadata_error = _parse_metadata(metadata_json)
    if metadata_error:
        return _tool_response({"ok": False, "error": metadata_error})
    if adapter_id.strip():
        metadata.setdefault("adapter_id", adapter_id.strip())

    runtime = _browser_task_runtime()
    try:
        if op in {"create", "run", "start"}:
            request_payload: dict[str, Any] = {
                "profile_id": profile_id,
                "target_domains": _parse_domains(target_domains),
                "access": access or "read",
                "mode": mode or "background_observable",
                "instruction": instruction,
                "url": url,
                "requires_foreground_confirmation": True,
                "max_actions": max_actions,
                "max_duration_seconds": max_duration_seconds,
                "metadata": metadata,
            }
            if run_id:
                request_payload["run_id"] = run_id
            request = BrowserTaskRequest.model_validate(request_payload)
            task = await runtime.create(request)
            return _tool_response({"ok": True, "task": task.model_dump(mode="json")})
        if op == "list":
            return _tool_response(
                {
                    "ok": True,
                    "tasks": [
                        task.model_dump(mode="json") for task in runtime.list_runs()
                    ],
                },
            )
        if op in {"get", "status"}:
            task = runtime.get_run(run_id)
            return _tool_response(
                {
                    "ok": task is not None,
                    "task": task.model_dump(mode="json") if task else None,
                    "error": "" if task else "browser_task_not_found",
                },
            )
        if op == "events":
            return _tool_response(
                {
                    "ok": True,
                    "events": [
                        event.model_dump(mode="json")
                        for event in runtime.list_events(run_id)
                    ],
                },
            )
        if op == "artifacts":
            return _tool_response({"ok": True, "artifacts": runtime.artifacts(run_id)})
        if op == "pause":
            task = runtime.pause(run_id)
            return _tool_response({"ok": True, "task": task.model_dump(mode="json")})
        if op == "resume":
            task = runtime.resume(run_id)
            return _tool_response({"ok": True, "task": task.model_dump(mode="json")})
        if op == "cancel":
            task = runtime.cancel(run_id)
            return _tool_response({"ok": True, "task": task.model_dump(mode="json")})
        if op == "confirm":
            task = await runtime.confirm(
                run_id,
                BrowserTaskConfirmation(approved=approved, reason=reason),
            )
            return _tool_response({"ok": True, "task": task.model_dump(mode="json")})
    except Exception as exc:  # pragma: no cover - defensive tool boundary
        return _tool_response({"ok": False, "error": str(exc), "operation": op})
    return _tool_response(
        {
            "ok": False,
            "error": "unsupported browser_task operation",
            "operation": op,
        },
    )
