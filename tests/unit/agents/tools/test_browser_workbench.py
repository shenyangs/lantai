# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from importlib import import_module

import pytest

from lantai.app.browser_workbench.models import (
    BrowserTaskConfirmation,
    BrowserTaskEvent,
    BrowserTaskRequest,
    BrowserTaskRun,
)

browser_workbench_tool = import_module("lantai.agents.tools.browser_workbench")


def _response_json(response) -> dict:
    dumped = response.model_dump(mode="json") if hasattr(response, "model_dump") else {}
    blocks = dumped.get("content") or getattr(response, "content", None) or []
    text = "\n".join(
        str(
            (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", "")
            )
            or ""
        )
        for block in blocks
        if (
            block.get("text")
            if isinstance(block, dict)
            else getattr(block, "text", None)
        )
    )
    return json.loads(text)


class FakeBrowserTaskRuntime:
    def __init__(self) -> None:
        self.created_requests: list[BrowserTaskRequest] = []
        self.confirmations: list[tuple[str, BrowserTaskConfirmation]] = []
        self.run = BrowserTaskRun(
            run_id="agent-task-1",
            profile_id="github-work",
            target_domains=["github.com"],
            access="read",
            instruction="读取 issue",
            status="queued",
            replay_run_id="agent-task-1",
            planned_action_count=3,
            metadata={
                "plan": {
                    "strategy": "lantaicli_adapter",
                    "adapter_id": "github.issues.list",
                }
            },
        )
        self.event = BrowserTaskEvent(
            run_id="agent-task-1",
            type="task_created",
            status="queued",
            message="Browser task queued.",
        )

    async def create(self, request: BrowserTaskRequest) -> BrowserTaskRun:
        self.created_requests.append(request)
        return self.run.model_copy(
            update={
                "run_id": request.run_id,
                "profile_id": request.profile_id,
                "target_domains": request.target_domains,
                "access": request.access,
                "instruction": request.instruction,
                "metadata": {
                    **request.metadata,
                    "plan": {
                        "strategy": "lantaicli_adapter",
                        "adapter_id": request.metadata.get("adapter_id", ""),
                    },
                },
            }
        )

    def list_runs(self) -> list[BrowserTaskRun]:
        return [self.run]

    def get_run(self, _run_id: str) -> BrowserTaskRun | None:
        return self.run

    def list_events(self, _run_id: str) -> list[BrowserTaskEvent]:
        return [self.event]

    def artifacts(self, run_id: str) -> dict:
        return {"run_id": run_id, "screenshots": []}

    def pause(self, _run_id: str) -> BrowserTaskRun:
        return self.run.model_copy(update={"status": "paused"})

    def resume(self, _run_id: str) -> BrowserTaskRun:
        return self.run.model_copy(update={"status": "queued"})

    def cancel(self, _run_id: str) -> BrowserTaskRun:
        return self.run.model_copy(update={"status": "cancelled"})

    async def confirm(
        self,
        run_id: str,
        confirmation: BrowserTaskConfirmation,
    ) -> BrowserTaskRun:
        self.confirmations.append((run_id, confirmation))
        return self.run


@pytest.mark.asyncio
async def test_browser_task_create_uses_shared_runtime_and_adapter_metadata(monkeypatch):
    runtime = FakeBrowserTaskRuntime()
    monkeypatch.setattr(
        browser_workbench_tool,
        "_browser_task_runtime",
        lambda: runtime,
    )

    response = await browser_workbench_tool.browser_task(
        operation="create",
        run_id="agent-task-42",
        profile_id="github-work",
        target_domains='["github.com", "api.github.com"]',
        access="read",
        instruction="读取最近 issue 并整理",
        url="https://github.com/org/repo",
        adapter_id="github.issues.list",
        metadata_json='{"limit": 5}',
        max_actions=8,
    )

    payload = _response_json(response)

    assert payload["ok"] is True
    assert payload["task"]["run_id"] == "agent-task-42"
    assert payload["task"]["metadata"]["plan"]["adapter_id"] == "github.issues.list"
    created = runtime.created_requests[0]
    assert created.target_domains == ["github.com", "api.github.com"]
    assert created.metadata["adapter_id"] == "github.issues.list"
    assert created.metadata["limit"] == 5
    assert created.max_actions == 8


@pytest.mark.asyncio
async def test_browser_task_control_operations_return_structured_payload(monkeypatch):
    runtime = FakeBrowserTaskRuntime()
    monkeypatch.setattr(
        browser_workbench_tool,
        "_browser_task_runtime",
        lambda: runtime,
    )

    events = _response_json(
        await browser_workbench_tool.browser_task(
            operation="events",
            run_id="agent-task-1",
        )
    )
    confirmation = _response_json(
        await browser_workbench_tool.browser_task(
            operation="confirm",
            run_id="agent-task-1",
            approved=True,
            reason="用户允许一次",
        )
    )

    assert events["ok"] is True
    assert events["events"][0]["type"] == "task_created"
    assert confirmation["ok"] is True
    assert runtime.confirmations[0][0] == "agent-task-1"
    assert runtime.confirmations[0][1].approved is True


@pytest.mark.asyncio
async def test_browser_task_rejects_non_object_metadata(monkeypatch):
    runtime = FakeBrowserTaskRuntime()
    monkeypatch.setattr(
        browser_workbench_tool,
        "_browser_task_runtime",
        lambda: runtime,
    )

    payload = _response_json(
        await browser_workbench_tool.browser_task(
            operation="create",
            metadata_json="[]",
        )
    )

    assert payload == {
        "ok": False,
        "error": "metadata_json must be a JSON object",
    }
    assert runtime.created_requests == []
