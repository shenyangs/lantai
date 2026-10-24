# -*- coding: utf-8 -*-
from __future__ import annotations

# These behavior locks intentionally inspect private harness operations.
# pylint: disable=protected-access

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_office_edit_roundtrip.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "packaged_office_edit_roundtrip",
        SCRIPT_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_launch_passes_an_absolute_fixture_path_after_changing_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"fixture")
    app = tmp_path / "Lantai.app"
    executable = tmp_path / "Lantai"
    executable.write_bytes(b"executable")
    log = tmp_path / "launch.log"
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    class FakeCdp:
        def evaluate(self, _expression):
            if "file_title_attribute" in _expression:
                return {
                    "url": "file:///docs/index.html",
                    "file_title": fixture.name,
                    "file_title_attribute": str(fixture.resolve()),
                    "global_available": True,
                }
            return True

        def close(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "_app_executable", lambda _app: executable)
    monkeypatch.setattr(module, "_free_port", lambda: 43123)
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        module,
        "OwnedProcessCohort",
        lambda _process: SimpleNamespace(
            settle=lambda: {"ok": True, "live": [], "unknown": []},
        ),
    )
    monkeypatch.setattr(module, "_find_office_cdp", lambda *_args: FakeCdp())
    monkeypatch.setattr(
        module,
        "_find_workbench_evidence",
        lambda *_args: {
            "host": {"width": 900, "height": 700},
            "state": {
                "active": True,
                "visible": True,
                "kind": "docs",
                "display_name": fixture.name,
                "workbench_id": hashlib.sha256(
                    f"docs:{fixture.resolve()}".encode(),
                ).hexdigest()[:20],
                "bounds": {"width": 800, "height": 600},
            },
        },
    )
    monkeypatch.setattr(module, "_wait", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    session = module._launch_once(app, Path("sample.docx"), log, 1)
    try:
        command = captured["command"]
        assert command[-1] == str(fixture.resolve())
        assert captured["cwd"] == str(app.parent)
    finally:
        session.stop()


def test_launch_waits_for_a_sized_interactive_office_viewport(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"fixture")
    app = tmp_path / "Lantai.app"
    executable = tmp_path / "Lantai"
    executable.write_bytes(b"executable")
    expressions: list[str] = []
    predicates = []

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    class FakeCdp:
        def evaluate(self, expression):
            expressions.append(expression)
            if "file_title_attribute" in expression:
                return {
                    "url": "file:///docs/index.html",
                    "file_title": fixture.name,
                    "file_title_attribute": str(fixture.resolve()),
                    "global_available": True,
                }
            return True

        def close(self):
            return None

    monkeypatch.setattr(module, "_app_executable", lambda _app: executable)
    monkeypatch.setattr(module, "_free_port", lambda: 43123)
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        module,
        "OwnedProcessCohort",
        lambda _process: SimpleNamespace(
            settle=lambda: {"ok": True, "live": [], "unknown": []},
        ),
    )
    monkeypatch.setattr(module, "_find_office_cdp", lambda *_args: FakeCdp())
    monkeypatch.setattr(
        module,
        "_find_workbench_evidence",
        lambda *_args: {
            "host": {"width": 900, "height": 700},
            "state": {
                "active": True,
                "visible": True,
                "kind": "docs",
                "display_name": fixture.name,
                "workbench_id": hashlib.sha256(
                    f"docs:{fixture.resolve()}".encode(),
                ).hexdigest()[:20],
                "bounds": {"width": 800, "height": 600},
            },
        },
    )

    def fake_wait(predicate, **_kwargs):
        predicates.append(predicate)
        assert predicate() is True

    monkeypatch.setattr(module, "_wait", fake_wait)

    session = module._launch_once(app, fixture, tmp_path / "launch.log", 1)
    try:
        assert len(predicates) == 2
        assert any(
            "window.innerWidth >= 300" in value for value in expressions
        )
        assert all(
            "document.visibilityState" not in value for value in expressions
        )
        assert any(
            "window.innerHeight >= 200" in value for value in expressions
        )
    finally:
        session.stop()


def test_launch_retry_reports_both_failures_on_python_310(monkeypatch) -> None:
    module = _load_module()
    attempts = iter(
        [
            AssertionError("Office renderer missing first"),
            AssertionError("Office renderer missing second"),
        ],
    )
    monkeypatch.setattr(
        module,
        "_launch_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(next(attempts)),
    )
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    try:
        module._launch(Path("app"), Path("file.docx"), Path("app.log"), 1)
    except AssertionError as exc:
        assert "missing first" in str(exc)
        assert "missing second" in str(exc)
    else:
        raise AssertionError("both failed cold starts must fail the gate")


