#!/usr/bin/env python3
"""Exercise a real hosted collaboration through the frozen packaged backend.

The smoke stages only the configured host and two named collaborators in an
isolated profile.  It never prints credentials, prompts, or model output.  A
pass proves that either an explicit collaboration work method or an explicit
natural-language meeting-room request invokes the native batch tool, exposes a
progressive room, completes both members, and closes the room without a
legacy-runtime fallback or approval pause.
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
LIFECYCLE_HELPERS = PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
HOST_AGENT_ID = "default"
MEMBER_AGENT_IDS = ("Lantai_Coconut_Agent", "Lantai_Americano_Agent")
EXPECTED_PHASES = {"staffing", "working", "reconciling", "verifying", "closed"}
COLLABORATION_PROMPT = (
    "请真正使用协作室完成一次无人值守的两人会议：邀请椰汁从科学事实、"
    "美式从逻辑定义独立分析‘先有鸡还是先有蛋’，由沈行知主持、对齐、"
    "验证并闭环交付。必须真实调用协作工具，不得角色扮演。"
)


def _load_helpers() -> Any:
    spec = importlib.util.spec_from_file_location(
        "packaged_collaboration_lifecycle_helpers",
        LIFECYCLE_HELPERS,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load smoke helpers from {LIFECYCLE_HELPERS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_for_health(base_url: str, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Packaged backend exited early with code {process.returncode}")
        try:
            with urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # readiness retry
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"Packaged backend health check timed out: {last_error}")


def _copy_root_persona_files(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in (
        "agent.json",
        "AGENTS.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "MEMORY.md",
        "PROFILE.md",
        "SOUL.md",
        "skill.json",
    ):
        source_path = source / name
        if source_path.is_file():
            shutil.copy2(source_path, target / name)


def _stage_profile(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
    target_root: Path,
    host_provider_id: str = "",
    host_model: str = "",
    disable_utility_intelligence: bool = False,
    member_agent_ids: tuple[str, ...] = MEMBER_AGENT_IDS,
) -> tuple[Path, Path, dict[str, str]]:
    source_config = json.loads(
        (source_working_dir / "config.json").read_text(encoding="utf-8"),
    )
    source_profiles = source_config.get("agents", {}).get("profiles", {})
    working_dir = target_root / "working"
    secret_dir = target_root / "working.secret"
    staged_profiles: dict[str, dict[str, str]] = {}
    agent_names: dict[str, str] = {}

    required_agent_ids = {HOST_AGENT_ID, *member_agent_ids}
    missing_required = required_agent_ids.difference(source_profiles)
    if missing_required:
        raise RuntimeError(
            "Configured collaboration agents are unavailable: "
            f"{sorted(missing_required)!r}"
        )

    # Stage the complete configured roster.  Starting with a deliberately
    # incomplete built-in roster activates the normal bootstrap migration,
    # which correctly restores missing agents but assigns the current global
    # model to those restored profiles.  That would make a package smoke test
    # mutate the very per-agent model isolation it is supposed to verify.
    for agent_id, profile in source_profiles.items():
        if not isinstance(profile, dict) or profile.get("enabled") is False:
            continue
        profile = source_profiles.get(agent_id)
        if not isinstance(profile, dict) or not profile.get("workspace_dir"):
            if agent_id in required_agent_ids:
                raise RuntimeError(
                    f"Configured collaboration agent is unavailable: {agent_id}"
                )
            continue
        source_workspace = Path(str(profile["workspace_dir"])).expanduser().resolve()
        target_workspace = working_dir / "workspaces" / agent_id
        _copy_root_persona_files(source_workspace, target_workspace)
        agent_path = target_workspace / "agent.json"
        if not agent_path.is_file():
            if agent_id in required_agent_ids:
                raise RuntimeError(
                    f"Configured agent file is unavailable: {source_workspace}"
                )
            continue
        agent = json.loads(agent_path.read_text(encoding="utf-8"))
        agent["id"] = agent_id
        agent["workspace_dir"] = str(target_workspace)
        agent["runtime"] = {"engine": "native"}
        if agent_id == HOST_AGENT_ID and host_provider_id and host_model:
            agent["active_model"] = {
                "provider_id": host_provider_id,
                "model": host_model,
            }
        agent_path.write_text(
            json.dumps(agent, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        staged_profiles[agent_id] = {
            "id": agent_id,
            "workspace_dir": str(target_workspace),
        }
        agent_names[agent_id] = str(agent.get("name") or agent_id)

    config = dict(source_config)
    config["agents"] = {
        "active_agent": HOST_AGENT_ID,
        "profiles": staged_profiles,
    }
    if disable_utility_intelligence:
        config["utility_intelligence"] = {
            "mode": "follow_active",
            "background_processing_enabled": False,
        }
    working_dir.mkdir(parents=True, exist_ok=True)
    (working_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for name in (".master_key", "providers", "cloud-account.json"):
        source = source_secret_dir / name
        target = secret_dir / name
        if not source.exists():
            raise RuntimeError(f"Configured secret store is missing {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    return working_dir, secret_dir, agent_names


def _stage_officecli_runtime(*, backend: Path, working_dir: Path) -> Path:
    """Mirror Electron's signed-bundle protection for direct backend smokes."""

    bundled = backend.parent / "bin" / "officecli"
    if not bundled.is_file():
        raise FileNotFoundError(f"Bundled OfficeCLI is missing: {bundled}")
    runtime = working_dir / "runtime-tools" / "officecli"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled, runtime)
    runtime.chmod(runtime.stat().st_mode | 0o111)
    return runtime


