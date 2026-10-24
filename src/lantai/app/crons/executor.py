# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from .models import CronJobSpec
from ...utils.async_settlement import await_settled

logger = logging.getLogger(__name__)


class CronExecutor:
    def __init__(
        self,
        *,
        runner: Any,
        channel_manager: Any,
        agent_id: str | None = None,
    ):
        self._runner = runner
        self._channel_manager = channel_manager
        self._agent_id = agent_id or "default"

    async def execute(self, job: CronJobSpec) -> None:
        """Execute one job once.

        - task_type text: send fixed text to channel
        - task_type agent: ask agent with prompt, send reply to channel (
            stream_query + send_event)
        """
        target_user_id = job.dispatch.target.user_id
        target_session_id = job.dispatch.target.session_id
        target_channel = job.dispatch.channel
        dispatch_meta: Dict[str, Any] = dict(job.dispatch.meta or {})
        dispatch_meta.setdefault("push_source_kind", "cron_job")
        dispatch_meta.setdefault("push_source_id", job.id)
        dispatch_meta.setdefault("push_source_name", job.name)
        logger.info(
            "cron execute: job_id=%s channel=%s task_type=%s "
            "target_user_id=%s target_session_id=%s",
            job.id,
            target_channel,
            job.task_type,
            target_user_id[:40] if target_user_id else "",
            target_session_id[:40] if target_session_id else "",
        )

        if job.task_type == "text" and job.text:
            logger.info(
                "cron send_text: job_id=%s channel=%s len=%s",
                job.id,
                target_channel,
                len(job.text or ""),
            )
            await self._channel_manager.send_text(
                channel=target_channel,
                user_id=target_user_id,
                session_id=target_session_id,
                text=job.text.strip(),
                meta=dispatch_meta,
            )
            return

        if job.task_type == "capability":
            assert job.capability_request is not None
            from ..capabilities.models import CapabilityRunRequest
            from ..capabilities.repo import CapabilityRepository
            from ..capabilities.service import CapabilityService

            workspace_dir = getattr(self._runner, "workspace_dir", None)
            if workspace_dir is None:
                raise RuntimeError("capability cron requires runner.workspace_dir")

            service = CapabilityService(CapabilityRepository(workspace_dir))
            await service.run_capability(
                job.capability_request.capability_id,
                CapabilityRunRequest(
                    parameters=job.capability_request.parameters,
                    session_id=target_session_id or f"cron:{job.id}",
                    user_id=target_user_id or "cron",
                    channel=target_channel,
                ),
                runner=self._runner,
                workspace_dir=str(workspace_dir),
                agent_id=self._agent_id,
                channel_manager=self._channel_manager,
                source="cron",
            )
            return

        if job.task_type == "pipeline":
            assert job.pipeline_request is not None
            from ..capabilities.models import PipelineRunRequest
            from ..capabilities.repo import CapabilityRepository
            from ..capabilities.service import CapabilityService

            workspace_dir = getattr(self._runner, "workspace_dir", None)
            if workspace_dir is None:
                raise RuntimeError("pipeline cron requires runner.workspace_dir")

            service = CapabilityService(CapabilityRepository(workspace_dir))
            await service.run_pipeline(
                job.pipeline_request.pipeline_id,
                PipelineRunRequest(
                    parameters=job.pipeline_request.parameters,
                    session_id=target_session_id or f"cron:{job.id}",
                    user_id=target_user_id or "cron",
                    channel=target_channel,
                ),
                runner=self._runner,
                workspace_dir=str(workspace_dir),
                agent_id=self._agent_id,
                channel_manager=self._channel_manager,
                source="cron",
            )
            return

        # agent: run request as the dispatch target user so context matches
        logger.info(
            "cron agent: job_id=%s channel=%s stream_query then send_event",
            job.id,
            job.dispatch.channel,
        )
        assert job.request is not None
        req: Dict[str, Any] = job.request.model_dump(mode="json")

        req["channel"] = target_channel
        req["user_id"] = target_user_id or "cron"

        # Determine session_id based on share_session
        share_session = job.runtime.share_session
        if share_session:
            req["session_id"] = target_session_id or f"cron:{job.id}"
        else:
            req["session_id"] = (
                f"{target_session_id}:cron:{job.id}"
                if target_session_id
                else f"cron:{job.id}"
            )

        async def _run() -> None:
            async for event in self._runner.stream_query(req):
                await self._channel_manager.send_event(
                    channel=target_channel,
                    user_id=target_user_id,
                    session_id=target_session_id,
                    event=event,
                    meta=dispatch_meta,
                )

        run_task = asyncio.create_task(_run(), name=f"cron-agent-{job.id}")
        try:
            await asyncio.wait_for(
                asyncio.shield(run_task),
                timeout=job.runtime.timeout_seconds,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as interruption:
            # wait_for owns only the shield. Cancel actual work once, then
            # drain it even if external cancellation overlaps timeout cleanup.
            if not run_task.done():
                run_task.cancel()

            async def drain() -> None:
                await asyncio.gather(run_task, return_exceptions=True)
                if not run_task.cancelled():
                    error = run_task.exception()
                    if error is not None:
                        logger.error(
                            "cron agent cleanup failed: job_id=%s error_type=%s",
                            job.id,
                            type(error).__name__,
                        )

            await await_settled(asyncio.create_task(
                drain(), name=f"cron-agent-cleanup-{job.id}",
            ))
            if isinstance(interruption, asyncio.TimeoutError):
                logger.warning(
                    "cron execute: job_id=%s timed out after %ss",
                    job.id,
                    job.runtime.timeout_seconds,
                )
            else:
                logger.info("cron execute: job_id=%s cancelled", job.id)
            raise
