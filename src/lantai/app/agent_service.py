# -*- coding: utf-8 -*-
"""Lantai-owned HTTP facade for agent process endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..utils.logging import setup_logger

logger = setup_logger("info")

MIN_AGENT_TASK_TIMEOUT_SECONDS = 60.0
MAX_AGENT_TASK_TIMEOUT_SECONDS = 3600.0


def _event_to_payload(event: Any) -> Dict[str, Any]:
    if isinstance(event, dict):
        return event
    model_dump = getattr(event, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
    dict_method = getattr(event, "dict", None)
    if callable(dict_method):
        return dict_method()
    return {"data": str(event)}


def _event_to_json(event: Any) -> str:
    model_dump_json = getattr(event, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json()
    json_method = getattr(event, "json", None)
    if callable(json_method):
        return json_method()
    return json.dumps(_event_to_payload(event), ensure_ascii=False, default=str)


def _event_error_message(payload: Dict[str, Any]) -> str | None:
    if payload.get("type") != "error":
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("message")
    return str(error or payload.get("message") or "Agent process failed")


def _meaningful_final_response(
    payload: Dict[str, Any],
    request: Dict[str, Any],
) -> Dict[str, Any] | None:
    output = payload.get("output")
    if isinstance(output, list) and output:
        return payload
    if not (
        payload.get("object") == "message"
        and payload.get("type") == "message"
        and payload.get("role") == "assistant"
        and payload.get("status") == "completed"
        and isinstance(payload.get("content"), list)
    ):
        return None
    return {
        "object": "response",
        "status": "completed",
        "session_id": request.get("session_id"),
        "output": [payload],
    }


class LantaiAgentService:
    """Minimal Lantai process API facade.

    The service deliberately owns only the HTTP facade. Turn execution remains
    delegated to the workspace runner selected by ``DynamicMultiAgentRunner``.
    """

    def __init__(
        self,
        *,
        app_name: str,
        app_description: str,
        runner: Any,
        endpoint_path: str = "/process",
        stream_task_queue: str = "stream_query",
        stream_task_timeout: Optional[float] = None,
    ) -> None:
        self.app_name = app_name
        self.app_description = app_description
        self.runner = runner
        self.endpoint_path = endpoint_path.rstrip("/") or "/process"
        self.stream_task_queue = stream_task_queue
        self.stream_task_timeout = stream_task_timeout
        self.active_tasks: dict[str, dict[str, Any]] = {}
        self.task_locks: dict[str, asyncio.Lock] = {}
        self._idempotent_tasks: dict[str, str] = {}
        self.router = APIRouter()
        self._add_routes()

    def _task_timeout_seconds(self, request: dict[str, Any]) -> float | None:
        """Resolve a caller-owned task deadline without allowing unsafe extremes."""

        metadata = request.get("metadata")
        raw_timeout = (
            metadata.get("agent_task_timeout_seconds")
            if isinstance(metadata, dict)
            else None
        )
        if raw_timeout is None:
            raw_timeout = self.stream_task_timeout
        if raw_timeout is None:
            return None
        try:
            parsed = float(raw_timeout)
        except (TypeError, ValueError):
            parsed = float(self.stream_task_timeout or 900.0)
        return min(
            MAX_AGENT_TASK_TIMEOUT_SECONDS,
            max(MIN_AGENT_TASK_TIMEOUT_SECONDS, parsed),
        )

    async def _stream_generator(self, request: dict[str, Any]):
        try:
            async for chunk in self.runner.stream_query(request):
                yield f"data: {_event_to_json(chunk)}\n\n"
        except Exception as exc:
            logger.error("Agent process stream failed: %s", exc, exc_info=True)
            payload = {"error": str(exc), "type": "error"}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def _run_stream_task(self, task_id: str, request: dict[str, Any]) -> None:
        info = self.active_tasks[task_id]
        task_timeout = self._task_timeout_seconds(request)
        info.update(
            {
                "status": "running",
                "started_at": time.time(),
                "timeout_seconds": task_timeout,
            }
        )

        async def collect_final_response() -> dict[str, Any]:
            final_response: dict[str, Any] = {}
            meaningful_response: dict[str, Any] = {}
            async for event in self.runner.stream_query(request):
                payload = _event_to_payload(event)
                error_message = _event_error_message(payload)
                if error_message:
                    raise RuntimeError(error_message)
                final_response = payload
                candidate = _meaningful_final_response(payload, request)
                if candidate is not None:
                    meaningful_response = candidate
            return meaningful_response or final_response

        try:
            if task_timeout:
                result = await asyncio.wait_for(
                    collect_final_response(),
                    timeout=task_timeout,
                )
            else:
                result = await collect_final_response()
            info.update(
                {
                    "status": "finished",
                    "result": result,
                    "completed_at": time.time(),
                },
            )
        except asyncio.CancelledError:
            info.update(
                {
                    "status": "cancelled",
                    "error": {"message": "Task cancelled"},
                    "cancelled_at": time.time(),
                },
            )
            raise
        except Exception as exc:
            logger.error("Agent background task failed: %s", exc, exc_info=True)
            info.update(
                {
                    "status": "failed",
                    "error": {"message": str(exc)},
                    "failed_at": time.time(),
                },
            )

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        task = self.active_tasks.get(task_id)
        if task is None:
            return {
                "task_id": task_id,
                "status": "not_found",
                "error": {"message": f"Task not found: {task_id}"},
            }
        return {
            key: value
            for key, value in task.items()
            if not key.startswith("_")
        }

    def get_task_by_submission(self, idempotency_key: str) -> dict[str, Any]:
        """Resolve a lost submission receipt without creating another task."""

        task_id = self._idempotent_tasks.get(idempotency_key)
        if not task_id:
            return {"status": "not_found"}
        return self.get_task_status(task_id)

    def create_background_task(self, request: dict[str, Any]) -> str:
        """Start one observable background run and preserve its session key.

        Collaboration clients receive this identity before the child produces
        any output, which lets the console subscribe to the authoritative child
        RuntimeRun instead of guessing from display names.
        """

        metadata = request.get("metadata")
        raw_idempotency_key = (
            metadata.get("background_task_idempotency_key")
            if isinstance(metadata, dict)
            else None
        )
        idempotency_key = str(raw_idempotency_key or "").strip()[:256]
        request_fingerprint = hashlib.sha256(
            json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8"),
        ).hexdigest()
        if idempotency_key:
            existing_task_id = self._idempotent_tasks.get(idempotency_key)
            existing = self.active_tasks.get(existing_task_id or "")
            if existing is not None:
                if existing.get("_request_fingerprint") != request_fingerprint:
                    raise ValueError(
                        "Idempotency key was reused with a different request payload",
                    )
                existing["idempotent_replay"] = True
                return str(existing_task_id)
            if existing_task_id:
                self._idempotent_tasks.pop(idempotency_key, None)

        task_id = str(uuid.uuid4())
        self.active_tasks[task_id] = {
            "task_id": task_id,
            "session_id": str(request.get("session_id") or "").strip(),
            "status": "submitted",
            "queue": self.stream_task_queue,
            "submitted_at": time.time(),
            "message": "Stream query task submitted successfully",
            "idempotent_replay": False,
            "_idempotency_key": idempotency_key,
            "_request_fingerprint": request_fingerprint,
        }
        if idempotency_key:
            self._idempotent_tasks[idempotency_key] = task_id
        self.task_locks[task_id] = asyncio.Lock()
        self.active_tasks[task_id]["_task"] = asyncio.create_task(
            self._run_stream_task(task_id, request),
            name=f"lantai-agent-task-{task_id}",
        )
        return task_id

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel one running background task and return its public status."""

        info = self.active_tasks.get(task_id)
        if info is None:
            return self.get_task_status(task_id)
        if info.get("status") in {"finished", "failed", "cancelled"}:
            return self.get_task_status(task_id)

        running = info.get("_task")
        if isinstance(running, asyncio.Task) and not running.done():
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass
        if info.get("status") not in {"finished", "failed", "cancelled"}:
            info.update(
                {
                    "status": "cancelled",
                    "error": {"message": "Task cancelled"},
                    "cancelled_at": time.time(),
                },
            )
        return self.get_task_status(task_id)

    async def _cleanup_expired_tasks(self) -> int:
        now = time.time()
        ttl_seconds = 3600
        expired = []
        for task_id, info in self.active_tasks.items():
            status = info.get("status")
            if status not in {"finished", "failed", "cancelled"}:
                continue
            finished_at = (
                info.get("completed_at")
                or info.get("failed_at")
                or info.get("cancelled_at")
            )
            if finished_at and (now - finished_at) > ttl_seconds:
                expired.append(task_id)
        for task_id in expired:
            info = self.active_tasks.pop(task_id, None)
            idempotency_key = (
                str(info.get("_idempotency_key") or "")
                if isinstance(info, dict)
                else ""
            )
            if self._idempotent_tasks.get(idempotency_key) == task_id:
                self._idempotent_tasks.pop(idempotency_key, None)
            self.task_locks.pop(task_id, None)
        return len(expired)

    def _add_routes(self) -> None:
        @self.router.get("/")
        async def root():
            endpoints_info = {
                "process": self.endpoint_path,
                "stream": f"{self.endpoint_path}/stream",
                "health": "/health",
                "task": f"{self.endpoint_path}/task",
                "task_status": f"{self.endpoint_path}/task/{{task_id}}",
            }
            return {
                "service": self.app_name,
                "description": self.app_description,
                "endpoints": endpoints_info,
            }

        @self.router.get("/health")
        async def health_check():
            return {
                "status": "healthy",
                "mode": "embedded",
                "runner": "ready" if self.runner is not None else "not_ready",
            }

        @self.router.post(self.endpoint_path)
        async def process(request: dict[str, Any]):
            return StreamingResponse(
                self._stream_generator(request),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        @self.router.post(f"{self.endpoint_path}/task")
        async def submit_stream_query_task(request: dict[str, Any]):
            try:
                task_id = self.create_background_task(request)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return self.get_task_status(task_id)

        @self.router.get(f"{self.endpoint_path}/task/{{task_id}}")
        async def get_stream_query_task_status(task_id: str):
            return self.get_task_status(task_id)

        @self.router.get(
            f"{self.endpoint_path}/task/submission/{{idempotency_key}}",
        )
        async def get_stream_query_task_by_submission(idempotency_key: str):
            return self.get_task_by_submission(idempotency_key)

        @self.router.post(f"{self.endpoint_path}/task/{{task_id}}/cancel")
        async def cancel_stream_query_task(task_id: str):
            return await self.cancel_task(task_id)

        @self.router.post("/shutdown")
        async def shutdown_process_simple():
            async def delayed_shutdown() -> None:
                await asyncio.sleep(0.5)
                os.kill(os.getpid(), signal.SIGTERM)

            asyncio.create_task(delayed_shutdown())
            return {"status": "shutting down"}

        @self.router.post("/admin/shutdown")
        async def shutdown_process():
            async def delayed_shutdown() -> None:
                await asyncio.sleep(1)
                os.kill(os.getpid(), signal.SIGTERM)

            asyncio.create_task(delayed_shutdown())
            return {"message": "Shutdown initiated"}

        @self.router.get("/admin/status")
        async def get_process_status():
            try:
                import psutil

                proc = psutil.Process(os.getpid())
                return {
                    "pid": os.getpid(),
                    "status": proc.status(),
                    "memory_usage": proc.memory_info().rss,
                    "cpu_percent": proc.cpu_percent(),
                    "uptime": proc.create_time(),
                }
            except Exception as exc:
                return {
                    "pid": os.getpid(),
                    "status": "unknown",
                    "error": str(exc),
                }


__all__ = ["LantaiAgentService"]
