from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "frozen_backend_console_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "frozen_backend_console_smoke", SCRIPT_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(**overrides):
    values = {
        "desktop_auth": False,
        "restart_cycle": False,
        "desktop_token": "",
        "desktop_instance_nonce": "",
        "distribution": "official",
        "native_default": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_desktop_auth_builds_env_and_headers(tmp_path: Path) -> None:
    module = _load_module()
    args = _args(
        desktop_auth=True,
        desktop_token="desktop-token",
        desktop_instance_nonce="desktop-nonce",
        distribution="mas",
        native_default=True,
    )

    desktop_env, headers, nonce = module._desktop_auth_for_cycle(args, cycle=1)
    env = module._build_env(args, tmp_path, desktop_env=desktop_env)

    assert headers == {"X-Lantai-Desktop-Token": "desktop-token"}
    assert nonce == "desktop-nonce"
    assert env["LANTAI_DESKTOP_TOKEN"] == "desktop-token"
    assert env["LANTAI_DESKTOP_INSTANCE_NONCE"] == "desktop-nonce"
    assert env["LANTAI_APP_STORE"] == "1"
    assert env["LANTAI_DISTRIBUTION_CHANNEL"] == "mas"
    assert env["LANTAI_RUNTIME_ENGINE_DEFAULT"] == "native"


def test_restart_cycle_implies_desktop_auth() -> None:
    module = _load_module()
    args = _args(
        restart_cycle=True,
        desktop_token="restart-token",
        desktop_instance_nonce="restart-nonce",
    )

    desktop_env, headers, nonce = module._desktop_auth_for_cycle(args, cycle=2)

    assert desktop_env == {
        "LANTAI_DESKTOP_TOKEN": "restart-token",
        "LANTAI_DESKTOP_INSTANCE_NONCE": "restart-nonce",
    }
    assert headers == {"X-Lantai-Desktop-Token": "restart-token"}
    assert nonce == "restart-nonce"


def test_desktop_readiness_requires_matching_nonce(monkeypatch) -> None:
    module = _load_module()
    calls = []

    def fake_json_request(method, url, *, headers, timeout):
        calls.append((method, url, headers, timeout))
        return 200, "{}", {
            "status": "ok",
            "desktop": True,
            "instance_nonce": "desktop-nonce",
            "pid": 12345,
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    pid = module._assert_desktop_readiness(
        "http://127.0.0.1:19090",
        headers={"X-Lantai-Desktop-Token": "desktop-token"},
        expected_nonce="desktop-nonce",
        timeout=3,
    )

    assert pid == 12345
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:19090/api/desktop/readiness",
            {"X-Lantai-Desktop-Token": "desktop-token"},
            3,
        )
    ]


def test_desktop_readiness_reports_stale_frozen_backend_on_404(monkeypatch) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, headers, timeout):
        raise HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    try:
        module._assert_desktop_readiness(
            "http://127.0.0.1:19090",
            headers={"X-Lantai-Desktop-Token": "desktop-token"},
            expected_nonce="desktop-nonce",
            timeout=3,
        )
    except AssertionError as exc:
        assert "frozen backend is stale" in str(exc)
    else:
        raise AssertionError("Expected stale frozen backend assertion")


def test_api_version_rejects_stale_running_backend(monkeypatch) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, headers, timeout):
        assert method == "GET"
        assert url.endswith("/api/version")
        return 200, '{"version":"1.0.48"}', {"version": "1.0.48"}

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    with pytest.raises(AssertionError, match="1\\.0\\.48.*1\\.0\\.49"):
        module._assert_api_version(
            "http://127.0.0.1:19090",
            headers={"X-Lantai-Desktop-Token": "desktop-token"},
            expected_version="1.0.49",
            timeout=3,
        )


