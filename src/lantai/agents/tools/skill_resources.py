# -*- coding: utf-8 -*-
"""Discover and execute enabled Skill resources through logical paths.

The model never receives a Skill installation path.  It addresses resources
with ``skill_name`` plus a path relative to that Skill, while the runtime owns
resolution, workspace scoping, subprocess isolation, and packaged execution.
"""

import asyncio
from contextvars import ContextVar
from functools import wraps
import json
import os
import shlex
import shutil
import signal
import sys
from pathlib import Path, PurePosixPath
from typing import cast

from ...config.context import (
    get_current_workspace_context,
    get_current_workspace_dir,
)
from ...constant import WORKING_DIR
from ...runtime.tool_response import FileBlock, TextBlock, ToolResponse
from ...security.tool_execution_context import build_sandboxed_tool_env
from ...security.output_sanitizer import (
    sanitize_public_output,
    summarize_subprocess_failure,
)
from ..skills_manager import (
    get_workspace_skills_dir,
    read_skill_manifest,
    resolve_effective_skills,
)
from .skill_identity import resolve_effective_skill_identities

_EXECUTABLE_SUFFIXES = frozenset({".py", ".js", ".cjs", ".mjs", ".sh"})
_IGNORED_PARTS = frozenset({"__pycache__", ".git", ".DS_Store"})
_MAX_RESOURCE_ITEMS = 200
_MAX_ARGUMENTS = 128
_MAX_ARGUMENT_LENGTH = 8_192
_MAX_OUTPUT_CHARS = 256 * 1024


_skill_channel: ContextVar[str] = ContextVar(
    "skill_channel",
    default="console",
)


def get_skill_channel() -> str:
    return _skill_channel.get()


def bind_skill_channel(function, channel: str):
    """Bind host-owned routing without exposing a model-controlled argument."""

    @wraps(function)
    async def execute(*args, **kwargs):
        token = _skill_channel.set(channel)
        try:
            return await function(*args, **kwargs)
        finally:
            _skill_channel.reset(token)

    return execute


def _error_response(message: str) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=f"Error: {message}")],
        is_error=True,
    )


def _skill_workspace_and_project() -> tuple[Path, Path]:
    context = get_current_workspace_context()
    skill_workspace = (
        Path(
            context.agent_home
            if context is not None
            else get_current_workspace_dir() or WORKING_DIR,
        )
        .expanduser()
        .resolve()
    )
    project_workspace = (
        Path(
            context.project_root
            if context is not None
            else get_current_workspace_dir() or WORKING_DIR,
        )
        .expanduser()
        .resolve()
    )
    return skill_workspace, project_workspace


def _safe_relative_path(value: str) -> PurePosixPath | None:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized or "\x00" in normalized:
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _enabled_skill_dir(skill_name: str) -> tuple[Path, Path] | None:
    normalized_name = str(skill_name or "").strip()
    if not normalized_name:
        return None
    skill_workspace, project_workspace = _skill_workspace_and_project()
    if normalized_name not in resolve_effective_skills(
        skill_workspace,
        get_skill_channel(),
    ):
        return None
    skill_dir = (
        get_workspace_skills_dir(skill_workspace) / normalized_name
    ).resolve()
    if not skill_dir.is_dir():
        return None
    return skill_dir, project_workspace


def _enabled_skill_identity(skill_name: str):
    skill_workspace, _project_workspace = _skill_workspace_and_project()
    return next(
        (
            identity
            for identity in resolve_effective_skill_identities(
                skill_workspace,
                get_skill_channel(),
            )
            if identity.name == skill_name
        ),
        None,
    )


def _identity_metadata(skill_name: str) -> dict[str, object]:
    identity = _enabled_skill_identity(skill_name)
    if identity is None:
        return {}
    return {
        "skill_source": identity.source,
        "skill_content_sha256": identity.content_sha256,
        "skill_resolution_priority": identity.resolution_priority,
        "skill_version": identity.version_text,
    }


