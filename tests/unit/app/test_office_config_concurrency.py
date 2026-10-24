"""Synthetic actual-ASGI config concurrency and external atomic reader lock."""
from __future__ import annotations
import asyncio
import json
import threading
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import lantai.app.office_documents as office_documents
import lantai.app.routers.office_documents as routes


def test_actual_concurrent_partial_puts_do_not_lose_preferences(monkeypatch, tmp_path):
    config_path = tmp_path / "office_documents.json"
    monkeypatch.setattr(office_documents, "_CONFIG_FILE", config_path)
    office_documents.save_officecli_config(office_documents.OfficeCliConfig())
    original_load = office_documents._load_config
    second_read = threading.Event()
    counter_lock = threading.Lock()
    read_count = 0

    def load_with_interleaving():
        nonlocal read_count
        current = original_load()
        with counter_lock:
            read_count += 1
            nth = read_count
        if nth == 1:
            # Two unprotected read-modify-write calls both read old values.
            # If update owns a lock, the timeout allows its protected write.
            second_read.wait(0.2)
        elif nth == 2:
            second_read.set()
        return current

    monkeypatch.setattr(office_documents, "_load_config", load_with_interleaving)
    monkeypatch.setattr(routes, "get_officecli_status", lambda: SimpleNamespace(to_dict=lambda: {"ok": True}))
    app = FastAPI()
    app.include_router(routes.router)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            left, right = await asyncio.gather(
                client.put("/office-documents/config", json={"enabled": False}),
                client.put("/office-documents/config", json={"officecli_path": "/synthetic/new-officecli"}),
            )
            assert left.status_code == right.status_code == 200
    asyncio.run(scenario())
    state = original_load()
    assert state.enabled is False, "partial path PUT erased concurrent enabled preference"
    assert state.officecli_path == "/synthetic/new-officecli", "partial enabled PUT erased concurrent executable preference"


def test_actual_config_writer_never_exposes_partial_json(monkeypatch, tmp_path):
    config_path = tmp_path / "office_documents.json"
    monkeypatch.setattr(office_documents, "_CONFIG_FILE", config_path)
    office_documents.save_officecli_config(office_documents.OfficeCliConfig(enabled=False, officecli_path="seed"))
    running = threading.Event()
    stopped = threading.Event()
    failures = []
    reads = 0
    payload = "/synthetic/" + "x" * 262144

    def writer():
        running.set()
        try:
            for index in range(20):
                office_documents.update_officecli_config(officecli_path=payload + str(index))
        except BaseException as exc:
            failures.append("writer: " + type(exc).__name__)
        finally:
            stopped.set()

    owned = threading.Thread(target=writer, daemon=True)
    owned.start()
    assert running.wait(1)
    try:
        while not stopped.is_set():
            raw = config_path.read_bytes()
            reads += 1
            try:
                data = json.loads(raw)
                if data.get("enabled") is not False or not isinstance(data.get("officecli_path"), str):
                    failures.append("reader: incomplete preferences")
            except (ValueError, TypeError):
                failures.append("reader: partial JSON")
    finally:
        owned.join(5)
    assert not owned.is_alive(), "synthetic config writer was not settled"
    assert reads > 0, "external reader did not overlap real writes"
    assert not failures, "; ".join(sorted(set(failures)))