def _collaboration_request_payload(
    *,
    client_turn_id: str,
    chat_id: str,
    session_id: str,
    trigger: str,
    prompt: str = COLLABORATION_PROMPT,
) -> dict[str, Any]:
    work_method_id = "collaboration" if trigger == "selector" else "standard"
    return {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ],
        "session_id": session_id,
        "user_id": HOST_AGENT_ID,
        "channel": "console",
        "stream": True,
        "client_turn_id": client_turn_id,
        "work_method_id": work_method_id,
        "chat_id": chat_id,
        "meta": {
            "client_turn_id": client_turn_id,
            "work_method_id": work_method_id,
            "chat_id": chat_id,
        },
    }


def _read_sse(
    *,
    base_url: str,
    desktop_token: str,
    chat_id: str,
    session_id: str,
    trigger: str,
    prompt: str,
    timeout: float,
) -> list[dict[str, Any]]:
    token = uuid.uuid4().hex[:12]
    client_turn_id = f"packaged-collaboration-{token}"
    payload = _collaboration_request_payload(
        client_turn_id=client_turn_id,
        chat_id=chat_id,
        session_id=session_id,
        trigger=trigger,
        prompt=prompt,
    )
    request = Request(
        f"{base_url}/api/console/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Lantai-Desktop-Token": desktop_token,
        },
        method="POST",
    )
    frames: list[dict[str, Any]] = []
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
                    frames.append(json.loads("\n".join(data_lines)))
                    data_lines = []
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Packaged collaboration returned HTTP {exc.code}: {detail}") from exc
    return frames


