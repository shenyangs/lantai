"""The ACP transport must retain one backend turn through ambiguous receipts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from lantai.integrations.acp_server.bridge import (
    AcpBridgeError,
    AcpWorkspaceGrant,
    LantaiAcpBridge,
)


def _workspace(root: Path) -> AcpWorkspaceGrant:
    return AcpWorkspaceGrant(
        grant_id="grant-1",
        project_root=root,
        access_mode="read_write",
        trust_state="trusted",
    )


def _bridge(handler: object) -> tuple[LantaiAcpBridge, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="http://lantai.test/api",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return LantaiAcpBridge(client=client, poll_interval_seconds=0.01), client


@pytest.mark.asyncio
async def test_cancel_while_submission_receipt_is_in_flight(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled: list[str] = []
    submitted: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/task"):
            submitted.append(json.loads(request.content)["metadata"]["background_task_idempotency_key"])
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"task_id": "task-pending"})
        if request.method == "POST" and path.endswith("/cancel"):
            cancelled.append(path)
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(200, json={"status": "cancelled" if cancelled else "running"})

    bridge, client = _bridge(handler)
    prompt = asyncio.create_task(
        bridge.run_prompt(
            session_id="acp-pending",
            text="long task",
            workspace=_workspace(tmp_path),
        ),
    )
    await asyncio.wait_for(entered.wait(), 2)
    assert await bridge.cancel("acp-pending") is True
    release.set()
    result = await asyncio.wait_for(prompt, 2)
    assert result.stop_reason == "cancelled"
    assert len(submitted) == 1
    assert cancelled == ["/api/agent/process/task/task-pending/cancel"]
    await client.aclose()


@pytest.mark.asyncio
async def test_lost_receipt_resolves_existing_submission_without_reposting(
    tmp_path: Path,
) -> None:
    submissions: list[str] = []
    lookups: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/task"):
            submissions.append(json.loads(request.content)["metadata"]["background_task_idempotency_key"])
            raise httpx.ReadError("response was lost", request=request)
        if "/task/submission/" in path:
            lookups.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"task_id": "task-accepted", "status": "running"})
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {"output": [{"content": [{"type": "text", "text": "done"}]}]},
            },
        )

    bridge, client = _bridge(handler)
    result = await bridge.run_prompt(
        session_id="acp-lost",
        text="inspect",
        workspace=_workspace(tmp_path),
    )
    assert result.text == "done"
    assert len(submissions) == 1
    assert lookups == submissions
    await client.aclose()


@pytest.mark.asyncio
async def test_poll_failure_keeps_backend_identity_for_later_cancel(
    tmp_path: Path,
) -> None:
    cancelled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/task"):
            return httpx.Response(200, json={"task_id": "task-live"})
        if request.method == "POST" and path.endswith("/cancel"):
            cancelled.append(path)
            return httpx.Response(200, json={"status": "cancelled"})
        raise httpx.ReadError("poll disconnected", request=request)

    bridge, client = _bridge(handler)
    with pytest.raises(AcpBridgeError, match="Cannot reach"):
        await bridge.run_prompt(
            session_id="acp-poll",
            text="long task",
            workspace=_workspace(tmp_path),
        )
    assert await bridge.cancel("acp-poll") is True
    assert cancelled == ["/api/agent/process/task/task-live/cancel"]
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_receipt_without_authorized_task_releases_session(
    tmp_path: Path,
) -> None:
    submissions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        path = request.url.path
        if request.method == "POST" and path.endswith("/task"):
            submissions += 1
            if submissions == 1:
                return httpx.Response(200, json={"status": "submitted"})
            return httpx.Response(200, json={"task_id": "task-second"})
        if "/task/submission/" in path:
            return httpx.Response(200, json={"status": "not_found"})
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {"output": [{"content": [{"type": "text", "text": "second done"}]}]},
            },
        )

    bridge, client = _bridge(handler)
    with pytest.raises(AcpBridgeError, match="task identity"):
        await bridge.run_prompt(
            session_id="acp-bad-receipt",
            text="first",
            workspace=_workspace(tmp_path),
        )
    result = await bridge.run_prompt(
        session_id="acp-bad-receipt",
        text="second",
        workspace=_workspace(tmp_path),
    )
    assert result.text == "second done"
    assert submissions == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_rejected_submission_releases_session_without_another_post(
    tmp_path: Path,
) -> None:
    submissions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        path = request.url.path
        if request.method == "POST" and path.endswith("/task"):
            submissions += 1
            if submissions == 1:
                return httpx.Response(403, json={"detail": "pairing required"})
            return httpx.Response(200, json={"task_id": "task-after-pairing"})
        if "/task/submission/" in path:
            raise AssertionError("A rejected request cannot own a backend task")
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {"output": [{"content": [{"type": "text", "text": "done"}]}]},
            },
        )

    bridge, client = _bridge(handler)
    with pytest.raises(AcpBridgeError, match="rejected"):
        await bridge.run_prompt(
            session_id="acp-rejected",
            text="first",
            workspace=_workspace(tmp_path),
        )
    result = await bridge.run_prompt(
        session_id="acp-rejected",
        text="second",
        workspace=_workspace(tmp_path),
    )
    assert result.text == "done"
    assert submissions == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_cancelled_prompt_coroutine_settles_late_submission(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    submissions = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        path = request.url.path
        if request.method == "POST" and path.endswith("/task"):
            submissions += 1
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"task_id": "task-late"})
        if request.method == "POST" and path.endswith("/cancel"):
            assert path.endswith("/task-late/cancel")
            cancelled.set()
            return httpx.Response(200, json={"status": "cancelled"})
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    bridge, client = _bridge(handler)
    prompt = asyncio.create_task(
        bridge.run_prompt(
            session_id="acp-detached",
            text="long task",
            workspace=_workspace(tmp_path),
        ),
    )
    await asyncio.wait_for(entered.wait(), 2)
    prompt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prompt
    release.set()
    await asyncio.wait_for(cancelled.wait(), 2)
    assert submissions == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_submission_keeps_key_for_later_cancel(
    tmp_path: Path,
) -> None:
    key = ""
    lookups = 0
    cancelled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal key, lookups
        path = request.url.path
        if request.method == "POST" and path.endswith("/task"):
            key = json.loads(request.content)["metadata"]["background_task_idempotency_key"]
            raise httpx.ReadError("receipt lost", request=request)
        if "/task/submission/" in path:
            lookups += 1
            assert path.endswith(f"/{key}")
            if lookups == 1:
                raise httpx.ReadError("lookup disconnected", request=request)
            return httpx.Response(200, json={"task_id": "task-unknown", "status": "running"})
        if request.method == "POST" and path.endswith("/cancel"):
            cancelled.append(path)
            return httpx.Response(200, json={"status": "cancelled"})
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    bridge, client = _bridge(handler)
    with pytest.raises(AcpBridgeError, match="outcome is unknown"):
        await bridge.run_prompt(
            session_id="acp-unknown",
            text="long task",
            workspace=_workspace(tmp_path),
        )
    assert await bridge.cancel("acp-unknown") is True
    assert lookups == 2
    assert cancelled == ["/api/agent/process/task/task-unknown/cancel"]
    await client.aclose()


@pytest.mark.asyncio
async def test_two_equal_text_submissions_keep_distinct_identities(
    tmp_path: Path,
) -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            keys.append(json.loads(request.content)["metadata"]["background_task_idempotency_key"])
            return httpx.Response(200, json={"task_id": f"task-{len(keys)}"})
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {"output": [{"content": [{"type": "text", "text": "done"}]}]},
            },
        )

    bridge, client = _bridge(handler)
    for _ in range(2):
        result = await bridge.run_prompt(
            session_id="acp-equal-text",
            text="same text",
            workspace=_workspace(tmp_path),
        )
        assert result.text == "done"
    assert len(keys) == 2
    assert keys[0] != keys[1]
    await client.aclose()