def test_docx_readiness_probe_returns_only_a_boolean(monkeypatch) -> None:
    module = _load_module()
    expressions: list[str] = []

    class FakeCdp:
        def evaluate(self, expression):
            expressions.append(expression)
            if expression == "Boolean(window.__lantaiDocs?.editor)":
                return True
            if (
                "const blocks=[...document.querySelectorAll('.Pros"
                "eMirror > [data-idx]')]"
            ) in expression:
                return {
                    "x": 120,
                    "y": 180,
                    "docxIndex": 1,
                    "blockText": "existing",
                    "hitTag": "P",
                    "hitClass": "",
                    "rect": {
                        "left": 100,
                        "top": 170,
                        "width": 20,
                        "height": 20,
                    },
                }
            if "const e=window.__lantaiDocs?.editor" in expression:
                return {
                    "text": "existing undo final",
                    "from": 1,
                    "to": 1,
                    "docxIndex": 1,
                    "activeEditor": True,
                    "activeTag": "DIV",
                    "activeClass": "ProseMirror",
                }
            return False

        def command(self, _method, _params):
            return {}

        def insert_text(self, _text):
            return None

        def key(self, *_args, **_kwargs):
            return None

    class FakeProcess:
        def poll(self):
            return None

    session = type(
        "Session",
        (),
        {"cdp": FakeCdp(), "process": FakeProcess()},
    )()
    monkeypatch.setattr(
        module,
        "_wait",
        lambda predicate, **_kwargs: predicate(),
    )
    monkeypatch.setattr(module, "_body_text", lambda _cdp: "")
    monkeypatch.setattr(
        module,
        "_native_click",
        lambda *_args, **_kwargs: {"input": "Input.dispatchMouseEvent"},
    )
    monkeypatch.setattr(module, "_button_disabled", lambda *_args: False)

    module._edit_docx(session, "final", "undo", 1)

    assert expressions[0] == "Boolean(window.__lantaiDocs?.editor)"


def test_xlsx_canvas_probe_requires_positive_canvas_and_native_hit_test() -> (
    None
):
    module = _load_module()
    expressions: list[str] = []
    expected = {
        "canvas": {"x": 0, "y": 171, "width": 900, "height": 500},
        "viewport": {"x": 0, "y": 171, "width": 900, "height": 500},
        "points": {"A1": {"x": 80, "y": 205}, "B2": {"x": 160, "y": 225}},
        "hits": {"A1": {"tag": "CANVAS"}, "B2": {"tag": "CANVAS"}},
    }

    class FakeCdp:
        def evaluate(self, expression):
            expressions.append(expression)
            return expected

    assert module._xlsx_canvas_probe(FakeCdp()) == expected
    assert "univer-sheet-main-canvas" in expressions[0]
    assert "r.height < 200" in expressions[0]
    assert "vr.height < 200" in expressions[0]
    assert "elementFromPoint" in expressions[0]
    assert "HTMLCanvasElement" in expressions[0]


def test_xlsx_canvas_probe_waits_through_transient_renderer_layout(
    monkeypatch,
) -> None:
    module = _load_module()
    expected = {
        "canvas": {"x": 0, "y": 171, "width": 900, "height": 500},
        "viewport": {"x": 0, "y": 171, "width": 900, "height": 500},
        "points": {"A1": {"x": 80, "y": 205}, "B2": {"x": 160, "y": 225}},
        "hits": {"A1": {"tag": "CANVAS"}, "B2": {"tag": "CANVAS"}},
    }
    attempts = {"count": 0}

    class FakeCdp:
        def evaluate(self, _expression):
            attempts["count"] += 1
            return None if attempts["count"] == 1 else expected

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    assert module._xlsx_wait_for_canvas_probe(FakeCdp(), 1) == expected
    assert attempts["count"] == 2


def test_xlsx_native_click_must_change_visible_name_box(monkeypatch) -> None:
    module = _load_module()
    selected = {"value": "A1"}

    class FakeCdp:
        def evaluate(self, expression):
            if "querySelectorAll('input')" in expression:
                return selected["value"]
            return {
                "canvas": {"width": 900, "height": 500},
                "points": {
                    "A1": {"x": 80, "y": 205},
                    "B2": {"x": 160, "y": 225},
                },
                "hits": {"A1": {"tag": "CANVAS"}, "B2": {"tag": "CANVAS"}},
            }

        def command(self, _method, params):
            if "type" not in params:
                return {}
            if params["type"] == "mouseReleased":
                selected["value"] = "B2" if params["x"] == 160 else "A1"
            return {}

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    evidence = module._xlsx_prove_native_selection(FakeCdp(), 1)
    assert evidence["initial_name_box"] == "A1"
    assert evidence["after_b2_click"] == "B2"
    assert evidence["after_a1_click"] == "A1"


