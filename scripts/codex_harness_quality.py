#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run Lantai's read-only quality missions through the Codex Agents API.

The application process keeps the broad OPENAI_API_KEY. Only the restricted
CODEX_API_KEY is passed to ``codex exec-server`` in the self-hosted workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    REPO_ROOT / "contracts" / "codex-harness-quality" / "mission-manifest.v1.json"
)
DEFAULT_API_BASE = "https://api.openai.com/v1"
BETA_HEADER = "agents=v1"
SCHEMA_VERSION = 1
TERMINAL_EVENTS = {
    "agent.session.turn.completed",
    "agent.session.turn.failed",
    "agent.session.turn.cancelled",
}
LIFECYCLE_FAILURES = {
    "error",
    "agent.session.failed",
    "agent.session.environment.failed",
}
CONNECTED_EVENT = "agent.session.environment.connected"
ALLOWED_PYTHON_EXECUTABLES = {"python3", ".venv/bin/python"}
SAFE_PYTEST_ARGUMENTS = {"-p", "no:cacheprovider", "-q"}
APPROVED_ARTIFACT_ROOT = ".lantai/codex-harness"
APPROVED_ENTRYPOINTS = {
    "scripts/check_foundation_experience_gate.py",
    "scripts/check_legacy_intelligence_assembly_baseline.py",
    "scripts/runtime_evaluator_preflight.py",
    "module:pytest",
}
APPROVED_SCRIPT_ARGUMENTS = {
    "scripts/check_foundation_experience_gate.py": set(),
    "scripts/check_legacy_intelligence_assembly_baseline.py": set(),
    "scripts/runtime_evaluator_preflight.py": {"--json"},
}


