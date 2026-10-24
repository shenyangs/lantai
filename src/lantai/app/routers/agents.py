# -*- coding: utf-8 -*-
"""Multi-agent management API.

Provides RESTful API for managing multiple agent instances.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, field_validator

from lantai.exceptions import AppBaseException

from ...agents.utils.file_handling import read_text_file_with_encoding_fallback
from ..utils import schedule_agent_reload
from ...config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    ModelSlotConfig,
    load_agent_config,
    save_agent_config,
    generate_short_agent_id,
    sanitize_agent_id,
    validate_agent_id,
)
from ...config.utils import load_config, save_config
from ...agents.utils import copy_workspace_md_files, normalize_agent_language
from ...agents.skills_manager import SkillPoolService, get_workspace_skills_dir
from ...agents.role_templates import (
    RoleTemplateDetail,
    RoleTemplateListResponse,
    RoleTemplatePreviewRequest,
    apply_role_template_to_workspace,
    get_role_template,
    list_role_templates,
    parse_role_template_markdown,
)
from ...runtime.store import RuntimeRunStore
from ..capabilities.artifacts import (
    artifact_gc_after_workspace_delete,
    artifact_lifecycle_cascade,
)
from ..multi_agent_manager import MultiAgentManager
from ..workspace_access import (
    should_defer_desktop_workspace_io,
    workspace_access_required_detail,
)
from ..workspace_recovery import (
    WorkspaceRecoveryError,
    adopt_staged_desktop_workspace,
)
from ...constant import WORKING_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentSummary(BaseModel):
    """Agent summary information."""

    id: str
    name: str
    description: str
    workspace_dir: str
    enabled: bool
    active_model: ModelSlotConfig | None = None
    workspace_access_required: bool = False


class AgentListResponse(BaseModel):
    """Response for listing agents."""

    agents: list[AgentSummary]


class ReorderAgentsRequest(BaseModel):
    """Request model for persisting agent order."""

    agent_ids: list[str]


class RestoreWorkspaceRequest(BaseModel):
    """Electron-staged external workspace recovery request."""

    expected_source_dir: str
    staged_workspace_dir: str


class RestoreWorkspaceResponse(BaseModel):
    success: bool
    agent_id: str
    workspace_dir: str
    backup_dir: str | None = None
    source_preserved: bool


class CreateAgentRequest(BaseModel):
    """Request model for creating a new agent.

    The ``id`` field is optional.  When provided the server uses it as
    the agent identifier (after sanitization); when omitted a random
    short UUID is generated automatically.
    """

    id: str | None = None
    name: str
    description: str = ""
    workspace_dir: str | None = None
    language: str | None = None
    skill_names: list[str] | None = None
    active_model: ModelSlotConfig | None = None
    role_template_id: str | None = None
    role_template_markdown: str | None = None
    role_template_source_name: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def sanitize_id(cls, value: str | None) -> str | None:
        """Strip whitespace from the custom ID."""
        if value is None:
            return None
        if isinstance(value, str):
            sanitized = sanitize_agent_id(value)
            return sanitized if sanitized else None
        return value

    @field_validator("workspace_dir", mode="before")
    @classmethod
    def strip_workspace_dir(cls, value: str | None) -> str | None:
        """Strip accidental whitespace"""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        return value

    @field_validator(
        "role_template_id",
        "role_template_markdown",
        "role_template_source_name",
        mode="before",
    )
    @classmethod
    def strip_role_template_fields(cls, value: str | None) -> str | None:
        """Strip role-template fields from the create request."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        return value


def _get_multi_agent_manager(request: Request) -> MultiAgentManager:
    """Get MultiAgentManager from app state."""
    if not hasattr(request.app.state, "multi_agent_manager"):
        raise HTTPException(
            status_code=500,
            detail="MultiAgentManager not initialized",
        )
    return request.app.state.multi_agent_manager


def _raise_if_workspace_access_required(
    agent_id: str,
    workspace_dir: str,
) -> None:
    if should_defer_desktop_workspace_io(workspace_dir):
        raise HTTPException(
            status_code=423,
            detail=workspace_access_required_detail(agent_id, workspace_dir),
        )