def test_xlsx_roundtrip_requires_visible_a1_readback_after_undo() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "post_undo_visible_a1 = _xlsx_copy_selected_cell(cdp)" in source
    assert "if post_undo_visible_a1 != original_a1:" in source
    assert '"post_undo_expected_a1": original_a1' in source
    assert 'str(before_state["a1"])' in source


def test_native_click_requires_hit_testing_and_dispatches_mouse_events() -> (
    None
):
    module = _load_module()
    calls: list[tuple[str, Any]] = []

    class FakeCdp:
        def evaluate(self, expression):
            calls.append(("evaluate", expression))
            return {
                "x": 120,
                "y": 80,
                "rect": {"left": 100, "top": 60, "width": 40, "height": 40},
                "tag": "BUTTON",
                "title": "Save",
                "aria": "",
                "text": "",
                "hitTag": "BUTTON",
                "hitClass": "",
            }

        def command(self, method, params):
            calls.append((method, params))
            return {}

    evidence = module._native_click(
        FakeCdp(),
        "document.querySelector('button')",
        label="save",
    )

    assert "elementFromPoint" in calls[0][1]
    assert ".click()" not in calls[0][1]
    assert calls[1][0] == "Input.dispatchMouseEvent"
    assert calls[1][1]["type"] == "mousePressed"
    assert calls[2][1]["type"] == "mouseReleased"
    assert evidence["input"] == "Input.dispatchMouseEvent"


def test_pptx_add_slide_opens_current_ribbon_menu_when_command_is_hidden(
    monkeypatch,
) -> None:
    module = _load_module()
    visible = {"add": False}
    calls: list[str] = []

    monkeypatch.setattr(
        module,
        "_native_click_target",
        lambda _cdp, expression: (
            {"text": "新建幻灯片"}
            if visible["add"] and "新建幻灯片" in expression
            else None
        ),
    )

    def fake_click(_cdp, _expression, *, label):
        calls.append(label)
        if label.endswith("open slide menu"):
            visible["add"] = True
        return {"label": label, "input": "Input.dispatchMouseEvent"}

    monkeypatch.setattr(module, "_native_click", fake_click)
    monkeypatch.setattr(
        module,
        "_wait",
        lambda predicate, **_kwargs: predicate(),
    )

    evidence = module._pptx_click_add_slide(
        object(),
        label="PPTX add temporary slide",
    )

    assert calls == [
        "PPTX add temporary slide open slide menu",
        "PPTX add temporary slide",
    ]
    assert evidence["menu_opened"] is True
    assert evidence["menu_click"]["input"] == "Input.dispatchMouseEvent"
    assert evidence["add_click"]["input"] == "Input.dispatchMouseEvent"


def test_pptx_add_slide_uses_visible_direct_command_without_opening_menu(
    monkeypatch,
) -> None:
    module = _load_module()
    calls: list[str] = []

    monkeypatch.setattr(
        module,
        "_native_click_target",
        lambda _cdp, _expression: {"text": "新建幻灯片"},
    )

    def fake_click(_cdp, _expression, *, label):
        calls.append(label)
        return {"label": label, "input": "Input.dispatchMouseEvent"}

    monkeypatch.setattr(module, "_native_click", fake_click)

    evidence = module._pptx_click_add_slide(
        object(),
        label="PPTX add final slide",
    )

    assert calls == ["PPTX add final slide"]
    assert evidence["menu_opened"] is False
    assert evidence["menu_click"] is None


def test_pdf_rectangle_waits_for_the_mounted_drawing_layer(
    monkeypatch,
) -> None:
    module = _load_module()
    layer_attempts = {"count": 0}

    class FakeCdp:
        def evaluate(self, expression):
            if (
                "Boolean(document.querySelector('.pdf-draw-layer.p"
                "df-draw-active'))"
            ) in expression:
                return False
            if "getBoundingClientRect" in expression:
                layer_attempts["count"] += 1
                if layer_attempts["count"] == 1:
                    return None
                return {"x": 10, "y": 20, "width": 900, "height": 600}
            raise AssertionError(expression)

    clicks: list[str] = []
    monkeypatch.setattr(
        module,
        "_native_click_target",
        lambda *_args: {"tag": "BUTTON"},
    )

    def native_click(_cdp, _expression, *, label):
        clicks.append(label)
        return {"label": label}

    monkeypatch.setattr(
        module,
        "_native_click",
        native_click,
    )
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    assert module._activate_pdf_rect(FakeCdp()) == {
        "x": 10.0,
        "y": 20.0,
        "width": 900.0,
        "height": 600.0,
    }
    assert layer_attempts["count"] == 2
    assert clicks == ["PDF annotation tab", "PDF rectangle tool"]


