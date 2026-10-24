# -*- coding: utf-8 -*-
"""Unit coverage for the exact-candidate steering acceptance driver."""

# pylint: disable=protected-access

from __future__ import annotations

import importlib.util
import socket
import socketserver
import threading
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_steering_control_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "packaged_steering_control_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_driver_is_hard_bound_to_authorized_provider_and_budget() -> None:
    module = _load_module()

    assert module.EXPECTED_PROVIDER_ID == "minimax-cn"
    assert module.EXPECTED_MODEL_ID == "MiniMax-M3"
    assert module.MAX_PROVIDER_REQUESTS == 8
    module._assert_authorized_model(
        {"provider_id": "minimax-cn", "model": "MiniMax-M3"},
    )
    with pytest.raises(RuntimeError, match="authorized configured model"):
        module._assert_authorized_model(
            {"provider_id": "openai", "model": "gpt-other"},
        )


def test_driver_can_bind_an_explicit_authorized_provider_route() -> None:
    module = _load_module()

    module._assert_authorized_model(
        {"provider_id": "kimi-codingplan", "model": "kimi-for-coding"},
        expected_provider_id="kimi-codingplan",
        expected_model_id="kimi-for-coding",
    )
    with pytest.raises(RuntimeError, match="authorized configured model"):
        module._assert_authorized_model(
            {"provider_id": "minimax-cn", "model": "MiniMax-M3"},
            expected_provider_id="kimi-codingplan",
            expected_model_id="kimi-for-coding",
        )


def test_candidate_scoped_connect_proxy_relays_then_drops_one_tunnel() -> None:
    module = _load_module()

    class EchoHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            while True:
                payload = self.request.recv(1024)
                if not payload:
                    return
                self.request.sendall(payload)

    echo_server = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0),
        EchoHandler,
    )
    echo_thread = threading.Thread(target=echo_server.serve_forever, daemon=True)
    echo_thread.start()
    proxy = module._ControlledConnectProxy()
    proxy.start()
    client = socket.create_connection(
        ("127.0.0.1", int(proxy.url.rsplit(":", 1)[1])),
        timeout=2,
    )
    client.settimeout(2)
    try:
        echo_port = echo_server.server_address[1]
        client.sendall(
            f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{echo_port}\r\n\r\n".encode("ascii")
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += client.recv(1024)
        assert response.startswith(b"HTTP/1.1 200")
        proxy.wait_for_active(timeout=2)
        client.sendall(b"candidate-only-probe")
        assert client.recv(1024) == b"candidate-only-probe"

        assert proxy.drop_active() == 1
        try:
            closed = client.recv(1024) == b""
        except OSError:
            closed = True
        assert closed is True
        assert proxy.tunnel_count == 1
        assert proxy.drop_count == 1
    finally:
        client.close()
        proxy.stop()
        echo_server.shutdown()
        echo_server.server_close()
        echo_thread.join(timeout=2)


def test_candidate_proxy_environment_bypasses_only_loopback() -> None:
    module = _load_module()
    env = {}

    module._route_candidate_through_proxy(
        env,
        proxy_url="http://127.0.0.1:45678",
    )

    assert env["HTTPS_PROXY"] == "http://127.0.0.1:45678"
    assert env["https_proxy"] == "http://127.0.0.1:45678"
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert env["no_proxy"] == "127.0.0.1,localhost,::1"


def test_payloads_cover_text_image_only_and_mixed_redirect() -> None:
    module = _load_module()
    upload_a = {
        "url": "/tmp/a.png",
        "file_name": "a.png",
        "size": 123,
    }
    upload_b = {
        "url": "/tmp/b.png",
        "file_name": "b.png",
        "size": 456,
    }

    text = module._control_payload(
        chat_id="chat-1",
        session_id="session-1",
        command_id="command-text",
        intent="supplement",
        content=[{"type": "text", "text": "extra constraint"}],
    )
    image_only = module._control_payload(
        chat_id="chat-1",
        session_id="session-1",
        command_id="command-image",
        intent="supplement",
        content=[module._image_block(upload_a)],
    )
    mixed = module._control_payload(
        chat_id="chat-1",
        session_id="session-1",
        command_id="command-mixed",
        intent="redirect",
        content=[module._image_block(upload_b), {"type": "text", "text": "redirect"}],
    )

    assert text["meta"]["control_intent"] == "supplement"
    assert image_only["input"][0]["content"] == [
        {
            "type": "image",
            "image_url": "/tmp/a.png",
            "filename": "a.png",
            "media_type": "image/png",
            "file_size": 123,
        }
    ]
    assert not any(
        block["type"] == "text"
        for block in image_only["input"][0]["content"]
    )
    assert [block["type"] for block in mixed["input"][0]["content"]] == [
        "image",
        "text",
    ]
    assert mixed["meta"]["control_intent"] == "redirect"


def test_document_payload_preserves_uploaded_source_metadata() -> None:
    module = _load_module()
    upload = {
        "url": "/tmp/selected-pages.pdf",
        "file_name": "selected-pages.pdf",
        "size": 789,
    }
    assert module._file_block(upload, media_type="application/pdf") == {
        "type": "file",
        "file_url": "/tmp/selected-pages.pdf",
        "filename": "selected-pages.pdf",
        "media_type": "application/pdf",
        "file_size": 789,
    }


def test_upload_uses_the_document_media_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    document = tmp_path / "selected-pages.pdf"
    document.write_bytes(b"pdf-proof")
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"url":"/upload.pdf","file_name":"selected-pages.pdf","size":9,"digest":"sha256:x","version":1}'

    def fake_urlopen(req, timeout):
        captured["request"] = req
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(module.request, "urlopen", fake_urlopen)

    module._upload_file(
        url="http://127.0.0.1/upload",
        headers={},
        path=document,
        media_type="application/pdf",
        timeout=5,
    )

    assert b"Content-Type: application/pdf" in captured["request"].data