def _normalized_agent_order(config) -> list[str]:
    """Return a deduplicated agent order covering every configured agent."""
    profile_ids = list(config.agents.profiles.keys())
    ordered_ids: list[str] = []

    for agent_id in config.agents.agent_order:
        if agent_id in config.agents.profiles and agent_id not in ordered_ids:
            ordered_ids.append(agent_id)

    for agent_id in profile_ids:
        if agent_id not in ordered_ids:
            ordered_ids.append(agent_id)

    return ordered_ids


def _read_profile_description(workspace_dir: str) -> str:
    """Read description from PROFILE.md if exists."""
    try:
        profile_path = Path(workspace_dir) / "PROFILE.md"
        if not profile_path.exists():
            return ""

        content = read_text_file_with_encoding_fallback(profile_path).strip()
        lines = []
        in_identity = False

        for line in content.split("\n"):
            if line.strip().startswith("## 身份") or line.strip().startswith(
                "## Identity",
            ):
                in_identity = True
                continue
            if in_identity:
                if line.strip().startswith("##"):
                    break
                if line.strip() and not line.strip().startswith("#"):
                    lines.append(line.strip())

        return " ".join(lines)[:200] if lines else ""
    except Exception:  # noqa: E722
        return ""


async def _approval_memory_lifecycle_report(
    *,
    workspace_dir: str,
    agent_id: str,
) -> dict:
    from ..approvals import get_approval_service

    return await get_approval_service().approval_memory_lifecycle_report(
        workspace_dir=workspace_dir,
        source_agent_id=agent_id,
        operation="delete",
        dry_run=False,
    )


def _browser_file_grant_lifecycle_report(*, agent_id: str) -> dict:
    from .browser_workbench import browser_file_grant_lifecycle_report

    return browser_file_grant_lifecycle_report(
        operation="delete",
        dry_run=False,
        profile_id=agent_id,
    )


async def _agent_workspace_lifecycle_report(
    *,
    agent_id: str,
    workspace_dir: str,
) -> dict:
    """Archive workspace-scoped runtime state before removing an agent profile."""

    workspace_path = Path(workspace_dir).expanduser()
    report: dict = {
        "agent_id": agent_id,
        "workspace_dir": str(workspace_path),
        "workspace_exists": workspace_path.exists(),
        "workspace_deleted": False,
        "artifact_archive": None,
        "artifact_delete_dry_run": None,
        "approval_memory_lifecycle": None,
        "file_grant_lifecycle": None,
        "runtime_archives": [],
    }
    try:
        report["approval_memory_lifecycle"] = (
            await _approval_memory_lifecycle_report(
                workspace_dir=str(workspace_path),
                agent_id=agent_id,
            )
        )
    except Exception as exc:  # pragma: no cover - lifecycle diagnostics only
        report["approval_memory_lifecycle"] = {
            "kind": "approval_memory_lifecycle",
            "operation": "delete",
            "dry_run": False,
            "workspace_dir": str(workspace_path),
            "source_agent_id": agent_id,
            "error": str(exc),
        }
    try:
        report["file_grant_lifecycle"] = _browser_file_grant_lifecycle_report(
            agent_id=agent_id,
        )
    except Exception as exc:  # pragma: no cover - lifecycle diagnostics only
        report["file_grant_lifecycle"] = {
            "kind": "browser_file_grant_lifecycle",
            "operation": "delete",
            "dry_run": False,
            "profile_id": agent_id,
            "error": str(exc),
        }
    if not workspace_path.exists():
        return report

    report["artifact_archive"] = artifact_lifecycle_cascade(
        workspace_path,
        scope="workspace",
        operation="archive",
        dry_run=False,
    )
    report["artifact_delete_dry_run"] = artifact_gc_after_workspace_delete(
        workspace_path,
        dry_run=True,
    )

    store = RuntimeRunStore(workspace_path)
    runtime_archives = []
    for contract in await store.list_contracts_async():
        archived = await store.archive_contract(
            contract.run_id,
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
        )
        runtime_archives.append(archived.model_dump(mode="json"))
    report["runtime_archives"] = runtime_archives
    return report