def test_xlsx_replacement_starts_with_a_real_printable_key(
    monkeypatch,
) -> None:
    module = _load_module()
    calls: list[tuple[str, Any]] = []
    editor = {
        "address": "A1",
        "value": "",
        "active": True,
        "visible": True,
        "hit": True,
        "rect": {"left": 10, "top": 20, "width": 80, "height": 26},
    }

    class FakeCdp:
        def command(self, method, params):
            calls.append((method, params))
            if (
                method == "Input.dispatchKeyEvent"
                and params["type"] == "keyDown"
            ):
                editor["value"] = params.get("text", "")
            return {}

        def insert_text(self, text):
            calls.append(("insert_text", text))
            editor["value"] += text

        def key(self, key, *, code=None, virtual=0):
            calls.append(
                ("key", {"key": key, "code": code, "virtual": virtual}),
            )

    monkeypatch.setattr(
        module,
        "_xlsx_visible_editor",
        lambda _cdp: dict(editor),
    )
    monkeypatch.setattr(module, "_button_disabled", lambda *_args: True)

    def fake_wait(predicate, **_kwargs):
        assert predicate() is True

    monkeypatch.setattr(module, "_wait", fake_wait)
    evidence = module._xlsx_replace_selected_cell(FakeCdp(), "A1", "LANTAI", 1)

    assert calls[0] == ("Page.bringToFront", {})
    assert calls[1] == (
        "Emulation.setFocusEmulationEnabled",
        {"enabled": True},
    )
    first = calls[2]
    assert first[0] == "Input.dispatchKeyEvent"
    assert first[1]["type"] == "keyDown"
    assert first[1]["text"] == "L"
    assert first[1]["code"] == "KeyL"
    assert calls[3][1]["type"] == "keyUp"
    assert calls[4] == ("insert_text", "ANTAI")
    assert calls[5] == (
        "key",
        {"key": "Enter", "code": "Enter", "virtual": 13},
    )
    assert evidence["editor"]["active"] is True
    assert evidence["editor"]["visible"] is True
    assert evidence["editor"]["hit"] is True
    assert evidence["address"] == "A1"
    assert evidence["marker_before_commit"] == "LANTAI"


def test_xlsx_replacement_allows_a_second_edit_while_workbook_is_already_dirty(
    monkeypatch,
) -> None:
    module = _load_module()
    editor = {
        "address": "B2",
        "value": "",
        "active": True,
        "visible": True,
        "hit": True,
        "rect": {"left": 10, "top": 20, "width": 80, "height": 26},
    }

    class FakeCdp:
        def command(self, method, params):
            if (
                method == "Input.dispatchKeyEvent"
                and params["type"] == "keyDown"
            ):
                editor["value"] = params.get("text", "")
            return {}

        def insert_text(self, text):
            editor["value"] += text

        def key(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(
        module,
        "_xlsx_visible_editor",
        lambda _cdp: dict(editor),
    )
    # False means Save is enabled; an earlier cell edit is pending.
    monkeypatch.setattr(module, "_button_disabled", lambda *_args: False)
    monkeypatch.setattr(
        module,
        "_wait",
        lambda predicate, **_kwargs: predicate(),
    )

    evidence = module._xlsx_replace_selected_cell(FakeCdp(), "B2", "123.5", 1)

    assert evidence["marker_before_commit"] == "123.5"
    assert evidence["save_was_disabled_before_edit"] is False


def test_xlsx_native_number_readback_remains_strict_without_a_waiver() -> None:
    module = _load_module()

    with pytest.raises(
        AssertionError,
        match="numeric input lost native semantics",
    ):
        module._assert_readback(
            suffix=".xlsx",
            before={"a1": "before"},
            after={
                "a1": "marker",
                "b2": "123.5",
                "b2_type": "s",
                "c2": "=B2*2",
                "c2_type": "f",
            },
            marker="marker",
            undo_marker="undo",
        )


def test_xlsx_native_number_waiver_is_narrow_and_auditable() -> None:
    module = _load_module()

    waivers = module._assert_readback(
        suffix=".xlsx",
        before={"a1": "before"},
        after={
            "a1": "marker",
            "b2": "123.5",
            "b2_type": "s",
            "c2": "=B2*2",
            "c2_type": "f",
        },
        marker="marker",
        undo_marker="undo",
        xlsx_native_number_waiver_reason=(
            "User deferred GenOffice " "editor changes"
        ),
    )

    assert waivers == [
        {
            "id": "genoffice.xlsx.native_number_input",
            "reason": "User deferred GenOffice editor changes",
            "observed_value": "123.5",
            "observed_type": "s",
        },
    ]
