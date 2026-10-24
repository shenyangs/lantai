# -*- coding: utf-8 -*-
"""Entry receipts must identify the selected file
and settled owned processes.
"""

from __future__ import annotations

# These behavior locks intentionally inspect private harness operations.
# pylint: disable=protected-access

import hashlib
import importlib.util
import plistlib
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[3]


def _module():
    name = "packaged_office_entry_smoke"
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "scripts" / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _app(tmp_path):
    app = tmp_path / "Synthetic.app"
    for relative in (
        "Contents/MacOS/Synthetic",
        "Contents/Resources/app.asar",
        (
            "Contents/Resources/lantai-server/Lantai "
            "Backend.app/Contents/MacOS/lantai-server"
        ),
    ):
        path = app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"inert component")
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": "Synthetic"}),
    )
    return app


def _fake_entry(
    module,
    monkeypatch,
    tmp_path,
    *,
    wrong_document=False,
    unsettled=False,
    mutate=False,
):
    """No app or process is launched; only receipt behavior is exercised."""
    fixture = tmp_path / "selected.pdf"
    fixture.write_bytes(b"inert selected file")
    (tmp_path / "evidence").mkdir()
    root = tmp_path / "isolated-user-data"
    root.mkdir()
    process = SimpleNamespace(pid=101, returncode=None)
    process.poll = lambda: process.returncode
    process.terminate = lambda: setattr(process, "returncode", 0)
    process.kill = process.terminate
    process.wait = lambda timeout: 0
    identity = {
        "url": "file:///editor/pdf/index.html",
        "title": "selected.pdf",
        "file_title": "other.pdf" if wrong_document else "selected.pdf",
        "file_title_attribute": str(fixture),
        "global_available": True,
        "editor": "pdf",
        "document_path": str(fixture),
    }
    session = SimpleNamespace(
        process=process,
        root=root,
        port=1234,
        log=tmp_path / "log",
        cdp=SimpleNamespace(evaluate=lambda expression: dict(identity)),
        renderer_identity=dict(identity),
        process_settlement={},
        workbench={
            "state": {
                "active": True,
                "visible": True,
                "kind": "pdf",
                "display_name": fixture.name,
                "workbench_id": hashlib.sha256(
                    f"pdf:{fixture}".encode(),
                ).hexdigest()[:20],
            },
        },
    )

    def stop():
        process.terminate()
        session.process_settlement = {
            "ok": not unsettled,
            "live": [],
            "unknown": [{"pid": 102}] if unsettled else [],
        }
        if unsettled:
            raise AssertionError(
                "Office owned process settlement is incomplete",
            )
        shutil.rmtree(root)

    session.stop = stop

    def capture(_session_or_port, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (900, 650), "white")
        ImageDraw.Draw(image).rectangle(
            (80, 80, 700, 500),
            outline="black",
            width=12,
        )
        image.save(target)
        if mutate:
            fixture.write_bytes(b"unexpectedly mutated during entry smoke")
        return {"mean_luma": 230, "luma_range": 255, "dark_ratio": 0.1}, dict(
            identity,
        )

    monkeypatch.setattr(
        module,
        "_launch",
        lambda *args: session,
        raising=False,
    )
    if hasattr(module, "tempfile"):
        monkeypatch.setattr(
            module.tempfile,
            "mkdtemp",
            lambda **kwargs: str(root),
        )
    if hasattr(module, "subprocess"):
        monkeypatch.setattr(
            module.subprocess,
            "Popen",
            lambda *args, **kwargs: process,
        )
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        module,
        "_windows_for_pid",
        lambda pid: [
            {
                "layer": 0,
                "bounds": {"x": 0, "y": 40, "width": 900, "height": 650},
            },
        ],
    )
    monkeypatch.setattr(module, "_capture_renderer", capture)
    return fixture, session


def test_entry_metadata_requires_all_three_app_components(tmp_path):
    module = _module()
    app = _app(tmp_path)
    (app / "Contents/Resources/app.asar").unlink()
    with pytest.raises(FileNotFoundError):
        module._app_metadata(app)


def test_visible_entry_for_another_document_is_rejected(monkeypatch, tmp_path):
    module = _module()
    fixture, session = _fake_entry(
        module,
        monkeypatch,
        tmp_path,
        wrong_document=True,
    )
    with pytest.raises(AssertionError, match="(document|renderer)"):
        module._run_one(tmp_path, fixture, tmp_path / "evidence", 1)
    assert session.process_settlement.get("ok") is True


def test_entry_cannot_pass_with_unknown_owned_child(monkeypatch, tmp_path):
    module = _module()
    fixture, session = _fake_entry(
        module,
        monkeypatch,
        tmp_path,
        unsettled=True,
    )
    with pytest.raises(AssertionError, match="settlement"):
        module._run_one(tmp_path, fixture, tmp_path / "evidence", 1)
    assert session.root.exists()


def test_entry_open_must_not_change_the_fixture(monkeypatch, tmp_path):
    module = _module()
    fixture, _session = _fake_entry(module, monkeypatch, tmp_path, mutate=True)
    with pytest.raises(AssertionError, match="(changed|hash|fixture)"):
        module._run_one(tmp_path, fixture, tmp_path / "evidence", 1)


def test_entry_receipt_binds_files_and_is_returned_after_settlement(
    monkeypatch,
    tmp_path,
):
    module = _module()
    fixture, session = _fake_entry(module, monkeypatch, tmp_path)
    result = module._run_one(tmp_path, fixture, tmp_path / "evidence", 1)
    assert (
        result["fixture_sha256"]
        == hashlib.sha256(fixture.read_bytes()).hexdigest()
    )
    assert (
        result["screenshot_sha256"]
        == hashlib.sha256(Path(result["screenshot"]).read_bytes()).hexdigest()
    )
    assert result["renderer"]["document_path"] == str(fixture)
    assert result["process_settlement"] == {
        "ok": True,
        "live": [],
        "unknown": [],
    }
    assert session.process.poll() == 0
    assert not session.root.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "Contents/Resources/app.asar",
        (
            "Contents/Resources/lantai-server/Lantai "
            "Backend.app/Contents/MacOS/lantai-server"
        ),
    ],
)
def test_entry_rejects_app_component_change_during_smoke(
    monkeypatch,
    tmp_path,
    relative,
):
    module = _module()
    app = _app(tmp_path)
    fixture = tmp_path / "selected.pdf"
    fixture.write_bytes(b"inert selected file")
    output = tmp_path / "receipt.json"

    def run_one(*_args):
        (app / relative).write_bytes(b"different app component")
        return {}

    monkeypatch.setattr(module, "_run_one", run_one)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "entry",
            "--app",
            str(app),
            "--fixture",
            str(fixture),
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--evidence-output",
            str(output),
        ],
    )
    with pytest.raises(AssertionError, match="(artifact|component|changed)"):
        module.main()
    assert not output.exists()