def _resource_paths(skill_name: str, prefix: str = "") -> list[str] | None:
    resolved = _enabled_skill_dir(skill_name)
    if resolved is None:
        return None
    skill_dir, _project_workspace = resolved
    normalized_prefix = _safe_relative_path(prefix) if prefix else None
    if prefix and normalized_prefix is None:
        return []
    base = (
        (skill_dir / Path(*normalized_prefix.parts)).resolve()
        if normalized_prefix
        else skill_dir
    )
    try:
        base.relative_to(skill_dir)
    except ValueError:
        return []
    if not base.exists():
        return []
    candidates = [base] if base.is_file() else list(base.rglob("*"))
    resources: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(skill_dir)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if candidate.suffix.lower() == ".pyc":
            continue
        resources.append(relative.as_posix())
    return sorted(resources)


def executable_skill_resources(skill_name: str) -> list[str]:
    """Return logical executable resource paths for an enabled Skill."""

    resources = _resource_paths(skill_name, "scripts")
    if resources is None:
        return []
    return [
        path
        for path in resources
        if PurePosixPath(path).suffix.lower() in _EXECUTABLE_SUFFIXES
    ]


async def list_skill_resources(
    skill_name: str,
    prefix: str = "",
) -> ToolResponse:
    """List logical resource paths belonging to an enabled Skill.

    Use this after ``read_skill`` when its instructions mention scripts,
    references, templates, or assets.  Paths are relative logical identifiers;
    no Skill installation directory is exposed.  Narrow large Skills with a
    prefix such as ``scripts`` or ``references``.
    """

    normalized_name = str(skill_name or "").strip()
    if not normalized_name:
        return _error_response("No `skill_name` provided.")
    if prefix and _safe_relative_path(prefix) is None:
        return _error_response("`prefix` must be a safe relative Skill path.")
    resources = _resource_paths(normalized_name, prefix)
    if resources is None:
        return _error_response(
            f"The skill '{normalized_name}' is not enabled for this workspace.",
        )
    visible = resources[:_MAX_RESOURCE_ITEMS]
    lines = [f"Resources for enabled skill '{normalized_name}':"]
    lines.extend(f"- {item}" for item in visible)
    if not visible:
        lines.append("- No matching resources.")
    if len(resources) > len(visible):
        lines.append(
            f"- … {len(resources) - len(visible)} more; use `prefix` to narrow the list.",
        )
    return ToolResponse(
        content=[TextBlock(type="text", text="\n".join(lines))],
        metadata={
            "skill_name": normalized_name,
            "prefix": str(prefix or "").strip().replace("\\", "/"),
            "resource_count": len(resources),
            **_identity_metadata(normalized_name),
        },
    )


def _resolve_script(
    skill_name: str,
    script_path: str,
) -> tuple[Path, Path] | None:
    relative = _safe_relative_path(script_path)
    if (
        relative is None
        or not relative.parts
        or relative.parts[0] != "scripts"
    ):
        return None
    if relative.suffix.lower() not in _EXECUTABLE_SUFFIXES:
        return None
    resolved = _enabled_skill_dir(skill_name)
    if resolved is None:
        return None
    skill_dir, project_workspace = resolved
    script = (skill_dir / Path(*relative.parts)).resolve()
    try:
        script.relative_to(skill_dir)
    except ValueError:
        return None
    if not script.is_file():
        return None
    return script, project_workspace


def _resolve_resource(
    skill_name: str,
    resource_path: str,
) -> tuple[Path, Path] | None:
    relative = _safe_relative_path(resource_path)
    if relative is None:
        return None
    resolved = _enabled_skill_dir(skill_name)
    if resolved is None:
        return None
    skill_dir, project_workspace = resolved
    resource = (skill_dir / Path(*relative.parts)).resolve()
    try:
        resource.relative_to(skill_dir)
    except ValueError:
        return None
    if not resource.exists() or resource.is_symlink():
        return None
    if resource.is_dir() and any(
        path.is_symlink() for path in resource.rglob("*")
    ):
        return None
    return resource, project_workspace


