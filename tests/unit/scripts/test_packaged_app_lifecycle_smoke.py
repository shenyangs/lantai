# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import importlib.util
import json
import plistlib
import sys
import time
from pathlib import Path
from urllib import error, request

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "packaged_app_lifecycle_smoke.py"
)
RENDERER_SMOKE_PATH = (
    Path(__file__).resolve().parents[3]
    / "console"
    / "electron"
    / "chatRuntimeStreamSmoke.cjs"
)


def test_renderer_smoke_never_retries_a_single_ui_submission() -> None:
    text = RENDERER_SMOKE_PATH.read_text(encoding="utf-8")

    # A delayed React paint does not mean the first activation was rejected.
    # Retrying with the shared send/stop selector can click Stop or enqueue the
    # same draft a second time, turning a smoke probe into the source of the
    # duplicate-message failure it is meant to detect.
    assert "if (snapshot().user_bubble_count < 1)" not in text
    assert "if (!snapshot().user_texts.some((text) => text.includes(value)))" not in text


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "packaged_app_lifecycle_smoke", SCRIPT_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_renderer_smoke_enables_thinking_in_effective_customization_state() -> None:
    script = (
        Path(__file__).resolve().parents[3]
        / "console"
        / "electron"
        / "chatRuntimeStreamSmoke.cjs"
    ).read_text(encoding="utf-8")

    assert '"/customization/effective-state"' in script
    assert '"conversation.show_thinking"' in script


