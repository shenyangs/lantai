# -*- coding: utf-8 -*-
from __future__ import annotations

# These locks exercise the private packaged Office launch harness.
# pylint: disable=protected-access

import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_office_edit_roundtrip.py"
)


@pytest.fixture(name="launch_environment")
def _launch_environment(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "office_readiness_locks",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    clock = SimpleNamespace(now=0.0)
    control = SimpleNamespace(
        suffix=".xlsx",
        ready_at=0.0,
        title_ready_before_attribute=False,
        initial_delay=0.0,
        evaluation_delay=0.0,
        exit_at=None,
        renderer_override={},
        host_override={},
        binding_polls=0,
        actions=[],
    )

    def sleep(seconds):
        assert 0 <= seconds <= 0.15
        clock.now += seconds

    class FakeProcess:
        pid = 90000001
        returncode = None

        def poll(self):
            if control.exit_at is not None and clock.now >= control.exit_at:
                self.returncode = 17
            return self.returncode

    class FakeCdp:
        def evaluate(self, expression):
            if (
                "document.body" in expression
                or "window.innerWidth" in expression
            ):
                return True
            assert "file_title_attribute" in expression
            control.binding_polls += 1
            clock.now += control.evaluation_delay
            fixture = control.fixture
            ready = clock.now >= control.ready_at
            expected_attribute = (
                str(fixture)
                if fixture.suffix in {".docx", ".pdf"}
                else fixture.name
            )
            renderer = {
                "url": "file:///synthetic/editor/index.html",
                "title": "Synthetic Office",
                "file_title": (
                    fixture.name
                    if ready or control.title_ready_before_attribute
                    else "Untitled"
                ),
                "file_title_attribute": expected_attribute
                if ready
                else "Untitled",
                "global_available": True,
            }
            renderer.update(control.renderer_override)
            return renderer

        def close(self):
            control.actions.append("cdp_closed")

    process = FakeProcess()
    cdp = FakeCdp()

    def find_cdp(*_args):
        clock.now += control.initial_delay
        return cdp

    def workbench(*_args):
        fixture = control.fixture
        editor = module.EDITOR_BY_SUFFIX[fixture.suffix]
        state = {
            "active": True,
            "visible": True,
            "kind": editor,
            "display_name": fixture.name,
            "workbench_id": hashlib.sha256(
                f"{editor}:{fixture}".encode(),
            ).hexdigest()[:20],
        }
        state.update(control.host_override)
        return {"host": {"width": 900, "height": 700}, "state": state}

    def popen(*_args, **_kwargs):
        control.actions.append("synthetic_popen")
        return process

    def settle():
        control.actions.append("owned_settlement")
        return {"ok": True, "live": [], "unknown": [], "signals": []}

    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.now,
            sleep=sleep,
        ),
    )
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        module,
        "OwnedProcessCohort",
        lambda _process: SimpleNamespace(
            settle=settle,
        ),
    )
    monkeypatch.setattr(
        module,
        "_app_executable",
        lambda _app: Path("synthetic"),
    )
    monkeypatch.setattr(module, "_office_environment", lambda _root: {})
    monkeypatch.setattr(module, "_free_port", lambda: 1)
    monkeypatch.setattr(module, "_find_office_cdp", find_cdp)
    monkeypatch.setattr(module, "_find_workbench_evidence", workbench)

    def launch():
        fixture = tmp_path / f"same-name{control.suffix}"
        fixture.write_bytes(b"synthetic; no native Office invocation")
        control.fixture = fixture.resolve(strict=True)
        control.log = tmp_path / "launch.log"
        return module._launch_once(
            Path("synthetic.app"),
            fixture,
            control.log,
            45.0,
        )

    return SimpleNamespace(
        module=module,
        clock=clock,
        control=control,
        launch=launch,
        cdp=cdp,
        workbench=workbench,
    )


@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx", ".pdf"])
@pytest.mark.parametrize("partial_title", [False, True])
def test_launch_waits_for_the_same_document_without_weakening_identity(
    launch_environment,
    suffix,
    partial_title,
):
    env = launch_environment
    env.control.suffix = suffix
    env.control.ready_at = 0.3
    env.control.title_ready_before_attribute = partial_title
    session = env.launch()
    try:
        assert session.renderer_identity["document_path"] == str(
            env.control.fixture,
        )
        assert (
            session.renderer_identity["file_title"] == env.control.fixture.name
        )
        expected = (
            str(env.control.fixture)
            if suffix in {".docx", ".pdf"}
            else env.control.fixture.name
        )
        assert session.renderer_identity["file_title_attribute"] == expected
        assert env.control.binding_polls >= 3
        assert 0.3 <= env.clock.now < 45
    finally:
        session.stop()