async def copy_skill_resource(
    skill_name: str,
    resource_path: str,
    destination_path: str,
    overwrite: bool = False,
) -> ToolResponse:
    """Copy a bundled Skill resource into the current topic workspace.

    Use this for binary assets, templates, fonts, or resource directories that
    cannot be consumed through ``read_skill``. Both paths are logical: the
    source is relative to the enabled Skill and the destination is relative to
    the current topic workspace. Private Skill installation paths stay hidden.
    """

    normalized_name = str(skill_name or "").strip()
    source_relative = _safe_relative_path(resource_path)
    destination_relative = _safe_relative_path(destination_path)
    if not normalized_name:
        return _error_response("No `skill_name` provided.")
    if source_relative is None:
        return _error_response(
            "`resource_path` must be a safe relative Skill path.",
        )
    if destination_relative is None:
        return _error_response(
            "`destination_path` must be a safe relative workspace path.",
        )
    resolved = _resolve_resource(normalized_name, source_relative.as_posix())
    if resolved is None:
        return _error_response(
            f"The Skill resource '{source_relative.as_posix()}' was not found or is unsafe.",
        )
    source, project_workspace = resolved
    destination = (
        project_workspace / Path(*destination_relative.parts)
    ).resolve()
    try:
        destination.relative_to(project_workspace)
    except ValueError:
        return _error_response(
            "The destination must stay inside the topic workspace.",
        )
    if destination.exists() and not overwrite:
        return _error_response(
            f"The workspace destination '{destination_relative.as_posix()}' already exists.",
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=overwrite)
            resource_type = "directory"
        else:
            shutil.copy2(source, destination)
            resource_type = "file"
    except OSError as exc:
        return _error_response(
            f"The Skill resource could not be copied: {exc}",
        )
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=(
                    "Skill resource copied into the topic workspace: "
                    f"{destination_relative.as_posix()}"
                ),
            ),
        ],
        metadata={
            "skill_name": normalized_name,
            "resource_path": source_relative.as_posix(),
            "destination_path": destination_relative.as_posix(),
            "resource_type": resource_type,
            **_identity_metadata(normalized_name),
        },
    )


def _validated_arguments(arguments: list[str]) -> list[str]:
    if isinstance(arguments, str):
        try:
            arguments = shlex.split(arguments)
        except ValueError as exc:
            raise TypeError(
                "`arguments` must be a JSON array of separate strings, for "
                'example ["--request", "request.json"]. The legacy '
                f"command-line string could not be parsed safely: {exc}",
            ) from exc
    if not isinstance(arguments, list):
        raise TypeError(
            "`arguments` must be a JSON array of separate strings, for "
            'example ["--request", "request.json"].',
        )
    if len(arguments) > _MAX_ARGUMENTS:
        raise ValueError(
            f"`arguments` cannot contain more than {_MAX_ARGUMENTS} items.",
        )
    normalized: list[str] = []
    for argument in arguments:
        if not isinstance(argument, str):
            raise TypeError("Every script argument must be a string.")
        if "\x00" in argument or len(argument) > _MAX_ARGUMENT_LENGTH:
            raise ValueError("A script argument is invalid or too long.")
        normalized.append(argument)
    return normalized


def _skill_runtime_env(
    skill_workspace: Path,
    skill_name: str,
) -> dict[str, str]:
    env = build_sandboxed_tool_env()
    # The host-selected agent ID is injected by the trusted execution boundary.
    # It is deliberately not exposed as a model-controlled script argument.
    from ...app.agent_context import get_current_agent_id

    env["LANTAI_ACTIVE_AGENT_ID"] = get_current_agent_id()
    # Packaged scripts are subprocesses of the desktop runtime. Preserve the
    # trusted state root selected by the host so child-side ProviderManager
    # lookups resolve the same model config and secret store as the app.
    env["LANTAI_WORKING_DIR"] = str(WORKING_DIR)
    manifest = read_skill_manifest(skill_workspace)
    entry = manifest.get("skills", {}).get(skill_name) or {}
    requirements = entry.get("requirements") or {}
    required_envs = requirements.get("require_envs") or []
    for env_name in required_envs:
        key = str(env_name or "").strip()
        if key and os.environ.get(key) is not None:
            env[key] = os.environ[key]
    identity = next(
        (
            item
            for item in resolve_effective_skill_identities(
                skill_workspace,
                get_skill_channel(),
            )
            if item.name == skill_name
        ),
        None,
    )
    if identity is not None:
        env.update(
            {
                "LANTAI_ACTIVE_SKILL_NAME": identity.name,
                "LANTAI_ACTIVE_SKILL_SOURCE": identity.source,
                "LANTAI_ACTIVE_SKILL_SHA256": identity.content_sha256,
                "LANTAI_ACTIVE_SKILL_PRIORITY": str(
                    identity.resolution_priority,
                ),
                "LANTAI_ACTIVE_SKILL_VERSION": identity.version_text,
            },
        )
    return env


