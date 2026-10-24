# -*- coding: utf-8 -*-
"""Read enabled skill instructions without exposing install paths."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from ...config.context import (
    get_current_workspace_context,
    get_current_workspace_dir,
)
from ...constant import WORKING_DIR
from ...runtime.tool_response import TextBlock, ToolResponse
from ..skills_manager import SkillService
from .skill_identity import resolve_effective_skill_identities
from .skill_resources import executable_skill_resources, get_skill_channel


def _error_response(message: str, *, error_code: str) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=f"Error: {message}")],
        metadata={
            "error_code": error_code,
            "deterministic_failure": True,
        },
        is_error=True,
    )


def _is_safe_relative_skill_path(file_path: str) -> bool:
    normalized = (file_path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts


async def read_skill(
    skill_name: str,
    file_path: str = "SKILL.md",
) -> ToolResponse:
    """Read one enabled skill instruction or resource file.

    Use ``SKILL.md`` first. Relative links in that document can then be loaded
    by calling this tool again with the same ``skill_name`` and a safe relative
    ``file_path`` such as ``pptxgenjs.md`` or ``references/example.md``.
    """

    normalized_name = (skill_name or "").strip()
    normalized_path = (file_path or "SKILL.md").strip().replace("\\", "/")
    if not normalized_name:
        return _error_response(
            "No `skill_name` provided.",
            error_code="missing_skill_name",
        )
    if not _is_safe_relative_skill_path(normalized_path):
        return _error_response(
            "`file_path` must be a safe relative path inside the skill.",
            error_code="invalid_skill_resource_path",
        )

    workspace_context = get_current_workspace_context()
    workspace_dir = Path(
        workspace_context.agent_home
        if workspace_context is not None
        else get_current_workspace_dir() or WORKING_DIR,
    ).expanduser()
    identity = next(
        (
            item
            for item in resolve_effective_skill_identities(
                workspace_dir,
                get_skill_channel(),
            )
            if item.name == normalized_name
        ),
        None,
    )
    if identity is None:
        return _error_response(
            f"The skill '{normalized_name}' is not enabled for this workspace.",
            error_code="skill_not_enabled",
        )

    content = SkillService(workspace_dir).load_skill_file(
        skill_name=normalized_name,
        file_path=normalized_path,
    )
    if content is None:
        return _error_response(
            f"The skill resource '{normalized_path}' was not found.",
            error_code="skill_resource_not_found",
        )
    visible_content = content
    if normalized_path == "SKILL.md":
        executable_resources = executable_skill_resources(normalized_name)
        footer = [
            "",
            "---",
            "Lantai runtime resources:",
            "- Use `list_skill_resources` to discover logical resource paths; do not search for Skill install directories.",
            "- Read text references with `read_skill(skill_name, file_path)` and copy binary assets or templates into the topic workspace with `copy_skill_resource`.",
        ]
        if executable_resources:
            footer.append(
                "- Run documented scripts with `run_skill_script(skill_name, script_path, arguments)`.",
            )
            footer.append(
                "- `arguments` must be a JSON array of separate strings, for "
                'example `arguments=["--flag", "value"]`; never pass one '
                "combined command-line string.",
            )
            footer.append("- Executable scripts:")
            footer.extend(f"  - {path}" for path in executable_resources[:80])
        visible_content = f"{content.rstrip()}\n" + "\n".join(footer)
    return ToolResponse(
        content=[TextBlock(type="text", text=visible_content)],
        metadata={
            "skill_name": normalized_name,
            "file_path": normalized_path,
            "skill_source": identity.source,
            "skill_content_sha256": identity.content_sha256,
            "skill_resolution_priority": identity.resolution_priority,
            "skill_version": identity.version_text,
        },
    )
