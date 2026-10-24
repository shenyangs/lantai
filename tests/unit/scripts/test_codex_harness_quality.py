# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_harness_quality.py"
MANIFEST_PATH = (
    REPO_ROOT / "contracts" / "codex-harness-quality" / "mission-manifest.v1.json"
)

SPEC = importlib.util.spec_from_file_location("codex_harness_quality", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_checked_in_manifest_is_valid_and_read_only() -> None:
    manifest = MODULE.validate_manifest(MANIFEST_PATH)

    assert manifest["policy"]["workspace_mode"] == "read_only"
    assert manifest["policy"]["network_access"] == "disabled"
    assert manifest["policy"]["forbid_repo_mutation"] is True
    assert manifest["policy"]["forbidden_paths"]
    assert list(manifest["profiles"]) == ["source-runtime"]
    assert "scripts/check_legacy_intelligence_assembly_baseline.py" in (
        manifest["policy"]["allowed_entrypoints"]
    )
    assert all(
        isinstance(step["command"], list)
        for step in manifest["profiles"]["source-runtime"]["steps"]
    )


def test_validate_command_is_ci_safe_and_machine_readable() -> None:
    result = _run("validate", "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "schema_version": 1,
        "ok": True,
        "manifest_id": "lantai-codex-harness-quality",
        "profiles": ["source-runtime"],
    }


def test_prompt_locks_commands_evidence_and_repository_state() -> None:
    manifest = MODULE.validate_manifest(MANIFEST_PATH)
    prompt = MODULE.build_mission_prompt(manifest, "source-runtime")

    assert "Investigation is read-only" in prompt
    assert "Do not access the network from mission commands" in prompt
    assert "git status --porcelain=v1" in prompt
    assert "actual commands, exit codes" in prompt
    assert "LANTAI_REPORT_BEGIN" in prompt
    for step in manifest["profiles"]["source-runtime"]["steps"]:
        assert step["id"] in prompt
        assert json.dumps(step["command"], ensure_ascii=False) in prompt


def test_live_preflight_names_missing_keys_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = MODULE.validate_manifest(MANIFEST_PATH)
    monkeypatch.setattr(MODULE, "_find_codex", lambda: "/usr/local/bin/codex")
    monkeypatch.setattr(MODULE, "_supports_harness_exec_server", lambda _path: True)
    monkeypatch.setattr(MODULE, "_git_status", lambda _root: "")
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: "/usr/bin/python3")
    monkeypatch.setattr(Path, "is_file", lambda _path: True)

    result = MODULE.collect_preflight(
        manifest,
        "source-runtime",
        live=True,
        environ={"OPENAI_API_KEY": "app-secret"},
    )

    assert result["ok"] is False
    checks = {item["id"]: item for item in result["checks"]}
    assert checks["OPENAI_API_KEY"]["ok"] is True
    assert checks["CODEX_API_KEY"]["ok"] is False
    serialized = json.dumps(result)
    assert "app-secret" not in serialized


def test_executor_receives_only_restricted_key() -> None:
    child = MODULE._executor_env(
        {"PATH": "/bin", "OPENAI_API_KEY": "broad", "UNRELATED": "kept"},
        "restricted",
    )

    assert "OPENAI_API_KEY" not in child
    assert child["CODEX_API_KEY"] == "restricted"
    assert "UNRELATED" not in child
    assert child["PYTHONDONTWRITEBYTECODE"] == "1"
    assert child["LANTAI_METADATA_ONLY"] == "1"


def test_executor_command_forces_read_only_noninteractive_sandbox() -> None:
    command = MODULE._executor_command(
        "/usr/local/bin/codex", "https://executor.example", "env-123"
    )

    assert 'sandbox_mode="read-only"' in command
    assert 'approval_policy="never"' in command
    assert "--strict-config" in command
    assert command[-1] == "--exit-on-stdin-close"


def test_sse_parser_handles_comments_multiline_data_and_done() -> None:
    events = list(
        MODULE.iter_sse_json(
            [
                ": keepalive\n",
                "data: {\"type\":\n",
                "data: \"agent.session.idle\"}\n",
                "\n",
                "data: [DONE]\n",
                "\n",
            ]
        )
    )

    assert events == [{"type": "agent.session.idle"}]


def test_only_root_turn_is_terminal() -> None:
    subagent = {
        "type": "agent.session.turn.completed",
        "turn": {"subagent_id": "subagent-1"},
    }
    root = {
        "type": "agent.session.turn.completed",
        "turn": {"subagent_id": None},
    }

    assert MODULE._is_root_turn(subagent) is False
    assert MODULE._is_root_turn(root) is True


