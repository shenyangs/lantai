"""Uploads stay responsive and retain ownership through cancellation."""
import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_upload_cancel_during_read_removes_partial_file(monkeypatch, tmp_path):
    from lantai.app.routers import console as module

    async def workspace(request):
        return object()

    async def channel(workspace):
        return SimpleNamespace(media_dir=tmp_path)

    monkeypatch.setattr(module, "get_agent_for_request", workspace)
    monkeypatch.setattr(module, "_get_console_channel", channel)
    waiting = asyncio.Event()

    class Upload:
        filename = "partial.txt"
        content_type = "text/plain"
        reads = 0

        async def read(self, size):
            self.reads += 1
            if self.reads == 1:
                return b"partial-upload-bytes"
            waiting.set()
            await asyncio.Event().wait()

    upload = asyncio.create_task(module.post_console_upload(None, Upload()))
    await asyncio.wait_for(waiting.wait(), timeout=5)
    assert list(tmp_path.glob(".upload-*.uploading"))
    upload.cancel()
    with pytest.raises(asyncio.CancelledError):
        await upload
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def upload_endpoint(monkeypatch, tmp_path):
    from lantai.app.routers import console as module

    async def workspace(request):
        return object()

    async def channel(workspace):
        return SimpleNamespace(media_dir=tmp_path)

    monkeypatch.setattr(module, "get_agent_for_request", workspace)
    monkeypatch.setattr(module, "_get_console_channel", channel)
    return module


@pytest.mark.asyncio
async def test_slow_upload_fsync_keeps_event_loop_responsive(
    upload_endpoint, monkeypatch, tmp_path,
):
    import hashlib
    import time
    from io import BytesIO
    from pathlib import Path
    from starlette.datastructures import UploadFile

    module = upload_endpoint
    original = module.os.fsync
    observed = []
    heartbeat = []
    loop = asyncio.get_running_loop()

    def slow_fsync(fd):
        loop.call_soon_threadsafe(lambda: heartbeat.append(time.monotonic()))
        time.sleep(0.2)
        observed.append(time.monotonic())
        original(fd)

    monkeypatch.setattr(module.os, "fsync", slow_fsync)
    payload = b"exact-upload-bytes" * 65536
    result = await module.post_console_upload(
        None, UploadFile(file=BytesIO(payload), filename="附件.txt")
    )
    await asyncio.sleep(0)
    assert observed and heartbeat
    assert heartbeat[0] < observed[0] - 0.1
    assert Path(result["url"]).read_bytes() == payload
    assert result["digest"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert not list(tmp_path.glob("*.uploading"))


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_fsync_before_cleanup(
    upload_endpoint, monkeypatch, tmp_path,
):
    import threading
    from io import BytesIO
    from starlette.datastructures import UploadFile

    module = upload_endpoint
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = module.os.fsync

    def waiting_fsync(fd):
        entered.set()
        assert release.wait(timeout=5)
        original(fd)
        finished.set()

    monkeypatch.setattr(module.os, "fsync", waiting_fsync)
    task = asyncio.create_task(module.post_console_upload(
        None, UploadFile(file=BytesIO(b"cancel-before-publish"), filename="cancel.txt")
    ))
    try:
        async def wait_entered():
            while not entered.is_set():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_entered(), timeout=3)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            assert list(tmp_path.glob(".upload-*.uploading"))
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cancel_during_publication_settles_and_retry_reuses_blob(
    upload_endpoint, monkeypatch, tmp_path,
):
    import threading
    from io import BytesIO
    from pathlib import Path
    from starlette.datastructures import UploadFile

    module = upload_endpoint
    entered = threading.Event()
    release = threading.Event()
    receipts = []
    original = module._create_console_upload_receipt

    def waiting_receipt(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        receipt = original(**kwargs)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(module, "_create_console_upload_receipt", waiting_receipt)
    payload = b"immutable-published-content"
    task = asyncio.create_task(module.post_console_upload(
        None, UploadFile(file=BytesIO(payload), filename="same.txt")
    ))
    try:
        async def wait_entered():
            while not entered.is_set():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_entered(), timeout=3)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(receipts) == 1
    blobs = [path for path in tmp_path.iterdir() if not path.name.startswith(".")]
    assert len(blobs) == 1
    assert blobs[0].read_bytes() == payload
    assert not list(tmp_path.glob(".upload-*.uploading"))
    retried = await module.post_console_upload(
        None, UploadFile(file=BytesIO(payload), filename="same.txt")
    )
    assert Path(retried["url"]) == blobs[0]
    assert retried["upload_receipt"] != receipts[0]


@pytest.mark.asyncio
async def test_upload_disk_error_cleans_partial_and_preserves_shared_blob(
    upload_endpoint, monkeypatch, tmp_path,
):
    from io import BytesIO
    from pathlib import Path
    from starlette.datastructures import UploadFile

    module = upload_endpoint
    payload = b"previous-success"
    first = await module.post_console_upload(
        None, UploadFile(file=BytesIO(payload), filename="same.txt")
    )
    before = set(tmp_path.rglob("*"))

    def broken_fsync(fd):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module.os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="simulated disk failure"):
        await module.post_console_upload(
            None, UploadFile(file=BytesIO(payload), filename="same.txt")
        )
    assert set(tmp_path.rglob("*")) == before
    assert Path(first["url"]).read_bytes() == payload
