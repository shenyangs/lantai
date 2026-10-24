#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run opt-in persona semantics against the already-running packaged Lantai.

The running desktop backend keeps provider credentials inside its own process.
This harness supplies explicit baseline/candidate system prompts to the stateless
Office Agent stream endpoint, with no tools and no source chat id.  The default
mode is a local-only preflight; real provider requests require ``--execute``.

This is deliberately *not* a full chat-runtime smoke.  It does not exercise
chat persistence, Memory, subject projection, ququ, or post-answer reflection.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

# Prompt compilation must not inspect or decrypt the user's production profile.
# The desktop credential is obtained only from the already-running App process.
# This directory intentionally lives for the process lifetime.
# pylint: disable-next=consider-using-with
_PROMPT_ISOLATION = tempfile.TemporaryDirectory(
    prefix="lantai-persona-prompt-",
)
_PROMPT_ISOLATION_ROOT = Path(_PROMPT_ISOLATION.name)
os.environ["LANTAI_WORKING_DIR"] = str(_PROMPT_ISOLATION_ROOT / "working")
os.environ["LANTAI_SECRET_DIR"] = str(_PROMPT_ISOLATION_ROOT / "secret")
os.environ["LANTAI_DISABLE_KEYRING"] = "1"
os.environ["LANTAI_SKIP_KEYRING"] = "1"

from lantai.agents.persona.constitution import (  # noqa: E402  # pylint: disable=wrong-import-position
    get_protected_temperament_enforcement,
    load_temperament_constitution,
)
from lantai.agents.prompt import (  # noqa: E402  # pylint: disable=wrong-import-position
    PromptBuilder,
    PromptConfig,
    _build_agent_identity_header,
    build_system_prompt_from_working_dir,
)


DEFAULT_BASE_REF = "c75a56daf29f8540574e453285b6e06681a08a20"
DEFAULT_PACKAGE = (
    Path.home() / "Downloads" / "shen-xingzhi-professional-growth-20260920.zip"
)
DEFAULT_APP = Path.home() / "Downloads" / "澜台.app"
DESKTOP_TOKEN_ENV = "LANTAI_DESKTOP_TOKEN"
DESKTOP_TOKEN_HEADER = "X-Lantai-Desktop-Token"
OFFICE_SUFFIX_ID = "surface.office_agent"
PROMPT_FILES = tuple(PromptConfig.DEFAULT_FILES)


@dataclass(frozen=True)
class RunningBackend:
    pid: int
    port: int
    desktop_token: str
    executable: str

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class PromptArm:
    name: str
    prompt: str
    version: str
    source: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