def test_agent_report_requires_exact_passing_command_evidence(tmp_path: Path) -> None:
    manifest = MODULE.validate_manifest(MANIFEST_PATH)
    steps = manifest["profiles"]["source-runtime"]["steps"]
    report = {
        "schema_version": 1,
        "profile": "source-runtime",
        "conclusion": "pass",
        "repo_unchanged": True,
        "steps": [
            {
                "id": step["id"],
                "command": step["command"],
                "exit_code": 0,
                "conclusion": "pass",
                "evidence": "observed",
            }
            for step in steps
        ],
        "findings": [],
        "summary": "All checks passed.",
    }
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "agent.session.turn.output_text.done",
                "text": (
                    "LANTAI_REPORT_BEGIN\n"
                    + json.dumps(report)
                    + "\nLANTAI_REPORT_END"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    parsed = MODULE._load_agent_report(events, "source-runtime", steps)

    assert parsed["conclusion"] == "pass"


def test_agent_report_cannot_claim_pass_after_a_failed_step(tmp_path: Path) -> None:
    manifest = MODULE.validate_manifest(MANIFEST_PATH)
    steps = manifest["profiles"]["source-runtime"]["steps"]
    report = {
        "schema_version": 1,
        "profile": "source-runtime",
        "conclusion": "pass",
        "repo_unchanged": True,
        "steps": [
            {
                "id": step["id"],
                "command": step["command"],
                "exit_code": 1 if index == 0 else 0,
                "conclusion": "needs_revision" if index == 0 else "pass",
                "evidence": "observed",
            }
            for index, step in enumerate(steps)
        ],
        "findings": [],
        "summary": "Incorrect pass.",
    }
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "agent.session.turn.output_text.done",
                "text": (
                    "LANTAI_REPORT_BEGIN\n"
                    + json.dumps(report)
                    + "\nLANTAI_REPORT_END"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MODULE.HarnessError, match="claimed pass"):
        MODULE._load_agent_report(events, "source-runtime", steps)


def test_agent_report_rejects_step_without_observed_evidence(tmp_path: Path) -> None:
    manifest = MODULE.validate_manifest(MANIFEST_PATH)
    steps = manifest["profiles"]["source-runtime"]["steps"]
    report = {
        "schema_version": 1,
        "profile": "source-runtime",
        "conclusion": "pass",
        "repo_unchanged": True,
        "steps": [
            {
                "id": step["id"],
                "command": step["command"],
                "exit_code": 0,
                "conclusion": "pass",
                "evidence": "" if index == 0 else "observed",
            }
            for index, step in enumerate(steps)
        ],
        "findings": [],
        "summary": "Incorrect pass.",
    }
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "agent.session.turn.output_text.done",
                "text": (
                    "LANTAI_REPORT_BEGIN\n"
                    + json.dumps(report)
                    + "\nLANTAI_REPORT_END"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MODULE.HarnessError, match="observed evidence"):
        MODULE._load_agent_report(events, "source-runtime", steps)


def test_api_base_must_be_https_and_cannot_embed_credentials() -> None:
    assert MODULE._validate_api_base("https://api.openai.com/v1/") == (
        "https://api.openai.com/v1"
    )
    with pytest.raises(MODULE.HarnessError, match="HTTPS"):
        MODULE._validate_api_base("http://api.openai.com/v1")
    with pytest.raises(MODULE.HarnessError, match="embedded credentials"):
        MODULE._validate_api_base("https://secret@example.com/v1")


def test_wait_surfaces_stream_error_without_waiting_for_timeout() -> None:
    state = MODULE.StreamState()
    state.errors.put("connection failed")
    started = time.monotonic()

    with pytest.raises(MODULE.HarnessError, match="connection failed"):
        MODULE._wait_or_error(state.connected, state, 5, "connection")

    assert time.monotonic() - started < 1


def test_manifest_rejects_artifact_path_escape(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["policy"]["artifact_root"] = "../outside"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.HarnessError, match="safe repository-relative"):
        MODULE.validate_manifest(bad)


def test_manifest_rejects_safe_but_unapproved_artifact_root(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["policy"]["artifact_root"] = "src/harness-results"
    bad = tmp_path / "bad-root.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.HarnessError, match="must remain"):
        MODULE.validate_manifest(bad)


def test_manifest_rejects_command_outside_allowlist(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["profiles"]["source-runtime"]["steps"][0]["command"] = [
        "python3",
        "scripts/unreviewed.py",
    ]
    bad = tmp_path / "bad-command.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.HarnessError, match="entrypoint is not allowed"):
        MODULE.validate_manifest(bad)


def test_manifest_rejects_new_entrypoint_even_if_added_to_policy(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["policy"]["allowed_entrypoints"].append("scripts/unreviewed.py")
    payload["profiles"]["source-runtime"]["steps"][0]["command"] = [
        "python3",
        "scripts/unreviewed.py",
    ]
    bad = tmp_path / "unapproved-entrypoint.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.HarnessError, match="unapproved entrypoints"):
        MODULE.validate_manifest(bad)


def test_manifest_rejects_unapproved_script_argument(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["profiles"]["source-runtime"]["steps"][0]["command"].append(
        "--output=src/result.json"
    )
    bad = tmp_path / "unapproved-argument.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.HarnessError, match="unapproved script arguments"):
        MODULE.validate_manifest(bad)


def test_live_run_requires_explicit_beta_policy_acknowledgement() -> None:
    result = _run("run", "--json")

    assert result.returncode == 2
    assert "--acknowledge-beta-data-policy" in result.stderr


def test_run_mission_uses_self_hosted_lifecycle_and_deletes_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = MODULE.validate_manifest(MANIFEST_PATH)
    profile = manifest["profiles"]["source-runtime"]
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    calls: list[tuple[str, str, str, object]] = []
    spawned: dict[str, object] = {}

    def fake_preflight(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "ok": True,
            "warnings": [],
            "checks": [],
            "codex_path": "/usr/local/bin/codex",
        }

    def fake_request(
        method: str,
        url: str,
        api_key: str,
        payload: object = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append((method, url, api_key, payload))
        if method == "POST" and url.endswith("/agents/sessions"):
            return {
                "id": "session/1",
                "environment": {
                    "id": "env-1",
                    "remote_url": "wss://executor.example/session/1",
                },
            }
        return {}

    def fake_stream(
        _url: str,
        _key: str,
        output_path: Path,
        state: object,
        _timeout: float,
    ) -> None:
        report = {
            "schema_version": 1,
            "profile": "source-runtime",
            "conclusion": "pass",
            "repo_unchanged": True,
            "steps": [
                {
                    "id": step["id"],
                    "command": step["command"],
                    "exit_code": 0,
                    "conclusion": "pass",
                    "evidence": "observed",
                }
                for step in profile["steps"]
            ],
            "findings": [],
            "summary": "All checks passed.",
        }
        events = [
            {
                "type": "agent.session.turn.output_text.done",
                "text": (
                    "LANTAI_REPORT_BEGIN\n"
                    + json.dumps(report)
                    + "\nLANTAI_REPORT_END"
                ),
            },
            {
                "type": "agent.session.turn.completed",
                "turn": {"subagent_id": None},
            },
        ]
        output_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        state.opened.set()
        state.connected.set()
        state.terminal_event = events[-1]
        state.terminal.set()

    class FakeProcess:
        def __init__(self, command: object, **kwargs: object) -> None:
            spawned["command"] = command
            spawned["env"] = kwargs["env"]
            self.stdin = io.StringIO()
            self.returncode = 0

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

        def terminate(self) -> None:
            raise AssertionError("completed fake executor must not be terminated")

        def kill(self) -> None:
            raise AssertionError("completed fake executor must not be killed")

    monkeypatch.setattr(MODULE, "collect_preflight", fake_preflight)
    monkeypatch.setattr(MODULE, "_json_request", fake_request)
    monkeypatch.setattr(MODULE, "_stream_events", fake_stream)
    monkeypatch.setattr(MODULE, "_git_status", lambda _root: "")
    monkeypatch.setattr(MODULE.subprocess, "Popen", FakeProcess)

    result = MODULE.run_mission(
        manifest,
        "source-runtime",
        model="gpt-6-astra",
        api_base="https://api.openai.com/v1",
        repo_root=repo_root,
        environ={
            "PATH": "/bin",
            "OPENAI_API_KEY": "app-secret",
            "CODEX_API_KEY": "executor-secret",
            "UNRELATED": "do-not-forward",
        },
    )

    assert result["conclusion"] == "pass"
    assert result["session_deleted"] is True
    assert calls[0][0:3] == (
        "POST",
        "https://api.openai.com/v1/agents/sessions",
        "app-secret",
    )
    assert calls[0][3]["environment"] == {
        "type": "self_hosted",
        "workspace_directory": str(repo_root),
    }
    assert calls[-1][0] == "DELETE"
    assert "session%2F1" in calls[-1][1]
    assert spawned["env"]["CODEX_API_KEY"] == "executor-secret"
    assert "OPENAI_API_KEY" not in spawned["env"]
    assert "UNRELATED" not in spawned["env"]
    assert (Path(result["artifact_dir"]) / "agent-report.json").is_file()
