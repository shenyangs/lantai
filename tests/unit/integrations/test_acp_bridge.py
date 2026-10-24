from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from lantai.integrations.acp_server.bridge import (
    AcpBridgeError,
    AcpSessionStore,
    AcpWorkspaceGrant,
    LantaiAcpBridge,
)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://lantai.test/api",
        transport=httpx.MockTransport(handler),
    )


def _session_store(path: Path) -> AcpSessionStore:
    return AcpSessionStore(
        path,
        encrypt_value=lambda value: f"encrypted:{value}",
        decrypt_value=lambda value: value.removeprefix("encrypted:"),
    )


def test_pairing_session_store_encrypts_and_reloads_exact_origin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "acp-sessions.json"
    store = _session_store(path)

    store.save(
        "http://127.0.0.1:8123",
        "browser-secret",
        int(time.time()) + 3600,
    )

    raw = path.read_text(encoding="utf-8")
    assert '"session": "browser-secret"' not in raw
    assert '"session": "encrypted:browser-secret"' in raw
    assert json.loads(raw)["version"] == 1
    assert store.load("http://127.0.0.1:8123") == "browser-secret"
    assert store.load("http://127.0.0.1:8124") is None


@pytest.mark.asyncio
async def test_cached_pairing_session_is_attached_to_first_request(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = _session_store(tmp_path / "acp-sessions.json")
    store.save(
        "http://127.0.0.1:8123",
        "cached-session",
        int(time.time()) + 3600,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "lantai_browser_session=cached-session" in request.headers[
            "cookie"
        ]
        return httpx.Response(
            200,
            json=[
                {
                    "id": "grant-cached",
                    "project_root": str(project),
                    "status": "active",
                },
            ],
        )

    client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8123/api",
        transport=httpx.MockTransport(handler),
    )
    bridge = LantaiAcpBridge(client=client, session_store=store)

    grant = await bridge.require_workspace_grant(str(project))

    assert grant.grant_id == "grant-cached"
    await client.aclose()


@pytest.mark.asyncio
async def test_pairing_code_is_loopback_only_and_session_is_cached(
    tmp_path: Path,
) -> None:
    expires_at = int(time.time()) + 3600

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/browser-pairing/complete"
        assert json.loads(request.content) == {"code": "ABCD-EFGH"}
        return httpx.Response(
            200,
            json={"paired": True, "expires_at": expires_at},
            headers={
                "set-cookie": (
                    "lantai_browser_session=session-123; Path=/; HttpOnly"
                ),
            },
        )

    client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8123/api",
        transport=httpx.MockTransport(handler),
    )
    store = _session_store(tmp_path / "acp-sessions.json")
    bridge = LantaiAcpBridge(client=client, session_store=store)

    assert await bridge.pair("ABCD-EFGH") == expires_at
    assert store.load("http://127.0.0.1:8123") == "session-123"
    await client.aclose()

    remote = httpx.AsyncClient(base_url="https://example.com/api")
    remote_bridge = LantaiAcpBridge(client=remote)
    with pytest.raises(AcpBridgeError, match="loopback"):
        await remote_bridge.pair("ABCD-EFGH")
    await remote.aclose()


@pytest.mark.asyncio
async def test_desktop_auth_failure_explains_one_time_pairing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": "Browser pairing required",
                "code": "desktop_token_required",
            },
        )

    client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8123/api",
        transport=httpx.MockTransport(handler),
    )
    bridge = LantaiAcpBridge(client=client)

    with pytest.raises(AcpBridgeError, match="lantai acp pair"):
        await bridge.require_workspace_grant(str(Path.cwd()))
    await client.aclose()


