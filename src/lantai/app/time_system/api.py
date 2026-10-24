# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import APIRouter, Request

from ...config import load_config
from ..agent_context import get_agent_for_request
from ..reminders.repo import ReminderRepository
from ..reminders.service import ReminderService
from .models import TimeItem, TodayTimeline
from .service import TimeSystemService

router = APIRouter(prefix="/time", tags=["time"])


async def _service_for_request(request: Request) -> TimeSystemService:
    workspace = await get_agent_for_request(request)

    async def automation_provider() -> list[TimeItem]:
        manager = getattr(workspace, "cron_manager", None)
        if manager is None:
            return []
        try:
            jobs = await manager.list_jobs()
        except Exception:
            return []
        items: list[TimeItem] = []
        for job in jobs:
            if not job.enabled or not job.id:
                continue
            state = manager.get_state(job.id)
            next_run = getattr(state, "next_run_at", None)
            if next_run is None:
                continue
            items.append(
                TimeItem(
                    id=f"cron:{job.id}",
                    kind="automation",
                    source="lantai",
                    source_label="自动化",
                    title=job.name,
                    summary=job.text or "",
                    start_at=next_run,
                    blocking=False,
                    status="scheduled",
                    action_path="/cron-jobs",
                    metadata={
                        "cron": job.schedule.cron,
                        "timezone": job.schedule.timezone,
                    },
                )
            )
        return items

    timezone_name = load_config().user_timezone or "UTC"
    return TimeSystemService(
        reminder_service=ReminderService(
            ReminderRepository(workspace.workspace_dir)
        ),
        timezone_name=timezone_name,
        automation_provider=automation_provider,
    )


@router.get("/today", response_model=TodayTimeline)
async def get_today(request: Request):
    return await (await _service_for_request(request)).today()
