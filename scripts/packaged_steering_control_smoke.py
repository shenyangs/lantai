#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one bounded real-provider steering smoke against an exact app candidate.

The driver launches only the supplied app bundle with an isolated user-data
directory.  It copies the selected encrypted provider profile into that
temporary directory, exercises one Native Runtime run, and emits a redacted
evidence document.  Prompts, model output, credentials, local paths, and raw
run/chat/command identifiers are never written to the evidence file.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_HELPERS_PATH = PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
STAGING_HELPERS_PATH = (
    PROJECT_ROOT / "scripts" / "source_configured_provider_stream_smoke.py"
)

EXPECTED_PROVIDER_ID = "minimax-cn"
EXPECTED_MODEL_ID = "MiniMax-M3"
MAX_PROVIDER_REQUESTS = 8
EVIDENCE_SCHEMA = "com.lantai.packaged-steering-control-smoke.v1"
FINAL_VISUAL_SENTINEL = "STEER_VISUAL_OK_CEDAR_427_VIOLET_913"
DOCUMENT_SCOPE_SENTINEL = "DOCUMENT_SCOPE_OK_AMBER_642"
TERMINAL_EXECUTION_STATUSES = {
    "applied",
    "superseded",
    "cancelled",
    "not_applied",
}


def _split_connect_target(target: str) -> tuple[str, int]:
    if target.startswith("["):
        closing = target.find("]")
        if closing <= 1 or target[closing + 1 : closing + 2] != ":":
            raise ValueError("invalid bracketed CONNECT target")
        host = target[1:closing]
        port_text = target[closing + 2 :]
    else:
        host, separator, port_text = target.rpartition(":")
        if not separator:
            raise ValueError("CONNECT target omitted its port")
    port = int(port_text)
    if not host or not 1 <= port <= 65535:
        raise ValueError("invalid CONNECT target")
    return host, port


