# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from pathlib import Path
from typing import Any

from .artifacts import artifact_from_path, register_artifact
from .evaluations import (
    apply_report_summary,
    build_report,
    make_finding,
    report_block_reason,
)
from .fast_fail import capability_gate_report
from .lifecycle import (
    EXECUTION_STALLED_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    TERMINAL_TASK_STATUSES,
    heartbeat_task_run,
    transition_task_run,
    transition_task_step,
)
from .models import (
    Artifact,
    Capability,
    CapabilityRun,
    CapabilityRunRequest,
    DeliveryTarget,
    TaskRun,
    TaskStep,
    utc_now,
)
from .repo import CapabilityRepository
from .template import build_parameter_values, render_template, validate_step_parameters


_TERMINAL_BROWSER_TASK_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "manual_takeover_required",
}
_CAPABILITY_TASK_RUN_HEARTBEAT_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_SECONDS


def _shared_browser_task_runtime() -> Any:
    from ..routers.browser_workbench import get_browser_task_runtime

    return get_browser_task_runtime()


def _safe_filename(value: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value.strip(), flags=re.UNICODE)
    return slug.strip("-")[:80] or "capability-output"


def _event_text(event: Any) -> str:
    if isinstance(event, (tuple, list)) and event:
        return str(event[0])
    return str(event)


def _metadata_bool(metadata: dict[str, Any], key: str, default: bool = False) -> bool:
    value = metadata.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _browser_task_artifact_summary(payload: dict[str, Any]) -> dict[str, Any]:
    screenshots = payload.get("screenshots") if isinstance(payload, dict) else []
    observation_summaries = payload.get("observation_summaries") if isinstance(payload, dict) else []
    network = payload.get("network") if isinstance(payload, dict) else []
    console = payload.get("console") if isinstance(payload, dict) else []
    return {
        "event_count": int(payload.get("event_count") or 0),
        "screenshot_count": len(screenshots) if isinstance(screenshots, list) else 0,
        "latest_screenshot": screenshots[-1] if isinstance(screenshots, list) and screenshots else "",
        "observation_summary_count": (
            len(observation_summaries) if isinstance(observation_summaries, list) else 0
        ),
        "network_count": len(network) if isinstance(network, list) else 0,
        "console_count": len(console) if isinstance(console, list) else 0,
    }