@router.get(
    "",
    response_model=AgentListResponse,
    summary="List all agents",
    description="Get list of all configured agents",
)
async def list_agents() -> AgentListResponse:
    """List all configured agents."""
    config = load_config()
    ordered_agent_ids = _normalized_agent_order(config)

    agents = []
    for agent_id in ordered_agent_ids:
        agent_ref = config.agents.profiles[agent_id]
        if should_defer_desktop_workspace_io(agent_ref.workspace_dir):
            agents.append(
                AgentSummary(
                    id=agent_id,
                    name=agent_id.title(),
                    description="",
                    workspace_dir=agent_ref.workspace_dir,
                    enabled=getattr(agent_ref, "enabled", True),
                    workspace_access_required=True,
                ),
            )
            continue
        try:
            agent_config = load_agent_config(agent_id)
            description = agent_config.description or ""

            profile_desc = _read_profile_description(agent_ref.workspace_dir)
            if profile_desc:
                if description.strip():
                    description = f"{description.strip()} | {profile_desc}"
                else:
                    description = profile_desc

            active_model = agent_config.active_model

            agents.append(
                AgentSummary(
                    id=agent_id,
                    name=agent_config.name,
                    description=description,
                    workspace_dir=agent_ref.workspace_dir,
                    enabled=getattr(agent_ref, "enabled", True),
                    active_model=active_model,
                ),
            )
        except Exception:  # noqa: E722
            agents.append(
                AgentSummary(
                    id=agent_id,
                    name=agent_id.title(),
                    description="",
                    workspace_dir=agent_ref.workspace_dir,
                    enabled=getattr(agent_ref, "enabled", True),
                ),
            )

    return AgentListResponse(agents=agents)


@router.put(
    "/order",
    summary="Persist agent order",
    description="Save the full ordered list of configured agent IDs",
)
async def reorder_agents(
    reorder_request: ReorderAgentsRequest = Body(...),
) -> dict:
    """Persist the full ordered list of agent IDs."""
    config = load_config()
    configured_ids = list(config.agents.profiles.keys())

    if len(reorder_request.agent_ids) != len(set(reorder_request.agent_ids)):
        raise HTTPException(
            status_code=400,
            detail="Each configured agent ID must appear exactly once.",
        )

    if set(reorder_request.agent_ids) != set(configured_ids):
        raise HTTPException(
            status_code=400,
            detail="Each configured agent ID must appear exactly once.",
        )

    config.agents.agent_order = list(reorder_request.agent_ids)
    save_config(config)

    return {"success": True, "agent_ids": config.agents.agent_order}


@router.post(
    "/{agentId}/restore-workspace",
    response_model=RestoreWorkspaceResponse,
    summary="Restore an external desktop workspace",
)
async def restore_desktop_workspace(
    restore_request: RestoreWorkspaceRequest,
    agentId: str = PathParam(...),
    request: Request = None,
) -> RestoreWorkspaceResponse:
    """Adopt a workspace copied into managed staging by Electron."""

    if os.environ.get("LANTAI_DESKTOP_APP", "").strip() != "1":
        raise HTTPException(status_code=403, detail="Desktop recovery only.")
    try:
        result = await asyncio.to_thread(
            adopt_staged_desktop_workspace,
            agent_id=agentId,
            expected_source_dir=restore_request.expected_source_dir,
            staged_workspace_dir=restore_request.staged_workspace_dir,
        )
    except WorkspaceRecoveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("Workspace recovery failed for %s", agentId)
        raise HTTPException(
            status_code=500,
            detail="Failed to commit the recovered workspace.",
        ) from exc

    manager = _get_multi_agent_manager(request)
    await manager.reload_agent(agentId)
    return RestoreWorkspaceResponse.model_validate(result)