class _ConnectProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ConnectProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        controller: _ControlledConnectProxy = self.server.controller  # type: ignore[attr-defined]
        upstream: socket.socket | None = None
        registered = False
        try:
            reader = self.request.makefile("rb")
            request_line = reader.readline(4097)
            if len(request_line) > 4096:
                raise ValueError("CONNECT request line is too large")
            parts = request_line.decode("ascii").strip().split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            header_bytes = len(request_line)
            while True:
                line = reader.readline(4097)
                header_bytes += len(line)
                if len(line) > 4096 or header_bytes > 65536:
                    raise ValueError("CONNECT headers are too large")
                if line in {b"\r\n", b"\n", b""}:
                    break
            host, port = _split_connect_target(parts[1])
            upstream = socket.create_connection((host, port), timeout=15)
            upstream.settimeout(None)
            controller._register(self.request, upstream)
            registered = True
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            sockets = (self.request, upstream)
            while True:
                readable, _writable, _exceptional = select.select(sockets, [], sockets, 1)
                if not readable:
                    continue
                for source in readable:
                    chunk = source.recv(65536)
                    if not chunk:
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(chunk)
        except (OSError, ValueError, UnicodeError):
            return
        finally:
            if registered and upstream is not None:
                controller._unregister(self.request, upstream)
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class _ControlledConnectProxy:
    """Candidate-scoped HTTPS CONNECT proxy with one explicit drop control."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active: set[tuple[socket.socket, socket.socket]] = set()
        self._tunnel_count = 0
        self._drop_count = 0
        self._server: _ConnectProxyServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("CONNECT proxy has not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def tunnel_count(self) -> int:
        with self._condition:
            return self._tunnel_count

    @property
    def drop_count(self) -> int:
        with self._condition:
            return self._drop_count

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("CONNECT proxy is already running")
        server = _ConnectProxyServer(("127.0.0.1", 0), _ConnectProxyHandler)
        server.controller = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="packaged-steering-connect-proxy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self.drop_active()
        server.shutdown()
        server.server_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._server = None
        self._thread = None

    def wait_for_active(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("candidate provider CONNECT tunnel was not active")
                self._condition.wait(min(remaining, 0.25))

    def drop_active(self) -> int:
        with self._condition:
            pairs = list(self._active)
            self._drop_count += len(pairs)
        for pair in pairs:
            for connection in pair:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    connection.close()
                except OSError:
                    pass
        return len(pairs)

    def _register(self, client: socket.socket, upstream: socket.socket) -> None:
        with self._condition:
            self._active.add((client, upstream))
            self._tunnel_count += 1
            self._condition.notify_all()

    def _unregister(self, client: socket.socket, upstream: socket.socket) -> None:
        with self._condition:
            self._active.discard((client, upstream))
            self._condition.notify_all()

_INITIAL_PROMPT = (
    "Do not use external tools. Work only from this conversation. Begin a careful, "
    "visible analysis of a hypothetical migration with at least twenty independent "
    "checks covering ordering, rollback, data integrity, and user-visible state. "
    "Do not rush to a final answer. If a newer in-run instruction arrives, follow "
    "that instruction while preserving this no-external-tools restriction."
)
_TEXT_SUPPLEMENT = (
    "Keep the no-external-tools restriction. Preserve the first visual input in "
    "context, and now perform another careful multi-step consistency pass before "
    "you conclude. A newer direction may arrive while you are reasoning."
)
_DOCUMENT_SUPPLEMENT = (
    "Keep the no-external-tools restriction. Read only the attached document "
    "whose ASSET_VERSION is steering-document-v1. Treat section SCOPE-B as the "
    "selected range and SCOPE-A as explicitly out of scope. Begin your visible "
    "answer with exactly the SELECTED_SCOPE_TOKEN value from SCOPE-B, then "
    "continue a careful multi-step consistency check so a newer direction can "
    "still arrive while you are responding."
)
_MIXED_REDIRECT_TEXT = (
    "Immediately leave the previous analysis at the next safe model boundary. Use "
    "both visual inputs already supplied. Reply with exactly one sentinel in this "
    "format: STEER_VISUAL_OK_<A_WORD>_<A_NUMBER>_<B_WORD>_<B_NUMBER>, taking the "
    "uppercase word and three digits from Visual Input A and Visual Input B. Do not "
    "use external tools and do not add any other text."
)


def _load_module(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("smoke helper module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_helpers() -> tuple[Any, Any]:
    return (
        _load_module(PACKAGED_HELPERS_PATH, "packaged_steering_helpers"),
        _load_module(STAGING_HELPERS_PATH, "packaged_steering_staging"),
    )


def _assert_authorized_model(
    configured_model: dict[str, Any],
    *,
    expected_provider_id: str = EXPECTED_PROVIDER_ID,
    expected_model_id: str = EXPECTED_MODEL_ID,
) -> None:
    observed = (
        str(configured_model.get("provider_id") or ""),
        str(configured_model.get("model") or ""),
    )
    if observed != (expected_provider_id, expected_model_id):
        raise RuntimeError("configured model does not match the authorized configured model")


def _image_block(upload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "image",
        "image_url": str(upload["url"]),
        "filename": str(upload["file_name"]),
        "media_type": "image/png",
        "file_size": int(upload["size"]),
    }


def _file_block(
    upload: dict[str, Any],
    *,
    media_type: str,
) -> dict[str, Any]:
    return {
        "type": "file",
        "file_url": str(upload["url"]),
        "filename": str(upload["file_name"]),
        "media_type": media_type,
        "file_size": int(upload["size"]),
    }


def _control_payload(
    *,
    chat_id: str,
    session_id: str,
    command_id: str,
    intent: str,
    content: list[dict[str, Any]],
    expected_run_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "client_turn_id": command_id,
        "control_intent": intent,
    }
    if expected_run_id:
        meta["expected_run_id"] = expected_run_id
    if expected_attempt_id:
        meta["expected_attempt_id"] = expected_attempt_id
    return {
        "input": [{"role": "user", "content": content}],
        "chat_id": chat_id,
        "session_id": session_id,
        "user_id": "default",
        "channel": "console",
        "stream": True,
        "operation": "steer",
        "meta": meta,
    }


def _provider_request_count(receipts: list[dict[str, Any]]) -> int:
    highest = 0
    for receipt in receipts:
        match = re.search(r":model:(\d+)$", str(receipt.get("model_request_id") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def _assert_provider_request_budget(receipts: list[dict[str, Any]]) -> None:
    count = _provider_request_count(receipts)
    if count > MAX_PROVIDER_REQUESTS:
        raise AssertionError(
            f"provider request cap exceeded: {count} > {MAX_PROVIDER_REQUESTS}"
        )


def _model_request_slot(value: Any) -> int | None:
    match = re.search(r":model:(\d+)$", str(value or ""))
    return int(match.group(1)) if match else None


def _safe_app_metadata(app_metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: app_metadata.get(key)
        for key in (
            "bundle_identifier",
            "short_version",
            "bundle_version",
            "executable",
            "artifact_sha256",
        )
        if key in app_metadata
    }


def _build_safe_evidence(
    *,
    configured_model: dict[str, Any],
    expected_provider_id: str = EXPECTED_PROVIDER_ID,
    expected_model_id: str = EXPECTED_MODEL_ID,
    app_metadata: dict[str, Any],
    run_identity: dict[str, Any],
    admissions: list[dict[str, Any]],
    final_receipts: list[dict[str, Any]],
    observation: dict[str, Any],
) -> dict[str, Any]:
    del run_identity  # Raw identities are deliberately represented only by slots.
    epochs = sorted(
        {
            int(receipt["model_epoch"])
            for receipt in final_receipts
            if isinstance(receipt.get("model_epoch"), int)
        }
    )
    revisions = sorted(
        {
            int(receipt["intent_revision"])
            for receipt in final_receipts
            if isinstance(receipt.get("intent_revision"), int)
        }
    )
    safe_admissions = []
    for index, admission in enumerate(admissions, start=1):
        safe_admissions.append(
            {
                "command_slot": f"command{index}",
                "intent": admission.get("intent"),
                "http_status": admission.get("http_status"),
                "execution_status": admission.get("execution_status"),
                "next_waiting_reason": admission.get("next_waiting_reason"),
                "admission_ms": admission.get("admission_ms"),
            }
        )
    safe_receipts = []
    for index, receipt in enumerate(final_receipts, start=1):
        safe_receipts.append(
            {
                "command_slot": f"command{index}",
                "execution_status": receipt.get("execution_status"),
                "disposition_status": receipt.get("disposition_status"),
                "next_waiting_reason": receipt.get("next_waiting_reason"),
                "model_request_slot": _model_request_slot(
                    receipt.get("model_request_id")
                ),
                "model_epoch": receipt.get("model_epoch"),
                "intent_revision": receipt.get("intent_revision"),
            }
        )
    provider_requests = _provider_request_count(final_receipts)
    run_count = int(observation.get("runtime_run_count") or 0)
    attempt_count = int(observation.get("runtime_attempt_count") or 0)
    tool_effect_count = int(observation.get("tool_effect_count") or 0)
    physical_reconnect_required = observation.get("physical_reconnect_required") is True
    acceptance = {
        "authorized_provider_model": (
            configured_model.get("provider_id") == expected_provider_id
            and configured_model.get("model") == expected_model_id
        ),
        "single_run_attempt": run_count == 1 and attempt_count == 1,
        "pure_image_no_synthetic_text": observation.get(
            "pure_image_no_synthetic_text", True
        )
        is True,
        "attachments_digest_version_grant_bound": observation.get(
            "attachments_digest_version_grant_bound", False
        )
        is True,
        "thinking_before_redirect": observation.get("thinking_before_redirect")
        is True,
        "answer_after_redirect": observation.get("answer_after_redirect") is True,
        "visual_sentinel_matched": observation.get("visual_sentinel_matched")
        is True,
        "document_scope_sentinel_matched": observation.get(
            "document_scope_sentinel_matched"
        )
        is True,
        "document_attachment_bound": int(
            observation.get("document_attachment_count") or 0
        )
        >= 1,
        "old_stream_yielded": observation.get("old_stream_yielded") is True,
        "retry_same_receipt": observation.get("retry_same_receipt") is True,
        "stream_completed": observation.get("stream_completed") is True,
        "physical_disconnect_recovered": (
            not physical_reconnect_required
            or observation.get("physical_disconnect_recovered") is True
        ),
        "candidate_scoped_proxy": (
            not physical_reconnect_required
            or observation.get("candidate_scoped_proxy") is True
        ),
        "no_tool_effects": tool_effect_count == 0,
        "within_provider_request_cap": provider_requests <= MAX_PROVIDER_REQUESTS,
    }
    return {
        "schema": EVIDENCE_SCHEMA,
        "ok": all(acceptance.values()),
        "distribution": "official-artifact-only-candidate",
        "app": _safe_app_metadata(app_metadata),
        "configured_model": {
            "provider_id": configured_model.get("provider_id"),
            "model": configured_model.get("model"),
        },
        "identity_slots": {
            "chat": "chat1",
            "run": "run1",
            "attempt": "attempt1",
            "commands": [f"command{index}" for index in range(1, len(admissions) + 1)],
        },
        "counts": {
            "provider_requests": provider_requests,
            "provider_request_cap": MAX_PROVIDER_REQUESTS,
            "runtime_runs": run_count,
            "runtime_attempts": attempt_count,
            "attachments": int(observation.get("attachment_count") or 0),
            "documents": int(observation.get("document_attachment_count") or 0),
            "tool_effects": tool_effect_count,
            "model_request_manifests": int(
                observation.get("model_request_manifest_count") or provider_requests
            ),
        },
        "lineage": {
            "model_epochs": epochs,
            "intent_revisions": revisions,
        },
        "uploads": list(observation.get("safe_uploads") or []),
        "admissions": safe_admissions,
        "final_receipts": safe_receipts,
        "timing_ms": dict(observation.get("timing_ms") or {}),
        "network_fault": {
            "required": physical_reconnect_required,
            "tunnels": int(observation.get("proxy_tunnel_count") or 0),
            "dropped_tunnels": int(observation.get("proxy_drop_count") or 0),
        },
        "migration": dict(observation.get("migration") or {}),
        "acceptance": acceptance,
    }


class _StreamObservation:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.initial_receipt: dict[str, Any] | None = None
        self.first_reasoning_at: float | None = None
        self.first_answer_at: float | None = None
        self.surface_at: dict[str, float] = {}
        self.reasoning_at: dict[str, float] = {}
        self.message_client_ids: dict[str, str] = {}
        self.message_types: dict[str, str] = {}
        self.answer_chunks: list[str] = []
        self.completed = False
        self.error: BaseException | None = None

    def feed(self, frame: dict[str, Any]) -> None:
        now = time.monotonic()
        with self.condition:
            obj = str(frame.get("object") or "")
            frame_type = str(frame.get("type") or "")
            metadata = frame.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            if obj == "turn_receipt" and isinstance(frame.get("receipt"), dict):
                self.initial_receipt = dict(frame["receipt"])
            nested_receipt = metadata.get("turn_receipt")
            if self.initial_receipt is None and isinstance(nested_receipt, dict):
                self.initial_receipt = dict(nested_receipt)
            client_id = str(metadata.get("client_turn_id") or "")
            if obj == "message" and frame_type in {"reasoning", "message"}:
                message_id = str(frame.get("id") or "")
                if message_id:
                    self.message_types[message_id] = frame_type
                    if client_id:
                        self.message_client_ids[message_id] = client_id
                if frame_type == "reasoning":
                    self.first_reasoning_at = self.first_reasoning_at or now
                    if client_id:
                        self.reasoning_at.setdefault(client_id, now)
                else:
                    self.first_answer_at = self.first_answer_at or now
                if client_id:
                    self.surface_at.setdefault(client_id, now)
                text = _content_text(frame.get("content"))
                if frame_type == "message" and text:
                    self.answer_chunks.append(text)
            elif obj == "content" and frame.get("delta") is True:
                message_id = str(frame.get("msg_id") or "")
                message_type = self.message_types.get(message_id, "")
                mapped_client_id = self.message_client_ids.get(message_id, "")
                if mapped_client_id:
                    self.surface_at.setdefault(mapped_client_id, now)
                    if message_type == "reasoning":
                        self.reasoning_at.setdefault(mapped_client_id, now)
                text = str(frame.get("text") or "")
                if message_type == "message" and text:
                    self.answer_chunks.append(text)
            if obj == "response" and str(frame.get("status") or "") == "completed":
                final_text = _final_response_text(frame)
                if final_text:
                    self.answer_chunks.append(final_text)
                self.completed = True
            self.condition.notify_all()

    def fail(self, exc: BaseException) -> None:
        with self.condition:
            self.error = exc
            self.condition.notify_all()

    def wait_for(self, predicate: Any, *, timeout: float, label: str) -> None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while not predicate():
                if self.error is not None:
                    raise RuntimeError(f"initial stream failed before {label}") from self.error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {label}")
                self.condition.wait(min(remaining, 0.5))

    def joined_answer(self) -> str:
        with self.condition:
            return "".join(self.answer_chunks)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(item.get("text") or "") for item in content if isinstance(item, dict)
    )


def _final_response_text(frame: dict[str, Any]) -> str:
    output = frame.get("output")
    if not isinstance(output, list):
        return ""
    return "".join(
        _content_text(item.get("content"))
        for item in output
        if isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role", "assistant") == "assistant"
    )


def _iter_sse_frames(response: Any) -> Any:
    data_lines: list[str] = []
    while True:
        raw = response.readline()
        if not raw:
            if data_lines:
                yield _parse_sse_frame(data_lines)
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield _parse_sse_frame(data_lines)
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


def _parse_sse_frame(data_lines: list[str]) -> dict[str, Any]:
    payload = json.loads("\n".join(data_lines))
    if not isinstance(payload, dict):
        raise AssertionError("SSE frame was not an object")
    return payload


def _read_initial_stream(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    observation: _StreamObservation,
) -> None:
    request_headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    request_headers.update(headers)
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError("initial stream returned a non-success status")
            for frame in _iter_sse_frames(response):
                observation.feed(frame)
    except BaseException as exc:  # noqa: BLE001 - forwarded to controller thread
        observation.fail(exc)


def _post_control(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, Any], int]:
    request_headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    request_headers.update(headers)
    started = time.monotonic()
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            frames = list(_iter_sse_frames(response))
            status = response.status
    except error.HTTPError as exc:
        exc.read()
        raise RuntimeError(f"control admission returned HTTP {exc.code}") from exc
    receipt = next(
        (
            frame["receipt"]
            for frame in frames
            if frame.get("object") == "turn_receipt"
            and isinstance(frame.get("receipt"), dict)
        ),
        None,
    )
    if status != 200 or receipt is None:
        raise AssertionError("control admission omitted its durable receipt")
    return status, dict(receipt), round((time.monotonic() - started) * 1000)


def _query_control(
    helpers: Any,
    state: dict[str, Any],
    *,
    session_id: str,
    chat_id: str,
    command_id: str,
    timeout: float,
) -> dict[str, Any]:
    query = parse.urlencode(
        {"session_id": session_id, "chat_id": chat_id, "command_id": command_id}
    )
    status, _body, payload = helpers._json_request(
        "GET",
        f"{helpers._state_base_url(state)}/api/console/chat/control-input?{query}",
        headers=helpers._state_headers(state),
        timeout=timeout,
    )
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError("control receipt query failed")
    return payload


def _wait_for_terminal_receipts(
    helpers: Any,
    state: dict[str, Any],
    *,
    session_id: str,
    chat_id: str,
    command_ids: list[str],
    timeout: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    latest: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        latest = [
            _query_control(
                helpers,
                state,
                session_id=session_id,
                chat_id=chat_id,
                command_id=command_id,
                timeout=min(timeout, 15),
            )
            for command_id in command_ids
        ]
        _assert_provider_request_budget(latest)
        if all(
            receipt.get("execution_status") in TERMINAL_EXECUTION_STATUSES
            for receipt in latest
        ):
            return latest
        time.sleep(0.25)
    raise TimeoutError("timed out waiting for terminal control receipts")


def _upload_file(
    *,
    url: str,
    headers: dict[str, str],
    path: Path,
    media_type: str = "image/png",
    timeout: float,
) -> tuple[dict[str, Any], int]:
    boundary = f"----lantai-steering-{uuid.uuid4().hex}"
    raw = path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n"
    ).encode("utf-8") + raw + f"\r\n--{boundary}--\r\n".encode("ascii")
    request_headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    request_headers.update(headers)
    started = time.monotonic()
    req = request.Request(url, data=body, headers=request_headers, method="POST")
    with request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError("upload did not return a durable descriptor")
    for key in ("url", "file_name", "size", "digest", "version"):
        if payload.get(key) in {None, ""}:
            raise AssertionError("upload descriptor is incomplete")
    return payload, round((time.monotonic() - started) * 1000)


def _create_visual_fixture(path: Path, *, label: str, code: str) -> None:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]

    image = Image.new("RGB", (1200, 700), "#f7f2e8")
    draw = ImageDraw.Draw(image)
    font_candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    )
    font_path = next((candidate for candidate in font_candidates if Path(candidate).is_file()), None)
    title_font = ImageFont.truetype(font_path, 72) if font_path else ImageFont.load_default()
    code_font = ImageFont.truetype(font_path, 112) if font_path else ImageFont.load_default()
    body_font = ImageFont.truetype(font_path, 42) if font_path else ImageFont.load_default()
    draw.rounded_rectangle((45, 45, 1155, 655), radius=36, fill="#ffffff", outline="#1d2733", width=8)
    draw.text((100, 105), label, fill="#1d2733", font=title_font)
    draw.text((100, 235), code, fill="#713f98", font=code_font)
    draw.multiline_text(
        (100, 430),
        "Do not use external tools.\nThink carefully before concluding.",
        fill="#1d2733",
        font=body_font,
        spacing=16,
    )
    image.save(path, format="PNG")


def _create_document_fixture(path: Path) -> None:
    path.write_text(
        """# Steering document acceptance fixture

