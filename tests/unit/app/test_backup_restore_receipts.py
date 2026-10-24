# -*- coding: utf-8 -*-
"""Restore operation receipts keep response loss separate from execution."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers import backup as backup_router


def _request(operation_id: str) -> dict:
    return {
        "operation_id": operation_id,
        "mode": "custom",
        "include_agents": False,
        "agent_ids": [],
        "include_global_config": True,
        "include_secrets": False,
        "include_skill_pool": False,
        "default_workspace_dir": None,
    }


@pytest.fixture(autouse=True)
def _clear_restore_operations():
    backup_router._restore_operations.clear()
    backup_router._restore_operation_tasks.clear()
    yield
    for task in backup_router._restore_operation_tasks.values():
        task.cancel()
    backup_router._restore_operations.clear()
    backup_router._restore_operation_tasks.clear()


@pytest.mark.asyncio
async def test_restore_receipt_is_queryable_while_running_and_after_apply(
    monkeypatch,
):
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def fake_execute_restore(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()

    monkeypatch.setattr(backup_router, "execute_restore", fake_execute_restore)
    app = FastAPI()
    app.include_router(backup_router.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        post = asyncio.create_task(
            client.post(
                "/backups/backup-1/restore",
                json=_request("restore-operation-1"),
            ),
        )
        await started.wait()

        running = await client.get(
            "/backups/restore-operations/restore-operation-1",
        )
        assert running.status_code == 200
        assert running.json()["state"] == "running"

        duplicate = asyncio.create_task(
            client.post(
                "/backups/backup-1/restore",
                json=_request("restore-operation-1"),
            ),
        )
        await asyncio.sleep(0)
        assert calls == 1

        finish.set()
        first_response, duplicate_response = await asyncio.gather(
            post,
            duplicate,
        )

        assert first_response.status_code == 200
        assert duplicate_response.status_code == 200
        assert first_response.json()["state"] == "applied"
        assert duplicate_response.json()["state"] == "applied"
        applied = await client.get(
            "/backups/restore-operations/restore-operation-1",
        )
        assert applied.json()["state"] == "applied"
        assert calls == 1


@pytest.mark.asyncio
async def test_operation_id_cannot_be_reused_for_a_different_restore(
    monkeypatch,
):
    async def fake_execute_restore(*_args, **_kwargs):
        return None

    monkeypatch.setattr(backup_router, "execute_restore", fake_execute_restore)
    app = FastAPI()
    app.include_router(backup_router.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/backups/backup-1/restore",
            json=_request("restore-operation-2"),
        )
        conflict = await client.post(
            "/backups/backup-2/restore",
            json=_request("restore-operation-2"),
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "operation_id_conflict"
