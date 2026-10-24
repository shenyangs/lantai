"""Office launch policy and actual process ownership; no Provider or network."""

import asyncio
import json
import os
from pathlib import Path
import signal
import sys

import psutil
import pytest

from lantai.app import office_documents
from lantai.app.mcp.stateful_client import StdIOStatefulClient


def test_native_helpers_cannot_reenable_updates_or_auto_residents(monkeypatch):
    monkeypatch.setenv("OFFICECLI_SKIP_UPDATE", "0")
    monkeypatch.setenv("OFFICECLI_NO_AUTO_RESIDENT", "0")
    monkeypatch.setenv("HOME", "/synthetic/original-home")
    env = office_documents._officecli_env()
    assert env["OFFICECLI_SKIP_UPDATE"] == "1"
    assert env["OFFICECLI_NO_AUTO_RESIDENT"] == "1"
    assert env["HOME"] == "/synthetic/original-home"


_SERVER = '''import json,os,signal,subprocess,sys
from pathlib import Path
import psutil
home=Path(os.environ["HOME"])
config=home/".officecli"/"config.json"
child=None
if os.environ.get("TEST_CHILD"):
    code="import signal,time; "
    if os.environ.get("TEST_IGNORE_TERM"):
        code+="signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    code+="print('ready',flush=True); time.sleep(120)"
    child=subprocess.Popen([sys.executable,"-c",code],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE)
    child.stdout.readline()
payload={"pid":os.getpid(),"birth":psutil.Process().create_time(),"home":str(home),
    "config":json.loads(config.read_text()) if config.exists() else None,
    "skip":os.environ.get("OFFICECLI_SKIP_UPDATE"),
    "no_resident":os.environ.get("OFFICECLI_NO_AUTO_RESIDENT"),
    "flush":os.environ.get("OFFICECLI_RESIDENT_FLUSH"),"cwd":os.getcwd(),
    "custom":os.environ.get("TEST_PRESERVED"),
    "child":{"pid":child.pid,"birth":psutil.Process(child.pid).create_time()} if child else None}
Path(os.environ["TEST_RECEIPT"]).write_text(json.dumps(payload))
for line in sys.stdin:
    message=json.loads(line)
    if "id" not in message: continue
    method=message.get("method"); params=message.get("params",{})
    if method=="initialize":
        if os.environ.get("TEST_NO_INITIALIZE"): continue
        result={"protocolVersion":params["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"synthetic-office","version":"1"}}
    elif method=="tools/list":
        result={"tools":[{"name":"inspect","description":"Synthetic process evidence","inputSchema":{"type":"object","properties":{}}}]}
    elif method=="tools/call":
        result={"content":[{"type":"text","text":json.dumps(payload)}],"isError":False}
    else: result={}
    print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}),flush=True)
if os.environ.get("TEST_WAIT_AFTER_EOF"):
    import time
    time.sleep(120)
'''