def _script_command(script: Path, env: dict[str, str]) -> list[str]:
    suffix = script.suffix.lower()
    if suffix == ".py":
        # Python Skill children already receive manifest-declared environment
        # values from the parent; never bootstrap unrelated host credentials.
        env["LANTAI_SKILL_SCRIPT_CHILD"] = "1"
        if getattr(sys, "frozen", False):
            env["LANTAI_RUN_SKILL_SCRIPT_PATH"] = str(script)
            return [sys.executable]
        # Builtin Skills use the public Lantai runtime boundary rather than
        # carrying private copies of Office orchestration code.  The sandboxed
        # environment intentionally drops a caller-supplied PYTHONPATH, so add
        # only the directory containing this installed ``lantai`` package.
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
        return [sys.executable, str(script)]
    if suffix == ".sh":
        shell = shutil.which("bash") or shutil.which("sh")
        if shell is None:
            raise RuntimeError(
                "The shell runtime required by this Skill is unavailable.",
            )
        return [shell, str(script)]

    executable = os.environ.get("PRESENTATION_NODE_EXECUTABLE", "").strip()
    node = Path(executable).expanduser() if executable else None
    if node is None or not node.is_file():
        detected = shutil.which("node")
        node = Path(detected) if detected else None
    if node is None or not node.is_file():
        raise RuntimeError(
            "The JavaScript runtime required by this Skill is unavailable.",
        )
    if (
        os.environ.get("PRESENTATION_NODE_MODE", "").strip().lower()
        == "electron"
    ):
        env["ELECTRON_RUN_AS_NODE"] = "1"
    return [str(node), str(script)]


def _redact_output(text: str, secret_values: list[str]) -> str:
    redacted = text
    for value in secret_values:
        if value:
            redacted = redacted.replace(value, "***")
    if len(redacted) > _MAX_OUTPUT_CHARS:
        return redacted[:_MAX_OUTPUT_CHARS] + "\n… output truncated by Lantai."
    return redacted


def _generated_artifact_block(
    stdout_text: str,
    project_workspace: Path,
) -> dict[str, object] | None:
    """Project a successful runner's declared artifact into the tool result.

    Skill runners may log before printing their final JSON envelope, so inspect
    lines from the end.  Only a real file inside the topic workspace is ever
    promoted.  Returning the file in the *same* successful tool result creates
    a durable delivery checkpoint even when a later model continuation fails.
    """

    payload: dict[str, object] | None = None
    for line in reversed(stdout_text.splitlines()):
        try:
            candidate = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    raw_output = str((payload or {}).get("output") or "").strip()
    if not raw_output:
        return None
    artifact = (project_workspace / raw_output).resolve()
    try:
        artifact.relative_to(project_workspace)
    except ValueError:
        return None
    if not artifact.is_file():
        return None
    return FileBlock(
        source={"type": "url", "url": artifact.as_uri()},
        filename=artifact.name,
    )