@pytest.mark.asyncio
async def test_pairing_code_recovers_first_protected_request(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    expires_at = int(time.time()) + 3600
    protected_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal protected_calls
        if request.url.path.endswith("/browser-pairing/complete"):
            return httpx.Response(
                200,
                json={"paired": True, "expires_at": expires_at},
                headers={
                    "set-cookie": (
                        "lantai_browser_session=session-456; Path=/; HttpOnly"
                    ),
                },
            )
        protected_calls += 1
        if protected_calls == 1:
            return httpx.Response(
                403,
                json={
                    "detail": "Browser pairing required",
                    "code": "desktop_token_required",
                },
            )
        assert "lantai_browser_session=session-456" in request.headers["cookie"]
        return httpx.Response(
            200,
            json=[
                {
                    "id": "grant-456",
                    "project_root": str(project),
                    "status": "active",
                },
            ],
        )

    client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8123/api",
        transport=httpx.MockTransport(handler),
    )
    bridge = LantaiAcpBridge(
        client=client,
        pairing_code="ABCD-EFGH",
        session_store=_session_store(tmp_path / "acp-sessions.json"),
    )

    grant = await bridge.require_workspace_grant(str(project))

    assert grant.grant_id == "grant-456"
    assert protected_calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_workspace_grant_must_match_the_exact_ide_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Agent-Id"] == "default"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "grant-1",
                    "project_root": str(project),
                    "access_mode": "read_write",
                    "trust_state": "trusted",
                    "status": "active",
                },
            ],
        )

    client = _client(handler)
    bridge = LantaiAcpBridge(client=client)
    grant = await bridge.require_workspace_grant(str(project))

    assert grant.grant_id == "grant-1"
    assert grant.project_root == project.resolve()
    with pytest.raises(AcpBridgeError, match="has not been authorized"):
        await bridge.require_workspace_grant(str(sibling))
    await client.aclose()


@pytest.mark.asyncio
async def test_prompt_uses_lantai_task_api_and_preserves_workspace_authority(
    tmp_path: Path,
) -> None:
    project = tmp_path.resolve()
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            body = __import__("json").loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"task_id": "task-1", "status": "submitted"},
            )
        return httpx.Response(
            200,
            json={
                "task_id": "task-1",
                "status": "finished",
                "result": {
                    "output": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                        },
                    ],
                },
            },
        )

    client = _client(handler)
    bridge = LantaiAcpBridge(client=client, poll_interval_seconds=0.01)
    result = await bridge.run_prompt(
        session_id="acp-session",
        text="inspect this project",
        workspace=AcpWorkspaceGrant(
            grant_id="grant-1",
            project_root=project,
            access_mode="read_write",
            trust_state="trusted",
        ),
    )

    assert result.text == "done"
    assert result.stop_reason == "end_turn"
    submitted = requests[0][2]
    assert submitted is not None
    assert submitted["session_id"] == "acp-session"
    assert submitted["channel"] == "acp"
    assert submitted["channel_meta"]["workspace_grant_id"] == "grant-1"
    assert submitted["channel_meta"]["coding_mode"] is True
    assert submitted["input"][0]["content"][0]["text"] == (
        "inspect this project"
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_targets_the_same_authoritative_background_task(
    tmp_path: Path,
) -> None:
    prompt_started = asyncio.Event()
    cancelled = False
    cancel_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        if request.method == "POST" and request.url.path.endswith(
            "/agent/process/task",
        ):
            prompt_started.set()
            return httpx.Response(200, json={"task_id": "task-9"})
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            cancelled = True
            cancel_paths.append(request.url.path)
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(
            200,
            json={"status": "cancelled" if cancelled else "running"},
        )

    client = _client(handler)
    bridge = LantaiAcpBridge(client=client, poll_interval_seconds=0.01)
    prompt_task = asyncio.create_task(
        bridge.run_prompt(
            session_id="acp-cancel",
            text="long task",
            workspace=AcpWorkspaceGrant(
                grant_id="grant-9",
                project_root=tmp_path,
                access_mode="read_write",
                trust_state="trusted",
            ),
        ),
    )
    await prompt_started.wait()
    await asyncio.sleep(0)

    assert await bridge.cancel("acp-cancel") is True
    result = await prompt_task

    assert result.stop_reason == "cancelled"
    assert cancel_paths == ["/api/agent/process/task/task-9/cancel"]
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_turn_is_rejected_before_a_second_task_is_submitted(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    submission_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submission_count
        if request.method == "POST":
            submission_count += 1
            return httpx.Response(200, json={"task_id": "task-one"})
        await release.wait()
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {
                    "output": [
                        {
                            "content": [{"type": "text", "text": "done"}],
                        },
                    ],
                },
            },
        )

    client = _client(handler)
    bridge = LantaiAcpBridge(client=client, poll_interval_seconds=0.01)
    workspace = AcpWorkspaceGrant(
        grant_id="grant-one",
        project_root=tmp_path,
        access_mode="read_write",
        trust_state="trusted",
    )
    first = asyncio.create_task(
        bridge.run_prompt(
            session_id="same-session",
            text="first",
            workspace=workspace,
        ),
    )
    while submission_count == 0:
        await asyncio.sleep(0)

    with pytest.raises(AcpBridgeError, match="already has a running"):
        await bridge.run_prompt(
            session_id="same-session",
            text="second",
            workspace=workspace,
        )
    assert submission_count == 1

    release.set()
    await first
    await client.aclose()
