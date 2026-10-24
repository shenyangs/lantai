#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Opt-in source smoke for a real model, one read-only tool, and a steer."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
_STAGED_WORKING_FILES = (
    "config.json",
    "settings.json",
    "membership.json",
)


@dataclass(frozen=True, slots=True)
class StagedSourceConfiguration:
    """Disposable configuration roots used by a real Provider smoke."""

    root: Path
    working_dir: Path
    secret_dir: Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_fingerprint(path: Path) -> dict[str, object]:
    stat_result = path.stat()
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "mtime_ns": stat_result.st_mtime_ns,
        "mode": oct(stat_result.st_mode & 0o777),
        "size": stat_result.st_size,
    }


def _selected_provider_path(
    source_secret_dir: Path,
    provider_id: str,
) -> Path:
    provider_root = source_secret_dir / "providers"
    matches: list[Path] = []
    for scope in ("builtin", "custom", "plugin"):
        for candidate in sorted((provider_root / scope).glob("*.json")):
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("id") == provider_id:
                matches.append(candidate)
    if len(matches) != 1:
        raise RuntimeError(
            "Expected exactly one selected Provider config, found "
            f"{len(matches)} for {provider_id!r}",
        )
    return matches[0]


def _fingerprint_source_credentials(
    source_secret_dir: Path,
) -> dict[str, dict[str, object]]:
    """Record immutable evidence for the three live credential inputs."""

    active_model_path = source_secret_dir / "providers" / "active_model.json"
    master_key_path = source_secret_dir / ".master_key"
    active_model = json.loads(active_model_path.read_text(encoding="utf-8"))
    if not isinstance(active_model, dict):
        raise RuntimeError("Active model selection must be an object")
    provider_id = str(active_model.get("provider_id") or "").strip()
    if not provider_id:
        raise RuntimeError("Active model selection has no provider_id")
    provider_path = _selected_provider_path(source_secret_dir, provider_id)
    return {
        "active_model": _file_fingerprint(active_model_path),
        "master_key": _file_fingerprint(master_key_path),
        "provider": _file_fingerprint(provider_path),
    }


def _assert_source_credentials_unchanged(
    expected: dict[str, dict[str, object]],
) -> None:
    changed: list[str] = []
    for label, fingerprint in expected.items():
        path = Path(str(fingerprint["path"]))
        try:
            current = _file_fingerprint(path)
        except OSError:
            changed.append(label)
            continue
        if current != fingerprint:
            changed.append(label)
    if changed:
        raise RuntimeError(
            "Source Provider credential inputs changed during smoke: "
            + ", ".join(changed),
        )


@contextmanager
def _staged_source_configuration(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
) -> Iterator[StagedSourceConfiguration]:
    """Copy all mutable Provider inputs away from the live source roots."""

    stage_root = Path(tempfile.mkdtemp(prefix="lantai-source-config-smoke-"))
    staged_working_dir = stage_root / "working"
    staged_secret_dir = stage_root / "working.secret"
    staged_working_dir.mkdir(mode=0o700)
    try:
        for filename in _STAGED_WORKING_FILES:
            source = source_working_dir / filename
            if source.is_file():
                shutil.copy2(source, staged_working_dir / filename)
        shutil.copytree(
            source_secret_dir,
            staged_secret_dir,
            symlinks=False,
        )
        yield StagedSourceConfiguration(
            root=stage_root,
            working_dir=staged_working_dir,
            secret_dir=staged_secret_dir,
        )
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(
            root.expanduser().resolve(strict=True),
        )
    except ValueError:
        return False
    return True


def _configure_keyring_environment(*, allow_keyring: bool) -> None:
    """Keep no-prompt defaults while allowing an explicit Keychain smoke."""

    if allow_keyring:
        os.environ.pop("LANTAI_SKIP_KEYRING", None)
        os.environ.pop("LANTAI_DISABLE_KEYRING", None)
        return
    os.environ["LANTAI_SKIP_KEYRING"] = "1"
    os.environ["LANTAI_DISABLE_KEYRING"] = "1"


