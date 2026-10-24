import asyncio
import io
import json
import sys
import tarfile

import pytest

from lantai.app.reading.managed import (
    ASSETS,
    ManagedRSSHub,
    ReadingRuntimeError,
    extract_node_archive,
)
from lantai.app.reading.store import ReadingStore
from lantai.app.reading import catalog


def test_runtime_manifest_is_portable_and_every_dependency_is_locked():
    package = json.loads((ASSETS / "package.json").read_text())
    lock = json.loads((ASSETS / "package-lock.json").read_text())
    assert package["dependencies"]["rsshub"] == "1.0.0-master.85c7a8a"
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    for key, item in lock["packages"].items():
        assert not key or key.startswith("node_modules/")
        if item.get("resolved"):
            assert item["resolved"].startswith("https://")
            assert item.get("integrity")
    assert "--ignore-certificate-errors" in (ASSETS / "bridge.mjs").read_text()
    assert "chromiumSandbox: true" in (ASSETS / "bridge.mjs").read_text()


def test_node_archive_cannot_escape_reading_runtime_destination(tmp_path):
    archive = tmp_path / "node.tgz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("node/../../escaped")
        info.size = 1
        handle.addfile(info, io.BytesIO(b"x"))

    with pytest.raises(ReadingRuntimeError):
        extract_node_archive(archive, tmp_path / "runtime")
    assert not (tmp_path / "escaped").exists()


def test_local_configuration_is_encrypted_and_only_names_are_exposed(
    tmp_path, monkeypatch
):
    from cryptography.fernet import Fernet
    from lantai.security import secret_store

    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(secret_store, "_get_fernet", lambda: cipher)
    store = ReadingStore(tmp_path)
    store.save_local_config({"GITHUB_ACCESS_TOKEN": "synthetic-test-secret"})
    assert store.local_config_keys() == ["GITHUB_ACCESS_TOKEN"]
    assert b"synthetic-test-secret" not in store.path.read_bytes()
    assert store.local_config()["GITHUB_ACCESS_TOKEN"] == "synthetic-test-secret"
    store.save_local_config({"GITHUB_ACCESS_TOKEN": ""})
    assert store.local_config_keys() == []
    assert "NODE_OPTIONS" not in catalog.allowed_config_keys()
    assert "WEIBO_COOKIES" in catalog.allowed_config_keys()


def fake_installed(manager):
    manager.node.parent.mkdir(parents=True)
    manager.node.touch()
    package = manager.directory / "engine/node_modules/rsshub/dist-lib/pkg.mjs"
    package.parent.mkdir(parents=True)
    package.touch()
    (manager.directory / "complete").write_text("test")


@pytest.mark.asyncio
async def test_owned_stdio_process_stops_and_can_restart(tmp_path, monkeypatch):
    manager = ManagedRSSHub(tmp_path, cache_root=tmp_path / "cache")
    fake_installed(manager)
    create = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        return await create(
            sys.executable,
            "-u",
            "-c",
            """import sys,json
for line in sys.stdin:
 value=json.loads(line)
 print(json.dumps({'id':value['id'],'ok':True,'data':{'ready':True} if value['action']=='init' else {'title':'Feed','item':[]}}),flush=True)
""",
            **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await manager.start()
    first = manager.process
    assert manager.snapshot()["running"]
    assert (await manager.request("/cls/telegraph"))["title"] == "Feed"
    await manager.stop()
    assert first.returncode == 0 and not manager.snapshot()["running"]
    await manager.start()
    assert manager.process.pid != first.pid
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_preparation_never_claims_install_complete(tmp_path, monkeypatch):
    manager = ManagedRSSHub(tmp_path, cache_root=tmp_path / "cache")
    waiting = asyncio.Event()

    async def install():
        waiting.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(manager, "install", install)
    manager.prepare()
    await waiting.wait()
    await manager.stop()
    assert not manager.installed and manager.state == "idle"
    assert manager.task.done()


@pytest.mark.asyncio
async def test_stop_cancels_active_route_without_waiting_for_network_timeout(
    tmp_path, monkeypatch
):
    manager = ManagedRSSHub(tmp_path, cache_root=tmp_path / "cache")
    entered = asyncio.Event()

    async def start():
        pass

    async def exchange(*args, **kwargs):
        entered.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(manager, "_start", start)
    monkeypatch.setattr(manager, "_exchange", exchange)
    task = asyncio.create_task(manager.request("/slow"))
    await entered.wait()
    await asyncio.wait_for(manager.stop(), 2)
    with pytest.raises(ReadingRuntimeError, match="读取已停止"):
        await task