def test_find_app_executable_reads_bundle_info_plist(tmp_path: Path) -> None:
    module = _load_module()
    app = tmp_path / "Lantai.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True)
    (macos / "Lantai").write_text("#!/bin/sh\n", encoding="utf-8")
    resources = contents / "Resources"
    backend = (
        resources
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    backend.parent.mkdir(parents=True)
    (resources / "app.asar").write_bytes(b"test-app-asar")
    backend.write_bytes(b"test-frozen-backend")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleExecutable": "Lantai"}, handle)

    assert module._find_app_executable(app) == macos / "Lantai"


def test_stop_app_is_idempotent_when_process_exits_before_terminate() -> None:
    module = _load_module()

    class ProcessExitedDuringStop:
        def __init__(self) -> None:
            self.poll_count = 0
            self.wait_count = 0

        def poll(self):
            self.poll_count += 1
            return None if self.poll_count == 1 else 0

        def terminate(self) -> None:
            raise ProcessLookupError(3, "No such process")

        def wait(self, timeout: float):
            self.wait_count += 1
            return 0

        def kill(self) -> None:
            raise AssertionError("an already-exited process must not be killed")

    process = ProcessExitedDuringStop()
    module._stop_app(process)

    assert process.wait_count == 1


def test_local_smoke_provider_can_inject_one_network_failure() -> None:
    module = _load_module()
    payload = json.dumps(
        {
            "model": module.SMOKE_MODEL_ID,
            "stream": False,
            "messages": [{"role": "user", "content": "network fault"}],
        }
    ).encode("utf-8")

    with module._LocalOpenAISmokeServer() as server:
        server.fail_next_chat_requests()
        first = request.Request(
            f"{server.base_url}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            request.urlopen(first, timeout=5)
        except error.HTTPError as exc:
            assert exc.code == 503
            assert "network_disconnect" in exc.read().decode("utf-8")
        else:
            raise AssertionError("injected provider failure must return HTTP 503")

        second = request.Request(
            f"{server.base_url}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(second, timeout=5) as response:
            assert response.status == 200

        assert server.request_count == 2
        assert server.request_payloads[-1]["messages"] == [
            {"role": "user", "content": "network fault"}
        ]


def test_packaged_customization_integrity_locks_factory_registry_and_slots(
    monkeypatch,
) -> None:
    module = _load_module()
    responses = {
        "/api/customization/registry": {
            "schema_version": 1,
            "items": [{"id": f"item-{index}"} for index in range(20)],
        },
        "/api/customization/state": {
            "mode": "factory",
            "active_profile_id": None,
        },
        "/api/customization/effective-state": {
            "values": {
                "appearance.palette": "mist",
                "navigation.sidebar.chat.visible": True,
            }
        },
        "/api/customization/slots": [
            {"index": index, "profile_id": None} for index in range(1, 6)
        ],
    }

    def fake_json_request(_method, url, **_kwargs):
        endpoint = next(path for path in responses if url.endswith(path))
        return 200, "", responses[endpoint]

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    summary = module._assert_packaged_customization_integrity(
        {
            "api_base_url": "http://127.0.0.1:43123",
            "desktop_token": "test-token",
        },
        timeout=1,
    )

    assert summary["registry_item_count"] == 20
    assert summary["slot_count"] == 5
    assert summary["mode"] == "factory"


def test_new_desktop_instance_requires_a_changed_nonce() -> None:
    module = _load_module()

    assert (
        module._is_new_desktop_instance(
            {"desktop_instance_nonce": "same-instance"},
            previous_nonce="same-instance",
        )
        is False
    )
    assert (
        module._is_new_desktop_instance(
            {"desktop_instance_nonce": "next-instance"},
            previous_nonce="same-instance",
        )
        is True
    )


def test_main_accepts_headless_app_relaunch_cycle(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    received = {}

    monkeypatch.setattr(module, "_run_smoke", lambda args: received.update(vars(args)))

    assert (
        module.main(
            ["--app", str(tmp_path), "--headless", "--app-relaunch-cycle"]
        )
        == 0
    )
    assert received["headless"] is True
    assert received["app_relaunch_cycle"] is True


def test_termination_signal_handler_restores_handlers_before_unwinding(
    monkeypatch,
) -> None:
    module = _load_module()
    installed = {}
    previous = {
        int(module.signal.SIGHUP): object(),
        int(module.signal.SIGTERM): object(),
    }

    monkeypatch.setattr(
        module.signal,
        "getsignal",
        lambda signal_number: previous[int(signal_number)],
    )
    monkeypatch.setattr(
        module.signal,
        "signal",
        lambda signal_number, handler: installed.__setitem__(
            int(signal_number), handler
        ),
    )

    restore = module._install_termination_signal_handlers()
    handler = installed[int(module.signal.SIGTERM)]

    try:
        handler(int(module.signal.SIGTERM), None)
    except RuntimeError as exc:
        assert "received SIGTERM" in str(exc)
    else:
        raise AssertionError("termination handler must unwind the smoke controller")

    assert installed[int(module.signal.SIGHUP)] is previous[int(module.signal.SIGHUP)]
    assert installed[int(module.signal.SIGTERM)] is previous[int(module.signal.SIGTERM)]

    restore()
    assert installed[int(module.signal.SIGHUP)] is previous[int(module.signal.SIGHUP)]
    assert installed[int(module.signal.SIGTERM)] is previous[int(module.signal.SIGTERM)]


def test_relaunched_chat_control_plane_rechecks_completed_turn_safety(
    monkeypatch,
) -> None:
    module = _load_module()
    calls = []

    def fake_terminal(state, *, chat_id, timeout):
        calls.append(("terminal", state, chat_id, timeout))

    def fake_controls(
        state,
        *,
        chat_id,
        session_id,
        timeout,
        destructive_cleanup=False,
        expected_statuses=None,
    ):
        calls.append(
            (
                "controls",
                state,
                chat_id,
                session_id,
                timeout,
                destructive_cleanup,
                expected_statuses,
            )
        )

    monkeypatch.setattr(module, "_assert_terminal_interrupt_is_noop", fake_terminal)
    monkeypatch.setattr(module, "_assert_canonical_runtime_controls", fake_controls)

    state = {"api_base_url": "http://127.0.0.1:19090"}
    evidence = module._assert_relaunched_chat_control_plane(
        state,
        chat_id="chat-1",
        session_id="session-1",
        timeout=7,
    )

    assert evidence == {
        "terminal_interrupt_noop": True,
        "canonical_runtime_controls": True,
    }
    assert calls == [
        ("terminal", state, "chat-1", 7),
        (
            "controls",
            state,
            "chat-1",
            "session-1",
            7,
            False,
            {"completed", "succeeded"},
        ),
    ]


def test_packaged_lifecycle_evidence_helpers_write_non_secret_json(tmp_path: Path) -> None:
    module = _load_module()
    app = tmp_path / "Lantai.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True)
    (macos / "Lantai").write_text("#!/bin/sh\n", encoding="utf-8")
    resources = contents / "Resources"
    backend = (
        resources
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    backend.parent.mkdir(parents=True)
    (resources / "app.asar").write_bytes(b"test-app-asar")
    backend.write_bytes(b"test-frozen-backend")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "Lantai",
                "CFBundleIdentifier": "com.lantai.app",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleVersion": "1.2.3",
            },
            handle,
        )

    evidence_path = tmp_path / "evidence" / "packaged-smoke.json"
    payload = {
        "schema": module.SMOKE_EVIDENCE_SCHEMA,
        "passed": True,
        "distribution": "official",
        "desktop_token": "must-not-survive",
        "app": module._app_metadata_for_evidence(app),
    }
    payload.pop("desktop_token")
    module._write_json_file(evidence_path, payload)

    raw = evidence_path.read_text(encoding="utf-8")
    saved = module.json.loads(raw)
    assert saved["schema"] == module.SMOKE_EVIDENCE_SCHEMA
    assert saved["app"]["bundle_identifier"] == "com.lantai.app"
    assert set(saved["app"]["artifact_sha256"]) == {
        "main_executable",
        "app_asar",
        "backend_executable",
    }
    assert all(
        value.startswith("sha256:")
        for value in saved["app"]["artifact_sha256"].values()
    )
    assert "must-not-survive" not in raw


def test_backend_evidence_path_accepts_legacy_pre_helper_layout(tmp_path: Path) -> None:
    module = _load_module()
    app = tmp_path / "Legacy.app"
    legacy = app / "Contents" / "Resources" / "lantai-server" / "lantai-server"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-backend")

    assert module._backend_executable_for_evidence(app) == legacy


def test_write_smoke_openai_provider_config_sets_active_custom_model(
    tmp_path: Path,
) -> None:
    module = _load_module()

    info = module._write_smoke_openai_provider_config(
        tmp_path / "user-data",
        base_url="http://127.0.0.1:9999/v1",
    )

    provider_path = Path(info["provider_path"])
    active_model_path = Path(info["active_model_path"])
    provider = json.loads(provider_path.read_text(encoding="utf-8"))
    active_model = json.loads(active_model_path.read_text(encoding="utf-8"))
    assert provider_path == (
        tmp_path
        / "user-data"
        / "working.secret"
        / "providers"
        / "custom"
        / f"{module.SMOKE_MODEL_PROVIDER_ID}.json"
    )
    assert provider["id"] == module.SMOKE_MODEL_PROVIDER_ID
    assert provider["base_url"] == "http://127.0.0.1:9999/v1"
    assert provider["is_custom"] is True
    assert provider["extra_models"][0]["id"] == module.SMOKE_MODEL_ID
    assert active_model == {
        "provider_id": module.SMOKE_MODEL_PROVIDER_ID,
        "model": module.SMOKE_MODEL_ID,
    }


def test_local_openai_smoke_server_streams_thinking_and_text() -> None:
    module = _load_module()

    with module._LocalOpenAISmokeServer() as server:
        req = request.Request(
            f"{server.base_url}/chat/completions",
            data=json.dumps(
                {
                    "model": module.SMOKE_MODEL_ID,
                    "messages": [{"role": "user", "content": "hello smoke"}],
                    "stream": True,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8")

        assert resp.status == 200
        encoded_thinking = json.dumps(
            module.SMOKE_MODEL_THINKING_TEXT,
            ensure_ascii=False,
        )[1:-1]
        assert encoded_thinking in text
        assert module.SMOKE_MODEL_REPLY_PREFIX in text
        assert "data: [DONE]" in text
        assert server.request_count == 1


def test_success_event_gate_preserves_provider_reasoning_and_rejects_fixed_status() -> None:
    module = _load_module()
    base_events = [
        {"type": "turn.started", "status": "running", "payload": {}},
        {"type": "message.started", "status": "running", "payload": {}},
        {
            "type": "message.delta",
            "status": "running",
            "payload": {
                "content_type": "thinking",
                "delta": module.SMOKE_MODEL_THINKING_TEXT,
            },
        },
        {
            "type": "message.completed",
            "status": "completed",
            "payload": {
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": module.SMOKE_MODEL_REPLY_PREFIX,
                        }
                    ]
                }
            },
        },
        {"type": "turn.completed", "status": "completed", "payload": {}},
    ]

    module._assert_success_events({"events": base_events})

    rewritten_events = json.loads(json.dumps(base_events, ensure_ascii=False))
    rewritten_events[2]["payload"]["delta"] = (
        module.SMOKE_FORBIDDEN_FABRICATED_PROGRESS_TEXT
    )
    with pytest.raises(AssertionError, match="did not preserve provider reasoning"):
        module._assert_success_events({"events": rewritten_events})


def test_local_openai_smoke_server_delivers_frames_before_stream_end() -> None:
    module = _load_module()
    module.SMOKE_MODEL_PROVIDER_STREAM_STEP_DELAY_SECONDS = 0.15
    prompt = f"{module.SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX}timing"

    with module._LocalOpenAISmokeServer() as server:
        req = request.Request(
            f"{server.base_url}/chat/completions",
            data=json.dumps(
                {
                    "model": module.SMOKE_MODEL_ID,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": True,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        with request.urlopen(req, timeout=5) as resp:
            first_data_line = b""
            while not first_data_line.startswith(b"data:"):
                first_data_line = resp.readline()
                assert first_data_line
            first_frame_seconds = time.monotonic() - started
            remainder = resp.read()
            total_seconds = time.monotonic() - started

    assert b"chat.completion.chunk" in first_data_line
    assert b"data: [DONE]" in remainder
    minimum_stream_after_first_frame = (
        module.SMOKE_MODEL_PROVIDER_STREAM_STEP_DELAY_SECONDS * 3
    )
    assert first_frame_seconds < total_seconds
    assert total_seconds - first_frame_seconds >= minimum_stream_after_first_frame


def test_smoke_provider_finds_ui_prompt_before_injected_runtime_context() -> None:
    module = _load_module()
    prompt = f"{module.SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX}12ab34cd"

    extracted = module._extract_smoke_prompt_text(
        [
            {"role": "user", "content": prompt},
            {
                "role": "system",
                "content": "<hybrid-memory-context>ranked memories</hybrid-memory-context>",
            },
        ]
    )

    assert extracted == prompt


def test_smoke_provider_uses_latest_user_message_for_queued_turn() -> None:
    module = _load_module()
    prompt = f"{module.SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX}12ab34cd"

    extracted = module._extract_smoke_prompt_text(
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "queued second"},
            {
                "role": "system",
                "content": "<hybrid-memory-context>context</hybrid-memory-context>",
            },
        ]
    )

    assert extracted == "queued second"


def test_chat_basic_ui_send_evidence_keeps_dom_counts() -> None:
    module = _load_module()

    evidence = module._chat_basic_ui_send_evidence(
        {
            "basic_chat_ui_send": {
                "ok": True,
                "chat_id": "basic-chat-1",
                "isolation_chat_id": "basic-chat-empty",
                "max_user_feedback_ms": 3000,
                "max_assistant_feedback_ms": 15000,
                "backend_turn_observed": True,
                "console_chat_post_count": 2,
                "console_chat_post_statuses": [200, 200],
                "experience_matrix": [
                    {"id": "initial_empty_chat_dom", "passed": True},
                    {"id": "backend_pending_cleared", "passed": True},
                ],
                "initial": {
                    "user_bubble_count": 0,
                    "assistant_bubble_count": 0,
                    "body_text_length": 80,
                },
                "initial_backend": {
                    "chat_id": "basic-chat-1",
                    "status": "idle",
                    "pending_user_message": "",
                    "message_count": 0,
                    "user_message_count": 0,
                    "assistant_message_count": 0,
                },
                "first": {
                    "user_visible_ms": 120,
                    "assistant_visible_ms": 450,
                    "user_bubble_count": 1,
                    "assistant_bubble_count": 1,
                    "response_avatar_count": 1,
                    "action_bar_count": 1,
                    "empty_assistant_count": 0,
                    "body_text_length": 200,
                },
                "second": {
                    "user_visible_ms": 130,
                    "assistant_visible_ms": 460,
                    "user_bubble_count": 2,
                    "assistant_bubble_count": 2,
                    "response_avatar_count": 2,
                    "action_bar_count": 2,
                    "empty_assistant_count": 0,
                    "body_text_length": 300,
                },
                "final": {
                    "user_bubble_count": 2,
                    "assistant_bubble_count": 2,
                    "response_avatar_count": 2,
                    "action_bar_count": 2,
                    "empty_assistant_count": 0,
                    "body_text_length": 320,
                },
                "final_stable": {
                    "user_bubble_count": 2,
                    "assistant_bubble_count": 2,
                    "response_avatar_count": 2,
                    "action_bar_count": 2,
                    "empty_assistant_count": 0,
                    "body_text_length": 320,
                },
                "final_backend": {
                    "chat_id": "basic-chat-1",
                    "status": "idle",
                    "pending_user_message": "",
                    "message_count": 4,
                    "user_message_count": 2,
                    "assistant_message_count": 2,
                },
                "isolation": {
                    "user_bubble_count": 0,
                    "assistant_bubble_count": 0,
                    "body_text_length": 100,
                },
                "isolation_backend": {
                    "chat_id": "basic-chat-empty",
                    "status": "idle",
                    "pending_user_message": "",
                    "message_count": 0,
                    "user_message_count": 0,
                    "assistant_message_count": 0,
                },
                "restored": {
                    "user_bubble_count": 2,
                    "assistant_bubble_count": 2,
                    "body_text_length": 320,
                },
                "restored_backend": {
                    "chat_id": "basic-chat-1",
                    "status": "idle",
                    "pending_user_message": "",
                    "message_count": 4,
                    "user_message_count": 2,
                    "assistant_message_count": 2,
                },
            }
        }
    )

    assert evidence == {
        "ok": True,
        "chat_id": "basic-chat-1",
        "isolation_chat_id": "basic-chat-empty",
        "max_user_feedback_ms": 3000,
        "max_assistant_feedback_ms": 15000,
        "backend_turn_observed": True,
        "console_chat_post_count": 2,
        "console_chat_post_statuses": [200, 200],
        "experience_matrix": [
            {"id": "initial_empty_chat_dom", "passed": True},
            {"id": "backend_pending_cleared", "passed": True},
        ],
        "initial": {
            "user_bubble_count": 0,
            "assistant_bubble_count": 0,
            "body_text_length": 80,
        },
        "initial_backend": {
            "chat_id": "basic-chat-1",
            "status": "idle",
            "pending_user_message": "",
            "message_count": 0,
            "user_message_count": 0,
            "assistant_message_count": 0,
        },
        "first": {
            "user_visible_ms": 120,
            "assistant_visible_ms": 450,
            "user_bubble_count": 1,
            "assistant_bubble_count": 1,
            "response_avatar_count": 1,
            "action_bar_count": 1,
            "empty_assistant_count": 0,
            "body_text_length": 200,
        },
        "second": {
            "user_visible_ms": 130,
            "assistant_visible_ms": 460,
            "user_bubble_count": 2,
            "assistant_bubble_count": 2,
            "response_avatar_count": 2,
            "action_bar_count": 2,
            "empty_assistant_count": 0,
            "body_text_length": 300,
        },
        "final": {
            "user_bubble_count": 2,
            "assistant_bubble_count": 2,
            "response_avatar_count": 2,
            "action_bar_count": 2,
            "empty_assistant_count": 0,
            "body_text_length": 320,
        },
        "final_stable": {
            "user_bubble_count": 2,
            "assistant_bubble_count": 2,
            "response_avatar_count": 2,
            "action_bar_count": 2,
            "empty_assistant_count": 0,
            "body_text_length": 320,
        },
        "final_backend": {
            "chat_id": "basic-chat-1",
            "status": "idle",
            "pending_user_message": "",
            "message_count": 4,
            "user_message_count": 2,
            "assistant_message_count": 2,
        },
        "isolation": {
            "user_bubble_count": 0,
            "assistant_bubble_count": 0,
            "body_text_length": 100,
        },
        "isolation_backend": {
            "chat_id": "basic-chat-empty",
            "status": "idle",
            "pending_user_message": "",
            "message_count": 0,
            "user_message_count": 0,
            "assistant_message_count": 0,
        },
        "restored": {
            "user_bubble_count": 2,
            "assistant_bubble_count": 2,
            "body_text_length": 320,
        },
        "restored_backend": {
            "chat_id": "basic-chat-1",
            "status": "idle",
            "pending_user_message": "",
            "message_count": 4,
            "user_message_count": 2,
            "assistant_message_count": 2,
        },
    }


def test_packaged_operator_effect_seed_writes_running_journal(tmp_path: Path) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"

    seeded = module.asyncio.run(
        module._write_packaged_operator_effect_seed(
            workspace,
            run_id="operator-effect-run",
            release_edition="official",
        )
    )

    assert seeded == {
        "attempt_id": "operator-effect-run:attempt-1",
        "effect_id": "operator-effect-run:effect-click-publish",
    }
    assert module._runtime_effect_statuses(
        workspace,
        run_id="operator-effect-run",
        attempt_id=seeded["attempt_id"],
        effect_id=seeded["effect_id"],
    ) == ["intent", "running"]


def test_packaged_operator_effect_recovery_uses_control_plane(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    state = {
        "api_base_url": "http://127.0.0.1:19090",
        "desktop_token": "token-1",
    }
    find_calls = 0
    repair_calls = 0
    request_payloads = []

    def fake_find_runtime_run_by_id(_state, *, run_id, timeout):
        nonlocal find_calls
        find_calls += 1
        base = {
            "id": run_id,
            "attempt_id": f"{run_id}:attempt-1",
            "revision": 2,
            "workspace_dir": str(workspace),
            "source": "browser",
        }
        if find_calls == 1:
            return {
                **base,
                "status": "running",
                "ambiguous_effect": True,
                "recovery": {
                    "effect_replay_forbidden": True,
                    "effects": [
                        {
                            "effect_id": f"{run_id}:effect-click-publish",
                            "status": "running",
                            "external_call_must_not_be_replayed": True,
                            "automatic_external_call_allowed": False,
                        }
                    ],
                },
            }
        return {
            **base,
            "revision": 4,
            "status": "completed",
            "authority_status": "succeeded",
        }

    def fake_json_request(method, url, *, payload, headers, timeout):
        nonlocal repair_calls
        request_payloads.append((method, url, payload, headers))
        assert method == "POST"
        assert headers == {"X-Lantai-Desktop-Token": "token-1"}
        if url.endswith("/resolve"):
            assert payload["resolution"] == "human_resolved"
            assert payload["expected_effect_revision"] == 1
            return 200, "{}", {
                "resolved": True,
                "revision": payload["expected_revision"],
                "effect_status": "human_resolved",
                "effect_revision": 3,
                "applied_actions": ["mark_ambiguous", "human_resolve"],
            }
        if url.endswith("/repair"):
            repair_calls += 1
            if repair_calls == 1:
                assert payload["action"] == "rebuild_checkpoint"
                assert payload["expected_revision"] == 2
                assert payload["expected_effect_revision"] == 3
                return 200, "{}", {
                    "repaired": True,
                    "revision": 3,
                    "checkpoint": {"checkpoint_id": payload["checkpoint_id"], "seq": 42},
                }
            assert payload["action"] == "rewrite_terminal"
            if repair_calls == 2:
                assert payload["expected_revision"] == 3
                return 200, "{}", {
                    "repaired": True,
                    "revision": 4,
                    "status": "succeeded",
                }
            assert payload["expected_revision"] == 4
            return 200, "{}", {
                "repaired": True,
                "idempotent": True,
                "revision": 4,
                "status": "succeeded",
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(module, "_find_runtime_run_by_id", fake_find_runtime_run_by_id)
    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setattr(
        module,
        "_runtime_effect_statuses",
        lambda *args, **kwargs: ["intent", "running", "ambiguous", "human_resolved"],
    )

    evidence = module._assert_packaged_operator_effect_recovery(
        state,
        workspace_dir=workspace,
        expected_distribution="official",
        timeout=5,
    )

    assert evidence["resolve_actions"] == ["mark_ambiguous", "human_resolve"]
    assert evidence["effect_statuses"] == [
        "intent",
        "running",
        "ambiguous",
        "human_resolved",
    ]
    assert evidence["repeated_terminal_idempotent"] is True
    assert evidence["final_status"] == "completed"
    assert evidence["final_authority_status"] == "succeeded"
    assert len(request_payloads) == 4


def test_desktop_readiness_uses_smoke_state_token_and_nonce(monkeypatch) -> None:
    module = _load_module()
    calls = []

    def fake_json_request(method, url, *, headers, timeout):
        calls.append((method, url, headers, timeout))
        return 200, "{}", {
            "status": "ok",
            "desktop": True,
            "instance_nonce": "nonce-1",
            "pid": 4321,
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    pid = module._assert_desktop_readiness(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
            "desktop_instance_nonce": "nonce-1",
        },
        timeout=3,
    )

    assert pid == 4321
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:19090/api/desktop/readiness",
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_ready_state_requires_packaged_matching_distribution() -> None:
    module = _load_module()

    module._assert_ready_state(
        {
            "is_packaged": True,
            "distribution_channel": "official",
            "backend_pid": 4321,
            "desktop_instance_nonce": "nonce-1",
        },
        expected_distribution="official",
    )

    try:
        module._assert_ready_state(
            {
                "is_packaged": False,
                "distribution_channel": "official",
                "backend_pid": 4321,
                "desktop_instance_nonce": "nonce-1",
            },
            expected_distribution="official",
        )
    except AssertionError as exc:
        assert "packaged Electron app" in str(exc)
    else:
        raise AssertionError("Expected non-packaged state to fail")


def test_wait_for_state_fails_fast_when_packaged_app_exits(tmp_path: Path) -> None:
    module = _load_module()

    class ExitedProcess:
        def poll(self):
            return 173

    try:
        module._wait_for_state(
            tmp_path / "missing-state.json",
            timeout=5,
            predicate=lambda payload: bool(payload),
            process=ExitedProcess(),
            expected_distribution="mas",
        )
    except RuntimeError as exc:
        message = str(exc)
        assert "exited before writing lifecycle smoke state" in message
        assert "exit code 173" in message
        assert "App Store distribution-signed .app bundles" in message
    else:
        raise AssertionError("Expected exited packaged app process to fail fast")


def test_console_electron_binary_prefers_direct_dist_binary(tmp_path: Path) -> None:
    module = _load_module()
    binary = (
        tmp_path
        / "console"
        / "node_modules"
        / "electron"
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    framework = (
        binary.parents[1]
        / "Frameworks"
        / "Electron Framework.framework"
        / "Electron Framework"
    )
    binary.parent.mkdir(parents=True)
    framework.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    framework.write_text("framework\n", encoding="utf-8")

    assert module._console_electron_binary(tmp_path) == binary


def test_console_electron_binary_bootstraps_missing_electron_dist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    console_dir = tmp_path / "console"
    electron_pkg = console_dir / "node_modules" / "electron"
    bin_dir = console_dir / "node_modules" / ".bin"
    install_script = electron_pkg / "install.js"
    binary = (
        electron_pkg
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    install_script.parent.mkdir(parents=True)
    install_script.write_text("// install electron\n", encoding="utf-8")
    bin_dir.mkdir(parents=True)
    (bin_dir / "electron").write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        calls.append((command, cwd, text, capture_output, timeout, check))
        binary.parent.mkdir(parents=True)
        (
            binary.parents[1]
            / "Frameworks"
            / "Electron Framework.framework"
        ).mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        (
            binary.parents[1]
            / "Frameworks"
            / "Electron Framework.framework"
            / "Electron Framework"
        ).write_text("framework\n", encoding="utf-8")
        return Result()

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._console_electron_binary(tmp_path) == binary
    assert calls == [
        (
            ["/usr/bin/node", str(install_script)],
            str(console_dir),
            True,
            True,
            120,
            False,
        )
    ]


def test_console_electron_binary_recovers_from_cached_archive(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    console_dir = tmp_path / "console"
    electron_pkg = console_dir / "node_modules" / "electron"
    install_script = electron_pkg / "install.js"
    archive = tmp_path / "electron-cache" / "electron-v42.0.1-darwin-arm64.zip"
    binary = (
        electron_pkg
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    framework = (
        binary.parents[1]
        / "Frameworks"
        / "Electron Framework.framework"
        / "Electron Framework"
    )
    install_script.parent.mkdir(parents=True)
    install_script.write_text("// install electron\n", encoding="utf-8")
    (electron_pkg / "package.json").write_text(
        module.json.dumps({"version": "42.0.1"}),
        encoding="utf-8",
    )
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"cached electron")
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        calls.append((command, cwd, text, capture_output, timeout, check))
        if command[0] == "/usr/bin/ditto":
            binary.parent.mkdir(parents=True)
            framework.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            framework.write_text("framework\n", encoding="utf-8")
        return Result()

    def fake_which(name):
        return { "node": "/usr/bin/node", "ditto": "/usr/bin/ditto" }.get(name)

    monkeypatch.setenv("ELECTRON_CACHE", str(archive.parent))
    monkeypatch.setenv("ELECTRON_INSTALL_PLATFORM", "darwin")
    monkeypatch.setenv("ELECTRON_INSTALL_ARCH", "arm64")
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.shutil, "which", fake_which)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._console_electron_binary(tmp_path) == binary
    assert calls[0][0] == ["/usr/bin/node", str(install_script)]
    assert calls[1][0] == [
        "/usr/bin/ditto",
        "-x",
        "-k",
        str(archive),
        str(electron_pkg / "dist"),
    ]
    assert (electron_pkg / "path.txt").read_text(encoding="utf-8") == (
        "Electron.app/Contents/MacOS/Electron"
    )


def test_distribution_tools_reject_mas_shell_tool(monkeypatch) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, headers, timeout):
        assert method == "GET"
        assert url == "http://127.0.0.1:19090/api/tools"
        assert headers == {"X-Lantai-Desktop-Token": "token-1"}
        return 200, "[]", [
            {"name": "safe_tool"},
            {"name": "execute_shell_command"},
        ]

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    try:
        module._assert_distribution_tools(
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            expected_distribution="mas",
            timeout=3,
        )
    except AssertionError as exc:
        assert "forbidden local execution tools" in str(exc)
        assert "execute_shell_command" in str(exc)
    else:
        raise AssertionError("Expected MAS shell tool exposure to fail")


def test_browser_capabilities_lock_mas_policy(monkeypatch) -> None:
    module = _load_module()
    calls = []

    def fake_json_request(method, url, *, headers, timeout):
        calls.append((method, url, headers, timeout))
        return 200, "{}", {
            "config": {"app_store_distribution": True},
            "capability_matrix": {
                "browser_read_navigation": {
                    "mode": "read_only",
                    "reason_code": "mas_browser_read_only",
                },
                "browser_write": {
                    "mode": "disabled",
                    "reason_code": "mas_browser_write_disabled",
                },
                "background_scheduled_execution": {
                    "mode": "interactive_only",
                    "reason_code": "mas_background_execution_limited",
                },
            },
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    module._assert_browser_capabilities(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        expected_distribution="mas",
        timeout=3,
    )

    assert calls == [
        (
            "GET",
            "http://127.0.0.1:19090/api/browser-workbench/state",
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_browser_capabilities_lock_official_policy(monkeypatch) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, headers, timeout):
        return 200, "{}", {
            "config": {"app_store_distribution": False},
            "capability_matrix": {
                "browser_read_navigation": {
                    "mode": "enabled",
                    "reason_code": "developer_id_allowed",
                },
                "browser_write": {
                    "mode": "interactive_only",
                    "reason_code": "developer_id_browser_write_confirmation_required",
                },
                "background_scheduled_execution": {
                    "mode": "enabled",
                    "reason_code": "developer_id_allowed",
                },
            },
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    module._assert_browser_capabilities(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        expected_distribution="official",
        timeout=3,
    )


def test_browser_capabilities_reject_wrong_distribution(monkeypatch) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, headers, timeout):
        return 200, "{}", {
            "config": {"app_store_distribution": False},
            "capability_matrix": {},
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    try:
        module._assert_browser_capabilities(
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            expected_distribution="mas",
            timeout=3,
        )
    except AssertionError as exc:
        assert "Expected MAS browser config" in str(exc)
    else:
        raise AssertionError("Expected wrong browser distribution to fail")


def test_terminal_interrupt_noop_uses_runtime_control_route(monkeypatch) -> None:
    module = _load_module()
    calls = []

    def fake_json_request(method, url, *, payload, headers, timeout):
        calls.append((method, url, payload, headers, timeout))
        return 200, "{}", {"stopped": False, "run_id": "chat-1", "chat_id": "chat-1"}

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    module._assert_terminal_interrupt_is_noop(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        chat_id="chat-1",
        timeout=3,
    )

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:19090/api/runtime/runs/chat-1/interrupt",
            {},
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_terminal_interrupt_noop_rejects_false_stop(monkeypatch) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, payload, headers, timeout):
        return 200, "{}", {"stopped": True, "run_id": "chat-1", "chat_id": "chat-1"}

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    try:
        module._assert_terminal_interrupt_is_noop(
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            chat_id="chat-1",
            timeout=3,
        )
    except AssertionError as exc:
        assert "should be a no-op" in str(exc)
    else:
        raise AssertionError("Expected false stopped=true interrupt to fail")


def test_http_error_helper_parses_runtime_error_detail(monkeypatch) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, payload, headers, timeout):
        raise error.HTTPError(
            url,
            409,
            "Conflict",
            hdrs={},
            fp=io.BytesIO(
                b'{"detail":{"code":"stale_attempt","run_id":"run-1"}}'
            ),
        )

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    payload = module._json_request_expect_http_error(
        "POST",
        "http://127.0.0.1:19090/api/runtime/runs/run-1/interrupt",
        expected_status=409,
        payload={"expected_revision": 2},
        headers={"X-Lantai-Desktop-Token": "token-1"},
        timeout=3,
    )

    assert payload["detail"]["code"] == "stale_attempt"
    assert payload["detail"]["run_id"] == "run-1"


def test_runtime_event_stream_reconnect_recovers_terminal_from_replay_cursor(
    monkeypatch,
) -> None:
    module = _load_module()
    opened_urls = []
    batch_urls = []

    class FakeStream:
        def __init__(self, text: str):
            self.lines = iter(text.splitlines(keepends=True))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            try:
                return next(self.lines).encode("utf-8")
            except StopIteration:
                return b""

    def fake_urlopen(req, *, timeout):
        opened_urls.append((req.full_url, dict(req.header_items()), timeout))
        if req.full_url.endswith("after_seq=0"):
            return FakeStream(
                'data: {"chat_id":"chat-1","after_seq":0,"latest_seq":4,'
                '"next_after_seq":4,"events":['
                '{"id":"evt-running","seq":3,"type":"turn.started",'
                '"status":"running","payload":{}},'
                '{"id":"evt-terminal","seq":4,"type":"turn.failed",'
                '"status":"failed","payload":{"final_task_status":"failed"}}'
                "]}\n\n"
            )
        if req.full_url.endswith("after_seq=3"):
            return FakeStream(
                'data: {"chat_id":"chat-1","after_seq":3,"latest_seq":4,'
                '"next_after_seq":4,"events":['
                '{"id":"evt-terminal","seq":4,"type":"turn.failed",'
                '"status":"failed","payload":{"final_task_status":"failed"}}'
                "]}\n\n"
            )
        raise AssertionError(f"Unexpected stream URL: {req.full_url}")

    def fake_json_request(method, url, *, headers, timeout):
        batch_urls.append((method, url, headers, timeout))
        return 200, "{}", {"chat_id": "chat-1", "after_seq": 4, "latest_seq": 4, "events": []}

    monkeypatch.setattr(module.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(module, "_json_request", fake_json_request)

    module._assert_runtime_event_stream_reconnect_recovers_terminal(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        chat_id="chat-1",
        timeout=3,
    )

    assert [url for url, _headers, _timeout in opened_urls] == [
        "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=0",
        "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=3",
    ]
    assert batch_urls == [
        (
            "GET",
            "http://127.0.0.1:19090/api/chats/chat-1/events?after_seq=4",
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_runtime_event_stream_reconnect_waits_for_terminal_after_live_cursor(
    monkeypatch,
) -> None:
    module = _load_module()
    opened_urls = []

    class FakeStream:
        def __init__(self, text: str):
            self.lines = iter(text.splitlines(keepends=True))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            try:
                return next(self.lines).encode("utf-8")
            except StopIteration:
                return b""

    def fake_urlopen(req, *, timeout):
        opened_urls.append(req.full_url)
        if req.full_url.endswith("after_seq=0"):
            return FakeStream(
                'data: {"chat_id":"chat-1","after_seq":0,"latest_seq":4,'
                '"next_after_seq":4,"events":['
                '{"id":"evt-running","seq":4,"type":"turn.started",'
                '"status":"running","payload":{}}'
                "]}\n\n"
            )
        if req.full_url.endswith("after_seq=4"):
            return FakeStream(
                'data: {"chat_id":"chat-1","after_seq":4,"latest_seq":5,'
                '"next_after_seq":5,"events":['
                '{"id":"evt-terminal","seq":5,"type":"turn.failed",'
                '"status":"failed","payload":{"final_task_status":"failed"}}'
                "]}\n\n"
            )
        raise AssertionError(f"Unexpected stream URL: {req.full_url}")

    monkeypatch.setattr(module.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        module,
        "_json_request",
        lambda method, url, *, headers, timeout: (
            200,
            "{}",
            {"chat_id": "chat-1", "after_seq": 5, "latest_seq": 5, "events": []},
        ),
    )

    module._assert_runtime_event_stream_reconnect_recovers_terminal(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        chat_id="chat-1",
        timeout=3,
    )

    assert opened_urls == [
        "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=0",
        "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=4",
    ]


def test_runtime_event_stream_reconnect_rejects_stale_replay(monkeypatch) -> None:
    module = _load_module()

    class FakeStream:
        def __init__(self, text: str):
            self.lines = iter(text.splitlines(keepends=True))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            try:
                return next(self.lines).encode("utf-8")
            except StopIteration:
                return b""

    def fake_urlopen(req, *, timeout):
        if req.full_url.endswith("after_seq=0"):
            return FakeStream(
                'data: {"chat_id":"chat-1","after_seq":0,"latest_seq":4,'
                '"next_after_seq":4,"events":['
                '{"id":"evt-running","seq":3,"type":"turn.started",'
                '"status":"running","payload":{}},'
                '{"id":"evt-terminal","seq":4,"type":"turn.failed",'
                '"status":"failed","payload":{"final_task_status":"failed"}}'
                "]}\n\n"
            )
        if req.full_url.endswith("after_seq=3"):
            return FakeStream(
                'data: {"chat_id":"chat-1","after_seq":3,"latest_seq":4,'
                '"next_after_seq":4,"events":['
                '{"id":"evt-stale","seq":3,"type":"turn.started",'
                '"status":"running","payload":{}},'
                '{"id":"evt-terminal","seq":4,"type":"turn.failed",'
                '"status":"failed","payload":{"final_task_status":"failed"}}'
                "]}\n\n"
            )
        raise AssertionError(f"Unexpected stream URL: {req.full_url}")

    monkeypatch.setattr(module.request, "urlopen", fake_urlopen)

    try:
        module._assert_runtime_event_stream_reconnect_recovers_terminal(
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            chat_id="chat-1",
            timeout=3,
        )
    except AssertionError as exc:
        assert "replayed stale events after reconnect" in str(exc)
    else:
        raise AssertionError("Expected stale runtime event replay to fail")


def test_runtime_run_summary_finds_failed_chat_run(monkeypatch) -> None:
    module = _load_module()

    def fake_json_request(method, url, *, headers, timeout):
        assert method == "GET"
        assert url == "http://127.0.0.1:19090/api/runtime/runs"
        return 200, "{}", {
            "items": [
                {
                    "id": "run-1",
                    "source": "chat",
                    "status": "failed",
                    "attempt_id": "run-1:attempt-1",
                    "revision": 5,
                    "chat_id": "chat-1",
                    "session_id": "session-1",
                    "workspace_dir": "/tmp/workspace",
                    "recovery": {"can_interrupt": False},
                }
            ]
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    summary = module._find_runtime_run_summary(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        chat_id="chat-1",
        session_id="session-1",
        timeout=3,
    )

    assert summary["id"] == "run-1"
    assert summary["attempt_id"] == "run-1:attempt-1"
    assert summary["revision"] == 5


def test_packaged_reminder_delivery_worker_helper_requires_settled_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    smoke_file = tmp_path / "reminder-delivery.jsonl"
    smoke_file.write_text(
        "\n".join(
            [
                module.json.dumps(
                    {
                        "kind": "reminder_delivery",
                        "reminder_id": "reminder-1",
                        "run_id": "reminder:reminder-1:run-1",
                    }
                ),
                module.json.dumps(
                    {
                        "kind": "reminder_delivery_tick",
                        "claimed_count": 1,
                        "delivered_count": 1,
                        "deduped_count": 0,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_json_request(method, url, *, payload=None, headers, timeout):
        calls.append((method, url, payload, headers, timeout))
        assert headers == {"X-Lantai-Desktop-Token": "token-1"}
        if method == "POST" and url.endswith("/api/reminders"):
            assert payload["title"] == "Packaged reminder delivery smoke"
            assert payload["priority"] == "high"
            return 200, "{}", {"id": "reminder-1"}
        if method == "GET" and url.endswith("/api/runtime/runs"):
            return 200, "{}", {
                "items": [
                    {
                        "id": "reminder:reminder-1:run-1",
                        "source": "reminder",
                        "status": "completed",
                        "authority_status": "succeeded",
                        "revision": 3,
                    }
                ]
            }
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    summary = module._assert_packaged_reminder_delivery_worker(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        smoke_file=smoke_file,
        timeout=3,
    )

    assert summary == {
        "reminder_id": "reminder-1",
        "run_id": "reminder:reminder-1:run-1",
        "tick_count": 1,
        "delivery_count": 1,
        "revision": 3,
    }
    assert calls[0][0] == "POST"


def test_legacy_writer_observation_summary_helper(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    from lantai.app.runtime_legacy_writers import (
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        LEGACY_RUNTIME_WRITER_OBSERVATION_SUMMARY_SCHEMA,
        assert_legacy_runtime_writer_allowed,
    )

    observation_file = tmp_path / "legacy-writers.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        str(observation_file),
    )
    assert_legacy_runtime_writer_allowed(
        "legacy_runtime_event_ledger_writer",
        path=tmp_path / ".lantai" / "experience" / "events.jsonl",
    )

    summary = module._legacy_writer_observation_summary(
        observation_file,
        release_edition="official",
    )

    assert summary["schema"] == LEGACY_RUNTIME_WRITER_OBSERVATION_SUMMARY_SCHEMA
    assert summary["release_edition"] == "official"
    assert summary["event_count"] == 1
    assert summary["fallback_write_count"] == 1
    assert summary["counts_by_writer"]["legacy_runtime_event_ledger_writer"] == 1


def test_legacy_boundary_observation_summary_helper_accepts_native_construction(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    from lantai.app.runtime_legacy_execution_observations import (
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        record_legacy_runtime_execution_observation,
    )

    execution_file = tmp_path / "legacy-execution.jsonl"
    writer_file = tmp_path / "legacy-writers.jsonl"
    writer_file.touch()
    monkeypatch.setenv(
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        str(execution_file),
    )
    assert record_legacy_runtime_execution_observation(
        kind="legacy_agent_constructed",
        surface="LegacyLantaiAgent.__init__",
        engine="native",
        entrypoint="runner.standard_turn",
        channel="console",
    )

    summary = module._legacy_boundary_observation_summary(
        execution_file,
        writer_file,
        release_edition="official",
    )

    assert summary["construction_only"] is True
    assert summary["zero_forbidden_legacy_activity"] is True


def test_legacy_boundary_observation_summary_helper_rejects_loop_invocation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    from lantai.app.runtime_legacy_execution_observations import (
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        record_legacy_runtime_execution_observation,
    )

    execution_file = tmp_path / "legacy-execution.jsonl"
    writer_file = tmp_path / "legacy-writers.jsonl"
    writer_file.touch()
    monkeypatch.setenv(
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        str(execution_file),
    )
    assert record_legacy_runtime_execution_observation(
        kind="legacy_loop_invoked",
        surface="LegacyAgentLoop.reply",
        engine="legacy",
        entrypoint="cli.task",
        channel="console",
    )

    with pytest.raises(AssertionError, match="Forbidden legacy Runtime activity"):
        module._legacy_boundary_observation_summary(
            execution_file,
            writer_file,
            release_edition="official",
        )


def test_packaged_reminder_delivery_restart_helper_requires_new_tick_no_duplicate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    smoke_file = tmp_path / "reminder-delivery.jsonl"
    smoke_file.write_text(
        "\n".join(
            [
                module.json.dumps(
                    {
                        "kind": "reminder_delivery",
                        "reminder_id": "reminder-1",
                        "run_id": "reminder:reminder-1:run-1",
                    }
                ),
                module.json.dumps({"kind": "reminder_delivery_tick"}),
                module.json.dumps(
                    {
                        "kind": "reminder_delivery_tick",
                        "deduped_count": 1,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    def fake_json_request(method, url, *, headers, timeout):
        assert method == "GET"
        assert url == "http://127.0.0.1:19090/api/runtime/runs"
        return 200, "{}", {
            "items": [
                {
                    "id": "reminder:reminder-1:run-1",
                    "source": "reminder",
                    "status": "completed",
                    "authority_status": "succeeded",
                    "revision": 3,
                }
            ]
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    summary = module._assert_packaged_reminder_delivery_restart_deduped(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        smoke_file=smoke_file,
        previous={
            "reminder_id": "reminder-1",
            "run_id": "reminder:reminder-1:run-1",
            "tick_count": 1,
            "delivery_count": 1,
        },
        timeout=3,
    )

    assert summary == {
        "reminder_id": "reminder-1",
        "run_id": "reminder:reminder-1:run-1",
        "tick_count": 2,
        "delivery_count": 1,
        "revision": 3,
    }


def test_packaged_restart_decision_refresh_checks_stalled_and_healthy_runs(
    monkeypatch,
) -> None:
    module = _load_module()
    calls = []
    run_ids = {
        "expired_lease": "run-expired-lease",
        "expired_deadline": "run-expired-deadline",
        "healthy": "run-healthy",
    }

    def fake_runtime_run_items(state, *, timeout):
        calls.append((state, timeout))
        return [
            {
                "id": "run-expired-lease",
                "status": "stalled",
                "revision": 3,
                "summary": "Loop lease expired.",
            },
            {
                "id": "run-expired-deadline",
                "status": "stalled",
                "authority_status": "paused",
                "revision": 3,
                "summary": "paused",
                "recovery": {"reason_code": None},
            },
            {
                "id": "run-healthy",
                "status": "running",
                "revision": 2,
                "summary": "running",
            },
        ]

    monkeypatch.setattr(module, "_runtime_run_items", fake_runtime_run_items)

    module._assert_packaged_restart_decision_summaries(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        run_ids=run_ids,
        timeout=3,
    )

    assert calls == [
        (
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            3,
        )
    ]


def test_packaged_restart_decision_refresh_rejects_missing_reason(monkeypatch) -> None:
    module = _load_module()
    run_ids = {
        "expired_lease": "run-expired-lease",
        "expired_deadline": "run-expired-deadline",
        "healthy": "run-healthy",
    }

    monkeypatch.setattr(
        module,
        "_runtime_run_items",
        lambda state, *, timeout: [
            {
                "id": "run-expired-lease",
                "status": "stalled",
                "revision": 3,
                "summary": "unexpected",
            },
            {
                "id": "run-expired-deadline",
                "status": "stalled",
                "revision": 3,
                "summary": "Loop watchdog deadline expired.",
            },
            {
                "id": "run-healthy",
                "status": "running",
                "revision": 2,
            },
        ],
    )

    try:
        module._assert_packaged_restart_decision_summaries(
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            run_ids=run_ids,
            timeout=3,
        )
    except AssertionError as exc:
        assert "Expired lease run reason mismatch" in str(exc)
    else:
        raise AssertionError("Expected missing restart-decision reason to fail")


def test_packaged_restart_decision_refresh_rejects_terminal_deadline(
    monkeypatch,
) -> None:
    module = _load_module()
    run_ids = {
        "expired_lease": "run-expired-lease",
        "expired_deadline": "run-expired-deadline",
        "healthy": "run-healthy",
    }

    monkeypatch.setattr(
        module,
        "_runtime_run_items",
        lambda state, *, timeout: [
            {
                "id": "run-expired-lease",
                "status": "stalled",
                "revision": 3,
                "summary": "Loop lease expired.",
            },
            {
                "id": "run-expired-deadline",
                "status": "stalled",
                "authority_status": "stalled",
                "revision": 3,
                "summary": "Loop watchdog deadline expired.",
            },
            {
                "id": "run-healthy",
                "status": "running",
                "revision": 2,
            },
        ],
    )

    try:
        module._assert_packaged_restart_decision_summaries(
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            run_ids=run_ids,
            timeout=3,
        )
    except AssertionError as exc:
        assert "did not preserve paused Runtime authority" in str(exc)
    else:
        raise AssertionError("Expected terminal deadline settlement to fail")


def test_runtime_summary_error_code_reads_recovery_reason_code() -> None:
    module = _load_module()

    assert (
        module._runtime_summary_error_code(
            {
                "summary": "Loop no-progress budget exhausted.",
                "recovery": {"reason_code": "loop_no_progress_exhausted"},
            }
        )
        == "loop_no_progress_exhausted"
    )


def test_canonical_runtime_interrupt_uses_run_id_and_fence(monkeypatch) -> None:
    module = _load_module()
    calls = []
    summary = {
        "id": "run-1",
        "attempt_id": "run-1:attempt-1",
        "revision": 5,
        "workspace_dir": "/tmp/workspace",
    }

    def fake_json_request(method, url, *, payload, headers, timeout):
        calls.append((method, url, payload, headers, timeout))
        return 200, "{}", {
            "stopped": False,
            "run_id": "run-1",
            "chat_id": "chat-1",
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    module._assert_canonical_runtime_interrupt_is_noop(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        summary=summary,
        chat_id="chat-1",
        session_id="session-1",
        timeout=3,
    )

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:19090/api/runtime/runs/run-1/interrupt",
            {
                "expected_attempt_id": "run-1:attempt-1",
                "expected_revision": 5,
                "chat_id": "chat-1",
                "session_id": "session-1",
                "workspace_dir": "/tmp/workspace",
            },
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_runtime_stale_fence_uses_current_run_identity(monkeypatch) -> None:
    module = _load_module()
    calls = []
    summary = {
        "id": "run-1",
        "attempt_id": "run-1:attempt-1",
        "revision": 5,
        "workspace_dir": "/tmp/workspace",
    }

    def fake_http_error(method, url, *, expected_status, payload, headers, timeout):
        calls.append((method, url, expected_status, payload, headers, timeout))
        return {
            "detail": {
                "code": "stale_attempt",
                "run_id": "run-1",
                "current_attempt_id": "run-1:attempt-1",
                "current_revision": 5,
            }
        }

    monkeypatch.setattr(module, "_json_request_expect_http_error", fake_http_error)

    module._assert_runtime_stale_fence_rejected(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        summary=summary,
        chat_id="chat-1",
        session_id="session-1",
        timeout=3,
    )

    assert calls[0][0:3] == (
        "POST",
        "http://127.0.0.1:19090/api/runtime/runs/run-1/interrupt",
        409,
    )
    assert calls[0][3]["expected_revision"] == 1004


def test_terminal_budget_update_rejected_uses_fenced_budget_payload(monkeypatch) -> None:
    module = _load_module()
    calls = []
    summary = {
        "id": "run-1",
        "attempt_id": "run-1:attempt-1",
        "revision": 5,
        "workspace_dir": "/tmp/workspace",
    }

    def fake_http_error(method, url, *, expected_status, payload, headers, timeout):
        calls.append((method, url, expected_status, payload, headers, timeout))
        return {
            "detail": {
                "code": "runtime_transition_rejected",
                "run_id": "run-1",
                "action": "update_budget",
                "current_status": "failed",
            }
        }

    monkeypatch.setattr(module, "_json_request_expect_http_error", fake_http_error)

    module._assert_terminal_budget_update_rejected(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        summary=summary,
        chat_id="chat-1",
        session_id="session-1",
        timeout=3,
    )

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:19090/api/runtime/runs/run-1/budget",
            409,
            {
                "expected_attempt_id": "run-1:attempt-1",
                "expected_revision": 5,
                "chat_id": "chat-1",
                "session_id": "session-1",
                "workspace_dir": "/tmp/workspace",
                "budget": {"max_steps": 9},
                "changed_by": "user",
                "reason": "packaged smoke terminal budget rejection",
            },
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_runtime_archive_and_delete_cleanup_checks_reports(monkeypatch) -> None:
    module = _load_module()
    success_calls = []
    error_calls = []
    summary = {
        "id": "run-1",
        "attempt_id": "run-1:attempt-1",
        "revision": 5,
        "workspace_dir": "/tmp/workspace",
    }

    def fake_json_request(method, url, *, payload, headers, timeout):
        success_calls.append((method, url, payload, headers, timeout))
        if method == "POST" and url.endswith("/archive"):
            return 200, "{}", {
                "archived": True,
                "run_id": "run-1",
                "status": "failed",
                "runtime_archive": {
                    "kind": "runtime_run_archive",
                    "active_contract_retained": True,
                },
            }
        if method == "DELETE":
            return 200, "{}", {
                "deleted": True,
                "delete_artifacts": False,
                "runtime_delete": {
                    "kind": "runtime_run_delete",
                    "active_contract_deleted": True,
                },
            }
        raise AssertionError(f"Unexpected success request: {method} {url}")

    def fake_http_error(method, url, *, expected_status, payload, headers, timeout):
        error_calls.append((method, url, expected_status, payload, headers, timeout))
        if method == "DELETE":
            return {"detail": {"code": "runtime_run_delete_approval_required"}}
        return {
            "detail": {
                "code": "stale_attempt",
                "run_id": "run-1",
                "current_attempt_id": None,
            }
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setattr(module, "_json_request_expect_http_error", fake_http_error)

    module._assert_runtime_archive_and_delete_cleanup(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        summary=summary,
        chat_id="chat-1",
        session_id="session-1",
        timeout=3,
    )

    assert [call[0:2] for call in success_calls] == [
        ("POST", "http://127.0.0.1:19090/api/runtime/runs/run-1/archive"),
        ("DELETE", "http://127.0.0.1:19090/api/runtime/runs/run-1"),
    ]
    assert [call[0:3] for call in error_calls] == [
        ("DELETE", "http://127.0.0.1:19090/api/runtime/runs/run-1", 403),
        ("POST", "http://127.0.0.1:19090/api/runtime/runs/run-1/interrupt", 409),
    ]


def test_loop_trial_export_package_uses_fenced_approval(monkeypatch) -> None:
    module = _load_module()
    calls = []
    summary = {
        "id": "loop-run-1",
        "attempt_id": "loop-run-1:attempt-1",
        "revision": 6,
        "workspace_dir": "/tmp/workspace",
    }

    def fake_json_request(method, url, *, payload, headers, timeout):
        calls.append((method, url, payload, headers, timeout))
        return 200, "{}", {
            "exported": True,
            "package": {
                "contract": {"contract_id": "loop-trial-contract"},
                "runtime_run": {"run_id": "loop-run-1"},
                "latest_evaluation": {"report_id": "loop-run-1-eval-2"},
            },
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    module._assert_loop_trial_export_package(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        summary=summary,
        timeout=3,
    )

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:19090/api/runtime/runs/loop-run-1/export",
            {
                "expected_attempt_id": "loop-run-1:attempt-1",
                "expected_revision": 6,
                "workspace_dir": "/tmp/workspace",
                "approved": True,
                "reason": "packaged LOOP-05 recovery export smoke",
                "exported_by": "packaged-lifecycle-smoke",
                "version_info": {"smoke": "packaged-loop-trial"},
            },
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_loop_trial_export_package_accepts_browser_content_contract(
    monkeypatch,
) -> None:
    module = _load_module()
    calls = []
    summary = {
        "id": "browser-content-run-1",
        "attempt_id": "browser-content-run-1:attempt-1",
        "revision": 4,
        "workspace_dir": "/tmp/workspace",
    }

    def fake_json_request(method, url, *, payload, headers, timeout):
        calls.append((method, url, payload, headers, timeout))
        return 200, "{}", {
            "exported": True,
            "package": {
                "contract": {
                    "contract_id": "loop-browser-content-draft-preview-contract",
                },
                "runtime_run": {"run_id": "browser-content-run-1"},
                "latest_evaluation": {
                    "report_id": (
                        "browser-content-run-1-eval-browser-content-1-approved"
                    ),
                },
            },
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    module._assert_loop_trial_export_package(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        summary=summary,
        timeout=3,
        expected_contract_id="loop-browser-content-draft-preview-contract",
        expected_report_id="browser-content-run-1-eval-browser-content-1-approved",
        reason="packaged Browser content handoff smoke",
        version_smoke="packaged-browser-content-trial",
    )

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:19090/api/runtime/runs/browser-content-run-1/export",
            {
                "expected_attempt_id": "browser-content-run-1:attempt-1",
                "expected_revision": 4,
                "workspace_dir": "/tmp/workspace",
                "approved": True,
                "reason": "packaged Browser content handoff smoke",
                "exported_by": "packaged-lifecycle-smoke",
                "version_info": {"smoke": "packaged-browser-content-trial"},
            },
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_packaged_loop_trial_recovery_runs_two_phases_and_exports(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module()
    phase_calls = []
    export_calls = []
    state_by_run: dict[str, dict[str, object]] = {}

    def fake_run_once(workspace_dir, *, run_id, max_iterations, release_edition):
        phase_calls.append((workspace_dir, run_id, max_iterations, release_edition))
        if max_iterations == 1:
            state_by_run[run_id] = {
                "id": run_id,
                "source": "capability",
                "status": "running",
                "attempt_id": f"{run_id}:attempt-1",
                "revision": 3,
                "workspace_dir": str(tmp_path),
            }
            return {
                "run_id": run_id,
                "attempt_id": f"{run_id}:attempt-1",
                "status": "running",
                "revision": 3,
                "iterations_executed": 1,
                "error_code": None,
                "progress": {"consecutive_no_progress_iterations": 1},
            }
        state_by_run[run_id] = {
            **state_by_run[run_id],
            "status": "stalled",
            "revision": 5,
            "debug_refs": {"error_code": "loop_no_progress_exhausted"},
        }
        return {
            "run_id": run_id,
            "attempt_id": f"{run_id}:attempt-1",
            "status": "stalled",
            "revision": 5,
            "iterations_executed": 1,
            "error_code": "loop_no_progress_exhausted",
            "progress": {"consecutive_no_progress_iterations": 2},
        }

    monkeypatch.setattr(module, "_run_packaged_loop_trial_once", fake_run_once)
    monkeypatch.setattr(
        module,
        "_runtime_run_items",
        lambda state, *, timeout: list(state_by_run.values()),
    )
    monkeypatch.setattr(
        module,
        "_assert_loop_trial_export_package",
        lambda state, *, summary, timeout: export_calls.append((state, summary, timeout)),
    )

    run_id = module._assert_packaged_loop_trial_recovery(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        workspace_dir=tmp_path,
        expected_distribution="mas",
        timeout=3,
    )

    assert run_id.startswith("packaged-loop-trial-")
    assert phase_calls == [
        (tmp_path, run_id, 1, "mas"),
        (tmp_path, run_id, 10, "mas"),
    ]
    assert export_calls == [
        (
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            state_by_run[run_id],
            3,
        )
    ]


def test_packaged_browser_content_trial_handoff_preserves_attempt_and_exports(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module()
    phase_calls = []
    export_calls = []
    state_by_run: dict[str, dict[str, object]] = {}

    def fake_run_once(
        workspace_dir,
        *,
        run_id,
        human_gate_decision,
        release_edition,
    ):
        phase_calls.append(
            (workspace_dir, run_id, human_gate_decision, release_edition)
        )
        if human_gate_decision == "pending":
            state_by_run[run_id] = {
                "id": run_id,
                "source": "capability",
                "status": "running",
                "authority_status": "waiting_approval",
                "pending_approval": True,
                "current_step": {
                    "step_id": "browser-content-step-1",
                    "index": 0,
                    "kind": "approval",
                    "title": "Browser content draft/preview human gate",
                },
                "attempt_id": f"{run_id}:attempt-1",
                "revision": 3,
                "workspace_dir": str(tmp_path),
            }
            return {
                "run_id": run_id,
                "attempt_id": f"{run_id}:attempt-1",
                "status": "waiting_approval",
                "revision": 3,
                "iterations_executed": 1,
                "error_code": None,
                "evidence_hash": "sha256:browser-content",
                "issues": [],
                "progress": {"consecutive_no_progress_iterations": 0},
            }
        state_by_run[run_id] = {
            **state_by_run[run_id],
            "status": "completed",
            "authority_status": "succeeded",
            "pending_approval": False,
            "current_step": {
                "step_id": "browser-content-step-1",
                "index": 0,
                "kind": "evaluation",
                "title": "Browser content draft/preview human gate",
            },
            "revision": 5,
        }
        return {
            "run_id": run_id,
            "attempt_id": f"{run_id}:attempt-1",
            "status": "succeeded",
            "revision": 5,
            "iterations_executed": 1,
            "error_code": None,
            "evidence_hash": "sha256:browser-content",
            "issues": [],
            "progress": {"consecutive_no_progress_iterations": 0},
        }

    def fake_export(state, *, summary, timeout, **kwargs):
        export_calls.append((state, summary, timeout, kwargs))

    monkeypatch.setattr(
        module,
        "_run_packaged_browser_content_trial_once",
        fake_run_once,
    )
    monkeypatch.setattr(
        module,
        "_runtime_run_items",
        lambda state, *, timeout: list(state_by_run.values()),
    )
    monkeypatch.setattr(module, "_assert_loop_trial_export_package", fake_export)

    run_id = module._assert_packaged_browser_content_trial_handoff(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        workspace_dir=tmp_path,
        expected_distribution="mas",
        timeout=3,
    )

    assert run_id.startswith("packaged-browser-content-")
    assert phase_calls == [
        (tmp_path, run_id, "pending", "mas"),
        (tmp_path, run_id, "approved", "mas"),
    ]
    assert export_calls == [
        (
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            state_by_run[run_id],
            3,
            {
                "expected_contract_id": (
                    "loop-browser-content-draft-preview-contract"
                ),
                "expected_report_id": (
                    f"{run_id}-eval-browser-content-1-approved"
                ),
                "reason": (
                    "packaged LOOP-05 Browser content handoff export smoke"
                ),
                "version_smoke": "packaged-browser-content-trial",
            },
        )
    ]


def test_browser_preflight_failure_settles_runtime_run(monkeypatch, tmp_path) -> None:
    module = _load_module()
    success_calls = []
    error_calls = []

    def fake_json_request(method, url, *, payload=None, headers, timeout):
        success_calls.append((method, url, payload, headers, timeout))
        if method == "POST" and url.endswith("/api/browser-workbench/tasks"):
            return 200, "{}", {
                "run_id": payload["run_id"],
                "status": "failed",
                "error_code": "browser_profile_agent_control_disabled",
            }
        if method == "GET" and url.endswith("/api/runtime/runs"):
            run_id = success_calls[0][2]["run_id"]
            return 200, "{}", {
                "items": [
                    {
                        "id": run_id,
                        "source": "browser",
                        "status": "failed",
                        "attempt_id": f"{run_id}:browser-attempt",
                        "revision": 3,
                        "workspace_dir": str(tmp_path),
                    }
                ]
            }
        raise AssertionError(f"Unexpected request: {method} {url}")

    def fake_http_error(method, url, *, expected_status, payload, headers, timeout):
        error_calls.append((method, url, expected_status, payload, headers, timeout))
        return {
            "detail": {
                "code": "runtime_transition_rejected",
                "run_id": payload["expected_attempt_id"].split(":")[0],
                "action": "pause",
                "current_status": "failed",
            }
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setattr(module, "_json_request_expect_http_error", fake_http_error)

    run_id = module._assert_browser_preflight_failure_settles_runtime_run(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        workspace_dir=tmp_path,
        timeout=3,
    )

    assert run_id.startswith("browser-packaged-smoke-")
    assert success_calls[0][1] == "http://127.0.0.1:19090/api/browser-workbench/tasks"
    assert success_calls[0][2]["metadata"]["workspace_dir"] == str(tmp_path)
    assert error_calls == [
        (
            "POST",
            f"http://127.0.0.1:19090/api/runtime/runs/{run_id}/pause",
            409,
            {
                "expected_attempt_id": f"{run_id}:browser-attempt",
                "expected_revision": 3,
                "workspace_dir": str(tmp_path),
            },
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        )
    ]


def test_browser_positive_pause_resume_uses_runtime_fences(monkeypatch, tmp_path) -> None:
    module = _load_module()
    success_calls = []
    stale_calls = []
    state_by_run: dict[str, dict[str, object]] = {}

    def fake_json_request(method, url, *, payload=None, headers, timeout):
        success_calls.append((method, url, payload, headers, timeout))
        if method == "POST" and url.endswith("/api/browser-workbench/profiles"):
            return 200, "{}", {"id": payload["id"]}
        if method == "POST" and url.endswith("/api/browser-workbench/tasks"):
            run_id = payload["run_id"]
            state_by_run[run_id] = {
                "id": run_id,
                "source": "browser",
                "status": "running",
                "authority_status": "running",
                "attempt_id": f"{run_id}:browser-attempt",
                "revision": 2,
                "workspace_dir": str(tmp_path),
                "recovery": {"can_pause": True, "can_resume": False},
            }
            return 200, "{}", {"run_id": run_id, "status": "queued"}
        if method == "GET" and url.endswith("/api/runtime/runs"):
            return 200, "{}", {"items": list(state_by_run.values())}
        if method == "POST" and url.endswith("/pause"):
            run_id = url.rsplit("/", 2)[-2]
            state_by_run[run_id] = {
                **state_by_run[run_id],
                "status": "stalled",
                "authority_status": "paused",
                "revision": 3,
                "recovery": {"can_pause": False, "can_resume": True},
            }
            return 200, "{}", {
                "run_id": run_id,
                "paused": True,
                "resumed": False,
                "revision": 3,
            }
        if method == "POST" and url.endswith("/resume"):
            run_id = url.rsplit("/", 2)[-2]
            state_by_run[run_id] = {
                **state_by_run[run_id],
                "status": "running",
                "authority_status": "running",
                "revision": 4,
                "recovery": {"can_pause": True, "can_resume": False},
            }
            return 200, "{}", {
                "run_id": run_id,
                "paused": False,
                "resumed": True,
                "revision": 4,
            }
        if method == "POST" and url.endswith("/cancel"):
            return 200, "{}", {"status": "cancelled"}
        raise AssertionError(f"Unexpected request: {method} {url}")

    def fake_json_request_expect_http_error(
        method,
        url,
        *,
        expected_status,
        payload=None,
        headers,
        timeout,
    ):
        stale_calls.append((method, url, expected_status, payload, headers, timeout))
        run_id, action = url.rsplit("/", 2)[-2:]
        current = state_by_run[run_id]
        assert expected_status == 409
        assert action in {"pause", "resume"}
        assert payload["expected_revision"] != current["revision"]
        return {
            "detail": {
                "code": "stale_attempt",
                "expected_attempt_id": payload["expected_attempt_id"],
                "expected_revision": payload["expected_revision"],
                "current_attempt_id": current["attempt_id"],
                "current_revision": current["revision"],
            },
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setattr(
        module,
        "_json_request_expect_http_error",
        fake_json_request_expect_http_error,
    )

    run_id = module._assert_browser_positive_pause_resume(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        workspace_dir=tmp_path,
        timeout=3,
    )

    assert run_id.startswith("browser-positive-smoke-")
    pause_call = next(call for call in success_calls if call[1].endswith("/pause"))
    resume_call = next(call for call in success_calls if call[1].endswith("/resume"))
    cancel_call = next(call for call in success_calls if call[1].endswith("/cancel"))
    assert pause_call[2] == {
        "expected_attempt_id": f"{run_id}:browser-attempt",
        "expected_revision": 2,
        "workspace_dir": str(tmp_path),
    }
    assert resume_call[2] == {
        "expected_attempt_id": f"{run_id}:browser-attempt",
        "expected_revision": 3,
        "workspace_dir": str(tmp_path),
    }
    assert cancel_call[0] == "POST"
    assert [(call[1].rsplit("/", 1)[-1], call[3]["expected_revision"]) for call in stale_calls] == [
        ("pause", 2),
        ("resume", 3),
    ]


def test_task_center_multi_window_smoke_invokes_electron(monkeypatch, tmp_path) -> None:
    module = _load_module()
    console_dir = tmp_path / "console"
    electron_bin = console_dir / "node_modules" / ".bin" / "electron"
    electron_runtime = (
        console_dir
        / "node_modules"
        / "electron"
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    electron_framework = (
        electron_runtime.parents[1]
        / "Frameworks"
        / "Electron Framework.framework"
        / "Electron Framework"
    )
    script = console_dir / "electron" / "taskCenterMultiWindowSmoke.cjs"
    preload = console_dir / "electron" / "taskCenterMultiWindowSmokePreload.cjs"
    electron_bin.parent.mkdir(parents=True)
    (console_dir / "node_modules" / "electron").mkdir(parents=True)
    electron_runtime.parent.mkdir(parents=True)
    electron_framework.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    electron_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_framework.write_text("framework\n", encoding="utf-8")
    (console_dir / "node_modules" / "electron" / "path.txt").write_text(
        "dist/Electron.app/Contents/MacOS/Electron\n",
        encoding="utf-8",
    )
    script.write_text("module.exports = {}\n", encoding="utf-8")
    preload.write_text("module.exports = {}\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        calls.append((command, cwd, text, capture_output, timeout, check))
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "renderer log\n"
                '{"ok": true, "run_id": "run-1", '
                '"budget_statuses": [200], "pause_statuses": [200, 409]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    payload = module._run_task_center_multi_window_smoke(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        run_id="run-1",
        timeout=3,
        project_root=tmp_path,
    )

    assert payload["ok"] is True
    command, cwd, text, capture_output, timeout, check = calls[0]
    assert command == [
        str(electron_runtime),
        str(script),
        "--base-url",
        "http://127.0.0.1:19090",
        "--desktop-token",
        "token-1",
        "--run-id",
        "run-1",
        "--timeout-ms",
        "3000",
    ]
    assert cwd == str(console_dir)
    assert text is True
    assert capture_output is True
    assert timeout == 23
    assert check is False


def test_chat_runtime_stream_renderer_smoke_invokes_electron(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module()
    console_dir = tmp_path / "console"
    electron_bin = console_dir / "node_modules" / ".bin" / "electron"
    electron_runtime = (
        console_dir
        / "node_modules"
        / "electron"
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    electron_framework = (
        electron_runtime.parents[1]
        / "Frameworks"
        / "Electron Framework.framework"
        / "Electron Framework"
    )
    script = console_dir / "electron" / "chatRuntimeStreamSmoke.cjs"
    electron_bin.parent.mkdir(parents=True)
    (console_dir / "node_modules" / "electron").mkdir(parents=True)
    electron_runtime.parent.mkdir(parents=True)
    electron_framework.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    electron_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_framework.write_text("framework\n", encoding="utf-8")
    (console_dir / "node_modules" / "electron" / "path.txt").write_text(
        "dist/Electron.app/Contents/MacOS/Electron\n",
        encoding="utf-8",
    )
    script.write_text("module.exports = {}\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        calls.append((command, cwd, text, capture_output, timeout, check))
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "renderer log\n"
                '{"ok": true, "chat_id": "chat-1", '
                '"terminal_type": "turn.failed", "terminal_status": "failed", '
                '"reconnect_after_seq": 3, "no_replay_event_count": 0, '
                '"observed_stream_requests": ['
                '{"url": "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=0", '
                '"status": "sent"}, '
                '{"url": "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=3", '
                '"status": "sent"}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    payload = module._run_chat_runtime_stream_renderer_smoke(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        chat_id="chat-1",
        timeout=3,
        project_root=tmp_path,
    )

    assert payload["ok"] is True
    command, cwd, text, capture_output, timeout, check = calls[0]
    assert command == [
        str(electron_runtime),
        str(script),
        "--base-url",
        "http://127.0.0.1:19090",
        "--desktop-token",
        "token-1",
        "--chat-id",
        "chat-1",
        "--timeout-ms",
        "180000",
        "--expected-terminal-type",
        "turn.failed",
        "--expected-terminal-statuses",
        "failed",
    ]
    assert cwd == str(console_dir)
    assert text is True
    assert capture_output is True
    assert timeout == 200
    assert check is False


def test_chat_runtime_stream_renderer_smoke_accepts_completed_terminal(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module()
    console_dir = tmp_path / "console"
    electron_bin = console_dir / "node_modules" / ".bin" / "electron"
    electron_runtime = (
        console_dir
        / "node_modules"
        / "electron"
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    electron_framework = (
        electron_runtime.parents[1]
        / "Frameworks"
        / "Electron Framework.framework"
        / "Electron Framework"
    )
    script = console_dir / "electron" / "chatRuntimeStreamSmoke.cjs"
    electron_bin.parent.mkdir(parents=True)
    (console_dir / "node_modules" / "electron").mkdir(parents=True)
    electron_runtime.parent.mkdir(parents=True)
    electron_framework.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    electron_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_framework.write_text("framework\n", encoding="utf-8")
    (console_dir / "node_modules" / "electron" / "path.txt").write_text(
        "dist/Electron.app/Contents/MacOS/Electron\n",
        encoding="utf-8",
    )
    script.write_text("module.exports = {}\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        calls.append((command, cwd, text, capture_output, timeout, check))
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "renderer log\n"
                '{"ok": true, "chat_id": "chat-1", '
                '"terminal_type": "turn.completed", "terminal_status": "finished", '
                '"reconnect_after_seq": 3, "no_replay_event_count": 0, '
                '"observed_stream_requests": ['
                '{"url": "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=0", '
                '"status": "sent"}, '
                '{"url": "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=3", '
                '"status": "sent"}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    payload = module._run_chat_runtime_stream_renderer_smoke(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        chat_id="chat-1",
        timeout=3,
        expected_terminal_type="turn.completed",
        expected_terminal_statuses={"completed", "finished"},
        project_root=tmp_path,
    )

    assert payload["terminal_type"] == "turn.completed"
    command = calls[0][0]
    assert command[command.index("--expected-terminal-type") + 1] == "turn.completed"
    assert command[command.index("--expected-terminal-statuses") + 1] == (
        "completed,finished"
    )


def test_chat_runtime_stream_renderer_smoke_checks_basic_ui_send(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module()
    console_dir = tmp_path / "console"
    electron_bin = console_dir / "node_modules" / ".bin" / "electron"
    electron_runtime = (
        console_dir
        / "node_modules"
        / "electron"
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    electron_framework = (
        electron_runtime.parents[1]
        / "Frameworks"
        / "Electron Framework.framework"
        / "Electron Framework"
    )
    script = console_dir / "electron" / "chatRuntimeStreamSmoke.cjs"
    electron_bin.parent.mkdir(parents=True)
    (console_dir / "node_modules" / "electron").mkdir(parents=True)
    electron_runtime.parent.mkdir(parents=True)
    electron_framework.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    electron_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_framework.write_text("framework\n", encoding="utf-8")
    (console_dir / "node_modules" / "electron" / "path.txt").write_text(
        "dist/Electron.app/Contents/MacOS/Electron\n",
        encoding="utf-8",
    )
    script.write_text("module.exports = {}\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        calls.append((command, cwd, text, capture_output, timeout, check))
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "renderer log\n"
                '{"ok": true, "chat_id": "chat-1", '
                '"terminal_type": "turn.failed", "terminal_status": "failed", '
                '"reconnect_after_seq": 3, "no_replay_event_count": 0, '
                '"basic_chat_ui_send": {"ok": true, "chat_id": "basic-chat", '
                '"isolation_chat_id": "isolation-chat", '
                '"first_text": "FIRST", "second_text": "SECOND", '
                '"max_user_feedback_ms": 3000, '
                '"max_assistant_feedback_ms": 15000, '
                '"backend_turn_observed": true, '
                '"console_chat_post_count": 2, '
                '"console_chat_post_statuses": [200, 200], '
                '"experience_matrix": ['
                '{"id": "initial_empty_chat_dom", "passed": true}, '
                '{"id": "initial_empty_chat_backend", "passed": true}, '
                '{"id": "first_user_feedback_within_budget", "passed": true}, '
                '{"id": "first_assistant_feedback_within_budget", "passed": true}, '
                '{"id": "second_user_feedback_within_budget", "passed": true}, '
                '{"id": "second_assistant_feedback_within_budget", "passed": true}, '
                '{"id": "two_primary_answers_with_optional_persona_lines", "passed": true}, '
                '{"id": "no_empty_assistant_bubbles", "passed": true}, '
                '{"id": "no_duplicate_assistant_chrome", "passed": true}, '
                '{"id": "textarea_cleared_after_submit", "passed": true}, '
                '{"id": "backend_state_consistent_with_submission_mode", "passed": true}, '
                '{"id": "backend_pending_cleared", "passed": true}, '
                '{"id": "route_isolation_empty_dom", "passed": true}, '
                '{"id": "route_isolation_empty_backend", "passed": true}, '
                '{"id": "route_restore_preserved_dom", "passed": true}, '
                '{"id": "route_restore_preserved_backend", "passed": true}, '
                '{"id": "final_dom_stable", "passed": true}], '
                '"first": {"user_visible_ms": 120, '
                '"assistant_visible_ms": 450}, '
                '"second": {"user_visible_ms": 130, '
                '"assistant_visible_ms": 460}, '
                '"final": {"user_bubble_count": 2, '
                '"assistant_bubble_count": 2, "response_avatar_count": 2, '
                '"action_bar_count": 2, "empty_assistant_count": 0}, '
                '"final_stable": {"user_bubble_count": 2, '
                '"assistant_bubble_count": 2}, '
                '"final_backend": {"user_message_count": 2, '
                '"assistant_message_count": 2, "pending_user_message": ""}, '
                '"isolation": {"user_bubble_count": 0, '
                '"assistant_bubble_count": 0}, '
                '"isolation_backend": {"message_count": 0, '
                '"pending_user_message": ""}, '
                '"restored": {"user_bubble_count": 2, '
                '"assistant_bubble_count": 2}, '
                '"restored_backend": {"user_message_count": 2, '
                '"assistant_message_count": 2}}, '
                '"observed_stream_requests": ['
                '{"url": "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=0", '
                '"status": "sent"}, '
                '{"url": "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=3", '
                '"status": "sent"}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    payload = module._run_chat_runtime_stream_renderer_smoke(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        chat_id="chat-1",
        timeout=3,
        basic_ui_send={
            "chat_id": "basic-chat",
            "isolation_chat_id": "isolation-chat",
            "first_text": "FIRST",
            "second_text": "SECOND",
        },
        project_root=tmp_path,
    )

    assert payload["basic_chat_ui_send"]["ok"] is True
    command, cwd, text, capture_output, timeout, check = calls[0]
    assert "--basic-ui-chat-id" in command
    assert command[command.index("--basic-ui-chat-id") + 1] == "basic-chat"
    assert command[command.index("--basic-ui-isolation-chat-id") + 1] == (
        "isolation-chat"
    )
    assert command[command.index("--basic-ui-first-text") + 1] == "FIRST"
    assert command[command.index("--basic-ui-second-text") + 1] == "SECOND"
    assert cwd == str(console_dir)
    assert text is True
    assert capture_output is True
    assert timeout == 200
    assert check is False


def test_chat_runtime_stream_renderer_smoke_checks_provider_rapid_queue(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module()
    console_dir = tmp_path / "console"
    electron_bin = console_dir / "node_modules" / ".bin" / "electron"
    electron_runtime = (
        console_dir
        / "node_modules"
        / "electron"
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    electron_framework = (
        electron_runtime.parents[1]
        / "Frameworks"
        / "Electron Framework.framework"
        / "Electron Framework"
    )
    script = console_dir / "electron" / "chatRuntimeStreamSmoke.cjs"
    electron_bin.parent.mkdir(parents=True)
    (console_dir / "node_modules" / "electron").mkdir(parents=True)
    electron_runtime.parent.mkdir(parents=True)
    electron_framework.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    electron_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_framework.write_text("framework\n", encoding="utf-8")
    (console_dir / "node_modules" / "electron" / "path.txt").write_text(
        "dist/Electron.app/Contents/MacOS/Electron\n",
        encoding="utf-8",
    )
    script.write_text("module.exports = {}\n", encoding="utf-8")
    calls = []
    provider_payload = {
        "ok": True,
        "chat_id": "provider-chat",
        "prompt_text": "PROMPT",
        "queued_second_text": "SECOND",
        "queued_third_text": "THIRD",
        "thinking_text": "PUBLIC PROGRESS",
        "partial_text": "PARTIAL",
        "answer_text": "ANSWER: PROMPT",
        "pending_visible_ms": 50,
        "thinking_visible_ms": 100,
        "partial_visible_ms": 200,
        "final_visible_ms": 300,
        "pending": {
            "feedback_visible": True,
            "assistant_bubble_count": 1,
            "pending_assistant_count": 0,
            "composer_turn_progress_count": 1,
            "composer_turn_progress_busy_count": 1,
            "composer_turn_progress_status_texts": ["沈行知：正在发送；正在发送"],
        },
        "thinking": {
            "thinking_visible": True,
            "assistant_bubble_count": 1,
            "thinking_count": 1,
            "thinking_open_count": 0,
        },
        "partial": {
            "partial_visible": True,
            "assistant_bubble_count": 1,
        },
        "final": {
            "redirected_answer_visible": True,
            "old_full_answer_visible": False,
            "assistant_bubble_count": 2,
            "empty_assistant_count": 0,
            "response_avatar_count": 2,
            "action_bar_count": 2,
        },
        "rapid_queue": {
            "queued_submission_count": 2,
            "queued_before_first_answer": True,
            "queued_drafts": ["SECOND", "THIRD"],
            "submission_intents": ["supplement", "redirect"],
            "admitted_statuses": {
                "supplement": {"status": "admitted"},
                "redirect": {"status": "admitted"},
            },
            "final": {
                "user_bubble_count": 3,
                "assistant_bubble_count": 2,
                "empty_assistant_count": 0,
                "response_avatar_count": 2,
                "action_bar_count": 2,
                "user_texts_in_fifo_order": True,
                "assistant_texts_in_fifo_order": True,
                "old_partial_preserved": True,
                "old_full_answer_absent": True,
                "superseded_answer_absent": True,
                "redirect_answer_present": True,
                "conversation_items_preserve_fifo_order": True,
                "supplement_disposition": {"status": "superseded"},
                "redirect_disposition": {"status": "applied"},
            },
        },
        "backend": {
            "user_message_count": 3,
            "assistant_message_count": 2,
            "pending_user_message": "",
            "user_texts_in_fifo_order": True,
        },
    }
    stop_resend_payload = {
        "ok": True,
        "chat_id": "provider-stop-chat",
        "stop_terminal": "interrupted",
        "full_stopped_answer_absent": True,
        "final": {
            "user_bubble_count": 2,
            "assistant_bubble_count": 2,
            "empty_assistant_count": 0,
            "response_avatar_count": 2,
            "action_bar_count": 2,
        },
        "backend": {
            "user_message_count": 2,
            "assistant_message_count": 2,
            "pending_user_message": "",
        },
    }
    route_switch_payload = {
        "ok": True,
        "chat_id": "provider-switch-chat",
        "isolation_chat_id": "provider-switch-isolation-chat",
        "isolation": {"user_bubble_count": 0, "assistant_bubble_count": 0},
        "restored": {
            "user_bubble_count": 1,
            "assistant_bubble_count": 1,
            "empty_assistant_count": 0,
            "response_avatar_count": 1,
            "action_bar_count": 1,
        },
        "backend": {
            "user_message_count": 1,
            "assistant_message_count": 2,
            "thinking_occurrence_count": 1,
            "answer_occurrence_count": 1,
            "pending_user_message": "",
        },
    }

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        calls.append((command, cwd, text, capture_output, timeout, check))
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "renderer log\n"
                + json.dumps(
                    {
                        "ok": True,
                        "chat_id": "chat-1",
                        "terminal_type": "turn.failed",
                        "terminal_status": "failed",
                        "reconnect_after_seq": 3,
                        "no_replay_event_count": 0,
                        "provider_stream_ui": provider_payload,
                        "provider_stop_resend_ui": stop_resend_payload,
                        "provider_stream_route_switch_ui": route_switch_payload,
                        "observed_stream_requests": [
                            {
                                "url": "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=0",
                                "status": "sent",
                            },
                            {
                                "url": "http://127.0.0.1:19090/api/chats/chat-1/events/stream?after_seq=3",
                                "status": "sent",
                            },
                        ],
                    }
                )
            ),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    provider_stream_ui_input = {
        "chat_id": "provider-chat",
        "prompt_text": "PROMPT",
        "queued_second_text": "SECOND",
        "queued_third_text": "THIRD",
        "thinking_text": "THINKING",
        "public_progress_text": "PUBLIC PROGRESS",
        "partial_text": "PARTIAL",
        "assistant_text": "ANSWER: PROMPT",
        "stop_chat_id": "provider-stop-chat",
        "stop_prompt_text": "STOP PROMPT",
        "stop_assistant_text": "ANSWER: STOP PROMPT",
        "stop_resend_text": "RESEND",
        "stop_resend_assistant_text": "ANSWER: RESEND",
        "switch_chat_id": "provider-switch-chat",
        "switch_isolation_chat_id": "provider-switch-isolation-chat",
        "switch_prompt_text": "SWITCH PROMPT",
        "switch_assistant_text": "ANSWER: SWITCH PROMPT",
    }
    renderer_state = {
        "api_base_url": "http://127.0.0.1:19090",
        "desktop_token": "token-1",
    }
    payload = module._run_chat_runtime_stream_renderer_smoke(
        renderer_state,
        chat_id="chat-1",
        timeout=3,
        provider_stream_ui=provider_stream_ui_input,
        project_root=tmp_path,
    )

    assert payload["provider_stream_ui"]["rapid_queue"][
        "queued_before_first_answer"
    ] is True
    command, cwd, text, capture_output, timeout, check = calls[0]
    assert command[command.index("--provider-stream-queued-second-text") + 1] == (
        "SECOND"
    )
    assert command[command.index("--provider-stream-queued-third-text") + 1] == (
        "THIRD"
    )
    assert command[command.index("--provider-stream-thinking-text") + 1] == (
        "PUBLIC PROGRESS"
    )
    assert command[command.index("--provider-stop-chat-id") + 1] == (
        "provider-stop-chat"
    )
    assert command[command.index("--provider-switch-chat-id") + 1] == (
        "provider-switch-chat"
    )
    assert cwd == str(console_dir)
    assert text is True
    assert capture_output is True
    assert timeout == 200
    assert check is False

    provider_payload["pending"]["pending_assistant_count"] = 1
    with pytest.raises(AssertionError, match="invalid pending feedback"):
        module._run_chat_runtime_stream_renderer_smoke(
            renderer_state,
            chat_id="chat-1",
            timeout=3,
            provider_stream_ui=provider_stream_ui_input,
            project_root=tmp_path,
        )


def test_seed_chat_basic_ui_send_registers_empty_chat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    calls = []
    ids = iter(["basic-chat", "isolation-chat"])

    def fake_json_request(method, url, *, payload, headers, timeout):
        calls.append((method, url, payload, headers, timeout))
        return 200, "{}", {"id": next(ids)}

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setattr(
        module.uuid,
        "uuid4",
        lambda: type("FakeUuid", (), {"hex": "abcdef1234567890"})(),
    )

    result = module._seed_chat_basic_ui_send(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        workspace_dir=tmp_path / "workspace",
        timeout=3,
    )

    assert result == {
        "chat_id": "basic-chat",
        "isolation_chat_id": "isolation-chat",
        "session_id": "console:packaged-basic-ui-send-abcdef12",
        "isolation_session_id": "console:packaged-basic-ui-isolation-abcdef12",
        "run_id": "packaged-basic-ui-send-abcdef12",
        "first_text": "PACKAGED_BASIC_UI_FIRST_abcdef12",
        "second_text": "PACKAGED_BASIC_UI_SECOND_abcdef12",
        "topic_workspace_dir": str(
            tmp_path
            / "workspace"
            / "topic-workspaces"
            / "packaged-basic-ui-send-abcdef12"
        ),
    }
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:19090/api/chats",
            {
                "name": "basic chat UI send smoke abcdef12",
                "session_id": "console:packaged-basic-ui-send-abcdef12",
                "user_id": "default",
                "channel": "console",
                "meta": {
                    "topic_workspace_dir": str(
                        tmp_path
                        / "workspace"
                        / "topic-workspaces"
                        / "packaged-basic-ui-send-abcdef12"
                    )
                },
            },
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        ),
        (
            "POST",
            "http://127.0.0.1:19090/api/chats",
            {
                "name": "basic chat UI isolation smoke abcdef12",
                "session_id": "console:packaged-basic-ui-isolation-abcdef12",
                "user_id": "default",
                "channel": "console",
                "meta": {
                    "topic_workspace_dir": str(
                        tmp_path
                        / "workspace"
                        / "topic-workspaces"
                        / "packaged-basic-ui-send-abcdef12-isolation"
                    )
                },
            },
            {"X-Lantai-Desktop-Token": "token-1"},
            3,
        ),
    ]


def test_seed_chat_provider_stream_ui_registers_rapid_queue_drafts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    calls = []

    def fake_json_request(method, url, *, payload, headers, timeout):
        calls.append((method, url, payload, headers, timeout))
        return 200, "{}", {"id": "provider-chat"}

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setattr(
        module.uuid,
        "uuid4",
        lambda: type("FakeUuid", (), {"hex": "abcdef1234567890"})(),
    )

    result = module._seed_chat_provider_stream_ui(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        workspace_dir=tmp_path / "workspace",
        timeout=3,
    )

    assert result["chat_id"] == "provider-chat"
    assert result["prompt_text"].endswith("abcdef12")
    assert result["queued_second_text"] == "PACKAGED_PROVIDER_QUEUE_SECOND_abcdef12"
    assert result["queued_third_text"] == "PACKAGED_PROVIDER_QUEUE_THIRD_abcdef12"
    assert result["stop_prompt_text"].endswith("STOP_abcdef12")
    assert result["stop_resend_text"] == "PACKAGED_PROVIDER_STOP_RESEND_abcdef12"
    assert result["switch_prompt_text"].endswith("SWITCH_abcdef12")
    assert len(calls) == 4
    assert calls[0][0:2] == (
        "POST",
        "http://127.0.0.1:19090/api/chats",
    )


def test_browser_content_renderer_smoke_invokes_electron(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module()
    console_dir = tmp_path / "console"
    electron_bin = console_dir / "node_modules" / ".bin" / "electron"
    electron_runtime = (
        console_dir
        / "node_modules"
        / "electron"
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    electron_framework = (
        electron_runtime.parents[1]
        / "Frameworks"
        / "Electron Framework.framework"
        / "Electron Framework"
    )
    script = console_dir / "electron" / "browserContentRuntimeSmoke.cjs"
    electron_bin.parent.mkdir(parents=True)
    (console_dir / "node_modules" / "electron").mkdir(parents=True)
    electron_runtime.parent.mkdir(parents=True)
    electron_framework.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    electron_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    electron_framework.write_text("framework\n", encoding="utf-8")
    (console_dir / "node_modules" / "electron" / "path.txt").write_text(
        "dist/Electron.app/Contents/MacOS/Electron\n",
        encoding="utf-8",
    )
    script.write_text("module.exports = {}\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        calls.append((command, cwd, text, capture_output, timeout, check))
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "renderer log\n"
                '{"ok": true, "run_id": "browser-content-run-1", '
                '"renderer_run_status": "completed", '
                '"renderer_authority_status": "succeeded", '
                '"renderer_pending_approval": false, '
                '"export_statuses": [200], "captured_export_status": 200, '
                '"captured_contract_id": '
                '"loop-browser-content-draft-preview-contract"}'
            ),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    payload = module._run_browser_content_renderer_smoke(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        run_id="browser-content-run-1",
        timeout=3,
        project_root=tmp_path,
    )

    assert payload["ok"] is True
    command, cwd, text, capture_output, timeout, check = calls[0]
    assert command == [
        str(electron_runtime),
        str(script),
        "--base-url",
        "http://127.0.0.1:19090",
        "--desktop-token",
        "token-1",
        "--run-id",
        "browser-content-run-1",
        "--expected-contract-id",
        "loop-browser-content-draft-preview-contract",
        "--timeout-ms",
        "3000",
    ]
    assert cwd == str(console_dir)
    assert text is True
    assert capture_output is True
    assert timeout == 23
    assert check is False


def test_task_center_multi_window_control_creates_run_and_cleans_up(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module()
    success_calls = []
    smoke_calls = []
    state_by_run: dict[str, dict[str, object]] = {}

    def fake_json_request(method, url, *, payload=None, headers, timeout):
        success_calls.append((method, url, payload, headers, timeout))
        if method == "POST" and url.endswith("/api/browser-workbench/profiles"):
            return 200, "{}", {"id": payload["id"]}
        if method == "POST" and url.endswith("/api/browser-workbench/tasks"):
            run_id = payload["run_id"]
            state_by_run[run_id] = {
                "id": run_id,
                "source": "browser",
                "status": "running",
                "authority_status": "running",
                "attempt_id": f"{run_id}:browser-attempt",
                "revision": 2,
                "workspace_dir": str(tmp_path),
                "recovery": {"can_pause": True, "can_resume": False},
            }
            return 200, "{}", {"run_id": run_id, "status": "queued"}
        if method == "GET" and url.endswith("/api/runtime/runs"):
            return 200, "{}", {"items": list(state_by_run.values())}
        if method == "POST" and url.endswith("/cancel"):
            return 200, "{}", {"status": "cancelled"}
        raise AssertionError(f"Unexpected request: {method} {url}")

    def fake_window_smoke(state, *, run_id, timeout):
        smoke_calls.append((state, run_id, timeout))
        state_by_run[run_id] = {
            **state_by_run[run_id],
            "status": "stalled",
            "authority_status": "paused",
            "revision": 3,
            "recovery": {"can_pause": False, "can_resume": True},
        }
        return {
            "ok": True,
            "run_id": run_id,
            "budget_statuses": [200],
            "pause_statuses": [200, 409],
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setattr(module, "_run_task_center_multi_window_smoke", fake_window_smoke)

    run_id = module._assert_task_center_multi_window_stale_control(
        {
            "api_base_url": "http://127.0.0.1:19090",
            "desktop_token": "token-1",
        },
        workspace_dir=tmp_path,
        timeout=3,
    )

    assert run_id.startswith("browser-taskcenter-window-smoke-")
    task_call = next(call for call in success_calls if call[1].endswith("/tasks"))
    cancel_call = next(call for call in success_calls if call[1].endswith("/cancel"))
    assert task_call[2]["metadata"]["session_id"] == "browser-taskcenter-window-smoke"
    assert task_call[2]["metadata"]["workspace_dir"] == str(tmp_path)
    assert task_call[2]["actions"][0]["access"] == "write"
    assert smoke_calls == [
        (
            {
                "api_base_url": "http://127.0.0.1:19090",
                "desktop_token": "token-1",
            },
            run_id,
            3,
        )
    ]
    assert cancel_call[0] == "POST"


def test_restart_cycle_crashes_backend_instead_of_graceful_shutdown(monkeypatch) -> None:
    module = _load_module()
    calls = []

    monkeypatch.setattr(module.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    module._crash_managed_backend_for_restart(12345)

    assert calls == [(12345, module.signal.SIGKILL)]