def _collaboration_snapshots(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        snapshot = payload.get("collaboration")
        if isinstance(snapshot, dict):
            snapshots.append(snapshot)
    return snapshots


def _verified_host_delivery(events: list[dict[str, Any]]) -> dict[str, str]:
    """Require the host delivery gate after the member room has closed.

    A hosted room closes at ``ready_for_synthesis``: member completion is not
    semantic acceptance.  The later chat settlement is the authoritative
    proof that the host synthesized, verified, and delivered the answer.
    """

    final_answer: dict[str, Any] | None = None
    settled: dict[str, Any] | None = None
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = str(event.get("kind") or "")
        event_type = str(event.get("type") or "")
        if kind == "chat.final_answer" or (
            event_type == "item.completed"
            and "outcome_status" in payload
            and "delivery_status" in payload
        ):
            final_answer = payload
        elif kind == "chat.turn.settled" or event_type == "turn.completed":
            settled = payload
    if final_answer is None or settled is None:
        raise AssertionError(
            "Host delivery evidence is incomplete after collaboration closure"
        )
    for label, payload in (("final answer", final_answer), ("turn settlement", settled)):
        if payload.get("outcome_status") != "delivered":
            raise AssertionError(
                f"Host {label} was not delivered: "
                f"outcome={payload.get('outcome_status')!r}"
            )
        if payload.get("delivery_status") != "verified":
            raise AssertionError(
                f"Host {label} did not pass Completion Gate: "
                f"delivery={payload.get('delivery_status')!r}"
            )
    return {
        "outcome": "delivered",
        "delivery_status": "verified",
    }


def _read_settled_runtime_events(
    *,
    helpers: Any,
    base_url: str,
    desktop_token: str,
    chat_id: str,
    timeout: float,
    poll_interval: float = 0.2,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Wait for the event ledger to catch up with the terminal SSE frame."""

    deadline = time.monotonic() + min(timeout, 30.0)
    last_error = "Host delivery evidence is incomplete"
    while True:
        status, _body, event_payload = helpers._json_request(
            "GET",
            f"{base_url}/api/chats/{chat_id}/events?after_seq=0",
            headers={"X-Lantai-Desktop-Token": desktop_token},
            timeout=min(timeout, 90),
        )
        if status != 200 or not isinstance(event_payload, dict):
            raise AssertionError(
                "Packaged collaboration smoke could not read Runtime events"
            )
        events = event_payload.get("events")
        if not isinstance(events, list):
            raise AssertionError("Packaged collaboration Runtime event ledger is missing")
        try:
            return events, _verified_host_delivery(events)
        except AssertionError as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            tail = [
                f"{event.get('type')}:{event.get('kind')}:{event.get('status')}"
                for event in events[-8:]
                if isinstance(event, dict)
            ]
            raise AssertionError(
                "Runtime event ledger did not settle after terminal collaboration SSE: "
                f"{last_error}; event_tail={tail!r}"
            )
        time.sleep(poll_interval)


def run(
    *,
    app: Path,
    source_working_dir: Path,
    source_secret_dir: Path,
    timeout: float,
    trigger: str = "selector",
    host_provider_id: str = "",
    host_model: str = "",
    prompt: str = COLLABORATION_PROMPT,
    member_agent_ids: tuple[str, ...] = MEMBER_AGENT_IDS,
    preserve_workspace_on_failure: bool = False,
) -> dict[str, Any]:
    helpers = _load_helpers()
    app_path = app.expanduser().resolve()
    backend = (
        app_path
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    if not backend.is_file():
        raise FileNotFoundError(f"Packaged backend is missing: {backend}")
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-packaged-collaboration-"))
    process: subprocess.Popen[str] | None = None
    log_path = smoke_root / "backend.log"
    succeeded = False
    try:
        working_dir, secret_dir, agent_names = _stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(),
            source_secret_dir=source_secret_dir.expanduser().resolve(),
            target_root=smoke_root,
            host_provider_id=host_provider_id,
            host_model=host_model,
            disable_utility_intelligence=trigger == "prose",
            member_agent_ids=member_agent_ids,
        )
        runtime_officecli = _stage_officecli_runtime(
            backend=backend,
            working_dir=working_dir,
        )
        port = _available_port()
        base_url = f"http://127.0.0.1:{port}"
        desktop_token = f"packaged-collaboration-{uuid.uuid4().hex}"
        env = os.environ.copy()
        env.update(
            {
                "LANTAI_WORKING_DIR": str(working_dir),
                "LANTAI_SECRET_DIR": str(secret_dir),
                "LANTAI_DESKTOP_TOKEN": desktop_token,
                "LANTAI_AUTH_ENABLED": "0",
                "LANTAI_DISABLE_KEYRING": "1",
                "LANTAI_SKIP_KEYRING": "1",
                "LANTAI_REMINDER_DELIVERY_WORKER": "0",
                "LANTAI_DISTRIBUTION_CHANNEL": "official",
                "LANTAI_RELEASE_CHANNEL": "internal",
                "OFFICECLI_PATH": str(runtime_officecli),
                "OFFICECLI_SKIP_UPDATE": "1",
            }
        )
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [
                    str(backend),
                    "app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=str(app_path.parent),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _wait_for_health(base_url, process, min(timeout, 90))
            suffix = uuid.uuid4().hex[:12]
            session_id = f"console:packaged-collaboration-{suffix}"
            workspace_dir = working_dir / "workspaces" / HOST_AGENT_ID
            work_method_id = (
                "collaboration" if trigger == "selector" else "standard"
            )
            status, _body, created = helpers._json_request(
                "POST",
                f"{base_url}/api/chats",
                payload={
                    "name": f"packaged collaboration smoke {suffix}",
                    "session_id": session_id,
                    "user_id": HOST_AGENT_ID,
                    "channel": "console",
                    "meta": {
                        "work_method_id": work_method_id,
                        "topic_workspace_dir": str(
                            workspace_dir / "topic-workspaces" / f"collaboration-{suffix}"
                        ),
                    },
                },
                headers={"X-Lantai-Desktop-Token": desktop_token},
                timeout=min(timeout, 90),
            )
            if status != 200 or not isinstance(created, dict) or not created.get("id"):
                raise AssertionError("Packaged collaboration smoke could not create a chat")
            chat_id = str(created["id"])
            frames = _read_sse(
                base_url=base_url,
                desktop_token=desktop_token,
                chat_id=chat_id,
                session_id=session_id,
                trigger=trigger,
                prompt=prompt,
                timeout=timeout,
            )
            events, host_delivery = _read_settled_runtime_events(
                helpers=helpers,
                base_url=base_url,
                desktop_token=desktop_token,
                chat_id=chat_id,
                timeout=timeout,
            )
        snapshots = _collaboration_snapshots(events)
        if not snapshots:
            raise AssertionError(
                f"{trigger} collaboration did not expose a collaboration room"
            )
        final = max(snapshots, key=lambda item: int(item.get("revision") or 0))
        phases = {str(item.get("phase") or "") for item in snapshots}
        members = final.get("members") if isinstance(final.get("members"), list) else []
        member_ids = {str(item.get("agent_id") or "") for item in members if isinstance(item, dict)}
        member_statuses = [str(item.get("status") or "") for item in members if isinstance(item, dict)]
        serialized_events = json.dumps(events, ensure_ascii=False)
        if not EXPECTED_PHASES.issubset(phases):
            raise AssertionError(
                f"Collaboration room skipped phases: {sorted(EXPECTED_PHASES - phases)}; "
                f"final={json.dumps(final, ensure_ascii=False, sort_keys=True)}"
            )
        if final.get("phase") != "closed" or final.get("status") != "completed":
            raise AssertionError(
                "Collaboration room did not close successfully: "
                f"final={json.dumps(final, ensure_ascii=False, sort_keys=True)}"
            )
        if not 2 <= len(member_ids) <= 4 or any(
            status != "completed" for status in member_statuses
        ):
            raise AssertionError(
                "Collaboration members did not all complete: "
                "expected=2-4 completed configured agents, "
                f"actual={sorted(member_ids)!r}, statuses={member_statuses!r}, "
                f"final={json.dumps(final, ensure_ascii=False, sort_keys=True)}"
            )
        if final.get("unattended") is not True:
            raise AssertionError("Collaboration did not preserve unattended host ownership")
        closure = final.get("closure")
        if not isinstance(closure, dict):
            raise AssertionError("Collaboration closed without a closure assessment")
        if (
            closure.get("outcome") != "ready_for_synthesis"
            or closure.get("synthesis_ready") is not True
            or closure.get("deliverable_ready") is not False
        ):
            raise AssertionError(
                "Member room did not close at the host synthesis boundary: "
                f"closure={json.dumps(closure, ensure_ascii=False, sort_keys=True)}"
            )
        if "runtime_fallback" in serialized_events or "legacy_runtime_fallback" in serialized_events:
            raise AssertionError("Collaboration silently fell back to the legacy Runtime")
        if "approval_required" in serialized_events:
            raise AssertionError("Low-risk collaboration incorrectly paused for approval")
        terminal_frames = [
            frame
            for frame in frames
            if frame.get("object") == "response" and frame.get("status") == "completed"
        ]
        if not terminal_frames:
            raise AssertionError("Collaboration stream did not complete with a host answer")
        preparing_events = [
            event
            for event in events
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("kind") == "collaboration_preparing"
        ]
        if not preparing_events:
            raise AssertionError("Collaboration did not expose its preparing acknowledgement")
        closed_event_index = next(
            (
                index
                for index, event in enumerate(events)
                if isinstance(event.get("payload"), dict)
                and isinstance(event["payload"].get("collaboration"), dict)
                and event["payload"]["collaboration"].get("phase") == "closed"
            ),
            None,
        )
        if closed_event_index is None:
            raise AssertionError("Collaboration never exposed a closed hosted snapshot")
        leaked_message_events = [
            event
            for event in events[:closed_event_index]
            if str(event.get("type") or "")
            in {"message.started", "message.delta", "message.completed"}
        ]
        if leaked_message_events:
            raise AssertionError(
                "Assistant message surfaced before required collaboration completed"
            )
        result = {
            "ok": True,
            "trigger": trigger,
            "utility_intelligence": "disabled" if trigger == "prose" else "configured",
            "host_model": {
                "provider_id": host_provider_id or "profile_default",
                "model": host_model or "profile_default",
            },
            "app": helpers._app_metadata_for_evidence(app_path),
            "agents": {
                "host": agent_names[HOST_AGENT_ID],
                "members": [
                    agent_names.get(str(member.get("agent_id") or ""), str(member.get("display_name") or ""))
                    for member in members
                    if isinstance(member, dict)
                ],
            },
            "frame_count": len(frames),
            "runtime_event_count": len(events),
            "snapshot_count": len(snapshots),
            "phases": sorted(phases),
            "final": {
                "phase": final.get("phase"),
                "status": final.get("status"),
                "unattended": final.get("unattended"),
                "closure_outcome": final.get("closure", {}).get("outcome"),
                "host_outcome": host_delivery["outcome"],
                "host_delivery_status": host_delivery["delivery_status"],
                "member_statuses": member_statuses,
                "member_attempts": [
                    int(member.get("attempt") or 0)
                    for member in members
                    if isinstance(member, dict)
                ],
            },
        }
        succeeded = True
        return result
    finally:
        _terminate(process)
        if succeeded or not preserve_workspace_on_failure:
            shutil.rmtree(smoke_root, ignore_errors=True)
        else:
            print(f"Preserved failed smoke workspace: {smoke_root}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--source-working-dir", type=Path, required=True)
    parser.add_argument("--source-secret-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=360)
    parser.add_argument(
        "--trigger",
        choices=("selector", "prose"),
        default="selector",
        help="Use the explicit selector contract or standard mode with prose only.",
    )
    parser.add_argument("--host-provider-id", default="")
    parser.add_argument("--host-model", default="")
    parser.add_argument(
        "--prompt",
        default=COLLABORATION_PROMPT,
        help="Override the user prompt for an incident-shape replay.",
    )
    parser.add_argument(
        "--member-agent-id",
        action="append",
        dest="member_agent_ids",
        help="Stage a configured member agent. Repeat exactly twice; defaults to Coconut/Americano.",
    )
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument(
        "--preserve-workspace-on-failure",
        action="store_true",
        help="Keep the isolated profile and backend log when validation fails.",
    )
    args = parser.parse_args()
    try:
        member_agent_ids = tuple(args.member_agent_ids or MEMBER_AGENT_IDS)
        if len(member_agent_ids) != 2 or len(set(member_agent_ids)) != 2:
            raise ValueError("Packaged collaboration smoke requires two unique member agents")
        result = run(
            app=args.app,
            source_working_dir=args.source_working_dir,
            source_secret_dir=args.source_secret_dir,
            timeout=args.timeout,
            trigger=args.trigger,
            host_provider_id=args.host_provider_id,
            host_model=args.host_model,
            prompt=args.prompt,
            member_agent_ids=member_agent_ids,
            preserve_workspace_on_failure=args.preserve_workspace_on_failure,
        )
        if args.evidence_output:
            args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI release-gate diagnostics
        print(f"Packaged collaboration smoke failed: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
