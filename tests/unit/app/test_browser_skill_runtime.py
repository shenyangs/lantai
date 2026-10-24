from __future__ import annotations

from pathlib import Path

import pytest

from lantai.app.browser_workbench.browser_skill import (
    BrowserSkillClient,
    get_browser_skill_client,
)
from lantai.app.browser_workbench.broker import BrowserActionBroker
from lantai.app.browser_workbench.models import BrowserAction


class FakeBrowserSkillClient(BrowserSkillClient):
    def __init__(self, replies: list[dict]) -> None:
        super().__init__()
        self.cli_path = Path("/fake/bsk")
        self.replies = list(replies)
        self.commands: list[tuple[list[str], float, bool]] = []

    async def _run_json(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        effect_unknown_on_timeout: bool = False,
    ) -> dict:
        self.commands.append((args, timeout_seconds, effect_unknown_on_timeout))
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_status_is_read_only_when_browser_skill_is_not_installed() -> None:
    client = BrowserSkillClient(cli_path="/definitely/missing/bsk")
    client.cli_path = None

    status = await client.status()

    assert status["ok"] is True
    assert status["installed"] is False
    assert status["connected"] is False
    assert status["ready"] is False
    assert status["status"] == "not_installed"


def test_default_browser_skill_client_is_process_shared() -> None:
    assert get_browser_skill_client() is get_browser_skill_client()


@pytest.mark.asyncio
async def test_multiple_connected_browsers_are_visible_but_not_implicitly_ready() -> None:
    client = FakeBrowserSkillClient(
        [
            {
                "ok": True,
                "browsers": [
                    {"instance_id": "chrome-work"},
                    {"instance_id": "edge-personal"},
                ],
                "sessions": [],
            }
        ]
    )

    status = await client.status()

    assert status["connected"] is True
    assert status["ready"] is False
    assert "多个" in status["message"]


@pytest.mark.asyncio
async def test_navigate_starts_background_agent_window_and_reuses_session() -> None:
    client = FakeBrowserSkillClient(
        [
            {"ok": True, "browsers": [{"instance_id": "chrome-main"}], "sessions": []},
            {"ok": True, "session_id": "abcd", "agent_window_id": 12},
            {"ok": True, "tab_id": 41, "url": "https://example.com"},
            {"ok": True, "tab_id": 41, "url": "https://example.com/docs"},
        ]
    )

    first = await client.execute(
        BrowserAction(action="open", url="https://example.com", profile_id="work")
    )
    second = await client.execute(
        BrowserAction(
            action="navigate",
            url="https://example.com/docs",
            page_id="41",
            profile_id="work",
        )
    )

    assert first["runtime"] == "browser_skill"
    assert first["session_id"] == "abcd"
    assert first["page_id"] == "41"
    assert second["session_id"] == "abcd"
    assert [entry[0] for entry in client.commands] == [
        ["status"],
        ["session", "start", "--no-focus", "--browser", "chrome-main"],
        ["navigate", "https://example.com", "--session", "abcd"],
        [
            "navigate",
            "https://example.com/docs",
            "--session",
            "abcd",
            "--tab-id",
            "41",
        ],
    ]


@pytest.mark.asyncio
async def test_snapshot_uses_semantic_observe_and_preserves_refs() -> None:
    client = FakeBrowserSkillClient(
        [
            {"ok": True, "browsers": [{"instance_id": "edge"}], "sessions": []},
            {"ok": True, "session_id": "wxyz"},
            {"ok": True, "tab_id": 9, "text": '@e1 button "继续"', "ref_count": 1},
        ]
    )

    payload = await client.execute(
        BrowserAction(
            action="snapshot",
            page_id="9",
            metadata={"max_tokens": 4000},
        )
    )

    assert payload["snapshot"] == '@e1 button "继续"'
    assert client.commands[-1][0] == [
        "observe",
        "--session",
        "wxyz",
        "--tab-id",
        "9",
        "--max-tokens",
        "4000",
    ]


@pytest.mark.asyncio
async def test_borrow_requires_numeric_tab_and_maps_to_session_command() -> None:
    client = FakeBrowserSkillClient(
        [
            {"ok": True, "browsers": [{"instance_id": "chrome"}], "sessions": []},
            {"ok": True, "session_id": "tabs"},
            {"ok": True, "tab_id": 88, "agent_window_id": 5},
        ]
    )

    payload = await client.execute(
        BrowserAction(action="tab_borrow", page_id="88", confirmed=True)
    )

    assert payload["ok"] is True
    assert client.commands[-1][0] == [
        "tab",
        "borrow",
        "88",
        "--session",
        "tabs",
    ]


@pytest.mark.asyncio
async def test_stop_all_releases_every_profile_session() -> None:
    client = FakeBrowserSkillClient(
        [
            {"ok": True, "stopped": ["session-a"]},
            {"ok": True, "stopped": ["session-b"]},
        ]
    )
    client._sessions = {"work": "session-a", "personal": "session-b"}

    payload = await client.stop_all()

    assert payload["ok"] is True
    assert client._sessions == {}
    assert [entry[0] for entry in client.commands] == [
        ["session", "stop", "session-a"],
        ["session", "stop", "session-b"],
    ]


