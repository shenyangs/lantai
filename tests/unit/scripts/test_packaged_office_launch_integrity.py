# -*- coding: utf-8 -*-
"""Launch receipts must bind a document
and retain uncertain process ownership.
"""

from __future__ import annotations

# These behavior locks intentionally inspect private harness operations.
# pylint: disable=protected-access

import hashlib
import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[3]


def _module(name):
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "scripts" / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _cohort(support):
    cohort = support.OwnedProcessCohort.__new__(support.OwnedProcessCohort)
    cohort.process = SimpleNamespace(pid=101)
    cohort.born = 100.0
    cohort.sid = 101
    cohort._owners = {101: 100.0}
    cohort._lock = threading.RLock()
    cohort._monitor_error = None
    cohort._sid_compromised = False
    return cohort


def test_reused_sid_stays_untrusted_after_foreign_leader_exits(monkeypatch):
    support = _module("packaged_office_support")
    cohort = _cohort(support)
    births = {101: 200.0, 102: 201.0}

    def process(pid):
        if pid not in births:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(
            pid=pid,
            create_time=lambda: births[pid],
            status=lambda: "running",
            children=lambda recursive: [],
        )

    monkeypatch.setattr(support.psutil, "Process", process)
    monkeypatch.setattr(support.psutil, "pids", lambda: list(births))
    monkeypatch.setattr(support.os, "getsid", lambda pid: 101)
    cohort._snapshot()
    del births[101]
    snapshot = cohort._snapshot()
    assert snapshot["live"] == []
    assert cohort._owners == {101: 100.0}


def test_sid_leader_reuse_during_candidate_identity_capture_is_rejected(
    monkeypatch,
):
    support = _module("packaged_office_support")
    cohort = _cohort(support)
    births = {101: 100.0, 102: 201.0}

    def process(pid):
        def born():
            if pid == 102:
                births[101] = 200.0
            return births[pid]

        return SimpleNamespace(
            pid=pid,
            create_time=born,
            status=lambda: "running",
            children=lambda recursive: [],
        )

    monkeypatch.setattr(support.psutil, "Process", process)
    monkeypatch.setattr(support.psutil, "pids", lambda: [102])
    monkeypatch.setattr(support.os, "getsid", lambda pid: 101)
    cohort._snapshot()
    assert 102 not in cohort._owners


def test_unseen_sid_reuse_without_a_leader_cannot_adopt_foreign_orphan(
    monkeypatch,
):
    support = _module("packaged_office_support")
    cohort = _cohort(support)
    cohort.process.poll = lambda: 0
    cohort._stop = threading.Event()
    cohort._signals = []
    cohort._thread = SimpleNamespace(
        join=lambda timeout: None,
        is_alive=lambda: False,
    )
    signals = []
    births = {901: 201.0}

    def process(pid):
        if pid not in births:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(
            pid=pid,
            create_time=lambda: births[pid],
            status=lambda: "running",
            children=lambda recursive: [],
            send_signal=lambda sig: signals.append((pid, sig)),
        )

    monkeypatch.setattr(support.psutil, "Process", process)
    monkeypatch.setattr(support.psutil, "pids", lambda: list(births))
    monkeypatch.setattr(support.os, "getsid", lambda pid: 101)
    receipt = cohort.settle(term_timeout=0, kill_timeout=0)
    # Assert the exact empty signal receipt.
    # pylint: disable=use-implicit-booleaness-not-comparison
    assert (
        signals == []
    )  # pylint: disable=use-implicit-booleaness-not-comparison
    # pylint: enable=use-implicit-booleaness-not-comparison
    assert cohort._owners == {101: 100.0}
    assert receipt["ok"] is False
    assert receipt["unknown"]


def test_parent_reuse_during_descendant_capture_cannot_adopt_foreign_child(
    monkeypatch,
):
    support = _module("packaged_office_support")
    cohort = _cohort(support)
    births = {101: 100.0, 901: 201.0}

    def process(pid):
        def children(recursive):
            assert recursive is False
            births[101] = 200.0
            return [process(901)]

        return SimpleNamespace(
            pid=pid,
            create_time=lambda: births[pid],
            status=lambda: "running",
            children=children if pid == 101 else lambda recursive: [],
            ppid=lambda: 101,
        )

    monkeypatch.setattr(support.psutil, "Process", process)
    monkeypatch.setattr(support.psutil, "pids", lambda: [])
    snapshot = cohort._snapshot()
    assert 901 not in cohort._owners
    assert snapshot["unknown"]


