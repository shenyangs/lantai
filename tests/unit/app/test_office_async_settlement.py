"""Blocking Office/PDF work remains owned through actual entry cancellation."""
from __future__ import annotations

import asyncio
import contextvars
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import lantai.app.routers.office_documents as routes
import lantai.app.office_documents as office_documents
import lantai.agents.tools.file_io as file_io
from lantai.config.context import current_workspace_dir


@pytest.mark.parametrize("route_name", ["get_status", "put_config", "post_preview", "post_compare", "read_file"])
def test_office_wait_does_not_block_unrelated_chat_stop_heartbeat(monkeypatch, tmp_path, route_name):
    ticked = threading.Event()
    observations = []
    result = SimpleNamespace(to_dict=lambda: {"ok": True}, content="fixture text", method="fixture")

    def slow_office(*args, **kwargs):
        ticked.wait(0.15)
        observations.append(ticked.is_set())
        return result

    monkeypatch.setattr(routes, "get_officecli_status", slow_office)
    monkeypatch.setattr(routes, "update_officecli_config", lambda **kwargs: None)
    monkeypatch.setattr(routes, "preview_office_document", slow_office)
    monkeypatch.setattr(routes, "compare_office_documents", slow_office)
    monkeypatch.setattr(office_documents, "preview_office_document", slow_office)
    monkeypatch.setattr(file_io, "_workspace_directory_overview", lambda _: None)
    monkeypatch.setattr(file_io, "_resolve_file_path", lambda value, **kwargs: value)
    document = tmp_path / "fixture.docx"
    document.write_bytes(b"fixture; extraction is mocked")

    async def scenario():
        async def heartbeat_and_stop():
            await asyncio.sleep(0.01)
            ticked.set()

        heartbeat = asyncio.create_task(heartbeat_and_stop())
        if route_name == "get_status":
            operation = routes.get_status()
        elif route_name == "put_config":
            operation = routes.put_config(routes.OfficeCliConfigUpdate(enabled=True))
        elif route_name == "post_preview":
            operation = routes.post_preview(routes.OfficeDocumentPreviewRequest(file_path=str(document), mode="html"))
        elif route_name == "post_compare":
            operation = routes.post_compare(routes.OfficeDocumentCompareRequest(before_file_path=str(document), after_file_path=str(document)))
        else:
            operation = file_io.read_file(str(document))
        await asyncio.gather(operation, heartbeat)

    asyncio.run(scenario())
    assert observations == [True], "the event loop could not serve chat/stop heartbeat until the synchronous Office work ended"