class HarnessError(RuntimeError):
    """Raised for a fail-closed Harness configuration or lifecycle error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HarnessError(f"Manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HarnessError(f"Invalid manifest JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError("Manifest root must be a JSON object")
    return value


def _safe_repo_relative(value: str, *, label: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise HarnessError(f"{label} must be a safe repository-relative path: {value!r}")


def validate_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _load_json(path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise HarnessError(f"Unsupported manifest schema: {manifest.get('schema_version')!r}")

    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        raise HarnessError("Manifest policy must be an object")
    if policy.get("workspace_mode") != "read_only":
        raise HarnessError("Harness quality missions must use workspace_mode=read_only")
    if policy.get("network_access") != "disabled":
        raise HarnessError("Harness quality missions must use network_access=disabled")
    if policy.get("forbid_repo_mutation") is not True:
        raise HarnessError("Harness quality missions must forbid repository mutation")
    artifact_root = policy.get("artifact_root")
    if not isinstance(artifact_root, str) or not artifact_root:
        raise HarnessError("policy.artifact_root must be a non-empty string")
    _safe_repo_relative(artifact_root, label="policy.artifact_root")
    if artifact_root != APPROVED_ARTIFACT_ROOT:
        raise HarnessError(
            f"policy.artifact_root must remain {APPROVED_ARTIFACT_ROOT!r}"
        )

    allowed_entrypoints = policy.get("allowed_entrypoints")
    if not isinstance(allowed_entrypoints, list) or not allowed_entrypoints:
        raise HarnessError("policy.allowed_entrypoints must be a non-empty list")
    if any(not isinstance(value, str) or not value for value in allowed_entrypoints):
        raise HarnessError("Every allowed entrypoint must be a non-empty string")
    allowed_entrypoint_set = set(allowed_entrypoints)
    unapproved_entrypoints = allowed_entrypoint_set - APPROVED_ENTRYPOINTS
    if unapproved_entrypoints:
        raise HarnessError(
            "Manifest contains unapproved entrypoints: "
            + ", ".join(sorted(unapproved_entrypoints))
        )

    forbidden_paths = policy.get("forbidden_paths")
    if not isinstance(forbidden_paths, list) or not forbidden_paths:
        raise HarnessError("policy.forbidden_paths must be a non-empty list")
    for value in forbidden_paths:
        if not isinstance(value, str) or not value:
            raise HarnessError("Every forbidden path must be a non-empty string")
        _safe_repo_relative(value, label="forbidden path")

    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise HarnessError("Manifest profiles must be a non-empty object")
    for profile_id, profile in profiles.items():
        if not isinstance(profile_id, str) or not profile_id:
            raise HarnessError("Profile IDs must be non-empty strings")
        if not isinstance(profile, dict):
            raise HarnessError(f"Profile {profile_id!r} must be an object")
        timeout = profile.get("timeout_seconds")
        if not isinstance(timeout, int) or not 60 <= timeout <= 14_400:
            raise HarnessError(f"Profile {profile_id!r} timeout must be 60..14400 seconds")
        steps = profile.get("steps")
        if not isinstance(steps, list) or not steps:
            raise HarnessError(f"Profile {profile_id!r} must contain steps")
        seen_steps: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                raise HarnessError(f"Profile {profile_id!r} contains a non-object step")
            step_id = step.get("id")
            if not isinstance(step_id, str) or not step_id or step_id in seen_steps:
                raise HarnessError(f"Profile {profile_id!r} has an invalid/duplicate step ID")
            seen_steps.add(step_id)
            command = step.get("command")
            if not isinstance(command, list) or not command:
                raise HarnessError(f"Step {step_id!r} command must be a non-empty argv list")
            if any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command):
                raise HarnessError(f"Step {step_id!r} command contains an invalid argument")
            if command[0] not in ALLOWED_PYTHON_EXECUTABLES or len(command) < 2:
                raise HarnessError(
                    f"Step {step_id!r} must use an approved Python executable and entrypoint"
                )
            if command[1] == "-m":
                if len(command) < 3:
                    raise HarnessError(f"Step {step_id!r} is missing its Python module")
                entrypoint = f"module:{command[2]}"
                if entrypoint == "module:pytest":
                    for arg in command[3:]:
                        if arg in SAFE_PYTEST_ARGUMENTS:
                            continue
                        _safe_repo_relative(arg, label=f"Step {step_id!r} pytest target")
                        if not arg.startswith("tests/"):
                            raise HarnessError(
                                f"Step {step_id!r} pytest target must stay under tests/"
                            )
            else:
                entrypoint = command[1]
                _safe_repo_relative(entrypoint, label=f"Step {step_id!r} entrypoint")
            if entrypoint not in allowed_entrypoint_set:
                raise HarnessError(
                    f"Step {step_id!r} entrypoint is not allowed: {entrypoint!r}"
                )
            if command[1] != "-m":
                approved_arguments = APPROVED_SCRIPT_ARGUMENTS.get(entrypoint)
                if approved_arguments is None or any(
                    arg not in approved_arguments for arg in command[2:]
                ):
                    raise HarnessError(
                        f"Step {step_id!r} contains unapproved script arguments"
                    )
    return manifest


def get_profile(manifest: Mapping[str, Any], profile_id: str) -> Mapping[str, Any]:
    profiles = manifest["profiles"]
    if profile_id not in profiles:
        choices = ", ".join(sorted(profiles))
        raise HarnessError(f"Unknown profile {profile_id!r}; choose one of: {choices}")
    return profiles[profile_id]


def build_mission_prompt(manifest: Mapping[str, Any], profile_id: str) -> str:
    profile = get_profile(manifest, profile_id)
    policy = manifest["policy"]
    forbidden = "\n".join(f"- {path}" for path in policy["forbidden_paths"])
    steps = "\n".join(
        f"{index}. {step['id']}: {json.dumps(step['command'], ensure_ascii=False)}"
        for index, step in enumerate(profile["steps"], start=1)
    )
    return f"""You are Lantai's read-only stability and functionality auditor.

Mission: {profile_id}
{profile['description']}

Hard safety policy:
- Work only inside the supplied Lantai repository.
- Investigation is read-only. Do not create, edit, delete, move, install, commit,
  reset, checkout, stash, or push anything in the repository.
- Do not read user credentials, configured provider stores, normal desktop user
  data, or unrelated files outside the repository.
