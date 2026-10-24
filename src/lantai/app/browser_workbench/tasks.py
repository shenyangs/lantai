# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ...utils.async_settlement import await_settled
from ...constant import WORKING_DIR
from ..concurrency import (
    ConcurrencyAdmissionError,
    get_runtime_concurrency_governor,
)
from ..capabilities.policy import is_app_store_distribution
from ...security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)
from ...runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from ...runtime.runs import RuntimeEffectRecord, RuntimeRunOwner
from ...runtime.store import (
    RuntimeRunStoreConflictError,
    RuntimeRunStoreError,
)
from ...runtime.sanitizer import sanitize_runtime_payload
from .agent import (
    BootstrapPlannerAdapter,
    BrowserAgentContext,
    BrowserAgentLoop,
    BrowserAgentUsage,
    BrowserExpectedOutcome,
    BrowserPageState,
    DeterministicPlannerAdapter,
    ModelPlannerAdapter,
    browser_agent_budget,
    normalize_browser_observation,
)
from .agent.verification import BrowserOutcomeVerifier
from .agent.safety import redact_untrusted_text, redact_untrusted_value
from .effects import browser_action_effect_recovery
from .broker import BrowserActionBroker
from .models import (
    BrowserAction,
    BrowserObservation,
    BrowserTaskConfirmation,
    BrowserTaskEvent,
    BrowserTaskRequest,
    BrowserTaskRun,
    BrowserTaskStatus,
    BrowserTakeoverReceipt,
    utc_now_iso,
)
from .planner import BrowserTaskPlanner
from .runtime_runs import (
    record_browser_task_runtime_run_control,
    record_browser_task_runtime_run_settled,
    record_browser_task_runtime_run_started,
    record_browser_task_runtime_takeover_resumed,
    record_browser_task_runtime_step,
    touch_browser_task_runtime_run_heartbeat,
)


_logger = logging.getLogger(__name__)


_STOPPED_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "manual_takeover_required",
}
_ACTIVE_STATUSES = {
    "queued",
    "running_background",
    "waiting_for_user_confirmation",
}
_BROWSER_TASK_HEARTBEAT_INTERVAL_SECONDS = 10.0


async def _browser_task_runtime_heartbeat_loop(
    run: BrowserTaskRun,
    *,
    interval_seconds: float,
) -> None:
    if interval_seconds <= 0:
        return
    while True:
        await asyncio.sleep(interval_seconds)
        touched = await touch_browser_task_runtime_run_heartbeat(run)
        if touched is None:
            return