class CapabilityExecutor:
    def __init__(
        self,
        *,
        repo: CapabilityRepository,
        runner: Any,
        workspace_dir: str | Path,
        agent_id: str,
        channel_manager: Any | None = None,
        browser_task_runtime: Any | None = None,
    ):
        self._repo = repo
        self._runner = runner
        self._workspace_dir = Path(workspace_dir).expanduser()
        self._agent_id = agent_id
        self._channel_manager = channel_manager
        self._browser_task_runtime = browser_task_runtime

    async def run(
        self,
        capability: Capability,
        request: CapabilityRunRequest,
        *,
        source: str = "capability",
    ) -> CapabilityRun:
        started = utc_now()
        task_run = TaskRun(
            agent_id=self._agent_id,
            source="capability" if source != "cron" else "cron",
            session_id=request.session_id,
            title=capability.name,
            user_intent=str(request.parameters.get("task_goal") or capability.description),
            status="dispatched",
            started_at=started,
            last_heartbeat_at=started,
        )
        cap_run = CapabilityRun(
            capability_id=capability.id,
            capability_name=capability.name,
            task_run_id=task_run.id,
            status="dispatched",
            parameters=dict(request.parameters),
            started_at=started,
        )
        await self._repo.upsert_task_run(task_run)
        await self._repo.save_capability_run(cap_run)

        transition_task_run(task_run, "acked")
        cap_run.status = "acked"
        await self._repo.upsert_task_run(task_run)
        await self._repo.save_capability_run(cap_run)

        outputs: list[str] = []
        artifacts: list[Artifact] = []
        try:
            try:
                values = build_parameter_values(capability.parameters, request.parameters)
            except ValueError as exc:
                report = build_report(
                    task_run_id=task_run.id,
                    evaluator="fast_fail_gate",
                    findings=[
                        make_finding(
                            code="capability_parameters_missing",
                            title="能力参数不完整",
                            detail=str(exc),
                        )
                    ],
                    passed_summary="Fast-fail gate passed.",
                    blocked_summary="Fast-fail gate blocked this capability run before execution.",
                )
                await self._block_run(task_run, cap_run, report, str(exc))
                raise

            task_run.user_intent = str(values.get("task_goal") or capability.description)
            cap_run.parameters = values
            report = capability_gate_report(
                task_run_id=task_run.id,
                capability=capability,
                request=request,
                values=values,
                workspace_dir=self._workspace_dir,
                source=source,
            )
            await self._repo.save_evaluation_report(report)
            apply_report_summary(task_run, report)
            if report.conclusion == "blocked":
                reason = report_block_reason(report)
                await self._block_run(task_run, cap_run, report, reason)
                raise PermissionError(reason)

            transition_task_run(task_run, "running")
            cap_run.status = "running"
            await self._repo.upsert_task_run(task_run)
            await self._repo.save_capability_run(cap_run)

            ordered_steps = sorted(capability.steps, key=lambda step: step.index)
            for index, step in enumerate(ordered_steps):
                task_step = TaskStep(
                    index=index,
                    kind=(
                        "delivery"
                        if step.kind == "delivery"
                        else "tool_call"
                        if step.kind in {"gui_prompt", "browser_prompt"}
                        else "message"
                    ),
                    title=step.title,
                    status="running",
                    risk_level=step.risk_level,
                    started_at=utc_now(),
                    tool_name=step.skill_name,
                    metadata={
                        "capability_step_id": step.id,
                        "capability_step_kind": step.kind,
                    },
                )
                if step.kind in {"gui_prompt", "browser_prompt"}:
                    replay_path = self._repo.replay_path(task_run.id, task_step.id)
                    task_step.metadata.update(
                        {
                            "gui_target": (
                                "browser"
                                if step.kind == "browser_prompt"
                                else step.gui_target or "browser"
                            ),
                            "replay_policy": step.replay_policy or "full",
                            "replay_path": str(replay_path),
                        }
                    )
                    if step.kind == "browser_prompt":
                        task_step.metadata.update(
                            {
                                "browser_profile_id": step.profile_id or "default",
                                "browser_target_domains": step.target_domains,
                                "browser_access": step.access or "read",
                            }
                        )
                task_run.steps.append(task_step)
                heartbeat_task_run(task_run)
                await self._repo.upsert_task_run(task_run)

                validate_step_parameters(step, values)
                instruction = render_template(step.instruction_template, values)
                if step.kind == "skill_prompt" and step.skill_name:
                    instruction = (
                        f"请使用 {step.skill_name} skill 完成以下步骤：\n\n"
                        f"{instruction}"
                    )

                if step.kind in {"agent_prompt", "skill_prompt"}:
                    detail = await self._await_with_task_run_heartbeat(
                        task_run,
                        self._run_agent_step(
                            instruction,
                            request=request,
                            timeout_seconds=step.timeout_seconds,
                        ),
                    )
                    if detail:
                        outputs.append(detail)
                    task_step.detail = detail[-4000:] if detail else "步骤已完成"
                elif step.kind == "gui_prompt":
                    await self._record_replay_event(
                        run_id=task_run.id,
                        step_id=task_step.id,
                        kind="prompt",
                        payload={
                            "title": step.title,
                            "target": step.gui_target or "browser",
                            "instruction": instruction,
                        },
                    )
                    await self._record_replay_event(
                        run_id=task_run.id,
                        step_id=task_step.id,
                        kind="approval",
                        payload={
                            "manual_gui_ack": request.manual_gui_ack,
                            "approved_risks": request.approved_risks,
                        },
                    )
                    gui_instruction = (
                        "这是一个需要可视化界面操作的高风险步骤。"
                        "请优先使用现有 browser/computer 相关技能或工具守卫，"
                        "执行前后说明关键动作和结果；不要绕过审批机制。\n\n"
                        f"目标：{step.gui_target or 'browser'}\n\n{instruction}"
                    )
                    detail = await self._await_with_task_run_heartbeat(
                        task_run,
                        self._run_agent_step(
                            gui_instruction,
                            request=request,
                            timeout_seconds=step.timeout_seconds,
                        ),
                    )
                    await self._record_replay_event(
                        run_id=task_run.id,
                        step_id=task_step.id,
                        kind="result",
                        payload={"output": detail[-12000:] if detail else ""},
                    )
                    if detail:
                        outputs.append(detail)
                    task_step.detail = detail[-4000:] if detail else "GUI 步骤已完成"
                elif step.kind == "browser_prompt":
                    await self._record_replay_event(
                        run_id=task_run.id,
                        step_id=task_step.id,
                        kind="browser_prompt",
                        payload={
                            "title": step.title,
                            "profile_id": step.profile_id or "default",
                            "target_domains": step.target_domains,
                            "access": step.access or "read",
                            "instruction": instruction,
                        },
                    )
                    detail = await self._await_with_task_run_heartbeat(
                        task_run,
                        self._create_browser_task_step(
                            task_run=task_run,
                            task_step=task_step,
                            capability=capability,
                            step=step,
                            instruction=instruction,
                        ),
                    )
                    await self._record_replay_event(
                        run_id=task_run.id,
                        step_id=task_step.id,
                        kind="browser_result",
                        payload={"output": detail[-12000:] if detail else ""},
                    )
                    if detail:
                        outputs.append(detail)
                    task_step.detail = detail[-4000:] if detail else "浏览器步骤已完成"
                elif step.kind == "delivery":
                    step_artifacts = await self._await_with_task_run_heartbeat(
                        task_run,
                        self._deliver(
                            capability=capability,
                            request=request,
                            text="\n\n".join(outputs).strip() or instruction,
                            run_id=task_run.id,
                            step_id=task_step.id,
                        ),
                    )
                    artifacts.extend(step_artifacts)
                    task_run.artifacts.extend(step_artifacts)
                    task_step.detail = f"已交付 {len(step_artifacts)} 个产物"
                else:
                    task_step.detail = "未知步骤类型，已跳过"
                    task_step.status = "skipped"

                if task_step.status == "running":
                    transition_task_step(task_step, "succeeded")
                heartbeat_task_run(task_run)
                await self._repo.upsert_task_run(task_run)

            transition_task_run(task_run, "succeeded")
            cap_run.status = "succeeded"
            cap_run.ended_at = task_run.ended_at
            cap_run.artifacts = artifacts
            cap_run.output_text = "\n\n".join(outputs).strip()
        except asyncio.TimeoutError as exc:
            await self._fail_run(
                task_run,
                cap_run,
                status="stalled",
                step_status="stalled",
                title="能力执行卡死",
                detail=str(exc) or "执行步骤超过超时限制。",
                artifacts=artifacts,
                outputs=outputs,
            )
            raise
        except PermissionError as exc:
            if task_run.status != "blocked":
                report = build_report(
                    task_run_id=task_run.id,
                    evaluator="fast_fail_gate",
                    findings=[
                        make_finding(
                            code="permission_blocked",
                            title="权限或风控策略阻断",
                            detail=str(exc),
                        )
                    ],
                    passed_summary="Fast-fail gate passed.",
                    blocked_summary="Fast-fail gate blocked this capability run before execution.",
                )
                await self._block_run(task_run, cap_run, report, str(exc))
            raise
        except Exception as exc:
            if task_run.status in {"blocked", "failed", "stalled", "timed_out"}:
                await self._repo.upsert_task_run(task_run)
                await self._repo.save_capability_run(cap_run)
                raise
            await self._fail_run(
                task_run,
                cap_run,
                status="failed",
                step_status="failed",
                title="能力执行失败",
                detail=str(exc),
                artifacts=artifacts,
                outputs=outputs,
            )
            raise

        await self._repo.upsert_task_run(task_run)
        await self._repo.save_capability_run(cap_run)
        return cap_run

    async def _block_run(
        self,
        task_run: TaskRun,
        cap_run: CapabilityRun,
        report,
        reason: str,
    ) -> None:
        apply_report_summary(task_run, report)
        await self._repo.save_evaluation_report(report)
        await self._fail_run(
            task_run,
            cap_run,
            status="blocked",
            step_status="blocked",
            title="能力执行被阻断",
            detail=reason,
            artifacts=[],
            outputs=[],
        )

    async def _fail_run(
        self,
        task_run: TaskRun,
        cap_run: CapabilityRun,
        *,
        status,
        step_status,
        title: str,
        detail: str,
        artifacts: list[Artifact],
        outputs: list[str],
    ) -> None:
        ended = utc_now()
        for step in reversed(task_run.steps):
            if step.status == "running":
                step.detail = detail
                transition_task_step(step, step_status)
                break
        else:
            task_run.steps.append(
                TaskStep(
                    index=len(task_run.steps),
                    kind="error",
                    title=title,
                    detail=detail,
                    status=step_status,
                    started_at=ended,
                    ended_at=ended,
                )
            )
        transition_task_run(task_run, status, reason=detail)
        task_run.artifacts.extend(
            artifact for artifact in artifacts if artifact not in task_run.artifacts
        )
        cap_run.status = status
        cap_run.ended_at = task_run.ended_at or ended
        cap_run.error = detail
        cap_run.artifacts = artifacts
        cap_run.output_text = "\n\n".join(outputs).strip() or None
        await self._repo.upsert_task_run(task_run)
        await self._repo.save_capability_run(cap_run)

    async def _run_agent_step(
        self,
        instruction: str,
        *,
        request: CapabilityRunRequest,
        timeout_seconds: int | None,
    ) -> str:
        if self._runner is None:
            return f"未连接运行器，步骤指令已记录：{instruction}"

        payload = {
            "input": instruction,
            "session_id": request.session_id or "capability:local",
            "user_id": request.user_id or "capability",
            "channel": request.channel or "console",
        }

        async def collect() -> str:
            parts: list[str] = []
            async for event in self._runner.stream_query(payload):
                text = _event_text(event)
                if text:
                    parts.append(text)
            return "\n".join(parts)[-12000:]

        return await asyncio.wait_for(
            collect(),
            timeout=timeout_seconds or EXECUTION_STALLED_SECONDS,
        )

    async def _task_run_heartbeat_loop(self, task_run: TaskRun) -> None:
        interval = _CAPABILITY_TASK_RUN_HEARTBEAT_INTERVAL_SECONDS
        if interval <= 0:
            return
        while task_run.status not in TERMINAL_TASK_STATUSES:
            await asyncio.sleep(interval)
            if task_run.status in TERMINAL_TASK_STATUSES:
                return
            heartbeat_task_run(task_run)
            try:
                await self._repo.upsert_task_run(task_run)
            except Exception:  # pragma: no cover - best-effort heartbeat
                return

    async def _await_with_task_run_heartbeat(
        self,
        task_run: TaskRun,
        awaitable,
    ) -> Any:
        heartbeat_task = asyncio.create_task(
            self._task_run_heartbeat_loop(task_run),
            name=f"capability-heartbeat-{task_run.id}",
        )
        try:
            return await awaitable
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _create_browser_task_step(
        self,
        *,
        task_run: TaskRun,
        task_step: TaskStep,
        capability: Capability,
        step: Any,
        instruction: str,
    ) -> str:
        from ..browser_workbench.models import BrowserTaskRequest

        runtime = self._browser_task_runtime or _shared_browser_task_runtime()
        metadata = {
            **step.metadata,
            "capability_id": capability.id,
            "capability_name": capability.name,
            "capability_task_run_id": task_run.id,
            "capability_step_id": step.id,
            "capability_step_title": step.title,
            "source": "capability.browser_prompt",
        }
        max_actions = int(metadata.pop("max_actions", 200) or 200)
        max_duration_seconds = float(
            metadata.pop("max_duration_seconds", step.timeout_seconds or 1800)
            or 1800
        )
        browser_task = await runtime.create(
            BrowserTaskRequest(
                run_id=f"{task_run.id}-{task_step.id}",
                profile_id=step.profile_id or "default",
                target_domains=step.target_domains,
                access=step.access or "read",
                mode="background_observable",
                instruction=instruction,
                requires_foreground_confirmation=step.requires_manual_confirmation,
                max_actions=max_actions,
                max_duration_seconds=max_duration_seconds,
                metadata=metadata,
            )
        )
        final_task = browser_task
        wait_for_completion = _metadata_bool(step.metadata, "wait_for_completion")
        wait_result = "not_requested"
        if wait_for_completion and (step.access or "read") == "read":
            wait_timeout_seconds = float(
                step.metadata.get("wait_timeout_seconds")
                or step.timeout_seconds
                or min(max_duration_seconds, 60.0)
            )
            poll_interval_seconds = float(
                step.metadata.get("poll_interval_seconds") or 0.25
            )
            final_task, wait_result = await self._wait_for_browser_task(
                runtime=runtime,
                run_id=browser_task.run_id,
                timeout_seconds=wait_timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            final_task = final_task or browser_task
        elif wait_for_completion:
            wait_result = "skipped_non_read_access"

        plan = browser_task.metadata.get("plan") or {}
        final_plan = final_task.metadata.get("plan") or plan
        artifacts_payload: dict[str, Any] | None = None
        if wait_result == "completed" and hasattr(runtime, "artifacts"):
            artifacts_payload = runtime.artifacts(final_task.run_id)
        artifact_summary = (
            _browser_task_artifact_summary(artifacts_payload)
            if isinstance(artifacts_payload, dict)
            else None
        )
        task_step.metadata.update(
            {
                "browser_task_run_id": final_task.run_id,
                "browser_task_status": final_task.status,
                "browser_task_error_code": final_task.error_code,
                "browser_task_plan": final_plan,
                "browser_task_wait_result": wait_result,
                "browser_task_planned_action_count": final_task.planned_action_count,
                "browser_task_completed_action_count": final_task.completed_action_count,
                "browser_task_artifacts_available": True,
            }
        )
        if artifact_summary is not None:
            task_step.metadata["browser_task_artifacts"] = artifact_summary
        await self._record_replay_event(
            run_id=task_run.id,
            step_id=task_step.id,
            kind="browser_task_created",
            payload={
                "browser_task_run_id": browser_task.run_id,
                "status": browser_task.status,
                "profile_id": browser_task.profile_id,
                "target_domains": browser_task.target_domains,
                "access": browser_task.access,
                "plan": plan,
            },
        )
        if wait_result not in {"not_requested", "skipped_non_read_access"}:
            await self._record_replay_event(
                run_id=task_run.id,
                step_id=task_step.id,
                kind="browser_task_wait_result",
                payload={
                    "browser_task_run_id": final_task.run_id,
                    "status": final_task.status,
                    "wait_result": wait_result,
                    "error_code": final_task.error_code,
                    "artifacts": artifact_summary or {},
                },
            )
        if wait_for_completion and final_task.status in {
            "failed",
            "cancelled",
            "manual_takeover_required",
        }:
            raise RuntimeError(
                "浏览器后台任务未成功完成："
                f"{final_task.status}"
                + (f" / {final_task.error_code}" if final_task.error_code else "")
            )
        adapter_id = str(final_plan.get("adapter_id") or "generic")
        artifact_text = ""
        if artifact_summary:
            artifact_text = (
                "\nArtifacts: "
                f"{artifact_summary['event_count']} events, "
                f"{artifact_summary['screenshot_count']} screenshots"
            )
        return (
            "已创建澜台后台浏览器任务。\n"
            f"Run ID: {final_task.run_id}\n"
            f"Status: {final_task.status}\n"
            f"Plan: {final_plan.get('strategy') or 'planner'} / {adapter_id}"
            f"{artifact_text}\n"
            "可在 Browser Workbench 后台任务面板继续查看、暂停、终止或确认。"
        )

    async def _wait_for_browser_task(
        self,
        *,
        runtime: Any,
        run_id: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> tuple[Any, str]:
        if not hasattr(runtime, "get_run"):
            return None, "not_observable"
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        interval = max(0.05, min(poll_interval_seconds, 5.0))
        last_run = runtime.get_run(run_id)
        while last_run is not None:
            if last_run.status in _TERMINAL_BROWSER_TASK_STATUSES:
                return last_run, "completed" if last_run.status == "completed" else last_run.status
            if asyncio.get_running_loop().time() >= deadline:
                return last_run, "timeout"
            await asyncio.sleep(interval)
            last_run = runtime.get_run(run_id)
        return last_run, "not_found"

    async def _deliver(
        self,
        *,
        capability: Capability,
        request: CapabilityRunRequest,
        text: str,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> list[Artifact]:
        if request.suppress_delivery:
            return []
        targets = (
            request.delivery_targets
            if request.delivery_targets is not None
            else capability.delivery_defaults
        )
        artifacts: list[Artifact] = []
        for target in targets:
            if target.type == "chat":
                await self._deliver_chat(request, text)
            elif target.type == "workspace_file":
                artifacts.append(
                    await self._deliver_file(
                        capability,
                        target,
                        text,
                        run_id=run_id,
                        step_id=step_id,
                    )
                )
            elif target.type == "wiki_draft":
                artifacts.append(
                    await self._deliver_wiki_draft(
                        capability,
                        target,
                        text,
                        run_id=run_id,
                        step_id=step_id,
                    )
                )
        return artifacts

    async def _deliver_chat(self, request: CapabilityRunRequest, text: str) -> None:
        if (
            self._channel_manager is None
            or not request.user_id
            or not request.session_id
            or not hasattr(self._channel_manager, "send_text")
        ):
            return
        await self._channel_manager.send_text(
            channel=request.channel or "console",
            user_id=request.user_id,
            session_id=request.session_id,
            text=text,
            meta={"source": "capability"},
        )

    async def _deliver_file(
        self,
        capability: Capability,
        target: DeliveryTarget,
        text: str,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> Artifact:
        relative = target.path or f"capabilities/outputs/{_safe_filename(capability.name)}.md"
        path = (self._workspace_dir / relative).resolve()
        workspace_root = self._workspace_dir.resolve()
        if workspace_root not in path.parents and path != workspace_root:
            raise ValueError("交付路径必须位于当前工作区内")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        artifact = artifact_from_path(
            path,
            title=target.title or capability.name,
            producer="capability",
            run_id=run_id,
            step_id=step_id,
            classification="user_content",
        )
        register_artifact(self._workspace_dir, artifact)
        return artifact

    async def _record_replay_event(
        self,
        *,
        run_id: str,
        step_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        await self._repo.append_replay_event(
            run_id=run_id,
            step_id=step_id,
            event={
                "ts": utc_now().isoformat(),
                "kind": kind,
                "payload": payload,
            },
        )

    async def _deliver_wiki_draft(
        self,
        capability: Capability,
        target: DeliveryTarget,
        text: str,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> Artifact:
        filename = f"{_safe_filename(target.title or capability.name)}.md"
        path = self._workspace_dir / ".wiki" / "drafts" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        artifact = artifact_from_path(
            path,
            title=target.title or capability.name,
            producer="capability",
            run_id=run_id,
            step_id=step_id,
            classification="user_content",
        )
        register_artifact(self._workspace_dir, artifact)
        return artifact