def test_local_retrieval_probe_requires_real_frozen_vector_operations(
    monkeypatch,
) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, headers, timeout):
        assert method == "GET"
        assert url.endswith("/api/workspace/layered-memory")
        return 200, "{}", {
            "engine_status": {
                "overall": "ready",
                "embedding": {
                    "status": "已就绪",
                    "provider": "local",
                    "dimensions": 512,
                },
                "vector_index": {
                    "status": "已就绪",
                    "probe": {"write": True, "query": True, "delete": True},
                },
            }
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    module._assert_local_retrieval_runtime(
        "http://127.0.0.1:19090",
        headers={"X-Lantai-Desktop-Token": "desktop-token"},
        timeout=5,
    )


def test_persisted_failure_event_reads_preserve_desktop_headers(monkeypatch) -> None:
    module = _load_module()
    calls = []

    def fake_json_request(method, url, *, headers, timeout):
        calls.append((method, url, headers, timeout))
        if url.endswith("/api/chats?mode=all"):
            return 200, "[]", [{"id": "chat-1", "session_id": "console:test"}]
        return 200, "{}", {
            "events": [
                {
                    "type": "turn.failed",
                    "status": "failed",
                    "payload": {"final_task_status": "failed"},
                }
            ]
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    chat_id = module._assert_persisted_failure_events(
        "http://127.0.0.1:19090",
        session_id="console:test",
        headers={"X-Lantai-Desktop-Token": "desktop-token"},
        timeout=5,
    )

    assert chat_id == "chat-1"
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:19090/api/chats?mode=all",
            {"X-Lantai-Desktop-Token": "desktop-token"},
            5,
        ),
        (
            "GET",
            "http://127.0.0.1:19090/api/chats/chat-1/events?after_seq=0",
            {"X-Lantai-Desktop-Token": "desktop-token"},
            5,
        ),
    ]


def test_console_failure_path_replays_client_turn_id_as_deduped(
    monkeypatch,
) -> None:
    module = _load_module()
    calls = []
    chat_requests = []

    def sse(payload):
        return f"data: {json.dumps(payload)}\n\n"

    def fake_json_request(method, url, *, payload=None, headers, timeout):
        calls.append((method, url, payload, headers, timeout))
        if method == "POST" and url.endswith("/api/console/chat"):
            assert payload is not None
            chat_requests.append(payload)
            client_turn_id = payload["meta"]["client_turn_id"]
            if len(chat_requests) == 1:
                return 200, sse({"object": "response", "status": "failed"}), None
            return 200, sse(
                {
                    "object": "response",
                    "status": "completed",
                    "metadata": {
                        "deduped_client_turn": True,
                        "client_turn_id": client_turn_id,
                        "duplicate_of_run_id": "run-original",
                    },
                }
            ), None
        if method == "GET" and url.endswith("/api/chats?mode=all"):
            assert chat_requests
            return 200, "[]", [
                {
                    "id": "chat-1",
                    "session_id": chat_requests[0]["session_id"],
                }
            ]
        if method == "GET" and url.endswith("/api/chats/chat-1/events?after_seq=0"):
            return 200, "{}", {
                "events": [
                    {"type": "turn.started", "status": "running"},
                    {"type": "model.routing_decision", "status": "running"},
                    {
                        "type": "runtime.error",
                        "status": "failed",
                        "payload": {"message": "No active model configured."},
                    },
                    {
                        "type": "turn.failed",
                        "status": "failed",
                        "payload": {"final_task_status": "failed"},
                    },
                ]
            }
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    session_id, chat_id = module._exercise_console_failure_path(
        "http://127.0.0.1:19090",
        headers={"X-Lantai-Desktop-Token": "desktop-token"},
        timeout=5,
    )

    assert chat_id == "chat-1"
    assert session_id == chat_requests[0]["session_id"]
    assert len(chat_requests) == 2
    assert chat_requests[0]["meta"]["client_turn_id"].startswith(
        "client-turn-frozen-smoke-"
    )
    assert (
        chat_requests[0]["meta"]["client_turn_id"]
        == chat_requests[1]["meta"]["client_turn_id"]
    )
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:19090/api/console/chat",
            chat_requests[0],
            {"X-Lantai-Desktop-Token": "desktop-token"},
            5,
        ),
        (
            "GET",
            "http://127.0.0.1:19090/api/chats?mode=all",
            None,
            {"X-Lantai-Desktop-Token": "desktop-token"},
            5,
        ),
        (
            "GET",
            "http://127.0.0.1:19090/api/chats/chat-1/events?after_seq=0",
            None,
            {"X-Lantai-Desktop-Token": "desktop-token"},
            5,
        ),
        (
            "POST",
            "http://127.0.0.1:19090/api/console/chat",
            chat_requests[1],
            {"X-Lantai-Desktop-Token": "desktop-token"},
            5,
        ),
        (
            "GET",
            "http://127.0.0.1:19090/api/chats/chat-1/events?after_seq=0",
            None,
            {"X-Lantai-Desktop-Token": "desktop-token"},
            5,
        ),
    ]