class BrowserTaskStore:
    """Durable local task ledger for browser task runs and events."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.workspace_dir = Path(root_dir or WORKING_DIR).expanduser()
        self.root_dir = self.workspace_dir / "browser_workbench" / "tasks"
        self.canonical_root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
            "browser_tasks",
        )
        self._lock = threading.Lock()

    def load_all(
        self,
    ) -> list[
        tuple[BrowserTaskRun, list[BrowserTaskEvent], list[BrowserAction], int]
    ]:
        out: list[
            tuple[
                BrowserTaskRun,
                list[BrowserTaskEvent],
                list[BrowserAction],
                int,
            ]
        ] = []
        seen_run_ids: set[str] = set()
        for path in self._iter_canonical_paths():
            loaded = self._load_payload_path(
                path,
                require_canonical_path_identity=True,
            )
            if loaded is None:
                continue
            run, events, actions, next_index = loaded
            seen_run_ids.add(run.run_id)
            out.append((run, events, actions, next_index))
        for path in self._iter_legacy_paths():
            loaded = self._load_payload_path(path)
            if loaded is None:
                continue
            run, events, actions, next_index = loaded
            if run.run_id in seen_run_ids:
                continue
            out.append((run, events, actions, next_index))
        return out

    def save(
        self,
        run: BrowserTaskRun,
        events: list[BrowserTaskEvent],
        actions: list[BrowserAction],
        next_index: int,
    ) -> Path:
        with self._lock:
            path = self._canonical_path_for_run_id(run.run_id)
            payload = self._payload_for_storage(
                run,
                events,
                actions,
                next_index,
            )
            write_text_atomic_no_follow(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )
            return path

    def _iter_canonical_paths(self) -> list[Path]:
        if not self.canonical_root.is_dir():
            return []
        return sorted(
            path
            for path in self.canonical_root.glob("*.json")
            if path.is_file() and not path.is_symlink()
        )

    def _iter_legacy_paths(self) -> list[Path]:
        if not self.root_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.root_dir.glob("*.json")
            if path.is_file() and not path.is_symlink()
        )

    def _canonical_path_for_run_id(self, run_id: str) -> Path:
        filename = safe_storage_key(
            "browser-task-state",
            run_id,
            suffix=".json",
        )
        return contained_storage_path(self.canonical_root, filename)

    def _load_payload_path(
        self,
        path: Path,
        *,
        require_canonical_path_identity: bool = False,
    ) -> (
        tuple[BrowserTaskRun, list[BrowserTaskEvent], list[BrowserAction], int]
        | None
    ):
        try:
            payload = json.loads(read_text_no_follow(path, "utf-8"))
            run = BrowserTaskRun.model_validate(payload.get("run") or {})
            if (
                require_canonical_path_identity
                and not self._matches_canonical_run_path(
                    path,
                    run.run_id,
                )
            ):
                return None
            events = [
                BrowserTaskEvent.model_validate(item)
                for item in payload.get("events") or []
                if isinstance(item, dict)
            ]
            actions = [
                BrowserAction.model_validate(item)
                for item in payload.get("actions") or []
                if isinstance(item, dict)
            ]
            next_index = int(
                payload.get("next_index", run.completed_action_count) or 0,
            )
        except Exception:
            return None
        return run, events, actions, next_index

    def _matches_canonical_run_path(self, path: Path, run_id: str) -> bool:
        expected = self._canonical_path_for_run_id(run_id)
        return path.resolve(strict=False) == expected.resolve(strict=False)

    def _payload_for_storage(
        self,
        run: BrowserTaskRun,
        events: list[BrowserTaskEvent],
        actions: list[BrowserAction],
        next_index: int,
    ) -> dict[str, Any]:
        sanitized_run = self._sanitize_run_for_storage(run)
        return {
            "schema": "com.lantai.browser-task-state.v1",
            "version": 1,
            "run": sanitized_run.model_dump(mode="json"),
            "events": [
                self._sanitize_event_for_storage(event).model_dump(mode="json")
                for event in events
            ],
            "actions": [
                self._sanitize_action_for_storage(action).model_dump(
                    mode="json",
                )
                for action in actions
            ],
            "next_index": next_index,
            "safety": {
                "local_only": True,
                "cookie_exported": False,
                "password_exported": False,
                "replay_is_source_of_audit_truth": False,
                "canonical_runtime_sidecar": True,
                "legacy_browser_task_state_writer": (
                    "stopped_read_compatibility"
                ),
            },
        }

    @staticmethod
    def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        return (
            sanitize_runtime_payload(dict(metadata or {})) if metadata else {}
        )

    @classmethod
    def _sanitize_action_for_storage(
        cls,
        action: BrowserAction,
    ) -> BrowserAction:
        metadata = {
            key: value
            for key, value in action.metadata.items()
            if key not in {"fields_json", "paths_json", "values_json"}
        }
        safe_metadata = cls._sanitize_metadata(metadata)
        if action.text:
            safe_metadata["stored_text"] = {
                "redacted": True,
                "length": len(action.text),
            }
        if action.path:
            safe_metadata["stored_path_name"] = Path(action.path).name
        return action.model_copy(
            update={
                "text": "",
                "code": "",
                "path": "",
                "reason": redact_untrusted_text(action.reason, limit=2000),
                "metadata": safe_metadata,
            },
        )

    @classmethod
    def _sanitize_event_for_storage(
        cls,
        event: BrowserTaskEvent,
    ) -> BrowserTaskEvent:
        return event.model_copy(
            update={
                "message": redact_untrusted_text(event.message, limit=2000),
                "metadata": cls._sanitize_metadata(event.metadata),
            },
        )

    @classmethod
    def _sanitize_run_for_storage(cls, run: BrowserTaskRun) -> BrowserTaskRun:
        pending_confirmation = (
            cls._sanitize_action_for_storage(run.pending_confirmation)
            if run.pending_confirmation is not None
            else None
        )
        result = (
            sanitize_runtime_payload(dict(run.result)) if run.result else {}
        )
        return run.model_copy(
            update={
                "instruction": redact_untrusted_text(
                    run.instruction,
                    limit=12_000,
                ),
                "metadata": cls._sanitize_metadata(run.metadata),
                "pending_confirmation": pending_confirmation,
                "result": result,
            },
        )

    # pylint: disable=R0912
    def prune(
        self,
        *,
        max_runs: int = 200,
        max_age_days: int = 30,
        include_active: bool = False,
    ) -> dict[str, Any]:
        max_runs = max(1, int(max_runs or 200))
        max_age_days = max(1, int(max_age_days or 30))
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        records: list[tuple[Path, BrowserTaskRun]] = []
        seen_run_ids: set[str] = set()
        for path in self._iter_canonical_paths():
            loaded = self._load_payload_path(
                path,
                require_canonical_path_identity=True,
            )
            if loaded is None:
                continue
            run, _events, _actions, _next_index = loaded
            if run.run_id in seen_run_ids:
                continue
            seen_run_ids.add(run.run_id)
            records.append((path, run))
        for path in self._iter_legacy_paths():
            loaded = self._load_payload_path(path)
            if loaded is None:
                continue
            run, _events, _actions, _next_index = loaded
            if run.run_id in seen_run_ids:
                continue
            seen_run_ids.add(run.run_id)
            records.append((path, run))

        removable: set[Path] = set()
        for path, run in records:
            if not include_active and run.status not in _STOPPED_STATUSES:
                continue
            updated = self._parse_ts(run.updated_at) or self._parse_ts(
                run.created_at,
            )
            if updated and updated < cutoff:
                removable.add(path)

        kept_by_recency = sorted(
            records,
            key=lambda item: item[1].updated_at or item[1].created_at,
            reverse=True,
        )[:max_runs]
        kept_paths = {path for path, _run in kept_by_recency}
        for path, run in records:
            if path in kept_paths:
                continue
            if include_active or run.status in _STOPPED_STATUSES:
                removable.add(path)

        removed_run_ids: list[str] = []
        for path, run in records:
            if path not in removable:
                continue
            try:
                path.unlink()
                removed_run_ids.append(run.run_id)
            except OSError:
                continue
        return {
            "ok": True,
            "removed_count": len(removed_run_ids),
            "removed_run_ids": removed_run_ids,
            "max_runs": max_runs,
            "max_age_days": max_age_days,
            "include_active": include_active,
        }

    # pylint: enable=R0912

    @staticmethod
    def _parse_ts(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


class BrowserTaskRuntime:
    """Background task runner for controlled browser actions."""

    def __init__(
        self,
        *,
        broker: BrowserActionBroker | None = None,
        app_store_distribution: Callable[[], bool] = is_app_store_distribution,
        store: BrowserTaskStore | None = None,
        effect_journal: RuntimeEffectJournal | None = None,
        max_concurrent_runs: int = 4,
        planner: BrowserTaskPlanner | None = None,
        agent_loop_settings: Callable[[], dict[str, Any]] | None = None,
        model_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.broker = broker or BrowserActionBroker()
        self.app_store_distribution = app_store_distribution
        self.store = store
        self.effect_journal = effect_journal
        self.max_concurrent_runs = max(1, max_concurrent_runs)
        self.planner = planner or BrowserTaskPlanner()
        self.agent_loop_settings = agent_loop_settings or (lambda: {})
        self.model_client_factory = model_client_factory
        self._runs: dict[str, BrowserTaskRun] = {}
        self._events: dict[str, list[BrowserTaskEvent]] = {}
        self._actions: dict[str, list[BrowserAction]] = {}
        self._next_index: dict[str, int] = {}
        self._handles: dict[str, asyncio.Task[None]] = {}
        self._runtime_controls: dict[str, asyncio.Task[None]] = {}
        self._pause_requested: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._write_locks: dict[tuple[str, str], str] = {}
        self._agent_resume_actions: dict[str, list[BrowserAction]] = {}
        self._restore_from_store()

    def list_runs(self) -> list[BrowserTaskRun]:
        return sorted(
            self._runs.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def get_run(self, run_id: str) -> BrowserTaskRun | None:
        return self._runs.get(run_id)

    def list_events(
        self,
        run_id: str,
        *,
        after_id: str = "",
    ) -> list[BrowserTaskEvent]:
        events = list(self._events.get(run_id, []))
        if not after_id:
            return events
        for index, event in enumerate(events):
            if event.id == after_id:
                return events[index + 1 :]
        return events

    async def create(self, request: BrowserTaskRequest) -> BrowserTaskRun:
        safe_instruction = redact_untrusted_text(
            request.instruction,
            limit=12_000,
        )
        raw_safe_metadata = redact_untrusted_value(request.metadata)
        safe_request_metadata = sanitize_runtime_payload(
            raw_safe_metadata if isinstance(raw_safe_metadata, dict) else {},
        )
        agent_loop = self._agent_loop_config(request, {})
        planning_request = (
            request.model_copy(
                update={
                    "instruction": safe_instruction,
                    "metadata": safe_request_metadata,
                },
            )
            if agent_loop["enabled"]
            else request
        )
        plan = self.planner.plan(planning_request)
        actions = plan.actions
        agent_loop["planner_strategy"] = str(
            plan.metadata.get("strategy") or "",
        )
        request_budget = browser_agent_budget(agent_loop["budget_profile"])
        effective_max_actions = min(
            request.max_actions,
            request_budget.max_actions,
        )
        effective_max_duration = min(
            request.max_duration_seconds,
            request_budget.max_duration_seconds,
        )
        metadata = {
            **safe_request_metadata,
            "plan": sanitize_runtime_payload(
                redact_untrusted_value(plan.metadata)  # type: ignore[arg-type]
                if isinstance(plan.metadata, dict)
                else {},
            ),
            "budget": {
                "max_actions": effective_max_actions
                if agent_loop["enabled"]
                else request.max_actions,
                "max_duration_seconds": effective_max_duration
                if agent_loop["enabled"]
                else request.max_duration_seconds,
            },
            "agent_loop": agent_loop,
            "start_url": redact_untrusted_text(request.url, limit=4096),
            "task_group_id": safe_request_metadata.get("task_group_id")
            or f"task-{request.run_id}",
        }
        if (
            self.store is not None
            and not str(metadata.get("workspace_dir") or "").strip()
        ):
            metadata["workspace_dir"] = str(self.store.workspace_dir)
        run = BrowserTaskRun(
            run_id=request.run_id,
            profile_id=request.profile_id,
            target_domains=request.target_domains,
            access=request.access,
            mode=request.mode,
            instruction=safe_instruction,
            replay_run_id=request.run_id,
            planned_action_count=(
                effective_max_actions
                if agent_loop["enabled"]
                else len(actions)
            ),
            requires_foreground_confirmation=(
                request.requires_foreground_confirmation
            ),
            metadata=metadata,
        )
        self._runs[run.run_id] = run
        self._actions[run.run_id] = actions
        self._next_index[run.run_id] = 0
        await record_browser_task_runtime_run_started(
            run,
            app_store_distribution=self.app_store_distribution(),
        )
        self._append_event(run, "task_created", "Browser task queued.")

        async def finish_before_schedule(
            status: BrowserTaskStatus,
            message: str,
            error_code: str,
            *,
            recovery: dict[str, Any] | None = None,
        ) -> BrowserTaskRun:
            self._finish(
                run,
                status,
                message,
                error_code,
                recovery=recovery,
            )
            await record_browser_task_runtime_run_settled(
                run,
                app_store_distribution=self.app_store_distribution(),
            )
            return run

        if self.app_store_distribution() and run.access != "read":
            return await finish_before_schedule(
                "failed",
                "App Store distribution blocks browser task automation.",
                "app_store_browser_task_blocked",
            )

        if len(actions) > request.max_actions and not agent_loop["enabled"]:
            return await finish_before_schedule(
                "failed",
                "Browser task exceeded its action budget.",
                "browser_task_action_budget_exceeded",
            )

        if self._active_run_count() > self.max_concurrent_runs:
            return await finish_before_schedule(
                "failed",
                "Too many browser tasks are already active.",
                "browser_task_concurrency_limit_reached",
            )

        policy_block = self._preflight_policy_block(run, actions)
        if policy_block is not None:
            action, decision = policy_block
            error_code = (
                decision.matched_rule_id or "browser_task_policy_blocked"
            )
            return await finish_before_schedule(
                "failed",
                decision.reason or "Browser task blocked by policy.",
                error_code,
                recovery={
                    "kind": "policy_blocked",
                    "error_code": error_code,
                    "matched_rule_id": decision.matched_rule_id,
                    "action": action.action,
                    "recoverable": False,
                    "manual_takeover_required": False,
                    "suggestion": (
                        (
                            "Review the Browser Workbench capability "
                            "policy before retrying."
                        )
                    ),
                },
            )

        lock_keys = self._write_lock_keys(run, actions)
        conflict_key = self._write_lock_conflict(run.profile_id, lock_keys)
        if conflict_key:
            return await finish_before_schedule(
                "failed",
                (
                    "Another browser write task is already using "
                    "this profile/domain."
                ),
                "browser_task_write_lock_conflict",
            )
        if lock_keys:
            for lock_key in lock_keys:
                self._write_locks[lock_key] = run.run_id
            run.metadata["write_locks"] = [
                {"profile_id": key[0], "domain": key[1]} for key in lock_keys
            ]
            run.metadata["write_lock"] = run.metadata["write_locks"][0]

        self._schedule(run.run_id)
        return run

    def pause(self, run_id: str) -> BrowserTaskRun:
        run = self._require_run(run_id)
        if run.status not in {"completed", "failed", "cancelled"}:
            self._pause_requested.add(run_id)
            run.status = "paused"
            run.current_action = "等待用户继续"
            run.updated_at = utc_now_iso()
            self._append_event(
                run,
                "task_paused",
                "Browser task paused by user.",
            )
            self._schedule_runtime_control(run, "pause")
        return run

    def resume(self, run_id: str) -> BrowserTaskRun:
        run = self._require_run(run_id)
        if run.status == "paused":
            self._pause_requested.discard(run_id)
            run.status = "queued"
            run.current_action = ""
            run.updated_at = utc_now_iso()
            self._append_event(
                run,
                "task_resumed",
                "Browser task resumed by user.",
            )
            self._schedule_runtime_control(run, "resume")
            self._schedule(run_id)
        return run

    def cancel(self, run_id: str) -> BrowserTaskRun:
        run = self._require_run(run_id)
        if run.status not in {"completed", "failed", "cancelled"}:
            self._cancel_requested.add(run_id)
            self._finish(
                run,
                "cancelled",
                "Browser task cancelled by user.",
                "browser_task_cancelled",
            )
            self._schedule_runtime_settlement(run)
        return run

    async def pause_canonical(self, run_id: str) -> BrowserTaskRun:
        run = self.pause(run_id)
        await self._drain_runtime_controls(run.run_id)
        return run

    async def request_takeover(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 12.0,
    ) -> BrowserTakeoverReceipt:
        """Pause and prove the active task loop reached a safe handoff
        point.
        """
        run = self._require_run(run_id)
        if run.status not in {"paused", "manual_takeover_required"}:
            run = await self.pause_canonical(run_id)
        handle = self._handles.get(run_id)
        if handle is not None and not handle.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(handle),
                    timeout=max(0.001, min(float(timeout_seconds), 30.0)),
                )
            except asyncio.TimeoutError:
                return BrowserTakeoverReceipt(
                    ready=False,
                    run=run,
                    error_code="browser_takeover_action_still_running",
                    message=(
                        "The current browser action has not reached a safe "
                        "handoff point yet."
                    ),
                )
            except Exception:
                # The task loop owns failure settlement. Re-read the canonical
                # run below instead of projecting the task exception through
                # this handoff request.
                pass
        run = self._require_run(run_id)
        ambiguous = self._ambiguous_effects(run, self._attempt_id_for_run(run))
        if ambiguous:
            return BrowserTakeoverReceipt(
                ready=False,
                run=run,
                error_code="browser_takeover_effect_ambiguous",
                message=(
                    "A browser effect is still ambiguous; "
                    "inspect the page before takeover."
                ),
            )
        ready = run.status in {"paused", "manual_takeover_required"}
        return BrowserTakeoverReceipt(
            ready=ready,
            run=run,
            error_code="" if ready else "browser_takeover_not_ready",
            message=(
                "Browser control is ready for the user."
                if ready
                else "Browser task is not in a takeover-ready state."
            ),
        )

    async def resume_canonical(self, run_id: str) -> BrowserTaskRun:
        run = self._require_run(run_id)
        if run.status == "manual_takeover_required":
            previous_attempt_id = self._attempt_id_for_run(run)
            ambiguous = self._ambiguous_effects(run, previous_attempt_id)
            if ambiguous:
                run.result = {
                    **run.result,
                    "message": (
                        (
                            "Cannot return control while a prior browser "
                            "Effect is ambiguous; inspect external state "
                            "before retrying."
                        )
                    ),
                    "ambiguous_effect_ids": ambiguous,
                }
                run.error_code = "browser_task_ambiguous_effect_requires_probe"
                run.updated_at = utc_now_iso()
                self._append_event(
                    run,
                    "task_takeover_resume_blocked",
                    str(run.result["message"]),
                    metadata={"ambiguous_effect_ids": ambiguous},
                )
                return run
            run.metadata["takeover_count"] = (
                int(run.metadata.get("takeover_count") or 0) + 1
            )
            lock_keys = self._write_lock_keys(
                run,
                self._actions.get(run_id, []),
            )
            conflict = self._write_lock_conflict(run.profile_id, lock_keys)
            if (
                conflict is not None
                and self._write_locks.get(conflict) != run_id
            ):
                run.error_code = "browser_task_write_lock_conflict"
                run.result = {
                    **run.result,
                    "message": (
                        (
                            "Cannot return browser control while another "
                            "write task owns this profile/domain."
                        )
                    ),
                }
                run.updated_at = utc_now_iso()
                self._append_event(
                    run,
                    "task_takeover_resume_blocked",
                    str(run.result["message"]),
                    metadata={
                        "lock_profile_id": conflict[0],
                        "lock_domain": conflict[1],
                    },
                )
                return run
            contract = await record_browser_task_runtime_takeover_resumed(run)
            if contract is None and str(
                run.metadata.get("workspace_dir") or "",
            ):
                run.error_code = "browser_task_takeover_attempt_failed"
                return run
            if contract is not None:
                run.metadata["attempt_id"] = contract.attempt_id
            run.metadata["agent_action_count"] = 0
            run.metadata["agent_phase"] = "observing"
            run.pending_confirmation = None
            run.error_code = ""
            run.result = {}
            run.status = "queued"
            run.current_action = "重新观察接管后的页面"
            run.updated_at = utc_now_iso()
            self._cancel_requested.discard(run_id)
            self._pause_requested.discard(run_id)
            for key in lock_keys:
                self._write_locks[key] = run.run_id
            self._append_event(
                run,
                "task_takeover_returned",
                (
                    "User returned browser control; a new "
                    "Attempt will observe before acting."
                ),
                metadata={
                    "previous_attempt_id": previous_attempt_id,
                    "attempt_id": self._attempt_id_for_run(run),
                },
            )
            self._schedule(run_id)
            return run
        run = self.resume(run_id)
        await self._drain_runtime_controls(run.run_id)
        return run

    async def cancel_canonical(self, run_id: str) -> BrowserTaskRun:
        run = self.cancel(run_id)
        await self._drain_runtime_controls(run.run_id)
        return run

    async def confirm(
        self,
        run_id: str,
        confirmation: BrowserTaskConfirmation,
    ) -> BrowserTaskRun:
        run = self._require_run(run_id)
        pending = run.pending_confirmation
        if run.status != "waiting_for_user_confirmation" or pending is None:
            return run
        if not confirmation.approved:
            observation = self.broker.deny_confirmation(
                pending,
                reason=confirmation.reason
                or "User denied browser task action.",
            )
            self._append_observation_event(
                run,
                observation,
                "task_confirmation_denied",
                "User denied browser task confirmation.",
            )
            self._finish(
                run,
                "cancelled",
                "Browser task stopped after user denied confirmation.",
                "browser_task_confirmation_denied",
            )
            return run

        confirmation_target = (
            self._prepare_action(run, pending)
            if self._agent_loop_enabled(run)
            else pending
        )
        confirm_action = getattr(self.broker, "confirm_action", None)
        if callable(confirm_action):
            approved_action = confirm_action(
                confirmation_target,
                reason=confirmation.reason,
            )
        else:
            approved_action = confirmation_target.model_copy(
                update={
                    "confirmed": True,
                    "metadata": {
                        **pending.metadata,
                        "confirmation_result": "approved",
                        "confirmation_reason": confirmation.reason,
                    },
                },
            )
        if self._agent_loop_enabled(run):
            self._agent_resume_actions.setdefault(run_id, []).append(
                approved_action,
            )
        else:
            actions = self._actions[run_id]
            index = self._next_index.get(run_id, 0)
            actions[index] = approved_action
        run.pending_confirmation = None
        run.status = "queued"
        run.updated_at = utc_now_iso()
        self._append_event(
            run,
            "task_confirmation_approved",
            "User approved action.",
        )
        await record_browser_task_runtime_run_control(run, action="resume")
        self._schedule(run_id)
        return run

    def artifacts(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        replay_events = self.broker.replay.list_events(
            run.replay_run_id or run_id,
        )
        domain_skill_candidate = None
        if replay_events:
            domain_skill_candidate = (
                self.broker.replay.build_domain_skill_draft(
                    run.replay_run_id or run_id,
                    title=run.instruction,
                    summary=(
                        "Candidate Domain Skill generated from Browser Task "
                        f"`{run_id}`. Save only after user review."
                    ),
                ).model_dump(mode="json")
            )
        screenshots = [
            str(event.get("screenshot_path") or "")
            for event in replay_events
            if event.get("screenshot_path")
        ]
        summaries = [
            event.get("metadata", {}).get("observation_summary")
            for event in replay_events
            if isinstance(event.get("metadata"), dict)
            and event.get("metadata", {}).get("observation_summary")
        ]
        task_events = self.list_events(run_id)
        return {
            "run_id": run_id,
            "replay_run_id": run.replay_run_id,
            "event_count": len(replay_events),
            "task_events": [
                event.model_dump(mode="json") for event in task_events
            ],
            "replay_events": replay_events,
            "screenshots": screenshots,
            "observation_summaries": summaries,
            "preflights": self._preflights_from_events(
                task_events,
                replay_events,
            ),
            "recovery": run.metadata.get("last_recovery")
            or run.result.get("recovery")
            or None,
            "recoveries": [
                event.metadata.get("recovery")
                for event in task_events
                if isinstance(event.metadata.get("recovery"), dict)
            ],
            "domain_skill_candidate": domain_skill_candidate,
            "network": [
                event.get("metadata", {}).get("network_summary")
                for event in replay_events
                if isinstance(event.get("metadata"), dict)
                and event.get("metadata", {}).get("network_summary")
            ],
            "console": [
                event.get("metadata", {}).get("console_summary")
                for event in replay_events
                if isinstance(event.get("metadata"), dict)
                and event.get("metadata", {}).get("console_summary")
            ],
        }

    @staticmethod
    def _preflights_from_events(
        task_events: list[BrowserTaskEvent],
        replay_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        replay_by_id = {
            str(event.get("id") or ""): event
            for event in replay_events
            if event.get("id")
        }
        out: list[dict[str, Any]] = []
        for event in task_events:
            observation = event.metadata.get("observation")
            if not isinstance(observation, dict):
                continue
            replay_event = replay_by_id.get(
                str(observation.get("replay_event_id") or ""),
            )
            replay_metadata = (
                replay_event.get("metadata", {})
                if isinstance(replay_event, dict)
                else {}
            )
            if not isinstance(replay_metadata, dict):
                replay_metadata = {}
            preflight = str(
                replay_metadata.get("domain_skill_preflight") or "",
            )
            if not preflight:
                continue
            raw_results = BrowserTaskRuntime._observation_payload(
                observation,
                "data",
            )
            results = raw_results if isinstance(raw_results, list) else []
            selector_results: list[dict[str, Any]] = [
                {
                    "selector": str(item.get("selector") or "")[:240],
                    "count": int(item.get("count") or 0),
                    "error": str(item.get("error") or "")[:240],
                    **(
                        {"scope_counts": item.get("scope_counts")}
                        if isinstance(item.get("scope_counts"), dict)
                        else {}
                    ),
                    **(
                        {
                            "blocked_frame_count": int(
                                item.get("blocked_frame_count") or 0,
                            ),
                        }
                        if "blocked_frame_count" in item
                        else {}
                    ),
                    **(
                        {
                            "scopes": (
                                BrowserTaskRuntime._safe_preflight_scopes(
                                    item.get("scopes"),
                                )
                            ),
                        }
                        if isinstance(item.get("scopes"), list)
                        else {}
                    ),
                }
                for item in results
                if isinstance(item, dict)
            ][:50]
            scope_candidates = BrowserTaskRuntime._preflight_scope_candidates(
                selector_results,
            )
            out.append(
                {
                    "type": preflight,
                    "ok": bool(observation.get("ok")),
                    "action": str(observation.get("action") or ""),
                    "domain_skill_id": str(
                        replay_metadata.get("domain_skill_id") or "",
                    ),
                    "matched_selector_count": sum(
                        1 for item in selector_results if item["count"] > 0
                    ),
                    "selector_count": len(selector_results),
                    "selectors": selector_results,
                    **(
                        {"scope_candidates": scope_candidates}
                        if scope_candidates
                        else {}
                    ),
                },
            )
        return out

    @staticmethod
    def _safe_preflight_scopes(raw_scopes: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_scopes, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw_scopes[:40]:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    key: value
                    for key, value in {
                        "kind": str(item.get("kind") or "")[:80],
                        "scope": str(item.get("scope") or "")[:120],
                        "count": int(item.get("count") or 0),
                        "blocked": bool(item.get("blocked")),
                        "frame_selector": str(
                            item.get("frame_selector") or "",
                        )[:240],
                        "shadow_host_selector": str(
                            item.get("shadow_host_selector") or "",
                        )[:240],
                        "error": str(item.get("error") or "")[:240],
                    }.items()
                    if value not in {"", None}
                },
            )
        return out

    @staticmethod
    def _preflight_scope_candidates(
        selector_results: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        seen: set[tuple[str, str, str]] = set()
        out: list[dict[str, str]] = []
        for selector_result in selector_results:
            selector = str(selector_result.get("selector") or "").strip()[:240]
            raw_scopes = selector_result.get("scopes")
            if not isinstance(raw_scopes, list):
                continue
            for scope in raw_scopes:
                if not isinstance(scope, dict):
                    continue
                try:
                    count = int(scope.get("count") or 0)
                except (TypeError, ValueError):
                    count = 0
                if count <= 0:
                    continue
                frame_selector = str(
                    scope.get("frame_selector") or "",
                ).strip()[:240]
                shadow_host_selector = str(
                    scope.get("shadow_host_selector") or "",
                ).strip()[:240]
                if not (frame_selector or shadow_host_selector):
                    continue
                key = (selector, frame_selector, shadow_host_selector)
                if key in seen:
                    continue
                seen.add(key)
                candidate = {
                    "selector": selector,
                    "frame_selector": frame_selector,
                    "shadow_host_selector": shadow_host_selector,
                }
                out.append(
                    {key: value for key, value in candidate.items() if value},
                )
        return out[:40]

    @staticmethod
    def _observation_payload(
        observation: dict[str, Any],
        key: str,
    ) -> Any:
        data = observation.get("data")
        if not isinstance(data, dict):
            return None
        return data.get(key)

    def cleanup_history(
        self,
        *,
        max_runs: int = 200,
        max_age_days: int = 30,
        include_active: bool = False,
    ) -> dict[str, Any]:
        if self.store is None:
            return {
                "ok": True,
                "removed_count": 0,
                "removed_run_ids": [],
                "max_runs": max_runs,
                "max_age_days": max_age_days,
                "include_active": include_active,
                "message": "Browser task persistence is not enabled.",
            }
        result = self.store.prune(
            max_runs=max_runs,
            max_age_days=max_age_days,
            include_active=include_active,
        )
        for run_id in result.get("removed_run_ids", []):
            run = self._runs.get(str(run_id))
            if run is None:
                continue
            if include_active or run.status in _STOPPED_STATUSES:
                self._runs.pop(run.run_id, None)
                self._events.pop(run.run_id, None)
                self._actions.pop(run.run_id, None)
                self._next_index.pop(run.run_id, None)
                self._release_write_lock(run)
        return result

    def _schedule(self, run_id: str) -> None:
        handle = self._handles.get(run_id)
        if handle and not handle.done():
            run = self._runs.get(run_id)
            if run is not None and run.status == "queued":
                handle.add_done_callback(
                    lambda _finished: self._schedule(run_id),
                )
            return
        self._handles[run_id] = asyncio.create_task(self._run(run_id))

    async def _run(self, run_id: str) -> None:
        run = self._runs[run_id]
        owner_key = str(
            run.metadata.get("root_session_id")
            or run.metadata.get("session_id")
            or run.run_id,
        )
        try:
            async with get_runtime_concurrency_governor().slot(
                category="browser",
                owner_key=f"browser:{owner_key}",
            ):
                await self._run_admitted(run_id)
        except ConcurrencyAdmissionError as exc:
            self._finish(
                run,
                "failed",
                str(exc),
                exc.code,
            )

    # pylint: disable=R0911,R0912,R0915
    async def _run_admitted(
        self,
        run_id: str,
    ) -> None:
        run = self._runs[run_id]
        if self._agent_loop_enabled(run):
            await self._run_agent(run_id)
            return
        run = self._runs[run_id]
        actions = self._actions.get(run_id, [])
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            if not actions:
                self._finish(
                    run,
                    "failed",
                    "Browser task has no planned actions.",
                    "browser_task_empty",
                )
                return
            run.status = "running_background"
            run.updated_at = utc_now_iso()
            self._append_event(run, "task_started", "Browser task started.")
            heartbeat_task = asyncio.create_task(
                _browser_task_runtime_heartbeat_loop(
                    run,
                    interval_seconds=_BROWSER_TASK_HEARTBEAT_INTERVAL_SECONDS,
                ),
            )
            while self._next_index.get(run_id, 0) < len(actions):
                budget = self._budget(run)
                if self._next_index.get(run_id, 0) >= budget["max_actions"]:
                    self._finish(
                        run,
                        "failed",
                        "Browser task exceeded its action budget.",
                        "browser_task_action_budget_exceeded",
                    )
                    return
                remaining_duration = budget["max_duration_seconds"] - (
                    loop.time() - started_at
                )
                if remaining_duration <= 0:
                    self._finish(
                        run,
                        "failed",
                        "Browser task exceeded its duration budget.",
                        "browser_task_duration_budget_exceeded",
                    )
                    return
                if run_id in self._cancel_requested:
                    self._finish(
                        run,
                        "cancelled",
                        "Browser task cancelled.",
                        "browser_task_cancelled",
                    )
                    return
                if run_id in self._pause_requested:
                    run.status = "paused"
                    run.current_action = "等待用户继续"
                    run.updated_at = utc_now_iso()
                    await record_browser_task_runtime_run_control(
                        run,
                        action="pause",
                    )
                    return
                index = self._next_index[run_id]
                action = self._prepare_action(
                    run,
                    actions[index],
                    action_timeout_seconds=remaining_duration,
                )
                effect = self._record_effect_intent(run, index, action)
                if effect is None and self.effect_journal is not None:
                    self._finish(
                        run,
                        "failed",
                        "Browser task could not record effect intent.",
                        "browser_task_effect_journal_failed",
                    )
                    return
                run.current_action = action.action
                run.updated_at = utc_now_iso()
                self._append_event(
                    run,
                    "action_started",
                    f"Running browser action: {action.action}",
                    action=action.action,
                )
                running_effect = self._record_effect_started(run, effect)
                if effect is not None and running_effect is None:
                    self._finish(
                        run,
                        "failed",
                        (
                            "Browser task could not record effect "
                            "execution start."
                        ),
                        "browser_task_effect_journal_failed",
                    )
                    return
                try:
                    observation = await asyncio.wait_for(
                        self.broker.execute(action),
                        timeout=max(0.001, remaining_duration),
                    )
                except asyncio.TimeoutError:
                    self._finish(
                        run,
                        "failed",
                        "Browser task exceeded its duration budget.",
                        "browser_task_duration_budget_exceeded",
                    )
                    return
                if (
                    run.status in _STOPPED_STATUSES
                    or run_id in self._cancel_requested
                ):
                    if run.status != "cancelled":
                        self._finish(
                            run,
                            "cancelled",
                            "Browser task cancelled.",
                            "browser_task_cancelled",
                        )
                    return
                if run_id in self._pause_requested:
                    run.status = "paused"
                    run.current_action = "等待用户继续"
                    run.updated_at = utc_now_iso()
                    await record_browser_task_runtime_run_control(
                        run,
                        action="pause",
                    )
                    return
                if (
                    observation.decision is not None
                    and observation.decision.decision == "prompt"
                    and observation.decision.requires_confirmation
                ):
                    self._record_effect_failed(running_effect, observation)
                    recovery = self._failure_recovery(observation, action)
                    run.status = "waiting_for_user_confirmation"
                    run.pending_confirmation = action
                    run.current_action = action.action
                    run.error_code = (
                        observation.decision.matched_rule_id
                        or "browser_task_confirmation_required"
                    )
                    run.updated_at = utc_now_iso()
                    await record_browser_task_runtime_run_control(
                        run,
                        action="wait_approval",
                    )
                    self._append_observation_event(
                        run,
                        observation,
                        "action_waiting_for_confirmation",
                        observation.error or observation.decision.reason,
                        extra_metadata={"recovery": recovery},
                    )
                    return
                self._record_effect_settled(run, running_effect, observation)
                self._next_index[run_id] = index + 1
                run.completed_action_count = self._next_index[run_id]
                recovery = (
                    self._failure_recovery(observation, action)
                    if self._requires_manual_takeover(observation)
                    or not observation.ok
                    else None
                )
                self._append_observation_event(
                    run,
                    observation,
                    "action_completed" if observation.ok else "action_failed",
                    observation.message or observation.error or "",
                    extra_metadata={"recovery": recovery}
                    if recovery
                    else None,
                )
                if recovery and recovery.get("manual_takeover_required"):
                    self._finish(
                        run,
                        "manual_takeover_required",
                        observation.error
                        or "Browser task requires manual takeover.",
                        str(
                            recovery.get("error_code")
                            or "browser_task_manual_takeover_required",
                        ),
                        recovery=recovery,
                    )
                    return
                if not observation.ok:
                    self._finish(
                        run,
                        "failed",
                        observation.error or "Browser action failed.",
                        str(recovery.get("error_code") if recovery else "")
                        or self._error_code(
                            observation,
                            "browser_task_action_failed",
                        ),
                        recovery=recovery,
                    )
                    return
            await self._finish_settled(
                run,
                "completed",
                "Browser task completed.",
                "",
            )
        except asyncio.CancelledError:
            self._finish(
                run,
                "cancelled",
                "Browser task cancelled.",
                "browser_task_cancelled",
            )
        except RuntimeRunStoreError:
            if run.status not in _STOPPED_STATUSES:
                self._finish(
                    run,
                    "failed",
                    "Browser task could not persist its final result.",
                    "browser_task_runtime_settlement_failed",
                )
        finally:
            await await_settled(
                asyncio.create_task(
                    self._settle_and_cleanup(run, heartbeat_task),
                )
            )

    # pylint: enable=R0911,R0912,R0915

    def _agent_loop_planner(
        self,
        run: BrowserTaskRun,
        actions: list[BrowserAction],
        current_state: BrowserPageState,
    ):
        agent_config = run.metadata.get("agent_loop")
        config = agent_config if isinstance(agent_config, dict) else {}
        strategy = str(config.get("planner_strategy") or "")
        deterministic_strategies = {
            "explicit_actions",
            "lantaicli_adapter",
            "domain_skill_adapter",
            "domain_skill_generic_read",
        }
        static_index = max(
            0,
            int(run.metadata.get("agent_static_plan_index") or 0),
        )
        if strategy in deterministic_strategies:
            remaining: list[BrowserAction] = []
            for index, action in enumerate(
                actions[static_index:],
                start=static_index,
            ):
                remaining.append(
                    action.model_copy(
                        update={
                            "metadata": {
                                **action.metadata,
                                "browser_agent_static_plan_index": index,
                            },
                        },
                    ),
                )
            return DeterministicPlannerAdapter(remaining)

        try:
            if self.model_client_factory is not None:
                client = self.model_client_factory()
            else:
                from ...agents.model_factory import create_model_client

                client = create_model_client(
                    agent_id=str(run.metadata.get("agent_id") or "") or None,
                )
        except Exception as exc:
            raise RuntimeError(
                f"Browser Agent model is unavailable: {exc}",
            ) from exc
        adaptive = ModelPlannerAdapter(client)
        bootstrap: list[BrowserAction] = []
        start_url = str(run.metadata.get("start_url") or "").strip()
        if not start_url and actions:
            start_url = str(actions[0].url or "").strip()
        if start_url and current_state.url.rstrip("/") != start_url.rstrip(
            "/",
        ):
            bootstrap.append(
                BrowserAction(
                    action="open",
                    profile_id=run.profile_id,
                    url=start_url,
                    access="read",
                    run_id=run.run_id,
                    reason="Open the Browser Agent task start URL.",
                    metadata={"browser_agent_bootstrap": True},
                ),
            )
        if bootstrap:
            return BootstrapPlannerAdapter(bootstrap, adaptive)
        return adaptive

    async def _execute_agent_action(
        self,
        run: BrowserTaskRun,
        action: BrowserAction,
        index: int,
        *,
        deadline: float,
    ) -> BrowserObservation:
        loop = asyncio.get_running_loop()
        remaining_duration = deadline - loop.time()
        if remaining_duration <= 0:
            return BrowserObservation(
                ok=False,
                action=action.action,
                runtime=action.runtime or "electron_bridge",
                profile_id=action.profile_id or run.profile_id,
                page_id=action.page_id,
                url=action.url,
                error="Browser Agent duration budget exhausted.",
                data={"error_code": "browser_task_duration_budget_exceeded"},
            )
        prepared = self._prepare_action(
            run,
            action,
            action_timeout_seconds=remaining_duration,
        )
        effect = self._record_effect_intent(run, index, prepared)
        if effect is None and self.effect_journal is not None:
            return BrowserObservation(
                ok=False,
                action=prepared.action,
                runtime=prepared.runtime or "electron_bridge",
                profile_id=prepared.profile_id,
                page_id=prepared.page_id,
                url=prepared.url,
                error="Browser Agent could not record Effect intent.",
                data={"error_code": "browser_task_effect_journal_failed"},
            )
        run.current_action = prepared.action
        run.updated_at = utc_now_iso()
        self._append_event(
            run,
            "agent_action_started",
            f"Running verified Browser Agent action: {prepared.action}",
            action=prepared.action,
            metadata={
                "agent_phase": "executing",
                "attempt_id": self._attempt_id_for_run(run),
                "action_index": index,
                "expected_outcomes": prepared.metadata.get(
                    "browser_agent_expected_outcomes",
                    [],
                ),
            },
        )
        running_effect = self._record_effect_started(run, effect)
        if effect is not None and running_effect is None:
            return BrowserObservation(
                ok=False,
                action=prepared.action,
                runtime=prepared.runtime or "electron_bridge",
                profile_id=prepared.profile_id,
                page_id=prepared.page_id,
                url=prepared.url,
                error="Browser Agent could not start Effect execution.",
                data={"error_code": "browser_task_effect_journal_failed"},
            )
        try:
            observation = await asyncio.wait_for(
                self.broker.execute(prepared),
                timeout=max(0.001, remaining_duration),
            )
        except asyncio.TimeoutError:
            observation = BrowserObservation(
                ok=False,
                action=prepared.action,
                runtime=prepared.runtime or "electron_bridge",
                profile_id=prepared.profile_id,
                page_id=prepared.page_id,
                url=prepared.url,
                error="Browser Agent duration budget exhausted.",
                data={"error_code": "browser_task_duration_budget_exceeded"},
            )
        if (
            observation.decision is not None
            and observation.decision.decision == "prompt"
        ):
            self._record_effect_failed(running_effect, observation)
        else:
            self._record_effect_settled(run, running_effect, observation)
        run.completed_action_count = max(run.completed_action_count, index + 1)
        run.metadata["agent_action_count"] = run.completed_action_count
        static_plan_index = prepared.metadata.get(
            "browser_agent_static_plan_index",
        )
        if isinstance(static_plan_index, int):
            run.metadata["agent_static_plan_index"] = max(
                int(run.metadata.get("agent_static_plan_index") or 0),
                static_plan_index + 1,
            )
        self._append_observation_event(
            run,
            observation,
            "agent_action_completed"
            if observation.ok
            else "agent_action_failed",
            observation.message or observation.error or "",
            extra_metadata={
                "agent_phase": "executing",
                "attempt_id": self._attempt_id_for_run(run),
                "action_index": index,
            },
        )
        return observation

    # pylint: disable=R0911,R0912,R0915
    async def _run_agent(
        self,
        run_id: str,
    ) -> None:
        run = self._runs[run_id]
        actions = self._actions.get(run_id, [])
        loop = asyncio.get_running_loop()
        raw_budget = run.metadata.get("budget")
        budget_data = raw_budget if isinstance(raw_budget, dict) else {}
        agent_config = run.metadata.get("agent_loop")
        config = agent_config if isinstance(agent_config, dict) else {}
        budget = browser_agent_budget(
            str(config.get("budget_profile") or "standard"),
        )
        budget = budget.model_copy(
            update={
                "max_actions": min(
                    budget.max_actions,
                    max(
                        1,
                        int(
                            budget_data.get("max_actions")
                            or budget.max_actions,
                        ),
                    ),
                ),
                "max_duration_seconds": min(
                    budget.max_duration_seconds,
                    max(
                        1.0,
                        float(
                            budget_data.get("max_duration_seconds")
                            or budget.max_duration_seconds,
                        ),
                    ),
                ),
            },
        )
        deadline = loop.time() + budget.max_duration_seconds
        heartbeat_task: asyncio.Task[None] | None = None

        async def finish_agent_run(
            status: BrowserTaskStatus,
            message: str,
            error_code: str,
            *,
            recovery: dict[str, Any] | None = None,
        ) -> bool:
            return await self._finish_settled(
                run,
                status,
                message,
                error_code,
                recovery=recovery,
            )

        try:
            run.status = "running_background"
            run.current_action = "观察当前页面"
            run.updated_at = utc_now_iso()
            run.metadata["agent_phase"] = "observing"
            run.metadata["agent_budget"] = budget.model_dump(mode="json")
            self._append_event(
                run,
                "agent_loop_started",
                "Browser Agent loop started.",
                metadata={
                    "attempt_id": self._attempt_id_for_run(run),
                    "budget": budget.model_dump(mode="json"),
                    "planner_strategy": config.get("planner_strategy"),
                },
            )
            heartbeat_task = asyncio.create_task(
                _browser_task_runtime_heartbeat_loop(
                    run,
                    interval_seconds=_BROWSER_TASK_HEARTBEAT_INTERVAL_SECONDS,
                ),
            )

            async def on_phase(phase: str, details: dict[str, object]) -> None:
                raw_safe_details = redact_untrusted_value(details)
                safe_details = sanitize_runtime_payload(
                    raw_safe_details
                    if isinstance(raw_safe_details, dict)
                    else {},
                )
                phase_sequence = int(
                    run.metadata.get("agent_phase_sequence") or 0,
                )
                run.metadata["agent_phase_sequence"] = phase_sequence + 1
                if phase == "planning":
                    run.metadata["agent_turn_index"] = (
                        int(run.metadata.get("agent_turn_index") or 0) + 1
                    )
                turn_index = max(
                    1,
                    int(run.metadata.get("agent_turn_index") or 1),
                )
                run.metadata["agent_phase"] = phase
                run.metadata["agent_phase_details"] = safe_details
                run.current_action = str(
                    details.get("action")
                    or {
                        "observing": "观察页面",
                        "planning": "规划下一步",
                        "checking_policy": "检查动作权限",
                        "executing": "执行浏览器动作",
                        "verifying": "验证动作结果",
                        "recovering": "恢复并重规划",
                    }.get(phase, phase),
                )
                run.updated_at = utc_now_iso()
                self._append_event(
                    run,
                    f"agent_{phase}",
                    run.current_action,
                    action=str(details.get("action") or ""),
                    metadata={
                        "agent_phase": phase,
                        "attempt_id": self._attempt_id_for_run(run),
                        **safe_details,
                    },
                )
                await record_browser_task_runtime_step(
                    run,
                    phase=phase,
                    index=phase_sequence,
                    turn_index=turn_index,
                    title=run.current_action or phase,
                )

            initial_state = BrowserPageState(
                page_revision="page:initial",
                content_hash="sha256:initial",
                runtime="electron_bridge",
                profile_id=run.profile_id,
                page_id="default",
                url="about:blank",
                title="",
            )

            async def execute(
                action: BrowserAction,
                index: int,
            ) -> BrowserObservation:
                return await self._execute_agent_action(
                    run,
                    action,
                    index,
                    deadline=deadline,
                )

            initial_action = BrowserAction(
                action="snapshot",
                profile_id=run.profile_id,
                access="read",
                run_id=run.run_id,
                reason="Observe current Browser Agent page before planning.",
                metadata={
                    "browser_agent_internal_observation": True,
                    "attempt_id": self._attempt_id_for_run(run),
                },
            )
            await on_phase(
                "observing",
                {"action": "snapshot", "initial_observation": True},
            )
            initial_observation = await execute(
                initial_action,
                int(run.metadata.get("agent_action_count") or 0),
            )
            if (
                initial_observation.decision is not None
                and initial_observation.decision.decision == "prompt"
            ):
                run.status = "waiting_for_user_confirmation"
                run.pending_confirmation = initial_action
                run.error_code = (
                    initial_observation.decision.matched_rule_id
                    or "browser_task_confirmation_required"
                )
                run.updated_at = utc_now_iso()
                await record_browser_task_runtime_run_control(
                    run,
                    action="wait_approval",
                )
                self._append_event(
                    run,
                    "agent_waiting_for_confirmation",
                    initial_observation.error
                    or initial_observation.decision.reason,
                    action=initial_action.action,
                )
                return
            initial_state = normalize_browser_observation(
                initial_observation,
                budget=budget,
            )
            if initial_state.requires_manual_takeover:
                await finish_agent_run(
                    "manual_takeover_required",
                    (
                        "The current page requires manual takeover "
                        "before Browser Agent planning."
                    ),
                    "browser_task_manual_takeover_required",
                )
                return
            resume_actions = self._agent_resume_actions.pop(run.run_id, [])
            preverified_evidence_refs: list[str] = []
            for approved_action in resume_actions:
                before_approved = initial_state
                await on_phase(
                    "checking_policy",
                    {"action": approved_action.action, "user_approved": True},
                )
                await on_phase(
                    "executing",
                    {"action": approved_action.action, "user_approved": True},
                )
                approved_observation = await execute(
                    approved_action,
                    int(run.metadata.get("agent_action_count") or 0),
                )
                if not approved_observation.ok:
                    await finish_agent_run(
                        "failed",
                        approved_observation.error
                        or "Approved Browser Agent action failed.",
                        self._error_code(
                            approved_observation,
                            "browser_agent_approved_action_failed",
                        ),
                    )
                    return
                verify_snapshot = BrowserAction(
                    action="snapshot",
                    profile_id=run.profile_id,
                    page_id=approved_observation.page_id
                    or initial_state.page_id,
                    url=approved_observation.url or initial_state.url,
                    access="read",
                    run_id=run.run_id,
                    reason="Verify the user-approved Browser Agent action.",
                    metadata={
                        "browser_agent_internal_observation": True,
                        "attempt_id": self._attempt_id_for_run(run),
                    },
                )
                await on_phase(
                    "observing",
                    {
                        "action": "snapshot",
                        "verify_user_approved_action": approved_action.action,
                    },
                )
                verified_observation = await execute(
                    verify_snapshot,
                    int(run.metadata.get("agent_action_count") or 0),
                )
                initial_state = normalize_browser_observation(
                    verified_observation,
                    budget=budget,
                )
                raw_expected = approved_action.metadata.get(
                    "browser_agent_expected_outcomes",
                )
                expectations: list[BrowserExpectedOutcome] = []
                if isinstance(raw_expected, list):
                    for item in raw_expected:
                        if not isinstance(item, dict):
                            continue
                        try:
                            expectations.append(
                                BrowserExpectedOutcome.model_validate(item),
                            )
                        except Exception:
                            continue
                if not expectations:
                    expectations.append(
                        BrowserExpectedOutcome(kind="action_ok"),
                    )
                approved_outcome = BrowserOutcomeVerifier().verify(
                    action=approved_action,
                    expectations=tuple(expectations),
                    before=before_approved,
                    after=initial_state,
                    observation=approved_observation,
                )
                await on_phase(
                    "verifying",
                    {
                        "action": approved_action.action,
                        "outcome": approved_outcome.model_dump(mode="json"),
                        "user_approved": True,
                    },
                )
                if approved_outcome.status != "verified":
                    await finish_agent_run(
                        "failed",
                        approved_outcome.reason,
                        "browser_agent_approved_action_unverified",
                    )
                    return
                preverified_evidence_refs.extend(
                    approved_outcome.evidence_refs,
                )
            try:
                planner = self._agent_loop_planner(run, actions, initial_state)
            except RuntimeError as exc:
                await finish_agent_run(
                    "failed",
                    str(exc),
                    "browser_agent_model_unavailable",
                )
                return

            context = BrowserAgentContext(
                run_id=run.run_id,
                attempt_id=self._attempt_id_for_run(run),
                instruction=run.instruction,
                start_url=str(run.metadata.get("start_url") or ""),
                profile_id=run.profile_id,
                target_domains=tuple(run.target_domains),
                access=run.access,
                page_state=initial_state,
                budget=budget,
                action_count=int(run.metadata.get("agent_action_count") or 1),
                model_decision_count=int(
                    (
                        run.metadata.get("agent_usage")
                        if isinstance(run.metadata.get("agent_usage"), dict)
                        else {}
                    ).get("model_calls")
                    or 0,
                ),
                replan_count=int(
                    (
                        run.metadata.get("agent_result")
                        if isinstance(run.metadata.get("agent_result"), dict)
                        else {}
                    ).get("replan_count")
                    or 0,
                ),
                verified_evidence_refs=tuple(
                    dict.fromkeys(
                        ref for ref in preverified_evidence_refs if ref
                    ),
                ),
                usage=BrowserAgentUsage.model_validate(
                    {
                        key: value
                        for key, value in (
                            run.metadata.get("agent_usage")
                            if isinstance(
                                run.metadata.get("agent_usage"),
                                dict,
                            )
                            else {}
                        ).items()
                        if key in BrowserAgentUsage.model_fields
                    },
                ),
            )

            def control_probe():
                if (
                    run_id in self._cancel_requested
                    or run.status == "cancelled"
                ):
                    return "cancel"
                if run_id in self._pause_requested or run.status == "paused":
                    return "pause"
                return "continue"

            result = await BrowserAgentLoop(
                broker=self.broker,
                planner=planner,
                execute_action=execute,
                on_phase=on_phase,
                control_probe=control_probe,
                visual_available=bool(
                    run.metadata.get("model_supports_multimodal"),
                ),
                monotonic=loop.time,
            ).run(context)
            run.metadata["agent_phase"] = "complete"
            raw_agent_result = redact_untrusted_value(
                result.model_dump(mode="json", exclude={"pending_action"}),
            )
            run.metadata["agent_result"] = sanitize_runtime_payload(
                raw_agent_result if isinstance(raw_agent_result, dict) else {},
            )
            run.metadata["agent_usage"] = {
                **result.usage.model_dump(mode="json"),
                "cost_points": result.usage.cost_cny,
            }
            run.completed_action_count = result.action_count
            if result.stop_reason == "paused":
                run.status = "paused"
                run.current_action = "等待用户继续"
                run.updated_at = utc_now_iso()
                await record_browser_task_runtime_run_control(
                    run,
                    action="pause",
                )
                self._persist_run(run.run_id)
                return
            if result.stop_reason == "cancelled":
                await finish_agent_run(
                    "cancelled",
                    result.message,
                    "browser_task_cancelled",
                )
                return
            if (
                result.stop_reason == "waiting_confirmation"
                and result.pending_action
            ):
                run.status = "waiting_for_user_confirmation"
                run.pending_confirmation = result.pending_action
                run.current_action = result.pending_action.action
                run.error_code = "browser_task_confirmation_required"
                run.updated_at = utc_now_iso()
                await record_browser_task_runtime_run_control(
                    run,
                    action="wait_approval",
                )
                self._append_event(
                    run,
                    "agent_waiting_for_confirmation",
                    result.message,
                    action=result.pending_action.action,
                    metadata={
                        "agent_phase": "waiting_approval",
                        "pending_action": result.pending_action.model_dump(
                            mode="json",
                        ),
                    },
                )
                return
            if result.stop_reason in {
                "manual_takeover",
                "waiting_confirmation",
            }:
                await finish_agent_run(
                    "manual_takeover_required",
                    result.message,
                    "browser_task_manual_takeover_required",
                    recovery={
                        "kind": "manual_takeover",
                        "manual_takeover_required": True,
                        "recoverable": True,
                        "suggestion": (
                            "Complete the requested browser step and "
                            "return control."
                        ),
                    },
                )
                return
            if result.ok:
                if not await finish_agent_run("completed", result.message, ""):
                    return
                run.result.update(
                    {
                        "agent_loop": True,
                        "stop_reason": result.stop_reason,
                        "model_decision_count": result.model_decision_count,
                        "replan_count": result.replan_count,
                        "evidence_refs": list(result.evidence_refs),
                        "usage": run.metadata["agent_usage"],
                    },
                )
                self._persist_run(run.run_id)
                return
            await finish_agent_run(
                "failed",
                result.message,
                f"browser_agent_{result.stop_reason}",
                recovery={
                    "kind": result.stop_reason,
                    "manual_takeover_required": False,
                    "recoverable": result.stop_reason
                    in {"no_progress", "repeated_failure", "duration_budget"},
                    "suggestion": (
                        "Inspect verified step evidence before " "retrying."
                    ),
                },
            )
        except asyncio.CancelledError:
            await finish_agent_run(
                "cancelled",
                "Browser Agent cancelled.",
                "browser_task_cancelled",
            )
        except Exception as exc:
            await finish_agent_run(
                "failed",
                f"Browser Agent loop failed safely: {exc}",
                "browser_agent_runtime_exception",
            )
        finally:
            await await_settled(
                asyncio.create_task(
                    self._settle_and_cleanup(run, heartbeat_task),
                )
            )

    # pylint: enable=R0911,R0912,R0915

    async def _settle_and_cleanup(
        self,
        run: BrowserTaskRun,
        heartbeat_task: asyncio.Task | None,
    ) -> None:
        failure: BaseException | None = None
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                failure = exc
        try:
            try:
                await self._drain_runtime_controls(run.run_id)
            except (Exception, asyncio.CancelledError) as exc:
                failure = failure or exc
            if run.status in _STOPPED_STATUSES:
                await record_browser_task_runtime_run_settled(
                    run,
                    app_store_distribution=self.app_store_distribution(),
                )
        finally:
            try:
                if (
                    run.status in {"completed", "failed", "cancelled"}
                    and "terminal_tab_cleanup" not in run.metadata
                ):
                    await self._cleanup_terminal_task_tabs(run)
            finally:
                if run.status in _STOPPED_STATUSES:
                    self._release_write_lock(run)
        if failure is not None:
            raise failure

    async def _finish_settled(
        self,
        run: BrowserTaskRun,
        status: BrowserTaskStatus,
        message: str,
        error_code: str,
        *,
        recovery: dict[str, Any] | None = None,
    ) -> bool:
        """Publish a terminal result after cleanup and canonical
        persistence.
        """
        initial_status = run.status

        def can_settle() -> bool:
            return (
                run.status == initial_status
                and run.status not in _STOPPED_STATUSES
                and run.run_id not in self._cancel_requested
                and run.run_id not in self._pause_requested
            )

        if not can_settle():
            return False
        if (
            status in {"completed", "failed", "cancelled"}
            and "terminal_tab_cleanup" not in run.metadata
        ):
            await self._cleanup_terminal_task_tabs(run)
        if not can_settle():
            return False
        message = redact_untrusted_text(message, limit=2000)
        staged = run.model_copy(deep=True)
        staged.status = status
        staged.error_code = error_code
        staged.result = {
            "message": message,
            "ok": status == "completed",
            "error_code": error_code,
            "completed_action_count": run.completed_action_count,
        }
        if recovery:
            staged.result["recovery"] = recovery
        try:
            settled = await record_browser_task_runtime_run_settled(
                staged,
                app_store_distribution=self.app_store_distribution(),
                can_settle=can_settle,
            )
        except RuntimeRunStoreError:
            if not can_settle():
                return False
            raise
        if not can_settle():
            return False
        expected_status = {
            "completed": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "manual_takeover_required": "stalled",
        }[status]
        if run.metadata.get("workspace_dir") and (
            settled is None or settled.status != expected_status
        ):
            raise RuntimeRunStoreConflictError(
                "Browser task canonical settlement did not match its result.",
            )
        self._finish(run, status, message, error_code, recovery=recovery)
        return True

    async def _cleanup_terminal_task_tabs(self, run: BrowserTaskRun) -> None:
        """Release only the background tabs owned by one terminal task.

        Manual-takeover runs deliberately keep their page alive so the user can
        finish the requested step. Cleanup is best effort and never rewrites an
        already settled task result.
        """

        task_group_id = str(
            run.metadata.get("task_group_id") or f"task-{run.run_id}",
        ).strip()
        if not task_group_id:
            return
        bridge = getattr(self.broker, "bridge", None)
        if bridge is None or not bool(getattr(bridge, "available", False)):
            return
        try:
            payload = await bridge.execute(
                BrowserAction(
                    action="close_task_group",
                    profile_id=run.profile_id,
                    access="read",
                    run_id=run.replay_run_id or run.run_id,
                    reason="Release terminal BrowserTask tabs.",
                    metadata={
                        "browser_task_run_id": run.run_id,
                        "task_group_id": task_group_id,
                        "browser_task_terminal_cleanup": True,
                    },
                ),
            )
            payload = payload if isinstance(payload, dict) else {}
            run.metadata["terminal_tab_cleanup"] = {
                "ok": bool(payload.get("ok", True)),
                "closed_tabs": max(0, int(payload.get("closed_tabs") or 0)),
                "error_code": str(payload.get("error_code") or ""),
            }
        except (
            Exception
        ) as exc:  # pragma: no cover - cleanup must be fail-soft
            run.metadata["terminal_tab_cleanup"] = {
                "ok": False,
                "closed_tabs": 0,
                "error_code": f"browser_task_tab_cleanup:{type(exc).__name__}",
            }
        self._persist_run(run.run_id)

    def _prepare_action(
        self,
        run: BrowserTaskRun,
        action: BrowserAction,
        *,
        action_timeout_seconds: float | None = None,
    ) -> BrowserAction:
        metadata = {
            **action.metadata,
            "browser_task_run_id": run.run_id,
            "task_group_id": run.metadata.get("task_group_id")
            or f"task-{run.run_id}",
            "target_domains": run.target_domains,
            "background_observable": run.mode == "background_observable",
        }
        if action_timeout_seconds is not None:
            metadata["browser_task_action_timeout_seconds"] = max(
                0.001,
                float(action_timeout_seconds),
            )
        prepared = action.model_copy(
            update={
                "run_id": run.replay_run_id or run.run_id,
                "profile_id": action.profile_id or run.profile_id,
                "access": action.access or run.access,
                "metadata": metadata,
            },
        )
        authorize_read = getattr(
            self.broker,
            "authorize_read_task_interaction",
            None,
        )
        if callable(authorize_read):
            return authorize_read(prepared, task_access=run.access)
        return prepared

    @staticmethod
    def _stable_hash(value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _attempt_id_for_run(run: BrowserTaskRun) -> str:
        return str(
            run.metadata.get("attempt_id") or f"{run.run_id}:browser-attempt",
        )

    @staticmethod
    def _effect_id_for_action(
        run: BrowserTaskRun,
        index: int,
        action: BrowserAction,
    ) -> str:
        return str(
            action.metadata.get("effect_id")
            or f"browser:{run.run_id}:{index}:{action.action}",
        )

    @staticmethod
    def _owner_for_run(run: BrowserTaskRun) -> RuntimeRunOwner:
        workspace_id = (
            str(run.metadata.get("workspace_dir") or "").strip() or None
        )
        return RuntimeRunOwner(
            user_id=str(run.metadata.get("user_id") or "default"),
            agent_id=str(run.metadata.get("agent_id") or "browser"),
            session_id=str(run.metadata.get("session_id") or run.run_id),
            workspace_id=workspace_id,
            channel="browser",
        )

    def _record_effect_intent(
        self,
        run: BrowserTaskRun,
        index: int,
        action: BrowserAction,
    ) -> RuntimeEffectRecord | None:
        if self.effect_journal is None:
            return None
        try:
            recovery = browser_action_effect_recovery(action)
            effect = RuntimeEffectRecord(
                run_id=run.run_id,
                attempt_id=self._attempt_id_for_run(run),
                step_id=f"browser-action-{index}",
                effect_id=self._effect_id_for_action(run, index, action),
                source="browser",
                owner=self._owner_for_run(run),
                status="intent",
                tool_name=f"browser.{action.action}",
                args_hash=self._stable_hash(action.model_dump(mode="json")),
                policy_snapshot_hash=str(
                    action.metadata.get("policy_snapshot_hash")
                    or run.metadata.get("policy_snapshot_hash")
                    or self._stable_hash(
                        {
                            "access": action.access or run.access,
                            "target_domains": run.target_domains,
                            "effect_recovery": recovery.model_dump(
                                mode="json",
                            ),
                        },
                    ),
                ),
                idempotency_key=str(
                    action.metadata.get("idempotency_key")
                    or (
                        f"{run.run_id}"
                        ":"
                        f"{self._attempt_id_for_run(run)}"
                        ":"
                        f"{self._effect_id_for_action(run, index, action)}"
                    ),
                ),
                release_edition=(
                    "mas" if self.app_store_distribution() else "official"
                ),
            )
            self.effect_journal.append_effect(effect)
            return effect
        except Exception:
            return None

    def _record_effect_started(
        self,
        run: BrowserTaskRun,
        effect: RuntimeEffectRecord | None,
    ) -> RuntimeEffectRecord | None:
        del run
        if self.effect_journal is None or effect is None:
            return effect
        try:
            running = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(type="start"),
            ).effect
            self.effect_journal.append_effect(running)
            return running
        except Exception:
            return None

    def _record_effect_failed(
        self,
        effect: RuntimeEffectRecord | None,
        observation: BrowserObservation,
    ) -> RuntimeEffectRecord | None:
        if self.effect_journal is None or effect is None:
            return effect
        try:
            failed = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(
                    type="fail",
                    result_hash=self._stable_hash(
                        observation.model_dump(mode="json"),
                    ),
                ),
            ).effect
            self.effect_journal.append_effect(failed)
            return failed
        except Exception:
            return None

    def _record_effect_settled(
        self,
        run: BrowserTaskRun,
        effect: RuntimeEffectRecord | None,
        observation: BrowserObservation,
    ) -> RuntimeEffectRecord | None:
        if self.effect_journal is None or effect is None:
            return effect
        try:
            action_type = "commit" if observation.ok else "fail"
            settled = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(
                    type=action_type,
                    result_hash=self._stable_hash(
                        observation.model_dump(mode="json"),
                    ),
                ),
            ).effect
            self.effect_journal.append_effect(settled)
            return settled
        except Exception:
            self._finish(
                run,
                "manual_takeover_required",
                (
                    "Browser action completed but effect journal "
                    "settlement failed."
                ),
                "browser_task_effect_journal_settlement_failed",
                recovery={
                    "kind": "effect_journal_settlement_failed",
                    "manual_takeover_required": True,
                    "recoverable": False,
                    "suggestion": (
                        "Inspect browser state before retrying this " "action."
                    ),
                },
            )
            return None

    def _preflight_policy_block(
        self,
        run: BrowserTaskRun,
        actions: list[BrowserAction],
    ) -> tuple[BrowserAction, Any] | None:
        evaluate = getattr(self.broker, "evaluate", None)
        if not callable(evaluate):
            return None
        for action in actions:
            prepared = self._prepare_action(run, action)
            decision = evaluate(prepared)
            if decision.decision == "block":
                return prepared, decision
        return None

    def _active_run_count(self) -> int:
        return sum(
            1 for run in self._runs.values() if run.status in _ACTIVE_STATUSES
        )

    @staticmethod
    def _budget(run: BrowserTaskRun) -> dict[str, float]:
        budget = run.metadata.get("budget")
        if not isinstance(budget, dict):
            return {"max_actions": 200.0, "max_duration_seconds": 1800.0}
        return {
            "max_actions": max(1.0, float(budget.get("max_actions") or 200)),
            "max_duration_seconds": max(
                1.0,
                float(budget.get("max_duration_seconds") or 1800.0),
            ),
        }

    def _agent_loop_config(
        self,
        request: BrowserTaskRequest,
        plan_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            raw = self.agent_loop_settings()
        except Exception:
            raw = {}
        settings = raw if isinstance(raw, dict) else {}
        mode = str(settings.get("agent_loop_mode") or "off").strip().lower()
        configured = bool(settings.get("agent_loop_enabled")) and mode != "off"
        enabled = configured
        if mode == "fixture":
            enabled = enabled and bool(
                request.metadata.get("agent_loop_fixture"),
            )
        elif mode == "read_canary":
            enabled = enabled and request.access == "read"
        elif mode == "interactive":
            pass
        else:
            enabled = False
        if request.metadata.get("agent_loop_disabled") is True:
            enabled = False
        budget_profile = str(
            settings.get("agent_loop_budget_profile") or "standard",
        ).strip()
        if request.access != "read" and budget_profile == "standard":
            budget_profile = "draft_write"
        if budget_profile not in {
            "quick",
            "standard",
            "long_read",
            "draft_write",
        }:
            budget_profile = "standard"
        strategy = str(plan_metadata.get("strategy") or "")
        return {
            "enabled": enabled,
            "mode": mode,
            "budget_profile": budget_profile,
            "planner_strategy": strategy,
            "rollback": "static_planner",
            "schema_version": 1,
        }

    @staticmethod
    def _agent_loop_enabled(run: BrowserTaskRun) -> bool:
        config = run.metadata.get("agent_loop")
        return isinstance(config, dict) and bool(config.get("enabled"))

    def _write_lock_keys(
        self,
        run: BrowserTaskRun,
        actions: list[BrowserAction],
    ) -> list[tuple[str, str]]:
        if run.access == "read" and all(
            (action.access or "read") == "read" for action in actions
        ):
            return []
        domains: list[str] = []
        for domain in run.target_domains:
            normalized = self._normalize_domain(domain)
            if normalized and normalized not in domains:
                domains.append(normalized)
        for action in actions:
            domain = self._domain_for_action(action)
            if domain and domain not in domains:
                domains.append(domain)
        if not domains:
            domains = ["*"]
        return [(run.profile_id, domain) for domain in domains]

    def _write_lock_conflict(
        self,
        profile_id: str,
        lock_keys: list[tuple[str, str]],
    ) -> tuple[str, str] | None:
        if not lock_keys:
            return None
        requested_domains = {domain for _profile, domain in lock_keys}
        for key in self._write_locks:
            locked_profile, locked_domain = key
            if locked_profile != profile_id:
                continue
            if (
                locked_domain == "*"
                or "*" in requested_domains
                or locked_domain in requested_domains
            ):
                return key
        return None

    @staticmethod
    def _domain_for_action(action: BrowserAction) -> str:
        if action.url:
            parsed = urlparse(action.url)
            if parsed.hostname:
                return parsed.hostname.lower()
        return str(action.metadata.get("domain") or "").strip().lower()

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        value = str(domain or "").strip().lower()
        if not value:
            return ""
        parsed = urlparse(value if "://" in value else f"https://{value}")
        host = parsed.hostname or value.split("/", 1)[0]
        return host.strip().lower()

    def _append_event(
        self,
        run: BrowserTaskRun,
        event_type: str,
        message: str,
        *,
        action: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        raw_metadata = redact_untrusted_value(metadata or {})
        safe_metadata = (
            sanitize_runtime_payload(raw_metadata)
            if isinstance(raw_metadata, dict)
            else {}
        )
        event = BrowserTaskEvent(
            run_id=run.run_id,
            type=event_type,
            status=run.status,
            message=message,
            action=action,
            metadata=safe_metadata,
        )
        self._events.setdefault(run.run_id, []).append(event)
        self._persist_run(run.run_id)

    def _schedule_runtime_control(
        self,
        run: BrowserTaskRun,
        action: str,
    ) -> None:
        self._queue_runtime_control(run, action)

    def _schedule_runtime_settlement(self, run: BrowserTaskRun) -> None:
        self._queue_runtime_control(run, None)

    def _queue_runtime_control(
        self,
        run: BrowserTaskRun,
        action: str | None,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        previous = self._runtime_controls.get(run.run_id)
        snapshot = run.model_copy(deep=True)

        async def persist() -> None:
            # Preserve control order and own writes through task completion.
            if previous is not None:
                try:
                    await await_settled(previous)
                except (Exception, asyncio.CancelledError):
                    _logger.warning(
                        "Previous browser control failed; applying next control",
                        exc_info=True,
                    )
            if action is None:
                await record_browser_task_runtime_run_settled(
                    snapshot,
                    app_store_distribution=self.app_store_distribution(),
                )
            else:
                result = await record_browser_task_runtime_run_control(
                    snapshot,
                    action=action,
                )
                if result is None and snapshot.metadata.get("workspace_dir"):
                    raise RuntimeRunStoreError(
                        "Browser control was not persisted"
                    )

        self._runtime_controls[run.run_id] = loop.create_task(persist())

    async def _drain_runtime_controls(self, run_id: str) -> None:
        await await_settled(
            asyncio.create_task(
                self._drain_runtime_controls_owned(run_id),
            )
        )

    async def _drain_runtime_controls_owned(self, run_id: str) -> None:
        failure: BaseException | None = None
        while (task := self._runtime_controls.get(run_id)) is not None:
            try:
                await await_settled(task)
            except (Exception, asyncio.CancelledError) as exc:
                failure = failure or exc
            finally:
                if self._runtime_controls.get(run_id) is task and task.done():
                    self._runtime_controls.pop(run_id, None)
        if failure is not None:
            raise failure

    def _ambiguous_effects(
        self,
        run: BrowserTaskRun,
        attempt_id: str,
    ) -> list[str]:
        if self.effect_journal is None:
            return []
        try:
            latest: dict[str, RuntimeEffectRecord] = {}
            for effect in self.effect_journal.list_effects(
                run.run_id,
                attempt_id,
            ):
                latest[effect.effect_id] = effect
            return sorted(
                effect_id
                for effect_id, effect in latest.items()
                if effect.status in {"running", "ambiguous"}
            )
        except Exception:
            return ["effect_journal_unreadable"]

    def _append_observation_event(
        self,
        run: BrowserTaskRun,
        observation: BrowserObservation,
        event_type: str,
        message: str,
        *,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        safe_observation = observation.model_dump(mode="json")
        safe_observation["data"] = redact_untrusted_value(observation.data)
        if safe_observation.get("message"):
            safe_observation["message"] = redact_untrusted_text(
                safe_observation["message"],
                limit=2000,
            )
        if safe_observation.get("error"):
            safe_observation["error"] = redact_untrusted_text(
                safe_observation["error"],
                limit=2000,
            )
        metadata = {
            "observation": safe_observation,
            "decision": observation.decision.model_dump(mode="json")
            if observation.decision
            else None,
            "error_code": self._error_code(observation, ""),
            "screenshot_path": observation.screenshot_path,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        self._append_event(
            run,
            event_type,
            message,
            action=observation.action,
            metadata=metadata,
        )

    def _finish(
        self,
        run: BrowserTaskRun,
        status: BrowserTaskStatus,
        message: str,
        error_code: str,
        *,
        recovery: dict[str, Any] | None = None,
    ) -> None:
        message = redact_untrusted_text(message, limit=2000)
        run.status = status
        run.current_action = ""
        run.updated_at = utc_now_iso()
        run.error_code = error_code
        if recovery:
            run.metadata["last_recovery"] = recovery
        run.result = {
            "message": message,
            "ok": status == "completed",
            "error_code": error_code,
            "completed_action_count": run.completed_action_count,
        }
        if recovery:
            run.result["recovery"] = recovery
        metadata = {"error_code": error_code}
        if recovery:
            metadata["recovery"] = recovery
        self._append_event(run, f"task_{status}", message, metadata=metadata)
        if status in {
            "completed",
            "failed",
            "cancelled",
            "manual_takeover_required",
        }:
            self._release_write_lock(run)

    def _persist_run(self, run_id: str) -> None:
        if self.store is None:
            return
        run = self._runs.get(run_id)
        if run is None:
            return
        self.store.save(
            run,
            self._events.get(run_id, []),
            self._actions.get(run_id, []),
            self._next_index.get(run_id, run.completed_action_count),
        )

    def _restore_from_store(self) -> None:
        if self.store is None:
            return
        for run, events, actions, next_index in self.store.load_all():
            if run.status in {"queued", "running_background"}:
                restored_status = run.status
                run.status = "paused"
                run.current_action = "等待用户继续"
                run.updated_at = utc_now_iso()
                run.metadata = {
                    **run.metadata,
                    "restored_after_restart": True,
                    "restored_status": restored_status,
                }
                events.append(
                    BrowserTaskEvent(
                        run_id=run.run_id,
                        type="task_restored_paused",
                        status=run.status,
                        message=(
                            (
                                "Browser task was restored after restart and "
                                "paused until the user resumes it."
                            )
                        ),
                    ),
                )
            self._runs[run.run_id] = run
            self._events[run.run_id] = events
            self._actions[run.run_id] = actions
            self._next_index[run.run_id] = max(
                0,
                min(next_index, len(actions)),
            )
            if run.status not in _STOPPED_STATUSES:
                lock_keys = self._write_lock_keys(run, actions)
                for lock_key in lock_keys:
                    self._write_locks[lock_key] = run.run_id
            self._persist_run(run.run_id)

    def _release_write_lock(self, run: BrowserTaskRun) -> None:
        for key, owner in list(self._write_locks.items()):
            if owner == run.run_id:
                self._write_locks.pop(key, None)

    def _require_run(self, run_id: str) -> BrowserTaskRun:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Browser task not found: {run_id}")
        return run

    # pylint: disable=R0912,R0915
    @classmethod
    def _failure_recovery(
        cls,
        observation: BrowserObservation,
        action: BrowserAction,
    ) -> dict[str, Any]:
        text = cls._failure_text(observation)
        raw_error_code = cls._error_code(observation, "")
        error_code = raw_error_code or "browser_task_action_failed"
        kind = "unknown"
        recoverable = False
        manual_takeover_required = False
        suggestion = (
            "Inspect the task replay, refresh the page, then retry the action."
        )

        if cls._requires_manual_takeover(observation):
            kind = "manual_takeover"
            error_code = (
                raw_error_code or "browser_task_manual_takeover_required"
            )
            manual_takeover_required = True
            suggestion = (
                "Open the task in Browser Workbench and let the user complete "
                "the login, captcha, publish, delete, payment, authorization, "
                "or message-sending step."
            )
        elif (
            observation.decision is not None
            and observation.decision.requires_confirmation
        ):
            kind = "confirmation_required"
            error_code = (
                observation.decision.matched_rule_id
                or "browser_task_confirmation_required"
            )
            recoverable = True
            suggestion = (
                "Ask the user to approve once or reject the pending action."
            )
        elif error_code in {
            "browser_profile_not_found",
            "browser_profile_agent_control_disabled",
            "browser_domain_not_allowed",
            "browser_raw_cookie_blocked",
            "browser_scheduled_write_blocked",
            "app_store_browser_task_blocked",
        }:
            kind = "policy_blocked"
            suggestion = (
                "Review the Profile, allowed domains, distribution channel, "
                "and task policy before retrying."
            )
        elif (
            "shadow root is closed" in text
            or "shadow host element not found" in text
        ):
            kind = "shadow_unavailable"
            error_code = "browser_shadow_root_unavailable"
            manual_takeover_required = True
            suggestion = (
                "The target is inside an unavailable shadow "
                "root. Bring the task foreground, inspect "
                "the page, and update the Domain Skill "
                "scope."
            )
        elif (
            "frame is cross-origin" in text
            or "frame document is unavailable" in text
            or "frame element not found" in text
        ):
            kind = "frame_unavailable"
            error_code = "browser_frame_unavailable"
            manual_takeover_required = True
            suggestion = (
                "The target frame is cross-origin or "
                "unavailable. Use foreground takeover or "
                "update the frame selector before retrying."
            )
        elif (
            "element not found" in text
            or "file input element not found" in text
        ):
            kind = "element_not_found"
            error_code = "browser_element_not_found"
            recoverable = True
            suggestion = (
                "Refresh the page, re-run selector probe, then update the "
                "LantaiCLI adapter or Domain Skill selector."
            )
        elif action.action == "download_wait" or "download" in text:
            kind = "download_failed"
            error_code = "browser_download_failed"
            recoverable = True
            suggestion = (
                "Check whether the download was triggered, "
                "wait longer, or retry with a more specific "
                "expected filename."
            )
        elif error_code.startswith("electron_bridge_"):
            kind = "electron_bridge_error"
            recoverable = error_code not in {"electron_bridge_unauthorized"}
            suggestion = (
                "Restart or reconnect Browser Workbench "
                "Bridge, then retry the task."
            )
            if error_code == "electron_bridge_unauthorized":
                suggestion = (
                    "Refresh the Browser Bridge session token and retry from "
                    "the active workbench."
                )
        elif error_code == "browser_runtime_exception":
            kind = "runtime_exception"
            recoverable = True
            suggestion = (
                "Inspect the runtime error, then retry or "
                "use fallback runtime."
            )

        recovery: dict[str, Any] = {
            "kind": kind,
            "error_code": error_code,
            "manual_takeover_required": manual_takeover_required,
            "recoverable": recoverable,
            "suggestion": suggestion,
            "action": action.action,
            "profile_id": observation.profile_id or action.profile_id,
            "url": cls._short_text(observation.url or action.url, 500),
        }
        selector = cls._short_text(action.selector or action.ref, 240)
        if selector:
            recovery["selector"] = selector
        scope = cls._action_scope(action)
        if scope:
            recovery["scope"] = scope
        if observation.decision and observation.decision.matched_rule_id:
            recovery["matched_rule_id"] = observation.decision.matched_rule_id
        return recovery

    # pylint: enable=R0912,R0915

    @staticmethod
    def _failure_text(observation: BrowserObservation) -> str:
        parts: list[str] = [
            observation.error or "",
            observation.message or "",
        ]
        data = observation.data if isinstance(observation.data, dict) else {}
        for key in ("error", "message", "error_code"):
            value = data.get(key)
            if isinstance(value, (str, int, float)):
                parts.append(str(value))
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("error", "message", "error_code"):
                value = inner.get(key)
                if isinstance(value, (str, int, float)):
                    parts.append(str(value))
        scope = data.get("scope")
        if isinstance(scope, dict):
            value = scope.get("error")
            if isinstance(value, (str, int, float)):
                parts.append(str(value))
        return " ".join(parts).lower()

    @classmethod
    def _action_scope(cls, action: BrowserAction) -> dict[str, str]:
        scope: dict[str, str] = {}
        for source, target in (
            ("frame_selector", "frame_selector"),
            ("shadow_host_selector", "shadow_host_selector"),
        ):
            value = cls._short_text(action.metadata.get(source), 240)
            if value:
                scope[target] = value
        return scope

    @staticmethod
    def _short_text(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    @staticmethod
    def _requires_manual_takeover(observation: BrowserObservation) -> bool:
        if (
            observation.decision
            and observation.decision.matched_rule_id
            == "browser_manual_takeover_required"
        ):
            return True
        data = observation.data or {}
        if data.get("interruption") == "stopped":
            return True
        page_context = data.get("page_context")
        if isinstance(page_context, dict) and page_context.get(
            "requires_manual_takeover",
        ):
            return True
        inner = data.get("data")
        if isinstance(inner, dict):
            page_context = inner.get("page_context")
            if isinstance(page_context, dict) and page_context.get(
                "requires_manual_takeover",
            ):
                return True
        return False

    @staticmethod
    def _error_code(observation: BrowserObservation, default: str) -> str:
        code = (
            observation.data.get("error_code")
            if isinstance(observation.data, dict)
            else ""
        )
        if code:
            return str(code)
        if observation.decision and observation.decision.matched_rule_id:
            return observation.decision.matched_rule_id
        return default
