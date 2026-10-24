# -*- coding: utf-8 -*-
"""Tests for the lazily refreshed Claude Code client version."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import lantai.providers.claude_code_version as version_module


def _write_cache(
    path: Path,
    *,
    stable_version: str,
    checked_at: float,
    last_attempt_at: float | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package": "@anthropic-ai/claude-code",
                "stable_version": stable_version,
                "checked_at": checked_at,
                "last_attempt_at": (
                    checked_at if last_attempt_at is None else last_attempt_at
                ),
                "source": "npm",
            }
        ),
        encoding="utf-8",
    )


def test_embedded_fallback_tracks_a_recent_stable_release(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "claude-code-dist-tags.json"
    monkeypatch.setattr(version_module, "_cache_path", lambda: cache_path)
    monkeypatch.setattr(
        version_module,
        "schedule_claude_code_version_refresh",
        lambda **_kwargs: False,
    )

    assert (
        version_module.resolve_claude_cli_user_agent()
        == "claude-cli/2.1.212 (consumer, cli)"
    )


def test_fresh_cache_is_used_without_scheduling_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "claude-code-dist-tags.json"
    now = 2_000_000_000.0
    _write_cache(
        cache_path,
        stable_version="2.1.195",
        checked_at=now - version_module.REFRESH_INTERVAL_SECONDS + 1,
    )
    monkeypatch.setattr(version_module, "_cache_path", lambda: cache_path)
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        version_module,
        "schedule_claude_code_version_refresh",
        lambda **kwargs: scheduled.append(kwargs) or False,
    )

    assert (
        version_module.resolve_claude_cli_user_agent(now=now)
        == "claude-cli/2.1.195 (consumer, cli)"
    )
    assert scheduled == []


def test_expired_cache_schedules_one_silent_background_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "claude-code-dist-tags.json"
    now = 2_000_000_000.0
    _write_cache(
        cache_path,
        stable_version="2.1.195",
        checked_at=now - version_module.REFRESH_INTERVAL_SECONDS - 1,
    )
    monkeypatch.setattr(version_module, "_cache_path", lambda: cache_path)
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        version_module,
        "schedule_claude_code_version_refresh",
        lambda **kwargs: scheduled.append(kwargs) or True,
    )

    user_agent = version_module.resolve_claude_cli_user_agent(now=now)

    assert user_agent == "claude-cli/2.1.195 (consumer, cli)"
    assert scheduled == [{"cache_path": cache_path, "now": now}]


def test_invalid_fresh_cache_still_schedules_repair(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "claude-code-dist-tags.json"
    now = 2_000_000_000.0
    _write_cache(
        cache_path,
        stable_version="not-a-version",
        checked_at=now,
    )
    monkeypatch.setattr(version_module, "_cache_path", lambda: cache_path)
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        version_module,
        "schedule_claude_code_version_refresh",
        lambda **kwargs: scheduled.append(kwargs) or True,
    )

    assert (
        version_module.resolve_claude_cli_user_agent(now=now)
        == "claude-cli/2.1.212 (consumer, cli)"
    )
    assert scheduled == [{"cache_path": cache_path, "now": now}]


def test_registry_query_reads_the_stable_dist_tag(
    monkeypatch,
) -> None:
    calls: list[tuple[str, float]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"latest":"2.1.220","stable":"2.1.212"}'

    def fake_urlopen(request, *, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse()

    monkeypatch.setattr(version_module, "urlopen", fake_urlopen)

    tags = version_module._query_dist_tags()

    assert tags["stable"] == "2.1.212"
    assert calls == [
        (
            version_module.NPM_DIST_TAGS_URL,
            version_module.QUERY_TIMEOUT_SECONDS,
        ),
    ]


def test_successful_refresh_persists_stable_version_atomically(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "provider-cache" / "claude-code-dist-tags.json"
    monkeypatch.setattr(
        version_module,
        "_query_dist_tags",
        lambda: {"latest": "2.1.220", "stable": "2.1.212"},
    )

    assert (
        version_module.refresh_claude_code_version(
            cache_path=cache_path,
            now=2_000_000_000.0,
        )
        == "2.1.212"
    )
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "package": "@anthropic-ai/claude-code",
        "stable_version": "2.1.212",
        "checked_at": 2_000_000_000.0,
        "last_attempt_at": 2_000_000_000.0,
        "source": "npm_registry",
    }
    assert list(cache_path.parent.glob("*.tmp")) == []


def test_failed_refresh_preserves_last_stable_and_records_retry_backoff(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "claude-code-dist-tags.json"
    _write_cache(
        cache_path,
        stable_version="2.1.195",
        checked_at=1_900_000_000.0,
    )

    def fail_query():
        raise OSError("offline")

    monkeypatch.setattr(version_module, "_query_dist_tags", fail_query)

    assert (
        version_module.refresh_claude_code_version(
            cache_path=cache_path,
            now=2_000_000_000.0,
        )
        is None
    )
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["stable_version"] == "2.1.195"
    assert payload["checked_at"] == 1_900_000_000.0
    assert payload["last_attempt_at"] == 2_000_000_000.0
    assert payload["last_status"] == "failed"
    assert version_module._refresh_due(
        payload,
        now=2_000_000_000.0 + version_module.FAILURE_RETRY_SECONDS - 1,
    ) is False
    assert version_module._refresh_due(
        payload,
        now=2_000_000_000.0 + version_module.FAILURE_RETRY_SECONDS + 1,
    ) is True


def test_registry_failure_remains_observable_to_refresh_worker(monkeypatch) -> None:
    monkeypatch.setattr(
        version_module,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    try:
        version_module._query_dist_tags()
    except URLError:
        pass
    else:
        raise AssertionError("The low-level registry error should remain observable")


def test_refresh_scheduler_deduplicates_concurrent_requests(
    monkeypatch,
    tmp_path,
) -> None:
    started: list[object] = []

    class FakeThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            return None

    monkeypatch.setattr(version_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(version_module, "_refresh_inflight", False)
    cache_path = tmp_path / "claude-code-dist-tags.json"

    try:
        assert version_module.schedule_claude_code_version_refresh(
            cache_path=cache_path
        )
        assert not version_module.schedule_claude_code_version_refresh(
            cache_path=cache_path
        )
        assert len(started) == 1
    finally:
        version_module._refresh_inflight = False


def test_invalid_stable_version_never_replaces_a_valid_cache(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "claude-code-dist-tags.json"
    _write_cache(
        cache_path,
        stable_version="2.1.195",
        checked_at=1_900_000_000.0,
    )
    monkeypatch.setattr(
        version_module,
        "_query_dist_tags",
        lambda: {"stable": "not a version"},
    )

    assert (
        version_module.refresh_claude_code_version(
            cache_path=cache_path,
            now=2_000_000_000.0,
        )
        is None
    )
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["stable_version"] == "2.1.195"