def test_stale_recursive_tree_cannot_adopt_reused_intermediate_parent(
    monkeypatch,
):
    support = _module("packaged_office_support")
    cohort = _cohort(support)
    cohort.process.poll = lambda: None
    cohort._stop = threading.Event()
    cohort._signals = []
    cohort._thread = SimpleNamespace(
        join=lambda timeout: None,
        is_alive=lambda: False,
    )
    births = {101: 100.0, 201: 200.0, 901: 201.0}
    parents = {101: 1, 201: 999, 901: 201}
    signals = []

    def process(pid):
        def children(recursive):
            if pid == 101:
                return [
                    process(value)
                    for value in ((201, 901) if recursive else (201,))
                ]
            return [process(901)] if pid == 201 else []

        return SimpleNamespace(
            pid=pid,
            create_time=lambda: births[pid],
            ppid=lambda: parents[pid],
            status=lambda: "running",
            children=children,
            send_signal=lambda sig: signals.append((pid, sig)),
        )

    monkeypatch.setattr(support.psutil, "Process", process)
    monkeypatch.setattr(support.psutil, "pids", lambda: [])
    receipt = cohort.settle(term_timeout=0, kill_timeout=0)
    assert not any(pid in (201, 901) for pid, _sig in signals)
    assert cohort._owners == {101: 100.0}
    assert receipt["ok"] is False
    assert receipt["unknown"]


def test_layered_detached_descendants_remain_owned_after_root_exits(
    monkeypatch,
):
    support = _module("packaged_office_support")
    cohort = _cohort(support)
    births = {101: 100.0, 201: 101.0, 901: 102.0}
    parents = {101: 1, 201: 101, 901: 201}

    def process(pid):
        if pid not in births:
            raise psutil.NoSuchProcess(pid)

        def children(recursive):
            direct = [
                value
                for value, parent in parents.items()
                if parent == pid and value in births
            ]
            if recursive and pid == 101:
                direct.append(901)
            return [process(value) for value in direct]

        return SimpleNamespace(
            pid=pid,
            create_time=lambda: births[pid],
            ppid=lambda: parents[pid],
            status=lambda: "running",
            children=children,
        )

    monkeypatch.setattr(support.psutil, "Process", process)
    monkeypatch.setattr(support.psutil, "pids", lambda: [])
    assert cohort._snapshot()["unknown"] == []
    assert cohort._owners == births
    del births[101]
    parents[201] = 1
    snapshot = cohort._snapshot()
    assert snapshot["unknown"] == []
    assert {item["pid"] for item in snapshot["live"]} == {201, 901}


def test_office_environment_cannot_inherit_provider_secrets_or_runtime_hooks(
    monkeypatch,
    tmp_path,
):
    module = _module("packaged_office_edit_roundtrip")
    for key in (
        "COPAW_WORKING_DIR",
        "COPAW_SECRET_DIR",
        "OPENAI_API_KEY",
        "KIMI_API_KEY",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "LANTAI_DESKTOP_SMOKE_TOKEN",
    ):
        monkeypatch.setenv(key, "synthetic forbidden input")
    env = module._office_environment(tmp_path)
    assert not any(
        key in env
        for key in (
            "COPAW_WORKING_DIR",
            "COPAW_SECRET_DIR",
            "OPENAI_API_KEY",
            "KIMI_API_KEY",
            "NODE_OPTIONS",
            "PYTHONPATH",
            "LANTAI_DESKTOP_SMOKE_TOKEN",
        )
    )
    assert env["LANTAI_WORKING_DIR"] == str(tmp_path / "user-data/working")
    assert env["LANTAI_SECRET_DIR"] == str(
        tmp_path / "user-data/working.secret",
    )
    assert env["HOME"] == str(tmp_path / "home")
    assert env["PYTHON_DOTENV_DISABLED"] == "1"


def _binding(fixture):
    editor = {
        ".docx": "docs",
        ".xlsx": "sheets",
        ".pptx": "slides",
        ".pdf": "pdf",
    }[fixture.suffix]
    return {
        "host": {"width": 900, "height": 700},
        "state": {
            "active": True,
            "visible": True,
            "kind": editor,
            "display_name": fixture.name,
            "workbench_id": hashlib.sha256(
                f"{editor}:{fixture}".encode(),
            ).hexdigest()[:20],
            "bounds": {"width": 900, "height": 600},
        },
    }