@pytest.mark.asyncio
async def test_select_uses_first_browser_use_option_value() -> None:
    client = FakeBrowserSkillClient(
        [
            {"ok": True, "browsers": [{"instance_id": "chrome"}], "sessions": []},
            {"ok": True, "session_id": "select"},
            {"ok": True, "tab_id": 3},
        ]
    )

    payload = await client.execute(
        BrowserAction(
            action="select_option",
            page_id="3",
            ref="@e7",
            metadata={"values_json": '["zh-CN"]'},
            confirmed=True,
        )
    )

    assert payload["ok"] is True
    assert client.commands[-1][0] == [
        "select",
        "@e7",
        "--value",
        "zh-CN",
        "--session",
        "select",
        "--tab-id",
        "3",
    ]


@pytest.mark.asyncio
async def test_click_attaches_post_action_url_for_broker_policy() -> None:
    client = FakeBrowserSkillClient(
        [
            {"ok": True, "browsers": [{"instance_id": "chrome"}], "sessions": []},
            {"ok": True, "session_id": "redirect"},
            {"ok": True, "tab_id": 4},
            {
                "ok": True,
                "tabs": [
                    {
                        "tab_id": 4,
                        "url": "https://accounts.example.com/continue",
                        "title": "Continue",
                    }
                ],
            },
        ]
    )

    payload = await client.execute(
        BrowserAction(action="click", page_id="4", ref="@e2", confirmed=True)
    )

    assert payload["final_url"] == "https://accounts.example.com/continue"
    assert payload["title"] == "Continue"
    assert client.commands[-1][0] == [
        "tab",
        "list",
        "--session",
        "redirect",
        "--scope",
        "agent",
    ]


def test_browser_skill_screenshot_path_cannot_escape_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "lantai.app.browser_workbench.browser_skill.get_current_workspace_dir",
        lambda: workspace,
    )

    assert BrowserSkillClient._resolve_output_path("shots/page.png") == (
        workspace / "browser" / "shots" / "page.png"
    )
    with pytest.raises(ValueError, match="workspace/browser"):
        BrowserSkillClient._resolve_output_path("../../outside.png")


@pytest.mark.asyncio
async def test_broker_uses_connected_browser_skill_before_playwright_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingBridge:
        available = False

    class ConnectedBrowserSkill:
        async def status(self):
            return {"ok": True, "connected": True}

        async def execute(self, action):
            return {"ok": True, "tab_id": 7, "url": action.url}

    broker = BrowserActionBroker(
        bridge=MissingBridge(),  # type: ignore[arg-type]
        browser_skill=ConnectedBrowserSkill(),  # type: ignore[arg-type]
    )

    async def unexpected_fallback(_action):
        raise AssertionError("connected BrowserSkill must precede Playwright fallback")

    monkeypatch.setattr(broker, "_execute_playwright_fallback", unexpected_fallback)
    payload = await broker._execute_runtime(
        BrowserAction(action="open", url="https://example.com"),
        "electron_bridge",
    )

    assert payload["runtime"] == "browser_skill"
    assert payload["fallback_reason"] == "Electron Browser Bridge is unavailable."


@pytest.mark.asyncio
async def test_broker_does_not_guess_between_multiple_browser_skill_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingBridge:
        available = False

    class AmbiguousBrowserSkill:
        async def status(self):
            return {"ok": True, "connected": True, "ready": False}

        async def execute(self, _action):
            raise AssertionError("ambiguous logged-in browser must not be selected")

    broker = BrowserActionBroker(
        bridge=MissingBridge(),  # type: ignore[arg-type]
        browser_skill=AmbiguousBrowserSkill(),  # type: ignore[arg-type]
    )

    async def expected_fallback(action):
        return {"ok": True, "url": action.url}

    monkeypatch.setattr(broker, "_execute_playwright_fallback", expected_fallback)
    payload = await broker._execute_runtime(
        BrowserAction(action="open", url="https://example.com"),
        "electron_bridge",
    )

    assert payload["runtime"] == "playwright_fallback"
    assert "BrowserSkill is not ready" in payload["fallback_reason"]


@pytest.mark.asyncio
async def test_explicit_browser_skill_runtime_fails_closed_without_cross_runtime_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingBrowserSkill:
        async def execute(self, _action):
            return {
                "ok": False,
                "error_code": "browser_skill_not_installed",
                "error": "missing",
            }

    broker = BrowserActionBroker(
        browser_skill=MissingBrowserSkill(),  # type: ignore[arg-type]
    )

    async def unexpected_fallback(_action):
        raise AssertionError("explicit logged-in runtime must not change browser silently")

    monkeypatch.setattr(broker, "_execute_playwright_fallback", unexpected_fallback)
    payload = await broker._execute_runtime(
        BrowserAction(action="snapshot"),
        "browser_skill",
    )

    assert payload["ok"] is False
    assert payload["runtime"] == "browser_skill"
