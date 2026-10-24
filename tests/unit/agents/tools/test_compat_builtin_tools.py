# -*- coding: utf-8 -*-
"""Behavior locks for compatibility built-in tool names."""

from __future__ import annotations

import asyncio
import sys

import pytest

import lantai.agents.tools.compat_builtin as compat_builtin
from lantai.agents.tools import (
    execute_python_code,
    view_text_file,
    write_text_file,
)
from lantai.config.context import set_current_workspace_dir
from lantai.security.tool_policy import _detect_distribution


@pytest.fixture(autouse=True)
def workspace_context(tmp_path, monkeypatch):
    monkeypatch.setenv("LANTAI_DISTRIBUTION_CHANNEL", "developer_id")
    monkeypatch.delenv("LANTAI_RELEASE_CHANNEL", raising=False)
    monkeypatch.delenv("LANTAI_DISTRIBUTION", raising=False)
    monkeypatch.delenv("LANTAI_APP_STORE", raising=False)
    set_current_workspace_dir(tmp_path)
    try:
        yield
    finally:
        set_current_workspace_dir(None)


def _skip_source_python_execution_when_app_store_runtime() -> None:
    if _detect_distribution() == "app_store":
        pytest.skip("App Store runtime intentionally disables execute_python_code")


async def test_execute_python_code_returns_structured_text() -> None:
    _skip_source_python_execution_when_app_store_runtime()

    response = await execute_python_code("print('hello')", timeout=3)

    text = response.content[0]["text"]
    assert "<returncode>0</returncode>" in text
    assert "<stdout>hello\n</stdout>" in text
    assert "<stderr></stderr>" in text


async def test_execute_python_code_uses_sandboxed_environment(monkeypatch) -> None:
    _skip_source_python_execution_when_app_store_runtime()

    monkeypatch.setenv("LANTAI_BROWSER_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    response = await execute_python_code(
        "import os; print(os.getenv('LANTAI_BROWSER_BRIDGE_TOKEN')); print(os.getenv('OPENAI_API_KEY'))",
        timeout=3,
    )

    text = response.content[0]["text"]
    assert "<returncode>0</returncode>" in text
    assert "bridge-secret" not in text
    assert "provider-secret" not in text
    assert "<stdout>None\nNone\n</stdout>" in text


async def test_execute_python_code_is_unavailable_for_app_store(monkeypatch) -> None:
    monkeypatch.setenv("LANTAI_DISTRIBUTION_CHANNEL", "app_store")

    response = await execute_python_code("print('hello')", timeout=3)

    text = response.content[0]["text"]
    assert "unavailable" in text
    assert "App Store" in text
    assert "hello" not in text


async def test_execute_python_code_is_unavailable_when_frozen(monkeypatch) -> None:
    _skip_source_python_execution_when_app_store_runtime()

    monkeypatch.setattr(compat_builtin.sys, "frozen", True, raising=False)

    response = await execute_python_code("print('hello')", timeout=3)

    text = response.content[0]["text"]
    assert "unavailable" in text
    assert "packaged desktop runtime" in text
    assert "hello" not in text


async def test_execute_python_code_terminates_when_output_exceeds_limit(
    monkeypatch,
) -> None:
    _skip_source_python_execution_when_app_store_runtime()

    monkeypatch.setattr(compat_builtin, "PYTHON_OUTPUT_MAX_BYTES", 4096, raising=False)
    code = "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()"

    response = await execute_python_code(code, timeout=5)

    text = response.content[0]["text"]
    assert "<returncode>-1</returncode>" in text
    assert "Python output exceeded the limit of 4096 bytes" in text
    assert "[lantai:stdout_truncated]" in text
    assert len(text.encode("utf-8")) < 12000


async def test_execute_python_code_timeout_kills_child_process_tree(tmp_path) -> None:
    _skip_source_python_execution_when_app_store_runtime()

    marker = tmp_path / "late-child-marker.txt"
    child_code = (
        "import pathlib, time; "
        "time.sleep(0.6); "
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])\n"
        "time.sleep(10)\n"
    )

    response = await execute_python_code(code, timeout=0.2)
    await asyncio.sleep(1.0)

    text = response.content[0]["text"]
    assert "<returncode>-1</returncode>" in text
    assert "TimeoutError" in text
    assert not marker.exists()


async def test_view_and_write_text_file_compat_names(tmp_path) -> None:
    path = tmp_path / "demo.txt"

    write_response = await write_text_file(str(path), "one\ntwo\nthree")
    assert "Wrote" in write_response.content[0]["text"]

    view_response = await view_text_file(str(path), [2, 3])
    view_text = view_response.content[0]["text"]
    assert "two\nthree" in view_text


async def test_write_text_file_replaces_range(tmp_path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("one\ntwo\nthree\nfour", encoding="utf-8")

    response = await write_text_file(str(path), "TWO\nTHREE", [2, 3])

    assert "Wrote" in response.content[0]["text"]
    assert path.read_text(encoding="utf-8") == "one\nTWO\nTHREE\nfour"


async def test_write_text_file_preserves_crlf_and_final_newline(tmp_path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("one\r\ntwo\r\nthree\r\nfour\r\n", encoding="utf-8")

    response = await write_text_file(str(path), "TWO\nTHREE", [2, 3])

    assert "Wrote" in response.content[0]["text"]
    assert path.read_bytes() == b"one\r\nTWO\r\nTHREE\r\nfour\r\n"


async def test_write_text_file_preserves_missing_final_newline_at_eof(tmp_path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("one\ntwo\nthree", encoding="utf-8")

    response = await write_text_file(str(path), "TWO\nTHREE\n", [2, 3])

    assert "Wrote" in response.content[0]["text"]
    assert path.read_text(encoding="utf-8") == "one\nTWO\nTHREE"