@router.get(
    "/role-templates",
    response_model=RoleTemplateListResponse,
    summary="List agent role templates",
    description="List built-in role templates for creating collaborator agents",
)
async def list_agent_role_templates() -> RoleTemplateListResponse:
    """List curated role templates."""
    return RoleTemplateListResponse(templates=list_role_templates())


@router.post(
    "/role-templates/preview",
    response_model=RoleTemplateDetail,
    summary="Preview imported role template",
    description="Parse and lint a markdown role template before creating an agent",
)
async def preview_agent_role_template(
    request: RoleTemplatePreviewRequest = Body(...),
) -> RoleTemplateDetail:
    """Preview an imported markdown role template."""
    try:
        return parse_role_template_markdown(
            request.content,
            source_name=request.source_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get(
    "/role-templates/{templateId}",
    response_model=RoleTemplateDetail,
    summary="Get agent role template",
    description="Get a built-in role template detail",
)
async def get_agent_role_template(templateId: str = PathParam(...)) -> RoleTemplateDetail:
    """Get a built-in role template."""
    try:
        return get_role_template(templateId)
    except KeyError as e:
        raise HTTPException(
            status_code=404,
            detail=f"Role template '{templateId}' not found",
        ) from e


@router.get(
    "/{agentId}",
    response_model=AgentProfileConfig,
    summary="Get agent details",
    description="Get complete configuration for a specific agent",
)
async def get_agent(agentId: str = PathParam(...)) -> AgentProfileConfig:
    """Get agent configuration."""
    config = load_config()
    agent_ref = config.agents.profiles.get(agentId)
    if agent_ref is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agentId}' not found")
    _raise_if_workspace_access_required(agentId, agent_ref.workspace_dir)
    try:
        agent_config = load_agent_config(agentId)
        return agent_config
    except (ValueError, AppBaseException) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _generate_unique_id(existing_ids: set[str]) -> str:
    """Generate a unique random short agent ID.

    Raises:
        HTTPException: If a unique ID could not be generated.
    """
    max_attempts = 10
    for _ in range(max_attempts):
        candidate_id = generate_short_agent_id()
        if candidate_id not in existing_ids:
            return candidate_id
    raise HTTPException(
        status_code=500,
        detail="Failed to generate unique agent ID after 10 attempts",
    )


def _resolve_role_template(request: CreateAgentRequest) -> RoleTemplateDetail | None:
    """Resolve a create request's optional role template."""
    if request.role_template_markdown:
        return parse_role_template_markdown(
            request.role_template_markdown,
            source_name=request.role_template_source_name,
        )
    if request.role_template_id:
        return get_role_template(request.role_template_id)
    return None


@router.post(
    "",
    response_model=AgentProfileRef,
    status_code=201,
    summary="Create new agent",
    description="Create a new agent with optional custom ID",
)
async def create_agent(
    request: CreateAgentRequest = Body(...),
) -> AgentProfileRef:
    """Create a new agent.

    When ``request.id`` is provided, it is used as the agent identifier
    (validated for URL-safe characters, length, reserved words, and
    uniqueness).  Otherwise a random short UUID is generated.
    """
    config = load_config()
    existing_ids = set(config.agents.profiles.keys())
    try:
        role_template = _resolve_role_template(request)
    except KeyError as e:
        raise HTTPException(
            status_code=404,
            detail=f"Role template '{request.role_template_id}' not found",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if request.id:
        try:
            validate_agent_id(request.id, existing_ids)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=str(e),
            ) from e
        new_id = request.id
    else:
        new_id = _generate_unique_id(existing_ids)

    workspace_dir = Path(
        request.workspace_dir or f"{WORKING_DIR}/workspaces/{new_id}",
    ).expanduser()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    from ...config.config import (
        ChannelConfig,
        MCPConfig,
        HeartbeatConfig,
        ToolsConfig,
    )

    language = normalize_agent_language(
        request.language or config.agents.language or "en",
    )

    agent_config = AgentProfileConfig(
        id=new_id,
        name=request.name,
        description=(
            request.description
            or (role_template.description if role_template else "")
        ),
        workspace_dir=str(workspace_dir),
        language=language,
        channels=ChannelConfig(),
        mcp=MCPConfig(),
        heartbeat=HeartbeatConfig(),
        tools=ToolsConfig(),
        active_model=request.active_model,
    )

    _initialize_agent_workspace(
        workspace_dir,
        skill_names=(
            request.skill_names
            if request.skill_names is not None
            else (role_template.recommended_skills if role_template else [])
        ),
        language=language,
    )

    if role_template:
        apply_role_template_to_workspace(workspace_dir, role_template)

    agent_ref = AgentProfileRef(
        id=new_id,
        workspace_dir=str(workspace_dir),
        enabled=True,
    )

    config.agents.profiles[new_id] = agent_ref
    config.agents.agent_order = _normalized_agent_order(config)
    save_config(config)
    save_agent_config(new_id, agent_config)

    logger.info(
        "Created new agent: %s (name=%s, role_template=%s)",
        new_id,
        request.name,
        role_template.id if role_template else None,
    )

    return agent_ref


