# -*- coding: utf-8 -*-
"""API routers."""

from fastapi import APIRouter

from .agents import router as agents_router
from .config import router as config_router
from .local_models import router as local_models_router
from .providers import router as providers_router
from .skills import router as skills_router
from .skills_stream import router as skills_stream_router
from .wiki import router as wiki_router
from .workspace import router as workspace_router
from .research import router as research_router
from .envs import router as envs_router
from .mcp import router as mcp_router
from .tools import router as tools_router
from ..crons.api import router as cron_router
from ..runner.api import router as runner_router
from .console import router as console_router
from .token_usage import router as token_usage_router
from .agent_stats import router as agent_stats_router
from .auth import router as auth_router
from .cloud_account import router as cloud_account_router
from .messages import router as messages_router
from .files import router as files_router
from .settings import router as settings_router
from .plugins import router as plugins_router
from .backup import router as backup_router
from .plan import router as plan_router
from .runtime import router as runtime_router
from .computer_permissions import router as computer_permissions_router
from .mac_use import router as mac_use_router
from .browser_pairing import router as browser_pairing_router
from .browser_workbench import router as browser_workbench_router
from .office_documents import router as office_documents_router
from .coding import router as coding_router
from .utility_intelligence import router as utility_intelligence_router
from .welcome_greetings import router as welcome_greetings_router
from .image_generation import router as image_generation_router
from .video_generation import router as video_generation_router
from .web_search import (
    router as web_search_router,
    legacy_router as legacy_web_search_router,
)
from .work_methods import router as work_methods_router
from .docx_double_write import router as docx_double_write_router
from .works import router as works_router
from .reading import router as reading_router
from .customization import router as customization_router
from .presentations import router as presentations_router
from .private_editor import router as private_editor_router
from ..capabilities.api import (
    capabilities_router,
    pipelines_router,
    task_runs_router,
)
from ..reminders.api import router as reminders_router
from ..daily_brief.api import router as daily_brief_router
from ..time_system.api import router as time_system_router
from ..workspace_grants import workspace_grants_router

router = APIRouter()

router.include_router(agents_router)
router.include_router(config_router)
router.include_router(console_router)
router.include_router(cron_router)
router.include_router(local_models_router)
router.include_router(mcp_router)
router.include_router(messages_router)
router.include_router(providers_router)
router.include_router(runner_router)
router.include_router(skills_router)
router.include_router(skills_stream_router)
router.include_router(tools_router)
router.include_router(wiki_router)
router.include_router(workspace_router)
router.include_router(research_router)
router.include_router(envs_router)
router.include_router(token_usage_router)
router.include_router(agent_stats_router)
router.include_router(auth_router)
router.include_router(cloud_account_router)
router.include_router(files_router)
router.include_router(settings_router)
router.include_router(plugins_router)
router.include_router(backup_router)
router.include_router(plan_router)
router.include_router(runtime_router)
router.include_router(computer_permissions_router)
router.include_router(mac_use_router)
router.include_router(browser_pairing_router)
router.include_router(browser_workbench_router)
router.include_router(office_documents_router)
router.include_router(coding_router)
router.include_router(utility_intelligence_router)
router.include_router(welcome_greetings_router)
router.include_router(image_generation_router)
router.include_router(video_generation_router)
router.include_router(web_search_router)
router.include_router(legacy_web_search_router)
router.include_router(work_methods_router)
router.include_router(docx_double_write_router)
router.include_router(works_router)
router.include_router(reading_router)
router.include_router(customization_router)
router.include_router(presentations_router)
router.include_router(private_editor_router)
router.include_router(reminders_router)
router.include_router(daily_brief_router)
router.include_router(time_system_router)
router.include_router(capabilities_router)
router.include_router(pipelines_router)
router.include_router(task_runs_router)
router.include_router(workspace_grants_router)


def create_agent_scoped_router() -> APIRouter:
    """Create agent-scoped router that wraps existing routers.

    Returns:
        APIRouter with all routers mounted under /agents/{agentId}/
    """
    from .agent_scoped import create_agent_scoped_router as _create

    return _create()


__all__ = ["router", "create_agent_scoped_router"]