def _client(tmp_path, *, env=None, key="officecli"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    user_home = tmp_path / "user-home"
    (user_home / ".officecli").mkdir(parents=True)
    (user_home / ".officecli" / "config.json").write_text(
        '{"autoUpdate":true,"userPreference":"preserved"}',
    )
    receipt = tmp_path / "receipt.json"
    client = StdIOStatefulClient(
        "display-name-independent-of-key",
        sys.executable,
        args=[str(script), "mcp"],
        env={
            "HOME": str(user_home),
            "OFFICECLI_SKIP_UPDATE": "0",
            "OFFICECLI_NO_AUTO_RESIDENT": "0",
            "TEST_RECEIPT": str(receipt),
            "TEST_PRESERVED": "kept",
            **(env or {}),
        },
        cwd=str(tmp_path),
    )
    client._lantai_client_key = key
    return client, user_home, receipt


def _live(identity):
    try:
        process = psutil.Process(identity["pid"])
        return (
            process.create_time() == identity["birth"]
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


def _cleanup_failed_lock(receipt):
    if not receipt.exists():
        return
    body = json.loads(receipt.read_text())
    for identity in (body.get("child"), body):
        if identity and _live(identity):
            psutil.Process(identity["pid"]).send_signal(signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="packaged POSIX native policy")
async def test_builtin_connections_disable_native_config_without_user_writes(tmp_path):
    client, user_home, receipt = _client(tmp_path)
    user_config = user_home / ".officecli" / "config.json"
    before = user_config.read_bytes()
    homes = []
    try:
        await client.connect(timeout=5)
        for round_index in range(2):
            result = await client.call_tool("inspect", {})
            body = json.loads(result.content[0].text)
            homes.append(Path(body["home"]))
            assert homes[-1] != user_home
            assert body["config"]["autoUpdate"] is False
            assert body["skip"] == body["no_resident"] == "1"
            assert body["flush"] == "each"
            assert body["cwd"] == str(tmp_path)
            assert body["custom"] == "kept"
            assert user_config.read_bytes() == before
            if round_index == 0:
                await client.reload(timeout=5)
                assert not homes[-1].exists()
        assert homes[0] != homes[1]
    finally:
        await client.close()
        _cleanup_failed_lock(receipt)
    assert all(not home.exists() for home in homes)
    assert user_config.read_bytes() == before
    assert client.read_timeout_seconds == 300.0


@pytest.mark.skipif(os.name != "posix", reason="packaged POSIX native policy")
async def test_parallel_connections_have_independent_native_configs(tmp_path):
    entries = [_client(tmp_path / str(index)) for index in range(2)]
    try:
        await asyncio.gather(*(entry[0].connect(timeout=5) for entry in entries))
        bodies = [json.loads(entry[2].read_text()) for entry in entries]
        assert bodies[0]["home"] != bodies[1]["home"]
        assert all(body["config"] == {"autoUpdate": False} for body in bodies)
    finally:
        await asyncio.gather(*(entry[0].close() for entry in entries))
        for entry in entries:
            _cleanup_failed_lock(entry[2])


@pytest.mark.skipif(os.name != "posix", reason="packaged POSIX native policy")
async def test_config_failure_blocks_native_launch(monkeypatch, tmp_path):
    client, _, receipt = _client(tmp_path)
    write_bytes = Path.write_bytes

    def deny_config_write(path, data):
        if path.name == "config.json" and path.parent.name == ".officecli":
            raise PermissionError("synthetic private config refusal")
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", deny_config_write)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.connect(timeout=1)
    finally:
        await client.close()
        _cleanup_failed_lock(receipt)
    assert not receipt.exists(), "a failed policy must never launch native defaults"


@pytest.mark.skipif(os.name != "posix", reason="actual POSIX group settlement")
@pytest.mark.parametrize("ignore_term", [False, True])
async def test_close_settles_child_after_native_leader_exits(tmp_path, ignore_term):
    client, _, receipt = _client(
        tmp_path,
        env={"TEST_CHILD": "1", "TEST_IGNORE_TERM": "1" if ignore_term else ""},
    )
    body = None
    try:
        await client.connect(timeout=5)
        body = json.loads(receipt.read_text())
        assert _live(body) and _live(body["child"])
        assert os.getpgid(body["pid"]) == body["pid"]
        assert os.getpgid(body["child"]["pid"]) == body["pid"]
        await client.close()
        assert not _live(body)
        assert not _live(body["child"]), "leader EOF cannot abandon its live child"
        assert client._lifecycle_task is None
    finally:
        await client.close()
        _cleanup_failed_lock(receipt)


@pytest.mark.skipif(os.name != "posix", reason="actual POSIX group settlement")
async def test_cancelled_initialization_settles_its_process_group(tmp_path):
    client, _, receipt = _client(
        tmp_path,
        env={"TEST_CHILD": "1", "TEST_NO_INITIALIZE": "1"},
    )
    connecting = asyncio.create_task(client.connect(timeout=10))
    try:
        async def wait_for_receipt():
            while not receipt.exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_receipt(), timeout=5)
        body = json.loads(receipt.read_text())
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert not _live(body)
        assert not _live(body["child"])
        assert client._lifecycle_task is None
    finally:
        if not connecting.done():
            connecting.cancel()
        await client.close()
        _cleanup_failed_lock(receipt)


@pytest.mark.skipif(os.name != "posix", reason="local environment observation")
async def test_unrelated_stdio_client_keeps_its_native_preferences(tmp_path):
    client, user_home, receipt = _client(tmp_path, key="other-mcp")
    try:
        await client.connect(timeout=5)
        body = json.loads(receipt.read_text())
        assert body["home"] == str(user_home)
        assert body["config"]["autoUpdate"] is True
        assert body["skip"] == body["no_resident"] == "0"
    finally:
        await client.close()
        _cleanup_failed_lock(receipt)


@pytest.mark.skipif(os.name != "posix", reason="actual POSIX group settlement")
async def test_denied_signal_retains_owner_until_actual_settlement(monkeypatch, tmp_path):
    from lantai.app.mcp import officecli_transport as transport

    client, _, receipt = _client(tmp_path, env={"TEST_CHILD": "1"})
    denied = asyncio.Event()
    permit = asyncio.Event()
    signal_group = transport._PosixScope.signal

    def controlled_signal(scope, value):
        if not permit.is_set():
            denied.set()
            raise PermissionError("synthetic signal refusal")
        signal_group(scope, value)

    monkeypatch.setattr(transport._PosixScope, "signal", controlled_signal)
    closing = None
    try:
        await client.connect(timeout=5)
        body = json.loads(receipt.read_text())
        closing = asyncio.create_task(client.close(ignore_errors=False))
        await asyncio.wait_for(denied.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not closing.done(), "signal refusal cannot acknowledge cleanup"
        owner = client._lifecycle_task
        for _ in range(3):
            owner.cancel()
            await asyncio.sleep(0.02)
        assert client._lifecycle_task is owner and not owner.done()
        assert _live(body["child"])
        assert Path(body["home"]).exists()
        with pytest.raises(RuntimeError):
            await client.connect(timeout=1)
        permit.set()
        await closing
        assert not _live(body) and not _live(body["child"])
        assert not Path(body["home"]).exists()
        assert client._lifecycle_task is None
    finally:
        permit.set()
        if closing is not None:
            await closing
        await client.close()
        _cleanup_failed_lock(receipt)


@pytest.mark.skipif(os.name != "posix", reason="actual POSIX admission barrier")
async def test_cancel_during_spawn_keeps_the_actual_handle(monkeypatch, tmp_path):
    from lantai.app.mcp import officecli_transport as transport

    client, _, receipt = _client(tmp_path, env={"TEST_CHILD": "1"})
    admitted = asyncio.Event()
    permit = asyncio.Event()
    open_process = transport.anyio.open_process

    async def delayed_open(*args, **kwargs):
        process = await open_process(*args, **kwargs)
        admitted.set()
        await permit.wait()
        return process

    monkeypatch.setattr(transport.anyio, "open_process", delayed_open)
    connecting = asyncio.create_task(client.connect(timeout=10))
    try:
        await asyncio.wait_for(admitted.wait(), timeout=5)
        while not receipt.exists():
            await asyncio.sleep(0.01)
        body = json.loads(receipt.read_text())
        connecting.cancel()
        await asyncio.sleep(0.05)
        assert not connecting.done()
        assert client._lifecycle_task is not None
        assert _live(body) and _live(body["child"])
        permit.set()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert not _live(body) and not _live(body["child"])
        assert not Path(body["home"]).exists()
    finally:
        permit.set()
        await client.close()
        _cleanup_failed_lock(receipt)


@pytest.mark.skipif(os.name != "posix", reason="actual POSIX identity refusal")
async def test_failed_identity_capture_retains_the_started_process(monkeypatch, tmp_path):
    from lantai.app.mcp import officecli_transport as transport

    client, _, receipt = _client(
        tmp_path,
        env={"TEST_CHILD": "1", "TEST_WAIT_AFTER_EOF": "1"},
    )
    denied = asyncio.Event()

    def deny_identity(scope, process):
        denied.set()
        raise PermissionError("synthetic process identity refusal")

    monkeypatch.setattr(transport._PosixScope, "__init__", deny_identity)
    connecting = asyncio.create_task(client.connect(timeout=10))
    try:
        await asyncio.wait_for(denied.wait(), timeout=5)
        while not receipt.exists():
            await asyncio.sleep(0.01)
        body = json.loads(receipt.read_text())
        connecting.cancel()
        await asyncio.sleep(0.05)
        assert not connecting.done(), "identity refusal cannot abandon a started process"
        owner = client._lifecycle_task
        assert owner is not None and not owner.done()
        for _ in range(3):
            owner.cancel()
            await asyncio.sleep(0.02)
        assert not owner.done() and not connecting.done()
        assert _live(body) and _live(body["child"])
        assert Path(body["home"]).exists()
        _cleanup_failed_lock(receipt)
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert not _live(body) and not _live(body["child"])
        assert not Path(body["home"]).exists()
        assert client._lifecycle_task is None
    finally:
        _cleanup_failed_lock(receipt)
        await client.close()
