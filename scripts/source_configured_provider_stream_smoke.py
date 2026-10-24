#!/usr/bin/env python3
"""Opt-in, isolated source smoke for a user's configured model provider.

The command never prints copied credentials or model output. It stages the
selected agent profile and encrypted provider store in a temporary directory,
forces the legacy-profile migration path, and verifies that a real model emits
reasoning before answer text over the Console SSE endpoint.
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
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS_PATH = PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
_NATIVE_RUNTIME_DEFAULT_MIGRATION = "native-runtime-default-2026-07-24"
_NATIVE_RUNTIME_DEV_CANARY_MIGRATION = "native-runtime-dev-canary-default-2026-05-31"
_SMOKE_PROMPT = (
    "Reason through this dependency puzzle before answering: task D requires "
    "both B and C; B requires A; C requires A; list the valid execution order "
    "internally. After completing the analysis, reply with exactly "
    "RUNTIME_CONFIGURED_PROVIDER_OK."
)
_FOLLOWUP_SMOKE_PROMPT = (
    "Continue in this same conversation. Reason internally about the reverse "
    "dependency check: if D finished, which prerequisites must already have "
    "finished? After completing the analysis, reply with exactly "
    "RUNTIME_CONFIGURED_PROVIDER_SECOND_OK."
)


def _configured_provider_request_payload(
    turn_id: str,
    *,
    session_id: str | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    client_turn_id = f"configured-provider-smoke-{turn_id}"
    return {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt or _SMOKE_PROMPT}],
            }
        ],
        "session_id": session_id or f"console:configured-provider-smoke-{turn_id}",
        "user_id": "default",
        "channel": "console",
        "stream": True,
        "client_turn_id": client_turn_id,
        # Keep this request byte-for-byte compatible in shape with ChatPage's
        # actual Console request. The router only enables reasoning when this
        # is a top-level options object, not a legacy boolean in meta.
        "deep_thinking": {"enabled": True, "effort": "high"},
        "meta": {"client_turn_id": client_turn_id},
    }

def _load_helpers():
    spec = importlib.util.spec_from_file_location(
        "configured_provider_smoke_helpers",
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


def _wait_for_health(base_url: str, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Source backend exited early with code {process.returncode}")
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


def _stage_profile(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
    agent_id: str,
    target_root: Path,
    active_model_override: dict[str, str] | None = None,
) -> tuple[Path, Path, dict[str, str]]:
    source_config = json.loads(
        (source_working_dir / "config.json").read_text(encoding="utf-8"),
    )
    profiles = source_config.get("agents", {}).get("profiles", {})
    profile = profiles.get(agent_id)
    if not isinstance(profile, dict) or not profile.get("workspace_dir"):
        raise RuntimeError(f"Configured agent profile is unavailable: {agent_id}")

    source_agent_path = Path(str(profile["workspace_dir"])).expanduser() / "agent.json"
    if not source_agent_path.is_file():
        raise RuntimeError(f"Configured agent file is unavailable: {source_agent_path}")
    if not source_secret_dir.is_dir():
        raise RuntimeError(f"Configured secret directory is unavailable: {source_secret_dir}")

    working_dir = target_root / "working"
    workspace_dir = working_dir / "workspaces" / "default"
    secret_dir = target_root / "working.secret"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    agent = json.loads(source_agent_path.read_text(encoding="utf-8"))
    active_model = agent.get("active_model")
    if not isinstance(active_model, dict) or not active_model.get("provider_id"):
        active_model_path = source_secret_dir / "providers" / "active_model.json"
        if active_model_path.is_file():
            try:
                active_model = json.loads(
                    active_model_path.read_text(encoding="utf-8"),
                )
            except (OSError, ValueError):
                active_model = None
        if isinstance(active_model, dict) and active_model.get("provider_id"):
            agent["active_model"] = active_model
    if active_model_override is not None:
        provider_id = str(active_model_override.get("provider_id") or "").strip()
        model = str(active_model_override.get("model") or "").strip()
        if not provider_id or not model:
            raise ValueError(
                "Configured-provider route override requires provider_id and model",
            )
        active_model = {"provider_id": provider_id, "model": model}
        agent["active_model"] = active_model
    if not isinstance(active_model, dict) or not active_model.get("provider_id"):
        raise RuntimeError("Configured agent has no active model")
    agent["id"] = "default"
    agent["workspace_dir"] = str(workspace_dir)
    agent["runtime"] = {"engine": "legacy"}
    migrations = agent.get("runtime_migrations")
    if isinstance(migrations, list):
        agent["runtime_migrations"] = [
            value
            for value in migrations
            if value
            not in {
                _NATIVE_RUNTIME_DEFAULT_MIGRATION,
                _NATIVE_RUNTIME_DEV_CANARY_MIGRATION,
            }
        ]
    agent_path = workspace_dir / "agent.json"
    agent_path.write_text(json.dumps(agent, ensure_ascii=False, indent=2), encoding="utf-8")
    (working_dir / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "active_agent": "default",
                    "profiles": {
                        "default": {"id": "default", "workspace_dir": str(workspace_dir)},
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    for name in (".master_key", "providers"):
        source = source_secret_dir / name
        target = secret_dir / name
        if not source.exists():
            raise RuntimeError(f"Configured secret store is missing {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    if active_model_override is not None:
        staged_active_path = secret_dir / "providers" / "active_model.json"
        staged_active_path.write_text(
            json.dumps(active_model, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(staged_active_path, 0o600)
        except OSError:
            pass
    return working_dir, secret_dir, {
        "provider_id": str(active_model.get("provider_id")),
        "model": str(active_model.get("model") or ""),
        "agent_path": str(agent_path),
    }


def _assert_profile_migrated(agent_path: Path) -> dict[str, Any]:
    agent = json.loads(agent_path.read_text(encoding="utf-8"))
    backups = sorted(agent_path.parent.glob("agent.*.native-runtime-default.bak"))
    if agent.get("runtime", {}).get("engine") != "native":
        raise AssertionError("Configured-provider smoke did not migrate runtime to native")
    if _NATIVE_RUNTIME_DEFAULT_MIGRATION not in agent.get("runtime_migrations", []):
        raise AssertionError("Configured-provider smoke is missing runtime migration marker")
    if len(backups) != 1:
        raise AssertionError("Configured-provider smoke expected exactly one migration backup")
    return {"migration_backup_count": len(backups), "migrated_engine": "native"}


def _configured_model_evidence(configured_model: dict[str, Any]) -> dict[str, str]:
    """Return only the non-sensitive model identity suitable for saved evidence."""

    return {
        "provider_id": str(configured_model.get("provider_id") or ""),
        "model": str(configured_model.get("model") or ""),
    }


def _read_console_sse(
    *,
    helpers: object,
    base_url: str,
    desktop_token: str,
    timeout: float,
    session_id: str,
    prompt: str,
) -> list[tuple[int, dict[str, Any]]]:
    turn_id = uuid.uuid4().hex[:12]
    payload = _configured_provider_request_payload(
        turn_id,
        session_id=session_id,
        prompt=prompt,
    )
    request = Request(
        f"{base_url}/api/console/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **helpers._state_headers({"desktop_token": desktop_token})},
        method="POST",
    )
    started = time.monotonic()
    frames: list[tuple[int, dict[str, Any]]] = []
    data_lines: list[str] = []
    try:
        with urlopen(request, timeout=timeout) as response:
            while True:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text:
                    if text.startswith("data:"):
                        data_lines.append(text[5:].lstrip())
                    continue
                if data_lines:
                    frame = helpers._parse_sse_data_lines(data_lines, source="configured provider")
                    frames.append((round((time.monotonic() - started) * 1000), frame))
                    data_lines = []
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Configured provider returned HTTP {exc.code}: {detail}") from exc
    return frames


def _safe_frame_trace(frames: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Summarize SSE ordering without retaining model output or raw IDs."""
    slots: dict[str, str] = {}

    def slot(prefix: str, value: object) -> str:
        key = str(value or "").strip()
        if not key:
            return ""
        lookup = f"{prefix}:{key}"
        if lookup not in slots:
            slots[lookup] = f"{prefix}{len(slots) + 1}"
        return slots[lookup]

    trace: list[dict[str, Any]] = []
    for elapsed_ms, frame in frames:
        output = frame.get("output")
        trace.append(
            {
                "elapsed_ms": elapsed_ms,
                "object": str(frame.get("object") or ""),
                "type": str(frame.get("type") or ""),
                "status": str(frame.get("status") or ""),
                "id": slot("id", frame.get("id")),
                "msg_id": slot("id", frame.get("msg_id")),
                "delta": frame.get("delta") is True,
                "text_length": len(str(frame.get("text") or frame.get("thinking") or "")),
                "output": [
                    {
                        "id": slot("id", item.get("id")),
                        "type": str(item.get("type") or ""),
                        "status": str(item.get("status") or ""),
                    }
                    for item in output
                    if isinstance(item, dict)
                ]
                if isinstance(output, list)
                else [],
            }
        )
    return trace


