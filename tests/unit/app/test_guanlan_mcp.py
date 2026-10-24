# -*- coding: utf-8 -*-
"""Tests for the built-in Guanlan MCP integration."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import lantai.app.mcp.guanlan as guanlan_mcp
from lantai.app.routers.mcp import router as mcp_router
from lantai.app.mcp.guanlan import (
    GUANLAN_CLIENT_KEY,
    _raw_config_has_enabled_guanlan,
    build_guanlan_client_config,
    ensure_guanlan_default,
    get_guanlan_package_state,
    is_guanlan_package_update_disabled,
    should_prepare_guanlan_for_startup,
)
from lantai.app.mcp.guanlan_native import (
    build_guanlan_native_prompt,
    compact_guanlan_search_payload,
    guanlan_call_is_privileged_read_only,
    guanlan_profile_for_query,
    official_guanlan_policy_decision,
    prepare_guanlan_tool_call,
    review_guanlan_search_payload,
)


@pytest.fixture(autouse=True)
def isolate_external_guanlan_discovery(monkeypatch):
    """Keep unit tests independent from Guanlan tools installed on the host."""

    guanlan_mcp.select_guanlan_runtime.cache_clear()
    monkeypatch.setattr(
        guanlan_mcp,
        "_external_guanlan_mcp_candidates",
        lambda: (),
    )
    yield
    guanlan_mcp.select_guanlan_runtime.cache_clear()


def test_native_search_uses_wide_structured_evidence_defaults():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "profile": {
                "type": "string",
                "enum": ["global", "china", "english", "hybrid"],
            },
            "format": {"type": "string"},
            "evidence_mode": {"type": "string"},
        },
    }

    prepared = prepare_guanlan_tool_call(
        "guanlan_search",
        {"query": "最近国内 AI 政策"},
        input_schema=schema,
    )

    assert prepared.arguments == {
        "query": "最近国内 AI 政策",
        "limit": 80,
        "profile": "china",
        "format": "json",
        "evidence_mode": "shadow",
    }
    assert prepared.timeout_seconds == 90


def test_compacted_search_turns_ansi_counter_into_human_progress():
    payload = {
        "progress": "\x1b[33m5/19\x1b[0m",
        "results": [
            {
                "title": "Source",
                "url": "https://example.com/source",
                "domain": "example.com",
            },
        ],
    }

    compacted = compact_guanlan_search_payload(
        payload, min_items=1, max_items=1
    )

    assert compacted["lantai_view"]["progress"] == {
        "completed": 5,
        "total": 19,
        "remaining": 14,
        "summary": "Processed 5 of 19 sources; 14 remaining.",
    }


def test_native_profile_distinguishes_product_tokens_from_bilingual_queries():
    assert guanlan_profile_for_query("最近国内 AI 政策") == "china"
    assert guanlan_profile_for_query("latest AI policy in Europe") == "english"
    assert (
        guanlan_profile_for_query(
            "比较 OpenAI Responses API and Anthropic Messages API worldwide",
        )
        == "hybrid"
    )


def test_guanlan_privilege_is_capability_scoped_not_a_namespace_bypass():
    assert guanlan_call_is_privileged_read_only(
        "guanlan_search",
        {"query": "最近政策"},
    )
    assert guanlan_call_is_privileged_read_only("guanlan_status", {})
    assert guanlan_call_is_privileged_read_only(
        "guanlan_read",
        {"url": "https://example.com/report"},
    )
    assert not guanlan_call_is_privileged_read_only(
        "guanlan_read",
        {"url": "http://127.0.0.1/private"},
    )
    assert not guanlan_call_is_privileged_read_only(
        "guanlan_browser_assist_run",
        {"url": "https://example.com", "execute": True},
    )
    assert not guanlan_call_is_privileged_read_only(
        "guanlan_feeds",
        {"watchlist_path": "/Users/example/private.json"},
    )
    assert not guanlan_call_is_privileged_read_only(
        "guanlan_archive_search",
        {"query": "private notes"},
    )
    assert not guanlan_call_is_privileged_read_only(
        "guanlan_hotnews",
        {"newsnow_base_url": "http://127.0.0.1:8080"},
    )
    assert not guanlan_call_is_privileged_read_only(
        "guanlan_feeds",
        {"source": "http://192.168.1.2/private.xml"},
    )
    assert guanlan_call_is_privileged_read_only(
        "guanlan_feeds",
        {"source": "https://example.com/public.xml"},
    )


def test_guanlan_privilege_does_not_override_plan_mode_network_block():
    assert (
        official_guanlan_policy_decision(
            "guanlan_search",
            {"query": "latest policy"},
            permission_mode="plan",
        )
        is None
    )


def test_native_prompt_is_conditional_and_encodes_search_first_contract():
    official = SimpleNamespace(_lantai_client_key="guanlan")
    namesake = SimpleNamespace(name="guanlan")

    prompt = build_guanlan_native_prompt([official], language="zh")

    assert "search 是默认外部信息入口" in prompt
    assert "80 条候选" in prompt
    assert "5—12 个代表项" in prompt
    assert "can_cite_as_page_body=true" in prompt
    assert "research" in prompt
    assert "recommended_route=guanlan_read" in prompt
    assert "同一 URL" in prompt
    assert build_guanlan_native_prompt([namesake], language="zh") == ""
    unavailable = build_guanlan_native_prompt(
        [],
        language="zh",
        capability_status="error",
    )
    assert "观澜是澜台内置的系统级能力" in unavailable
    assert "暂时不可用" in unavailable
    assert "不要让用户安装" in unavailable
    assert "web_search" in unavailable
    assert build_guanlan_native_prompt([], language="zh") == ""


def test_search_payload_compacts_ranked_pool_without_losing_review_signals():
    payload = {
        "results": [
            {
                "title": f"Result {index}",
                "url": f"https://source{index % 7}.example/{index}",
                "domain": f"source{index % 7}.example",
                "snippet": f"Summary {index}",
                "source_type": "official" if index % 3 == 0 else "media",
                "evidence_role": "primary" if index % 4 == 0 else "context",
                "topic_key": f"topic-{index % 9}",
                "topic_role": "representative" if index < 9 else "related",
                "trust_level": 3,
                "score_parts": {"noise": 1},
                "trace": {"large": "debug"},
            }
            for index in range(30)
        ],
        "diagnostics": {"quality_gate": {"reason": "partial_salvage"}},
        "agent_followup": {"next_decision": "continue"},
    }

    compacted = compact_guanlan_search_payload(payload)

    assert compacted["lantai_view"] == {
        "schema_version": "lantai_guanlan_search_view_v1",
        "original_count": 30,
        "representative_count": 12,
        "compacted": True,
    }
    assert len(compacted["results"]) == 12
    assert len({row["url"] for row in compacted["results"]}) == 12
    assert "score_parts" not in compacted["results"][0]
    assert "trace" not in compacted["results"][0]
    assert compacted["agent_followup"]["next_decision"] == "continue"
    assert compacted["diagnostics"]["quality_gate"]["reason"] == (
        "partial_salvage"
    )


def test_search_review_repairs_empty_results_instead_of_claiming_no_evidence():
    review = review_guanlan_search_payload(
        {"results": [], "agent_followup": {}},
        {"query": "最近政策", "limit": 80},
    )

    assert review["decision"] == "repair"
    assert review["strong_conclusion_allowed"] is False
    assert "empty_results" in review["reasons"]


def test_search_review_marks_small_limit_as_non_conclusive_smoke_sample():
    review = review_guanlan_search_payload(
        {
            "results": [
                {
                    "url": f"https://source{index}.example/item",
                    "domain": f"source{index}.example",
                    "topic_key": f"topic-{index}",
                    "evidence_role": "primary",
                }
                for index in range(5)
            ],
            "agent_followup": {"next_decision": "answer"},
        },
        {"query": "快速看五条", "limit": 5},
    )

    assert review["decision"] == "continue"
    assert review["strong_conclusion_allowed"] is False
    assert "small_limit_smoke_sample" in review["reasons"]


def test_search_review_repairs_results_outside_requested_site():
    review = review_guanlan_search_payload(
        {
            "results": [
                {
                    "url": "https://example.com/policy",
                    "domain": "example.com",
                    "topic_key": "policy",
                    "evidence_role": "primary",
                }
            ],
            "agent_followup": {"next_decision": "answer"},
        },
        {"query": "最新政策", "site": "gov.cn", "limit": 80},
    )

    assert review["decision"] == "repair"
    assert review["strong_conclusion_allowed"] is False
    assert "site_constraint_violated" in review["reasons"]


def test_search_review_continues_on_partial_salvage_or_thin_diversity():
    review = review_guanlan_search_payload(
        {
            "results": [
                {
                    "url": f"https://official.example/{index}",
                    "domain": "official.example",
                    "topic_key": "same-topic",
                    "evidence_role": "official",
                }
                for index in range(40)
            ],
            "diagnostics": {"quality_gate": {"reason": "partial_salvage"}},
            "agent_followup": {"next_decision": "answer"},
        },
        {"query": "市场和用户反响", "limit": 80},
    )

    assert review["decision"] == "continue"
    assert review["strong_conclusion_allowed"] is False
    assert "partial_salvage" in review["reasons"]
    assert "single_domain" in review["reasons"]
    assert "single_topic" in review["reasons"]
    assert "single_evidence_role" in review["reasons"]


def test_build_guanlan_client_config_uses_backend_python():
    cfg = build_guanlan_client_config(enabled=True)

    assert cfg.enabled is True
    assert cfg.transport == "stdio"
    assert cfg.command
    assert cfg.args
    assert "PYTHONPATH" not in cfg.env
    assert "LANTAI_GUANLAN_PACKAGE_DIR" not in cfg.env


def test_runtime_package_update_helpers_are_removed():
    assert not hasattr(guanlan_mcp, "GUANLAN_PACKAGE_REQUIREMENT")
    assert not hasattr(guanlan_mcp, "_find_install_python")
    assert not hasattr(guanlan_mcp, "_python_can_install_guanlan")
    assert not hasattr(guanlan_mcp, "_run_pip_install")
    # A bounded subprocess probe is allowed for a user-installed MCP entry
    # point; package installation or environment mutation remains forbidden.
    assert "pip" not in guanlan_mcp.__dict__
    assert not hasattr(guanlan_mcp, "_get_guanlan_updater")
    assert not hasattr(guanlan_mcp, "update_guanlan_package")
    assert not hasattr(guanlan_mcp, "schedule_guanlan_package_update")


def test_official_distribution_disables_manual_guanlan_updates(monkeypatch):
    monkeypatch.delenv("LANTAI_DISABLE_GUANLAN_PACKAGE_UPDATE", raising=False)
    monkeypatch.delenv("LANTAI_ENABLE_GUANLAN_PACKAGE_UPDATE", raising=False)
    monkeypatch.delenv("LANTAI_DISTRIBUTION_CHANNEL", raising=False)
    monkeypatch.delenv("LANTAI_RELEASE_CHANNEL", raising=False)
    monkeypatch.delenv("LANTAI_DISTRIBUTION", raising=False)
    monkeypatch.delenv("LANTAI_APP_STORE", raising=False)

    assert is_guanlan_package_update_disabled() is True


def test_legacy_enable_env_cannot_reenable_manual_updates(monkeypatch):
    monkeypatch.delenv("LANTAI_DISABLE_GUANLAN_PACKAGE_UPDATE", raising=False)
    monkeypatch.delenv("LANTAI_DISTRIBUTION_CHANNEL", raising=False)
    monkeypatch.delenv("LANTAI_RELEASE_CHANNEL", raising=False)
    monkeypatch.delenv("LANTAI_DISTRIBUTION", raising=False)
    monkeypatch.delenv("LANTAI_APP_STORE", raising=False)
    monkeypatch.setenv("LANTAI_ENABLE_GUANLAN_PACKAGE_UPDATE", "1")

    assert is_guanlan_package_update_disabled() is True
    assert build_guanlan_client_config(enabled=True).env == {}


def test_frozen_official_runtime_disables_manual_updates(monkeypatch):
    monkeypatch.delenv("LANTAI_DISABLE_GUANLAN_PACKAGE_UPDATE", raising=False)
    monkeypatch.delenv("LANTAI_DISTRIBUTION_CHANNEL", raising=False)
    monkeypatch.delenv("LANTAI_RELEASE_CHANNEL", raising=False)
    monkeypatch.delenv("LANTAI_DISTRIBUTION", raising=False)
    monkeypatch.delenv("LANTAI_APP_STORE", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert is_guanlan_package_update_disabled() is True


def test_bootstrap_does_not_prepend_writable_package_dir_when_updates_disabled(
    monkeypatch,
):
    monkeypatch.delenv("LANTAI_ENABLE_GUANLAN_PACKAGE_UPDATE", raising=False)
    monkeypatch.setenv("LANTAI_DISABLE_GUANLAN_PACKAGE_UPDATE", "1")
    package_dir = str(guanlan_mcp.get_guanlan_package_dir())
    original_path = list(sys.path)
    try:
        if package_dir in sys.path:
            sys.path.remove(package_dir)

        returned = guanlan_mcp.bootstrap_guanlan_import_path()

        assert str(returned) == package_dir
        assert package_dir not in sys.path
    finally:
        sys.path[:] = original_path


def test_ensure_guanlan_package_reports_ready_state_without_network_update(
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_ENABLE_GUANLAN_PACKAGE_UPDATE", "1")
    monkeypatch.setattr(
        "lantai.app.mcp.guanlan.get_guanlan_package_version",
        lambda: "1.2.3",
    )

    state = asyncio.run(guanlan_mcp.ensure_guanlan_package())

    assert state.status == "ready"
    assert state.version == "1.2.3"
    assert "disabled" not in state.message


def test_build_guanlan_client_config_uses_cli_command_when_frozen(
    monkeypatch,
):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/澜台/lantai-server")

    guanlan_mcp.select_guanlan_runtime.cache_clear()
    monkeypatch.setattr(
        guanlan_mcp,
        "_external_guanlan_mcp_candidates",
        lambda: (),
    )
    cfg = build_guanlan_client_config(enabled=True)

    assert cfg.command == "/Applications/澜台/lantai-server"
    assert cfg.args == ["guanlan-mcp"]
    guanlan_mcp.select_guanlan_runtime.cache_clear()


def test_external_runtime_probe_rejects_cli_that_cannot_initialize_mcp(
    monkeypatch,
    tmp_path,
):
    command = tmp_path / "guanlan-mcp"
    cli = tmp_path / "guanlan"
    command.touch(mode=0o755)
    cli.touch(mode=0o755)
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="guanlan 0.11.0", stderr=""),
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="MCP not installed",
            ),
        ]
    )
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr(
        guanlan_mcp.subprocess,
        "run",
        fake_run,
    )

    assert guanlan_mcp._probe_external_guanlan_runtime(command) is None
    assert calls[1][0][0] == [str(command)]
    assert '"method": "initialize"' in calls[1][1]["input"]


def test_external_runtime_probe_accepts_legacy_initialize_response(
    monkeypatch,
    tmp_path,
):
    command = tmp_path / "guanlan-mcp"
    cli = tmp_path / "guanlan"
    command.touch(mode=0o755)
    cli.touch(mode=0o755)
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="guanlan 0.11.0", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"jsonrpc":"2.0","id":2,"result":'
                    '{"protocolVersion":"2025-06-18","capabilities":{},'
                    '"serverInfo":{"name":"guanlan","version":"0.11.0"}}}\n'
                ),
                stderr="",
            ),
        ]
    )
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr(
        guanlan_mcp.subprocess,
        "run",
        fake_run,
    )

    runtime = guanlan_mcp._probe_external_guanlan_runtime(command)

    assert runtime is not None
    assert runtime.command == str(command)
    assert runtime.version == "0.11.0"
    assert runtime.source == "user"
    assert calls[1][0][0] == [str(command)]
    assert '"method": "server/discover"' in calls[1][1]["input"]
    assert '"method": "initialize"' in calls[1][1]["input"]


def test_external_runtime_probe_accepts_modern_discover_response(
    monkeypatch,
    tmp_path,
):
    command = tmp_path / "guanlan-mcp"
    cli = tmp_path / "guanlan"
    command.touch(mode=0o755)
    cli.touch(mode=0o755)
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="guanlan 0.12.0", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"jsonrpc":"2.0","id":1,"result":'
                    '{"supportedVersions":["2026-07-28"],'
                    '"serverInfo":{"name":"guanlan","version":"0.12.0"},'
                    '"capabilities":{}}}\n'
                ),
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(
        guanlan_mcp.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )

    runtime = guanlan_mcp._probe_external_guanlan_runtime(command)

    assert runtime is not None
    assert runtime.version == "0.12.0"


def test_newer_user_installed_guanlan_is_preferred(monkeypatch):
    bundled = guanlan_mcp.GuanlanRuntime(
        command="/Applications/澜台/lantai-server",
        args=("guanlan-mcp",),
        version="0.10.9",
        source="bundled",
    )
    external = guanlan_mcp.GuanlanRuntime(
        command="/Users/example/.local/bin/guanlan-mcp",
        args=(),
        version="0.11.0",
        source="user",
    )
    monkeypatch.setattr(guanlan_mcp, "_bundled_guanlan_runtime", lambda: bundled)
    monkeypatch.setattr(
        guanlan_mcp,
        "_external_guanlan_mcp_candidates",
        lambda: (Path(external.command),),
    )
    monkeypatch.setattr(
        guanlan_mcp,
        "_probe_external_guanlan_runtime",
        lambda _command: external,
    )
    guanlan_mcp.select_guanlan_runtime.cache_clear()

    cfg = build_guanlan_client_config(enabled=True)

    assert cfg.command == external.command
    assert cfg.args == []
    guanlan_mcp.select_guanlan_runtime.cache_clear()


@pytest.mark.parametrize("external_version", ["0.10.9", "0.10.8"])
def test_equal_or_older_external_guanlan_keeps_bundled_fallback(
    monkeypatch,
    external_version,
):
    bundled = guanlan_mcp.GuanlanRuntime(
        command="/Applications/澜台/lantai-server",
        args=("guanlan-mcp",),
        version="0.10.9",
        source="bundled",
    )
    external = guanlan_mcp.GuanlanRuntime(
        command="/Users/example/.local/bin/guanlan-mcp",
        args=(),
        version=external_version,
        source="user",
    )
    monkeypatch.setattr(guanlan_mcp, "_bundled_guanlan_runtime", lambda: bundled)
    monkeypatch.setattr(
        guanlan_mcp,
        "_external_guanlan_mcp_candidates",
        lambda: (Path(external.command),),
    )
    monkeypatch.setattr(
        guanlan_mcp,
        "_probe_external_guanlan_runtime",
        lambda _command: external,
    )
    guanlan_mcp.select_guanlan_runtime.cache_clear()

    runtime = guanlan_mcp.select_guanlan_runtime()

    assert runtime == bundled
    guanlan_mcp.select_guanlan_runtime.cache_clear()


def test_unstartable_external_guanlan_keeps_bundled_fallback(monkeypatch):
    bundled = guanlan_mcp.GuanlanRuntime(
        command="/Applications/澜台/lantai-server",
        args=("guanlan-mcp",),
        version="0.10.9",
        source="bundled",
    )
    monkeypatch.setattr(guanlan_mcp, "_bundled_guanlan_runtime", lambda: bundled)
    monkeypatch.setattr(
        guanlan_mcp,
        "_external_guanlan_mcp_candidates",
        lambda: (Path("/Users/example/.local/bin/guanlan-mcp"),),
    )
    monkeypatch.setattr(
        guanlan_mcp,
        "_probe_external_guanlan_runtime",
        lambda _command: None,
    )
    guanlan_mcp.select_guanlan_runtime.cache_clear()

    assert guanlan_mcp.select_guanlan_runtime() == bundled
    guanlan_mcp.select_guanlan_runtime.cache_clear()


def test_raw_config_has_enabled_guanlan_detects_reserved_client():
    data = {
        "mcp": {
            "clients": {
                GUANLAN_CLIENT_KEY: {
                    "enabled": True,
                },
            },
        },
    }

    assert _raw_config_has_enabled_guanlan(data) is True


def test_raw_config_has_enabled_guanlan_ignores_disabled_client():
    data = {
        "mcp": {
            "clients": {
                GUANLAN_CLIENT_KEY: {
                    "enabled": False,
                },
            },
        },
    }

    assert _raw_config_has_enabled_guanlan(data) is False


def test_missing_guanlan_defaults_to_enabled():
    data = {
        "mcp": {
            "clients": {},
        },
    }

    assert _raw_config_has_enabled_guanlan(data) is True


def test_ensure_guanlan_default_preserves_explicit_disabled():
    from lantai.config.config import MCPConfig

    cfg = MCPConfig(clients={})
    cfg.clients[GUANLAN_CLIENT_KEY] = build_guanlan_client_config(
        enabled=False,
    )

    merged = ensure_guanlan_default(cfg)

    assert merged.clients[GUANLAN_CLIENT_KEY].enabled is False


def test_mas_distribution_disables_guanlan_package_updates(monkeypatch):
    monkeypatch.setenv("LANTAI_DISTRIBUTION_CHANNEL", "mas")

    assert is_guanlan_package_update_disabled() is True


def test_app_store_flag_disables_guanlan_package_updates(monkeypatch):
    monkeypatch.delenv("LANTAI_DISTRIBUTION_CHANNEL", raising=False)
    monkeypatch.setenv("LANTAI_APP_STORE", "1")

    assert is_guanlan_package_update_disabled() is True


def test_disabled_guanlan_package_state_reports_bundled_runtime(monkeypatch):
    monkeypatch.setenv("LANTAI_DISABLE_GUANLAN_PACKAGE_UPDATE", "1")
    monkeypatch.setattr(
        "lantai.app.mcp.guanlan.get_guanlan_package_version",
        lambda: "1.2.3",
    )

    state = get_guanlan_package_state()

    assert state.status == "ready"
    assert state.version == "1.2.3"
    assert "updates with Lantai" in state.message


def test_should_prepare_guanlan_for_startup_is_disabled():
    assert should_prepare_guanlan_for_startup() is False


@pytest.mark.asyncio
async def test_status_reports_bundled_update_policy(monkeypatch):
    async def fake_get_agent_for_request(_request):
        return SimpleNamespace(
            agent_id="default",
            config=SimpleNamespace(mcp=None),
            mcp_manager=None,
        )

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent_for_request,
    )
    monkeypatch.setattr(
        "lantai.app.mcp.guanlan.get_guanlan_package_version",
        lambda: "0.10.1",
    )
    app = FastAPI()
    app.include_router(mcp_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/mcp/guanlan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["package_version"] == "0.10.1"
    assert payload["bundled_version"] == "0.10.1"
    assert payload["runtime_source"] == "bundled"
    assert payload["capability_status"] == "starting"
    assert payload["connected"] is False
    assert payload["package_update_supported"] is False
    assert "updates with Lantai" in payload["package_message"]


@pytest.mark.asyncio
async def test_status_never_reports_ready_when_native_runtime_failed(
    monkeypatch,
):
    from lantai.app.mcp.manager import MCPRuntimeState
    from lantai.config.config import MCPConfig

    class Manager:
        async def get_client(self, _key):
            return None

        async def get_runtime_state(self, _key):
            return MCPRuntimeState(
                client_key="guanlan",
                status="error",
                attempt_count=2,
                error_code="RuntimeError",
                diagnostic_message="server exited before initialize",
                updated_at=123.0,
            )

    async def fake_get_agent_for_request(_request):
        return SimpleNamespace(
            agent_id="default",
            config=SimpleNamespace(mcp=MCPConfig(clients={})),
            mcp_manager=Manager(),
        )

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent_for_request,
    )
    monkeypatch.setattr(
        "lantai.app.mcp.guanlan.get_guanlan_package_version",
        lambda: "0.10.1",
    )
    app = FastAPI()
    app.include_router(mcp_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/mcp/guanlan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["package_status"] == "ready"
    assert payload["capability_status"] == "error"
    assert payload["connected"] is False
    assert payload["runtime_error_code"] == "RuntimeError"
    assert "暂时不可用" in payload["runtime_message"]


@pytest.mark.asyncio
async def test_status_never_reports_ready_after_native_transport_disconnects(
    monkeypatch,
):
    from lantai.app.mcp.manager import MCPRuntimeState
    from lantai.config.config import MCPConfig

    class Manager:
        async def get_client(self, _key):
            return SimpleNamespace(
                is_connected=False,
                reconnect_attempt=3,
                last_connection_error="stdio server exited",
            )

        async def get_runtime_state(self, _key):
            return MCPRuntimeState(
                client_key="guanlan",
                status="ready",
                attempt_count=1,
                updated_at=123.0,
            )

    async def fake_get_agent_for_request(_request):
        return SimpleNamespace(
            agent_id="default",
            config=SimpleNamespace(mcp=MCPConfig(clients={})),
            mcp_manager=Manager(),
        )

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent_for_request,
    )
    monkeypatch.setattr(
        "lantai.app.mcp.guanlan.get_guanlan_package_version",
        lambda: "0.10.1",
    )
    app = FastAPI()
    app.include_router(mcp_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/mcp/guanlan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_status"] == "degraded"
    assert payload["connected"] is False
    assert payload["runtime_attempt_count"] == 3


@pytest.mark.asyncio
async def test_update_endpoint_is_removed():
    """Bundled Guanlan only updates as part of a Lantai app release."""

    app = FastAPI()
    app.include_router(mcp_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/mcp/guanlan/update")

    assert response.status_code == 404