- Do not access the network from mission commands. The host's executor control
  channel is the only network activity allowed during this mission.
- Never touch these protected implementation areas:
{forbidden}
- Use argv directly; do not combine steps into a shell pipeline.
- Run `git status --porcelain=v1` before and after the mission. If the snapshots
  differ, stop and report a critical repository-mutation finding.
- Report actual commands, exit codes, and observed evidence. Never claim a pass
  for a skipped, timed-out, or inferred result.
- On a failure, diagnose the narrowest likely cause and propose a behavior lock;
  do not implement a fix in this mission.

Execute these exact commands in order from the repository root:
{steps}

Finish with exactly one JSON object between `LANTAI_REPORT_BEGIN` and
`LANTAI_REPORT_END`. The object must contain: schema_version (1), profile,
conclusion (pass|needs_revision|blocked), repo_unchanged (boolean), steps (array
of id/command/exit_code/conclusion/evidence), findings (array of
severity/code/title/evidence/recommended_behavior_lock), and summary.
"""


def _git_status(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise HarnessError(f"Unable to inspect Git state: {result.stderr.strip()}")
    return result.stdout


def _artifact_root_is_ignored(repo_root: Path, artifact_root: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", f"{artifact_root}/.probe"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _find_codex() -> str | None:
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    app_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    return str(app_binary) if app_binary.is_file() else None


def _supports_harness_exec_server(codex_path: str | None) -> bool:
    if codex_path is None:
        return False
    try:
        result = subprocess.run(
            [codex_path, "exec-server", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    required_flags = {"--remote", "--environment-id", "--strict-config"}
    return result.returncode == 0 and all(flag in result.stdout for flag in required_flags)


def collect_preflight(
    manifest: Mapping[str, Any],
    profile_id: str,
    *,
    repo_root: Path = REPO_ROOT,
    live: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    profile = get_profile(manifest, profile_id)
    codex_path = _find_codex()
    git_status = _git_status(repo_root)
    checks: list[dict[str, Any]] = [
        {"id": "repository", "ok": (repo_root / ".git").exists(), "required": True},
        {"id": "codex-cli", "ok": codex_path is not None, "required": True},
        {
            "id": "codex-exec-server",
            "ok": _supports_harness_exec_server(codex_path),
            "required": True,
        },
        {
            "id": "artifact-root-ignored",
            "ok": _artifact_root_is_ignored(
                repo_root, manifest["policy"]["artifact_root"]
            ),
            "required": True,
        },
        {"id": "clean-git", "ok": not bool(git_status), "required": False},
    ]
    for step in profile["steps"]:
        executable = step["command"][0]
        if "/" in executable:
            exists = (repo_root / executable).is_file()
        else:
            exists = shutil.which(executable) is not None
        checks.append(
            {
                "id": f"step-executable:{step['id']}",
                "ok": exists,
                "required": True,
                "value": executable,
            }
        )
    if live:
        checks.extend(
            [
                {
                    "id": "OPENAI_API_KEY",
                    "ok": bool(env.get("OPENAI_API_KEY")),
                    "required": True,
                },
                {
                    "id": "CODEX_API_KEY",
                    "ok": bool(env.get("CODEX_API_KEY")),
                    "required": True,
                },
            ]
        )
    return {
        "schema_version": 1,
        "profile": profile_id,
        "mode": "live" if live else "offline",
        "ok": all(item["ok"] for item in checks if item["required"]),
        "warnings": [item["id"] for item in checks if not item["required"] and not item["ok"]],
        "checks": checks,
        "codex_path": codex_path,
    }


def _headers(api_key: str, *, accept_sse: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "OpenAI-Beta": BETA_HEADER,
        "User-Agent": "lantai-codex-harness-quality/1",
    }
    if accept_sse:
        headers["Accept"] = "text/event-stream"
    return headers


def _http_error_detail(exc: HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    return f"HTTP {exc.code}: {body[:1000]}" if body else f"HTTP {exc.code}"


def _json_request(
    method: str,
    url: str,
    api_key: str,
    payload: Mapping[str, Any] | None = None,
    *,
    timeout: float = 60,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers=_headers(api_key), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raise HarnessError(_http_error_detail(exc)) from exc
    except URLError as exc:
        raise HarnessError(f"Agents API request failed: {exc.reason}") from exc
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessError("Agents API returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise HarnessError("Agents API returned a non-object response")
    return value


def iter_sse_json(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines.clear()
                if payload != "[DONE]":
                    value = json.loads(payload)
                    if isinstance(value, dict):
                        yield value
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        payload = "\n".join(data_lines)
        if payload != "[DONE]":
            value = json.loads(payload)
            if isinstance(value, dict):
                yield value


def _is_root_turn(event: Mapping[str, Any]) -> bool:
    turn = event.get("turn")
    return isinstance(turn, dict) and turn.get("subagent_id") is None


def _event_failure(event: Mapping[str, Any]) -> str | None:
    event_type = event.get("type")
    if event_type not in LIFECYCLE_FAILURES and not (
        event_type in {"agent.session.turn.failed", "agent.session.turn.cancelled"}
        and _is_root_turn(event)
    ):
        return None
    error = event.get("error")
    if not isinstance(error, dict):
        turn = event.get("turn")
        error = turn.get("error") if isinstance(turn, dict) else None
    detail = error.get("message") if isinstance(error, dict) else ""
    return f"{event_type}: {detail}".rstrip()


@dataclass
class StreamState:
    opened: threading.Event = field(default_factory=threading.Event)
    connected: threading.Event = field(default_factory=threading.Event)
    terminal: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    errors: queue.Queue[str] = field(default_factory=queue.Queue)
    terminal_event: dict[str, Any] | None = None


def _stream_events(
    url: str,
    api_key: str,
    output_path: Path,
    state: StreamState,
    read_timeout: float,
) -> None:
    request = Request(url, headers=_headers(api_key, accept_sse=True), method="GET")
    try:
        with urlopen(request, timeout=read_timeout) as response, output_path.open(
            "a", encoding="utf-8"
        ) as output:
            state.opened.set()
            for event in iter_sse_json(response):
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
                output.flush()
                event_type = event.get("type")
                if event_type == CONNECTED_EVENT:
                    state.connected.set()
                failure = _event_failure(event)
                if failure:
                    state.errors.put(failure)
                    state.terminal_event = event
                    state.terminal.set()
                    return
                if event_type in TERMINAL_EVENTS and _is_root_turn(event):
                    state.terminal_event = event
                    state.terminal.set()
                    return
                if state.stop.is_set():
                    return
    except HTTPError as exc:
        state.errors.put(_http_error_detail(exc))
    except Exception as exc:
        state.errors.put(f"Event stream failed: {exc}")
    finally:
        state.opened.set()
        if not state.terminal.is_set() and not state.stop.is_set():
            state.errors.put("Event stream closed before the root turn ended")
            state.terminal.set()


def _wait_or_error(event: threading.Event, state: StreamState, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not state.errors.empty():
            raise HarnessError(state.errors.get_nowait())
        if event.wait(min(0.2, max(0.0, deadline - time.monotonic()))):
            if not state.errors.empty():
                raise HarnessError(state.errors.get_nowait())
            return
    if not state.errors.empty():
        raise HarnessError(state.errors.get_nowait())
    raise HarnessError(f"Timed out waiting for {label}")


def _executor_command(codex_path: str, remote_url: str, environment_id: str) -> list[str]:
    return [
        codex_path,
        "exec-server",
        "--config",
        'sandbox_mode="read-only"',
        "--config",
        'approval_policy="never"',
        "--strict-config",
        "--remote",
        remote_url,
        "--environment-id",
        environment_id,
        "--exit-on-stdin-close",
    ]


def _executor_env(environ: Mapping[str, str], codex_api_key: str) -> dict[str, str]:
    safe_names = {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "SHELL", "TMPDIR"}
    child_env = {name: environ[name] for name in safe_names if environ.get(name)}
    child_env["CODEX_API_KEY"] = codex_api_key
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["LANTAI_METADATA_ONLY"] = "1"
    return child_env


def _load_agent_report(
    events_path: Path,
    profile_id: str,
    expected_steps: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    completed_text: list[str] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarnessError("Stored Agents API event is not valid JSON") from exc
        if event.get("type") == "agent.session.turn.output_text.done":
            text = event.get("text")
            if isinstance(text, str):
                completed_text.append(text)
    output = "\n".join(completed_text)
    begin = "LANTAI_REPORT_BEGIN"
    end = "LANTAI_REPORT_END"
    start_index = output.rfind(begin)
    end_index = output.find(end, start_index + len(begin)) if start_index >= 0 else -1
    if start_index < 0 or end_index < 0:
        raise HarnessError("Completed agent turn did not contain the required Lantai report")
    raw_report = output[start_index + len(begin) : end_index].strip()
    if raw_report.startswith("```json") and raw_report.endswith("```"):
        raw_report = raw_report[7:-3].strip()
    elif raw_report.startswith("```") and raw_report.endswith("```"):
        raw_report = raw_report[3:-3].strip()
    try:
        report = json.loads(raw_report)
    except json.JSONDecodeError as exc:
        raise HarnessError("Lantai agent report is not valid JSON") from exc
    if not isinstance(report, dict):
        raise HarnessError("Lantai agent report must be a JSON object")
    if report.get("schema_version") != 1 or report.get("profile") != profile_id:
        raise HarnessError("Lantai agent report schema/profile does not match the mission")
    if report.get("conclusion") not in {"pass", "needs_revision", "blocked"}:
        raise HarnessError("Lantai agent report has an invalid conclusion")
    if report.get("repo_unchanged") is not True:
        raise HarnessError("Lantai agent report did not affirm an unchanged repository")
    steps = report.get("steps")
    expected = list(expected_steps)
    if not isinstance(steps, list) or len(steps) != len(expected):
        raise HarnessError("Lantai agent report is missing required step results")
    for actual, planned in zip(steps, expected):
        if not isinstance(actual, dict):
            raise HarnessError("Lantai agent report contains an invalid step result")
        if actual.get("id") != planned["id"] or actual.get("command") != planned["command"]:
            raise HarnessError("Lantai agent report command evidence does not match the mission")
        if not isinstance(actual.get("exit_code"), int):
            raise HarnessError("Lantai agent report step is missing an integer exit code")
        if actual.get("conclusion") not in {"pass", "needs_revision", "blocked"}:
            raise HarnessError("Lantai agent report step has an invalid conclusion")
        if not isinstance(actual.get("evidence"), str) or not actual["evidence"].strip():
            raise HarnessError("Lantai agent report step is missing observed evidence")
        if (actual["exit_code"] == 0) != (actual["conclusion"] == "pass"):
            raise HarnessError(
                "Lantai agent report step exit code and conclusion do not agree"
            )
    if report["conclusion"] == "pass" and any(
        step["exit_code"] != 0 or step["conclusion"] != "pass" for step in steps
    ):
        raise HarnessError("Lantai agent report claimed pass despite a non-passing step")
    findings = report.get("findings")
    summary = report.get("summary")
    if not isinstance(findings, list) or not isinstance(summary, str) or not summary.strip():
        raise HarnessError("Lantai agent report is missing findings/summary")
    if report["conclusion"] != "pass" and not findings:
        raise HarnessError("A non-passing Lantai agent report must contain a finding")
    required_finding_text = {
        "code",
        "title",
        "evidence",
        "recommended_behavior_lock",
    }
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("severity") not in {
            "critical",
            "major",
            "minor",
        }:
            raise HarnessError("Lantai agent report contains an invalid finding")
        if any(
            not isinstance(finding.get(key), str) or not finding[key].strip()
            for key in required_finding_text
        ):
            raise HarnessError("Lantai agent report finding is missing required evidence")
    return report


def _validate_api_base(api_base: str) -> str:
    normalized = api_base.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise HarnessError("Agents API base must be an HTTPS URL without embedded credentials")
    return normalized


def run_mission(
    manifest: Mapping[str, Any],
    profile_id: str,
    *,
    model: str,
    api_base: str,
    repo_root: Path = REPO_ROOT,
    environ: Mapping[str, str] | None = None,
    delete_session: bool = True,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    preflight = collect_preflight(
        manifest, profile_id, repo_root=repo_root, live=True, environ=env
    )
    if not preflight["ok"]:
        failed = [
            item["id"]
            for item in preflight["checks"]
            if item["required"] and not item["ok"]
        ]
        raise HarnessError(f"Live preflight failed: {', '.join(failed)}")
    if preflight["warnings"]:
        raise HarnessError("Refusing live Harness run from a dirty Git worktree")
    api_base = _validate_api_base(api_base)

    profile = get_profile(manifest, profile_id)
    prompt = build_mission_prompt(manifest, profile_id)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    artifact_root = (repo_root / manifest["policy"]["artifact_root"]).resolve()
    if repo_root.resolve() not in artifact_root.parents:
        raise HarnessError("Artifact root escaped the repository")
    run_dir = artifact_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (run_dir / "preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    app_key = env["OPENAI_API_KEY"]
    codex_key = env["CODEX_API_KEY"]
    session: dict[str, Any] | None = None
    executor: subprocess.Popen[str] | None = None
    executor_stdout = None
    executor_stderr = None
    state = StreamState()
    thread: threading.Thread | None = None
    before_status = _git_status(repo_root)
    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "profile": profile_id,
        "model": model,
        "started_at": _utc_now(),
        "conclusion": "failed",
        "session_deleted": False,
    }
    try:
        session = _json_request(
            "POST",
            f"{api_base}/agents/sessions",
            app_key,
            {
                "agent": {
                    "model": model,
                    "instructions": (
                        "Execute the supplied Lantai QA mission exactly and fail closed."
                    ),
                },
                "environment": {
                    "type": "self_hosted",
                    "workspace_directory": str(repo_root),
                },
            },
        )
        session_id = session.get("id")
        environment = session.get("environment")
        if not isinstance(session_id, str) or not isinstance(environment, dict):
            raise HarnessError("Agents API session response is missing id/environment")
        environment_id = environment.get("id")
        remote_url = environment.get("remote_url")
        if not isinstance(environment_id, str) or not isinstance(remote_url, str):
            raise HarnessError("Self-hosted environment is missing id/remote_url")
        result["session_id"] = session_id

        events_url = f"{api_base}/agents/sessions/{quote(session_id, safe='')}/events?stream=true"
        thread = threading.Thread(
            target=_stream_events,
            args=(
                events_url,
                app_key,
                run_dir / "events.jsonl",
                state,
                float(profile["timeout_seconds"] + 120),
            ),
            name="codex-harness-events",
            daemon=True,
        )
        thread.start()
        _wait_or_error(state.opened, state, 30, "the event stream")

        executor_stdout = (run_dir / "executor.stdout.log").open("w", encoding="utf-8")
        executor_stderr = (run_dir / "executor.stderr.log").open("w", encoding="utf-8")
        executor = subprocess.Popen(
            _executor_command(preflight["codex_path"], remote_url, environment_id),
            cwd=repo_root,
            env=_executor_env(env, codex_key),
            stdin=subprocess.PIPE,
            stdout=executor_stdout,
            stderr=executor_stderr,
            text=True,
        )
        _wait_or_error(state.connected, state, 60, "the self-hosted executor connection")
        _json_request(
            "POST",
            f"{api_base}/agents/sessions/{quote(session_id, safe='')}/events",
            app_key,
            {
                "events": [
                    {
                        "type": "agent.session.input.message",
                        "input": [
                            {
                                "role": "user",
                                "content": [{"type": "input_text", "text": prompt}],
                            }
                        ],
                    }
                ]
            },
        )
        _wait_or_error(
            state.terminal,
            state,
            float(profile["timeout_seconds"]),
            "the root agent turn",
        )
        after_status = _git_status(repo_root)
        if after_status != before_status:
            raise HarnessError("Harness mission changed the repository worktree")
        terminal_type = (state.terminal_event or {}).get("type")
        result["terminal_event"] = terminal_type
        if terminal_type != "agent.session.turn.completed":
            raise HarnessError(f"Root agent turn ended without completion: {terminal_type}")
        agent_report = _load_agent_report(
            run_dir / "events.jsonl", profile_id, profile["steps"]
        )
        (run_dir / "agent-report.json").write_text(
            json.dumps(agent_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result["conclusion"] = agent_report["conclusion"]
    except Exception as exc:
        result["error"] = str(exc)
        raise
    finally:
        state.stop.set()
        if executor is not None:
            if executor.stdin:
                executor.stdin.close()
            try:
                executor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                executor.terminate()
                try:
                    executor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    executor.kill()
                    executor.wait(timeout=5)
            result["executor_returncode"] = executor.returncode
        if executor_stdout is not None:
            executor_stdout.close()
        if executor_stderr is not None:
            executor_stderr.close()
        if thread is not None:
            thread.join(timeout=5)
        if delete_session and session and isinstance(session.get("id"), str):
            try:
                _json_request(
                    "DELETE",
                    f"{api_base}/agents/sessions/{quote(session['id'], safe='')}",
                    app_key,
                )
                result["session_deleted"] = True
            except Exception as exc:
                result["session_delete_error"] = str(exc)
        result["finished_at"] = _utc_now()
        (run_dir / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    result["artifact_dir"] = str(run_dir)
    return result


def _print(value: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    for key, item in value.items():
        print(f"{key}: {item}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, inspect, or run Lantai Codex Harness QA missions."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate the mission contract only")
    validate.add_argument("--json", action="store_true")

    plan = subparsers.add_parser("plan", help="Render the exact mission prompt")
    plan.add_argument("--profile", default="source-runtime")
    plan.add_argument("--json", action="store_true")

    preflight = subparsers.add_parser("preflight", help="Inspect local execution readiness")
    preflight.add_argument("--profile", default="source-runtime")
    preflight.add_argument("--live", action="store_true", help="Also require both API keys")
    preflight.add_argument("--json", action="store_true")

    run = subparsers.add_parser("run", help="Start a live self-hosted Agents API mission")
    run.add_argument("--profile", default="source-runtime")
    run.add_argument("--model", default="gpt-6-astra")
    run.add_argument("--api-base", default=DEFAULT_API_BASE)
    run.add_argument(
        "--acknowledge-beta-data-policy",
        action="store_true",
        help="Required acknowledgement of current beta residency/retention limitations",
    )
    run.add_argument(
        "--keep-session",
        action="store_true",
        help="Retain the remote session after completion instead of deleting it",
    )
    run.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        manifest = validate_manifest(args.manifest.expanduser().resolve())
        if args.command == "validate":
            result = {
                "schema_version": 1,
                "ok": True,
                "manifest_id": manifest["id"],
                "profiles": sorted(manifest["profiles"]),
            }
            _print(result, args.json)
            return 0
        if args.command == "plan":
            prompt = build_mission_prompt(manifest, args.profile)
            if args.json:
                print(
                    json.dumps(
                        {"profile": args.profile, "prompt": prompt},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(prompt)
            return 0
        if args.command == "preflight":
            result = collect_preflight(manifest, args.profile, live=args.live)
            _print(result, args.json)
            return 0 if result["ok"] else 2
        if not args.acknowledge_beta_data_policy:
            raise HarnessError(
                "Live run requires --acknowledge-beta-data-policy; the public beta "
                "currently has residency/retention limitations"
            )
        result = run_mission(
            manifest,
            args.profile,
            model=args.model,
            api_base=args.api_base,
            delete_session=not args.keep_session,
        )
        _print(result, args.json)
        return 0 if result["conclusion"] == "pass" else 1
    except HarnessError as exc:
        print(f"Codex Harness quality error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"Codex Harness quality error: unexpected {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