def run(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
    agent_id: str,
    timeout: float,
    provider_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    if (provider_id is None) != (model is None):
        raise ValueError("Provider route override requires both provider_id and model")
    helpers = _load_helpers()
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-configured-provider-smoke-"))
    process: subprocess.Popen[str] | None = None
    log_path = smoke_root / "backend.log"
    try:
        working_dir, secret_dir, configured_model = _stage_profile(
            source_working_dir=source_working_dir,
            source_secret_dir=source_secret_dir,
            agent_id=agent_id,
            target_root=smoke_root,
            active_model_override=(
                {"provider_id": provider_id, "model": model}
                if provider_id is not None and model is not None
                else None
            ),
        )
        port = _available_port()
        base_url = f"http://127.0.0.1:{port}"
        desktop_token = "configured-provider-smoke-token"
        env = os.environ.copy()
        source_root = str(PROJECT_ROOT / "src")
        existing_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (source_root, existing_pythonpath) if part
        )
        for key in ("LANTAI_RUNTIME_ENGINE", "LANTAI_RUNTIME_ENGINE_DEFAULT"):
            env.pop(key, None)
            env.pop(key.replace("LANTAI_", "COPAW_"), None)
        env.update(
            {
                "LANTAI_WORKING_DIR": str(working_dir),
                "LANTAI_SECRET_DIR": str(secret_dir),
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
                [runtime_python, "-m", "lantai", "app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _wait_for_health(base_url, process, timeout=min(timeout, 60))
            session_id = f"console:configured-provider-smoke-{uuid.uuid4().hex[:12]}"
            frame_sets = [
                _read_console_sse(
                    helpers=helpers,
                    base_url=base_url,
                    desktop_token=desktop_token,
                    timeout=timeout,
                    session_id=session_id,
                    prompt=prompt,
                )
                for prompt in (_SMOKE_PROMPT, _FOLLOWUP_SMOKE_PROMPT)
            ]
        turn_evidence: list[dict[str, Any]] = []
        for turn_number, frames in enumerate(frame_sets, start=1):
            if not frames:
                raise AssertionError(
                    f"Configured provider returned no SSE frames for turn {turn_number}"
                )
            first_reasoning = next(
                (at for at, frame in frames if frame.get("type") == "reasoning"),
                None,
            )
            first_answer = next(
                (at for at, frame in frames if frame.get("type") == "message"),
                None,
            )
            final = next(
                (
                    at
                    for at, frame in reversed(frames)
                    if frame.get("object") == "response"
                    and frame.get("status") == "completed"
                ),
                None,
            )
            if first_reasoning is None or first_answer is None or final is None:
                raise AssertionError(
                    "Configured provider did not emit reasoning, answer, and "
                    f"completion SSE frames for turn {turn_number}"
                )
            if not first_reasoning < first_answer < final:
                raise AssertionError(
                    "Configured provider did not stream reasoning before answer "
                    f"and completion for turn {turn_number}"
                )
            turn_evidence.append(
                {
                    "turn": turn_number,
                    "frame_types": sorted(
                        {str(frame.get("type") or "") for _, frame in frames}
                    ),
                    "frame_count": len(frames),
                    "reasoning_visible_ms": first_reasoning,
                    "answer_visible_ms": first_answer,
                    "final_visible_ms": final,
                    "frame_trace": _safe_frame_trace(frames),
                }
            )
        migration = _assert_profile_migrated(Path(configured_model["agent_path"]))
        return {
            "ok": True,
            "configured_model": _configured_model_evidence(configured_model),
            "migration": migration,
            "same_conversation": True,
            "turn_count": len(turn_evidence),
            "turns": turn_evidence,
        }
    finally:
        _terminate(process)
        shutil.rmtree(smoke_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-working-dir", type=Path, required=True)
    parser.add_argument("--source-secret-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--provider-id")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args()
    evidence_output = args.evidence_output
    result = run(
        source_working_dir=args.source_working_dir,
        source_secret_dir=args.source_secret_dir,
        agent_id=args.agent_id,
        timeout=args.timeout,
        provider_id=args.provider_id,
        model=args.model,
    )
    if evidence_output is not None:
        evidence_output.parent.mkdir(parents=True, exist_ok=True)
        evidence_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