@router.put(
    "/{agentId}",
    response_model=AgentProfileConfig,
    summary="Update agent",
    description="Update agent configuration and trigger reload",
)
async def update_agent(
    agentId: str = PathParam(...),
    agent_config: AgentProfileConfig = Body(...),
    request: Request = None,
) -> AgentProfileConfig:
    """Update agent configuration."""
    config = load_config()

    if agentId not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agentId}' not found",
        )

    _raise_if_workspace_access_required(
        agentId,
        config.agents.profiles[agentId].workspace_dir,
    )
    existing_config = load_agent_config(agentId)

    update_data = agent_config.model_dump(exclude_unset=True)
    workspace_dir_value = update_data.get("workspace_dir")
    if workspace_dir_value:
        workspace_dir = Path(str(workspace_dir_value)).expanduser()
        workspace_dir.mkdir(parents=True, exist_ok=True)
        _initialize_agent_workspace(
            workspace_dir,
            skill_names=[],
            language=(
                existing_config.language
                or config.agents.language
                or "zh"
            ),
        )
        update_data["workspace_dir"] = str(workspace_dir)
        config.agents.profiles[agentId].workspace_dir = str(workspace_dir)
        save_config(config)

    for key, value in update_data.items():
        if key != "id":
            setattr(existing_config, key, value)

    existing_config.id = agentId
    save_agent_config(agentId, existing_config)
    schedule_agent_reload(request, agentId)

    return agent_config


@router.delete(
    "/{agentId}",
    summary="Delete agent",
    description="Delete agent and workspace (cannot delete default agent)",
)
async def delete_agent(
    agentId: str = PathParam(...),
    request: Request = None,
) -> dict:
    """Delete an agent."""
    config = load_config()

    if agentId not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agentId}' not found",
        )

    if agentId == "default":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the default agent",
        )

    _raise_if_workspace_access_required(
        agentId,
        config.agents.profiles[agentId].workspace_dir,
    )

    manager = _get_multi_agent_manager(request)
    await manager.stop_agent(agentId)

    workspace_lifecycle = await _agent_workspace_lifecycle_report(
        agent_id=agentId,
        workspace_dir=config.agents.profiles[agentId].workspace_dir,
    )

    del config.agents.profiles[agentId]
    config.agents.agent_order = _normalized_agent_order(config)
    save_config(config)

    return {
        "success": True,
        "agent_id": agentId,
        "workspace_lifecycle": workspace_lifecycle,
    }


@router.patch(
    "/{agentId}/toggle",
    summary="Toggle agent enabled state",
    description="Enable or disable an agent (cannot disable default agent)",
)
async def toggle_agent_enabled(
    agentId: str = PathParam(...),
    enabled: bool = Body(..., embed=True),
    request: Request = None,
) -> dict:
    """Toggle agent enabled state."""
    config = load_config()

    if agentId not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agentId}' not found",
        )

    if agentId == "default":
        raise HTTPException(
            status_code=400,
            detail="Cannot disable the default agent",
        )

    agent_ref = config.agents.profiles[agentId]
    manager = _get_multi_agent_manager(request)

    if not enabled and getattr(agent_ref, "enabled", True):
        await manager.stop_agent(agentId)

    agent_ref.enabled = enabled
    save_config(config)

    if enabled:
        try:
            await manager.get_agent(agentId)
            logger.info(f"Agent {agentId} started successfully")
        except Exception as e:
            logger.error(f"Failed to start agent {agentId}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Agent enabled but failed to start: {str(e)}",
            ) from e

    return {
        "success": True,
        "agent_id": agentId,
        "enabled": enabled,
    }


