#!/usr/bin/env python3
"""Smoke-test a packaged macOS .app through Electron's backend lifecycle.

The app only exposes its desktop token/nonce/pid when launched with
LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE. This lets release validation exercise the
real packaged Electron process without weakening normal desktop startup.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import plistlib
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Literal, cast
from urllib import error, parse, request


DESKTOP_TOKEN_HEADER = "X-Lantai-Desktop-Token"
SMOKE_EVIDENCE_SCHEMA = "com.lantai.packaged-app-lifecycle-smoke.v1"
SMOKE_MODEL_PROVIDER_ID = "lantai-smoke-openai"
SMOKE_MODEL_ID = "lantai-smoke-model"
SMOKE_MODEL_REPLY_PREFIX = "PACKAGED_MODEL_REPLY"
# Keep the partial sentinel inside the first provider chunk rather than at its
# boundary. The prompt-leak matcher is allowed to retain a chunk suffix that
# could still grow into a protected system fingerprint.
SMOKE_MODEL_PARTIAL_SENTINEL = "PACKAGED_MODEL_"
SMOKE_MODEL_THINKING_TEXT = (
    "先读取附件并核对标题、数据与交付格式。\n"
    "接着生成演示文稿，检查缩略图和最终文件。"
)
SMOKE_PUBLIC_REASONING_TEXT = SMOKE_MODEL_THINKING_TEXT
SMOKE_FORBIDDEN_FABRICATED_PROGRESS_TEXT = "正在梳理任务目标、已有信息和下一步处理顺序。"
SMOKE_FORBIDDEN_FIXED_REASONING_STATUS = "正在分析任务、核对上下文与可用工具。"
SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX = "PACKAGED_PROVIDER_STREAM_UI_PROMPT_"
# Keep the reasoning-only DOM phase observable even when Electron is still
# finishing a route transition.  A sub-second window made the stop/resend gate
# depend on machine scheduling rather than on the product behaviour it checks.
# Keep each provider-stream phase visible long enough for a cold, signed
# Electron renderer to commit and paint it before the next SSE chunk arrives.
# A shorter interval made the strict thinking-before-text assertion flaky: the
# stream was correct, but React could commit reasoning together with the first
# text chunk under startup load.
SMOKE_MODEL_PROVIDER_STREAM_STEP_DELAY_SECONDS = 4.0
REQUIRED_BASIC_EXPERIENCE_CHECKS = {
    "initial_empty_chat_dom",
    "initial_empty_chat_backend",
    "first_user_feedback_within_budget",
    "first_assistant_feedback_within_budget",
    "second_user_feedback_within_budget",
    "second_assistant_feedback_within_budget",
    "two_primary_answers_with_optional_persona_lines",
    "no_empty_assistant_bubbles",
    "no_duplicate_assistant_chrome",
    "textarea_cleared_after_submit",
    "backend_state_consistent_with_submission_mode",
    "backend_pending_cleared",
    "route_isolation_empty_dom",
    "route_isolation_empty_backend",
    "route_restore_preserved_dom",
    "route_restore_preserved_backend",
    "final_dom_stable",
}


def _assert_packaged_customization_integrity(
    state: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    """Prove the frozen package retained the protected personalization system."""

    payloads: dict[str, Any] = {}
    for key, endpoint in (
        ("registry", "/api/customization/registry"),
        ("state", "/api/customization/state"),
        ("effective", "/api/customization/effective-state"),
        ("slots", "/api/customization/slots"),
    ):
        _status, _body, payloads[key] = _json_request(
            "GET",
            f"{_state_base_url(state)}{endpoint}",
            headers=_state_headers(state),
            timeout=timeout,
        )

    registry = payloads["registry"]
    customization_state = payloads["state"]
    effective = payloads["effective"]
    slots = payloads["slots"]
    items = registry.get("items") if isinstance(registry, dict) else None
    values = effective.get("values") if isinstance(effective, dict) else None
    if not isinstance(items, list) or len(items) < 20:
        raise AssertionError(f"Packaged customization registry is incomplete: {registry!r}")
    if not isinstance(customization_state, dict) or customization_state.get(
        "mode"
    ) not in {"normal", "factory", "safe"}:
        raise AssertionError(
            f"Packaged customization state is invalid: {customization_state!r}"
        )
    if not isinstance(slots, list) or len(slots) != 5:
        raise AssertionError(f"Packaged customization slots are incomplete: {slots!r}")
    if not isinstance(values, dict):
        raise AssertionError(f"Packaged effective customization is invalid: {effective!r}")
    required_values = {
        "appearance.palette": "mist",
        "navigation.sidebar.chat.visible": True,
    }
    missing_or_changed = {
        key: {"expected": expected, "observed": values.get(key)}
        for key, expected in required_values.items()
        if values.get(key) != expected
    }
    if missing_or_changed:
        raise AssertionError(
            "Packaged Factory customization values changed: "
            f"{missing_or_changed!r}"
        )
    return {
        "registry_schema_version": registry.get("schema_version"),
        "registry_item_count": len(items),
        "mode": customization_state.get("mode"),
        "active_profile_id": customization_state.get("active_profile_id"),
        "slot_count": len(slots),
        "factory_values": required_values,
    }
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if SRC_ROOT.is_dir() and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _smoke_message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        str(item.get("text") or "") for item in content if isinstance(item, dict)
    )


def _extract_smoke_prompt_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        text = _smoke_message_text(message)
        if text:
            return text
    message_texts = [_smoke_message_text(message) for message in messages]
    marker = re.compile(
        rf"{re.escape(SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX)}[0-9a-f]{{8}}"
    )
    for text in reversed(message_texts):
        match = marker.search(text)
        if match:
            return match.group(0)
    return next((text for text in reversed(message_texts) if text), "")


class _SmokeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    """Tiny OpenAI-compatible server for model-backed packaged smoke tests."""

    protocol_version = "HTTP/1.1"
    server_version = "LantaiSmokeOpenAI/1.0"

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = parse.urlparse(self.path).path.rstrip("/")
        if path not in {"/v1/models", "/models"}:
            self._send_json(404, {"error": {"message": "not found"}})
            return
        self._send_json(
            200,
            {
                "object": "list",
                "data": [
                    {
                        "id": SMOKE_MODEL_ID,
                        "object": "model",
                        "owned_by": "lantai-smoke",
                    }
                ],
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = parse.urlparse(self.path).path.rstrip("/")
        if path not in {"/v1/chat/completions", "/chat/completions"}:
            self._send_json(404, {"error": {"message": "not found"}})
            return
        content_length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(content_length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid json"}})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "invalid request"}})
            return
        requests_seen = getattr(self.server, "requests_seen", None)
        if isinstance(requests_seen, list):
            requests_seen.append(payload)
        failures_remaining = int(
            getattr(self.server, "fail_chat_requests_remaining", 0) or 0
        )
        if failures_remaining > 0:
            self.server.fail_chat_requests_remaining = failures_remaining - 1
            self._send_json(
                503,
                {
                    "error": {
                        "message": "injected packaged smoke network disconnect",
                        "type": "network_disconnect",
                    }
                },
            )
            return
        model = str(payload.get("model") or SMOKE_MODEL_ID)
        messages = payload.get("messages")
        prompt_text = _extract_smoke_prompt_text(messages)
        reply = f"{SMOKE_MODEL_REPLY_PREFIX}: {prompt_text[:80] or 'ok'}"
        stream_step_delay = (
            SMOKE_MODEL_PROVIDER_STREAM_STEP_DELAY_SECONDS
            if SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX in prompt_text
            else 0.05
        )
        if payload.get("stream") is False:
            self._send_json(
                200,
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": reply,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"

        def write_sse_frame(frame: bytes) -> None:
            self.wfile.write(f"{len(frame):X}\r\n".encode("ascii"))
            self.wfile.write(frame)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        def write_chunk(delta: dict[str, Any], *, finish_reason: str | None = None) -> None:
            event = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason,
                    }
                ],
            }
            write_sse_frame(
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode(
                    "utf-8"
                )
            )
            time.sleep(stream_step_delay)

        try:
            write_chunk({"role": "assistant"})
            write_chunk({"reasoning_content": SMOKE_MODEL_THINKING_TEXT})
            write_chunk({"content": SMOKE_MODEL_REPLY_PREFIX})
            write_chunk({"content": reply[len(SMOKE_MODEL_REPLY_PREFIX) :]})
            write_chunk({}, finish_reason="stop")
            usage_event = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
            write_sse_frame(
                f"data: {json.dumps(usage_event, ensure_ascii=False)}\n\n".encode(
                    "utf-8"
                )
            )
            write_sse_frame(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


class _LocalOpenAISmokeServer:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _SmokeOpenAIHandler)
        self._server.requests_seen = []  # type: ignore[attr-defined]
        self._server.fail_chat_requests_remaining = 0  # type: ignore[attr-defined]
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    @property
    def request_count(self) -> int:
        requests_seen = getattr(self._server, "requests_seen", [])
        return len(requests_seen) if isinstance(requests_seen, list) else 0

    @property
    def request_payloads(self) -> list[dict[str, Any]]:
        requests_seen = getattr(self._server, "requests_seen", [])
        if not isinstance(requests_seen, list):
            return []
        return [dict(item) for item in requests_seen if isinstance(item, dict)]

    def fail_next_chat_requests(self, count: int = 1) -> None:
        if count < 1:
            raise ValueError("failure count must be positive")
        self._server.fail_chat_requests_remaining = count  # type: ignore[attr-defined]

    def clear_chat_failures(self) -> None:
        self._server.fail_chat_requests_remaining = 0  # type: ignore[attr-defined]

    def __enter__(self) -> "_LocalOpenAISmokeServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _write_smoke_openai_provider_config(
    user_data_dir: Path,
    *,
    base_url: str,
) -> dict[str, Any]:
    provider_root = user_data_dir / "working.secret" / "providers"
    custom_dir = provider_root / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    provider_path = custom_dir / f"{SMOKE_MODEL_PROVIDER_ID}.json"
    active_model_path = provider_root / "active_model.json"
    provider_payload = {
        "id": SMOKE_MODEL_PROVIDER_ID,
        "name": "Lantai Packaged Smoke Model",
        "base_url": base_url,
        "api_key": "sk-lantai-packaged-smoke",
        "chat_model": "OpenAIChatModel",
        "models": [],
        "extra_models": [
            {
                "id": SMOKE_MODEL_ID,
                "name": "Lantai Packaged Smoke Model",
                "supports_image": False,
                "supports_video": False,
                "probe_source": "packaged_smoke",
            }
        ],
        "api_key_prefix": "sk-",
        "is_local": True,
        "freeze_url": False,
        "require_api_key": True,
        "is_custom": True,
        "support_model_discovery": False,
        "support_connection_check": False,
    }
    active_payload = {
        "provider_id": SMOKE_MODEL_PROVIDER_ID,
        "model": SMOKE_MODEL_ID,
    }
    provider_path.write_text(
        json.dumps(provider_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    active_model_path.write_text(
        json.dumps(active_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with contextlib.suppress(OSError):
        os.chmod(custom_dir, 0o700)
        os.chmod(provider_path, 0o600)
        os.chmod(active_model_path, 0o600)
    return {
        "provider_id": SMOKE_MODEL_PROVIDER_ID,
        "model": SMOKE_MODEL_ID,
        "base_url": base_url,
        "provider_path": str(provider_path),
        "active_model_path": str(active_model_path),
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _install_termination_signal_handlers() -> Callable[[], None]:
    """Make controller termination unwind through the normal app cleanup path."""

    previous_handlers: dict[int, Any] = {}

    def restore() -> None:
        for signal_number, previous in previous_handlers.items():
            signal.signal(signal_number, previous)

    def handle_termination(signal_number: int, _frame: Any) -> None:
        # Restore first so a second termination request keeps its normal forceful
        # semantics instead of recursively re-entering Python cleanup.
        restore()
        signal_name = signal.Signals(signal_number).name
        raise RuntimeError(
            f"Packaged lifecycle smoke controller received {signal_name}"
        )

    for signal_name in ("SIGHUP", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        previous_handlers[int(signal_number)] = signal.getsignal(signal_number)
        signal.signal(signal_number, handle_termination)
    return restore


def _json_request(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, str, Any]:
    data = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=request_headers, method=method)
    with request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        parsed: Any = None
        if body.strip():
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(body)
        return resp.status, body, parsed


def _json_request_expect_http_error(
    method: str,
    url: str,
    *,
    expected_status: int,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> Any:
    try:
        status, body, parsed = _json_request(
            method,
            url,
            payload=payload,
            headers=headers,
            timeout=timeout,
        )
    except error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
        parsed = None
        if body.strip():
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(body)
    if status != expected_status:
        raise AssertionError(
            f"Expected HTTP {expected_status} from {url}, got {status}: {body!r}"
        )
    return parsed


def _tail_text(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except FileNotFoundError:
        return ""


def _find_app_executable(app_path: Path) -> Path:
    info_plist = app_path / "Contents" / "Info.plist"
    if not info_plist.is_file():
        raise FileNotFoundError(f"Info.plist not found under app bundle: {app_path}")
    with info_plist.open("rb") as handle:
        info = plistlib.load(handle)
    executable_name = str(info.get("CFBundleExecutable") or "")
    if not executable_name:
        raise ValueError(f"CFBundleExecutable missing in {info_plist}")
    executable = app_path / "Contents" / "MacOS" / executable_name
    if not executable.is_file():
        raise FileNotFoundError(f"App executable not found: {executable}")
    return executable


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _backend_executable_for_evidence(app_path: Path) -> Path:
    """Resolve current Helper bundles and the legacy 2.0.26 backend layout."""

    backend_root = app_path / "Contents" / "Resources" / "lantai-server"
    candidates = (
        backend_root
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server",
        backend_root / "lantai-server",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return candidates[0]


def _app_metadata_for_evidence(app_path: Path) -> dict[str, Any]:
    info_plist = app_path / "Contents" / "Info.plist"
    with info_plist.open("rb") as handle:
        info = plistlib.load(handle)
    if not isinstance(info, dict):
        raise ValueError(f"Invalid Info.plist: {info_plist}")
    executable_name = str(info.get("CFBundleExecutable") or "")
    artifact_paths = {
        "main_executable": app_path / "Contents" / "MacOS" / executable_name,
        "app_asar": app_path / "Contents" / "Resources" / "app.asar",
        "backend_executable": _backend_executable_for_evidence(app_path),
    }
    artifact_sha256: dict[str, str] = {}
    for artifact_id, artifact_path in artifact_paths.items():
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise FileNotFoundError(
                f"Required packaged artifact missing for evidence: {artifact_path}"
            )
        artifact_sha256[artifact_id] = _sha256_file(artifact_path)
    return {
        "path": str(app_path),
        "bundle_identifier": info.get("CFBundleIdentifier"),
        "short_version": info.get("CFBundleShortVersionString"),
        "bundle_version": info.get("CFBundleVersion"),
        "executable": info.get("CFBundleExecutable"),
        "artifact_sha256": artifact_sha256,
    }


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _wait_for_state(
    path: Path,
    *,
    timeout: float,
    predicate: Callable[[dict[str, Any]], bool],
    process: subprocess.Popen[str] | None = None,
    expected_distribution: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = _read_state(path)
        if payload:
            last_payload = payload
            if predicate(payload):
                return payload
        if process is not None:
            returncode = process.poll()
            if returncode is not None:
                raise RuntimeError(
                    "Packaged app process exited before writing lifecycle smoke "
                    f"state (exit code {returncode})."
                    f"{_direct_launch_hint(expected_distribution)}"
                )
        time.sleep(0.25)
    raise TimeoutError(
        "Timed out waiting for packaged app lifecycle smoke state. "
        f"Last payload: {last_payload!r}"
        f"{_direct_launch_hint(expected_distribution)}"
    )


def _is_new_desktop_instance(
    state: dict[str, Any],
    *,
    previous_nonce: str,
) -> bool:
    nonce = str(state.get("desktop_instance_nonce") or "").strip()
    return bool(nonce) and nonce != previous_nonce


def _direct_launch_hint(expected_distribution: str | None) -> str:
    if expected_distribution != "mas":
        return ""
    return (
        "\nMAS distribution note: App Store distribution-signed .app bundles "
        "usually cannot be launched directly from build output on macOS. Use "
        "a mas-dev build for local sandbox runtime smoke, or install the app "
        "through TestFlight/App Store before running installed-app lifecycle "
        "checks. If this was expected to launch locally, inspect "
        "`/usr/bin/log show` for AMFI `No matching profile found` messages."
    )


def _state_headers(state: dict[str, Any]) -> dict[str, str]:
    token = str(state.get("desktop_token") or "")
    if not token:
        raise AssertionError(f"Smoke state did not include desktop_token: {state!r}")
    return {DESKTOP_TOKEN_HEADER: token}


def _state_base_url(state: dict[str, Any]) -> str:
    base_url = str(state.get("api_base_url") or "")
    if not base_url.startswith("http://127.0.0.1:"):
        raise AssertionError(f"Smoke state did not include loopback api_base_url: {state!r}")
    return base_url


def _assert_ready_state(
    state: dict[str, Any],
    *,
    expected_distribution: str,
) -> None:
    if state.get("is_packaged") is not True:
        raise AssertionError(f"Expected packaged Electron app state, got {state!r}")
    if state.get("distribution_channel") != expected_distribution:
        raise AssertionError(
            "Packaged app distribution mismatch: "
            f"expected {expected_distribution!r}, got {state.get('distribution_channel')!r}"
        )
    pid = state.get("backend_pid")
    if not isinstance(pid, int) or pid <= 0:
        raise AssertionError(f"Smoke state did not include a backend pid: {state!r}")
    if not state.get("desktop_instance_nonce"):
        raise AssertionError(f"Smoke state did not include desktop_instance_nonce: {state!r}")


def _assert_desktop_readiness(state: dict[str, Any], *, timeout: float) -> int:
    base_url = _state_base_url(state)
    _status, _body, payload = _json_request(
        "GET",
        f"{base_url}/api/desktop/readiness",
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected desktop readiness object, got {payload!r}")
    if payload.get("status") != "ok" or payload.get("desktop") is not True:
        raise AssertionError(f"Desktop readiness proof is not ok: {payload!r}")
    if payload.get("instance_nonce") != state.get("desktop_instance_nonce"):
        raise AssertionError(
            "Desktop readiness proof nonce mismatch: "
            f"state={state.get('desktop_instance_nonce')!r} payload={payload!r}"
        )
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise AssertionError(f"Desktop readiness proof has invalid pid: {payload!r}")
    return pid


def _extract_sse_payloads(text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw)
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


def _find_chat_id(chats: Any, session_id: str) -> str:
    if not isinstance(chats, list):
        raise AssertionError(f"Expected /api/chats list, got {chats!r}")
    for chat in chats:
        if isinstance(chat, dict) and chat.get("session_id") == session_id:
            chat_id = str(chat.get("id") or "")
            if chat_id:
                return chat_id
    raise AssertionError(f"Chat for session_id={session_id!r} was not listed")


def _assert_failure_events(events_payload: Any) -> None:
    if not isinstance(events_payload, dict):
        raise AssertionError(f"Expected runtime events object, got {events_payload!r}")
    events = events_payload.get("events")
    if not isinstance(events, list) or not events:
        raise AssertionError("Runtime events response did not include any events")
    final = events[-1]
    if not isinstance(final, dict):
        raise AssertionError(f"Final runtime event is not an object: {final!r}")
    if final.get("type") != "turn.failed" or final.get("status") != "failed":
        raise AssertionError(f"Expected final turn.failed event, got {final!r}")
    payload = final.get("payload") if isinstance(final.get("payload"), dict) else {}
    if payload.get("final_task_status") != "failed":
        raise AssertionError(f"Expected final_task_status=failed, got {payload!r}")


def _runtime_event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    content = message.get("content")
    chunks: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
    for key in ("text", "delta"):
        value = payload.get(key)
        if isinstance(value, str):
            chunks.append(value)
    summary = event.get("summary")
    if isinstance(summary, str):
        chunks.append(summary)
    return "\n".join(chunk for chunk in chunks if chunk)


def _contains_exact_string(value: Any, expected: str) -> bool:
    """Return whether a nested transport payload contains one visible summary.

    Comparing the JSON-encoded wire dump is insufficient for multiline text because
    encoding turns newlines into escape sequences. Walk the decoded payload instead
    so the gate verifies that Runtime did not replace or truncate a healthy provider
    reasoning summary. Narrow secret/corruption filtering is covered separately.
    """

    if isinstance(value, str):
        return expected in value
    if isinstance(value, dict):
        return any(_contains_exact_string(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_string(item, expected) for item in value)
    return False


def _assert_success_events(events_payload: Any) -> None:
    if not isinstance(events_payload, dict):
        raise AssertionError(f"Expected runtime events object, got {events_payload!r}")
    events = events_payload.get("events")
    if not isinstance(events, list) or not events:
        raise AssertionError("Runtime events response did not include any events")
    objects = [event for event in events if isinstance(event, dict)]
    event_types = [str(event.get("type") or "") for event in objects]
    if "turn.started" not in event_types:
        raise AssertionError(f"Success path did not persist turn.started: {events_payload!r}")
    if "message.started" not in event_types:
        raise AssertionError(f"Success path did not persist message.started: {events_payload!r}")
    if "message.delta" not in event_types:
        raise AssertionError(f"Success path did not persist message.delta: {events_payload!r}")
    if "message.completed" not in event_types:
        raise AssertionError(f"Success path did not persist message.completed: {events_payload!r}")
    terminal = next(
        (event for event in reversed(objects) if _is_terminal_runtime_event(event)),
        None,
    )
    if terminal is None:
        raise AssertionError(f"Success path did not persist a terminal event: {events_payload!r}")
    if terminal.get("type") != "turn.completed":
        raise AssertionError(f"Expected final turn.completed event, got {terminal!r}")
    if str(terminal.get("status") or "") not in {"completed", "finished"}:
        raise AssertionError(f"Expected completed/finished terminal status, got {terminal!r}")
    all_text = "\n".join(_runtime_event_text(event) for event in objects)
    if SMOKE_MODEL_THINKING_TEXT not in all_text:
        raise AssertionError(
            "Success path did not preserve provider reasoning content: "
            f"{events_payload!r}"
        )
    if SMOKE_FORBIDDEN_FABRICATED_PROGRESS_TEXT in all_text:
        raise AssertionError(
            "Success path fabricated public progress without runtime facts: "
            f"{events_payload!r}"
        )
    if SMOKE_FORBIDDEN_FIXED_REASONING_STATUS in all_text:
        raise AssertionError(
            "Success path replaced provider reasoning with a fixed status: "
            f"{events_payload!r}"
        )
    if SMOKE_MODEL_REPLY_PREFIX not in all_text:
        raise AssertionError(
            "Success path did not persist streamed assistant content: "
            f"{events_payload!r}"
        )


def _runtime_event_stream_url(
    state: dict[str, Any],
    *,
    chat_id: str,
    after_seq: int,
) -> str:
    encoded_chat_id = parse.quote(chat_id, safe="")
    return f"{_state_base_url(state)}/api/chats/{encoded_chat_id}/events/stream?after_seq={after_seq}"


def _runtime_event_batch_url(
    state: dict[str, Any],
    *,
    chat_id: str,
    after_seq: int,
) -> str:
    encoded_chat_id = parse.quote(chat_id, safe="")
    return f"{_state_base_url(state)}/api/chats/{encoded_chat_id}/events?after_seq={after_seq}"


def _parse_sse_data_lines(data_lines: list[str], *, source: str) -> dict[str, Any]:
    raw = "\n".join(data_lines).strip()
    if not raw:
        raise AssertionError(f"Empty SSE data frame from {source}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Malformed SSE JSON frame from {source}: {raw!r}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected SSE object frame from {source}, got {payload!r}")
    return payload


def _runtime_events_from_stream_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("events")
    if not isinstance(events, list):
        raise AssertionError(f"Runtime event stream frame omitted events list: {payload!r}")
    if not all(isinstance(event, dict) for event in events):
        raise AssertionError(f"Runtime event stream frame included non-object events: {payload!r}")
    return list(events)


def _runtime_event_seq(event: dict[str, Any]) -> int:
    seq = event.get("seq")
    if not isinstance(seq, int) or seq < 0:
        raise AssertionError(f"Runtime event has invalid seq: {event!r}")
    return seq


def _runtime_event_response_latest_seq(payload: dict[str, Any]) -> int:
    latest = payload.get("next_after_seq")
    if latest is None:
        latest = payload.get("latest_seq")
    if not isinstance(latest, int) or latest < 0:
        raise AssertionError(f"Runtime event stream frame has invalid cursor: {payload!r}")
    return latest


def _is_terminal_runtime_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    status = str(event.get("status") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    final_task_status = str(payload.get("final_task_status") or "")
    return (
        event_type
        in {
            "turn.completed",
            "turn.failed",
            "turn.cancelled",
            "turn.interrupted",
            "turn.stalled",
        }
        or status
        in {
            "completed",
            "failed",
            "finished",
            "cancelled",
            "interrupted",
            "stalled",
        }
        or final_task_status
        in {
            "completed",
            "failed",
            "finished",
            "cancelled",
            "interrupted",
            "stalled",
        }
    )


def _read_runtime_event_stream_payloads(
    state: dict[str, Any],
    *,
    chat_id: str,
    after_seq: int,
    timeout: float,
    stop_when: Callable[[list[dict[str, Any]]], bool],
    label: str,
) -> list[dict[str, Any]]:
    url = _runtime_event_stream_url(state, chat_id=chat_id, after_seq=after_seq)
    headers = {"Accept": "text/event-stream", **_state_headers(state)}
    req = request.Request(url, headers=headers, method="GET")
    deadline = time.monotonic() + timeout
    payloads: list[dict[str, Any]] = []
    data_lines: list[str] = []
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            while time.monotonic() < deadline:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text:
                    if data_lines:
                        payload = _parse_sse_data_lines(data_lines, source=url)
                        payloads.append(payload)
                        data_lines = []
                        if stop_when(payloads):
                            return payloads
                    continue
                if text.startswith("data:"):
                    data_lines.append(text[5:].lstrip())
            if data_lines:
                payload = _parse_sse_data_lines(data_lines, source=url)
                payloads.append(payload)
                if stop_when(payloads):
                    return payloads
    except TimeoutError as exc:
        raise AssertionError(
            f"Timed out reading {label} from runtime event stream after_seq={after_seq}"
        ) from exc
    raise AssertionError(
        f"Runtime event stream did not produce expected {label} "
        f"after_seq={after_seq}; payloads={payloads!r}"
    )


def _runtime_stream_events(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for payload in payloads:
        events.extend(_runtime_events_from_stream_payload(payload))
    events.sort(key=_runtime_event_seq)
    return events


def _latest_runtime_stream_cursor(payloads: list[dict[str, Any]]) -> int:
    if not payloads:
        raise AssertionError("Runtime event stream did not return any payloads")
    return max(_runtime_event_response_latest_seq(payload) for payload in payloads)


def _assert_runtime_event_stream_reconnect_recovers_terminal(
    state: dict[str, Any],
    *,
    chat_id: str,
    timeout: float,
    expected_terminal_type: str = "turn.failed",
    expected_terminal_statuses: set[str] | None = None,
    expected_final_task_statuses: set[str] | None = None,
) -> None:
    if expected_terminal_statuses is None:
        expected_terminal_statuses = {"failed"}
    if expected_final_task_statuses is None:
        expected_final_task_statuses = {"failed"}
    initial_payloads = _read_runtime_event_stream_payloads(
        state,
        chat_id=chat_id,
        after_seq=0,
        timeout=timeout,
        stop_when=lambda payloads: bool(_runtime_stream_events(payloads)),
        label="initial event batch",
    )
    initial_events = _runtime_stream_events(initial_payloads)
    if not initial_events:
        raise AssertionError("Initial runtime event stream frame had no events")
    initial_latest = _latest_runtime_stream_cursor(initial_payloads)
    terminal_events = [
        event for event in initial_events if _is_terminal_runtime_event(event)
    ]
    if terminal_events:
        # If the packaged no-model failure settles too quickly, reconnect from
        # just before the terminal event. This still verifies stream replay,
        # cursor fencing, and terminal-state recovery over a fresh connection.
        reconnect_after = max(0, _runtime_event_seq(terminal_events[-1]) - 1)
    else:
        reconnect_after = initial_latest

    reconnect_payloads = _read_runtime_event_stream_payloads(
        state,
        chat_id=chat_id,
        after_seq=reconnect_after,
        timeout=timeout,
        stop_when=lambda payloads: any(
            _is_terminal_runtime_event(event)
            for event in _runtime_stream_events(payloads)
        ),
        label="terminal event after reconnect",
    )
    reconnect_events = _runtime_stream_events(reconnect_payloads)
    for event in reconnect_events:
        seq = _runtime_event_seq(event)
        if seq <= reconnect_after:
            raise AssertionError(
                "Runtime event stream replayed stale events after reconnect: "
                f"after_seq={reconnect_after} event={event!r}"
            )
    terminal = next(
        (event for event in reversed(reconnect_events) if _is_terminal_runtime_event(event)),
        None,
    )
    if terminal is None:
        raise AssertionError(
            "Runtime event stream reconnect did not recover a terminal event: "
            f"payloads={reconnect_payloads!r}"
        )
    if terminal.get("type") != expected_terminal_type:
        raise AssertionError(
            f"Expected {expected_terminal_type} terminal runtime event, got {terminal!r}"
        )
    if str(terminal.get("status") or "") not in expected_terminal_statuses:
        raise AssertionError(
            "Terminal runtime event status mismatch: "
            f"expected={sorted(expected_terminal_statuses)!r} got={terminal!r}"
        )
    payload = terminal.get("payload") if isinstance(terminal.get("payload"), dict) else {}
    final_task_status = str(payload.get("final_task_status") or "")
    if final_task_status and final_task_status not in expected_final_task_statuses:
        raise AssertionError(
            "Terminal runtime event final_task_status mismatch: "
            f"expected={sorted(expected_final_task_statuses)!r} got={terminal!r}"
        )

    latest_cursor = max(
        initial_latest,
        _latest_runtime_stream_cursor(reconnect_payloads),
    )
    _status, _body, no_replay_payload = _json_request(
        "GET",
        _runtime_event_batch_url(state, chat_id=chat_id, after_seq=latest_cursor),
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(no_replay_payload, dict):
        raise AssertionError(
            f"Expected runtime event no-replay object, got {no_replay_payload!r}"
        )
    no_replay_events = no_replay_payload.get("events")
    if no_replay_events:
        raise AssertionError(
            "Runtime events endpoint returned duplicates after latest cursor: "
            f"after_seq={latest_cursor} payload={no_replay_payload!r}"
        )


def _assert_distribution_tools(
    state: dict[str, Any],
    *,
    expected_distribution: str,
    timeout: float,
) -> None:
    _status, _body, tools_payload = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/tools",
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(tools_payload, list):
        raise AssertionError(f"Expected /api/tools list, got {tools_payload!r}")
    tool_names = {
        str(item.get("name") or "")
        for item in tools_payload
        if isinstance(item, dict)
    }
    forbidden = {"execute_python_code"} & tool_names
    if expected_distribution == "mas":
        forbidden |= {"execute_shell_command"} & tool_names
    if forbidden:
        label = "MAS" if expected_distribution == "mas" else "packaged"
        raise AssertionError(
            f"{label} app exposed forbidden local execution tools: {sorted(forbidden)}"
        )


def _capability_entry(
    browser_state: dict[str, Any],
    capability_id: str,
) -> dict[str, Any]:
    matrix = browser_state.get("capability_matrix")
    if not isinstance(matrix, dict):
        raise AssertionError(
            f"Browser workbench state did not include capability_matrix: {browser_state!r}"
        )
    entry = matrix.get(capability_id)
    if not isinstance(entry, dict):
        raise AssertionError(
            f"Browser capability {capability_id!r} missing from matrix: {matrix!r}"
        )
    return entry


def _assert_capability_mode(
    browser_state: dict[str, Any],
    capability_id: str,
    *,
    mode: str,
    reason_code: str | None = None,
) -> None:
    entry = _capability_entry(browser_state, capability_id)
    if entry.get("mode") != mode:
        raise AssertionError(
            f"Browser capability {capability_id!r} expected mode {mode!r}, got {entry!r}"
        )
    if reason_code is not None and entry.get("reason_code") != reason_code:
        raise AssertionError(
            "Browser capability "
            f"{capability_id!r} expected reason {reason_code!r}, got {entry!r}"
        )


def _assert_browser_capabilities(
    state: dict[str, Any],
    *,
    expected_distribution: str,
    timeout: float,
) -> None:
    _status, _body, browser_state = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/browser-workbench/state",
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(browser_state, dict):
        raise AssertionError(
            f"Expected browser workbench state object, got {browser_state!r}"
        )
    config = browser_state.get("config")
    if not isinstance(config, dict):
        raise AssertionError(f"Browser workbench state did not include config: {browser_state!r}")
    app_store = bool(config.get("app_store_distribution"))
    if expected_distribution == "mas":
        if not app_store:
            raise AssertionError(f"Expected MAS browser config, got {config!r}")
        _assert_capability_mode(
            browser_state,
            "browser_read_navigation",
            mode="read_only",
            reason_code="mas_browser_read_only",
        )
        _assert_capability_mode(
            browser_state,
            "browser_write",
            mode="disabled",
            reason_code="mas_browser_write_disabled",
        )
        _assert_capability_mode(
            browser_state,
            "background_scheduled_execution",
            mode="interactive_only",
            reason_code="mas_background_execution_limited",
        )
    else:
        if app_store:
            raise AssertionError(f"Expected official browser config, got {config!r}")
        _assert_capability_mode(
            browser_state,
            "browser_read_navigation",
            mode="enabled",
            reason_code="developer_id_allowed",
        )
        _assert_capability_mode(
            browser_state,
            "browser_write",
            mode="interactive_only",
            reason_code="developer_id_browser_write_confirmation_required",
        )
        _assert_capability_mode(
            browser_state,
            "background_scheduled_execution",
            mode="enabled",
            reason_code="developer_id_allowed",
        )


def _assert_persisted_failure_events(
    state: dict[str, Any],
    *,
    session_id: str,
    timeout: float,
) -> str:
    base_url = _state_base_url(state)
    headers = _state_headers(state)
    _status, _body, chats = _json_request(
        "GET",
        f"{base_url}/api/chats?mode=all",
        headers=headers,
        timeout=timeout,
    )
    chat_id = _find_chat_id(chats, session_id)
    _status, _body, events_payload = _json_request(
        "GET",
        f"{base_url}/api/chats/{chat_id}/events?after_seq=0",
        headers=headers,
        timeout=timeout,
    )
    _assert_failure_events(events_payload)
    return chat_id


def _chat_history_text(chat_payload: Any) -> str:
    if not isinstance(chat_payload, dict):
        return ""
    messages = chat_payload.get("messages")
    if not isinstance(messages, list):
        return ""
    chunks: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
    return "\n".join(chunk for chunk in chunks if chunk)


def _assert_persisted_success_events(
    state: dict[str, Any],
    *,
    session_id: str,
    timeout: float,
) -> str:
    base_url = _state_base_url(state)
    headers = _state_headers(state)
    _status, _body, chats = _json_request(
        "GET",
        f"{base_url}/api/chats?mode=all",
        headers=headers,
        timeout=timeout,
    )
    chat_id = _find_chat_id(chats, session_id)
    _status, _body, events_payload = _json_request(
        "GET",
        f"{base_url}/api/chats/{chat_id}/events?after_seq=0",
        headers=headers,
        timeout=timeout,
    )
    _assert_success_events(events_payload)
    _status, _body, chat_payload = _json_request(
        "GET",
        f"{base_url}/api/chats/{chat_id}",
        headers=headers,
        timeout=timeout,
    )
    if not isinstance(chat_payload, dict):
        raise AssertionError(f"Expected chat history object, got {chat_payload!r}")
    if chat_payload.get("pending_user_message"):
        raise AssertionError(
            "Success path left a stale pending user message: "
            f"{chat_payload!r}"
        )
    if str(chat_payload.get("status") or "") not in {"idle", "finished", "completed"}:
        raise AssertionError(f"Success path chat status did not settle: {chat_payload!r}")
    history_text = _chat_history_text(chat_payload)
    if SMOKE_MODEL_REPLY_PREFIX not in history_text:
        raise AssertionError(
            "Success path did not persist assistant text into chat history: "
            f"{chat_payload!r}"
        )
    return chat_id


def _assert_terminal_interrupt_is_noop(
    state: dict[str, Any],
    *,
    chat_id: str,
    timeout: float,
) -> None:
    _status, _body, payload = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{chat_id}/interrupt",
        payload={},
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected runtime interrupt object, got {payload!r}")
    if payload.get("run_id") != chat_id:
        raise AssertionError(f"Runtime interrupt run_id mismatch: {payload!r}")
    if payload.get("stopped") is not False:
        raise AssertionError(
            "Interrupting a completed packaged smoke run should be a no-op, "
            f"got {payload!r}"
        )


def _assert_relaunched_chat_control_plane(
    state: dict[str, Any],
    *,
    chat_id: str,
    session_id: str,
    timeout: float,
) -> dict[str, bool]:
    """Recheck the settled chat control plane after a full Electron relaunch."""
    _assert_terminal_interrupt_is_noop(
        state,
        chat_id=chat_id,
        timeout=timeout,
    )
    _assert_canonical_runtime_controls(
        state,
        chat_id=chat_id,
        session_id=session_id,
        timeout=timeout,
        expected_statuses={"completed", "succeeded"},
    )
    return {
        "terminal_interrupt_noop": True,
        "canonical_runtime_controls": True,
    }


def _runtime_run_items(
    state: dict[str, Any],
    *,
    timeout: float,
) -> list[dict[str, Any]]:
    _status, _body, payload = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/runtime/runs",
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected runtime runs response object, got {payload!r}")
    items = payload.get("items")
    if not isinstance(items, list):
        raise AssertionError(f"Runtime runs response did not include items: {payload!r}")
    return [item for item in items if isinstance(item, dict)]


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return records
    for line in lines:
        if not line.strip():
            continue
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _legacy_writer_observation_summary(
    path: Path,
    *,
    release_edition: str,
) -> dict[str, Any]:
    from lantai.app.runtime_legacy_writers import (
        summarize_legacy_runtime_writer_observations,
    )

    summary = summarize_legacy_runtime_writer_observations(
        path,
        release_edition=release_edition,
    )
    if summary.get("malformed_count") != 0:
        raise AssertionError(f"Malformed legacy writer observations: {summary!r}")
    if summary.get("unknown_schema_count") != 0:
        raise AssertionError(f"Unknown legacy writer observation schemas: {summary!r}")
    if summary.get("invalid_writer_ids"):
        raise AssertionError(f"Invalid legacy writer observations: {summary!r}")
    return summary


def _legacy_boundary_observation_summary(
    execution_path: Path,
    writer_path: Path,
    *,
    release_edition: str,
) -> dict[str, Any]:
    from lantai.app.runtime_legacy_execution_observations import (
        summarize_legacy_runtime_boundary_observations,
    )

    summary = summarize_legacy_runtime_boundary_observations(
        execution_path,
        writer_path,
        release_edition=release_edition,
    )
    if not summary.get("observation_files_exist"):
        raise AssertionError(f"Legacy boundary observation files missing: {summary!r}")
    if not summary.get("evidence_valid"):
        raise AssertionError(f"Invalid legacy boundary observations: {summary!r}")
    if not summary.get("zero_forbidden_legacy_activity"):
        raise AssertionError(f"Forbidden legacy Runtime activity: {summary!r}")
    return summary


def _is_settled_reminder_runtime_run(run: dict[str, Any]) -> bool:
    return (
        run.get("source") == "reminder"
        and run.get("authority_status") == "succeeded"
        and run.get("status") == "completed"
    )


def _assert_packaged_reminder_delivery_worker(
    state: dict[str, Any],
    *,
    smoke_file: Path,
    timeout: float,
) -> dict[str, Any]:
    base_url = _state_base_url(state)
    headers = _state_headers(state)
    due_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _status, _body, reminder = _json_request(
        "POST",
        f"{base_url}/api/reminders",
        payload={
            "title": "Packaged reminder delivery smoke",
            "summary": "Exercise reminder delivery worker inside packaged backend.",
            "priority": "high",
            "due_at": due_at,
        },
        headers=headers,
        timeout=timeout,
    )
    if not isinstance(reminder, dict) or not reminder.get("id"):
        raise AssertionError(f"Reminder create response was invalid: {reminder!r}")
    reminder_id = str(reminder["id"])

    deadline = time.monotonic() + timeout
    last_records: list[dict[str, Any]] = []
    last_runs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        records = _read_jsonl_records(smoke_file)
        deliveries = [
            record
            for record in records
            if record.get("kind") == "reminder_delivery"
            and record.get("reminder_id") == reminder_id
        ]
        ticks = [
            record
            for record in records
            if record.get("kind") == "reminder_delivery_tick"
        ]
        runs = _runtime_run_items(state, timeout=min(timeout, 10.0))
        if len(deliveries) == 1:
            run_id = str(deliveries[0].get("run_id") or "")
            run = next(
                (
                    item
                    for item in runs
                    if item.get("id") == run_id
                ),
                None,
            )
            if run is not None and _is_settled_reminder_runtime_run(run):
                return {
                    "reminder_id": reminder_id,
                    "run_id": run_id,
                    "tick_count": len(ticks),
                    "delivery_count": len(deliveries),
                    "revision": run.get("revision"),
                }
        last_records = records
        last_runs = runs
        time.sleep(0.25)

    raise AssertionError(
        "Packaged reminder delivery worker did not deliver and settle "
        f"reminder={reminder_id!r}. records={last_records!r} runs={last_runs!r}"
    )


def _assert_packaged_reminder_delivery_restart_deduped(
    state: dict[str, Any],
    *,
    smoke_file: Path,
    previous: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    reminder_id = str(previous.get("reminder_id") or "")
    run_id = str(previous.get("run_id") or "")
    previous_tick_count = int(previous.get("tick_count") or 0)
    deadline = time.monotonic() + timeout
    last_records: list[dict[str, Any]] = []
    last_runs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        records = _read_jsonl_records(smoke_file)
        deliveries = [
            record
            for record in records
            if record.get("kind") == "reminder_delivery"
            and record.get("reminder_id") == reminder_id
        ]
        ticks = [
            record
            for record in records
            if record.get("kind") == "reminder_delivery_tick"
        ]
        runs = _runtime_run_items(state, timeout=min(timeout, 10.0))
        matching_runs = [
            item
            for item in runs
            if item.get("id") == run_id
        ]
        if (
            len(ticks) > previous_tick_count
            and len(deliveries) == 1
            and len(matching_runs) == 1
            and _is_settled_reminder_runtime_run(matching_runs[0])
        ):
            return {
                "reminder_id": reminder_id,
                "run_id": run_id,
                "tick_count": len(ticks),
                "delivery_count": len(deliveries),
                "revision": matching_runs[0].get("revision"),
            }
        last_records = records
        last_runs = runs
        time.sleep(0.25)

    raise AssertionError(
        "Packaged reminder delivery worker did not prove post-restart dedupe. "
        f"previous={previous!r} records={last_records!r} runs={last_runs!r}"
    )


def _find_runtime_run_summary(
    state: dict[str, Any],
    *,
    chat_id: str,
    session_id: str,
    timeout: float,
    expected_statuses: set[str] | None = None,
) -> dict[str, Any]:
    if expected_statuses is None:
        expected_statuses = {"failed"}
    items = _runtime_run_items(state, timeout=timeout)
    for item in items:
        if item.get("chat_id") == chat_id and item.get("session_id") == session_id:
            run_id = str(item.get("id") or "")
            attempt_id = str(item.get("attempt_id") or "")
            workspace_dir = str(item.get("workspace_dir") or "")
            revision = item.get("revision")
            if not run_id or not attempt_id or not workspace_dir:
                raise AssertionError(f"Runtime run summary is missing identity: {item!r}")
            if not isinstance(revision, int) or revision < 0:
                raise AssertionError(f"Runtime run summary has invalid revision: {item!r}")
            if item.get("source") != "chat" or str(item.get("status") or "") not in expected_statuses:
                raise AssertionError(
                    "Expected chat runtime run with terminal status "
                    f"{sorted(expected_statuses)!r}, got {item!r}"
                )
            recovery = item.get("recovery") if isinstance(item.get("recovery"), dict) else {}
            if recovery.get("can_interrupt") is not False:
                raise AssertionError(
                    f"Terminal packaged smoke run should not be interruptible: {item!r}"
                )
            return item
    raise AssertionError(
        "Runtime run summary for packaged smoke chat was not listed: "
        f"chat_id={chat_id!r} session_id={session_id!r} items={items!r}"
    )


def _find_runtime_run_by_id(
    state: dict[str, Any],
    *,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    items = _runtime_run_items(state, timeout=timeout)
    for item in items:
        if item.get("id") == run_id:
            return item
    raise AssertionError(f"Runtime run {run_id!r} was not listed: {items!r}")


def _runtime_summary_by_id(
    state: dict[str, Any],
    *,
    run_ids: set[str],
    timeout: float,
) -> dict[str, dict[str, Any]]:
    items = _runtime_run_items(state, timeout=timeout)
    by_id = {
        str(item.get("id")): item
        for item in items
        if isinstance(item.get("id"), str) and item.get("id") in run_ids
    }
    missing = sorted(run_ids - set(by_id))
    if missing:
        raise AssertionError(
            f"Runtime runs were not listed after packaged refresh: {missing!r}; "
            f"items={items!r}"
        )
    return by_id


def _runtime_summary_error_code(summary: dict[str, Any]) -> str | None:
    debug_refs = summary.get("debug_refs")
    if isinstance(debug_refs, dict):
        value = debug_refs.get("error_code") or debug_refs.get("stop_reason_code")
        if value:
            return str(value)
    recovery = summary.get("recovery")
    if isinstance(recovery, dict):
        value = recovery.get("reason_code") or recovery.get("error_code")
        if value:
            return str(value)
    text = str(summary.get("summary") or "")
    if text.startswith("Loop lease expired"):
        return "loop_lease_expired"
    if text.startswith("Loop watchdog deadline expired"):
        return "loop_watchdog_deadline_expired"
    if text.startswith("Loop no-progress budget exhausted"):
        return "loop_no_progress_exhausted"
    return None


async def _write_packaged_restart_decision_contracts(
    workspace_dir: Path,
    *,
    run_ids: dict[str, str],
) -> None:
    from lantai.runtime import RuntimeBudget, RuntimeRunContract, RuntimeRunOwner
    from lantai.runtime.store import RuntimeRunStore

    now = datetime.now(timezone.utc)
    owner = RuntimeRunOwner(
        user_id="default",
        agent_id="agent-default",
        session_id="console:packaged-restart-decision",
        workspace_id=str(workspace_dir),
        channel="console",
    )
    store = RuntimeRunStore(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    contracts = [
        RuntimeRunContract(
            run_id=run_ids["expired_lease"],
            attempt_id=f"{run_ids['expired_lease']}:attempt-1",
            source="chat",
            owner=owner,
            status="running",
            revision=2,
            lease_owner="packaged-smoke:expired-lease",
            lease_expires_at=now - timedelta(seconds=5),
            created_at=now - timedelta(minutes=5),
            started_at=now - timedelta(minutes=4),
            heartbeat_at=now - timedelta(seconds=10),
            budget=RuntimeBudget(deadline_at=None),
            contract_hash="sha256:packaged-restart-decision-contract",
            policy_snapshot_hash="sha256:packaged-restart-decision-policy",
            release_edition="official",
        ),
        RuntimeRunContract(
            run_id=run_ids["expired_deadline"],
            attempt_id=f"{run_ids['expired_deadline']}:attempt-1",
            source="chat",
            owner=owner,
            status="running",
            revision=2,
            lease_owner="packaged-smoke:expired-deadline",
            lease_expires_at=now + timedelta(minutes=5),
            created_at=now - timedelta(minutes=5),
            started_at=now - timedelta(minutes=4),
            heartbeat_at=now - timedelta(seconds=10),
            budget=RuntimeBudget(deadline_at=now - timedelta(seconds=5)),
            contract_hash="sha256:packaged-restart-decision-contract",
            policy_snapshot_hash="sha256:packaged-restart-decision-policy",
            release_edition="official",
        ),
        RuntimeRunContract(
            run_id=run_ids["healthy"],
            attempt_id=f"{run_ids['healthy']}:attempt-1",
            source="chat",
            owner=owner,
            status="running",
            revision=2,
            lease_owner="packaged-smoke:healthy",
            lease_expires_at=now + timedelta(minutes=5),
            created_at=now - timedelta(minutes=5),
            started_at=now - timedelta(minutes=4),
            heartbeat_at=now,
            budget=RuntimeBudget(deadline_at=now + timedelta(minutes=5)),
            contract_hash="sha256:packaged-restart-decision-contract",
            policy_snapshot_hash="sha256:packaged-restart-decision-policy",
            release_edition="official",
        ),
    ]
    for contract in contracts:
        await store.save_contract(contract, expected_revision=None)


def _seed_packaged_restart_decision_contracts(workspace_dir: Path) -> dict[str, str]:
    prefix = f"packaged-restart-decision-{uuid.uuid4().hex[:8]}"
    run_ids = {
        "expired_lease": f"{prefix}-expired-lease",
        "expired_deadline": f"{prefix}-expired-deadline",
        "healthy": f"{prefix}-healthy",
    }
    asyncio.run(
        _write_packaged_restart_decision_contracts(
            workspace_dir,
            run_ids=run_ids,
        )
    )
    return run_ids


def _assert_packaged_restart_decision_summaries(
    state: dict[str, Any],
    *,
    run_ids: dict[str, str],
    timeout: float,
) -> None:
    by_id = _runtime_summary_by_id(
        state,
        run_ids=set(run_ids.values()),
        timeout=timeout,
    )
    expired_lease = by_id[run_ids["expired_lease"]]
    if expired_lease.get("status") != "stalled":
        raise AssertionError(f"Expired lease run did not stall: {expired_lease!r}")
    if int(expired_lease.get("revision") or 0) < 3:
        raise AssertionError(f"Expired lease run revision did not advance: {expired_lease!r}")
    if _runtime_summary_error_code(expired_lease) != "loop_lease_expired":
        raise AssertionError(f"Expired lease run reason mismatch: {expired_lease!r}")

    expired_deadline = by_id[run_ids["expired_deadline"]]
    if expired_deadline.get("status") != "stalled":
        raise AssertionError(
            "Expired deadline run was not projected as awaiting recovery: "
            f"{expired_deadline!r}"
        )
    if expired_deadline.get("authority_status") != "paused":
        raise AssertionError(
            "Expired deadline run did not preserve paused Runtime authority: "
            f"{expired_deadline!r}"
        )
    if int(expired_deadline.get("revision") or 0) < 3:
        raise AssertionError(
            f"Expired deadline run revision did not advance: {expired_deadline!r}"
        )
    if _runtime_summary_error_code(expired_deadline) is not None:
        raise AssertionError(
            "Expired deadline run was incorrectly reported as a terminal error: "
            f"{expired_deadline!r}"
        )

    healthy = by_id[run_ids["healthy"]]
    if healthy.get("status") != "running":
        raise AssertionError(f"Healthy restart-decision run did not remain running: {healthy!r}")
    if int(healthy.get("revision") or 0) != 2:
        raise AssertionError(f"Healthy restart-decision run revision changed: {healthy!r}")


def _assert_packaged_restart_decision_refresh(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    timeout: float,
) -> dict[str, str]:
    run_ids = _seed_packaged_restart_decision_contracts(workspace_dir)
    _assert_packaged_restart_decision_summaries(
        state,
        run_ids=run_ids,
        timeout=timeout,
    )
    return run_ids


def _run_packaged_loop_trial_once(
    workspace_dir: Path,
    *,
    run_id: str,
    max_iterations: int,
    release_edition: str,
) -> dict[str, Any]:
    from lantai.runtime.loop_trial import run_loop_trial

    if release_edition not in {"official", "mas"}:
        raise AssertionError(f"Unsupported LOOP trial release edition: {release_edition!r}")
    edition = cast(Literal["official", "mas"], release_edition)
    result = asyncio.run(
        run_loop_trial(
            workspace_dir,
            run_id=run_id,
            max_iterations=max_iterations,
            release_edition=edition,
        )
    )
    return {
        "run_id": result.run_id,
        "attempt_id": result.attempt_id,
        "status": result.status,
        "revision": result.revision,
        "iterations_executed": result.iterations_executed,
        "error_code": result.error_code,
        "progress": result.progress.model_dump(mode="json"),
    }


def _assert_loop_trial_summary(
    state: dict[str, Any],
    *,
    run_id: str,
    expected_status: str,
    timeout: float,
    expected_error_code: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    summary: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
        if summary.get("status") == expected_status:
            break
        time.sleep(0.1)
    if summary is None or summary.get("status") != expected_status:
        raise AssertionError(
            f"LOOP trial run {run_id!r} did not become {expected_status!r}: "
            f"{summary!r}"
        )
    if summary.get("source") != "capability":
        raise AssertionError(f"LOOP trial should project as capability source: {summary!r}")
    if expected_error_code is not None:
        actual = _runtime_summary_error_code(summary)
        if actual != expected_error_code:
            raise AssertionError(
                f"LOOP trial error code mismatch: expected {expected_error_code!r}, "
                f"got {actual!r}; summary={summary!r}"
            )
    return summary


def _assert_loop_waiting_approval_projection(
    state: dict[str, Any],
    *,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    summary = _assert_loop_trial_summary(
        state,
        run_id=run_id,
        expected_status="running",
        timeout=timeout,
    )
    if summary.get("authority_status") != "waiting_approval":
        raise AssertionError(
            f"Expected LOOP trial authority_status=waiting_approval, got {summary!r}"
        )
    if summary.get("pending_approval") is not True:
        raise AssertionError(f"Expected LOOP trial pending_approval=true, got {summary!r}")
    current_step = (
        summary.get("current_step")
        if isinstance(summary.get("current_step"), dict)
        else {}
    )
    if current_step.get("kind") != "approval":
        raise AssertionError(f"Expected approval current_step, got {summary!r}")
    return summary


def _assert_loop_succeeded_projection(
    state: dict[str, Any],
    *,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    summary = _assert_loop_trial_summary(
        state,
        run_id=run_id,
        expected_status="completed",
        timeout=timeout,
    )
    if summary.get("authority_status") != "succeeded":
        raise AssertionError(
            f"Expected LOOP trial authority_status=succeeded, got {summary!r}"
        )
    if summary.get("pending_approval") is not False:
        raise AssertionError(f"Expected LOOP trial pending_approval=false, got {summary!r}")
    return summary


def _assert_loop_trial_export_package(
    state: dict[str, Any],
    *,
    summary: dict[str, Any],
    timeout: float,
    expected_contract_id: str = "loop-trial-contract",
    expected_report_id: str | None = None,
    reason: str = "packaged LOOP-05 recovery export smoke",
    version_smoke: str = "packaged-loop-trial",
) -> None:
    run_id = str(summary["id"])
    expected_report_id = expected_report_id or f"{run_id}-eval-2"
    export_payload = _runtime_control_payload_from_summary(summary)
    export_payload.update(
        {
            "approved": True,
            "reason": reason,
            "exported_by": "packaged-lifecycle-smoke",
            "version_info": {"smoke": version_smoke},
        }
    )
    _status, _body, exported = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/export",
        payload=export_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(exported, dict) or exported.get("exported") is not True:
        raise AssertionError(f"Expected LOOP trial export package, got {exported!r}")
    package = exported.get("package")
    if not isinstance(package, dict):
        raise AssertionError(f"LOOP trial export did not include package: {exported!r}")
    contract = package.get("contract")
    if not isinstance(contract, dict) or contract.get("contract_id") != expected_contract_id:
        raise AssertionError(f"LOOP trial export contract mismatch: {package!r}")
    runtime_run = package.get("runtime_run")
    if not isinstance(runtime_run, dict) or runtime_run.get("run_id") != run_id:
        raise AssertionError(f"LOOP trial export run mismatch: {package!r}")
    latest_evaluation = package.get("latest_evaluation")
    if not isinstance(latest_evaluation, dict):
        raise AssertionError(f"LOOP trial export did not include latest evaluation: {package!r}")
    if latest_evaluation.get("report_id") != expected_report_id:
        raise AssertionError(f"LOOP trial export evaluation mismatch: {package!r}")


def _assert_packaged_loop_trial_recovery(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    expected_distribution: str,
    timeout: float,
) -> str:
    run_id = f"packaged-loop-trial-{uuid.uuid4().hex[:8]}"
    first = _run_packaged_loop_trial_once(
        workspace_dir,
        run_id=run_id,
        max_iterations=1,
        release_edition=expected_distribution,
    )
    first_progress = first.get("progress") if isinstance(first.get("progress"), dict) else {}
    if first.get("status") != "running" or first_progress.get(
        "consecutive_no_progress_iterations"
    ) != 1:
        raise AssertionError(f"LOOP trial first phase did not persist running state: {first!r}")
    running_summary = _assert_loop_trial_summary(
        state,
        run_id=run_id,
        expected_status="running",
        timeout=timeout,
    )
    if running_summary.get("attempt_id") != first.get("attempt_id"):
        raise AssertionError(
            "Packaged backend projected a different LOOP trial attempt after "
            f"first phase: first={first!r} summary={running_summary!r}"
        )

    second = _run_packaged_loop_trial_once(
        workspace_dir,
        run_id=run_id,
        max_iterations=10,
        release_edition=expected_distribution,
    )
    second_progress = second.get("progress") if isinstance(second.get("progress"), dict) else {}
    if (
        second.get("status") != "stalled"
        or second.get("error_code") != "loop_no_progress_exhausted"
        or second_progress.get("consecutive_no_progress_iterations") != 2
    ):
        raise AssertionError(f"LOOP trial second phase did not settle correctly: {second!r}")
    stalled_summary = _assert_loop_trial_summary(
        state,
        run_id=run_id,
        expected_status="stalled",
        expected_error_code="loop_no_progress_exhausted",
        timeout=timeout,
    )
    if stalled_summary.get("attempt_id") != second.get("attempt_id"):
        raise AssertionError(
            "Packaged backend projected a different LOOP trial attempt after "
            f"second phase: second={second!r} summary={stalled_summary!r}"
        )
    _assert_loop_trial_export_package(
        state,
        summary=stalled_summary,
        timeout=timeout,
    )
    return run_id


def _packaged_browser_content_payload(run_id: str) -> dict[str, Any]:
    return {
        "draft": {
            "id": f"{run_id}-draft",
            "platform": "local_markdown",
            "title": "Packaged Browser content handoff smoke",
            "body": "Local draft body prepared for packaged desktop review.",
            "summary": "A local draft prepared for human review.",
            "status": "ready_for_review",
            "preview_url": f"preview://{run_id}",
        },
        "preview": {
            "url": f"preview://{run_id}",
            "status": "ready",
        },
        "queue_item": {
            "id": f"{run_id}-queue",
            "draft_id": f"{run_id}-draft",
            "platform": "browser_content_local_preview",
            "status": "needs_confirmation",
            "requires_confirmation": True,
        },
        "publish_action": {
            "action": "publish",
            "page_kind": "publish",
            "requires_human_gate": True,
            "requires_manual_takeover": True,
            "executed": False,
        },
    }


def _run_packaged_browser_content_trial_once(
    workspace_dir: Path,
    *,
    run_id: str,
    human_gate_decision: str,
    release_edition: str,
) -> dict[str, Any]:
    from lantai.runtime.loop_trial import run_browser_content_loop_trial

    if human_gate_decision not in {"pending", "approved", "rejected"}:
        raise AssertionError(
            f"Unsupported Browser content gate decision: {human_gate_decision!r}"
        )
    if release_edition not in {"official", "mas"}:
        raise AssertionError(
            f"Unsupported Browser content release edition: {release_edition!r}"
        )
    decision = cast(
        Literal["pending", "approved", "rejected"],
        human_gate_decision,
    )
    edition = cast(Literal["official", "mas"], release_edition)
    result = asyncio.run(
        run_browser_content_loop_trial(
            workspace_dir,
            payload=_packaged_browser_content_payload(run_id),
            run_id=run_id,
            human_gate_decision=decision,
            max_iterations=1,
            release_edition=edition,
        )
    )
    return {
        "run_id": result.run_id,
        "attempt_id": result.attempt_id,
        "status": result.status,
        "revision": result.revision,
        "iterations_executed": result.iterations_executed,
        "error_code": result.error_code,
        "evidence_hash": result.evidence_hash,
        "issues": list(result.issues),
        "progress": result.progress.model_dump(mode="json"),
    }


def _assert_packaged_browser_content_trial_handoff(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    expected_distribution: str,
    timeout: float,
) -> str:
    run_id = f"packaged-browser-content-{uuid.uuid4().hex[:8]}"
    pending = _run_packaged_browser_content_trial_once(
        workspace_dir,
        run_id=run_id,
        human_gate_decision="pending",
        release_edition=expected_distribution,
    )
    pending_progress = (
        pending.get("progress") if isinstance(pending.get("progress"), dict) else {}
    )
    if (
        pending.get("status") != "waiting_approval"
        or pending.get("iterations_executed") != 1
        or pending.get("issues") != []
        or pending_progress.get("consecutive_no_progress_iterations") != 0
    ):
        raise AssertionError(
            f"Browser content pending phase did not wait for approval: {pending!r}"
        )
    waiting_summary = _assert_loop_waiting_approval_projection(
        state,
        run_id=run_id,
        timeout=timeout,
    )
    if waiting_summary.get("attempt_id") != pending.get("attempt_id"):
        raise AssertionError(
            "Packaged backend projected a different Browser content attempt "
            f"for pending phase: pending={pending!r} summary={waiting_summary!r}"
        )

    approved = _run_packaged_browser_content_trial_once(
        workspace_dir,
        run_id=run_id,
        human_gate_decision="approved",
        release_edition=expected_distribution,
    )
    if (
        approved.get("status") != "succeeded"
        or approved.get("iterations_executed") != 1
        or approved.get("issues") != []
        or approved.get("attempt_id") != pending.get("attempt_id")
    ):
        raise AssertionError(
            f"Browser content approved phase did not preserve attempt and succeed: "
            f"pending={pending!r} approved={approved!r}"
        )
    succeeded_summary = _assert_loop_succeeded_projection(
        state,
        run_id=run_id,
        timeout=timeout,
    )
    if succeeded_summary.get("attempt_id") != approved.get("attempt_id"):
        raise AssertionError(
            "Packaged backend projected a different Browser content attempt "
            f"for approved phase: approved={approved!r} summary={succeeded_summary!r}"
        )
    _assert_loop_trial_export_package(
        state,
        summary=succeeded_summary,
        timeout=timeout,
        expected_contract_id="loop-browser-content-draft-preview-contract",
        expected_report_id=f"{run_id}-eval-browser-content-1-approved",
        reason="packaged LOOP-05 Browser content handoff export smoke",
        version_smoke="packaged-browser-content-trial",
    )
    return run_id


def _runtime_control_payload(
    summary: dict[str, Any],
    *,
    chat_id: str,
    session_id: str,
) -> dict[str, Any]:
    return {
        "expected_attempt_id": summary["attempt_id"],
        "expected_revision": summary["revision"],
        "chat_id": chat_id,
        "session_id": session_id,
        "workspace_dir": summary["workspace_dir"],
    }


def _assert_runtime_error(
    payload: Any,
    *,
    code: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected runtime error response object, got {payload!r}")
    detail = payload.get("detail")
    if not isinstance(detail, dict):
        raise AssertionError(f"Runtime error response did not include detail: {payload!r}")
    if detail.get("code") != code:
        raise AssertionError(f"Expected runtime error {code!r}, got {detail!r}")
    if run_id is not None and detail.get("run_id") != run_id:
        raise AssertionError(f"Runtime error run_id mismatch: {detail!r}")
    return detail


def _runtime_summary_terminal_statuses(summary: dict[str, Any]) -> set[str]:
    statuses = {
        str(summary.get("status") or ""),
        str(summary.get("authority_status") or ""),
        str(summary.get("transport_status") or ""),
    }
    statuses.discard("")
    return statuses or {"failed"}


def _assert_canonical_runtime_interrupt_is_noop(
    state: dict[str, Any],
    *,
    summary: dict[str, Any],
    chat_id: str,
    session_id: str,
    timeout: float,
) -> None:
    run_id = str(summary["id"])
    payload_body = _runtime_control_payload(
        summary,
        chat_id=chat_id,
        session_id=session_id,
    )
    _status, _body, payload = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/interrupt",
        payload=payload_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected canonical interrupt object, got {payload!r}")
    if payload.get("run_id") != run_id or payload.get("chat_id") != chat_id:
        raise AssertionError(f"Canonical interrupt identity mismatch: {payload!r}")
    if payload.get("stopped") is not False:
        raise AssertionError(
            "Interrupting a completed canonical packaged smoke run should be a no-op, "
            f"got {payload!r}"
        )


def _assert_runtime_stale_fence_rejected(
    state: dict[str, Any],
    *,
    summary: dict[str, Any],
    chat_id: str,
    session_id: str,
    timeout: float,
) -> None:
    run_id = str(summary["id"])
    payload_body = _runtime_control_payload(
        summary,
        chat_id=chat_id,
        session_id=session_id,
    )
    payload_body["expected_revision"] = int(summary["revision"]) + 999
    payload = _json_request_expect_http_error(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/interrupt",
        expected_status=409,
        payload=payload_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    detail = _assert_runtime_error(payload, code="stale_attempt", run_id=run_id)
    if detail.get("current_attempt_id") != summary["attempt_id"]:
        raise AssertionError(f"Stale fence did not report current attempt: {detail!r}")
    if detail.get("current_revision") != summary["revision"]:
        raise AssertionError(f"Stale fence did not report current revision: {detail!r}")


def _assert_chat_pause_is_not_supported(
    state: dict[str, Any],
    *,
    summary: dict[str, Any],
    chat_id: str,
    session_id: str,
    timeout: float,
) -> None:
    run_id = str(summary["id"])
    payload = _json_request_expect_http_error(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/pause",
        expected_status=409,
        payload=_runtime_control_payload(summary, chat_id=chat_id, session_id=session_id),
        headers=_state_headers(state),
        timeout=timeout,
    )
    detail = _assert_runtime_error(
        payload,
        code="runtime_control_not_supported",
        run_id=run_id,
    )
    if detail.get("source") != "chat" or detail.get("action") != "pause":
        raise AssertionError(f"Unexpected pause-not-supported detail: {detail!r}")


def _assert_export_requires_approval(
    state: dict[str, Any],
    *,
    summary: dict[str, Any],
    chat_id: str,
    session_id: str,
    timeout: float,
) -> None:
    run_id = str(summary["id"])
    payload = _json_request_expect_http_error(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/export",
        expected_status=403,
        payload=_runtime_control_payload(summary, chat_id=chat_id, session_id=session_id),
        headers=_state_headers(state),
        timeout=timeout,
    )
    _assert_runtime_error(payload, code="runtime_run_export_approval_required")


def _assert_terminal_budget_update_rejected(
    state: dict[str, Any],
    *,
    summary: dict[str, Any],
    chat_id: str,
    session_id: str,
    timeout: float,
) -> None:
    run_id = str(summary["id"])
    payload_body = _runtime_control_payload(
        summary,
        chat_id=chat_id,
        session_id=session_id,
    )
    payload_body.update(
        {
            "budget": {"max_steps": 9},
            "changed_by": "user",
            "reason": "packaged smoke terminal budget rejection",
        }
    )
    payload = _json_request_expect_http_error(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/budget",
        expected_status=409,
        payload=payload_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    detail = _assert_runtime_error(
        payload,
        code="runtime_transition_rejected",
        run_id=run_id,
    )
    expected_statuses = _runtime_summary_terminal_statuses(summary)
    if (
        detail.get("action") != "update_budget"
        or str(detail.get("current_status") or "") not in expected_statuses
    ):
        raise AssertionError(f"Unexpected terminal budget rejection detail: {detail!r}")


def _assert_runtime_archive_and_delete_cleanup(
    state: dict[str, Any],
    *,
    summary: dict[str, Any],
    chat_id: str,
    session_id: str,
    timeout: float,
) -> None:
    run_id = str(summary["id"])
    control_payload = _runtime_control_payload(
        summary,
        chat_id=chat_id,
        session_id=session_id,
    )
    _status, _body, archive = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/archive",
        payload=control_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(archive, dict) or archive.get("archived") is not True:
        raise AssertionError(f"Expected successful runtime archive, got {archive!r}")
    expected_statuses = _runtime_summary_terminal_statuses(summary)
    if (
        archive.get("run_id") != run_id
        or str(archive.get("status") or "") not in expected_statuses
    ):
        raise AssertionError(f"Runtime archive identity/status mismatch: {archive!r}")
    runtime_archive = archive.get("runtime_archive")
    if not isinstance(runtime_archive, dict):
        raise AssertionError(f"Runtime archive report missing: {archive!r}")
    if runtime_archive.get("kind") != "runtime_run_archive":
        raise AssertionError(f"Unexpected runtime archive report: {runtime_archive!r}")
    if runtime_archive.get("active_contract_retained") is not True:
        raise AssertionError(f"Archive should retain active contract: {runtime_archive!r}")

    denied = _json_request_expect_http_error(
        "DELETE",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}",
        expected_status=403,
        payload=control_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    _assert_runtime_error(denied, code="runtime_run_delete_approval_required")

    delete_payload = dict(control_payload)
    delete_payload.update(
        {
            "approved": True,
            "reason": "packaged smoke cleanup",
            "delete_artifacts": False,
        }
    )
    _status, _body, deleted = _json_request(
        "DELETE",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}",
        payload=delete_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(deleted, dict) or deleted.get("deleted") is not True:
        raise AssertionError(f"Expected successful runtime delete, got {deleted!r}")
    runtime_delete = deleted.get("runtime_delete")
    if not isinstance(runtime_delete, dict):
        raise AssertionError(f"Runtime delete report missing: {deleted!r}")
    if runtime_delete.get("kind") != "runtime_run_delete":
        raise AssertionError(f"Unexpected runtime delete report: {runtime_delete!r}")
    if runtime_delete.get("active_contract_deleted") is not True:
        raise AssertionError(f"Delete should remove active contract: {runtime_delete!r}")
    if deleted.get("delete_artifacts") is not False:
        raise AssertionError(f"Packaged smoke delete should dry-run artifacts: {deleted!r}")

    stale_after_delete = _json_request_expect_http_error(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/interrupt",
        expected_status=409,
        payload=control_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    detail = _assert_runtime_error(
        stale_after_delete,
        code="stale_attempt",
        run_id=run_id,
    )
    if detail.get("current_attempt_id") is not None:
        raise AssertionError(f"Deleted run should not report active attempt: {detail!r}")


def _assert_canonical_runtime_controls(
    state: dict[str, Any],
    *,
    chat_id: str,
    session_id: str,
    timeout: float,
    destructive_cleanup: bool = False,
    expected_statuses: set[str] | None = None,
) -> None:
    summary = _find_runtime_run_summary(
        state,
        chat_id=chat_id,
        session_id=session_id,
        timeout=timeout,
        expected_statuses=expected_statuses,
    )
    _assert_canonical_runtime_interrupt_is_noop(
        state,
        summary=summary,
        chat_id=chat_id,
        session_id=session_id,
        timeout=timeout,
    )
    _assert_runtime_stale_fence_rejected(
        state,
        summary=summary,
        chat_id=chat_id,
        session_id=session_id,
        timeout=timeout,
    )
    _assert_chat_pause_is_not_supported(
        state,
        summary=summary,
        chat_id=chat_id,
        session_id=session_id,
        timeout=timeout,
    )
    _assert_export_requires_approval(
        state,
        summary=summary,
        chat_id=chat_id,
        session_id=session_id,
        timeout=timeout,
    )
    _assert_terminal_budget_update_rejected(
        state,
        summary=summary,
        chat_id=chat_id,
        session_id=session_id,
        timeout=timeout,
    )
    if destructive_cleanup:
        _assert_runtime_archive_and_delete_cleanup(
            state,
            summary=summary,
            chat_id=chat_id,
            session_id=session_id,
            timeout=timeout,
        )


def _assert_browser_preflight_failure_settles_runtime_run(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    timeout: float,
) -> str:
    run_id = f"browser-packaged-smoke-{uuid.uuid4().hex[:8]}"
    task_body = {
        "run_id": run_id,
        "profile_id": "default",
        "access": "read",
        "mode": "background_observable",
        "instruction": "packaged browser preflight failure smoke",
        "url": "about:blank",
        "requires_foreground_confirmation": False,
        "max_actions": 200,
        "max_duration_seconds": 60,
        "metadata": {
            "workspace_dir": str(workspace_dir),
            "agent_id": "default",
            "session_id": "browser-packaged-smoke",
        },
    }
    _status, _body, task = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/browser-workbench/tasks",
        payload=task_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(task, dict):
        raise AssertionError(f"Expected browser task response object, got {task!r}")
    if task.get("run_id") != run_id or task.get("status") != "failed":
        raise AssertionError(f"Expected deterministic browser preflight failure, got {task!r}")
    if not str(task.get("error_code") or ""):
        raise AssertionError(f"Browser preflight failure did not include error_code: {task!r}")

    deadline = time.monotonic() + timeout
    summary: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
        if summary.get("status") == "failed":
            break
        time.sleep(0.1)
    if summary is None or summary.get("status") != "failed":
        raise AssertionError(
            "Browser preflight failure did not settle canonical RuntimeRun "
            f"to failed: {summary!r}"
        )
    if summary.get("source") != "browser":
        raise AssertionError(f"Expected browser runtime summary, got {summary!r}")

    control_payload = {
        "expected_attempt_id": summary["attempt_id"],
        "expected_revision": summary["revision"],
        "workspace_dir": summary["workspace_dir"],
    }
    pause_error = _json_request_expect_http_error(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/pause",
        expected_status=409,
        payload=control_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    detail = _assert_runtime_error(pause_error, run_id=run_id, code="runtime_transition_rejected")
    if detail.get("action") != "pause" or detail.get("current_status") != "failed":
        raise AssertionError(f"Unexpected browser terminal pause rejection: {detail!r}")
    after_pause = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
    if after_pause.get("status") != "failed" or after_pause.get("revision") != summary["revision"]:
        raise AssertionError(
            "Rejected browser pause mutated canonical RuntimeRun summary: "
            f"before={summary!r} after={after_pause!r}"
        )
    return run_id


def _assert_runtime_run_remains_failed_after_restart(
    state: dict[str, Any],
    *,
    run_id: str,
    timeout: float,
) -> None:
    summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
    if summary.get("status") != "failed":
        raise AssertionError(
            f"Runtime run {run_id!r} did not remain failed after restart: {summary!r}"
        )


async def _write_packaged_operator_effect_seed(
    workspace_dir: Path,
    *,
    run_id: str,
    release_edition: str,
) -> dict[str, str]:
    from lantai.runtime.effects import (
        RuntimeEffectJournal,
        RuntimeEffectReducerAction,
        reduce_runtime_effect,
    )
    from lantai.runtime.runs import RuntimeEffectRecord, RuntimeRunContract, RuntimeRunOwner
    from lantai.runtime.store import RuntimeRunStore

    if release_edition not in {"official", "mas"}:
        raise AssertionError(f"Unsupported release edition: {release_edition!r}")
    now = datetime.now(timezone.utc)
    attempt_id = f"{run_id}:attempt-1"
    effect_id = f"{run_id}:effect-click-publish"
    owner = RuntimeRunOwner(
        user_id="default",
        agent_id="browser",
        session_id="packaged-operator-effect-smoke",
        workspace_id=str(workspace_dir),
        channel="browser",
    )
    contract = RuntimeRunContract(
        run_id=run_id,
        attempt_id=attempt_id,
        source="browser",
        owner=owner,
        status="running",
        revision=2,
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        contract_hash="sha256:packaged-operator-effect-contract",
        policy_snapshot_hash="sha256:packaged-operator-effect-policy",
        release_edition=cast(Literal["official", "mas"], release_edition),
    )
    workspace_dir.mkdir(parents=True, exist_ok=True)
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    intent = RuntimeEffectRecord(
        run_id=run_id,
        attempt_id=attempt_id,
        step_id="browser-action-0",
        effect_id=effect_id,
        source="browser",
        owner=owner,
        status="intent",
        tool_name="browser.click",
        args_hash="sha256:packaged-operator-effect-args",
        policy_snapshot_hash="sha256:packaged-operator-effect-policy",
        idempotency_key=f"{attempt_id}:{effect_id}",
        created_at=now,
        release_edition=cast(Literal["official", "mas"], release_edition),
    )
    journal = RuntimeEffectJournal(workspace_dir)
    journal.append_effect(intent)
    running = reduce_runtime_effect(
        intent,
        RuntimeEffectReducerAction(type="start", now=now),
    ).effect
    journal.append_effect(running)
    return {"attempt_id": attempt_id, "effect_id": effect_id}


def _runtime_effect_statuses(
    workspace_dir: Path,
    *,
    run_id: str,
    attempt_id: str,
    effect_id: str,
) -> list[str]:
    from lantai.runtime.effects import RuntimeEffectJournal

    return [
        record.status
        for record in RuntimeEffectJournal(workspace_dir).list_effects(
            run_id,
            attempt_id,
        )
        if record.effect_id == effect_id
    ]


def _assert_packaged_operator_effect_recovery(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    expected_distribution: str,
    timeout: float,
) -> dict[str, Any]:
    run_id = f"packaged-operator-effect-{uuid.uuid4().hex[:8]}"
    seeded = asyncio.run(
        _write_packaged_operator_effect_seed(
            workspace_dir,
            run_id=run_id,
            release_edition=expected_distribution,
        )
    )
    attempt_id = seeded["attempt_id"]
    effect_id = seeded["effect_id"]
    summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
    if summary.get("status") != "running" or summary.get("source") != "browser":
        raise AssertionError(f"Seeded operator effect run was not running: {summary!r}")
    if summary.get("ambiguous_effect") is not True:
        raise AssertionError(f"Seeded operator effect was not surfaced as ambiguous: {summary!r}")
    recovery = summary.get("recovery") if isinstance(summary.get("recovery"), dict) else {}
    if recovery.get("effect_replay_forbidden") is not True:
        raise AssertionError(f"Ambiguous effect did not forbid replay: {summary!r}")
    effects = recovery.get("effects")
    if not isinstance(effects, list) or not effects:
        raise AssertionError(f"Runtime summary did not include effect recovery rows: {summary!r}")
    effect_row = next(
        (item for item in effects if isinstance(item, dict) and item.get("effect_id") == effect_id),
        None,
    )
    if not isinstance(effect_row, dict):
        raise AssertionError(f"Seeded effect row was not listed: {summary!r}")
    if effect_row.get("external_call_must_not_be_replayed") is not True:
        raise AssertionError(f"Effect row did not forbid external replay: {effect_row!r}")
    if effect_row.get("automatic_external_call_allowed") is not False:
        raise AssertionError(f"Effect row allowed automatic external replay: {effect_row!r}")

    control_payload = _runtime_control_payload_from_summary(summary)
    resolved_payload = {
        **control_payload,
        "resolution": "human_resolved",
        "expected_effect_revision": 1,
        "reason": "packaged operator confirmed external effect result",
        "result_hash": "sha256:packaged-operator-human-result",
    }
    _status, _body, resolved = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/effects/{effect_id}/resolve",
        payload=resolved_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(resolved, dict) or resolved.get("resolved") is not True:
        raise AssertionError(f"Runtime effect resolve response was invalid: {resolved!r}")
    if resolved.get("effect_status") != "human_resolved":
        raise AssertionError(f"Runtime effect was not human-resolved: {resolved!r}")
    if resolved.get("effect_revision") != 3:
        raise AssertionError(f"Runtime effect resolve revision mismatch: {resolved!r}")
    if resolved.get("applied_actions") != ["mark_ambiguous", "human_resolve"]:
        raise AssertionError(f"Runtime effect resolve actions mismatch: {resolved!r}")
    statuses_after_resolve = _runtime_effect_statuses(
        workspace_dir,
        run_id=run_id,
        attempt_id=attempt_id,
        effect_id=effect_id,
    )
    if statuses_after_resolve != ["intent", "running", "ambiguous", "human_resolved"]:
        raise AssertionError(
            "Runtime effect journal did not record the operator resolution path: "
            f"{statuses_after_resolve!r}"
        )

    checkpoint_payload = {
        **control_payload,
        "action": "rebuild_checkpoint",
        "expected_effect_revision": resolved["effect_revision"],
        "checkpoint_id": f"{effect_id}:checkpoint",
        "checkpoint_seq": 42,
        "checkpoint_summary": "packaged operator rebuilt checkpoint after effect",
    }
    _status, _body, checkpointed = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/effects/{effect_id}/repair",
        payload=checkpoint_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(checkpointed, dict) or checkpointed.get("repaired") is not True:
        raise AssertionError(f"Runtime effect checkpoint repair was invalid: {checkpointed!r}")
    if checkpointed.get("revision") != int(summary["revision"]) + 1:
        raise AssertionError(f"Runtime effect checkpoint repair revision mismatch: {checkpointed!r}")
    checkpoint = checkpointed.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("seq") != 42:
        raise AssertionError(f"Runtime effect checkpoint repair omitted checkpoint: {checkpointed!r}")

    terminal_payload = {
        **control_payload,
        "action": "rewrite_terminal",
        "expected_revision": checkpointed["revision"],
        "expected_effect_revision": resolved["effect_revision"],
        "terminal_status": "succeeded",
        "reason": "packaged operator completed repaired effect run",
    }
    _status, _body, terminal = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/effects/{effect_id}/repair",
        payload=terminal_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(terminal, dict) or terminal.get("repaired") is not True:
        raise AssertionError(f"Runtime effect terminal repair was invalid: {terminal!r}")
    if terminal.get("status") != "succeeded":
        raise AssertionError(f"Runtime effect terminal repair did not succeed: {terminal!r}")

    repeated_payload = {
        **terminal_payload,
        "expected_revision": terminal["revision"],
        "reason": "repeat packaged operator terminal repair",
    }
    _status, _body, repeated = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/effects/{effect_id}/repair",
        payload=repeated_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(repeated, dict) or repeated.get("idempotent") is not True:
        raise AssertionError(f"Repeated terminal repair was not idempotent: {repeated!r}")

    final_summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
    if final_summary.get("authority_status") != "succeeded":
        raise AssertionError(f"Final operator effect summary lost authority status: {final_summary!r}")
    if final_summary.get("status") not in {"completed", "succeeded"}:
        raise AssertionError(f"Final operator effect summary is not completed: {final_summary!r}")
    final_statuses = _runtime_effect_statuses(
        workspace_dir,
        run_id=run_id,
        attempt_id=attempt_id,
        effect_id=effect_id,
    )
    if final_statuses != ["intent", "running", "ambiguous", "human_resolved"]:
        raise AssertionError(f"Repair replayed or mutated the external effect: {final_statuses!r}")

    return {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "effect_id": effect_id,
        "resolve_actions": resolved["applied_actions"],
        "effect_statuses": final_statuses,
        "checkpoint_revision": checkpointed["revision"],
        "terminal_revision": terminal["revision"],
        "repeated_terminal_idempotent": repeated["idempotent"],
        "final_status": final_summary.get("status"),
        "final_authority_status": final_summary.get("authority_status"),
    }


def _assert_browser_positive_pause_resume(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    timeout: float,
) -> str:
    profile_id = f"smoke-writer-{uuid.uuid4().hex[:8]}"
    profile_body = {
        "id": profile_id,
        "name": "Smoke Writer",
        "description": "packaged pause/resume smoke profile",
        "allowed_domains": [],
        "agent_control_enabled": True,
        "risk_policy": "write",
        "metadata": {"packaged_smoke": True},
    }
    _status, _body, profile = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/browser-workbench/profiles",
        payload=profile_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(profile, dict) or profile.get("id") != profile_id:
        raise AssertionError(f"Expected browser smoke profile, got {profile!r}")

    run_id = f"browser-positive-smoke-{uuid.uuid4().hex[:8]}"
    task_body = {
        "run_id": run_id,
        "profile_id": profile_id,
        "access": "write",
        "mode": "background_observable",
        "instruction": "packaged browser pause/resume smoke",
        "requires_foreground_confirmation": False,
        "max_actions": 200,
        "max_duration_seconds": 60,
        "metadata": {
            "workspace_dir": str(workspace_dir),
            "agent_id": "default",
            "session_id": "browser-positive-smoke",
        },
        "actions": [
            {
                "action": "click",
                "profile_id": profile_id,
                "selector": "#save",
                "access": "write",
                "reason": "packaged write confirmation smoke",
            }
        ],
    }
    _status, _body, task = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/browser-workbench/tasks",
        payload=task_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(task, dict) or task.get("run_id") != run_id:
        raise AssertionError(f"Expected browser task response, got {task!r}")

    deadline = time.monotonic() + timeout
    summary: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
        if summary.get("status") == "running":
            break
        time.sleep(0.1)
    if summary is None or summary.get("status") != "running":
        raise AssertionError(f"Browser positive run did not become running: {summary!r}")
    recovery = summary.get("recovery") if isinstance(summary.get("recovery"), dict) else {}
    if recovery.get("can_pause") is not True:
        raise AssertionError(f"Running browser run did not advertise can_pause: {summary!r}")

    control_payload = _runtime_control_payload_from_summary(summary)
    _status, _body, paused = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/pause",
        payload=control_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(paused, dict) or paused.get("paused") is not True:
        raise AssertionError(f"Expected browser pause success, got {paused!r}")
    if paused.get("revision") != int(summary["revision"]) + 1:
        raise AssertionError(f"Browser pause did not advance revision: {paused!r}")
    _assert_stale_runtime_control(
        state,
        run_id=run_id,
        action="pause",
        stale_summary=summary,
        timeout=timeout,
    )

    paused_summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
    paused_recovery = (
        paused_summary.get("recovery")
        if isinstance(paused_summary.get("recovery"), dict)
        else {}
    )
    if paused_summary.get("authority_status") != "paused":
        raise AssertionError(f"Paused browser run was not projected as paused: {paused_summary!r}")
    if paused_recovery.get("can_resume") is not True:
        raise AssertionError(f"Paused browser run did not advertise can_resume: {paused_summary!r}")

    resume_payload = _runtime_control_payload_from_summary(paused_summary)
    _status, _body, resumed = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/resume",
        payload=resume_payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(resumed, dict) or resumed.get("resumed") is not True:
        raise AssertionError(f"Expected browser resume success, got {resumed!r}")
    if resumed.get("revision") != int(paused_summary["revision"]) + 1:
        raise AssertionError(f"Browser resume did not advance revision: {resumed!r}")
    _assert_stale_runtime_control(
        state,
        run_id=run_id,
        action="resume",
        stale_summary=paused_summary,
        timeout=timeout,
    )

    resumed_summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
    if resumed_summary.get("status") != "running":
        raise AssertionError(f"Resumed browser run was not running: {resumed_summary!r}")
    resumed_recovery = (
        resumed_summary.get("recovery")
        if isinstance(resumed_summary.get("recovery"), dict)
        else {}
    )
    if resumed_recovery.get("can_pause") is not True:
        raise AssertionError(f"Resumed browser run did not advertise can_pause: {resumed_summary!r}")

    _json_request(
        "POST",
        f"{_state_base_url(state)}/api/browser-workbench/tasks/{run_id}/cancel",
        payload={},
        headers=_state_headers(state),
        timeout=timeout,
    )
    return run_id


def _runtime_control_payload_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "expected_attempt_id": summary["attempt_id"],
        "expected_revision": summary["revision"],
        "workspace_dir": summary["workspace_dir"],
    }


def _assert_stale_runtime_control(
    state: dict[str, Any],
    *,
    run_id: str,
    action: str,
    stale_summary: dict[str, Any],
    timeout: float,
) -> None:
    payload = _runtime_control_payload_from_summary(stale_summary)
    parsed = _json_request_expect_http_error(
        "POST",
        f"{_state_base_url(state)}/api/runtime/runs/{run_id}/{action}",
        expected_status=409,
        payload=payload,
        headers=_state_headers(state),
        timeout=timeout,
    )
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if not isinstance(detail, dict) or detail.get("code") != "stale_attempt":
        raise AssertionError(f"Expected stale_attempt for stale browser {action}, got {parsed!r}")
    if detail.get("expected_attempt_id") != payload["expected_attempt_id"]:
        raise AssertionError(f"Stale {action} detail lost expected attempt: {detail!r}")
    if detail.get("expected_revision") != payload["expected_revision"]:
        raise AssertionError(f"Stale {action} detail lost expected revision: {detail!r}")


def _exercise_console_model_success_path(
    state: dict[str, Any],
    *,
    timeout: float,
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    session_id = f"console:packaged-app-smoke-{suffix}"
    client_turn_id = f"packaged-smoke-{suffix}"
    prompt = f"packaged app lifecycle model success smoke {suffix}"
    chat_body = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ],
        "session_id": session_id,
        "user_id": "default",
        "channel": "console",
        "stream": True,
        "client_turn_id": client_turn_id,
        "meta": {
            "client_turn_id": client_turn_id,
            "deep_thinking": True,
        },
    }
    status, sse_text, _parsed = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/console/chat",
        payload=chat_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if status != 200:
        raise AssertionError(f"Expected chat HTTP 200, got {status}")
    payloads = _extract_sse_payloads(sse_text)
    if not payloads:
        raise AssertionError(f"Expected SSE payloads, got {sse_text!r}")
    if payloads[0].get("type") != "response.in_progress":
        raise AssertionError(f"Expected response.in_progress first, got {payloads[:2]!r}")
    final = payloads[-1]
    if final.get("object") != "response" or final.get("status") != "completed":
        raise AssertionError(f"Expected model-backed completion, got {final!r}")
    raw_sse = "\n".join(json.dumps(payload, ensure_ascii=False) for payload in payloads)
    if not any(
        _contains_exact_string(payload, SMOKE_MODEL_THINKING_TEXT)
        for payload in payloads
    ):
        raise AssertionError(
            f"Provider reasoning was missing from SSE payloads: {payloads!r}"
        )
    if SMOKE_FORBIDDEN_FABRICATED_PROGRESS_TEXT in raw_sse:
        raise AssertionError(f"Fabricated public progress escaped into SSE: {payloads!r}")
    if SMOKE_FORBIDDEN_FIXED_REASONING_STATUS in raw_sse:
        raise AssertionError(
            f"Provider reasoning was replaced by a fixed status: {payloads!r}"
        )
    if SMOKE_MODEL_REPLY_PREFIX not in raw_sse:
        raise AssertionError(f"Expected streamed answer SSE payloads, got {payloads!r}")
    chat_id = _assert_persisted_success_events(
        state,
        session_id=session_id,
        timeout=timeout,
    )
    duplicate_status, duplicate_sse_text, _duplicate_parsed = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/console/chat",
        payload=chat_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if duplicate_status != 200:
        raise AssertionError(f"Expected duplicate chat HTTP 200, got {duplicate_status}")
    duplicate_payloads = _extract_sse_payloads(duplicate_sse_text)
    if not duplicate_payloads:
        raise AssertionError(
            f"Expected duplicate turn SSE payloads, got {duplicate_sse_text!r}"
        )
    duplicate_final = duplicate_payloads[-1]
    metadata = (
        duplicate_final.get("metadata")
        if isinstance(duplicate_final.get("metadata"), dict)
        else {}
    )
    if duplicate_final.get("status") != "completed" or metadata.get(
        "deduped_client_turn"
    ) is not True:
        raise AssertionError(
            "Expected duplicate client turn to be deduped after completion, "
            f"got {duplicate_payloads!r}"
        )
    duplicate_chat_id = _assert_persisted_success_events(
        state,
        session_id=session_id,
        timeout=timeout,
    )
    if duplicate_chat_id != chat_id:
        raise AssertionError(
            "Duplicate client turn resolved to a different chat id: "
            f"{duplicate_chat_id!r} != {chat_id!r}"
        )
    return session_id, chat_id


def _seed_chat_basic_ui_send(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    timeout: float,
) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    session_id = f"console:packaged-basic-ui-send-{suffix}"
    isolation_session_id = f"console:packaged-basic-ui-isolation-{suffix}"
    run_id = f"packaged-basic-ui-send-{suffix}"
    first_text = f"PACKAGED_BASIC_UI_FIRST_{suffix}"
    second_text = f"PACKAGED_BASIC_UI_SECOND_{suffix}"
    topic_workspace_dir = workspace_dir / "topic-workspaces" / run_id
    _status, _body, chat = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/chats",
        payload={
            "name": f"basic chat UI send smoke {suffix}",
            "session_id": session_id,
            "user_id": "default",
            "channel": "console",
            "meta": {"topic_workspace_dir": str(topic_workspace_dir)},
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), str):
        raise AssertionError(f"Expected seeded basic UI chat spec, got {chat!r}")
    _status, _body, isolation_chat = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/chats",
        payload={
            "name": f"basic chat UI isolation smoke {suffix}",
            "session_id": isolation_session_id,
            "user_id": "default",
            "channel": "console",
            "meta": {
                "topic_workspace_dir": str(
                    workspace_dir / "topic-workspaces" / f"{run_id}-isolation"
                )
            },
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(isolation_chat, dict) or not isinstance(
        isolation_chat.get("id"),
        str,
    ):
        raise AssertionError(
            f"Expected seeded basic UI isolation chat spec, got {isolation_chat!r}"
        )
    return {
        "chat_id": str(chat["id"]),
        "isolation_chat_id": str(isolation_chat["id"]),
        "session_id": session_id,
        "isolation_session_id": isolation_session_id,
        "run_id": run_id,
        "first_text": first_text,
        "second_text": second_text,
        "topic_workspace_dir": str(topic_workspace_dir),
    }


def _seed_chat_provider_stream_ui(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    timeout: float,
) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    session_id = f"console:packaged-provider-stream-ui-{suffix}"
    run_id = f"packaged-provider-stream-ui-{suffix}"
    prompt_text = f"{SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX}{suffix}"
    queued_second_text = f"PACKAGED_PROVIDER_QUEUE_SECOND_{suffix}"
    queued_third_text = f"PACKAGED_PROVIDER_QUEUE_THIRD_{suffix}"
    stop_session_id = f"console:packaged-provider-stop-resend-ui-{suffix}"
    stop_run_id = f"packaged-provider-stop-resend-ui-{suffix}"
    stop_prompt_text = f"{SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX}STOP_{suffix}"
    stop_resend_text = f"PACKAGED_PROVIDER_STOP_RESEND_{suffix}"
    switch_session_id = f"console:packaged-provider-switch-ui-{suffix}"
    switch_run_id = f"packaged-provider-switch-ui-{suffix}"
    switch_prompt_text = f"{SMOKE_MODEL_PROVIDER_STREAM_PROMPT_PREFIX}SWITCH_{suffix}"
    isolation_session_id = f"console:packaged-provider-switch-isolation-{suffix}"
    isolation_run_id = f"packaged-provider-switch-isolation-{suffix}"

    def create_chat(*, name: str, seeded_session_id: str, seeded_run_id: str) -> str:
        topic_workspace_dir = workspace_dir / "topic-workspaces" / seeded_run_id
        _status, _body, chat = _json_request(
            "POST",
            f"{_state_base_url(state)}/api/chats",
            payload={
                "name": name,
                "session_id": seeded_session_id,
                "user_id": "default",
                "channel": "console",
                "meta": {"topic_workspace_dir": str(topic_workspace_dir)},
            },
            headers=_state_headers(state),
            timeout=timeout,
        )
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), str):
            raise AssertionError(
                f"Expected seeded provider experience UI chat spec, got {chat!r}"
            )
        return str(chat["id"])

    chat_id = create_chat(
        name=f"provider stream UI smoke {suffix}",
        seeded_session_id=session_id,
        seeded_run_id=run_id,
    )
    stop_chat_id = create_chat(
        name=f"provider stop resend UI smoke {suffix}",
        seeded_session_id=stop_session_id,
        seeded_run_id=stop_run_id,
    )
    switch_chat_id = create_chat(
        name=f"provider stream switch UI smoke {suffix}",
        seeded_session_id=switch_session_id,
        seeded_run_id=switch_run_id,
    )
    switch_isolation_chat_id = create_chat(
        name=f"provider stream switch isolation smoke {suffix}",
        seeded_session_id=isolation_session_id,
        seeded_run_id=isolation_run_id,
    )
    return {
        "chat_id": chat_id,
        "session_id": session_id,
        "run_id": run_id,
        "prompt_text": prompt_text,
        "queued_second_text": queued_second_text,
        "queued_third_text": queued_third_text,
        "thinking_text": SMOKE_MODEL_THINKING_TEXT,
        "public_progress_text": SMOKE_PUBLIC_REASONING_TEXT,
        "partial_text": SMOKE_MODEL_PARTIAL_SENTINEL,
        "assistant_text": f"{SMOKE_MODEL_REPLY_PREFIX}: {prompt_text[:80]}",
        "topic_workspace_dir": str(workspace_dir / "topic-workspaces" / run_id),
        "stop_chat_id": stop_chat_id,
        "stop_session_id": stop_session_id,
        "stop_run_id": stop_run_id,
        "stop_prompt_text": stop_prompt_text,
        "stop_assistant_text": (
            f"{SMOKE_MODEL_REPLY_PREFIX}: {stop_prompt_text[:80]}"
        ),
        "stop_resend_text": stop_resend_text,
        "stop_resend_assistant_text": (
            f"{SMOKE_MODEL_REPLY_PREFIX}: {stop_resend_text[:80]}"
        ),
        "switch_chat_id": switch_chat_id,
        "switch_session_id": switch_session_id,
        "switch_run_id": switch_run_id,
        "switch_prompt_text": switch_prompt_text,
        "switch_assistant_text": (
            f"{SMOKE_MODEL_REPLY_PREFIX}: {switch_prompt_text[:80]}"
        ),
        "switch_isolation_chat_id": switch_isolation_chat_id,
        "switch_isolation_session_id": isolation_session_id,
    }


def _stop_app(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    # The process can exit after poll() but before the signal reaches it. Treat
    # that race as a successful, idempotent stop and still wait once so the
    # parent reaps an exited child instead of leaking a zombie.
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=8)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def _crash_managed_backend_for_restart(pid: int) -> None:
    # The restart-cycle smoke verifies crash recovery. SIGTERM can hang while
    # uvicorn drains active renderer EventSource/HTTP connections.
    os.kill(pid, signal.SIGKILL)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _usable_console_electron_binary(binary: Path) -> bool:
    if not binary.is_file():
        return False
    if (
        sys.platform == "darwin"
        and binary.name == "Electron"
        and binary.parent.name == "MacOS"
        and binary.parent.parent.name == "Contents"
    ):
        framework = (
            binary.parent.parent
            / "Frameworks"
            / "Electron Framework.framework"
            / "Electron Framework"
        )
        return framework.is_file()
    return True


def _console_electron_platform_arch() -> tuple[str, str]:
    electron_platform = (
        os.environ.get("ELECTRON_INSTALL_PLATFORM")
        or os.environ.get("npm_config_platform")
        or sys.platform
    )
    electron_arch = (
        os.environ.get("ELECTRON_INSTALL_ARCH")
        or os.environ.get("npm_config_arch")
        or platform.machine()
    ).lower()
    if electron_arch in {"aarch64"}:
        electron_arch = "arm64"
    if electron_arch in {"x86_64", "amd64"}:
        electron_arch = "x64"
    return electron_platform, electron_arch


def _console_electron_version(electron_pkg: Path) -> str:
    try:
        payload = json.loads((electron_pkg / "package.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    version = payload.get("version")
    return str(version).strip() if isinstance(version, str) else ""


def _console_electron_cache_archives(electron_pkg: Path) -> list[Path]:
    version = _console_electron_version(electron_pkg)
    if not version:
        return []
    electron_platform, electron_arch = _console_electron_platform_arch()
    archive_name = f"electron-v{version}-{electron_platform}-{electron_arch}.zip"
    roots = [
        os.environ.get("electron_config_cache"),
        os.environ.get("ELECTRON_CACHE"),
        str(Path.home() / "Library" / "Caches" / "electron"),
        str(Path.home() / ".cache" / "electron"),
    ]
    archives: list[Path] = []
    seen: set[str] = set()
    for raw_root in roots:
        if not raw_root:
            continue
        root = Path(raw_root).expanduser()
        if not root.is_dir():
            continue
        for candidate in [root / archive_name, *root.glob(f"*/{archive_name}")]:
            key = str(candidate)
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            archives.append(candidate)
    return archives


def _prepare_console_electron_from_cache(
    electron_pkg: Path,
    *,
    diagnostics: list[str],
) -> Path | None:
    if sys.platform != "darwin":
        return None
    ditto = shutil.which("ditto")
    if not ditto:
        diagnostics.append("cached Electron extraction skipped: ditto not found")
        return None
    archives = _console_electron_cache_archives(electron_pkg)
    if not archives:
        diagnostics.append("cached Electron extraction skipped: no matching cache archive")
        return None
    dist = electron_pkg / "dist"
    binary = dist / "Electron.app" / "Contents" / "MacOS" / "Electron"
    for archive in archives:
        shutil.rmtree(dist, ignore_errors=True)
        dist.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [ditto, "-x", "-k", str(archive), str(dist)],
            cwd=str(electron_pkg.parent.parent),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        diagnostics.append(
            "cached Electron extraction "
            f"archive={archive} exit={result.returncode} "
            f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
        if result.returncode != 0:
            continue
        (electron_pkg / "path.txt").write_text(
            "Electron.app/Contents/MacOS/Electron",
            encoding="utf-8",
        )
        if _usable_console_electron_binary(binary):
            return binary
    return None


def _console_electron_binary(project_root: Path | None = None) -> Path:
    root = project_root or _project_root()
    console_dir = root / "console"
    electron_pkg = console_dir / "node_modules" / "electron"
    electron_dist_binary = (
        electron_pkg
        / "dist"
        / "Electron.app"
        / "Contents"
        / "MacOS"
        / "Electron"
    )
    electron_install_script = electron_pkg / "install.js"
    electron_bin_wrapper = console_dir / "node_modules" / ".bin" / "electron"
    electron_path_txt = electron_pkg / "path.txt"
    direct_candidates = [
        electron_dist_binary,
        electron_pkg / "dist" / "electron",
    ]
    candidates = [*direct_candidates, electron_bin_wrapper]
    diagnostics: list[str] = []
    for binary in direct_candidates:
        if _usable_console_electron_binary(binary):
            return binary
    electron_wrapper_target: Path | None = None
    if electron_path_txt.is_file():
        electron_wrapper_target = (
            console_dir
            / "node_modules"
            / "electron"
            / "dist"
            / electron_path_txt.read_text(encoding="utf-8").strip()
        )
    if (
        electron_bin_wrapper.is_file()
        and electron_wrapper_target is not None
        and _usable_console_electron_binary(electron_wrapper_target)
    ):
        return electron_bin_wrapper
    if electron_install_script.is_file():
        node = shutil.which("node")
        if node:
            result = subprocess.run(
                [node, str(electron_install_script)],
                cwd=str(console_dir),
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            diagnostics.append(
                "electron install.js "
                f"exit={result.returncode} stdout={result.stdout.strip()!r} "
                f"stderr={result.stderr.strip()!r}"
            )
            if result.returncode == 0:
                for binary in direct_candidates:
                    if _usable_console_electron_binary(binary):
                        return binary
                if electron_path_txt.is_file():
                    electron_wrapper_target = (
                        console_dir
                        / "node_modules"
                        / "electron"
                        / "dist"
                        / electron_path_txt.read_text(encoding="utf-8").strip()
                    )
                if (
                    electron_bin_wrapper.is_file()
                    and electron_wrapper_target is not None
                    and _usable_console_electron_binary(electron_wrapper_target)
                ):
                    return electron_bin_wrapper
        else:
            diagnostics.append("electron install.js skipped: node not found")
    prepared = _prepare_console_electron_from_cache(
        electron_pkg,
        diagnostics=diagnostics,
    )
    if prepared is not None:
        return prepared
    raise FileNotFoundError(
        "Task Center multi-window smoke requires a console Electron binary. "
        f"Checked: {candidates}. Diagnostics: {diagnostics}"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        with contextlib.suppress(json.JSONDecodeError):
            parsed, _end = decoder.raw_decode(text[index:])
            if isinstance(parsed, dict):
                return parsed
    raise AssertionError(f"No JSON object found in output: {text!r}")


def _run_task_center_multi_window_smoke(
    state: dict[str, Any],
    *,
    run_id: str,
    timeout: float,
    project_root: Path | None = None,
) -> dict[str, Any]:
    root = project_root or _project_root()
    console_dir = root / "console"
    script = console_dir / "electron" / "taskCenterMultiWindowSmoke.cjs"
    preload = console_dir / "electron" / "taskCenterMultiWindowSmokePreload.cjs"
    if not script.is_file() or not preload.is_file():
        raise FileNotFoundError(
            "Task Center multi-window smoke scripts are missing: "
            f"script={script} preload={preload}"
        )

    command = [
        str(_console_electron_binary(root)),
        str(script),
        "--base-url",
        _state_base_url(state),
        "--desktop-token",
        _state_headers(state)[DESKTOP_TOKEN_HEADER],
        "--run-id",
        run_id,
        "--timeout-ms",
        str(max(1, int(timeout * 1000))),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(console_dir),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=timeout + 20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "Task Center multi-window Electron smoke timed out: "
            f"cmd={command!r} stdout={exc.stdout!r} stderr={exc.stderr!r}"
        ) from exc
    if result.returncode != 0:
        raise AssertionError(
            "Task Center multi-window Electron smoke failed: "
            f"exit={result.returncode} cmd={command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    payload = _extract_json_object(result.stdout)
    if payload.get("ok") is not True or payload.get("run_id") != run_id:
        raise AssertionError(f"Unexpected Task Center smoke payload: {payload!r}")
    statuses = payload.get("pause_statuses")
    if not isinstance(statuses, list) or 200 not in statuses or 409 not in statuses:
        raise AssertionError(
            "Task Center smoke did not observe both fresh and stale pause "
            f"responses: {payload!r}"
        )
    budget_statuses = payload.get("budget_statuses")
    if not isinstance(budget_statuses, list) or 200 not in budget_statuses:
        raise AssertionError(
            "Task Center smoke did not observe a successful budget update "
            f"response: {payload!r}"
        )
    return payload


def _run_chat_runtime_stream_renderer_smoke(
    state: dict[str, Any],
    *,
    chat_id: str,
    timeout: float,
    basic_ui_send: dict[str, str] | None = None,
    provider_stream_ui: dict[str, str] | None = None,
    expected_terminal_type: str = "turn.failed",
    expected_terminal_statuses: set[str] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    if expected_terminal_statuses is None:
        expected_terminal_statuses = {"failed"}
    root = project_root or _project_root()
    console_dir = root / "console"
    script = console_dir / "electron" / "chatRuntimeStreamSmoke.cjs"
    if not script.is_file():
        raise FileNotFoundError(f"Chat runtime stream Electron smoke is missing: {script}")

    # This renderer probe intentionally chains independent UI journeys
    # (reconnect, two regular turns, streamed/queued turns, stop-resend and
    # route isolation).  Keep each interaction's own feedback budget strict,
    # but give the aggregate process enough wall time to finish every journey
    # on a cold signed macOS bundle.
    renderer_timeout = max(float(timeout), 180.0)
    command = [
        str(_console_electron_binary(root)),
        str(script),
        "--base-url",
        _state_base_url(state),
        "--desktop-token",
        _state_headers(state)[DESKTOP_TOKEN_HEADER],
        "--chat-id",
        chat_id,
        "--timeout-ms",
        str(max(1, int(renderer_timeout * 1000))),
        "--expected-terminal-type",
        expected_terminal_type,
        "--expected-terminal-statuses",
        ",".join(sorted(expected_terminal_statuses)),
    ]
    if provider_stream_ui is not None:
        provider_stream_chat_id = str(provider_stream_ui.get("chat_id") or "")
        provider_stream_prompt = str(provider_stream_ui.get("prompt_text") or "")
        provider_stream_thinking = str(
            provider_stream_ui.get("public_progress_text") or ""
        )
        provider_stream_partial = str(provider_stream_ui.get("partial_text") or "")
        provider_stream_answer = str(provider_stream_ui.get("assistant_text") or "")
        provider_stream_queued_second = str(
            provider_stream_ui.get("queued_second_text") or ""
        )
        provider_stream_queued_third = str(
            provider_stream_ui.get("queued_third_text") or ""
        )
        provider_stop_chat_id = str(provider_stream_ui.get("stop_chat_id") or "")
        provider_stop_prompt = str(
            provider_stream_ui.get("stop_prompt_text") or ""
        )
        provider_stop_answer = str(
            provider_stream_ui.get("stop_assistant_text") or ""
        )
        provider_stop_resend = str(
            provider_stream_ui.get("stop_resend_text") or ""
        )
        provider_stop_resend_answer = str(
            provider_stream_ui.get("stop_resend_assistant_text") or ""
        )
        provider_switch_chat_id = str(
            provider_stream_ui.get("switch_chat_id") or ""
        )
        provider_switch_isolation_chat_id = str(
            provider_stream_ui.get("switch_isolation_chat_id") or ""
        )
        provider_switch_prompt = str(
            provider_stream_ui.get("switch_prompt_text") or ""
        )
        provider_switch_answer = str(
            provider_stream_ui.get("switch_assistant_text") or ""
        )
        if (
            not provider_stream_chat_id
            or not provider_stream_prompt
            or not provider_stream_thinking
            or not provider_stream_partial
            or not provider_stream_answer
            or not provider_stream_queued_second
            or not provider_stream_queued_third
            or not provider_stop_chat_id
            or not provider_stop_prompt
            or not provider_stop_answer
            or not provider_stop_resend
            or not provider_stop_resend_answer
            or not provider_switch_chat_id
            or not provider_switch_isolation_chat_id
            or not provider_switch_prompt
            or not provider_switch_answer
        ):
            raise AssertionError(
                f"Invalid provider stream UI smoke payload: {provider_stream_ui!r}"
            )
        command.extend(
            [
                "--provider-stream-chat-id",
                provider_stream_chat_id,
                "--provider-stream-prompt",
                provider_stream_prompt,
                "--provider-stream-thinking-text",
                provider_stream_thinking,
                "--provider-stream-partial-text",
                provider_stream_partial,
                "--provider-stream-answer-text",
                provider_stream_answer,
                "--provider-stream-queued-second-text",
                provider_stream_queued_second,
                "--provider-stream-queued-third-text",
                provider_stream_queued_third,
                "--provider-stop-chat-id",
                provider_stop_chat_id,
                "--provider-stop-prompt",
                provider_stop_prompt,
                "--provider-stop-answer-text",
                provider_stop_answer,
                "--provider-stop-resend-text",
                provider_stop_resend,
                "--provider-stop-resend-answer-text",
                provider_stop_resend_answer,
                "--provider-switch-chat-id",
                provider_switch_chat_id,
                "--provider-switch-isolation-chat-id",
                provider_switch_isolation_chat_id,
                "--provider-switch-prompt",
                provider_switch_prompt,
                "--provider-switch-answer-text",
                provider_switch_answer,
            ]
        )
    if basic_ui_send is not None:
        basic_chat_id = str(basic_ui_send.get("chat_id") or "")
        basic_isolation_chat_id = str(basic_ui_send.get("isolation_chat_id") or "")
        basic_first_text = str(basic_ui_send.get("first_text") or "")
        basic_second_text = str(basic_ui_send.get("second_text") or "")
        if (
            not basic_chat_id
            or not basic_isolation_chat_id
            or not basic_first_text
            or not basic_second_text
        ):
            raise AssertionError(
                f"Invalid basic UI send smoke payload: {basic_ui_send!r}"
            )
        command.extend(
            [
                "--basic-ui-chat-id",
                basic_chat_id,
                "--basic-ui-isolation-chat-id",
                basic_isolation_chat_id,
                "--basic-ui-first-text",
                basic_first_text,
                "--basic-ui-second-text",
                basic_second_text,
            ]
        )
    try:
        result = subprocess.run(
            command,
            cwd=str(console_dir),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=renderer_timeout + 20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "Chat runtime stream renderer Electron smoke timed out: "
            f"cmd={command!r} stdout={exc.stdout!r} stderr={exc.stderr!r}"
        ) from exc

    if result.returncode != 0:
        raise AssertionError(
            "Chat runtime stream renderer Electron smoke failed: "
            f"exit={result.returncode} cmd={command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    payload = _extract_json_object(result.stdout)
    if payload.get("ok") is not True or payload.get("chat_id") != chat_id:
        raise AssertionError(f"Unexpected chat runtime stream smoke payload: {payload!r}")
    if payload.get("terminal_type") != expected_terminal_type:
        raise AssertionError(
            "Chat renderer stream terminal type mismatch: "
            f"expected={expected_terminal_type!r} payload={payload!r}"
        )
    if str(payload.get("terminal_status") or "") not in expected_terminal_statuses:
        raise AssertionError(
            "Chat renderer stream terminal status mismatch: "
            f"expected={sorted(expected_terminal_statuses)!r} payload={payload!r}"
        )
    if payload.get("no_replay_event_count") != 0:
        raise AssertionError(f"Chat renderer stream replayed latest cursor: {payload!r}")
    reconnect_after_seq = payload.get("reconnect_after_seq")
    if not isinstance(reconnect_after_seq, int) or reconnect_after_seq < 0:
        raise AssertionError(f"Chat renderer stream returned invalid reconnect cursor: {payload!r}")
    observed_requests = payload.get("observed_stream_requests")
    if not isinstance(observed_requests, list) or len(observed_requests) < 2:
        raise AssertionError(
            "Chat renderer stream smoke did not observe reconnecting stream requests: "
            f"{payload!r}"
        )
    observed_after_seqs: set[str] = set()
    for item in observed_requests:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if "/events/stream" not in url:
            continue
        query = parse.parse_qs(parse.urlparse(url).query)
        after_seq = query.get("after_seq", [])
        if after_seq:
            observed_after_seqs.add(after_seq[0])
    expected_after_seqs = {"0", str(reconnect_after_seq)}
    if not expected_after_seqs.issubset(observed_after_seqs):
        raise AssertionError(
            "Chat renderer stream smoke missed expected stream cursors: "
            f"expected={sorted(expected_after_seqs)!r} "
            f"observed={sorted(observed_after_seqs)!r} payload={payload!r}"
        )
    if basic_ui_send is not None:
        basic = payload.get("basic_chat_ui_send")
        if not isinstance(basic, dict) or basic.get("ok") is not True:
            raise AssertionError(f"Chat renderer basic UI send did not pass: {payload!r}")
        if basic.get("chat_id") != basic_ui_send.get("chat_id"):
            raise AssertionError(f"Chat renderer basic UI send chat mismatch: {payload!r}")
        if basic.get("isolation_chat_id") != basic_ui_send.get("isolation_chat_id"):
            raise AssertionError(
                f"Chat renderer basic UI send isolation chat mismatch: {payload!r}"
            )
        if basic.get("first_text") != basic_ui_send.get("first_text"):
            raise AssertionError(f"Chat renderer basic UI first text mismatch: {payload!r}")
        if basic.get("second_text") != basic_ui_send.get("second_text"):
            raise AssertionError(f"Chat renderer basic UI second text mismatch: {payload!r}")
        final = basic.get("final")
        final_stable = basic.get("final_stable")
        final_backend = basic.get("final_backend")
        first_stage = basic.get("first")
        second_stage = basic.get("second")
        isolation = basic.get("isolation")
        isolation_backend = basic.get("isolation_backend")
        restored = basic.get("restored")
        restored_backend = basic.get("restored_backend")
        matrix = basic.get("experience_matrix")
        if not isinstance(final, dict):
            raise AssertionError(
                f"Chat renderer basic UI send omitted final DOM evidence: {payload!r}"
            )
        if not isinstance(final_stable, dict):
            raise AssertionError(
                f"Chat renderer basic UI send omitted final stable DOM evidence: {payload!r}"
            )
        if not isinstance(final_backend, dict):
            raise AssertionError(
                f"Chat renderer basic UI send omitted backend evidence: {payload!r}"
            )
        if not isinstance(first_stage, dict) or not isinstance(second_stage, dict):
            raise AssertionError(
                f"Chat renderer basic UI send omitted response timing evidence: {payload!r}"
            )
        if not isinstance(matrix, list):
            raise AssertionError(
                f"Chat renderer basic UI send omitted experience matrix: {payload!r}"
            )
        matrix_by_id = {
            str(item.get("id")): item
            for item in matrix
            if isinstance(item, dict) and item.get("id")
        }
        missing_matrix = sorted(REQUIRED_BASIC_EXPERIENCE_CHECKS - set(matrix_by_id))
        failed_matrix = sorted(
            check_id
            for check_id, item in matrix_by_id.items()
            if check_id in REQUIRED_BASIC_EXPERIENCE_CHECKS
            and item.get("passed") is not True
        )
        if missing_matrix or failed_matrix:
            raise AssertionError(
                "Chat renderer basic UI experience matrix did not pass: "
                f"missing={missing_matrix!r} failed={failed_matrix!r} payload={payload!r}"
            )
        max_user_feedback_ms = int(basic.get("max_user_feedback_ms") or 3000)
        max_assistant_feedback_ms = int(
            basic.get("max_assistant_feedback_ms") or 15000
        )
        for label, stage in (("first", first_stage), ("second", second_stage)):
            user_visible_ms = int(stage.get("user_visible_ms") or 0)
            assistant_visible_ms = int(stage.get("assistant_visible_ms") or 0)
            if user_visible_ms <= 0 or user_visible_ms > max_user_feedback_ms:
                raise AssertionError(
                    "Chat renderer basic UI send user feedback timing failed "
                    f"for {label}: {payload!r}"
                )
            if (
                assistant_visible_ms <= 0
                or assistant_visible_ms > max_assistant_feedback_ms
            ):
                raise AssertionError(
                    "Chat renderer basic UI send assistant feedback timing failed "
                    f"for {label}: {payload!r}"
                )
        user_count = int(final.get("user_bubble_count") or 0)
        assistant_count = int(final.get("assistant_bubble_count") or 0)
        avatar_count = int(final.get("response_avatar_count") or 0)
        action_count = int(final.get("action_bar_count") or 0)
        empty_count = int(final.get("empty_assistant_count") or 0)
        backend_turn_observed = bool(basic.get("backend_turn_observed"))
        console_chat_post_count = int(basic.get("console_chat_post_count") or 0)
        stable_user_count = int(final_stable.get("user_bubble_count") or 0)
        stable_assistant_count = int(final_stable.get("assistant_bubble_count") or 0)
        backend_message_count = int(final_backend.get("message_count") or 0)
        backend_user_count = int(final_backend.get("user_message_count") or 0)
        backend_assistant_count = int(
            final_backend.get("assistant_message_count") or 0
        )
        backend_pending_user_message = str(
            final_backend.get("pending_user_message") or ""
        )
        if not backend_turn_observed:
            raise AssertionError(
                "Chat renderer basic UI send did not observe the real backend "
                f"/api/console/chat turn: {payload!r}"
            )
        if console_chat_post_count != 2:
            raise AssertionError(
                "Chat renderer basic UI send should submit exactly two backend "
                f"turns in packaged smoke: {payload!r}"
            )
        if user_count != 2:
            raise AssertionError(
                f"Chat renderer basic UI send should render exactly two user messages: {payload!r}"
            )
        if assistant_count != 2:
            raise AssertionError(
                f"Chat renderer basic UI send should render exactly two assistant feedback messages: {payload!r}"
            )
        if empty_count != 0:
            raise AssertionError(
                f"Chat renderer basic UI send left empty assistant bubbles: {payload!r}"
            )
        if avatar_count > assistant_count:
            raise AssertionError(
                f"Chat renderer basic UI send duplicated assistant avatars: {payload!r}"
            )
        if action_count > assistant_count:
            raise AssertionError(
                f"Chat renderer basic UI send duplicated action bars: {payload!r}"
            )
        if stable_user_count != 2 or stable_assistant_count != 2:
            raise AssertionError(
                "Chat renderer basic UI send did not reach a stable two-turn state: "
                f"{payload!r}"
            )
        if backend_user_count != 2 or backend_assistant_count < 2:
            raise AssertionError(
                "Chat renderer basic UI send did not persist two user and at least two "
                f"assistant messages: {payload!r}"
            )
        if backend_pending_user_message:
            raise AssertionError(
                "Chat renderer basic UI send left a stale pending user message: "
                f"{payload!r}"
            )
        if not isinstance(isolation, dict) or not isinstance(restored, dict):
            raise AssertionError(
                f"Chat renderer basic UI send omitted session isolation evidence: {payload!r}"
            )
        if not isinstance(isolation_backend, dict) or not isinstance(
            restored_backend,
            dict,
        ):
            raise AssertionError(
                f"Chat renderer basic UI send omitted backend isolation evidence: {payload!r}"
            )
        if int(isolation.get("user_bubble_count") or 0) != 0:
            raise AssertionError(
                f"Chat renderer basic UI isolated chat leaked user messages: {payload!r}"
            )
        if int(isolation.get("assistant_bubble_count") or 0) != 0:
            raise AssertionError(
                f"Chat renderer basic UI isolated chat leaked assistant messages: {payload!r}"
            )
        if int(restored.get("user_bubble_count") or 0) != 2:
            raise AssertionError(
                f"Chat renderer basic UI original chat did not restore users: {payload!r}"
            )
        if int(restored.get("assistant_bubble_count") or 0) != 2:
            raise AssertionError(
                f"Chat renderer basic UI original chat did not restore assistants: {payload!r}"
            )
        if int(isolation_backend.get("message_count") or 0) != 0:
            raise AssertionError(
                f"Chat renderer basic UI isolated backend chat leaked messages: {payload!r}"
            )
        if int(restored_backend.get("user_message_count") or 0) != 2:
            raise AssertionError(
                f"Chat renderer basic UI original backend chat did not restore users: {payload!r}"
            )
        if int(restored_backend.get("assistant_message_count") or 0) < 2:
            raise AssertionError(
                "Chat renderer basic UI original backend chat did not restore "
                f"assistants: {payload!r}"
            )
    if provider_stream_ui is not None:
        provider = payload.get("provider_stream_ui")
        if not isinstance(provider, dict) or provider.get("ok") is not True:
            raise AssertionError(
                f"Chat renderer provider stream UI did not pass: {payload!r}"
            )
        if provider.get("chat_id") != provider_stream_ui.get("chat_id"):
            raise AssertionError(
                f"Chat renderer provider stream UI chat mismatch: {payload!r}"
            )
        if provider.get("prompt_text") != provider_stream_ui.get("prompt_text"):
            raise AssertionError(
                f"Chat renderer provider stream UI prompt mismatch: {payload!r}"
            )
        if provider.get("thinking_text") != provider_stream_ui.get(
            "public_progress_text"
        ):
            raise AssertionError(
                f"Chat renderer provider stream UI thinking mismatch: {payload!r}"
            )
        if provider.get("partial_text") != provider_stream_ui.get("partial_text"):
            raise AssertionError(
                f"Chat renderer provider stream UI partial mismatch: {payload!r}"
            )
        if provider.get("answer_text") != provider_stream_ui.get("assistant_text"):
            raise AssertionError(
                f"Chat renderer provider stream UI answer mismatch: {payload!r}"
            )
        if provider.get("queued_second_text") != provider_stream_ui.get(
            "queued_second_text"
        ):
            raise AssertionError(
                f"Chat renderer provider stream UI second queued draft mismatch: {payload!r}"
            )
        if provider.get("queued_third_text") != provider_stream_ui.get(
            "queued_third_text"
        ):
            raise AssertionError(
                f"Chat renderer provider stream UI third queued draft mismatch: {payload!r}"
            )
        thinking = provider.get("thinking")
        pending = provider.get("pending")
        partial = provider.get("partial")
        final = provider.get("final")
        rapid_queue = provider.get("rapid_queue")
        backend = provider.get("backend")
        if not isinstance(thinking, dict) or thinking.get("thinking_visible") is not True:
            raise AssertionError(
                f"Chat renderer provider stream UI missed thinking-only phase: {payload!r}"
            )
        if not isinstance(pending, dict) or pending.get("feedback_visible") is not True:
            raise AssertionError(
                f"Chat renderer provider stream UI missed pre-stream feedback: {payload!r}"
            )
        status_texts = pending.get("composer_turn_progress_status_texts")
        if (
            int(pending.get("pending_assistant_count") or 0) != 0
            or int(pending.get("composer_turn_progress_count") or 0) != 1
            or int(pending.get("composer_turn_progress_busy_count") or 0) != 1
            or not isinstance(status_texts, list)
            or not any("正在发送" in str(item) for item in status_texts)
        ):
            raise AssertionError(
                f"Chat renderer provider stream UI returned invalid pending feedback: {payload!r}"
            )
        if int(thinking.get("thinking_count") or 0) != 1:
            raise AssertionError(
                f"Chat renderer provider stream UI returned invalid thinking disclosure count: {payload!r}"
            )
        if int(thinking.get("thinking_open_count") or 0) != 0:
            raise AssertionError(
                f"Chat renderer provider stream UI exposed live provider reasoning by default: {payload!r}"
            )
        if not isinstance(partial, dict) or partial.get("partial_visible") is not True:
            raise AssertionError(
                f"Chat renderer provider stream UI missed partial answer phase: {payload!r}"
            )
        if (
            not isinstance(final, dict)
            or final.get("redirected_answer_visible") is not True
            or final.get("old_full_answer_visible") is not False
        ):
            raise AssertionError(
                "Chat renderer provider stream UI did not replace the retired "
                f"completion with the redirected successor: {payload!r}"
            )
        if int(thinking.get("assistant_bubble_count") or 0) != 1:
            raise AssertionError(
                f"Chat renderer provider stream UI split thinking assistant block: {payload!r}"
            )
        if int(partial.get("assistant_bubble_count") or 0) != 1:
            raise AssertionError(
                f"Chat renderer provider stream UI split partial assistant block: {payload!r}"
            )
        final_assistant_count = int(final.get("assistant_bubble_count") or 0)
        if final_assistant_count != 2:
            raise AssertionError(
                "Chat renderer provider stream UI did not preserve exactly the retired "
                "partial and redirected answer: "
                f"{payload!r}"
            )
        if int(final.get("response_avatar_count") or 0) > final_assistant_count:
            raise AssertionError(
                f"Chat renderer provider stream UI duplicated avatar: {payload!r}"
            )
        if int(final.get("action_bar_count") or 0) > final_assistant_count:
            raise AssertionError(
                f"Chat renderer provider stream UI duplicated action bar: {payload!r}"
            )
        pending_ms = int(provider.get("pending_visible_ms") or 0)
        thinking_ms = int(provider.get("thinking_visible_ms") or 0)
        partial_ms = int(provider.get("partial_visible_ms") or 0)
        final_ms = int(provider.get("final_visible_ms") or 0)
        if (
            pending_ms <= 0
            or thinking_ms <= pending_ms
            or partial_ms <= thinking_ms
            or final_ms <= partial_ms
        ):
            raise AssertionError(
                "Chat renderer provider stream UI did not preserve phased timing: "
                f"{payload!r}"
            )
        if not isinstance(backend, dict):
            raise AssertionError(
                f"Chat renderer provider stream UI omitted backend evidence: {payload!r}"
            )
        if not isinstance(rapid_queue, dict):
            raise AssertionError(
                f"Chat renderer provider stream UI omitted rapid queue evidence: {payload!r}"
            )
        rapid_final = rapid_queue.get("final")
        expected_queued = [
            provider_stream_ui.get("queued_second_text"),
            provider_stream_ui.get("queued_third_text"),
        ]
        if int(rapid_queue.get("queued_submission_count") or 0) != 2:
            raise AssertionError(
                f"Chat renderer provider stream UI did not queue two drafts: {payload!r}"
            )
        if rapid_queue.get("queued_before_first_answer") is not True:
            raise AssertionError(
                f"Chat renderer provider stream UI queued after the first answer: {payload!r}"
            )
        if rapid_queue.get("queued_drafts") != expected_queued:
            raise AssertionError(
                f"Chat renderer provider stream UI changed queued draft order: {payload!r}"
            )
        if rapid_queue.get("submission_intents") != ["supplement", "redirect"]:
            raise AssertionError(
                "Chat renderer provider stream UI did not distinguish default supplement "
                f"from explicit redirect: {payload!r}"
            )
        admitted_statuses = rapid_queue.get("admitted_statuses")
        if not isinstance(admitted_statuses, dict) or any(
            not isinstance(admitted_statuses.get(intent), dict)
            or admitted_statuses[intent].get("status") != "admitted"
            for intent in ("supplement", "redirect")
        ):
            raise AssertionError(
                "Chat renderer provider stream UI did not expose both commands as admitted "
                f"before application: {payload!r}"
            )
        if not isinstance(rapid_final, dict):
            raise AssertionError(
                f"Chat renderer provider stream UI omitted rapid final DOM: {payload!r}"
            )
        rapid_user_count = int(rapid_final.get("user_bubble_count") or 0)
        rapid_assistant_count = int(rapid_final.get("assistant_bubble_count") or 0)
        if rapid_user_count != 3 or rapid_assistant_count != 2:
            raise AssertionError(
                "Chat renderer provider stream UI returned an invalid retired/successor "
                f"turn count: {payload!r}"
            )
        if int(rapid_final.get("empty_assistant_count") or 0) != 0:
            raise AssertionError(
                f"Chat renderer provider stream UI left an empty rapid assistant: {payload!r}"
            )
        if int(rapid_final.get("response_avatar_count") or 0) > rapid_assistant_count:
            raise AssertionError(
                f"Chat renderer provider stream UI duplicated rapid avatars: {payload!r}"
            )
        if int(rapid_final.get("action_bar_count") or 0) > rapid_assistant_count:
            raise AssertionError(
                f"Chat renderer provider stream UI duplicated rapid action bars: {payload!r}"
            )
        if rapid_final.get("user_texts_in_fifo_order") is not True:
            raise AssertionError(
                f"Chat renderer provider stream UI reordered rapid user drafts: {payload!r}"
            )
        if any(
            rapid_final.get(field) is not True
            for field in (
                "assistant_texts_in_fifo_order",
                "old_partial_preserved",
                "old_full_answer_absent",
                "superseded_answer_absent",
                "redirect_answer_present",
                "conversation_items_preserve_fifo_order",
            )
        ):
            raise AssertionError(
                "Chat renderer provider stream UI violated redirect preservation or "
                f"suppression semantics: {payload!r}"
            )
        supplement_disposition = rapid_final.get("supplement_disposition")
        redirect_disposition = rapid_final.get("redirect_disposition")
        if (
            not isinstance(supplement_disposition, dict)
            or supplement_disposition.get("status") != "superseded"
            or not isinstance(redirect_disposition, dict)
            or redirect_disposition.get("status") != "applied"
        ):
            raise AssertionError(
                "Chat renderer provider stream UI did not persist explicit terminal "
                f"dispositions: {payload!r}"
            )
        if int(backend.get("user_message_count") or 0) != 3:
            raise AssertionError(
                f"Chat renderer provider stream UI backend lost rapid user messages: {payload!r}"
            )
        if int(backend.get("assistant_message_count") or 0) != 2:
            raise AssertionError(
                "Chat renderer provider stream UI backend returned the wrong retired/"
                f"successor assistant count: {payload!r}"
            )
        if str(backend.get("pending_user_message") or ""):
            raise AssertionError(
                f"Chat renderer provider stream UI left a pending rapid draft: {payload!r}"
            )
        if backend.get("user_texts_in_fifo_order") is not True:
            raise AssertionError(
                f"Chat renderer provider stream UI backend reordered rapid users: {payload!r}"
            )
        stop_resend = payload.get("provider_stop_resend_ui")
        if not isinstance(stop_resend, dict) or stop_resend.get("ok") is not True:
            raise AssertionError(
                f"Chat renderer provider stop-resend UI did not pass: {payload!r}"
            )
        if stop_resend.get("chat_id") != provider_stream_ui.get("stop_chat_id"):
            raise AssertionError(
                f"Chat renderer provider stop-resend chat mismatch: {payload!r}"
            )
        if stop_resend.get("stop_terminal") not in {"interrupted", "cancelled"}:
            raise AssertionError(
                f"Chat renderer provider stop-resend missed terminal state: {payload!r}"
            )
        if stop_resend.get("full_stopped_answer_absent") is not True:
            raise AssertionError(
                f"Chat renderer provider stop-resend leaked stopped answer: {payload!r}"
            )
        stop_final = stop_resend.get("final")
        stop_backend = stop_resend.get("backend")
        if not isinstance(stop_final, dict) or not isinstance(stop_backend, dict):
            raise AssertionError(
                f"Chat renderer provider stop-resend omitted final evidence: {payload!r}"
            )
        if int(stop_final.get("user_bubble_count") or 0) != 2:
            raise AssertionError(
                f"Chat renderer provider stop-resend lost resent user: {payload!r}"
            )
        if int(stop_final.get("empty_assistant_count") or 0) != 0:
            raise AssertionError(
                f"Chat renderer provider stop-resend left empty assistant: {payload!r}"
            )
        if int(stop_backend.get("user_message_count") or 0) != 2:
            raise AssertionError(
                f"Chat renderer provider stop-resend backend lost user: {payload!r}"
            )
        if str(stop_backend.get("pending_user_message") or ""):
            raise AssertionError(
                f"Chat renderer provider stop-resend left pending user: {payload!r}"
            )

        route_switch = payload.get("provider_stream_route_switch_ui")
        if not isinstance(route_switch, dict) or route_switch.get("ok") is not True:
            raise AssertionError(
                f"Chat renderer provider stream route-switch UI did not pass: {payload!r}"
            )
        if route_switch.get("chat_id") != provider_stream_ui.get("switch_chat_id"):
            raise AssertionError(
                f"Chat renderer provider stream route-switch chat mismatch: {payload!r}"
            )
        if route_switch.get("isolation_chat_id") != provider_stream_ui.get(
            "switch_isolation_chat_id"
        ):
            raise AssertionError(
                f"Chat renderer provider stream route-switch isolation mismatch: {payload!r}"
            )
        switch_isolation = route_switch.get("isolation")
        switch_restored = route_switch.get("restored")
        switch_backend = route_switch.get("backend")
        if not all(
            isinstance(item, dict)
            for item in (switch_isolation, switch_restored, switch_backend)
        ):
            raise AssertionError(
                f"Chat renderer provider stream route-switch omitted evidence: {payload!r}"
            )
        if int(switch_isolation.get("user_bubble_count") or 0) != 0 or int(
            switch_isolation.get("assistant_bubble_count") or 0
        ) != 0:
            raise AssertionError(
                f"Chat renderer provider stream route-switch leaked messages: {payload!r}"
            )
        if int(switch_restored.get("user_bubble_count") or 0) != 1 or int(
            switch_restored.get("assistant_bubble_count") or 0
        ) != 1:
            raise AssertionError(
                f"Chat renderer provider stream route-switch did not restore one turn: {payload!r}"
            )
        if int(switch_restored.get("empty_assistant_count") or 0) != 0:
            raise AssertionError(
                f"Chat renderer provider stream route-switch restored empty assistant: {payload!r}"
            )
        switch_backend_assistant_count = int(
            switch_backend.get("assistant_message_count") or 0
        )
        if (
            int(switch_backend.get("user_message_count") or 0) != 1
            or not 1 <= switch_backend_assistant_count <= 2
            or int(switch_backend.get("thinking_occurrence_count") or 0) != 1
            or int(switch_backend.get("answer_occurrence_count") or 0) != 1
        ):
            raise AssertionError(
                f"Chat renderer provider stream route-switch backend did not settle: {payload!r}"
            )
        if str(switch_backend.get("pending_user_message") or ""):
            raise AssertionError(
                f"Chat renderer provider stream route-switch left pending user: {payload!r}"
            )
    return payload


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _chat_basic_ui_send_evidence(
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    basic = payload.get("basic_chat_ui_send")
    if not isinstance(basic, dict):
        return None

    def stage_evidence(name: str) -> dict[str, Any] | None:
        stage = basic.get(name)
        if not isinstance(stage, dict):
            return None
        return {
            "user_visible_ms": _optional_int(stage.get("user_visible_ms")),
            "assistant_visible_ms": _optional_int(
                stage.get("assistant_visible_ms")
            ),
            "user_bubble_count": _optional_int(stage.get("user_bubble_count")),
            "assistant_bubble_count": _optional_int(
                stage.get("assistant_bubble_count")
            ),
            "response_avatar_count": _optional_int(
                stage.get("response_avatar_count")
            ),
            "action_bar_count": _optional_int(stage.get("action_bar_count")),
            "empty_assistant_count": _optional_int(
                stage.get("empty_assistant_count")
            ),
            "body_text_length": _optional_int(stage.get("body_text_length")),
        }

    def backend_evidence(name: str) -> dict[str, Any] | None:
        stage = basic.get(name)
        if not isinstance(stage, dict):
            return None
        return {
            "chat_id": stage.get("chat_id"),
            "status": stage.get("status"),
            "pending_user_message": stage.get("pending_user_message"),
            "message_count": _optional_int(stage.get("message_count")),
            "user_message_count": _optional_int(stage.get("user_message_count")),
            "assistant_message_count": _optional_int(
                stage.get("assistant_message_count")
            ),
        }

    matrix = basic.get("experience_matrix")
    if not isinstance(matrix, list):
        matrix = []
    final = basic.get("final") if isinstance(basic.get("final"), dict) else {}
    final_stable = (
        basic.get("final_stable")
        if isinstance(basic.get("final_stable"), dict)
        else {}
    )
    return {
        "ok": basic.get("ok") is True,
        "chat_id": basic.get("chat_id"),
        "isolation_chat_id": basic.get("isolation_chat_id"),
        "max_user_feedback_ms": _optional_int(basic.get("max_user_feedback_ms")),
        "max_assistant_feedback_ms": _optional_int(
            basic.get("max_assistant_feedback_ms")
        ),
        "backend_turn_observed": bool(basic.get("backend_turn_observed")),
        "console_chat_post_count": _optional_int(
            basic.get("console_chat_post_count")
        ),
        "console_chat_post_statuses": [
            _optional_int(status)
            for status in (
                basic.get("console_chat_post_statuses")
                if isinstance(basic.get("console_chat_post_statuses"), list)
                else []
            )
        ],
        "experience_matrix": [
            {
                "id": str(item.get("id")),
                "passed": item.get("passed") is True,
            }
            for item in matrix
            if isinstance(item, dict) and item.get("id")
        ],
        "initial": {
            "user_bubble_count": _optional_int(
                (basic.get("initial") or {}).get("user_bubble_count")
                if isinstance(basic.get("initial"), dict)
                else None
            ),
            "assistant_bubble_count": _optional_int(
                (basic.get("initial") or {}).get("assistant_bubble_count")
                if isinstance(basic.get("initial"), dict)
                else None
            ),
            "body_text_length": _optional_int(
                (basic.get("initial") or {}).get("body_text_length")
                if isinstance(basic.get("initial"), dict)
                else None
            ),
        },
        "initial_backend": backend_evidence("initial_backend"),
        "first": stage_evidence("first"),
        "second": stage_evidence("second"),
        "final": {
            "user_bubble_count": _optional_int(final.get("user_bubble_count")),
            "assistant_bubble_count": _optional_int(
                final.get("assistant_bubble_count")
            ),
            "response_avatar_count": _optional_int(
                final.get("response_avatar_count")
            ),
            "action_bar_count": _optional_int(final.get("action_bar_count")),
            "empty_assistant_count": _optional_int(
                final.get("empty_assistant_count")
            ),
            "body_text_length": _optional_int(final.get("body_text_length")),
        },
        "final_stable": {
            "user_bubble_count": _optional_int(
                final_stable.get("user_bubble_count")
            ),
            "assistant_bubble_count": _optional_int(
                final_stable.get("assistant_bubble_count")
            ),
            "response_avatar_count": _optional_int(
                final_stable.get("response_avatar_count")
            ),
            "action_bar_count": _optional_int(final_stable.get("action_bar_count")),
            "empty_assistant_count": _optional_int(
                final_stable.get("empty_assistant_count")
            ),
            "body_text_length": _optional_int(final_stable.get("body_text_length")),
        },
        "final_backend": backend_evidence("final_backend"),
        "isolation": {
            "user_bubble_count": _optional_int(
                (basic.get("isolation") or {}).get("user_bubble_count")
                if isinstance(basic.get("isolation"), dict)
                else None
            ),
            "assistant_bubble_count": _optional_int(
                (basic.get("isolation") or {}).get("assistant_bubble_count")
                if isinstance(basic.get("isolation"), dict)
                else None
            ),
            "body_text_length": _optional_int(
                (basic.get("isolation") or {}).get("body_text_length")
                if isinstance(basic.get("isolation"), dict)
                else None
            ),
        },
        "isolation_backend": backend_evidence("isolation_backend"),
        "restored": {
            "user_bubble_count": _optional_int(
                (basic.get("restored") or {}).get("user_bubble_count")
                if isinstance(basic.get("restored"), dict)
                else None
            ),
            "assistant_bubble_count": _optional_int(
                (basic.get("restored") or {}).get("assistant_bubble_count")
                if isinstance(basic.get("restored"), dict)
                else None
            ),
            "body_text_length": _optional_int(
                (basic.get("restored") or {}).get("body_text_length")
                if isinstance(basic.get("restored"), dict)
                else None
            ),
        },
        "restored_backend": backend_evidence("restored_backend"),
    }


def _chat_provider_stream_ui_evidence(
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    provider = payload.get("provider_stream_ui")
    if not isinstance(provider, dict):
        return None

    def phase_evidence(name: str) -> dict[str, Any] | None:
        phase = provider.get(name)
        if not isinstance(phase, dict):
            return None
        thinking_statuses = phase.get("thinking_statuses")
        if not isinstance(thinking_statuses, list):
            thinking_statuses = []
        return {
            "phase": phase.get("phase"),
            "thinking_visible": bool(phase.get("thinking_visible")),
            "partial_visible": bool(phase.get("partial_visible")),
            "answer_visible": bool(phase.get("answer_visible")),
            "redirected_answer_visible": bool(
                phase.get("redirected_answer_visible")
            ),
            "old_full_answer_visible": bool(
                phase.get("old_full_answer_visible")
            ),
            "answer_occurrence_count": _optional_int(
                phase.get("answer_occurrence_count")
            ),
            "assistant_bubble_count": _optional_int(
                phase.get("assistant_bubble_count")
            ),
            "thinking_count": _optional_int(phase.get("thinking_count")),
            "thinking_statuses": [
                str(status) for status in thinking_statuses if status is not None
            ],
            "response_avatar_count": _optional_int(
                phase.get("response_avatar_count")
            ),
            "action_bar_count": _optional_int(phase.get("action_bar_count")),
            "empty_assistant_count": _optional_int(
                phase.get("empty_assistant_count")
            ),
            "body_text_length": _optional_int(phase.get("body_text_length")),
        }

    backend = provider.get("backend") if isinstance(provider.get("backend"), dict) else {}
    rapid_queue = (
        provider.get("rapid_queue")
        if isinstance(provider.get("rapid_queue"), dict)
        else {}
    )
    rapid_final = (
        rapid_queue.get("final")
        if isinstance(rapid_queue.get("final"), dict)
        else {}
    )
    stop_resend = (
        payload.get("provider_stop_resend_ui")
        if isinstance(payload.get("provider_stop_resend_ui"), dict)
        else {}
    )
    stop_final = (
        stop_resend.get("final")
        if isinstance(stop_resend.get("final"), dict)
        else {}
    )
    stop_backend = (
        stop_resend.get("backend")
        if isinstance(stop_resend.get("backend"), dict)
        else {}
    )
    route_switch = (
        payload.get("provider_stream_route_switch_ui")
        if isinstance(payload.get("provider_stream_route_switch_ui"), dict)
        else {}
    )
    switch_isolation = (
        route_switch.get("isolation")
        if isinstance(route_switch.get("isolation"), dict)
        else {}
    )
    switch_restored = (
        route_switch.get("restored")
        if isinstance(route_switch.get("restored"), dict)
        else {}
    )
    switch_backend = (
        route_switch.get("backend")
        if isinstance(route_switch.get("backend"), dict)
        else {}
    )
    return {
        "ok": provider.get("ok") is True,
        "chat_id": provider.get("chat_id"),
        "queued_second_text": provider.get("queued_second_text"),
        "queued_third_text": provider.get("queued_third_text"),
        "thinking_visible_ms": _optional_int(provider.get("thinking_visible_ms")),
        "partial_visible_ms": _optional_int(provider.get("partial_visible_ms")),
        "final_visible_ms": _optional_int(provider.get("final_visible_ms")),
        "thinking": phase_evidence("thinking"),
        "partial": phase_evidence("partial"),
        "final": phase_evidence("final"),
        "active_controls": {
            "primary_label": (provider.get("active_controls") or {}).get(
                "primary_label"
            )
            if isinstance(provider.get("active_controls"), dict)
            else None,
            "intent_selector_label": (provider.get("active_controls") or {}).get(
                "intent_selector_label"
            )
            if isinstance(provider.get("active_controls"), dict)
            else None,
            "stop_label": (provider.get("active_controls") or {}).get("stop_label")
            if isinstance(provider.get("active_controls"), dict)
            else None,
            "explicit_intents": (provider.get("active_controls") or {}).get(
                "explicit_intents"
            )
            if isinstance(provider.get("active_controls"), dict)
            else None,
        },
        "rapid_queue": {
            "queued_submission_count": _optional_int(
                rapid_queue.get("queued_submission_count")
            ),
            "queued_before_first_answer": bool(
                rapid_queue.get("queued_before_first_answer")
            ),
            "queued_drafts": rapid_queue.get("queued_drafts"),
            "submission_intents": rapid_queue.get("submission_intents"),
            "admitted_statuses": rapid_queue.get("admitted_statuses"),
            "final": {
                "user_bubble_count": _optional_int(
                    rapid_final.get("user_bubble_count")
                ),
                "assistant_bubble_count": _optional_int(
                    rapid_final.get("assistant_bubble_count")
                ),
                "empty_assistant_count": _optional_int(
                    rapid_final.get("empty_assistant_count")
                ),
                "response_avatar_count": _optional_int(
                    rapid_final.get("response_avatar_count")
                ),
                "action_bar_count": _optional_int(
                    rapid_final.get("action_bar_count")
                ),
                "user_texts_in_fifo_order": bool(
                    rapid_final.get("user_texts_in_fifo_order")
                ),
                "assistant_texts_in_fifo_order": bool(
                    rapid_final.get("assistant_texts_in_fifo_order")
                ),
                "old_partial_preserved": bool(
                    rapid_final.get("old_partial_preserved")
                ),
                "old_full_answer_absent": bool(
                    rapid_final.get("old_full_answer_absent")
                ),
                "superseded_answer_absent": bool(
                    rapid_final.get("superseded_answer_absent")
                ),
                "redirect_answer_present": bool(
                    rapid_final.get("redirect_answer_present")
                ),
                "supplement_disposition": rapid_final.get(
                    "supplement_disposition"
                ),
                "redirect_disposition": rapid_final.get("redirect_disposition"),
                "conversation_items_preserve_fifo_order": bool(
                    rapid_final.get("conversation_items_preserve_fifo_order")
                ),
            },
        },
        "backend": {
            "pending_user_message": backend.get("pending_user_message"),
            "message_count": _optional_int(backend.get("message_count")),
            "user_message_count": _optional_int(backend.get("user_message_count")),
            "assistant_message_count": _optional_int(
                backend.get("assistant_message_count")
            ),
            "user_texts_in_fifo_order": bool(
                backend.get("user_texts_in_fifo_order")
            ),
        },
        "stop_resend": {
            "ok": stop_resend.get("ok") is True,
            "chat_id": stop_resend.get("chat_id"),
            "stop_terminal": stop_resend.get("stop_terminal"),
            "full_stopped_answer_absent": bool(
                stop_resend.get("full_stopped_answer_absent")
            ),
            "final": {
                "user_bubble_count": _optional_int(
                    stop_final.get("user_bubble_count")
                ),
                "assistant_bubble_count": _optional_int(
                    stop_final.get("assistant_bubble_count")
                ),
                "empty_assistant_count": _optional_int(
                    stop_final.get("empty_assistant_count")
                ),
                "response_avatar_count": _optional_int(
                    stop_final.get("response_avatar_count")
                ),
                "action_bar_count": _optional_int(
                    stop_final.get("action_bar_count")
                ),
            },
            "backend": {
                "user_message_count": _optional_int(
                    stop_backend.get("user_message_count")
                ),
                "assistant_message_count": _optional_int(
                    stop_backend.get("assistant_message_count")
                ),
                "pending_user_message": stop_backend.get("pending_user_message"),
            },
        },
        "route_switch": {
            "ok": route_switch.get("ok") is True,
            "chat_id": route_switch.get("chat_id"),
            "isolation_chat_id": route_switch.get("isolation_chat_id"),
            "isolation": {
                "user_bubble_count": _optional_int(
                    switch_isolation.get("user_bubble_count")
                ),
                "assistant_bubble_count": _optional_int(
                    switch_isolation.get("assistant_bubble_count")
                ),
            },
            "restored": {
                "user_bubble_count": _optional_int(
                    switch_restored.get("user_bubble_count")
                ),
                "assistant_bubble_count": _optional_int(
                    switch_restored.get("assistant_bubble_count")
                ),
                "empty_assistant_count": _optional_int(
                    switch_restored.get("empty_assistant_count")
                ),
                "response_avatar_count": _optional_int(
                    switch_restored.get("response_avatar_count")
                ),
                "action_bar_count": _optional_int(
                    switch_restored.get("action_bar_count")
                ),
            },
            "backend": {
                "user_message_count": _optional_int(
                    switch_backend.get("user_message_count")
                ),
                "assistant_message_count": _optional_int(
                    switch_backend.get("assistant_message_count")
                ),
                "thinking_occurrence_count": _optional_int(
                    switch_backend.get("thinking_occurrence_count")
                ),
                "answer_occurrence_count": _optional_int(
                    switch_backend.get("answer_occurrence_count")
                ),
                "pending_user_message": switch_backend.get("pending_user_message"),
            },
        },
    }


def _run_browser_content_renderer_smoke(
    state: dict[str, Any],
    *,
    run_id: str,
    timeout: float,
    project_root: Path | None = None,
) -> dict[str, Any]:
    root = project_root or _project_root()
    console_dir = root / "console"
    script = console_dir / "electron" / "browserContentRuntimeSmoke.cjs"
    if not script.is_file():
        raise FileNotFoundError(f"Browser content Electron smoke is missing: {script}")

    expected_contract_id = "loop-browser-content-draft-preview-contract"
    command = [
        str(_console_electron_binary(root)),
        str(script),
        "--base-url",
        _state_base_url(state),
        "--desktop-token",
        _state_headers(state)[DESKTOP_TOKEN_HEADER],
        "--run-id",
        run_id,
        "--expected-contract-id",
        expected_contract_id,
        "--timeout-ms",
        str(max(1, int(timeout * 1000))),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(console_dir),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=timeout + 20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "Browser content renderer Electron smoke timed out: "
            f"cmd={command!r} stdout={exc.stdout!r} stderr={exc.stderr!r}"
        ) from exc
    if result.returncode != 0:
        raise AssertionError(
            "Browser content renderer Electron smoke failed: "
            f"exit={result.returncode} cmd={command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    payload = _extract_json_object(result.stdout)
    if payload.get("ok") is not True or payload.get("run_id") != run_id:
        raise AssertionError(f"Unexpected Browser content renderer payload: {payload!r}")
    if payload.get("renderer_run_status") != "completed":
        raise AssertionError(f"Browser content renderer did not see completed run: {payload!r}")
    if payload.get("renderer_authority_status") != "succeeded":
        raise AssertionError(f"Browser content renderer authority mismatch: {payload!r}")
    if payload.get("renderer_pending_approval") is not False:
        raise AssertionError(f"Browser content renderer pending approval mismatch: {payload!r}")
    if payload.get("captured_contract_id") != expected_contract_id:
        raise AssertionError(f"Browser content renderer export contract mismatch: {payload!r}")
    export_statuses = payload.get("export_statuses")
    captured_export_status = payload.get("captured_export_status")
    if captured_export_status != 200 and (
        not isinstance(export_statuses, list) or 200 not in export_statuses
    ):
        raise AssertionError(
            "Browser content renderer did not observe a successful export response: "
            f"{payload!r}"
        )
    return payload


def _assert_task_center_multi_window_stale_control(
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    timeout: float,
) -> str:
    profile_id = f"smoke-window-{uuid.uuid4().hex[:8]}"
    profile_body = {
        "id": profile_id,
        "name": "Smoke Task Center Window",
        "description": "packaged Task Center multi-window smoke profile",
        "allowed_domains": [],
        "agent_control_enabled": True,
        "risk_policy": "write",
        "metadata": {"packaged_smoke": True},
    }
    _status, _body, profile = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/browser-workbench/profiles",
        payload=profile_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(profile, dict) or profile.get("id") != profile_id:
        raise AssertionError(f"Expected Task Center smoke profile, got {profile!r}")

    run_id = f"browser-taskcenter-window-smoke-{uuid.uuid4().hex[:8]}"
    task_body = {
        "run_id": run_id,
        "profile_id": profile_id,
        "access": "write",
        "mode": "background_observable",
        "instruction": "packaged Task Center multi-window stale-control smoke",
        "requires_foreground_confirmation": False,
        "max_actions": 200,
        "max_duration_seconds": 60,
        "metadata": {
            "workspace_dir": str(workspace_dir),
            "agent_id": "default",
            "session_id": "browser-taskcenter-window-smoke",
        },
        "actions": [
            {
                "action": "click",
                "profile_id": profile_id,
                "selector": "#save",
                "access": "write",
                "reason": "packaged Task Center stale-control smoke",
            }
        ],
    }
    _status, _body, task = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/browser-workbench/tasks",
        payload=task_body,
        headers=_state_headers(state),
        timeout=timeout,
    )
    if not isinstance(task, dict) or task.get("run_id") != run_id:
        raise AssertionError(f"Expected Task Center browser task response, got {task!r}")

    deadline = time.monotonic() + timeout
    summary: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
        if summary.get("status") == "running":
            break
        time.sleep(0.1)
    if summary is None or summary.get("status") != "running":
        raise AssertionError(f"Task Center smoke run did not become running: {summary!r}")
    recovery = summary.get("recovery") if isinstance(summary.get("recovery"), dict) else {}
    if recovery.get("can_pause") is not True:
        raise AssertionError(f"Task Center smoke run did not advertise can_pause: {summary!r}")

    try:
        _run_task_center_multi_window_smoke(
            state,
            run_id=run_id,
            timeout=timeout,
        )
        paused_summary = _find_runtime_run_by_id(state, run_id=run_id, timeout=timeout)
        paused_recovery = (
            paused_summary.get("recovery")
            if isinstance(paused_summary.get("recovery"), dict)
            else {}
        )
        if paused_summary.get("authority_status") != "paused":
            raise AssertionError(
                "Task Center multi-window smoke did not leave run paused: "
                f"{paused_summary!r}"
            )
        if paused_recovery.get("can_resume") is not True:
            raise AssertionError(
                "Task Center multi-window smoke did not refresh resumable state: "
                f"{paused_summary!r}"
            )
    finally:
        with contextlib.suppress(Exception):
            _json_request(
                "POST",
                f"{_state_base_url(state)}/api/browser-workbench/tasks/{run_id}/cancel",
                payload={},
                headers=_state_headers(state),
                timeout=timeout,
            )
    return run_id


def _run_smoke(args: argparse.Namespace) -> None:
    # The harness imports selected runtime stores in-process to seed restart
    # fixtures.  Release smoke tests must never consult the developer's login
    # keychain while doing so: on macOS a locked or approval-gated keychain can
    # block SecItemCopyMatching indefinitely and make a healthy package appear
    # hung.  The packaged child already receives the same policy below.
    os.environ.setdefault("LANTAI_DISABLE_KEYRING", "1")
    os.environ.setdefault("LANTAI_SKIP_KEYRING", "1")

    app_path = Path(args.app).expanduser().resolve()
    executable = _find_app_executable(app_path)
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-packaged-app-smoke-"))
    state_path = smoke_root / "desktop-lifecycle-state.json"
    user_data_dir = smoke_root / "user-data"
    app_log_path = smoke_root / "app.log"
    reminder_delivery_smoke_file = smoke_root / "reminder-delivery-worker.jsonl"
    legacy_execution_observation_file = (
        smoke_root / "legacy-execution-observations.jsonl"
    )
    legacy_execution_observation_file.touch()
    legacy_writer_observation_file = smoke_root / "legacy-writer-observations.jsonl"
    legacy_writer_observation_file.touch()
    started_at = _utc_now_iso()
    print(f"Smoke root: {smoke_root}")

    env = os.environ.copy()
    env.update(
        {
            "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE": str(state_path),
            "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data_dir),
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "1",
            "LANTAI_REMINDER_DELIVERY_INTERVAL_SECONDS": "0.25",
            "LANTAI_REMINDER_DELIVERY_DEDUPE_SECONDS": "7200",
            "LANTAI_REMINDER_DELIVERY_SMOKE_FILE": str(reminder_delivery_smoke_file),
            "LANTAI_REMINDER_DELIVERY_TEST_MODE": "1",
            "LANTAI_RUNTIME_LEGACY_EXECUTION_OBSERVATION_FILE": str(
                legacy_execution_observation_file,
            ),
            "LANTAI_RUNTIME_LEGACY_WRITER_OBSERVATION_FILE": str(
                legacy_writer_observation_file,
            ),
        }
    )
    if args.headless:
        env["LANTAI_DESKTOP_HEADLESS_SMOKE"] = "1"

    proc: subprocess.Popen[str] | None = None
    smoke_model_context: _LocalOpenAISmokeServer | None = None
    smoke_model: _LocalOpenAISmokeServer | None = None
    smoke_model_config: dict[str, Any] | None = None
    app_relaunch_summary: dict[str, Any] | None = None
    restarted_customization_summary: dict[str, Any] | None = None
    phase = "initial packaged app launch"
    restore_termination_signal_handlers = _install_termination_signal_handlers()
    try:
        restarted_backend_pid: int | None = None
        smoke_model_context = _LocalOpenAISmokeServer()
        smoke_model = smoke_model_context.__enter__()
        smoke_model_config = _write_smoke_openai_provider_config(
            user_data_dir,
            base_url=smoke_model.base_url,
        )
        with app_log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [str(executable)],
                cwd=str(app_path.parent),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            state = _wait_for_state(
                state_path,
                timeout=args.timeout,
                predicate=lambda payload: int(payload.get("sequence") or 0) >= 1,
                process=proc,
                expected_distribution=args.distribution,
            )
            _assert_ready_state(state, expected_distribution=args.distribution)
            ready_pid = _assert_desktop_readiness(state, timeout=args.timeout)
            if ready_pid != state.get("backend_pid"):
                raise AssertionError(
                    f"Readiness pid {ready_pid} did not match smoke state {state!r}"
                )
            _assert_distribution_tools(
                state,
                expected_distribution=args.distribution,
                timeout=args.timeout,
            )
            _assert_browser_capabilities(
                state,
                expected_distribution=args.distribution,
                timeout=args.timeout,
            )
            customization_summary = _assert_packaged_customization_integrity(
                state,
                timeout=args.timeout,
            )
            reminder_delivery_summary = _assert_packaged_reminder_delivery_worker(
                state,
                smoke_file=reminder_delivery_smoke_file,
                timeout=args.timeout,
            )
            restart_decision_run_ids = _assert_packaged_restart_decision_refresh(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                timeout=args.timeout,
            )
            operator_effect_recovery = _assert_packaged_operator_effect_recovery(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                expected_distribution=args.distribution,
                timeout=args.timeout,
            )
            loop_trial_run_id = _assert_packaged_loop_trial_recovery(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                expected_distribution=args.distribution,
                timeout=args.timeout,
            )
            browser_content_run_id = _assert_packaged_browser_content_trial_handoff(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                expected_distribution=args.distribution,
                timeout=args.timeout,
            )
            _run_browser_content_renderer_smoke(
                state,
                run_id=browser_content_run_id,
                timeout=args.timeout,
            )
            browser_run_id = _assert_browser_preflight_failure_settles_runtime_run(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                timeout=args.timeout,
            )
            _assert_browser_positive_pause_resume(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                timeout=args.timeout,
            )
            _assert_task_center_multi_window_stale_control(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                timeout=args.timeout,
            )
            session_id, chat_id = _exercise_console_model_success_path(
                state,
                timeout=args.timeout,
            )
            _assert_runtime_event_stream_reconnect_recovers_terminal(
                state,
                chat_id=chat_id,
                timeout=args.timeout,
                expected_terminal_type="turn.completed",
                expected_terminal_statuses={"completed", "finished"},
                expected_final_task_statuses={"completed", "finished"},
            )
            basic_ui_send = _seed_chat_basic_ui_send(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                timeout=args.timeout,
            )
            provider_stream_ui = _seed_chat_provider_stream_ui(
                state,
                workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                timeout=args.timeout,
            )
            chat_renderer_stream_smoke = _run_chat_runtime_stream_renderer_smoke(
                state,
                chat_id=chat_id,
                timeout=args.timeout,
                basic_ui_send=basic_ui_send,
                provider_stream_ui=provider_stream_ui,
                expected_terminal_type="turn.completed",
                expected_terminal_statuses={"completed", "finished"},
            )
            _assert_terminal_interrupt_is_noop(
                state,
                chat_id=chat_id,
                timeout=args.timeout,
            )
            _assert_canonical_runtime_controls(
                state,
                chat_id=chat_id,
                session_id=session_id,
                timeout=args.timeout,
                destructive_cleanup=not args.restart_cycle,
                expected_statuses={"completed", "succeeded"},
            )

            if args.restart_cycle:
                phase = "managed backend restart"
                old_sequence = int(state.get("sequence") or 0)
                old_pid = int(state["backend_pid"])
                _crash_managed_backend_for_restart(old_pid)
                restarted = _wait_for_state(
                    state_path,
                    timeout=args.timeout,
                    predicate=lambda payload: (
                        int(payload.get("sequence") or 0) > old_sequence
                        and int(payload.get("backend_pid") or 0) > 0
                        and int(payload.get("backend_pid") or 0) != old_pid
                    ),
                    process=proc,
                    expected_distribution=args.distribution,
                )
                _assert_ready_state(restarted, expected_distribution=args.distribution)
                _assert_desktop_readiness(restarted, timeout=args.timeout)
                restarted_backend_pid = int(restarted["backend_pid"])
                _assert_distribution_tools(
                    restarted,
                    expected_distribution=args.distribution,
                    timeout=args.timeout,
                )
                _assert_browser_capabilities(
                    restarted,
                    expected_distribution=args.distribution,
                    timeout=args.timeout,
                )
                restarted_customization_summary = (
                    _assert_packaged_customization_integrity(
                        restarted,
                        timeout=args.timeout,
                    )
                )
                reminder_delivery_restart_summary = (
                    _assert_packaged_reminder_delivery_restart_deduped(
                        restarted,
                        smoke_file=reminder_delivery_smoke_file,
                        previous=reminder_delivery_summary,
                        timeout=args.timeout,
                    )
                )
                _assert_packaged_restart_decision_summaries(
                    restarted,
                    run_ids=restart_decision_run_ids,
                    timeout=args.timeout,
                )
                operator_effect_restart_summary = _find_runtime_run_by_id(
                    restarted,
                    run_id=operator_effect_recovery["run_id"],
                    timeout=args.timeout,
                )
                if operator_effect_restart_summary.get("authority_status") != "succeeded":
                    raise AssertionError(
                        "Post-restart operator effect recovery lost authority status: "
                        f"{operator_effect_restart_summary!r}"
                    )
                if operator_effect_restart_summary.get("status") not in {
                    "completed",
                    "succeeded",
                }:
                    raise AssertionError(
                        "Post-restart operator effect recovery was not completed: "
                        f"{operator_effect_restart_summary!r}"
                    )
                post_restart_operator_effect_statuses = _runtime_effect_statuses(
                    user_data_dir / "working" / "workspaces" / "default",
                    run_id=operator_effect_recovery["run_id"],
                    attempt_id=operator_effect_recovery["attempt_id"],
                    effect_id=operator_effect_recovery["effect_id"],
                )
                if post_restart_operator_effect_statuses != operator_effect_recovery[
                    "effect_statuses"
                ]:
                    raise AssertionError(
                        "Post-restart operator effect journal changed, which would "
                        "suggest replay or mutation: "
                        f"before={operator_effect_recovery['effect_statuses']!r} "
                        f"after={post_restart_operator_effect_statuses!r}"
                    )
                restarted_loop_summary = _assert_loop_trial_summary(
                    restarted,
                    run_id=loop_trial_run_id,
                    expected_status="stalled",
                    expected_error_code="loop_no_progress_exhausted",
                    timeout=args.timeout,
                )
                _assert_loop_trial_export_package(
                    restarted,
                    summary=restarted_loop_summary,
                    timeout=args.timeout,
                )
                restarted_browser_content_summary = _assert_loop_succeeded_projection(
                    restarted,
                    run_id=browser_content_run_id,
                    timeout=args.timeout,
                )
                _assert_loop_trial_export_package(
                    restarted,
                    summary=restarted_browser_content_summary,
                    timeout=args.timeout,
                    expected_contract_id="loop-browser-content-draft-preview-contract",
                    expected_report_id=(
                        f"{browser_content_run_id}-eval-browser-content-1-approved"
                    ),
                    reason=(
                        "packaged LOOP-05 Browser content handoff export "
                        "smoke after restart"
                    ),
                    version_smoke="packaged-browser-content-trial-restart",
                )
                _run_browser_content_renderer_smoke(
                    restarted,
                    run_id=browser_content_run_id,
                    timeout=args.timeout,
                )
                _assert_runtime_run_remains_failed_after_restart(
                    restarted,
                    run_id=browser_run_id,
                    timeout=args.timeout,
                )
                restarted_chat_id = _assert_persisted_success_events(
                    restarted,
                    session_id=session_id,
                    timeout=args.timeout,
                )
                if restarted_chat_id != chat_id:
                    raise AssertionError(
                        "Restarted packaged app backend returned a different chat id "
                        f"for session {session_id!r}: {restarted_chat_id!r} != {chat_id!r}"
                    )
                _assert_runtime_event_stream_reconnect_recovers_terminal(
                    restarted,
                    chat_id=chat_id,
                    timeout=args.timeout,
                    expected_terminal_type="turn.completed",
                    expected_terminal_statuses={"completed", "finished"},
                    expected_final_task_statuses={"completed", "finished"},
                )
                post_restart_basic_ui_send = _seed_chat_basic_ui_send(
                    restarted,
                    workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                    timeout=args.timeout,
                )
                post_restart_provider_stream_ui = _seed_chat_provider_stream_ui(
                    restarted,
                    workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                    timeout=args.timeout,
                )
                post_restart_chat_renderer_stream_smoke = (
                    _run_chat_runtime_stream_renderer_smoke(
                        restarted,
                        chat_id=chat_id,
                        timeout=args.timeout,
                        basic_ui_send=post_restart_basic_ui_send,
                        provider_stream_ui=post_restart_provider_stream_ui,
                        expected_terminal_type="turn.completed",
                        expected_terminal_statuses={"completed", "finished"},
                    )
                )
                _assert_terminal_interrupt_is_noop(
                    restarted,
                    chat_id=chat_id,
                    timeout=args.timeout,
                )
                _assert_canonical_runtime_controls(
                    restarted,
                    chat_id=chat_id,
                    session_id=session_id,
                    timeout=args.timeout,
                    destructive_cleanup=not args.app_relaunch_cycle,
                    expected_statuses={"completed", "succeeded"},
                )

            if args.app_relaunch_cycle:
                phase = "desktop app relaunch"
                previous_state = restarted if args.restart_cycle else state
                previous_nonce = str(
                    previous_state.get("desktop_instance_nonce") or ""
                ).strip()
                if not previous_nonce:
                    raise AssertionError(
                        "Cannot verify packaged app relaunch without a desktop instance nonce: "
                        f"{previous_state!r}"
                    )
                _stop_app(proc)
                proc = None
                state_path.unlink(missing_ok=True)
                proc = subprocess.Popen(
                    [str(executable)],
                    cwd=str(app_path.parent),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                relaunched = _wait_for_state(
                    state_path,
                    timeout=args.timeout,
                    predicate=lambda payload: _is_new_desktop_instance(
                        payload,
                        previous_nonce=previous_nonce,
                    ),
                    process=proc,
                    expected_distribution=args.distribution,
                )
                _assert_ready_state(relaunched, expected_distribution=args.distribution)
                _assert_desktop_readiness(relaunched, timeout=args.timeout)
                _assert_distribution_tools(
                    relaunched,
                    expected_distribution=args.distribution,
                    timeout=args.timeout,
                )
                _assert_browser_capabilities(
                    relaunched,
                    expected_distribution=args.distribution,
                    timeout=args.timeout,
                )
                relaunched_customization_summary = (
                    _assert_packaged_customization_integrity(
                        relaunched,
                        timeout=args.timeout,
                    )
                )
                relaunched_chat_id = _assert_persisted_success_events(
                    relaunched,
                    session_id=session_id,
                    timeout=args.timeout,
                )
                if relaunched_chat_id != chat_id:
                    raise AssertionError(
                        "Relaunched packaged app backend returned a different chat id "
                        f"for session {session_id!r}: {relaunched_chat_id!r} != {chat_id!r}"
                    )
                relaunch_controls = _assert_relaunched_chat_control_plane(
                    relaunched,
                    chat_id=relaunched_chat_id,
                    session_id=session_id,
                    timeout=args.timeout,
                )
                if args.restart_cycle:
                    _assert_canonical_runtime_controls(
                        relaunched,
                        chat_id=relaunched_chat_id,
                        session_id=session_id,
                        timeout=args.timeout,
                        destructive_cleanup=True,
                        expected_statuses={"completed", "succeeded"},
                    )
                    relaunch_controls["archive_delete_cleanup"] = True
                relaunch_basic_ui_send = _seed_chat_basic_ui_send(
                    relaunched,
                    workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                    timeout=args.timeout,
                )
                relaunch_provider_stream_ui = _seed_chat_provider_stream_ui(
                    relaunched,
                    workspace_dir=user_data_dir / "working" / "workspaces" / "default",
                    timeout=args.timeout,
                )
                relaunch_renderer_smoke = _run_chat_runtime_stream_renderer_smoke(
                    relaunched,
                    chat_id=chat_id,
                    timeout=args.timeout,
                    basic_ui_send=relaunch_basic_ui_send,
                    provider_stream_ui=relaunch_provider_stream_ui,
                    expected_terminal_type="turn.completed",
                    expected_terminal_statuses={"completed", "finished"},
                )
                app_relaunch_summary = {
                    "backend_pid": relaunched.get("backend_pid"),
                    "chat_id": relaunched_chat_id,
                    "customization": relaunched_customization_summary,
                    "chat_renderer_basic_ui_send": _chat_basic_ui_send_evidence(
                        relaunch_renderer_smoke
                    ),
                    "chat_renderer_provider_stream_ui": _chat_provider_stream_ui_evidence(
                        relaunch_renderer_smoke
                    ),
                    "runtime_controls": relaunch_controls,
                }

            print(
                "Packaged app lifecycle smoke passed: "
                f"distribution={args.distribution} chat_id={chat_id}"
            )
            phase = "evidence writing"
            legacy_writer_summary = _legacy_writer_observation_summary(
                legacy_writer_observation_file,
                release_edition=args.distribution,
            )
            legacy_boundary_summary = _legacy_boundary_observation_summary(
                legacy_execution_observation_file,
                legacy_writer_observation_file,
                release_edition=args.distribution,
            )
            evidence = {
                "schema": SMOKE_EVIDENCE_SCHEMA,
                "passed": True,
                "distribution": args.distribution,
                "restart_cycle": bool(args.restart_cycle),
                "app_relaunch_cycle": bool(args.app_relaunch_cycle),
                "started_at": started_at,
                "completed_at": _utc_now_iso(),
                "app": _app_metadata_for_evidence(app_path),
                "smoke": {
                    "chat_id": chat_id,
                    "session_id": session_id,
                    "model_success_path": {
                        "provider_id": SMOKE_MODEL_PROVIDER_ID,
                        "model": SMOKE_MODEL_ID,
                        "configured": smoke_model_config is not None,
                        "request_count": (
                            smoke_model.request_count
                            if smoke_model is not None
                            else None
                        ),
                    },
                    "initial_backend_pid": state.get("backend_pid"),
                    "restarted_backend_pid": restarted_backend_pid,
                    "customization": customization_summary,
                    "post_restart_customization": restarted_customization_summary,
                    "restart_decision_run_ids": restart_decision_run_ids,
                    "operator_effect_recovery": operator_effect_recovery,
                    "loop_trial_run_id": loop_trial_run_id,
                    "browser_content_run_id": browser_content_run_id,
                    "browser_runtime_run_id": browser_run_id,
                    "basic_ui_send_chat_id": basic_ui_send["chat_id"],
                    "basic_ui_send_run_id": basic_ui_send["run_id"],
                    "chat_renderer_basic_ui_send": (
                        _chat_basic_ui_send_evidence(chat_renderer_stream_smoke)
                    ),
                    "provider_stream_ui_chat_id": provider_stream_ui["chat_id"],
                    "provider_stream_ui_run_id": provider_stream_ui["run_id"],
                    "chat_renderer_provider_stream_ui": (
                        _chat_provider_stream_ui_evidence(
                            chat_renderer_stream_smoke
                        )
                    ),
                    "post_restart_basic_ui_send_chat_id": (
                        post_restart_basic_ui_send["chat_id"]
                        if args.restart_cycle
                        else None
                    ),
                    "post_restart_basic_ui_send_run_id": (
                        post_restart_basic_ui_send["run_id"]
                        if args.restart_cycle
                        else None
                    ),
                    "post_restart_chat_renderer_basic_ui_send": (
                        _chat_basic_ui_send_evidence(
                            post_restart_chat_renderer_stream_smoke
                        )
                        if args.restart_cycle
                        else None
                    ),
                    "post_restart_provider_stream_ui_chat_id": (
                        post_restart_provider_stream_ui["chat_id"]
                        if args.restart_cycle
                        else None
                    ),
                    "post_restart_provider_stream_ui_run_id": (
                        post_restart_provider_stream_ui["run_id"]
                        if args.restart_cycle
                        else None
                    ),
                    "post_restart_chat_renderer_provider_stream_ui": (
                        _chat_provider_stream_ui_evidence(
                            post_restart_chat_renderer_stream_smoke
                        )
                        if args.restart_cycle
                        else None
                    ),
                    "reminder_delivery": reminder_delivery_summary,
                    "reminder_delivery_restart": (
                        reminder_delivery_restart_summary
                        if args.restart_cycle
                        else None
                    ),
                    "app_relaunch": app_relaunch_summary,
                    "legacy_writer_observation": legacy_writer_summary,
                    "legacy_boundary_observation": legacy_boundary_summary,
                    "smoke_root": str(smoke_root) if args.keep_workdir else None,
                },
                "checks": [
                    "desktop_readiness",
                    "distribution_tools",
                    "browser_capabilities",
                    "packaged_customization_integrity",
                    "reminder_delivery_worker",
                    "legacy_writer_observation_summary",
                    "legacy_boundary_observation_summary",
                    "runtime_restart_decision_projection",
                    "operator_effect_resolution_repair",
                    "loop_trial_recovery_export",
                    "browser_content_trial_handoff",
                    "browser_content_renderer_export",
                    "browser_preflight_failure_runtime_settlement",
                    "browser_pause_resume",
                    "task_center_multi_window_stale_control",
                    "console_model_success_path",
                    "runtime_event_sse_reconnect",
                    "chat_renderer_stream_reconnect",
                    "chat_renderer_basic_ui_send",
                    "chat_renderer_provider_stream_ui",
                    "chat_renderer_rapid_queue_ui",
                    "chat_renderer_stop_resend_ui",
                    "chat_renderer_stream_route_switch_ui",
                    "chat_renderer_backend_model_roundtrip",
                    "chat_renderer_basic_experience_matrix",
                    "terminal_interrupt_noop",
                    "canonical_runtime_controls",
                    *(
                        [
                            "backend_sigkill_restart",
                            "post_restart_customization_integrity",
                            "post_restart_reminder_delivery_dedupe",
                            "post_restart_runtime_projection",
                            "post_restart_operator_effect_no_replay",
                            "post_restart_loop_export",
                            "post_restart_browser_content_renderer_export",
                            "post_restart_chat_stream_reconnect",
                            "post_restart_chat_basic_ui_send",
                            "post_restart_chat_provider_stream_ui",
                            "post_restart_chat_rapid_queue_ui",
                            "post_restart_chat_stop_resend_ui",
                            "post_restart_chat_stream_route_switch_ui",
                            "post_restart_chat_backend_model_roundtrip",
                            "post_restart_chat_basic_experience_matrix",
                            "post_restart_canonical_runtime_controls",
                        ]
                        if args.restart_cycle
                        else []
                    ),
                    *(
                        [
                            "electron_app_relaunch",
                            "post_app_relaunch_customization_integrity",
                            "post_app_relaunch_chat_history",
                            "post_app_relaunch_terminal_interrupt_noop",
                            "post_app_relaunch_canonical_runtime_controls",
                            "post_app_relaunch_chat_basic_ui_send",
                            "post_app_relaunch_chat_provider_stream_ui",
                        ]
                        if args.app_relaunch_cycle
                        else []
                    ),
                ],
            }
            if args.evidence_output:
                _write_json_file(Path(args.evidence_output), evidence)
    except Exception as exc:
        raise RuntimeError(
            f"Packaged lifecycle smoke failed during {phase}: {exc}\n"
            f"App log tail:\n{_tail_text(app_log_path)}\n"
            f"Backend log tail:\n{_tail_text(user_data_dir / 'backend.log')}"
        ) from exc
    finally:
        _stop_app(proc)
        if smoke_model_context is not None:
            with contextlib.suppress(Exception):
                smoke_model_context.__exit__(None, None, None)
        if not args.keep_workdir:
            shutil.rmtree(smoke_root, ignore_errors=True)
        restore_termination_signal_handlers()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test a packaged macOS .app Electron backend lifecycle.",
    )
    parser.add_argument("--app", required=True, help="Path to the packaged .app bundle.")
    parser.add_argument(
        "--distribution",
        choices=("official", "mas"),
        default="official",
        help="Expected desktop distribution channel.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Seconds to wait for app readiness and HTTP operations.",
    )
    parser.add_argument(
        "--restart-cycle",
        action="store_true",
        help="Kill the managed backend once and verify Electron restarts it.",
    )
    parser.add_argument(
        "--app-relaunch-cycle",
        action="store_true",
        help="Quit and relaunch the packaged Electron app, then re-run chat UI checks.",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Keep temporary app smoke directories for inspection.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Keep the lifecycle smoke renderer hidden while preserving the full Electron path.",
    )
    parser.add_argument(
        "--evidence-output",
        type=Path,
        help="Write non-secret JSON evidence after a successful full smoke.",
    )
    args = parser.parse_args(argv)
    try:
        _run_smoke(args)
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(f"Packaged app lifecycle smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