def _read_process_table() -> str:
    completed = subprocess.run(
        ["ps", "eww", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("utf-8", errors="replace")


def _parse_running_backends(
    process_table: str,
    *,
    app_path: Path | None = DEFAULT_APP,
) -> list[RunningBackend]:
    """Parse backend metadata without returning or logging the full process row."""

    expected = str(app_path.resolve()) if app_path is not None else ""
    rows: list[RunningBackend] = []
    for raw_line in process_table.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)$", raw_line)
        if not match:
            continue
        command = match.group(2)
        if "lantai-server" not in command or not re.search(
            r"\sapp\s",
            command,
        ):
            continue
        if expected and expected not in command:
            continue
        port_match = re.search(
            r"(?:^|\s)--port(?:=|\s+)(\d{1,5})(?:\s|$)",
            command,
        )
        token_match = re.search(
            rf"(?:^|\s){re.escape(DESKTOP_TOKEN_ENV)}=([^\s]+)",
            command,
        )
        executable = command.split(" app ", 1)[0].strip()
        if not port_match or not token_match:
            continue
        port = int(port_match.group(1))
        token = token_match.group(1).strip("'\"")
        if not (1 <= port <= 65535) or not token:
            continue
        rows.append(
            RunningBackend(
                pid=int(match.group(1)),
                port=port,
                desktop_token=token,
                executable=executable,
            ),
        )
    return rows


def _discover_running_backend(
    *,
    app_path: Path | None = DEFAULT_APP,
) -> RunningBackend:
    rows = _parse_running_backends(_read_process_table(), app_path=app_path)
    if not rows:
        raise RuntimeError(
            "No authenticated running Lantai desktop backend was found for "
            f"{app_path or 'any app path'}",
        )
    if len(rows) != 1:
        pids = ", ".join(str(row.pid) for row in rows)
        raise RuntimeError(
            f"Multiple matching Lantai backends found (PIDs: {pids})",
        )
    return rows[0]


def _validate_loopback_base_url(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError(
            "The running-App bridge only accepts an HTTP loopback URL",
        )
    if not parsed.port:
        raise ValueError(
            "The running-App bridge requires an explicit loopback port",
        )
    return base_url.rstrip("/")


def _request_json(
    method: str,
    url: str,
    *,
    desktop_token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    data = None
    headers = {
        DESKTOP_TOKEN_HEADER: desktop_token,
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = {"detail": raw[:1000]}
        return exc.code, body
    except URLError as exc:
        raise RuntimeError(
            f"Local Lantai API is unavailable: {exc.reason}",
        ) from exc


def _request_office_stream(
    base_url: str,
    *,
    desktop_token: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    request = Request(
        f"{base_url}/api/console/office-agent/stream",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            DESKTOP_TOKEN_HEADER: desktop_token,
            "Accept": "application/x-ndjson",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    terminal = ""
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Office Agent stream returned HTTP {response.status}",
                )
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise RuntimeError(
                        "Office Agent stream emitted a non-object event",
                    )
                events.append(event)
                event_type = str(event.get("type") or "")
                if event_type == "delta":
                    text_parts.append(str(event.get("text") or ""))
                elif event_type == "tool-call":
                    call = event.get("toolCall")
                    if isinstance(call, dict):
                        tool_calls.append(call)
                elif event_type in {"done", "error"}:
                    terminal = event_type
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(
            f"Office Agent stream failed with HTTP {exc.code}: {detail}",
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Office Agent stream is unavailable: {exc.reason}",
        ) from exc
    if terminal != "done":
        error = next(
            (
                str(event.get("error") or "")
                for event in reversed(events)
                if event.get("type") == "error"
            ),
            "stream ended without done",
        )
        return {
            "status": "ERROR",
            "error": error,
            "text": "".join(text_parts).strip(),
            "events": events,
            "tool_calls": tool_calls,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
        }
    if tool_calls:
        raise RuntimeError(
            "Office Agent returned tool calls even though the tool list was empty",
        )
    return {
        "status": "DONE",
        "text": "".join(text_parts).strip(),
        "events": events,
        "tool_calls": tool_calls,
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
    }


def _git_bytes(repo_root: Path, ref: str, relative_path: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{ref}:{relative_path}"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _git_text(repo_root: Path, ref: str, relative_path: str) -> str:
    return _git_bytes(repo_root, ref, relative_path).decode("utf-8")


def _read_release_provenance(app_path: Path) -> dict[str, Any]:
    path = (
        app_path.expanduser().resolve()
        / "Contents"
        / "Resources"
        / "console"
        / "dist"
        / "release-provenance.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or not str(payload.get("gitSha") or "").strip()
    ):
        raise RuntimeError(f"Invalid running-App release provenance: {path}")
    return payload


def _verify_release_office_route(
    repo_root: Path,
    release_sha: str,
) -> dict[str, Any]:
    """Verify the exact packaged release's stream function has no state writer."""

    relative = "src/lantai/app/routers/console.py"
    source = _git_text(repo_root, release_sha, relative)
    tree = ast.parse(source, filename=f"{release_sha}:{relative}")
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "stream_office_agent"
        ),
        None,
    )
    if function is None:
        raise RuntimeError(
            "Packaged release source has no stream_office_agent function",
        )
    start_line = min(
        [function.lineno, *(item.lineno for item in function.decorator_list)],
    )
    segment = "\n".join(
        source.splitlines()[start_line - 1 : function.end_lineno],
    )
    required = (
        'router.post("/office-agent/stream")',
        "create_model_client",
        "ModelRequest(",
        "stream=True",
    )
    forbidden = (
        "ChatManager",
        "create_chat",
        "update_chat",
        "delete_chat",
        "memory_manager",
        "MemoryManager",
        "SafeJSONSession",
    )
    missing = [token for token in required if token not in segment]
    present_forbidden = [token for token in forbidden if token in segment]
    if missing or present_forbidden:
        raise RuntimeError(
            "Packaged Office stream source contract is not stateless: "
            f"missing={missing}, forbidden={present_forbidden}",
        )
    return {
        "release_sha": release_sha,
        "source_path": relative,
        "function_sha256": hashlib.sha256(segment.encode("utf-8")).hexdigest(),
        "required_model_stream_refs_present": True,
        "chat_memory_writer_refs_absent": True,
    }


def _assert_prompt_assembly_compatible(repo_root: Path, base_ref: str) -> None:
    """Fail on semantic compiler drift while allowing formatting and version bumps."""

    def fingerprint(source: str) -> str:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id if isinstance(node.func, ast.Name) else ""
            )
            if function_name != "PromptSection":
                continue
            for keyword in node.keywords:
                value = keyword.value
                if (
                    keyword.arg == "version"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and re.fullmatch(r"1\.(?:5\.3|6\.[01])", value.value)
                ):
                    value.value = "<persona-section-version>"
        return ast.dump(tree, include_attributes=False)

    changed: list[str] = []
    for relative in (
        "src/lantai/agents/prompt.py",
        "src/lantai/agents/persona/constitution.py",
    ):
        baseline = _git_text(repo_root, base_ref, relative)
        current = (repo_root / relative).read_text(encoding="utf-8")
        if fingerprint(baseline) != fingerprint(current):
            changed.append(relative)
    if changed:
        raise RuntimeError(
            "Prompt assembly code changed since the baseline; build the baseline with its "
            "own source instead of reusing the current compiler: "
            + ", ".join(changed),
        )


def _compile_prompt(
    workspace_dir: Path,
    constitution_path: Path,
    language: str,
) -> str:
    workspace_prompt = PromptBuilder(
        working_dir=workspace_dir,
        enabled_files=list(PROMPT_FILES),
        heartbeat_enabled=False,
        language=language,
        memory_manager=None,
    ).build()
    return "\n\n".join(
        (
            _build_agent_identity_header("default").rstrip(),
            load_temperament_constitution(constitution_path).compile_prompt(),
            workspace_prompt,
            get_protected_temperament_enforcement(language),
        ),
    )


def _build_prompt_arms(
    repo_root: Path,
    *,
    base_ref: str,
    language: str,
) -> dict[str, PromptArm]:
    _assert_prompt_assembly_compatible(repo_root, base_ref)
    relative_workspace = Path("src/lantai/agents/md_files") / language
    relative_constitution = (
        Path("src/lantai/agents/md_files/constitutions")
        / f"shen_xingzhi-{language}.yaml"
    )
    candidate_workspace = repo_root / relative_workspace
    candidate_constitution = repo_root / relative_constitution
    candidate_prompt = _compile_prompt(
        candidate_workspace,
        candidate_constitution,
        language,
    )
    canonical_candidate = build_system_prompt_from_working_dir(
        working_dir=candidate_workspace,
        enabled_files=list(PROMPT_FILES),
        agent_id="default",
        heartbeat_enabled=False,
        language=language,
        memory_manager=None,
    )
    if candidate_prompt != canonical_candidate:
        raise RuntimeError(
            "Candidate prompt does not match the production prompt compiler",
        )

    with tempfile.TemporaryDirectory(
        prefix="lantai-persona-baseline-",
    ) as raw_tmp:
        staged = Path(raw_tmp)
        for name in PROMPT_FILES:
            relative = (relative_workspace / name).as_posix()
            (staged / name).write_bytes(
                _git_bytes(repo_root, base_ref, relative),
            )
        staged_constitution = staged / relative_constitution.name
        staged_constitution.write_bytes(
            _git_bytes(repo_root, base_ref, relative_constitution.as_posix()),
        )
        baseline_prompt = _compile_prompt(
            staged,
            staged_constitution,
            language,
        )
        baseline_version = load_temperament_constitution(
            staged_constitution,
        ).version

    candidate_version = load_temperament_constitution(
        candidate_constitution,
    ).version
    return {
        "baseline": PromptArm(
            name="baseline",
            prompt=baseline_prompt,
            version=baseline_version,
            source=base_ref,
        ),
        "candidate": PromptArm(
            name="candidate",
            prompt=candidate_prompt,
            version=candidate_version,
            source=str(repo_root),
        ),
    }


def _load_semantic_cases(package_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(package_path) as archive:
        matches = [
            name
            for name in archive.namelist()
            if name.endswith("/evals/semantic_cases.json")
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "Expected exactly one */evals/semantic_cases.json in the package",
            )
        payload = json.loads(archive.read(matches[0]).decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Semantic case payload must be an object")
    return payload


def _load_semantic_cases_json(suite_path: Path) -> dict[str, Any]:
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Semantic case payload must be an object")
    _all_cases(payload)
    return payload


def _all_cases(suite: dict[str, Any]) -> list[dict[str, Any]]:
    cases = [
        *suite.get("single_turn_cases", []),
        *suite.get("multi_turn_scenarios", []),
    ]
    if not all(
        isinstance(case, dict) and str(case.get("id") or "") for case in cases
    ):
        raise RuntimeError("Semantic suite contains an invalid case")
    return cases


def _select_cases(
    suite: dict[str, Any],
    *,
    case_ids: Iterable[str],
    run_all: bool,
) -> list[dict[str, Any]]:
    cases = _all_cases(suite)
    by_id = {str(case["id"]): case for case in cases}
    requested = list(dict.fromkeys(case_ids))
    if run_all and requested:
        raise ValueError("Use either --all or --case-id, not both")
    if run_all:
        return cases
    if not requested:
        raise ValueError(
            "Real execution requires at least one --case-id or --all",
        )
    missing = [case_id for case_id in requested if case_id not in by_id]
    if missing:
        raise ValueError("Unknown semantic case id(s): " + ", ".join(missing))
    return [by_id[case_id] for case_id in requested]


def _case_turns(case: dict[str, Any]) -> list[str]:
    if case.get("type") == "single_turn":
        return [str(case.get("user_input") or "")]
    turns = case.get("turns") or []
    return [
        str(turn.get("user") or "") for turn in turns if isinstance(turn, dict)
    ]


def _arm_order(case_id: str, turn_index: int) -> list[str]:
    digest = hashlib.sha256(f"{case_id}:{turn_index}".encode("utf-8")).digest()
    return (
        ["baseline", "candidate"]
        if digest[0] % 2 == 0
        else ["candidate", "baseline"]
    )


def _selected_arm_names(selection: str) -> tuple[str, ...]:
    if selection == "both":
        return ("baseline", "candidate")
    if selection in {"baseline", "candidate"}:
        return (selection,)
    raise ValueError(f"Unsupported prompt-arm selection: {selection}")


def _office_payload(
    *,
    prompt: str,
    messages: list[dict[str, str]],
    provider_id: str,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    return {
        "agent_id": "default",
        "source_chat_id": "",
        "model_binding": {
            "provider_id": provider_id,
            "model": model,
            "source": "persona_semantic_smoke",
        },
        "system": prompt,
        "messages": messages,
        "tools": [],
        "max_tokens": max_tokens,
    }


def _active_model(payload: Any) -> tuple[str, str]:
    slot = payload.get("active_llm") if isinstance(payload, dict) else None
    if not isinstance(slot, dict):
        raise RuntimeError("Running App has no effective active model")
    provider_id = str(slot.get("provider_id") or "").strip()
    model = str(slot.get("model") or "").strip()
    if not provider_id or not model:
        raise RuntimeError(
            "Running App returned an incomplete active model binding",
        )
    return provider_id, model


def _select_model_binding(
    active_payload: Any,
    providers_payload: Any,
    *,
    requested_provider_id: str,
    requested_model: str,
) -> tuple[str, str, str]:
    """Resolve the active model or validate an explicit configured override."""

    provider_id = requested_provider_id.strip()
    model = requested_model.strip()
    if bool(provider_id) != bool(model):
        raise ValueError(
            "Both --provider-id and --model must be supplied together",
        )
    if not provider_id:
        active_provider_id, active_model = _active_model(active_payload)
        return active_provider_id, active_model, "effective_active_model"

    if not isinstance(providers_payload, list):
        raise RuntimeError("Running App returned an invalid provider catalog")
    provider = next(
        (
            item
            for item in providers_payload
            if isinstance(item, dict)
            and str(item.get("id") or "") == provider_id
        ),
        None,
    )
    if provider is None:
        raise ValueError(
            f"Provider '{provider_id}' is not configured in the running App",
        )
    if (
        provider.get("require_api_key", True)
        and not str(provider.get("api_key") or "").strip()
    ):
        raise ValueError(
            f"Provider '{provider_id}' has no configured credential",
        )

    catalog = [
        item
        for item in [
            *(provider.get("models") or []),
            *(provider.get("extra_models") or []),
        ]
        if isinstance(item, dict)
    ]
    model_info = next(
        (item for item in catalog if str(item.get("id") or "") == model),
        None,
    )
    protocols = (
        model_info.get("supported_protocols") or [] if model_info else []
    )
    supported_chat_protocols = {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
    }
    if (
        model_info is None
        or not model_info.get("show_in_chat", True)
        or (protocols and not supported_chat_protocols.intersection(protocols))
    ):
        raise ValueError(
            f"Model '{model}' is not an available chat model for provider '{provider_id}'",
        )
    return provider_id, model, "explicit_verified_override"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staged.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(staged, path)


def _run_cases(
    cases: list[dict[str, Any]],
    *,
    arms: dict[str, PromptArm],
    base_url: str,
    desktop_token: str,
    provider_id: str,
    model: str,
    max_tokens: int,
    timeout: float,
    output: Path,
    metadata: dict[str, Any],
    selected_arm_names: tuple[str, ...],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "metadata": metadata,
        "cases": [],
    }
    captured_errors = 0
    _write_json_atomic(output, result)
    for case in cases:
        case_id = str(case["id"])
        transcripts: dict[str, list[dict[str, str]]] = {
            arm_name: [] for arm_name in selected_arm_names
        }
        case_result: dict[str, Any] = {
            "id": case_id,
            "type": str(case.get("type") or ""),
            "domain": str(case.get("domain") or ""),
            "rubric": {
                key: case[key]
                for key in (
                    "must_observe",
                    "must_observe_across_turns",
                    "must_not_observe",
                )
                if key in case
            },
            "turns": [],
        }
        result["cases"].append(case_result)
        for turn_index, user_text in enumerate(_case_turns(case), start=1):
            turn_result: dict[str, Any] = {
                "turn": turn_index,
                "user": user_text,
                "request_order": [
                    arm_name
                    for arm_name in _arm_order(case_id, turn_index)
                    if arm_name in selected_arm_names
                ],
                "outputs": {},
            }
            case_result["turns"].append(turn_result)
            for arm_name in turn_result["request_order"]:
                messages = [
                    *transcripts[arm_name],
                    {"role": "user", "text": user_text},
                ]
                response = _request_office_stream(
                    base_url,
                    desktop_token=desktop_token,
                    payload=_office_payload(
                        prompt=arms[arm_name].prompt,
                        messages=messages,
                        provider_id=provider_id,
                        model=model,
                        max_tokens=max_tokens,
                    ),
                    timeout=timeout,
                )
                turn_result["outputs"][arm_name] = response
                if response["status"] == "DONE":
                    transcripts[arm_name] = [
                        *messages,
                        {"role": "assistant", "text": response["text"]},
                    ]
                else:
                    captured_errors += 1
                    transcripts[arm_name] = messages
                _write_json_atomic(output, result)
        case_result["status"] = (
            "CAPTURED_WITH_ERRORS_NOT_JUDGED"
            if any(
                output_item.get("status") == "ERROR"
                for turn in case_result["turns"]
                for output_item in turn["outputs"].values()
            )
            else "CAPTURED_NOT_JUDGED"
        )
        _write_json_atomic(output, result)
    result["captured_errors"] = captured_errors
    result["status"] = (
        "CAPTURED_WITH_ERRORS_NOT_JUDGED"
        if captured_errors
        else "CAPTURED_NOT_JUDGED"
    )
    _write_json_atomic(output, result)
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument(
        "--suite-json",
        type=Path,
        help="Explicit semantic suite JSON. The historical package remains unchanged.",
    )
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--app-path", type=Path, default=DEFAULT_APP)
    parser.add_argument("--base-url", default="")
    parser.add_argument(
        "--provider-id",
        default="",
        help="Configured running-App provider override; requires --model.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Configured running-App chat model override; requires --provider-id.",
    )
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--all", action="store_true", dest="run_all")
    parser.add_argument(
        "--arms",
        choices=("both", "candidate", "baseline"),
        default="both",
        help=(
            "Prompt arm(s) to execute. Use candidate-only for full release "
            "regression after paired critical cases."
        ),
    )
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Send real provider requests. Without this flag, only local preflight runs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not 1 <= args.max_tokens <= 32768:
        raise ValueError("--max-tokens must be between 1 and 32768")
    repo_root = args.repo_root.resolve()
    package = args.package.expanduser().resolve()
    arms = _build_prompt_arms(
        repo_root,
        base_ref=args.base_ref,
        language=args.language,
    )
    suite_path = (
        args.suite_json.expanduser().resolve() if args.suite_json else None
    )
    suite = (
        _load_semantic_cases_json(suite_path)
        if suite_path is not None
        else _load_semantic_cases(package)
    )
    suite_source = suite_path or package
    suite_sha256 = hashlib.sha256(suite_source.read_bytes()).hexdigest()
    selected_arm_names = _selected_arm_names(args.arms)

    discovered = _discover_running_backend(app_path=args.app_path)
    base_url = _validate_loopback_base_url(
        args.base_url or discovered.base_url,
    )
    if urlparse(base_url).port != discovered.port:
        raise ValueError(
            "--base-url port does not match the discovered App backend; refusing to "
            "send its desktop token to another loopback service",
        )
    desktop_token = (
        os.environ.get(DESKTOP_TOKEN_ENV, "").strip()
        or discovered.desktop_token
    )

    provenance = _read_release_provenance(args.app_path)
    release_contract = _verify_release_office_route(
        repo_root,
        str(provenance["gitSha"]),
    )

    active_url = f"{base_url}/api/models/active?" + urlencode(
        {"scope": "effective", "agent_id": "default"},
    )
    active_status, active_payload = _request_json(
        "GET",
        active_url,
        desktop_token=desktop_token,
        timeout=10.0,
    )
    if active_status != 200:
        raise RuntimeError(
            f"Active-model preflight failed with HTTP {active_status}",
        )
    providers_status, providers_payload = _request_json(
        "GET",
        f"{base_url}/api/models",
        desktop_token=desktop_token,
        timeout=10.0,
    )
    if providers_status != 200:
        raise RuntimeError(
            f"Provider-catalog preflight failed with HTTP {providers_status}",
        )
    provider_id, model, model_binding_source = _select_model_binding(
        active_payload,
        providers_payload,
        requested_provider_id=args.provider_id,
        requested_model=args.model,
    )
    route_status, _ = _request_json(
        "POST",
        f"{base_url}/api/console/office-agent/stream",
        desktop_token=desktop_token,
        payload={},
        timeout=10.0,
    )
    if route_status != 422:
        raise RuntimeError(
            "Office Agent validation preflight did not return HTTP 422; refusing execution",
        )

    preflight = {
        "status": "READY_NOT_EXECUTED"
        if not args.execute
        else "READY_TO_EXECUTE",
        "running_backend": {
            "pid": discovered.pid,
            "base_url": base_url,
            "executable": discovered.executable,
            "desktop_token_exported": False,
        },
        "release_provenance": {
            "version": provenance.get("version"),
            "git_sha": provenance.get("gitSha"),
            "source_branch": provenance.get("sourceBranch"),
            "build_time": provenance.get("buildTime"),
        },
        "release_route_contract": release_contract,
        "model_binding": {
            "provider_id": provider_id,
            "model": model,
            "source": model_binding_source,
            "configured_catalog_entry_verified": True,
        },
        "route": "/api/console/office-agent/stream",
        "route_validation_status": route_status,
        "surface_suffix": OFFICE_SUFFIX_ID,
        "no_tools": True,
        "source_chat_id": "",
        "limitations": [
            "Does not create or update a chat or Memory record.",
            (
                "Does not exercise the full chat Runtime, subject projection, "
                "ququ, or post-answer reflection."
            ),
            (
                "The running App appends the same fixed Office surface suffix "
                "to both prompt arms."
            ),
        ],
        "suite": {
            "source": str(suite_source),
            "source_sha256": suite_sha256,
            "base_head": suite.get("base_head"),
            "single_turn_cases": len(suite.get("single_turn_cases", [])),
            "multi_turn_scenarios": len(suite.get("multi_turn_scenarios", [])),
        },
        "prompt_arms": {
            name: {
                "constitution_version": arm.version,
                "source": arm.source,
                "sha256": arm.sha256,
                "chars": len(arm.prompt),
            }
            for name, arm in arms.items()
            if name in selected_arm_names
        },
    }
    if not args.execute:
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0

    if args.output is None:
        raise ValueError("--output is required with --execute")
    cases = _select_cases(suite, case_ids=args.case_id, run_all=args.run_all)
    metadata = {
        **preflight,
        "status": "EXECUTING",
        "base_ref": args.base_ref,
        "candidate_head": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "selected_case_ids": [str(case["id"]) for case in cases],
        "selected_prompt_arms": list(selected_arm_names),
        "max_tokens": args.max_tokens,
    }
    result = _run_cases(
        cases,
        arms=arms,
        base_url=base_url,
        desktop_token=desktop_token,
        provider_id=provider_id,
        model=model,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        output=args.output.resolve(),
        metadata=metadata,
        selected_arm_names=selected_arm_names,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "output": str(args.output.resolve()),
                "cases": [case["id"] for case in result["cases"]],
                "provider_id": provider_id,
                "model": model,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
