#!/usr/bin/env python3
"""Smoke-test a frozen desktop backend through the real console HTTP path.

This intentionally exercises the packaged backend executable instead of the
editable Python package. It catches P0 regressions where the app starts but a
basic console message silently does nothing, or a failed turn is persisted as
completed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKEND = (
    PROJECT_ROOT
    / "console"
    / "electron-dist"
    / "lantai-server"
    / "Lantai Backend.app"
    / "Contents"
    / "MacOS"
    / "lantai-server"
)
DESKTOP_TOKEN_ENV = "LANTAI_DESKTOP_TOKEN"
DESKTOP_TOKEN_HEADER = "X-Lantai-Desktop-Token"
DESKTOP_INSTANCE_NONCE_ENV = "LANTAI_DESKTOP_INSTANCE_NONCE"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _tail_text(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except FileNotFoundError:
        return ""


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


def _poll_health(base_url: str, *, timeout: float, log_path: Path) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status, _body, _parsed = _json_request(
                "GET",
                f"{base_url}/health",
                timeout=2,
            )
            if status == 200:
                return
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            last_error = repr(exc)
        time.sleep(0.25)
    raise RuntimeError(
        "Timed out waiting for frozen backend /health.\n"
        f"Last error: {last_error}\n"
        f"Backend log tail:\n{_tail_text(log_path)}"
    )


def _assert_desktop_readiness(
    base_url: str,
    *,
    headers: dict[str, str],
    expected_nonce: str,
    timeout: float,
) -> int:
    try:
        _status, _body, payload = _json_request(
            "GET",
            f"{base_url}/api/desktop/readiness",
            headers=headers,
            timeout=timeout,
        )
    except error.HTTPError as exc:
        if exc.code == 404:
            raise AssertionError(
                "Desktop readiness endpoint returned 404. "
                "The frozen backend is stale or was not built from source "
                "that includes /api/desktop/readiness."
            ) from exc
        raise
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected desktop readiness object, got {payload!r}")
    if payload.get("status") != "ok" or payload.get("desktop") is not True:
        raise AssertionError(f"Desktop readiness proof is not ok: {payload!r}")
    if payload.get("instance_nonce") != expected_nonce:
        raise AssertionError(
            "Desktop readiness proof nonce mismatch: "
            f"expected {expected_nonce!r}, got {payload.get('instance_nonce')!r}"
        )
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise AssertionError(f"Desktop readiness proof has invalid pid: {payload!r}")
    return pid


def _assert_api_version(
    base_url: str,
    *,
    headers: dict[str, str],
    expected_version: str,
    timeout: float,
) -> None:
    _status, _body, payload = _json_request(
        "GET",
        f"{base_url}/api/version",
        headers=headers,
        timeout=timeout,
    )
    actual_version = payload.get("version") if isinstance(payload, dict) else None
    if actual_version != expected_version:
        raise AssertionError(
            "Frozen backend API version mismatch: "
            f"running={actual_version!r}, expected={expected_version!r}."
        )


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
        raise AssertionError(f"Expected /api/chats to return a list, got: {chats!r}")
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        if chat.get("session_id") == session_id:
            chat_id = str(chat.get("id") or "")
            if chat_id:
                return chat_id
    raise AssertionError(f"Chat for session_id={session_id!r} was not listed")


def _events_from_payload(events_payload: Any) -> list[dict[str, Any]]:
    if not isinstance(events_payload, dict):
        raise AssertionError(f"Expected runtime events object, got {events_payload!r}")
    events = events_payload.get("events")
    if not isinstance(events, list) or not events:
        raise AssertionError("Runtime events response did not include any events")
    return [event for event in events if isinstance(event, dict)]


def _assert_failure_events(events_payload: Any) -> None:
    events = _events_from_payload(events_payload)
    final = events[-1]
    if final.get("type") != "turn.failed":
        raise AssertionError(f"Expected final runtime event turn.failed, got {final}")
    if final.get("status") != "failed":
        raise AssertionError(f"Expected final runtime status failed, got {final}")
    payload = final.get("payload") if isinstance(final.get("payload"), dict) else {}
    if payload.get("final_task_status") != "failed":
        raise AssertionError(
            "Expected final_task_status=failed in final runtime event, "
            f"got {payload!r}"
        )


def _assert_runtime_event_count(
    events_payload: Any,
    event_type: str,
    expected: int,
) -> None:
    events = _events_from_payload(events_payload)
    actual = sum(1 for event in events if event.get("type") == event_type)
    if actual != expected:
        raise AssertionError(
            f"Expected {expected} {event_type!r} runtime event(s), got {actual}: "
            f"{events!r}"
        )


def _assert_distribution_tools(
    base_url: str,
    *,
    distribution: str,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> None:
    _status, _body, tools_payload = _json_request(
        "GET",
        f"{base_url}/api/tools",
        headers=headers,
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
    if distribution == "mas":
        forbidden |= {"execute_shell_command"} & tool_names
    if forbidden:
        label = "MAS" if distribution == "mas" else "frozen"
        raise AssertionError(
            f"{label} backend exposed forbidden local execution tools: "
            f"{sorted(forbidden)}",
        )


def _assert_local_retrieval_runtime(
    base_url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float,
) -> None:
    """Require the packaged backend to execute its real local vector probe."""
    _status, _body, payload = _json_request(
        "GET",
        f"{base_url}/api/workspace/layered-memory",
        headers=headers,
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise AssertionError(
            f"Expected layered-memory diagnostics object, got {payload!r}"
        )
    status = payload.get("engine_status")
    if not isinstance(status, dict):
        raise AssertionError(f"Missing retrieval engine status: {payload!r}")
    embedding = status.get("embedding")
    vector_index = status.get("vector_index")
    if not isinstance(embedding, dict) or not isinstance(vector_index, dict):
        raise AssertionError(f"Incomplete retrieval engine status: {status!r}")
    expected_probe = {"write": True, "query": True, "delete": True}
    if (
        status.get("overall") != "ready"
        or embedding.get("status") != "已就绪"
        or embedding.get("provider") != "local"
        or int(embedding.get("dimensions") or 0) != 512
        or vector_index.get("status") != "已就绪"
        or vector_index.get("probe") != expected_probe
    ):
        raise AssertionError(
            "Packaged local retrieval runtime did not pass its real "
            f"embedding/vector probe: {status!r}"
        )


def _build_env(
    args: argparse.Namespace,
    smoke_root: Path,
    *,
    desktop_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    workdir = smoke_root / "workdir"
    data_dir = smoke_root / "lantai-data"
    workdir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    env.update(
        {
            "LANTAI_WORKING_DIR": str(data_dir),
            "LANTAI_DESKTOP_APP": "1",
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_CONSOLE_STATIC_DIR": str(PROJECT_ROOT / "console" / "dist"),
        }
    )
    if args.native_default:
        env["LANTAI_RUNTIME_ENGINE_DEFAULT"] = "native"
    if args.distribution == "mas":
        env.update(
            {
                "LANTAI_APP_STORE": "1",
                "LANTAI_DISTRIBUTION_CHANNEL": "mas",
                "LANTAI_RELEASE_CHANNEL": "mas",
            }
        )
    else:
        env.pop("LANTAI_APP_STORE", None)
        env["LANTAI_DISTRIBUTION_CHANNEL"] = "official"
        env["LANTAI_RELEASE_CHANNEL"] = "dev"
    if desktop_env:
        env.update(desktop_env)
    return env


def _desktop_auth_for_cycle(
    args: argparse.Namespace,
    *,
    cycle: int,
) -> tuple[dict[str, str], dict[str, str], str]:
    if not (args.desktop_auth or args.restart_cycle):
        return {}, {}, ""
    token = args.desktop_token or f"desktop-smoke-token-{uuid.uuid4().hex}"
    nonce = args.desktop_instance_nonce or (
        f"desktop-smoke-nonce-{cycle}-{uuid.uuid4().hex}"
    )
    desktop_env = {
        DESKTOP_TOKEN_ENV: token,
        DESKTOP_INSTANCE_NONCE_ENV: nonce,
    }
    headers = {DESKTOP_TOKEN_HEADER: token}
    return desktop_env, headers, nonce


def _start_backend_process(
    *,
    backend: Path,
    port: int,
    env: dict[str, str],
    workdir: Path,
    log_file: Any,
) -> subprocess.Popen[str]:
    cmd = [
        str(backend),
        "app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    print(f"Starting frozen backend: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=workdir,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_backend_process(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=8)
    if proc.poll() is None:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def _assert_persisted_failure_events(
    base_url: str,
    *,
    session_id: str,
    headers: dict[str, str] | None,
    timeout: float,
) -> str:
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


def _exercise_console_failure_path(
    base_url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float,
) -> tuple[str, str]:
    session_id = f"console:frozen-smoke-{uuid.uuid4().hex[:12]}"
    client_turn_id = f"client-turn-frozen-smoke-{uuid.uuid4().hex[:12]}"
    chat_body = {
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "frozen backend console smoke",
                    }
                ],
            }
        ],
        "session_id": session_id,
        "user_id": "default",
        "channel": "console",
        "stream": True,
        "meta": {"client_turn_id": client_turn_id},
    }
    status, sse_text, _parsed = _json_request(
        "POST",
        f"{base_url}/api/console/chat",
        payload=chat_body,
        headers=headers,
        timeout=timeout,
    )
    if status != 200:
        raise AssertionError(f"Expected chat HTTP 200, got {status}")
    sse_payloads = _extract_sse_payloads(sse_text)
    if not sse_payloads:
        raise AssertionError(
            f"Expected at least one SSE payload, got body: {sse_text!r}"
        )
    final_response = sse_payloads[-1]
    if final_response.get("object") != "response":
        raise AssertionError(
            f"Expected final SSE object=response, got {final_response!r}"
        )
    if final_response.get("status") != "failed":
        raise AssertionError(
            "Expected no-model smoke to fail visibly, got "
            f"{final_response!r}"
        )

    chat_id = _assert_persisted_failure_events(
        base_url,
        session_id=session_id,
        headers=headers,
        timeout=timeout,
    )
    duplicate_status, duplicate_sse_text, _duplicate_parsed = _json_request(
        "POST",
        f"{base_url}/api/console/chat",
        payload=chat_body,
        headers=headers,
        timeout=timeout,
    )
    if duplicate_status != 200:
        raise AssertionError(
            f"Expected duplicate chat HTTP 200, got {duplicate_status}"
        )
    duplicate_payloads = _extract_sse_payloads(duplicate_sse_text)
    if not duplicate_payloads:
        raise AssertionError(
            "Expected at least one duplicate SSE payload, got body: "
            f"{duplicate_sse_text!r}"
        )
    duplicate_final = duplicate_payloads[-1]
    if duplicate_final.get("object") != "response":
        raise AssertionError(
            f"Expected duplicate final SSE object=response, got {duplicate_final!r}"
        )
    if duplicate_final.get("status") != "completed":
        raise AssertionError(
            "Expected duplicate client_turn_id to complete as a deduped replay, "
            f"got {duplicate_final!r}"
        )
    duplicate_metadata = (
        duplicate_final.get("metadata")
        if isinstance(duplicate_final.get("metadata"), dict)
        else {}
    )
    if duplicate_metadata.get("deduped_client_turn") is not True:
        raise AssertionError(
            "Expected duplicate client_turn_id metadata to mark dedupe, got "
            f"{duplicate_final!r}"
        )
    if duplicate_metadata.get("client_turn_id") != client_turn_id:
        raise AssertionError(
            "Duplicate client_turn_id metadata mismatch: "
            f"{duplicate_metadata!r}"
        )
    if not duplicate_metadata.get("duplicate_of_run_id"):
        raise AssertionError(
            "Expected duplicate client_turn_id metadata to include original run id, "
            f"got {duplicate_metadata!r}"
        )
    _status, _body, events_payload = _json_request(
        "GET",
        f"{base_url}/api/chats/{chat_id}/events?after_seq=0",
        headers=headers,
        timeout=timeout,
    )
    _assert_failure_events(events_payload)
    _assert_runtime_event_count(events_payload, "turn.started", 1)
    _assert_runtime_event_count(events_payload, "model.routing_decision", 1)
    _assert_runtime_event_count(events_payload, "turn.failed", 1)
    return session_id, chat_id


def _run_smoke(args: argparse.Namespace) -> None:
    backend = Path(args.backend).expanduser().resolve()
    if not backend.exists():
        raise FileNotFoundError(f"Frozen backend executable not found: {backend}")

    port = int(args.port or _find_free_port())
    base_url = f"http://127.0.0.1:{port}"
    smoke_root = Path(
        tempfile.mkdtemp(prefix=f"lantai-frozen-smoke-{args.distribution}-")
    )
    log_path = smoke_root / "backend.log"
    workdir = smoke_root / "workdir"
    print(f"Smoke root: {smoke_root}")

    proc: subprocess.Popen[str] | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            desktop_env, auth_headers, expected_nonce = _desktop_auth_for_cycle(
                args,
                cycle=1,
            )
            env = _build_env(args, smoke_root, desktop_env=desktop_env)
            proc = _start_backend_process(
                backend=backend,
                port=port,
                env=env,
                workdir=workdir,
                log_file=log_file,
            )
            _poll_health(base_url, timeout=args.timeout, log_path=log_path)
            if args.expected_version:
                _assert_api_version(
                    base_url,
                    headers=auth_headers,
                    expected_version=args.expected_version,
                    timeout=args.timeout,
                )
            if auth_headers:
                _assert_desktop_readiness(
                    base_url,
                    headers=auth_headers,
                    expected_nonce=expected_nonce,
                    timeout=args.timeout,
                )
            _assert_distribution_tools(
                base_url,
                distribution=args.distribution,
                headers=auth_headers,
                timeout=args.timeout,
            )
            _assert_local_retrieval_runtime(
                base_url,
                headers=auth_headers,
                timeout=args.timeout,
            )

            session_id, chat_id = _exercise_console_failure_path(
                base_url,
                headers=auth_headers,
                timeout=args.timeout,
            )
            if args.restart_cycle:
                _stop_backend_process(proc)
                proc = None
                if not args.port:
                    port = _find_free_port()
                    base_url = f"http://127.0.0.1:{port}"
                desktop_env, auth_headers, expected_nonce = _desktop_auth_for_cycle(
                    args,
                    cycle=2,
                )
                env = _build_env(args, smoke_root, desktop_env=desktop_env)
                proc = _start_backend_process(
                    backend=backend,
                    port=port,
                    env=env,
                    workdir=workdir,
                    log_file=log_file,
                )
                _poll_health(base_url, timeout=args.timeout, log_path=log_path)
                if args.expected_version:
                    _assert_api_version(
                        base_url,
                        headers=auth_headers,
                        expected_version=args.expected_version,
                        timeout=args.timeout,
                    )
                _assert_desktop_readiness(
                    base_url,
                    headers=auth_headers,
                    expected_nonce=expected_nonce,
                    timeout=args.timeout,
                )
                restarted_chat_id = _assert_persisted_failure_events(
                    base_url,
                    session_id=session_id,
                    headers=auth_headers,
                    timeout=args.timeout,
                )
                if restarted_chat_id != chat_id:
                    raise AssertionError(
                        "Restarted backend returned a different chat id for "
                        f"session {session_id!r}: {restarted_chat_id!r} != {chat_id!r}"
                    )
            print(
                "Frozen console smoke passed: "
                f"distribution={args.distribution} chat_id={chat_id}"
            )
    except (error.URLError, AssertionError, RuntimeError, TimeoutError) as exc:
        log_tail = _tail_text(log_path)
        raise RuntimeError(f"{exc}\nBackend log tail:\n{log_tail}") from exc
    finally:
        _stop_backend_process(proc)
        if not args.keep_workdir:
            shutil.rmtree(smoke_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test the frozen desktop backend console path.",
    )
    parser.add_argument(
        "--backend",
        default=str(DEFAULT_BACKEND),
        help="Path to frozen lantai-server executable.",
    )
    parser.add_argument(
        "--distribution",
        choices=("official", "mas"),
        default="official",
        help="Distribution environment to emulate.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to bind. Defaults to an ephemeral local port.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Seconds for health and HTTP operations.",
    )
    parser.add_argument(
        "--expected-version",
        default="",
        help="Require the running frozen backend /api/version to match this version.",
    )
    parser.add_argument(
        "--native-default",
        action="store_true",
        help="Set LANTAI_RUNTIME_ENGINE_DEFAULT=native for this smoke.",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Keep temporary smoke directories for inspection.",
    )
    parser.add_argument(
        "--desktop-auth",
        action="store_true",
        help=(
            "Set Electron desktop auth env vars and require "
            "/api/desktop/readiness proof."
        ),
    )
    parser.add_argument(
        "--desktop-token",
        default="",
        help="Desktop auth token to use with --desktop-auth.",
    )
    parser.add_argument(
        "--desktop-instance-nonce",
        default="",
        help="Desktop instance nonce to expect with --desktop-auth.",
    )
    parser.add_argument(
        "--restart-cycle",
        action="store_true",
        help=(
            "Restart the packaged backend once with the same data directory "
            "and verify the failed chat event history is still readable. "
            "This implies --desktop-auth."
        ),
    )
    args = parser.parse_args(argv)
    try:
        _run_smoke(args)
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(f"Frozen console smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