def test_positive_workbench_for_another_document_is_rejected(tmp_path):
    module = _module("packaged_office_edit_roundtrip")
    fixture = tmp_path / "selected.pdf"
    fixture.write_bytes(b"inert fixture")
    workbench = _binding(fixture)
    workbench["state"]["workbench_id"] = "another-document"
    cdp = SimpleNamespace(
        evaluate=lambda expression: {
            "url": "file:///editor/pdf/index.html",
            "title": "selected.pdf",
            "file_title": "selected.pdf",
            "file_title_attribute": str(fixture),
        },
    )
    with pytest.raises(AssertionError, match="(document|workbench)"):
        module._document_binding(cdp, fixture, workbench)


def test_matching_global_with_another_renderer_file_title_is_rejected(
    tmp_path,
):
    module = _module("packaged_office_edit_roundtrip")
    fixture = tmp_path / "selected.xlsx"
    fixture.write_bytes(b"inert fixture")
    cdp = SimpleNamespace(
        evaluate=lambda expression: {
            "url": "file:///editor/sheets/index.html",
            "title": "other.xlsx",
            "file_title": "other.xlsx",
            "file_title_attribute": "other.xlsx",
        },
    )
    with pytest.raises(AssertionError, match="(document|renderer)"):
        module._document_binding(cdp, fixture, _binding(fixture))


def test_launch_failure_before_popen_does_not_leak_isolated_user_data(
    monkeypatch,
    tmp_path,
):
    module = _module("packaged_office_edit_roundtrip")
    fixture = tmp_path / "selected.pdf"
    fixture.write_bytes(b"inert fixture")
    root = tmp_path / "isolated"
    root.mkdir()
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(root))
    monkeypatch.setattr(module, "_app_executable", lambda app: app / "inert")

    def fail_start(*args, **kwargs):
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(module.subprocess, "Popen", fail_start)
    with pytest.raises(OSError, match="synthetic launch"):
        module._launch_once(tmp_path, fixture, tmp_path / "launch.log", 1)
    assert not root.exists()
    receipt = json.loads((tmp_path / "launch.settlement.json").read_text())
    assert receipt["ok"] is True and receipt["process_started"] is False


def test_unsettled_renderer_failure_must_not_start_a_second_app(
    monkeypatch,
    tmp_path,
):
    module = _module("packaged_office_edit_roundtrip")
    calls = []

    def fail(*_args):
        calls.append(True)
        try:
            raise RuntimeError("process settlement uncertain")
        except RuntimeError as cleanup:
            raise AssertionError(
                "Office renderer was unavailable",
            ) from cleanup

    monkeypatch.setattr(module, "_launch_once", fail)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    with pytest.raises(AssertionError, match="Office renderer"):
        module._launch(
            tmp_path,
            tmp_path / "file.pdf",
            tmp_path / "launch.log",
            1,
        )
    assert len(calls) == 1


def test_registered_sid_pid_reuse_cannot_hide_detached_live_child(monkeypatch):
    support = _module("packaged_office_support")
    cohort = _cohort(support)
    cohort._owners[201] = 101.0
    cohort._stop = threading.Event()
    cohort._signals = []
    cohort._thread = SimpleNamespace(
        join=lambda timeout: None,
        is_alive=lambda: False,
    )
    births = {101: 100.0, 201: 102.0}
    sessions = {101: 101, 201: 101}
    sent = []
    cohort.process.poll = lambda: None if 101 in births else 0

    def process(pid):
        if pid not in births:
            raise psutil.NoSuchProcess(pid)

        def send_signal(sig):
            sent.append((pid, sig))
            births.pop(pid, None)

        return SimpleNamespace(
            pid=pid,
            create_time=lambda: births[pid],
            status=lambda: "running",
            ppid=lambda: 1,
            children=lambda recursive: [],
            send_signal=send_signal,
        )

    monkeypatch.setattr(support.psutil, "Process", process)
    monkeypatch.setattr(support.psutil, "pids", lambda: list(births))
    monkeypatch.setattr(support.os, "getsid", lambda pid: sessions[pid])
    witnessed = cohort._snapshot()
    sessions[201] = 201
    receipt = cohort.settle(term_timeout=0, kill_timeout=0)
    assert witnessed["unknown"]
    assert receipt["ok"] is False
    assert receipt["unknown"]
    assert 201 in births
    assert not any(pid == 201 for pid, _sig in sent)
