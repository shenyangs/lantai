#!/usr/bin/env python3
"""Run the foundational Chat Runtime renderer gate against source code.

This is intentionally separate from the packaged-app lifecycle smoke: it is
fast enough to run before a build and uses an isolated workspace plus a local
OpenAI-compatible fake provider.  It exercises the source backend and the
real Electron renderer for the failures that otherwise escape unit tests:

* provider-visible thinking must retain its useful summary and order before a
  partial and final answer;
* a turn keeps one assistant card, avatar, and action bar;
* consecutive sends, rapid queueing, route changes, and stop/resend preserve
  the correct messages.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS_PATH = PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
_NATIVE_RUNTIME_DEFAULT_MIGRATION = "native-runtime-default-2026-07-24"


def _write_legacy_runtime_profile_for_smoke(working_dir: Path) -> dict[str, str]:
    """Seed the persisted legacy profile that a real upgraded user has."""

    workspace_dir = working_dir / "workspaces" / "default"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    config_path = working_dir / "config.json"
    agent_path = workspace_dir / "agent.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "active_agent": "default",
                    "profiles": {
                        "default": {
                            "id": "default",
                            "workspace_dir": str(workspace_dir),
                        },
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    agent_path.write_text(
        json.dumps(
            {
                "id": "default",
                "name": "Default",
                "description": "Legacy runtime migration smoke profile",
                "runtime": {"engine": "legacy"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "config_path": str(config_path),
        "agent_path": str(agent_path),
    }


def _assert_legacy_runtime_profile_migrated(profile: dict[str, str]) -> dict[str, object]:
    agent_path = Path(profile["agent_path"])
    migrated = json.loads(agent_path.read_text(encoding="utf-8"))
    backups = sorted(agent_path.parent.glob("agent.*.native-runtime-default.bak"))
    if migrated.get("runtime", {}).get("engine") != "native":
        raise RuntimeError("Persisted legacy Runtime profile was not migrated to native")
    if _NATIVE_RUNTIME_DEFAULT_MIGRATION not in migrated.get("runtime_migrations", []):
        raise RuntimeError("Persisted legacy Runtime profile is missing the migration marker")
    if len(backups) != 1:
        raise RuntimeError(
            "Persisted legacy Runtime profile must create exactly one migration backup",
        )
    backup = json.loads(backups[0].read_text(encoding="utf-8"))
    if backup.get("runtime", {}).get("engine") != "legacy":
        raise RuntimeError("Runtime migration backup does not preserve the legacy engine")
    return {
        "agent_path": str(agent_path),
        "migration_backup_count": len(backups),
        "migrated_engine": migrated["runtime"]["engine"],
        "migration_marker": _NATIVE_RUNTIME_DEFAULT_MIGRATION,
    }


def _load_helpers():
    spec = importlib.util.spec_from_file_location(
        "source_renderer_helpers",
        HELPERS_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load smoke helpers from {HELPERS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(
    base_url: str,
    process: subprocess.Popen[str],
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Source backend exited early with code {process.returncode}",
            )
        try:
            with urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # readiness retry
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"Source backend health check timed out: {last_error}")


def _terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _build_console_dist() -> None:
    """Build the renderer that this source smoke actually launches.

    The Electron smoke is served from ``console/dist``.  Letting that directory
    lag behind ``console/src`` makes a green unit suite meaningless: the smoke
    would exercise yesterday's renderer while reporting against today's source.
    Always rebuild here so this gate cannot silently validate a stale UI bundle.
    """

    subprocess.run(
        ["npm", "run", "build"],
        cwd=PROJECT_ROOT / "console",
        check=True,
    )


def _renderer_summary(renderer: dict[str, object]) -> dict[str, object]:
    basic = renderer.get("basic_chat_ui_send")
    provider = renderer.get("provider_stream_ui")
    return {
        "basic_chat_ui_send": basic,
        "provider_stream_ui": provider,
    }


def _exercise_initial_model_turn(
    helpers: object,
    state: dict[str, str],
    *,
    timeout: float,
) -> tuple[str, str]:
    """Preserve the backend's HTTP error detail in this release gate."""

    try:
        return helpers._exercise_console_model_success_path(state, timeout=timeout)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Initial persisted-profile model turn returned HTTP {exc.code}: {body}",
        ) from exc