async def wait_until(predicate, *, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "owned work did not reach its barrier"
        await asyncio.sleep(0.005)


def call_entry(entry, client, source):
    if entry == "get_status":
        return client.get("/office-documents/status")
    if entry == "put_config":
        return client.put("/office-documents/config", json={"enabled": True})
    if entry == "post_preview":
        return client.post("/office-documents/preview", json={"file_path": str(source), "mode": "issues"})
    if entry == "post_compare":
        return client.post("/office-documents/compare", json={"before_file_path": str(source), "after_file_path": str(source)})
    return file_io.read_file(str(source))


@pytest.mark.parametrize("entry", ["get_status", "put_config", "post_preview", "post_compare", "read_file"])
def test_actual_entry_repeated_cancel_waits_for_owned_worker(monkeypatch, tmp_path, entry):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    context_observations = []
    marker = contextvars.ContextVar("audit_marker", default="missing")
    result = SimpleNamespace(to_dict=lambda: {"ok": True}, content="synthetic Office text", method="fixture")

    def slow_office(*args, **kwargs):
        context_observations.append(marker.get())
        started.set()
        try:
            assert release.wait(3), "synthetic worker release missing"
            return result
        finally:
            finished.set()

    monkeypatch.setattr(routes, "get_officecli_status", slow_office)
    monkeypatch.setattr(routes, "update_officecli_config", lambda **kwargs: None)
    monkeypatch.setattr(routes, "preview_office_document", slow_office)
    monkeypatch.setattr(routes, "compare_office_documents", slow_office)
    monkeypatch.setattr(office_documents, "preview_office_document", slow_office)
    source = tmp_path / "owned.docx"
    source.write_bytes(b"synthetic document; extraction is mocked")
    app = FastAPI()
    app.include_router(routes.router)
    failsafe = threading.Timer(0.65, release.set)
    failsafe.daemon = True

    async def scenario():
        workspace_token = current_workspace_dir.set(tmp_path)
        marker_token = marker.set("owned-turn")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
                operation = asyncio.create_task(call_entry(entry, client, source))
                try:
                    await wait_until(started.is_set)
                    assert not operation.done(), "event loop could not deliver Stop while Office worker was running"
                    operation.cancel()
                    await asyncio.sleep(0.015)
                    assert not operation.done(), "first cancel detached the running Office worker"
                    assert not finished.is_set()
                    operation.cancel()
                    await asyncio.sleep(0.015)
                    assert not operation.done(), "repeated cancel detached the running Office worker"
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(operation, 2)
                    assert finished.is_set(), "cancellation returned before worker settlement"
                    assert context_observations == ["owned-turn"]
                finally:
                    release.set()
                    await asyncio.gather(operation, return_exceptions=True)
                    await wait_until(finished.is_set)
        finally:
            marker.reset(marker_token)
            current_workspace_dir.reset(workspace_token)

    failsafe.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        failsafe.cancel()
        failsafe.join()


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("entry", ["post_preview", "read_file"])
def test_actual_officecli_subprocess_owned_through_repeated_cancel(monkeypatch, tmp_path, entry):
    ready = tmp_path / "cli-ready"
    release = tmp_path / "cli-release"
    finished = tmp_path / "cli-finished"
    executable = tmp_path / "synthetic-officecli"
    script = "\n".join([
        "#!" + sys.executable,
        "from pathlib import Path",
        "import os,time",
        "ready=Path(" + repr(str(ready)) + ")",
        "release=Path(" + repr(str(release)) + ")",
        "finished=Path(" + repr(str(finished)) + ")",
        "ready.write_text(str(os.getpid()))",
        "deadline=time.monotonic()+4",
        "while not release.exists() and time.monotonic()<deadline: time.sleep(.005)",
        "finished.write_text('done')",
        "print('synthetic Office result')",
        "",
    ])
    executable.write_text(script)
    executable.chmod(0o700)
    monkeypatch.setattr(office_documents, "get_officecli_command", lambda: str(executable))
    monkeypatch.setattr(office_documents, "_fallback_extract_text", lambda _: "")
    source = tmp_path / "owned.docx"
    source.write_bytes(b"synthetic; extractor is mocked")
    app = FastAPI()
    app.include_router(routes.router)
    failsafe = threading.Timer(1, lambda: release.write_text("release"))
    failsafe.daemon = True

    async def scenario():
        workspace_token = current_workspace_dir.set(tmp_path)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
                operation = asyncio.create_task(call_entry(entry, client, source))
                try:
                    await wait_until(ready.exists)
                    owned_pid = int(ready.read_text())
                    assert not operation.done(), "event loop could not deliver Stop while OfficeCLI was running"
                    assert pid_alive(owned_pid)
                    operation.cancel()
                    await asyncio.sleep(0.02)
                    assert not operation.done(), "first cancel detached owned OfficeCLI"
                    operation.cancel()
                    await asyncio.sleep(0.02)
                    assert not operation.done(), "repeated cancel detached owned OfficeCLI"
                    assert pid_alive(owned_pid) and not finished.exists()
                    release.write_text("release")
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(operation, 3)
                    assert finished.exists(), "OfficeCLI did not finish before cancellation returned"
                    assert not pid_alive(owned_pid), "OfficeCLI process outlived canceled owner"
                finally:
                    release.write_text("release")
                    await asyncio.gather(operation, return_exceptions=True)
                    if ready.exists():
                        await wait_until(lambda: not pid_alive(int(ready.read_text())))
        finally:
            current_workspace_dir.reset(workspace_token)

    failsafe.start()
    try:
        asyncio.run(scenario())
    finally:
        release.write_text("release")
        failsafe.cancel()
        failsafe.join()


@pytest.mark.parametrize("entry", ["post_preview", "post_compare"])
def test_async_office_routes_keep_existing_400_contract(monkeypatch, tmp_path, entry):
    def missing(*args, **kwargs):
        raise FileNotFoundError("synthetic missing file")
    monkeypatch.setattr(routes, "preview_office_document", missing)
    monkeypatch.setattr(routes, "compare_office_documents", missing)
    app = FastAPI()
    app.include_router(routes.router)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            response = await call_entry(entry, client, tmp_path / "owned.docx")
            assert response.status_code == 400
            assert response.json() == {"detail": "synthetic missing file"}
    asyncio.run(scenario())


def test_pdf_read_entry_stays_responsive_and_settles_repeated_cancel(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_pdf(path, **kwargs):
        started.set()
        try:
            assert release.wait(3)
            return {"text": "synthetic readable PDF text", "status": "text_extracted",
                    "method": "fixture", "page_count": 1}
        finally:
            finished.set()

    monkeypatch.setattr("lantai.app.wiki.core.extract_source_text_detail", slow_pdf)
    source = tmp_path / "owned.pdf"
    source.write_bytes(b"%PDF-synthetic mocked extraction")
    failsafe = threading.Timer(0.65, release.set)
    failsafe.daemon = True

    async def scenario():
        token = current_workspace_dir.set(tmp_path)
        operation = asyncio.create_task(file_io.read_file(str(source)))
        try:
            await wait_until(started.is_set)
            assert not operation.done(), "PDF reader blocked the event loop"
            operation.cancel()
            await asyncio.sleep(0.015)
            assert not operation.done(), "PDF worker detached on first cancel"
            operation.cancel()
            await asyncio.sleep(0.015)
            assert not operation.done(), "PDF worker detached on repeated cancel"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 2)
            assert finished.is_set()
        finally:
            release.set()
            await asyncio.gather(operation, return_exceptions=True)
            current_workspace_dir.reset(token)

    failsafe.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        failsafe.cancel()
        failsafe.join()