def _apply_workspace_md_templates(
    workspace_dir: Path,
    language: str,
    *,
    md_template_id: str | None,
) -> None:
    """Copy common and template-specific markdown files for a workspace."""
    copy_workspace_md_files(
        language,
        workspace_dir,
        md_template_id=md_template_id,
    )


def _ensure_heartbeat_file(workspace_dir: Path, language: str) -> None:
    """Create the default HEARTBEAT.md if it is missing."""
    heartbeat_file = workspace_dir / "HEARTBEAT.md"
    if heartbeat_file.exists():
        return

    default_heartbeat_mds = {
        "zh": """# Heartbeat checklist
- 扫描收件箱紧急邮件
- 查看未来 2h 的日历
- 检查待办是否卡住
- 若安静超过 8h，轻量 check-in
""",
        "en": """# Heartbeat checklist
- Scan inbox for urgent email
- Check calendar for next 2h
- Check tasks for blockers
- Light check-in if quiet for 8h
""",
        "ru": """# Heartbeat checklist
- Проверить входящие на срочные письма
- Просмотреть календарь на ближайшие 2 часа
- Проверить задачи на наличие блокировок
- Лёгкая проверка при отсутствии активности более 8 часов
""",
    }
    heartbeat_content = default_heartbeat_mds.get(
        language,
        default_heartbeat_mds["en"],
    )
    with open(heartbeat_file, "w", encoding="utf-8") as file:
        file.write(heartbeat_content.strip())


def _install_initial_skills(
    workspace_dir: Path,
    skill_names: list[str] | None,
) -> None:
    """Install requested initial skills from the skill pool."""
    if not skill_names:
        return

    pool_service = SkillPoolService()
    for skill_name in skill_names:
        try:
            result = pool_service.download_to_workspace(
                skill_name=skill_name,
                workspace_dir=workspace_dir,
                overwrite=False,
            )
            if result.get("success"):
                continue
            logger.warning(
                "Failed to install initial skill %s for %s: %s",
                skill_name,
                workspace_dir,
                result.get("reason", "unknown"),
            )
        except Exception as e:
            logger.warning(
                "Failed to install initial skill %s for %s: %s",
                skill_name,
                workspace_dir,
                e,
            )


def _initialize_agent_workspace(
    workspace_dir: Path,
    skill_names: list[str] | None = None,
    md_template_id: str | None = None,
    language: str | None = None,
) -> None:
    """Initialize agent workspace with only explicitly requested skills."""
    from ...config import load_config as load_global_config

    (workspace_dir / "sessions").mkdir(exist_ok=True)
    (workspace_dir / "memory").mkdir(exist_ok=True)
    get_workspace_skills_dir(workspace_dir).mkdir(exist_ok=True)

    config = load_global_config()
    if not language:
        language = config.agents.language or "zh"

    _apply_workspace_md_templates(
        workspace_dir,
        language,
        md_template_id=md_template_id,
    )
    _ensure_heartbeat_file(workspace_dir, language)
    _install_initial_skills(workspace_dir, skill_names)

    jobs_file = workspace_dir / "jobs.json"
    if not jobs_file.exists():
        with open(jobs_file, "w", encoding="utf-8") as file:
            json.dump(
                {"version": 1, "jobs": []},
                file,
                ensure_ascii=False,
                indent=2,
            )

    chats_file = workspace_dir / "chats.json"
    if not chats_file.exists():
        with open(chats_file, "w", encoding="utf-8") as file:
            json.dump(
                {"version": 1, "chats": []},
                file,
                ensure_ascii=False,
                indent=2,
            )
