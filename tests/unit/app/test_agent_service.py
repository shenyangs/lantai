# -*- coding: utf-8 -*-
"""Tests for the Lantai-owned agent process API facade."""

import asyncio

import pytest
import httpx
from fastapi import FastAPI

from lantai.app.agent_service import LantaiAgentService


class _FakeRunner:
    async def stream_query(self, request):
        _ = request
        yield {"status": "created", "output": []}
        yield {"status": "completed", "output": [{"content": []}]}


class _NativeEventRunner:
    async def stream_query(self, request):
        _ = request
        yield {
            "object": "message",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "text", "text": "native reply"}],
        }
        yield {
            "object": "response",
            "status": "completed",
            "output": [],
        }


class _ErrorEventRunner:
    async def stream_query(self, request):
        _ = request
        yield {"type": "error", "error": "agent failed"}


class _BlockingRunner:
    async def stream_query(self, request):
        _ = request
        await asyncio.Event().wait()
        if False:
            yield {}


@pytest.mark.asyncio
async def test_agent_service_streams_sse_lines():
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_FakeRunner(),
    )

    chunks = []
    async for chunk in service._stream_generator({"input": []}):
        chunks.append(chunk)

    assert chunks[0].startswith('data: {"status": "created"')
    assert chunks[1].startswith('data: {"status": "completed"')


@pytest.mark.asyncio
async def test_agent_service_background_task_keeps_final_response():
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_FakeRunner(),
    )
    task_id = "task-test"
    service.active_tasks[task_id] = {
        "task_id": task_id,
        "status": "submitted",
    }

    await service._run_stream_task(task_id, {"input": []})

    status = service.get_task_status(task_id)
    assert status["status"] == "finished"
    assert status["result"]["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_service_background_task_keeps_native_completed_message():
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_NativeEventRunner(),
    )
    task_id = "task-native"
    service.active_tasks[task_id] = {
        "task_id": task_id,
        "status": "submitted",
    }

    await service._run_stream_task(
        task_id,
        {"session_id": "sid-native", "input": []},
    )

    status = service.get_task_status(task_id)
    assert status["status"] == "finished"
    assert status["result"]["status"] == "completed"
    assert status["result"]["session_id"] == "sid-native"
    assert status["result"]["output"][0]["content"][0]["text"] == (
        "native reply"
    )


@pytest.mark.asyncio
async def test_agent_service_background_task_marks_error_event_failed():
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_ErrorEventRunner(),
    )
    task_id = "task-error"
    service.active_tasks[task_id] = {
        "task_id": task_id,
        "status": "submitted",
    }

    await service._run_stream_task(task_id, {"input": []})

    status = service.get_task_status(task_id)
    assert status["status"] == "failed"
    assert status["error"]["message"] == "agent failed"


@pytest.mark.asyncio
async def test_agent_service_cancels_running_background_task() -> None:
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_BlockingRunner(),
    )
    task_id = "task-cancel"
    service.active_tasks[task_id] = {
        "task_id": task_id,
        "status": "submitted",
    }
    running = asyncio.create_task(
        service._run_stream_task(task_id, {"input": []}),
    )
    service.active_tasks[task_id]["_task"] = running
    await asyncio.sleep(0)

    cancelled = await service.cancel_task(task_id)
    await asyncio.gather(running, return_exceptions=True)

    assert cancelled["status"] == "cancelled"
    assert service.get_task_status(task_id)["status"] == "cancelled"


def test_agent_service_resolves_per_task_timeout_with_safe_bounds() -> None:
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_FakeRunner(),
        stream_task_timeout=900,
    )

    assert service._task_timeout_seconds({}) == 900
    assert service._task_timeout_seconds(
        {"metadata": {"agent_task_timeout_seconds": 1200}}
    ) == 1200
    assert service._task_timeout_seconds(
        {"metadata": {"agent_task_timeout_seconds": 5}}
    ) == 60
    assert service._task_timeout_seconds(
        {"metadata": {"agent_task_timeout_seconds": 9999}}
    ) == 3600


@pytest.mark.asyncio
async def test_agent_service_public_task_status_keeps_requested_session_identity() -> None:
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_FakeRunner(),
    )
    request = {"session_id": "child-session-1", "input": []}

    task_id = service.create_background_task(request)
    status = service.get_task_status(task_id)

    assert status["session_id"] == "child-session-1"
    assert status["status"] == "submitted"
    service.active_tasks[task_id]["_task"].cancel()
    await asyncio.gather(
        service.active_tasks[task_id]["_task"],
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_agent_service_deduplicates_identical_background_dispatch() -> None:
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_BlockingRunner(),
    )
    request = {
        "session_id": "child-session-idempotent",
        "metadata": {
            "background_task_idempotency_key": "room-1:member-1:attempt-1",
        },
        "input": [],
    }

    first_task_id = service.create_background_task(request)
    second_task_id = service.create_background_task(request)

    assert second_task_id == first_task_id
    assert len(service.active_tasks) == 1
    assert service.get_task_status(first_task_id)["idempotent_replay"] is True
    service.active_tasks[first_task_id]["_task"].cancel()
    await asyncio.gather(
        service.active_tasks[first_task_id]["_task"],
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_agent_service_resolves_existing_task_by_submission_key() -> None:
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_BlockingRunner(),
    )
    request = {
        "session_id": "acp-session",
        "metadata": {"background_task_idempotency_key": "acp-submission-1"},
        "input": [],
    }
    task_id = service.create_background_task(request)

    assert service.get_task_by_submission("acp-submission-1")["task_id"] == task_id
    assert service.get_task_by_submission("different-key")["status"] == "not_found"
    assert len(service.active_tasks) == 1
    service.active_tasks[task_id]["_task"].cancel()
    await asyncio.gather(
        service.active_tasks[task_id]["_task"],
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_agent_service_submission_lookup_route_is_read_only() -> None:
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_BlockingRunner(),
    )
    app = FastAPI()
    app.include_router(service.router, prefix="/agent")
    request = {
        "session_id": "acp-route",
        "metadata": {"background_task_idempotency_key": "acp-route-key"},
        "input": [],
    }
    task_id = service.create_background_task(request)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        found = await client.get("/agent/process/task/submission/acp-route-key")
        missing = await client.get("/agent/process/task/submission/other-key")
    assert found.json()["task_id"] == task_id
    assert missing.json()["status"] == "not_found"
    assert len(service.active_tasks) == 1
    service.active_tasks[task_id]["_task"].cancel()
    await asyncio.gather(
        service.active_tasks[task_id]["_task"],
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_agent_service_rejects_idempotency_key_payload_conflict() -> None:
    service = LantaiAgentService(
        app_name="Test",
        app_description="",
        runner=_BlockingRunner(),
    )
    first_request = {
        "session_id": "child-session-conflict",
        "metadata": {
            "background_task_idempotency_key": "room-1:member-1:attempt-1",
        },
        "input": [{"role": "user", "content": [{"text": "first"}]}],
    }
    second_request = {
        **first_request,
        "input": [{"role": "user", "content": [{"text": "changed"}]}],
    }

    task_id = service.create_background_task(first_request)
    with pytest.raises(ValueError, match="different request payload"):
        service.create_background_task(second_request)

    service.active_tasks[task_id]["_task"].cancel()
    await asyncio.gather(
        service.active_tasks[task_id]["_task"],
        return_exceptions=True,
    )