def run(
    timeout: float,
    *,
    keep_workdir: bool = False,
    basic_only: bool = False,
) -> dict[str, object]:
    helpers = _load_helpers()
    _build_console_dist()
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-source-renderer-smoke-"))
    working_dir = smoke_root / "working"
    workspace_dir = working_dir / "workspaces" / "default"
    log_path = smoke_root / "backend.log"
    port = _available_port()
    base_url = f"http://127.0.0.1:{port}"
    desktop_token = "source-renderer-smoke-token"
    process: subprocess.Popen[str] | None = None
    model_context = None
    try:
        legacy_runtime_profile = _write_legacy_runtime_profile_for_smoke(
            working_dir,
        )
        model_context = helpers._LocalOpenAISmokeServer()
        model = model_context.__enter__()
        helpers._write_smoke_openai_provider_config(
            smoke_root,
            base_url=model.base_url,
        )
        env = os.environ.copy()
        source_root = str(PROJECT_ROOT / "src")
        existing_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (source_root, existing_pythonpath) if part
        )
        for key in (
            "LANTAI_RUNTIME_ENGINE",
            "LANTAI_RUNTIME_ENGINE_DEFAULT",
            "LANTAI_DISTRIBUTION_CHANNEL",
            "LANTAI_RELEASE_CHANNEL",
            "LANTAI_APP_STORE",
        ):
            env.pop(key, None)
            env.pop(key.replace("LANTAI_", "COPAW_"), None)
        env.update(
            {
                "LANTAI_WORKING_DIR": str(working_dir),
                "LANTAI_SECRET_DIR": str(smoke_root / "working.secret"),
                "LANTAI_CONSOLE_STATIC_DIR": str(PROJECT_ROOT / "console" / "dist"),
                "LANTAI_DESKTOP_TOKEN": desktop_token,
                "LANTAI_AUTH_ENABLED": "0",
                "LANTAI_DISABLE_KEYRING": "1",
                "LANTAI_SKIP_KEYRING": "1",
                "LANTAI_REMINDER_DELIVERY_WORKER": "0",
            },
        )
        with log_path.open("w", encoding="utf-8") as log_file:
            project_python = PROJECT_ROOT / ".venv" / "bin" / "python"
            runtime_python = str(project_python) if project_python.is_file() else sys.executable
            process = subprocess.Popen(
                [
                    runtime_python,
                    "-m",
                    "lantai",
                    "app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _wait_for_health(base_url, process, timeout=min(timeout, 60))
            state = {"api_base_url": base_url, "desktop_token": desktop_token}
            _session_id, chat_id = _exercise_initial_model_turn(
                helpers,
                state,
                timeout=timeout,
            )
            legacy_runtime_migration = _assert_legacy_runtime_profile_migrated(
                legacy_runtime_profile,
            )
            helpers._assert_runtime_event_stream_reconnect_recovers_terminal(
                state,
                chat_id=chat_id,
                timeout=timeout,
                expected_terminal_type="turn.completed",
                expected_terminal_statuses={"completed", "finished"},
                expected_final_task_statuses={"completed", "finished"},
            )
            basic_ui = helpers._seed_chat_basic_ui_send(
                state,
                workspace_dir=workspace_dir,
                timeout=timeout,
            )
            provider_stream = (
                None
                if basic_only
                else helpers._seed_chat_provider_stream_ui(
                    state,
                    workspace_dir=workspace_dir,
                    timeout=timeout,
                )
            )
            renderer = helpers._run_chat_runtime_stream_renderer_smoke(
                state,
                chat_id=chat_id,
                timeout=timeout,
                basic_ui_send=basic_ui,
                provider_stream_ui=provider_stream,
                expected_terminal_type="turn.completed",
                expected_terminal_statuses={"completed", "finished"},
                project_root=PROJECT_ROOT,
            )
        return {
            "ok": True,
            "runtime_engine": "native",
            "legacy_runtime_migration": legacy_runtime_migration,
            "fake_provider_request_count": model.request_count,
            "renderer": _renderer_summary(renderer),
        }
    except Exception as exc:
        log_tail = ""
        if log_path.exists():
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-5000:]
        raise RuntimeError(
            "Source Chat Runtime renderer smoke failed. "
            f"Backend log tail:\n{log_tail}",
        ) from exc
    finally:
        _terminate(process)
        if model_context is not None:
            model_context.__exit__(None, None, None)
        if keep_workdir:
            print(f"Source smoke root kept at: {smoke_root}", file=sys.stderr)
        else:
            shutil.rmtree(smoke_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=75,
        help="Per-stage timeout in seconds (default: 75).",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Keep the isolated workspace and logs after the smoke run.",
    )
    parser.add_argument(
        "--basic-only",
        action="store_true",
        help=(
            "Run the base stream and two-turn basic chat UI gate without the "
            "long provider queue/route-switch scenarios."
        ),
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.timeout,
                keep_workdir=args.keep_workdir,
                basic_only=args.basic_only,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
