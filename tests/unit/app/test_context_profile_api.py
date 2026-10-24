# -*- coding: utf-8 -*-
"""Diagnostics must expose a calculated context window, not only config."""

from types import SimpleNamespace

import pytest

from lantai.app.runner.api import _context_profile_payload, get_chat_context_stats
from lantai.providers.provider import ModelInfo


class _ProviderManager:
    def __init__(self):
        self.provider = SimpleNamespace(
            models=[ModelInfo(id="remote", name="remote", context_window=128 * 1024)],
            extra_models=[],
        )

    def get_active_model(self):
        return None

    def get_provider(self, provider_id):
        return self.provider if provider_id == "remote-provider" else None


def test_context_profile_reports_requested_effective_and_unverified_state():
    workspace = SimpleNamespace(
        config=SimpleNamespace(
            running=SimpleNamespace(
                max_input_length=256 * 1024,
                context_policy_mode="economy",
            ),
            active_model=SimpleNamespace(provider_id="remote-provider", model="remote"),
        ),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(provider_manager=_ProviderManager())))

    payload = _context_profile_payload(workspace, request)

    assert payload["requested_window"] == 256 * 1024
    assert payload["model_window"] == 128 * 1024
    assert payload["effective_window"] == 128 * 1024
    assert payload["actual_window"] is None
    assert payload["context_profile_status"] == "unverified"
    assert payload["context_policy"]["mode"] == "economy"
    assert (
        payload["context_policy"]["target_input_tokens"]
        < payload["context_policy"]["usable_input_tokens"]
        < payload["effective_window"]
    )


@pytest.mark.asyncio
async def test_context_stats_use_effective_window_for_the_usage_denominator():
    class _Memory:
        def load_state_dict(self, *_args, **_kwargs):
            return None

        async def estimate_tokens(self, *, max_input_length):
            return {
                "total_messages": 1,
                "compressed_summary_tokens": 0,
                "messages_tokens": 100,
                "estimated_tokens": 100,
                "max_input_length": max_input_length,
                "context_usage_ratio": 100 / max_input_length * 100,
            }

    workspace = SimpleNamespace(
        config=SimpleNamespace(
            running=SimpleNamespace(max_input_length=256 * 1024),
            active_model=SimpleNamespace(provider_id="remote-provider", model="remote"),
        ),
        context_manager=SimpleNamespace(get_agent_context=lambda: _Memory()),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(provider_manager=_ProviderManager())),
    )
    manager = SimpleNamespace(
        get_chat=lambda _chat_id: _async_return(
                SimpleNamespace(
                    id="chat",
                    session_id="session",
                    user_id="user",
                channel="console",
                meta={"session_state_id": "console:chat:chat"},
            ),
        ),
    )
    session = SimpleNamespace(
        get_session_state_dict=lambda **_kwargs: _async_return(
            {
                "agent": {
                    "last_context_manifest": {
                        "categories": {
                            "system": {
                                "estimated": 20,
                                "upper_bound": 24,
                                "method": "estimated",
                            },
                            "history": {
                                "estimated": 80,
                                "upper_bound": 90,
                                "method": "estimated",
                            },
                        },
                        "discarded": {"old-wiki": "budget"},
                    },
                },
            },
        ),
    )

    result = await get_chat_context_stats(
        "chat",
        request,
        mgr=manager,
        session=session,
        workspace=workspace,
    )

    assert result["requested_window"] == 256 * 1024
    assert result["model_window"] == 128 * 1024
    assert result["effective_window"] == 128 * 1024
    assert result["max_input_length"] == result["context_policy"][
        "target_input_tokens"
    ]
    assert result["max_input_length"] < result["effective_window"]
    assert [category["key"] for category in result["categories"]] == [
        "system",
        "history",
    ]
    assert result["compaction"]["budget_discarded"] == 1
    assert result["count_source"] == "manifest_estimate"


async def _async_return(value):
    return value