ASSET_VERSION: steering-document-v1

## SCOPE-A (out of scope)

OUT_OF_SCOPE_TOKEN: DOCUMENT_SCOPE_WRONG_MAPLE_111

## SCOPE-B (selected range)

SELECTED_SCOPE_TOKEN: DOCUMENT_SCOPE_OK_AMBER_642
""",
        encoding="utf-8",
    )


def _create_chat(
    helpers: Any,
    state: dict[str, Any],
    *,
    agent_workspace_dir: Path,
    timeout: float,
) -> tuple[str, str, Path]:
    suffix = uuid.uuid4().hex[:12]
    session_id = f"console:steering-acceptance-{suffix}"
    topic_workspace = agent_workspace_dir / "topic-workspaces" / f"steering-{suffix}"
    status, _body, payload = helpers._json_request(
        "POST",
        f"{helpers._state_base_url(state)}/api/chats",
        payload={
            "name": f"steering acceptance {suffix}",
            "session_id": session_id,
            "user_id": "default",
            "channel": "console",
            "meta": {"topic_workspace_dir": str(topic_workspace)},
        },
        headers=helpers._state_headers(state),
        timeout=timeout,
    )
    if status != 200 or not isinstance(payload, dict) or not payload.get("id"):
        raise AssertionError("packaged smoke could not create its isolated chat")
    return str(payload["id"]), session_id, topic_workspace


def _initial_payload(*, chat_id: str, session_id: str, client_turn_id: str) -> dict[str, Any]:
    return {
        "input": [
            {"role": "user", "content": [{"type": "text", "text": _INITIAL_PROMPT}]}
        ],
        "chat_id": chat_id,
        "session_id": session_id,
        "user_id": "default",
        "channel": "console",
        "stream": True,
        "client_turn_id": client_turn_id,
        "deep_thinking": {"enabled": True, "effort": "high"},
        "meta": {"client_turn_id": client_turn_id},
    }


def _immutable_receipt_identity(receipt: dict[str, Any]) -> tuple[Any, ...]:
    return (
        receipt.get("server_seq"),
        receipt.get("payload_digest"),
        receipt.get("accepted_at"),
        receipt.get("run_id"),
        receipt.get("attempt_id"),
    )


def _runtime_events(
    helpers: Any,
    state: dict[str, Any],
    *,
    chat_id: str,
    timeout: float,
) -> list[dict[str, Any]]:
    encoded = parse.quote(chat_id, safe="")
    status, _body, payload = helpers._json_request(
        "GET",
        f"{helpers._state_base_url(state)}/api/chats/{encoded}/events?after_seq=0",
        headers=helpers._state_headers(state),
        timeout=timeout,
    )
    events = payload.get("events") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(events, list):
        raise AssertionError("runtime event ledger could not be read")
    return [event for event in events if isinstance(event, dict)]


def _provider_disconnect_recoveries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("type") == "runtime.warning"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("kind") == "model_output_recovery"
        and isinstance(event["payload"].get("details"), dict)
        and event["payload"]["details"].get("reason")
        == "provider_connection_interrupted"
    ]


def _wait_for_provider_disconnect_recovery(
    helpers: Any,
    state: dict[str, Any],
    *,
    chat_id: str,
    timeout: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = _runtime_events(
            helpers,
            state,
            chat_id=chat_id,
            timeout=min(timeout, 15),
        )
        if _provider_disconnect_recoveries(events):
            return events
        time.sleep(0.1)
    raise TimeoutError("candidate did not record provider disconnect recovery")


def _inspect_persistence(
    *,
    topic_workspace: Path,
    chat_id: str,
    command_ids: list[str],
    run_id: str,
    attempt_id: str,
    configured_model: dict[str, Any],
) -> dict[str, Any]:
    source_root = PROJECT_ROOT / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from lantai.runtime.coherence.surface import ModelRequestManifestStore
    from lantai.runtime.effects import RuntimeEffectJournal
    from lantai.runtime.store import RuntimeRunStore
    from lantai.runtime.turn_control import RuntimeTurnControlStore

    contracts = RuntimeRunStore(topic_workspace).list_contracts()
    attempt_ids = {contract.attempt_id for contract in contracts}
    if len(contracts) != 1 or {contract.run_id for contract in contracts} != {run_id}:
        raise AssertionError("steering smoke created more than one Runtime run")
    if attempt_ids != {attempt_id}:
        raise AssertionError("steering smoke changed Runtime attempt identity")

    control_store = RuntimeTurnControlStore(topic_workspace)
    items = [
        control_store.get_input_item(chat_id=chat_id, command_id=command_id)
        for command_id in command_ids
    ]
    if any(item is None for item in items):
        raise AssertionError("durable Run Inbox omitted a control input")
    dumped_messages = [item.message.model_dump(mode="json") for item in items if item]
    blocks_by_command = [message.get("content") or [] for message in dumped_messages]
    pure_blocks = blocks_by_command[0]
    pure_image_no_text = (
        len(pure_blocks) == 1 and pure_blocks[0].get("type") == "image"
    )
    attachment_blocks = [
        block
        for blocks in blocks_by_command
        for block in blocks
        if isinstance(block, dict) and block.get("type") in {"image", "file"}
    ]
    document_blocks = [
        block
        for blocks in blocks_by_command
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "file"
    ]
    binding_ok = bool(attachment_blocks) and all(
        isinstance(block.get("data"), dict)
        and str(block["data"].get("digest") or "").startswith("sha256:")
        and block["data"].get("version") == 1
        and bool(block["data"].get("grant_ref"))
        for block in attachment_blocks
    )
    manifests = ModelRequestManifestStore(topic_workspace).list_for_attempt(
        run_id=run_id,
        attempt_id=attempt_id,
    )
    if not manifests:
        raise AssertionError("model request manifests were not persisted")
    if any(
        manifest.provider_id != configured_model["provider_id"]
        or manifest.model_id != configured_model["model"]
        for manifest in manifests
    ):
        raise AssertionError("model request manifest used an unauthorized route")
    admitted = {
        admitted_id for manifest in manifests for admitted_id in manifest.admitted_input_ids
    }
    if not set(command_ids).issubset(admitted):
        raise AssertionError("a control input was not bound into an actual model request")
    effects = RuntimeEffectJournal(topic_workspace).list_effects(run_id, attempt_id)
    return {
        "runtime_run_count": len(contracts),
        "runtime_attempt_count": len(attempt_ids),
        "attachment_count": len(attachment_blocks),
        "document_attachment_count": len(document_blocks),
        "pure_image_no_synthetic_text": pure_image_no_text,
        "attachments_digest_version_grant_bound": binding_ok,
        "model_request_manifest_count": len(manifests),
        "tool_effect_count": len(effects),
    }


def _clean_app_env(
    packaged: Any,
    *,
    user_data_dir: Path,
    state_path: Path,
) -> dict[str, str]:
    env = packaged._clean_app_env(user_data_dir=user_data_dir, state_path=state_path)
    env["LANTAI_AUTH_ENABLED"] = "0"
    env["LANTAI_DISABLE_GITHUB_UPDATER"] = "1"
    return env


def _route_candidate_through_proxy(
    env: dict[str, str],
    *,
    proxy_url: str,
) -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[key] = proxy_url
    no_proxy = "127.0.0.1,localhost,::1"
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy


def run(
    *,
    app: Path,
    distribution: str,
    source_working_dir: Path,
    source_secret_dir: Path,
    agent_id: str,
    timeout: float,
    expected_provider_id: str = EXPECTED_PROVIDER_ID,
    expected_model_id: str = EXPECTED_MODEL_ID,
    exercise_physical_reconnect: bool = False,
) -> dict[str, Any]:
    if distribution != "official":
        raise ValueError("only the official artifact-only distribution is accepted")
    helpers, staging = _load_helpers()
    packaged = _load_module(
        PROJECT_ROOT / "scripts" / "packaged_configured_provider_stream_smoke.py",
        "packaged_steering_configured_helpers",
    )
    app_path = app.expanduser().resolve(strict=True)
    executable = helpers._find_app_executable(app_path)
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-packaged-steering-"))
    user_data_dir = smoke_root / "user-data"
    state_path = smoke_root / "desktop-lifecycle-state.json"
    app_log_path = smoke_root / "app.log"
    fixture_a = smoke_root / "visual-input-a.png"
    fixture_b = smoke_root / "visual-input-b.png"
    document_fixture = smoke_root / "selected-scope.md"
    process: subprocess.Popen[str] | None = None
    stream_thread: threading.Thread | None = None
    proxy = _ControlledConnectProxy() if exercise_physical_reconnect else None
    try:
        working_dir, _secret_dir, configured_model = staging._stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(strict=True),
            source_secret_dir=source_secret_dir.expanduser().resolve(strict=True),
            agent_id=agent_id,
            target_root=user_data_dir,
        )
        _assert_authorized_model(
            configured_model,
            expected_provider_id=expected_provider_id,
            expected_model_id=expected_model_id,
        )
        _create_visual_fixture(fixture_a, label="VISUAL INPUT A", code="CEDAR 427")
        _create_visual_fixture(fixture_b, label="VISUAL INPUT B", code="VIOLET 913")
        _create_document_fixture(document_fixture)
        env = _clean_app_env(packaged, user_data_dir=user_data_dir, state_path=state_path)
        if proxy is not None:
            proxy.start()
            _route_candidate_through_proxy(env, proxy_url=proxy.url)
        with app_log_path.open("w", encoding="utf-8") as app_log:
            process = subprocess.Popen(
                [str(executable)],
                cwd=str(app_path.parent),
                env=env,
                stdout=app_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            state = helpers._wait_for_state(
                state_path,
                timeout=timeout,
                predicate=lambda payload: int(payload.get("sequence") or 0) >= 1,
                process=process,
                expected_distribution=distribution,
            )
            helpers._assert_ready_state(state, expected_distribution=distribution)
            if helpers._assert_desktop_readiness(state, timeout=timeout) != state.get(
                "backend_pid"
            ):
                raise AssertionError("isolated candidate readiness pid mismatch")

            agent_workspace_dir = packaged._staged_agent_workspace_dir(working_dir)
            chat_id, session_id, topic_workspace = _create_chat(
                helpers,
                state,
                agent_workspace_dir=agent_workspace_dir,
                timeout=timeout,
            )
            base_url = helpers._state_base_url(state)
            headers = helpers._state_headers(state)
            upload_a, upload_a_ms = _upload_file(
                url=f"{base_url}/api/console/upload",
                headers=headers,
                path=fixture_a,
                timeout=timeout,
            )
            upload_b, upload_b_ms = _upload_file(
                url=f"{base_url}/api/console/upload",
                headers=headers,
                path=fixture_b,
                timeout=timeout,
            )
            document_upload, document_upload_ms = _upload_file(
                url=f"{base_url}/api/console/upload",
                headers=headers,
                path=document_fixture,
                media_type="text/markdown",
                timeout=timeout,
            )

            initial_id = f"initial-{uuid.uuid4().hex}"
            command_ids = [f"control-{uuid.uuid4().hex}" for _ in range(4)]
            observation = _StreamObservation()
            stream_thread = threading.Thread(
                target=_read_initial_stream,
                kwargs={
                    "url": f"{base_url}/api/console/chat",
                    "headers": headers,
                    "payload": _initial_payload(
                        chat_id=chat_id,
                        session_id=session_id,
                        client_turn_id=initial_id,
                    ),
                    "timeout": timeout,
                    "observation": observation,
                },
                name="packaged-steering-stream",
                daemon=True,
            )
            stream_thread.start()
            observation.wait_for(
                lambda: observation.initial_receipt is not None,
                timeout=timeout,
                label="initial run receipt",
            )
            initial_receipt = dict(observation.initial_receipt or {})
            run_id = str(initial_receipt.get("run_id") or "")
            attempt_id = str(initial_receipt.get("attempt_id") or "")
            if not run_id or not attempt_id:
                raise AssertionError("initial receipt omitted server-owned run identity")
            observation.wait_for(
                lambda: observation.first_reasoning_at is not None,
                timeout=timeout,
                label="initial provider reasoning",
            )
            thinking_before_redirect = observation.first_answer_at is None

            common = {
                "chat_id": chat_id,
                "session_id": session_id,
                "expected_run_id": run_id,
                "expected_attempt_id": attempt_id,
            }
            pure_payload = _control_payload(
                **common,
                command_id=command_ids[0],
                intent="redirect",
                content=[_image_block(upload_a)],
            )
            redirect_a_started = time.monotonic()
            status_a, receipt_a, admission_a_ms = _post_control(
                url=f"{base_url}/api/console/chat",
                headers=headers,
                payload=pure_payload,
                timeout=timeout,
            )
            _retry_status, retry_receipt, _retry_ms = _post_control(
                url=f"{base_url}/api/console/chat",
                headers=headers,
                payload=pure_payload,
                timeout=timeout,
            )
            retry_same_receipt = _immutable_receipt_identity(receipt_a) == (
                _immutable_receipt_identity(retry_receipt)
            )
            observation.wait_for(
                lambda: command_ids[0] in observation.surface_at,
                timeout=timeout,
                label="pure-image successor model surface",
            )

            text_payload = _control_payload(
                **common,
                command_id=command_ids[1],
                intent="supplement",
                content=[{"type": "text", "text": _TEXT_SUPPLEMENT}],
            )
            status_text, receipt_text, admission_text_ms = _post_control(
                url=f"{base_url}/api/console/chat",
                headers=headers,
                payload=text_payload,
                timeout=timeout,
            )
            observation.wait_for(
                lambda: command_ids[1] in observation.surface_at,
                timeout=timeout,
                label="text-supplement model surface",
            )

            document_payload = _control_payload(
                **common,
                command_id=command_ids[2],
                intent="supplement",
                content=[
                    {"type": "text", "text": _DOCUMENT_SUPPLEMENT},
                    _file_block(document_upload, media_type="text/markdown"),
                ],
            )
            status_document, receipt_document, admission_document_ms = _post_control(
                url=f"{base_url}/api/console/chat",
                headers=headers,
                payload=document_payload,
                timeout=timeout,
            )
            observation.wait_for(
                lambda: DOCUMENT_SCOPE_SENTINEL in observation.joined_answer(),
                timeout=timeout,
                label="selected document scope sentinel",
            )
            disconnect_recovery_ms: int | None = None
            disconnect_recovery_events: list[dict[str, Any]] = []
            if proxy is not None:
                proxy.wait_for_active(timeout=timeout)
                disconnect_started = time.monotonic()
                if proxy.drop_active() < 1:
                    raise AssertionError("candidate provider tunnel was not dropped")
                disconnect_recovery_events = _wait_for_provider_disconnect_recovery(
                    helpers,
                    state,
                    chat_id=chat_id,
                    timeout=timeout,
                )
                disconnect_recovery_ms = round(
                    (time.monotonic() - disconnect_started) * 1000
                )

            mixed_payload = _control_payload(
                **common,
                command_id=command_ids[3],
                intent="redirect",
                content=[
                    _image_block(upload_b),
                    {"type": "text", "text": _MIXED_REDIRECT_TEXT},
                ],
            )
            redirect_b_started = time.monotonic()
            status_b, receipt_b, admission_b_ms = _post_control(
                url=f"{base_url}/api/console/chat",
                headers=headers,
                payload=mixed_payload,
                timeout=timeout,
            )
            observation.wait_for(
                lambda: command_ids[3] in observation.surface_at,
                timeout=timeout,
                label="mixed redirect successor model surface",
            )
            observation.wait_for(
                lambda: observation.completed,
                timeout=timeout,
                label="single Runtime run completion",
            )
            if stream_thread is not None:
                stream_thread.join(timeout=5)

            final_receipts = _wait_for_terminal_receipts(
                helpers,
                state,
                session_id=session_id,
                chat_id=chat_id,
                command_ids=command_ids,
                timeout=timeout,
            )
            _assert_provider_request_budget(final_receipts)
            if any(
                receipt.get("execution_status") != "applied"
                for receipt in final_receipts
            ):
                raise AssertionError("an admitted smoke command was not applied")
            if any(
                receipt.get("run_id") != run_id
                or receipt.get("attempt_id") != attempt_id
                for receipt in final_receipts
            ):
                raise AssertionError("control receipts changed Run or Attempt lineage")

            events = _runtime_events(
                helpers,
                state,
                chat_id=chat_id,
                timeout=timeout,
            )
            yield_warnings = [
                event
                for event in events
                if event.get("type") == "runtime.warning"
                and isinstance(event.get("payload"), dict)
                and event["payload"].get("kind") == "model_stream_yielded_for_redirect"
            ]
            disconnect_recoveries = _provider_disconnect_recoveries(events)
            if disconnect_recovery_events and not disconnect_recoveries:
                raise AssertionError("provider recovery event was not durable")
            persistence = _inspect_persistence(
                topic_workspace=topic_workspace,
                chat_id=chat_id,
                command_ids=command_ids,
                run_id=run_id,
                attempt_id=attempt_id,
                configured_model=configured_model,
            )

        migration = staging._assert_profile_migrated(
            Path(str(configured_model["agent_path"]))
        )
        surface_a_at = observation.surface_at[command_ids[0]]
        surface_b_at = observation.surface_at[command_ids[3]]
        safe_uploads = [
            {
                "slot": "attachment1",
                "digest": upload_a["digest"],
                "version": upload_a["version"],
                "bytes": upload_a["size"],
                "upload_ms": upload_a_ms,
            },
            {
                "slot": "attachment2",
                "digest": upload_b["digest"],
                "version": upload_b["version"],
                "bytes": upload_b["size"],
                "upload_ms": upload_b_ms,
            },
            {
                "slot": "attachment3",
                "digest": document_upload["digest"],
                "version": document_upload["version"],
                "bytes": document_upload["size"],
                "upload_ms": document_upload_ms,
                "media_type": "text/markdown",
                "asset_version": "steering-document-v1",
                "selected_range": "SCOPE-B",
            },
        ]
        observation_payload = {
            **persistence,
            "thinking_before_redirect": thinking_before_redirect,
            "answer_after_redirect": command_ids[3] in observation.surface_at,
            "visual_sentinel_matched": FINAL_VISUAL_SENTINEL
            in observation.joined_answer(),
            "document_scope_sentinel_matched": DOCUMENT_SCOPE_SENTINEL
            in observation.joined_answer(),
            "stream_completed": observation.completed,
            "old_stream_yielded": len(yield_warnings) >= 2,
            "retry_same_receipt": retry_same_receipt,
            "physical_reconnect_required": exercise_physical_reconnect,
            "physical_disconnect_recovered": (
                len(disconnect_recoveries) == 1 if proxy is not None else True
            ),
            "candidate_scoped_proxy": proxy is not None,
            "proxy_tunnel_count": proxy.tunnel_count if proxy is not None else 0,
            "proxy_drop_count": proxy.drop_count if proxy is not None else 0,
            "safe_uploads": safe_uploads,
            "migration": migration,
            "timing_ms": {
                "first_redirect_admission": admission_a_ms,
                "text_supplement_admission": admission_text_ms,
                "document_supplement_admission": admission_document_ms,
                "second_redirect_admission": admission_b_ms,
                "first_redirect_to_successor_surface": round(
                    (surface_a_at - redirect_a_started) * 1000
                ),
                "second_redirect_to_successor_surface": round(
                    (surface_b_at - redirect_b_started) * 1000
                ),
                **(
                    {"physical_disconnect_to_recovery": disconnect_recovery_ms}
                    if disconnect_recovery_ms is not None
                    else {}
                ),
            },
        }
        admissions = [
            {
                **receipt_a,
                "intent": "redirect",
                "http_status": status_a,
                "admission_ms": admission_a_ms,
            },
            {
                **receipt_text,
                "intent": "supplement",
                "http_status": status_text,
                "admission_ms": admission_text_ms,
            },
            {
                **receipt_document,
                "intent": "supplement",
                "http_status": status_document,
                "admission_ms": admission_document_ms,
            },
            {
                **receipt_b,
                "intent": "redirect",
                "http_status": status_b,
                "admission_ms": admission_b_ms,
            },
        ]
        evidence = _build_safe_evidence(
            configured_model=configured_model,
            expected_provider_id=expected_provider_id,
            expected_model_id=expected_model_id,
            app_metadata=helpers._app_metadata_for_evidence(app_path),
            run_identity={"run_id": run_id, "attempt_id": attempt_id},
            admissions=admissions,
            final_receipts=final_receipts,
            observation=observation_payload,
        )
        if not evidence["ok"]:
            raise AssertionError("real-provider steering acceptance evidence is incomplete")
        return evidence
    finally:
        helpers._stop_app(process)
        if proxy is not None:
            proxy.stop()
        shutil.rmtree(smoke_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--distribution", choices=("official",), default="official")
    parser.add_argument("--source-working-dir", type=Path, required=True)
    parser.add_argument("--source-secret-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--expected-provider-id", default=EXPECTED_PROVIDER_ID)
    parser.add_argument("--expected-model-id", default=EXPECTED_MODEL_ID)
    parser.add_argument("--exercise-physical-reconnect", action="store_true")
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(
            app=args.app,
            distribution=args.distribution,
            source_working_dir=args.source_working_dir,
            source_secret_dir=args.source_secret_dir,
            agent_id=args.agent_id,
            timeout=args.timeout,
            expected_provider_id=args.expected_provider_id,
            expected_model_id=args.expected_model_id,
            exercise_physical_reconnect=args.exercise_physical_reconnect,
        )
        if args.evidence_output:
            helpers, _staging = _load_helpers()
            helpers._write_json_file(args.evidence_output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - redact all local/provider detail
        message = str(exc)
        failure = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_hash": hashlib.sha256(message.encode("utf-8")).hexdigest()[:16],
        }
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
