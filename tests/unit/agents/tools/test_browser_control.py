# -*- coding: utf-8 -*-
"""Regression tests for browser_use launch failure handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lantai.agents.tools.browser_control as browser_control
import lantai.app.browser_workbench as browser_workbench
from lantai.agents.tool_registry import _input_schema_from_callable
from lantai.app.browser_workbench.models import BrowserObservation


def _response_json(response) -> dict:
    dumped = response.model_dump(mode="json") if hasattr(response, "model_dump") else {}
    blocks = dumped.get("content") or getattr(response, "content", None) or []
    text = "\n".join(
        str(
            (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", "")
            )
            or ""
        )
        for block in blocks
        if (
            block.get("text")
            if isinstance(block, dict)
            else getattr(block, "text", None)
        )
    )
    return json.loads(text)


def test_named_browser_profiles_use_separate_user_data_dirs(tmp_path) -> None:
    default_state = browser_control._make_fresh_state(
        "workspace",
        str(tmp_path),
        "default",
    )
    named_state = browser_control._make_fresh_state(
        "workspace",
        str(tmp_path),
        "GitHub Work",
    )

    assert default_state["user_data_dir"].endswith("browser/user_data")
    assert named_state["profile_id"] == "github-work"
    assert named_state["user_data_dir"].endswith(
        "browser/profiles/github-work/user_data"
    )
    assert named_state["user_data_dir"] != default_state["user_data_dir"]


@pytest.mark.asyncio
async def test_browser_use_routes_standard_actions_to_workbench(monkeypatch) -> None:
    captured = {}

    class FakeBroker:
        async def execute(self, action):
            captured["action"] = action
            return BrowserObservation(
                ok=True,
                action=action.action,
                runtime="electron_bridge",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url=action.url,
                message="handled",
                data={"run_id": action.run_id},
            )

    monkeypatch.setattr(browser_workbench, "BrowserActionBroker", FakeBroker)

    response = await browser_control.browser_use(
        action="open",
        url="https://example.com/path",
        profile_id="work",
    )

    payload = _response_json(response)
    assert payload["ok"] is True
    assert payload["runtime"] == "electron_bridge"
    assert payload["compat_tool"] == "browser_use"
    assert payload["message"] == "handled"
    assert captured["action"].action == "open"
    assert captured["action"].url == "https://example.com/path"
    assert captured["action"].profile_id == "work"
    assert captured["action"].metadata["source"] == "browser_use_compat"


@pytest.mark.asyncio
async def test_browser_find_exposes_compact_snapshot_search(monkeypatch) -> None:
    captured = {}

    class FakeBroker:
        async def execute(self, action):
            captured["action"] = action
            return BrowserObservation(
                ok=True,
                action="find",
                runtime="electron_bridge",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url="https://example.com/invoices",
                message="Found 1 matching snapshot item(s).",
                data={
                    "match_count": 1,
                    "returned_match_count": 1,
                    "truncated": False,
                    "matches": [
                        {
                            "kind": "control",
                            "name": "Review invoice",
                            "selector": "button[data-testid=review]",
                        }
                    ],
                },
            )

    monkeypatch.setattr(browser_workbench, "BrowserActionBroker", FakeBroker)

    response = await browser_control.browser_find(
        text="review invoice",
        page_id="tab-1",
        profile_id="research",
        max_results=7,
        context_chars=64,
    )

    payload = _response_json(response)
    assert payload["ok"] is True
    assert payload["data"]["match_count"] == 1
    assert captured["action"].action == "find"
    assert captured["action"].page_id == "tab-1"
    assert captured["action"].profile_id == "research"
    assert captured["action"].text == "review invoice"
    assert captured["action"].metadata == {
        "source": "browser_find",
        "regex": "",
        "max_results": 7,
        "context_chars": 64,
    }


@pytest.mark.asyncio
async def test_browser_find_rejects_invalid_regex_before_browser_access(
    monkeypatch,
) -> None:
    class UnexpectedBroker:
        async def execute(self, _action):
            raise AssertionError("invalid regex must not reach the browser")

    monkeypatch.setattr(browser_workbench, "BrowserActionBroker", UnexpectedBroker)

    response = await browser_control.browser_find(regex="/[unterminated/i")

    payload = _response_json(response)
    assert payload == {
        "ok": False,
        "error": "Invalid browser find regex: unterminated character set at position 0",
        "error_code": "browser_find_invalid_regex",
    }


@pytest.mark.asyncio
async def test_browser_find_rejects_unsupported_regex_flags_before_browser_access(
    monkeypatch,
) -> None:
    class UnexpectedBroker:
        async def execute(self, _action):
            raise AssertionError("invalid regex flags must not reach the browser")

    monkeypatch.setattr(browser_workbench, "BrowserActionBroker", UnexpectedBroker)

    response = await browser_control.browser_find(regex="/release/g")

    payload = _response_json(response)
    assert payload == {
        "ok": False,
        "error": "Invalid browser find regex: Unsupported regex flag(s): g",
        "error_code": "browser_find_invalid_regex",
    }


def test_browser_use_model_runtime_escape_params_do_not_bypass_workbench() -> None:
    assert browser_control._should_delegate_browser_use_to_workbench(
        action="open",
        legacy_runtime=True,
        headed=True,
        cdp_port=9222,
        private_mode=True,
        browser_args="--remote-debugging-port=9223",
        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        cdp_url="http://127.0.0.1:9222",
    )
    with browser_control._allow_internal_browser_runtime():
        assert not browser_control._should_delegate_browser_use_to_workbench(
            action="open",
            legacy_runtime=True,
            headed=False,
            cdp_port=0,
            private_mode=False,
            browser_args="",
            executable_path="",
            cdp_url="",
        )


def test_browser_use_legacy_only_action_is_not_delegated() -> None:
    assert not browser_control._should_delegate_browser_use_to_workbench(
        action="start",
        legacy_runtime=False,
        headed=False,
        cdp_port=0,
        private_mode=False,
        browser_args="",
        executable_path="",
        cdp_url="",
    )


def test_browser_use_schema_hides_runtime_escape_parameters() -> None:
    schema = _input_schema_from_callable(browser_control.browser_use)
    properties = schema["properties"]

    for name in {
        "legacy_runtime",
        "headed",
        "private_mode",
        "cdp_port",
        "cdp_url",
        "browser_args",
        "executable_path",
        "port",
        "port_min",
        "port_max",
    }:
        assert name not in properties


def test_resolve_output_path_stays_inside_workspace_browser(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    browser_dir = workspace / "browser"
    monkeypatch.setattr(
        browser_control,
        "get_current_workspace_dir",
        lambda: workspace,
    )

    assert Path(browser_control._resolve_output_path("shots/page.png")) == (
        browser_dir / "shots" / "page.png"
    )

    inside_absolute = browser_dir / "reports" / "page.pdf"
    assert Path(browser_control._resolve_output_path(str(inside_absolute))) == (
        inside_absolute
    )

    for unsafe_path in {
        str(tmp_path / "outside.png"),
        "../outside.png",
        "~/outside.png",
    }:
        with pytest.raises(
            ValueError,
            match="browser output path must stay inside workspace/browser",
        ):
            browser_control._resolve_output_path(unsafe_path)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    escape_link = browser_dir / "escape"
    escape_link.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(
        ValueError,
        match="browser output path must stay inside workspace/browser",
    ):
        browser_control._resolve_output_path("escape/page.png")


def test_validate_executable_path_requires_trusted_discovered_path(
    monkeypatch,
    tmp_path,
) -> None:
    trusted = tmp_path / "Google Chrome"
    trusted.write_text("browser", "utf-8")
    fake = tmp_path / "chrome-helper"
    fake.write_text("not a browser", "utf-8")
    trusted_link = tmp_path / "trusted-link"
    trusted_link.symlink_to(trusted)
    fake_link = tmp_path / "chrome-link"
    fake_link.symlink_to(fake)

    monkeypatch.setattr(
        browser_control,
        "get_system_default_browser",
        lambda: ("chromium", str(trusted)),
    )
    monkeypatch.setattr(
        browser_control,
        "_chromium_executable_path",
        lambda: str(trusted),
    )

    browser_control._validate_executable_path(str(trusted))
    browser_control._validate_executable_path(str(trusted_link))

    for rejected in {str(fake), str(fake_link)}:
        with pytest.raises(ValueError, match="trusted browser allowlist"):
            browser_control._validate_executable_path(rejected)

    monkeypatch.setattr(
        browser_control,
        "get_system_default_browser",
        lambda: (None, None),
    )
    monkeypatch.setattr(
        browser_control,
        "_chromium_executable_path",
        lambda: None,
    )
    with pytest.raises(ValueError, match="trusted browser allowlist"):
        browser_control._validate_executable_path(str(trusted))


def test_workspace_state_id_uses_canonical_path_not_basename(tmp_path) -> None:
    first_workspace = tmp_path / "first" / "project"
    second_workspace = tmp_path / "second" / "project"
    first_workspace.mkdir(parents=True)
    second_workspace.mkdir(parents=True)

    first_id = browser_control._workspace_state_id(str(first_workspace))
    second_id = browser_control._workspace_state_id(str(second_workspace))

    assert first_id.startswith("workspace-")
    assert second_id.startswith("workspace-")
    assert first_id != second_id
    assert browser_control._workspace_state_id(str(first_workspace)) == first_id

    browser_control._workspace_states.clear()
    try:
        first_state = browser_control._get_workspace_state(
            first_id,
            str(first_workspace),
        )
        second_state = browser_control._get_workspace_state(
            second_id,
            str(second_workspace),
        )

        assert first_state is not second_state
        assert first_state["workspace_id"] == first_id
        assert second_state["workspace_id"] == second_id
        assert first_state["user_data_dir"] != second_state["user_data_dir"]
        assert first_state["user_data_dir"].startswith(str(first_workspace))
        assert second_state["user_data_dir"].startswith(str(second_workspace))
    finally:
        browser_control._workspace_states.clear()


@pytest.mark.asyncio
async def test_browser_use_rejects_legacy_only_action_for_agent_calls() -> None:
    response = await browser_control.browser_use(action="connect_cdp")

    payload = _response_json(response)
    assert payload["ok"] is False
    assert payload["error_code"] == "browser_legacy_action_blocked"
    assert payload["runtime"] == "browser_workbench"


@pytest.mark.asyncio
async def test_browser_use_ignores_legacy_runtime_param_and_routes_to_workbench(
    monkeypatch,
) -> None:
    captured = {}

    class FakeBroker:
        async def execute(self, action):
            captured["action"] = action
            return BrowserObservation(
                ok=True,
                action=action.action,
                runtime="electron_bridge",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url=action.url,
                message="handled",
                data={"run_id": action.run_id},
            )

    monkeypatch.setattr(browser_workbench, "BrowserActionBroker", FakeBroker)

    response = await browser_control.browser_use(
        action="open",
        url="https://example.com/path",
        legacy_runtime=True,
        headed=True,
        cdp_url="http://127.0.0.1:9222",
        browser_args="--remote-debugging-port=9223",
        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )

    payload = _response_json(response)
    assert payload["ok"] is True
    assert payload["runtime"] == "electron_bridge"
    assert captured["action"].action == "open"
    assert captured["action"].metadata["source"] == "browser_use_compat"


@pytest.mark.asyncio
async def test_open_reports_browser_start_failure_without_none_context_crash(
    monkeypatch,
    tmp_path,
) -> None:
    state = browser_control._make_fresh_state("test", str(tmp_path))

    async def fail_managed_cdp(*_args, **_kwargs):
        raise RuntimeError("managed cdp unavailable")

    async def fail_private_fallback(*_args, **_kwargs):
        return browser_control._tool_response(
            json.dumps(
                {
                    "ok": False,
                    "error": "Browser start failed: forced fallback failure",
                },
            ),
        )

    monkeypatch.setattr(
        browser_control,
        "_start_managed_cdp_browser",
        fail_managed_cdp,
    )
    monkeypatch.setattr(browser_control, "_action_start", fail_private_fallback)

    response = await browser_control._action_open(
        state,
        "https://example.com",
        "page_1",
    )

    payload = _response_json(response)
    assert payload["ok"] is False
    assert payload["error"] == "Browser start failed: forced fallback failure"
    assert "NoneType" not in payload["error"]