def _disable_keyring_writes_for_smoke() -> None:
    """Allow Keychain reads without ever synchronizing or creating a key."""

    from lantai.security import secret_store

    secret_store._try_keyring_set = (  # pylint: disable=protected-access
        lambda _key_hex: False
    )


def _visible_text(event: Any) -> str:
    if event.type != "message.completed":
        return ""
    message = event.payload.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


async def _run_smoke(  # pylint: disable=too-many-statements
    *,
    timeout: float,
) -> dict[str, Any]:
    from lantai.agents.model_factory import create_model_diagnostic_client
    from lantai.providers.provider_manager import ProviderManager
    from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
    from lantai.runtime.coherence.contracts import ExecutionFence
    from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
    from lantai.runtime.messages import RuntimeMessage
    from lantai.runtime.tools import (
        LantaiToolRegistry,
        ToolPolicyContext,
        ToolSpec,
    )
    from lantai.runtime.turn_control import RuntimeTurnControlStore

    manager = ProviderManager.get_instance()
    active_model = manager.get_active_model()
    if active_model is None:
        raise RuntimeError("No configured active model is available")
    client = create_model_diagnostic_client(model_slot=active_model)
    model_requests = 0

    class CountingClient:
        provider_id = client.provider_id
        model_name = client.model_name

        async def stream(self, request):
            nonlocal model_requests
            model_requests += 1
            async for event in client.stream(request):
                yield event

        async def complete(self, request):
            nonlocal model_requests
            model_requests += 1
            return await client.complete(request)

    executed: list[str] = []

    def inspect_loop_marker(token: str) -> dict[str, object]:
        executed.append(token)
        return {
            "content": [
                {
                    "type": "text",
                    "text": "The read-only loop marker is LOOP_TOOL_OK.",
                },
            ],
        }

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="inspect_loop_marker",
            description=(
                "Read the fixed Agent Loop smoke marker. Call exactly once."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "token": {"type": "string", "enum": ["probe"]},
                },
                "required": ["token"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        inspect_loop_marker,
    )

    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-agent-loop-smoke-"))
    run_id = f"agent-loop-smoke-{uuid.uuid4().hex[:12]}"
    session_id = f"console:{run_id}"
    client_turn_id = f"initial-{uuid.uuid4().hex[:8]}"
    steer_turn_id = f"steer-{uuid.uuid4().hex[:8]}"
    store = RuntimeTurnControlStore(smoke_root)
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id=session_id,
            user_id="default",
            channel="console",
            chat_id=session_id,
            run_id=run_id,
            workspace_dir=str(smoke_root),
        ),
        messages=[
            RuntimeMessage.text(
                "user",
                (
                    "Call inspect_loop_marker exactly once with token=probe. "
                    "After reading it, answer briefly and include LOOP_TOOL_OK."
                ),
                metadata={"client_turn_id": client_turn_id},
            ),
        ],
        tool_policy_context={
            "agent_id": "default",
            "session_id": session_id,
            "chat_id": session_id,
            "turn_id": run_id,
            "workspace_dir": str(smoke_root),
            "workspace_access_mode": "read_only",
            "workspace_trust_state": "trusted",
            "permission_profile": "workspace",
            "approval_level": "AUTO",
        },
        metadata={
            "state_workspace_dir": str(smoke_root),
            "attempt_id": run_id,
            "runtime_generation": 0,
            "client_turn_id": client_turn_id,
        },
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: CountingClient(),
        tool_registry=registry,
        tool_policy_context=ToolPolicyContext.model_validate(
            runtime_input.tool_policy_context,
        ),
        max_iters=6,
        turn_timeout_seconds=timeout,
        progressive_tools=False,
    )
    events = []
    steer_appended = False
    started = time.monotonic()

    async def collect() -> None:
        nonlocal steer_appended
        async for event in runtime.run_turn(runtime_input):
            events.append(event)
            if not steer_appended and event.type in {
                "message.started",
                "tool.call.started",
                "tool.started",
            }:
                store.append_steer(
                    client_turn_id=steer_turn_id,
                    run_id=run_id,
                    attempt_id=run_id,
                    chat_id=session_id,
                    text=(
                        "Keep the read-only tool result and also include "
                        "LOOP_STEER_OK in the final answer."
                    ),
                    fence=ExecutionFence(
                        attempt_id=run_id,
                        generation=0,
                    ),
                )
                steer_appended = True

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
        visible_answers = [
            text for event in events if (text := _visible_text(event))
        ]
        terminal = [
            event
            for event in events
            if event.type
            in {"turn.completed", "turn.failed", "turn.interrupted"}
        ]
        steer_consumed = any(
            event.type == "runtime.warning"
            and event.payload.get("kind") == "run_inbox_consumed"
            and event.payload.get("client_turn_id") == steer_turn_id
            for event in events
        )
        result = {
            "schema": "com.lantai.source-agent-loop-smoke.v1",
            "ok": bool(
                executed == ["probe"]
                and steer_appended
                and steer_consumed
                and len(terminal) == 1
                and terminal[0].type == "turn.completed"
                and visible_answers
                and "LOOP_TOOL_OK" in visible_answers[-1]
                and "LOOP_STEER_OK" in visible_answers[-1],
            ),
            "configured_model": {
                "provider_id": active_model.provider_id,
                "model": active_model.model,
            },
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "model_request_count": model_requests,
            "tool_execution_count": len(executed),
            "steer_appended": steer_appended,
            "steer_consumed": steer_consumed,
            "terminal_types": [event.type for event in terminal],
            "visible_answer_count": len(visible_answers),
            "final_has_tool_marker": bool(
                visible_answers and "LOOP_TOOL_OK" in visible_answers[-1],
            ),
            "final_has_steer_marker": bool(
                visible_answers and "LOOP_STEER_OK" in visible_answers[-1],
            ),
        }
        if not result["ok"]:
            raise AssertionError(json.dumps(result, ensure_ascii=False))
        return result
    finally:
        shutil.rmtree(smoke_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working-dir", type=Path, required=True)
    parser.add_argument("--secret-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument(
        "--allow-keyring",
        action="store_true",
        help=(
            "Use the OS Keychain when source credentials are not decryptable "
            "with the file-backed master key. This may show an OS prompt."
        ),
    )
    args = parser.parse_args()

    source_working_dir = args.working_dir.expanduser().resolve(strict=True)
    source_secret_dir = args.secret_dir.expanduser().resolve(strict=True)
    output = (
        args.evidence_output.expanduser().resolve()
        if args.evidence_output is not None
        else None
    )
    if output is not None and any(
        _path_is_within(output, source_root)
        for source_root in (source_working_dir, source_secret_dir)
    ):
        raise RuntimeError(
            "Evidence output must be outside the source working and secret dirs",
        )
    source_fingerprints = _fingerprint_source_credentials(source_secret_dir)
    try:
        with _staged_source_configuration(
            source_working_dir=source_working_dir,
            source_secret_dir=source_secret_dir,
        ) as staged:
            os.environ["LANTAI_WORKING_DIR"] = str(staged.working_dir)
            os.environ["LANTAI_SECRET_DIR"] = str(staged.secret_dir)
            _configure_keyring_environment(allow_keyring=args.allow_keyring)
            if str(SOURCE_ROOT) not in sys.path:
                sys.path.insert(0, str(SOURCE_ROOT))
            if args.allow_keyring:
                _disable_keyring_writes_for_smoke()
            result = asyncio.run(_run_smoke(timeout=max(1.0, args.timeout)))
    finally:
        _assert_source_credentials_unchanged(source_fingerprints)
    result["source_configuration"] = {
        "staged": True,
        "source_unchanged": True,
        "keyring_allowed": args.allow_keyring,
        "keyring_write_disabled": args.allow_keyring,
        "fingerprints": source_fingerprints,
    }
    if args.evidence_output is not None:
        assert output is not None
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