def test_document_fixture_has_versioned_selected_and_out_of_scope_sections(
    tmp_path: Path,
) -> None:
    module = _load_module()
    document = tmp_path / "scope-proof.md"

    module._create_document_fixture(document)

    text = document.read_text(encoding="utf-8")
    assert "ASSET_VERSION: steering-document-v1" in text
    assert "OUT_OF_SCOPE_TOKEN:" in text
    assert module.DOCUMENT_SCOPE_SENTINEL in text


def test_provider_request_count_uses_bound_lineage_and_enforces_cap() -> None:
    module = _load_module()
    receipts = [
        {"model_request_id": "run-1:model:2"},
        {"model_request_id": "run-1:model:2"},
        {"model_request_id": "run-1:model:3"},
    ]

    assert module._provider_request_count(receipts) == 3
    with pytest.raises(AssertionError, match="provider request cap"):
        module._assert_provider_request_budget(
            [{"model_request_id": "run-1:model:9"}],
        )


def test_safe_evidence_omits_prompts_responses_paths_and_raw_ids() -> None:
    module = _load_module()
    evidence = module._build_safe_evidence(
        configured_model={"provider_id": "minimax-cn", "model": "MiniMax-M3"},
        app_metadata={"short_version": "2.2.12", "artifact_sha256": {}},
        run_identity={"run_id": "run-secret", "attempt_id": "attempt-secret"},
        admissions=[
            {
                "command_id": "command-secret",
                "intent": "redirect",
                "http_status": 200,
                "execution_status": "pending",
                "next_waiting_reason": "awaiting_safe_yield",
                "admission_ms": 12,
            }
        ],
        final_receipts=[
            {
                "command_id": "command-secret",
                "run_id": "run-secret",
                "attempt_id": "attempt-secret",
                "execution_status": "applied",
                "model_request_id": "run-secret:model:2",
                "model_epoch": 2,
                "intent_revision": 3,
            }
        ],
        observation={
            "thinking_before_redirect": True,
            "answer_after_redirect": True,
            "visual_sentinel_matched": True,
            "stream_completed": True,
            "old_stream_yielded": True,
            "retry_same_receipt": True,
            "runtime_run_count": 1,
            "runtime_attempt_count": 1,
            "attachment_count": 2,
            "tool_effect_count": 0,
            "timing_ms": {"redirect_to_successor_reasoning": 42},
        },
    )

    serialized = repr(evidence)
    assert "run-secret" not in serialized
    assert "attempt-secret" not in serialized
    assert "command-secret" not in serialized
    assert "/tmp/" not in serialized
    assert evidence["counts"]["provider_requests"] == 2
    assert evidence["lineage"]["model_epochs"] == [2]
    assert evidence["lineage"]["intent_revisions"] == [3]
    assert evidence["acceptance"]["pure_image_no_synthetic_text"] is True
    assert evidence["acceptance"]["visual_sentinel_matched"] is True