async def _terminate_skill_process(process) -> None:
    """Stop the script's process group, including descendants, then reap it."""
    if os.name == "nt":
        from .shell import _kill_process_tree_win32

        await asyncio.to_thread(_kill_process_tree_win32, process.pid)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def _collect_skill_output(
    process,
    timeout: float,
) -> tuple[bytes, bytes]:
    """Bound retained output while draining pipes through cancellation cleanup."""
    limit = asyncio.Event()

    async def read(stream) -> bytes:
        chunks = []
        size = 0
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return b"".join(chunks)
            remaining = max(0, _MAX_OUTPUT_CHARS - size)
            if remaining:
                chunks.append(chunk[:remaining])
            size += len(chunk)
            if size > _MAX_OUTPUT_CHARS:
                limit.set()

    stdout_task = asyncio.create_task(read(process.stdout))
    stderr_task = asyncio.create_task(read(process.stderr))
    finished = asyncio.gather(process.wait(), stdout_task, stderr_task)
    limited = asyncio.create_task(limit.wait())
    try:
        done, _ = await asyncio.wait(
            {
                cast(asyncio.Future[object], finished),
                cast(asyncio.Future[object], limited),
            },
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if limit.is_set():
            raise ValueError("Skill script output exceeded the output limit.")
        if finished not in done:
            raise asyncio.TimeoutError()
        _, stdout, stderr = finished.result()
        return stdout, stderr
    except BaseException:
        # Keep pipe readers alive until the killed group has closed its pipes.
        await _terminate_skill_process(process)
        raise
    finally:
        limited.cancel()
        finished.cancel()
        await asyncio.gather(limited, finished, return_exceptions=True)


async def run_skill_script(
    skill_name: str,
    script_path: str,
    arguments: list[str],
    timeout: int = 180,
) -> ToolResponse:
    """Run a script bundled with an enabled Skill by logical relative path.

    Read the Skill first, then pass the documented ``scripts/...`` path and
    an argument array.  The runtime resolves the private install location and
    runs the script with the current topic workspace as its working directory.
    Generated files therefore remain visible to normal file tools.  Do not use
    shell ``find`` commands to locate Skill installation paths.
    """

    normalized_name = str(skill_name or "").strip()
    if not normalized_name:
        return _error_response("No `skill_name` provided.")
    relative = _safe_relative_path(script_path)
    if (
        relative is None
        or not relative.parts
        or relative.parts[0] != "scripts"
    ):
        return _error_response(
            "`script_path` must be a safe relative path under `scripts/`.",
        )
    if normalized_name not in resolve_effective_skills(
        _skill_workspace_and_project()[0],
        get_skill_channel(),
    ):
        return _error_response(
            f"The skill '{normalized_name}' is not enabled for this workspace.",
        )
    resolved = _resolve_script(normalized_name, relative.as_posix())
    if resolved is None:
        return _error_response(
            f"The executable Skill resource '{relative.as_posix()}' was not found or is unsupported.",
        )
    script, project_workspace = resolved
    arguments_repaired = isinstance(arguments, str)
    try:
        argv = _validated_arguments(arguments)
        timeout_seconds = max(1, min(int(timeout), 900))
    except (TypeError, ValueError) as exc:
        return _error_response(str(exc))

    skill_workspace, _project = _skill_workspace_and_project()
    baseline_env = build_sandboxed_tool_env()
    env = _skill_runtime_env(skill_workspace, normalized_name)
    secret_values = [
        value for key, value in env.items() if key not in baseline_env
    ]
    try:
        command = [*_script_command(script, env), *argv]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project_workspace),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = await _collect_skill_output(
                process,
                timeout_seconds,
            )
        except asyncio.TimeoutError:
            return _error_response(
                f"Skill script timed out after {timeout_seconds} seconds.",
            )
        except ValueError as exc:
            return _error_response(str(exc))
    except (OSError, RuntimeError) as exc:
        return _error_response(f"Skill script could not start: {exc}")

    private_roots = (script.parent.parent, skill_workspace, project_workspace)
    stdout_text = sanitize_public_output(
        _redact_output(
            stdout.decode("utf-8", errors="replace"),
            secret_values,
        ),
        private_roots=private_roots,
    ).strip()
    stderr_raw = _redact_output(
        stderr.decode("utf-8", errors="replace"),
        secret_values,
    )
    stderr_text = (
        summarize_subprocess_failure(stderr_raw, private_roots=private_roots)
        if process.returncode != 0
        else sanitize_public_output(stderr_raw, private_roots=private_roots)
    ).strip()
    if process.returncode == 0:
        message = (
            stdout_text or "Skill script completed successfully (no output)."
        )
        if stderr_text:
            message += f"\n[stderr]\n{stderr_text}"
    else:
        message = f"Skill script failed with exit code {process.returncode}."
        if stdout_text:
            message += f"\n[stdout]\n{stdout_text}"
        if stderr_text:
            message += f"\n[stderr]\n{stderr_text}"
    content = [TextBlock(type="text", text=message)]
    if process.returncode == 0:
        artifact_block = _generated_artifact_block(
            stdout_text,
            project_workspace,
        )
        if artifact_block is not None:
            content.append(artifact_block)
    return ToolResponse(
        content=content,
        metadata={
            "exit_code": process.returncode,
            "skill_name": normalized_name,
            "script_path": relative.as_posix(),
            **_identity_metadata(normalized_name),
            **(
                {"arguments_repaired": "string_to_array"}
                if arguments_repaired
                else {}
            ),
        },
        is_error=process.returncode != 0,
    )
