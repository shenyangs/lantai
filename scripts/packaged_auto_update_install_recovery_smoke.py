#!/usr/bin/env python3
"""Exercise a real macOS packaged update, including failure and retry recovery.

The smoke never publishes a release. It copies a previous signed app to an
operating-system temporary directory, points that isolated copy at a local HTTP
feed, deliberately serves one corrupt update, retries with the exact candidate
ZIP, invokes the app's real ``electron-updater`` install IPC, and requires the
updated app to relaunch with its isolated user data intact.

Only the temporary app copy is modified/re-signed. The source apps, Downloads
candidate, user's real app data, and the normal updater cache are read-only.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.server
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websocket


SCHEMA = "com.lantai.packaged-auto-update-install-recovery-smoke.v1"
UPDATE_FILENAME = "Lantai-2.0.0-arm64.zip"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha512_base64(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _bundle_info(app: Path) -> dict[str, str]:
    with (app / "Contents/Info.plist").open("rb") as handle:
        payload = plistlib.load(handle)
    return {
        "bundle_id": str(payload.get("CFBundleIdentifier") or ""),
        "version": str(payload.get("CFBundleShortVersionString") or ""),
        "build_version": str(payload.get("CFBundleVersion") or ""),
    }


def _app_executable(app: Path) -> Path:
    info = _bundle_info(app)
    with (app / "Contents/Info.plist").open("rb") as handle:
        payload = plistlib.load(handle)
    executable_name = str(payload.get("CFBundleExecutable") or "")
    executable = app / "Contents/MacOS" / executable_name
    if not executable.is_file():
        raise FileNotFoundError(f"Packaged app executable is missing: {executable}")
    if info["bundle_id"] != "com.lantai.app":
        raise AssertionError(f"Unexpected app bundle identifier: {info!r}")
    return executable


def _app_hashes(app: Path) -> dict[str, str]:
    app_asar = app / "Contents/Resources/app.asar"
    if not app_asar.is_file():
        raise FileNotFoundError(f"Packaged app.asar is missing: {app_asar}")
    return {
        "executable_sha256": _sha256(_app_executable(app)),
        "app_asar_sha256": _sha256(app_asar),
    }


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _directory_snapshot(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    snapshot: list[dict[str, Any]] = []
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        stat = item.stat()
        snapshot.append(
            {
                "path": str(item.relative_to(path)),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return snapshot


@dataclass
class _FeedState:
    yml: bytes
    update_zip: Path
    serve_corrupt_update: bool = True
    requests: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, *, path: str, status: int, size: int, ranged: bool) -> None:
        with self.lock:
            self.requests.append(
                {
                    "path": path,
                    "status": status,
                    "size": size,
                    "ranged": ranged,
                    "mode": "corrupt" if self.serve_corrupt_update else "candidate",
                }
            )

    def set_candidate(self) -> None:
        with self.lock:
            self.serve_corrupt_update = False

    def snapshot_requests(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.requests)


class _UpdateFeedHandler(http.server.BaseHTTPRequestHandler):
    server_version = "LantaiUpdateSmoke/1"

    @property
    def feed(self) -> _FeedState:
        return self.server.feed_state  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        total = path.stat().st_size
        range_header = str(self.headers.get("Range") or "")
        start = 0
        end = total - 1
        status = 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not match:
                self.send_error(416, "Unsupported range")
                self.feed.record(
                    path=urllib.parse.urlsplit(self.path).path,
                    status=416,
                    size=0,
                    ranged=True,
                )
                return
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else total - 1
            if start < 0 or end < start or start >= total:
                self.send_error(416, "Invalid range")
                self.feed.record(
                    path=urllib.parse.urlsplit(self.path).path,
                    status=416,
                    size=0,
                    ranged=True,
                )
                return
            end = min(end, total - 1)
            status = 206
        size = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if self.command != "HEAD":
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = size
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        self.feed.record(
            path=urllib.parse.urlsplit(self.path).path,
            status=status,
            size=size,
            ranged=bool(range_header),
        )

    def _handle(self) -> None:
        request_path = urllib.parse.urlsplit(self.path).path
        if request_path == "/latest-mac.yml":
            self._send_bytes(self.feed.yml, "text/yaml; charset=utf-8")
            self.feed.record(
                path=request_path,
                status=200,
                size=len(self.feed.yml),
                ranged=False,
            )
            return
        if request_path == f"/{UPDATE_FILENAME}":
            if self.feed.serve_corrupt_update:
                body = b"LANTAI_CORRUPT_UPDATE_SMOKE\n" * 4096
                self._send_bytes(body, "application/zip")
                self.feed.record(
                    path=request_path,
                    status=200,
                    size=len(body),
                    ranged=False,
                )
            else:
                self._send_file(self.feed.update_zip)
            return
        self.send_error(404, "Not found")
        self.feed.record(
            path=request_path,
            status=404,
            size=0,
            ranged=bool(self.headers.get("Range")),
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()


class _UpdateFeedServer:
    def __init__(self, state: _FeedState) -> None:
        self.state = state
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _UpdateFeedHandler
        )
        self.server.feed_state = state  # type: ignore[attr-defined]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="lantai-update-feed-smoke",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> _UpdateFeedServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _renderer_target(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=1
        ) as response:
            targets = json.load(response)
    except (OSError, ValueError):
        return None
    candidates = [
        target
        for target in targets
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
    ]
    candidates.sort(
        key=lambda target: str(target.get("url") or "").startswith("http://127.0.0.1"),
        reverse=True,
    )
    return candidates[0] if candidates else None


def _wait_renderer(port: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        target = _renderer_target(port)
        if target is not None:
            try:
                value = _cdp_evaluate(
                    port,
                    "Boolean(window.electronAPI?.getUpdateStatus)",
                    timeout=5,
                )
                if value is True:
                    return
            except (OSError, RuntimeError, websocket.WebSocketException):
                pass
        time.sleep(0.25)
    raise TimeoutError("Packaged renderer did not expose the desktop update API")


def _cdp_evaluate(port: int, expression: str, *, timeout: float) -> Any:
    target = _renderer_target(port)
    if target is None:
        raise RuntimeError("Packaged renderer debug target is unavailable")
    connection = websocket.create_connection(
        str(target["webSocketDebuggerUrl"]),
        timeout=timeout,
        suppress_origin=True,
    )
    try:
        connection.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
            )
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = json.loads(connection.recv())
            if reply.get("id") != 1:
                continue
            if "error" in reply:
                raise RuntimeError(f"CDP evaluate failed: {reply['error']!r}")
            result = reply.get("result", {}).get("result", {})
            if result.get("subtype") == "error":
                raise RuntimeError(
                    f"Renderer expression failed: {result.get('description') or result!r}"
                )
            return result.get("value")
        raise TimeoutError("CDP evaluate did not return before timeout")
    finally:
        connection.close()


def _update_status(port: int, *, timeout: float = 10) -> dict[str, Any]:
    value = _cdp_evaluate(
        port,
        "window.electronAPI.getUpdateStatus()",
        timeout=timeout,
    )
    if not isinstance(value, dict):
        raise AssertionError(f"Desktop update API returned invalid state: {value!r}")
    return value


def _run_update_check(port: int, *, timeout: float = 30) -> dict[str, Any]:
    value = _cdp_evaluate(
        port,
        "window.electronAPI.checkForUpdates()",
        timeout=timeout,
    )
    if not isinstance(value, dict):
        raise AssertionError(f"Desktop update check returned invalid state: {value!r}")
    return value


def _wait_update_status(
    port: int,
    expected: set[str],
    *,
    timeout: float,
    process: subprocess.Popen[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"Packaged app exited while waiting for update state: {process.returncode}"
            )
        try:
            latest = _update_status(port)
        except (OSError, RuntimeError, websocket.WebSocketException):
            time.sleep(0.25)
            continue
        if str(latest.get("status") or "") in expected:
            return latest
        time.sleep(0.25)
    raise TimeoutError(
        f"Timed out waiting for update state {sorted(expected)}: {latest!r}"
    )


def _wait_lifecycle_state(
    state_path: Path,
    *,
    timeout: float,
    process: subprocess.Popen[str] | None = None,
    previous_nonce: str = "",
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(
                f"Packaged app exited before desktop readiness: {process.returncode}"
            )
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            time.sleep(0.2)
            continue
        if isinstance(payload, dict):
            latest = payload
            nonce = str(payload.get("desktop_instance_nonce") or "")
            if (
                int(payload.get("backend_pid") or 0) > 0
                and nonce
                and (not previous_nonce or nonce != previous_nonce)
            ):
                return payload
        time.sleep(0.2)
    safe_latest = {
        "event": latest.get("event"),
        "sequence": latest.get("sequence"),
        "distribution_channel": latest.get("distribution_channel"),
        "has_backend_process": latest.get("has_backend_process"),
        "backend_pid_present": int(latest.get("backend_pid") or 0) > 0,
        "desktop_instance_nonce_present": bool(
            latest.get("desktop_instance_nonce")
        ),
        "new_instance_observed": bool(
            latest.get("desktop_instance_nonce")
            and latest.get("desktop_instance_nonce") != previous_nonce
        ),
    }
    raise TimeoutError(f"Packaged desktop readiness timed out: {safe_latest!r}")


def _assert_backend_healthy(state: dict[str, Any], *, timeout: float = 10) -> None:
    base_url = str(state.get("api_base_url") or "").rstrip("/")
    token = str(state.get("desktop_token") or "")
    if not base_url or not token:
        raise AssertionError("Lifecycle state omitted backend URL or desktop token")
    request = urllib.request.Request(
        f"{base_url}/api/desktop/readiness",
        headers={"X-Lantai-Desktop-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise AssertionError(f"Updated backend health returned {response.status}")
            payload = json.load(response)
    except urllib.error.URLError as error:
        raise AssertionError(f"Updated backend health failed: {error.reason}") from error
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or payload.get("desktop") is not True
        or payload.get("instance_nonce") != state.get("desktop_instance_nonce")
        or int(payload.get("pid") or 0) != int(state.get("backend_pid") or 0)
    ):
        raise AssertionError(f"Updated backend readiness proof is invalid: {payload!r}")


def _signing_identity(app: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/codesign", "-dv", "--verbose=2", str(app)],
        text=True,
        capture_output=True,
        check=False,
    )
    diagnostic = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"^Authority=(Developer ID Application: .+)$", diagnostic, re.M)
    if result.returncode != 0 or not match:
        raise AssertionError("Previous app does not expose a Developer ID signing identity")
    return match.group(1).strip()


def _resign_temporary_app(app: Path, *, identity: str) -> None:
    result = subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            identity,
            "--preserve-metadata=identifier,entitlements,requirements,flags",
            str(app),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "Unable to re-sign isolated updater fixture: "
            + (result.stderr or result.stdout).strip()
        )
    verify = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        text=True,
        capture_output=True,
        check=False,
    )
    if verify.returncode != 0:
        raise AssertionError(
            "Isolated updater fixture failed code-sign verification: "
            + (verify.stderr or verify.stdout).strip()
        )


def _matching_app_pids(executable: Path) -> list[int]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        text=True,
        capture_output=True,
        check=True,
    )
    marker = str(executable)
    pids: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or marker not in stripped:
            continue
        raw_pid, _separator, _command = stripped.partition(" ")
        with contextlib.suppress(ValueError):
            pids.append(int(raw_pid))
    return sorted(set(pids))


def _stop_temporary_processes(executable: Path) -> None:
    pids = _matching_app_pids(executable)
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _matching_app_pids(executable):
        time.sleep(0.2)
    for pid in _matching_app_pids(executable):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _wait_process_exit(process: subprocess.Popen[str], *, timeout: float) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("Old app did not exit for update installation") from error


def _wait_bundle_version(app: Path, expected: str, *, timeout: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    latest: dict[str, str] = {}
    while time.monotonic() < deadline:
        try:
            latest = _bundle_info(app)
        except (FileNotFoundError, OSError, plistlib.InvalidFileException):
            time.sleep(0.2)
            continue
        if latest.get("version") == expected:
            return latest
        time.sleep(0.2)
    raise TimeoutError(f"Installed app did not become v{expected}: {latest!r}")


def _validate_inputs(
    previous_app: Path,
    candidate_app: Path,
    update_zip: Path,
    latest_yml: Path,
) -> tuple[dict[str, str], dict[str, str], bytes]:
    for path in (previous_app, candidate_app):
        if not path.is_dir():
            raise FileNotFoundError(f"App bundle is missing: {path}")
    for path in (update_zip, latest_yml):
        if not path.is_file():
            raise FileNotFoundError(f"Update artifact is missing: {path}")
    previous = _bundle_info(previous_app)
    candidate = _bundle_info(candidate_app)
    if previous["version"] != "1.0.49":
        raise AssertionError(f"Expected previous app v1.0.49, got {previous!r}")
    if candidate["version"] != "2.0.0":
        raise AssertionError(f"Expected candidate app v2.0.0, got {candidate!r}")
    if previous["bundle_id"] != candidate["bundle_id"]:
        raise AssertionError("Previous and candidate app bundle identifiers differ")
    yml = latest_yml.read_bytes()
    text = yml.decode("utf-8")
    expected_sha512 = _sha512_base64(update_zip)
    if "version: 2.0.0" not in text or UPDATE_FILENAME not in text:
        raise AssertionError("latest-mac.yml does not describe the 2.0.0 ZIP")
    if expected_sha512 not in text:
        raise AssertionError("latest-mac.yml SHA-512 does not match the candidate ZIP")
    if f"size: {update_zip.stat().st_size}" not in text:
        raise AssertionError("latest-mac.yml size does not match the candidate ZIP")
    return previous, candidate, yml


def _run(args: argparse.Namespace) -> dict[str, Any]:
    previous_app = Path(args.previous_app).expanduser().resolve()
    candidate_app = Path(args.candidate_app).expanduser().resolve()
    update_zip = Path(args.update_zip).expanduser().resolve()
    latest_yml = Path(args.latest_yml).expanduser().resolve()
    evidence_output = Path(args.evidence_output).expanduser().resolve()
    previous_info, candidate_info, yml = _validate_inputs(
        previous_app, candidate_app, update_zip, latest_yml
    )
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-auto-update-smoke-"))
    install_root = smoke_root / "installation"
    installed_app = install_root / "澜台.app"
    user_data = smoke_root / "user-data"
    state_path = smoke_root / "desktop-lifecycle.json"
    log_path = smoke_root / "app.log"
    sentinel_path = user_data / "auto-update-data-sentinel.json"
    cache_name = f"lantai-auto-update-smoke-{uuid.uuid4().hex}"
    isolated_cache = Path.home() / "Library/Caches" / cache_name
    normal_cache = Path.home() / "Library/Caches/lantai-console-updater"
    normal_cache_before = _directory_snapshot(normal_cache)
    debug_port = _free_local_port()
    process: subprocess.Popen[str] | None = None
    failure: str | None = None
    failure_state: dict[str, Any] = {}
    downloaded_state: dict[str, Any] = {}
    installing_state: dict[str, Any] = {}
    initial_state: dict[str, Any] = {}
    relaunched_state: dict[str, Any] = {}
    feed_state = _FeedState(yml=yml, update_zip=update_zip)
    try:
        install_root.mkdir(parents=True)
        shutil.copytree(previous_app, installed_app, symlinks=True)
        fixture_update_config = installed_app / "Contents/Resources/app-update.yml"
        fixture_update_config.write_text(
            "owner: shenyangs\n"
            "repo: lantai3-releases\n"
            "provider: github\n"
            f"updaterCacheDirName: {cache_name}\n",
            encoding="utf-8",
        )
        identity = _signing_identity(previous_app)
        _resign_temporary_app(installed_app, identity=identity)
        executable = _app_executable(installed_app)
        sentinel = {
            "schema": "com.lantai.auto-update-data-sentinel.v1",
            "nonce": uuid.uuid4().hex,
            "created_at_epoch": int(time.time()),
        }
        _write_json(sentinel_path, sentinel)
        env = os.environ.copy()
        for key in (
            "LANTAI_DISABLE_GITHUB_UPDATER",
            "LANTAI_DESKTOP_UPDATE_REPO",
            "LANTAI_UPDATE_AUTH_HEADER",
        ):
            env.pop(key, None)
        env.update(
            {
                "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE": str(state_path),
                "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data),
                "LANTAI_DESKTOP_HEADLESS_SMOKE": "1",
                "LANTAI_WORKING_DIR": str(user_data / "working"),
                "LANTAI_SECRET_DIR": str(user_data / "working.secret"),
                "LANTAI_AUTH_ENABLED": "0",
                "LANTAI_DISABLE_KEYRING": "1",
                "LANTAI_SKIP_KEYRING": "1",
                "LANTAI_REMINDER_DELIVERY_WORKER": "0",
            }
        )
        with _UpdateFeedServer(feed_state) as feed:
            env["LANTAI_DESKTOP_UPDATE_URL"] = feed.base_url
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [str(executable), f"--remote-debugging-port={debug_port}"],
                    cwd=str(installed_app.parent),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                initial_state = _wait_lifecycle_state(
                    state_path, timeout=args.boot_timeout, process=process
                )
                _assert_backend_healthy(initial_state)
                _wait_renderer(debug_port, timeout=args.boot_timeout)
                initial_update = _update_status(debug_port)
                if initial_update.get("supported") is not True:
                    raise AssertionError(
                        f"Packaged updater is not supported: {initial_update!r}"
                    )

                _run_update_check(debug_port, timeout=args.check_timeout)
                failure_state = _wait_update_status(
                    debug_port,
                    {"error"},
                    timeout=args.download_timeout,
                    process=process,
                )
                if process.poll() is not None:
                    raise AssertionError("Corrupt update terminated the packaged app")
                _assert_backend_healthy(initial_state)
                failure_requests = feed_state.snapshot_requests()
                if not any(
                    request["path"] == f"/{UPDATE_FILENAME}"
                    and request["mode"] == "corrupt"
                    for request in failure_requests
                ):
                    raise AssertionError("Corrupt update was not requested from the feed")

                feed_state.set_candidate()
                _run_update_check(debug_port, timeout=args.check_timeout)
                downloaded_state = _wait_update_status(
                    debug_port,
                    {"downloaded"},
                    timeout=args.download_timeout,
                    process=process,
                )
                if downloaded_state.get("downloadedVersion") != "2.0.0":
                    raise AssertionError(
                        f"Candidate update did not download: {downloaded_state!r}"
                    )
                success_requests = feed_state.snapshot_requests()
                if not any(
                    request["path"] == f"/{UPDATE_FILENAME}"
                    and request["mode"] == "candidate"
                    and request["status"] in {200, 206}
                    for request in success_requests
                ):
                    raise AssertionError("Candidate ZIP was not requested from the feed")

                try:
                    value = _cdp_evaluate(
                        debug_port,
                        "window.electronAPI.installUpdate()",
                        timeout=args.install_timeout,
                    )
                    if isinstance(value, dict):
                        installing_state = value
                except (
                    OSError,
                    RuntimeError,
                    websocket.WebSocketException,
                ):
                    # A clean renderer disconnect is expected once
                    # quitAndInstall closes the old app.
                    installing_state = {"status": "renderer-disconnected-for-install"}

                old_nonce = str(initial_state.get("desktop_instance_nonce") or "")
                old_exit_code = _wait_process_exit(
                    process, timeout=args.install_timeout
                )
                process = None
                installed_info = _wait_bundle_version(
                    installed_app, "2.0.0", timeout=args.install_timeout
                )
                relaunched_state = _wait_lifecycle_state(
                    state_path,
                    timeout=args.relaunch_timeout,
                    previous_nonce=old_nonce,
                )
                _assert_backend_healthy(relaunched_state)
                if not _matching_app_pids(_app_executable(installed_app)):
                    raise AssertionError("Updated app did not remain running after relaunch")
                if json.loads(sentinel_path.read_text(encoding="utf-8")) != sentinel:
                    raise AssertionError("Isolated user-data sentinel changed during update")
                installed_hashes = _app_hashes(installed_app)
                candidate_hashes = _app_hashes(candidate_app)
                if installed_hashes != candidate_hashes:
                    raise AssertionError(
                        "Installed app does not match the exact candidate app: "
                        f"installed={installed_hashes!r} candidate={candidate_hashes!r}"
                    )
                codesign = subprocess.run(
                    [
                        "/usr/bin/codesign",
                        "--verify",
                        "--deep",
                        "--strict",
                        str(installed_app),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if codesign.returncode != 0:
                    raise AssertionError("Updated app failed strict code-sign verification")
                requests = feed_state.snapshot_requests()
                result = {
                    "schema": SCHEMA,
                    "passed": True,
                    "failure": None,
                    "transition": {
                        "from_version": previous_info["version"],
                        "to_version": installed_info["version"],
                        "old_process_exit_code": old_exit_code,
                        "auto_relaunch_observed": True,
                        "desktop_instance_changed": str(
                            relaunched_state.get("desktop_instance_nonce") or ""
                        )
                        != old_nonce,
                        "backend_healthy_after_relaunch": True,
                        "user_data_sentinel_preserved": True,
                    },
                    "recovery": {
                        "corrupt_download_rejected": True,
                        "app_remained_running_after_failure": True,
                        "backend_remained_healthy_after_failure": True,
                        "retry_downloaded_exact_candidate": True,
                        "failure_status": failure_state.get("status"),
                        "failure_message": failure_state.get("message"),
                        "failure_error_hash": hashlib.sha256(
                            str(failure_state.get("error") or "").encode("utf-8")
                        ).hexdigest()[:16],
                    },
                    "candidate": {
                        **candidate_info,
                        **candidate_hashes,
                        "zip_sha256": _sha256(update_zip),
                        "zip_sha512_base64": _sha512_base64(update_zip),
                        "zip_size": update_zip.stat().st_size,
                        "latest_yml_sha256": _sha256(latest_yml),
                    },
                    "installed": {
                        **installed_info,
                        **installed_hashes,
                        "strict_codesign_verified": True,
                    },
                    "states": {
                        "failure": {
                            "status": failure_state.get("status"),
                            "checking": failure_state.get("checking"),
                        },
                        "downloaded": {
                            "status": downloaded_state.get("status"),
                            "downloaded_version": downloaded_state.get(
                                "downloadedVersion"
                            ),
                            "progress_percent": downloaded_state.get(
                                "progressPercent"
                            ),
                        },
                        "install_invocation": {
                            "status": installing_state.get("status"),
                        },
                    },
                    "feed": {
                        "scheme": "http",
                        "loopback_only": True,
                        "request_count": len(requests),
                        "requests": requests,
                    },
                    "isolation": {
                        "temporary_app_copy": True,
                        "temporary_user_data": True,
                        "temporary_updater_cache": True,
                        "normal_updater_cache_unchanged": None,
                        "github_release_published": False,
                    },
                }
    except Exception as error:  # noqa: BLE001 - preserve failure evidence
        failure = f"{type(error).__name__}: {error}"
        result = {
            "schema": SCHEMA,
            "passed": False,
            "failure": failure,
            "previous": previous_info,
            "candidate": candidate_info,
            "states": {
                "failure": {
                    "status": failure_state.get("status"),
                    "checking": failure_state.get("checking"),
                },
                "downloaded": {
                    "status": downloaded_state.get("status"),
                    "downloaded_version": downloaded_state.get(
                        "downloadedVersion"
                    ),
                },
            },
            "feed": {
                "request_count": len(feed_state.snapshot_requests()),
                "requests": feed_state.snapshot_requests(),
            },
        }
        raise
    finally:
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=15)
        with contextlib.suppress(Exception):
            if installed_app.exists():
                _stop_temporary_processes(_app_executable(installed_app))
        normal_cache_after = _directory_snapshot(normal_cache)
        normal_cache_unchanged = normal_cache_after == normal_cache_before
        if isinstance(result, dict):
            isolation = result.setdefault("isolation", {})
            if isinstance(isolation, dict):
                isolation["normal_updater_cache_unchanged"] = normal_cache_unchanged
            if not normal_cache_unchanged and result.get("passed") is True:
                result["passed"] = False
                result["failure"] = "Normal updater cache changed during isolated smoke"
        _write_json(evidence_output, result)
        shutil.rmtree(isolated_cache, ignore_errors=True)
        if args.keep_workdir:
            print(f"Auto-update smoke workdir: {smoke_root}")
        else:
            shutil.rmtree(smoke_root, ignore_errors=True)
    if result.get("passed") is not True:
        raise AssertionError(str(result.get("failure") or "Auto-update smoke failed"))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-app", required=True)
    parser.add_argument("--candidate-app", required=True)
    parser.add_argument("--update-zip", required=True)
    parser.add_argument("--latest-yml", required=True)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--boot-timeout", type=float, default=120)
    parser.add_argument("--check-timeout", type=float, default=45)
    parser.add_argument("--download-timeout", type=float, default=180)
    parser.add_argument("--install-timeout", type=float, default=120)
    parser.add_argument("--relaunch-timeout", type=float, default=120)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except Exception as error:  # noqa: BLE001 - CLI diagnostics
        print(f"Packaged auto-update smoke failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