def test_safe_evidence_records_the_explicit_authorized_route() -> None:
    module = _load_module()

    evidence = module._build_safe_evidence(
        configured_model={
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
        },
        expected_provider_id="kimi-codingplan",
        expected_model_id="kimi-for-coding",
        app_metadata={},
        run_identity={},
        admissions=[],
        final_receipts=[],
        observation={},
    )

    assert evidence["acceptance"]["authorized_provider_model"] is True


def test_safe_evidence_requires_a_bound_document_scope_result() -> None:
    module = _load_module()

    evidence = module._build_safe_evidence(
        configured_model={"provider_id": "minimax-cn", "model": "MiniMax-M3"},
        app_metadata={},
        run_identity={},
        admissions=[],
        final_receipts=[],
        observation={
            "document_attachment_count": 1,
            "document_scope_sentinel_matched": True,
        },
    )

    assert evidence["counts"]["documents"] == 1
    assert evidence["acceptance"]["document_scope_sentinel_matched"] is True
    assert evidence["acceptance"]["document_attachment_bound"] is True


def test_safe_evidence_requires_physical_reconnect_when_requested() -> None:
    module = _load_module()
    base_observation = {
        "physical_reconnect_required": True,
        "candidate_scoped_proxy": True,
        "proxy_tunnel_count": 2,
        "proxy_drop_count": 1,
    }

    missing = module._build_safe_evidence(
        configured_model={"provider_id": "minimax-cn", "model": "MiniMax-M3"},
        app_metadata={},
        run_identity={},
        admissions=[],
        final_receipts=[],
        observation=base_observation,
    )
    recovered = module._build_safe_evidence(
        configured_model={"provider_id": "minimax-cn", "model": "MiniMax-M3"},
        app_metadata={},
        run_identity={},
        admissions=[],
        final_receipts=[],
        observation={**base_observation, "physical_disconnect_recovered": True},
    )

    assert missing["acceptance"]["physical_disconnect_recovered"] is False
    assert recovered["acceptance"]["physical_disconnect_recovered"] is True
    assert recovered["network_fault"] == {
        "required": True,
        "tunnels": 2,
        "dropped_tunnels": 1,
    }


def test_provider_disconnect_recovery_filter_requires_the_exact_reason() -> None:
    module = _load_module()
    events = [
        {
            "type": "runtime.warning",
            "payload": {
                "kind": "model_output_recovery",
                "details": {"reason": "provider_connection_interrupted"},
            },
        },
        {
            "type": "runtime.warning",
            "payload": {
                "kind": "model_output_recovery",
                "details": {"reason": "stream_idle_timeout"},
            },
        },
    ]

    assert module._provider_disconnect_recoveries(events) == [events[0]]


def test_cli_passes_explicit_authorized_route_to_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(module, "run", fake_run)

    assert module.main(
        [
            "--app",
            "/tmp/candidate.app",
            "--source-working-dir",
            "/tmp/working",
            "--source-secret-dir",
            "/tmp/working.secret",
            "--expected-provider-id",
            "kimi-codingplan",
            "--expected-model-id",
            "kimi-for-coding",
            "--exercise-physical-reconnect",
        ]
    ) == 0
    assert captured["expected_provider_id"] == "kimi-codingplan"
    assert captured["expected_model_id"] == "kimi-for-coding"
    assert captured["exercise_physical_reconnect"] is True