def test_launch_does_not_accept_a_response_after_the_original_deadline(
    launch_environment,
):
    env = launch_environment
    env.control.initial_delay = 44.0
    env.control.evaluation_delay = 2.0
    with pytest.raises(AssertionError, match="Office renderer"):
        env.launch()
    assert "owned_settlement" in env.control.actions
    assert "cdp_closed" in env.control.actions


def test_unbound_document_deadline_keeps_private_observations(
    launch_environment,
):
    env = launch_environment
    env.control.ready_at = 100.0
    with pytest.raises(AssertionError, match="Office renderer"):
        env.launch()
    assert env.clock.now == pytest.approx(45.0)
    path = env.control.log.with_suffix(".document-binding.json")
    observed = json.loads(path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert observed["accepted"] is False
    assert observed["renderer"]["file_title"] == "Untitled"
    assert observed["renderer"]["file_title_attribute"] == "Untitled"
    assert observed["renderer"]["global_available"] is True
    assert observed["renderer"]["url"] == "file:///synthetic/editor/index.html"
    assert (
        observed["workbench"]["workbench_id"]
        == hashlib.sha256(
            f"sheets:{env.control.fixture}".encode(),
        ).hexdigest()[:20]
    )
    assert "owned_settlement" in env.control.actions


def test_process_exit_interrupts_binding_and_records_last_observation(
    launch_environment,
):
    env = launch_environment
    env.control.ready_at = 100.0
    env.control.exit_at = 0.15
    with pytest.raises(AssertionError, match="app exited"):
        env.launch()
    assert env.clock.now == pytest.approx(0.15)
    observation = json.loads(
        env.control.log.with_suffix(
            ".document-binding.json",
        ).read_text(),
    )
    assert observation["process_returncode"] == 17
    assert observation["renderer"]["file_title"] == "Untitled"
    assert "owned_settlement" in env.control.actions


def test_failure_observation_excludes_url_values_and_unrequested_payload(
    launch_environment,
):
    env = launch_environment
    env.control.ready_at = 100.0
    env.control.exit_at = 0.15
    env.control.renderer_override = {
        "url": (
            "file:///synthetic/editor/index.html"
            "?token=synthetic-secret#private-value"
        ),
        "unrequested_payload": "synthetic-content-not-to-record",
    }
    with pytest.raises(AssertionError, match="app exited"):
        env.launch()
    raw = env.control.log.with_suffix(".document-binding.json").read_text()
    assert "synthetic-secret" not in raw
    assert "private-value" not in raw
    assert "synthetic-content-not-to-record" not in raw
    assert (
        json.loads(raw)["renderer"]["url"]
        == "file:///synthetic/editor/index.html"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"file_title": "other.xlsx"},
        {"file_title_attribute": "/wrong/same-name.xlsx"},
        {"url": "https://foreign.invalid/editor"},
        {"global_available": False},
        {"global_available": 1},
    ],
)
def test_existing_renderer_identity_predicate_still_rejects_foreign_results(
    launch_environment,
    override,
):
    env = launch_environment
    session = env.launch()
    try:
        env.control.renderer_override = override
        with pytest.raises(AssertionError, match="selected document"):
            env.module._document_binding(
                env.cdp,
                env.control.fixture,
                env.workbench(),
            )
    finally:
        session.stop()


@pytest.mark.parametrize(
    "override",
    [
        {"active": False},
        {"visible": False},
        {"kind": "docs"},
        {"display_name": "other.xlsx"},
        {
            "workbench_id": hashlib.sha256(
                b"sheets:/wrong/same-name.xlsx",
            ).hexdigest()[:20],
        },
    ],
)
def test_existing_host_identity_predicate_still_rejects_foreign_results(
    launch_environment,
    override,
):
    env = launch_environment
    session = env.launch()
    try:
        env.control.host_override = override
        with pytest.raises(AssertionError, match="another document"):
            env.module._document_binding(
                env.cdp,
                env.control.fixture,
                env.workbench(),
            )
    finally:
        session.stop()
