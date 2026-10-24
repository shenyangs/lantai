# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..concurrency import get_runtime_concurrency_governor
from .artifacts import artifact_from_path, register_artifact
from .evaluations import (
    apply_report_summary,
    build_report,
    make_finding,
    report_block_reason,
)
from .executor import CapabilityExecutor
from .fast_fail import pipeline_gate_report
from .lifecycle import (
    HEARTBEAT_INTERVAL_SECONDS,
    TERMINAL_TASK_STATUSES,
    heartbeat_task_run,
    transition_task_run,
    transition_task_step,
)
from .models import (
    Artifact,
    CapabilityRunRequest,
    DeliveryTarget,
    Pipeline,
    PipelineNode,
    PipelineRun,
    PipelineRunNode,
    PipelineRunRequest,
    TaskRun,
    TaskStep,
    utc_now,
)
from .policy import capability_has_interactive_steps, ensure_capability_allowed
from .repo import CapabilityRepository
from .template import build_parameter_values, render_template


def _safe_filename(value: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value.strip(), flags=re.UNICODE)
    return slug.strip("-")[:80] or "pipeline-output"


_PIPELINE_TASK_RUN_HEARTBEAT_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_SECONDS


def _render_nested(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return render_template(value, context)
    if isinstance(value, list):
        return [_render_nested(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _render_nested(item, context) for key, item in value.items()}
    return value


class PipelineValidationError(ValueError):
    pass


def _topological_layers(pipeline: Pipeline) -> list[list[PipelineNode]]:
    node_by_id = {node.id: node for node in pipeline.nodes}
    if len(node_by_id) != len(pipeline.nodes):
        raise PipelineValidationError("Pipeline 节点 ID 不能重复")
    if not node_by_id:
        raise PipelineValidationError("Pipeline 至少需要一个能力节点")

    indegree = {node_id: 0 for node_id in node_by_id}
    children: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
    for edge in pipeline.edges:
        if edge.source not in node_by_id or edge.target not in node_by_id:
            raise PipelineValidationError("Pipeline 连线引用了不存在的节点")
        children[edge.source].append(edge.target)
        indegree[edge.target] += 1

    ready = sorted(
        [node_id for node_id, degree in indegree.items() if degree == 0],
        key=lambda node_id: node_by_id[node_id].index,
    )
    layers: list[list[PipelineNode]] = []
    visited = 0
    while ready:
        current_ids = ready
        ready = []
        layers.append([node_by_id[node_id] for node_id in current_ids])
        for node_id in current_ids:
            visited += 1
            for child in children[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        ready.sort(key=lambda node_id: node_by_id[node_id].index)

    if visited != len(node_by_id):
        raise PipelineValidationError("Pipeline 不能包含环形依赖")
    return layers


class PipelineExecutor:
    def __init__(
        self,
        *,
        repo: CapabilityRepository,
        runner: Any,
        workspace_dir: str | Path,
        agent_id: str,
        channel_manager: Any | None = None,
    ):
        self._repo = repo
        self._runner = runner
        self._workspace_dir = Path(workspace_dir).expanduser()
        self._agent_id = agent_id
        self._channel_manager = channel_manager

    async def run(
        self,
        pipeline: Pipeline,
        request: PipelineRunRequest,
        *,
        source: str = "pipeline",
    ) -> PipelineRun:
        layers = _topological_layers(pipeline)
        started = utc_now()
        task_run = TaskRun(
            agent_id=self._agent_id,
            source="pipeline" if source != "cron" else "cron",
            session_id=request.session_id,
            title=pipeline.name,
            user_intent=pipeline.description,
            status="dispatched",
            started_at=started,
            last_heartbeat_at=started,
            steps=[],
        )
        pipeline_run = PipelineRun(
            pipeline_id=pipeline.id,
            pipeline_name=pipeline.name,
            task_run_id=task_run.id,
            status="dispatched",
            parameters=dict(request.parameters),
            started_at=started,
        )
        await self._repo.upsert_task_run(task_run)
        await self._repo.save_pipeline_run(pipeline_run)

        transition_task_run(task_run, "acked")
        pipeline_run.status = "acked"
        await self._repo.upsert_task_run(task_run)
        await self._repo.save_pipeline_run(pipeline_run)

        try:
            values = build_parameter_values(pipeline.parameters, request.parameters)
        except ValueError as exc:
            report = build_report(
                task_run_id=task_run.id,
                evaluator="fast_fail_gate",
                findings=[
                    make_finding(
                        code="pipeline_parameters_missing",
                        title="Pipeline 参数不完整",
                        detail=str(exc),
                    )
                ],
                passed_summary="Fast-fail gate passed.",
                blocked_summary="Fast-fail gate blocked this pipeline run before execution.",
            )
            await self._block_run(task_run, pipeline_run, report, str(exc))
            raise

        pipeline_run.parameters = values
        report = await pipeline_gate_report(
            task_run_id=task_run.id,
            pipeline=pipeline,
            request=request,
            workspace_dir=self._workspace_dir,
            source=source,
            repo=self._repo,
        )
        await self._repo.save_evaluation_report(report)
        apply_report_summary(task_run, report)
        if report.conclusion == "blocked":
            reason = report_block_reason(report)
            await self._block_run(task_run, pipeline_run, report, reason)
            raise PermissionError(reason)

        transition_task_run(task_run, "running")
        pipeline_run.status = "running"
        await self._repo.upsert_task_run(task_run)
        await self._repo.save_pipeline_run(pipeline_run)

        node_outputs: dict[str, dict[str, Any]] = {}
        run_nodes: dict[str, PipelineRunNode] = {}
        all_artifacts: list[Artifact] = []
        error: str | None = None
        error_status = "failed"
        semaphore = asyncio.Semaphore(pipeline.runtime.max_parallelism)
        capability_executor = CapabilityExecutor(
            repo=self._repo,
            runner=self._runner,
            workspace_dir=self._workspace_dir,
            agent_id=self._agent_id,
            channel_manager=self._channel_manager,
        )

        async def _run_node_admitted(node: PipelineNode) -> PipelineRunNode:
            async with semaphore:
                run_node_record = PipelineRunNode(
                    node_id=node.id,
                    title=node.title,
                    capability_id=node.capability_id,
                    started_at=utc_now(),
                )
                run_nodes[node.id] = run_node_record
                task_step = TaskStep(
                    index=len(task_run.steps),
                    kind="message",
                    title=f"Pipeline 节点：{node.title}",
                    status="running",
                    started_at=run_node_record.started_at,
                    metadata={"pipeline_node_id": node.id},
                )
                task_run.steps.append(task_step)
                heartbeat_task_run(task_run)
                await self._repo.upsert_task_run(task_run)

                try:
                    capability = await self._repo.get_capability(node.capability_id)
                    if capability is None:
                        raise KeyError(f"能力不存在：{node.capability_id}")
                    ensure_capability_allowed(capability)
                    if source == "cron" and capability_has_interactive_steps(capability):
                        raise PermissionError(
                            "定时 Pipeline 不允许包含 GUI/浏览器自动操作能力。"
                        )

                    context = {
                        "pipeline": values,
                        "nodes": node_outputs,
                    }
                    node_parameters = _render_nested(node.parameters, context)
                    cap_run = await capability_executor.run(
                        capability,
                        CapabilityRunRequest(
                            parameters=node_parameters,
                            session_id=request.session_id,
                            user_id=request.user_id,
                            channel=request.channel,
                            approved_risks=request.approved_risks,
                            manual_gui_ack=request.manual_gui_ack,
                            suppress_delivery=True,
                        ),
                        source="pipeline_cron" if source == "cron" else "pipeline",
                    )
                    run_node_record.capability_run_id = cap_run.id
                    run_node_record.task_run_id = cap_run.task_run_id
                    run_node_record.status = cap_run.status
                    run_node_record.output_text = cap_run.output_text
                    run_node_record.artifacts = cap_run.artifacts
                    run_node_record.ended_at = cap_run.ended_at or utc_now()
                    node_outputs[node.id] = {
                        "output_text": cap_run.output_text or "",
                        "artifacts": json.dumps(
                            [artifact.model_dump(mode="json") for artifact in cap_run.artifacts],
                            ensure_ascii=False,
                        ),
                    }
                    all_artifacts.extend(cap_run.artifacts)
                    transition_task_step(task_step, "succeeded")
                    task_step.detail = (cap_run.output_text or "节点已完成")[-4000:]
                    task_step.ended_at = run_node_record.ended_at
                    heartbeat_task_run(task_run)
                    await self._repo.upsert_task_run(task_run)
                    return run_node_record
                except asyncio.TimeoutError as exc:
                    run_node_record.status = "stalled"
                    run_node_record.error = str(exc)
                    run_node_record.ended_at = utc_now()
                    transition_task_step(task_step, "stalled")
                    task_step.detail = str(exc)
                    heartbeat_task_run(task_run)
                    await self._repo.upsert_task_run(task_run)
                    return run_node_record

                except (KeyError, PermissionError, ValueError) as exc:
                    run_node_record.status = "blocked"
                    run_node_record.error = str(exc)
                    run_node_record.ended_at = utc_now()
                    transition_task_step(task_step, "blocked")
                    task_step.detail = str(exc)
                    heartbeat_task_run(task_run)
                    await self._repo.upsert_task_run(task_run)
                    return run_node_record
                except Exception as exc:
                    run_node_record.status = "failed"
                    run_node_record.error = str(exc)
                    run_node_record.ended_at = utc_now()
                    transition_task_step(task_step, "failed")
                    task_step.detail = str(exc)
                    heartbeat_task_run(task_run)
                    await self._repo.upsert_task_run(task_run)
                    return run_node_record

        async def run_node(node: PipelineNode) -> PipelineRunNode:
            owner_key = f"{pipeline_run.id}:{node.id}"
            async with get_runtime_concurrency_governor().slot(
                category="pipeline",
                owner_key=owner_key,
            ):
                return await _run_node_admitted(node)

        try:
            for layer in layers:
                results = await self._await_with_task_run_heartbeat(
                    task_run,
                    asyncio.gather(*(run_node(node) for node in layer)),
                )
                pipeline_run.nodes = [
                    run_nodes[node.id]
                    for node in sorted(pipeline.nodes, key=lambda item: item.index)
                    if node.id in run_nodes
                ]
                await self._repo.save_pipeline_run(pipeline_run)
                failed = next(
                    (
                        item
                        for item in results
                        if item.status in {"failed", "blocked", "stalled", "timed_out"}
                    ),
                    None,
                )
                if failed is not None:
                    error = failed.error or "Pipeline 节点执行失败"
                    error_status = (
                        failed.status
                        if failed.status in {"blocked", "stalled", "timed_out"}
                        else "failed"
                    )
                    break

            if error is None:
                output_text = self._build_output_text(pipeline, run_nodes)
                delivery_artifacts = await self._await_with_task_run_heartbeat(
                    task_run,
                    self._deliver(
                        pipeline=pipeline,
                        request=request,
                        text=output_text,
                        run_id=task_run.id,
                    ),
                )
                all_artifacts.extend(delivery_artifacts)
                task_run.artifacts.extend(all_artifacts)
                pipeline_run.artifacts = all_artifacts
                pipeline_run.output_text = output_text
                pipeline_run.status = "succeeded"
                transition_task_run(task_run, "succeeded")
            else:
                pipeline_run.status = error_status
                pipeline_run.error = error
                pipeline_run.artifacts = all_artifacts
                transition_task_run(task_run, error_status, reason=error)
                task_run.steps.append(
                    TaskStep(
                        index=len(task_run.steps),
                        kind="error",
                        title="Pipeline 执行失败",
                        detail=error,
                        status=(
                            error_status
                            if error_status in {"blocked", "stalled"}
                            else "failed"
                        ),
                        started_at=utc_now(),
                        ended_at=utc_now(),
                    )
                )

            ended = utc_now()
            pipeline_run.ended_at = ended
            task_run.ended_at = task_run.ended_at or ended
            await self._repo.upsert_task_run(task_run)
            await self._repo.save_pipeline_run(pipeline_run)
            if error is not None:
                raise RuntimeError(error)
            return pipeline_run
        except Exception as exc:
            if task_run.status not in {"blocked", "failed", "stalled", "timed_out"}:
                await self._fail_run(
                    task_run,
                    pipeline_run,
                    status="failed",
                    title="Pipeline 执行失败",
                    detail=str(exc),
                    artifacts=all_artifacts,
                )
            else:
                await self._repo.upsert_task_run(task_run)
                await self._repo.save_pipeline_run(pipeline_run)
            raise

    async def _task_run_heartbeat_loop(self, task_run: TaskRun) -> None:
        interval = _PIPELINE_TASK_RUN_HEARTBEAT_INTERVAL_SECONDS
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
            name=f"pipeline-heartbeat-{task_run.id}",
        )
        try:
            return await awaitable
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _block_run(
        self,
        task_run: TaskRun,
        pipeline_run: PipelineRun,
        report,
        reason: str,
    ) -> None:
        apply_report_summary(task_run, report)
        await self._repo.save_evaluation_report(report)
        await self._fail_run(
            task_run,
            pipeline_run,
            status="blocked",
            title="Pipeline 执行被阻断",
            detail=reason,
            artifacts=[],
        )

    async def _fail_run(
        self,
        task_run: TaskRun,
        pipeline_run: PipelineRun,
        *,
        status,
        title: str,
        detail: str,
        artifacts: list[Artifact],
    ) -> None:
        ended = utc_now()
        task_run.steps.append(
            TaskStep(
                index=len(task_run.steps),
                kind="error",
                title=title,
                detail=detail,
                status="blocked" if status == "blocked" else "failed",
                started_at=ended,
                ended_at=ended,
            )
        )
        transition_task_run(task_run, status, reason=detail)
        pipeline_run.status = status
        pipeline_run.ended_at = task_run.ended_at or ended
        pipeline_run.error = detail
        pipeline_run.artifacts = artifacts
        await self._repo.upsert_task_run(task_run)
        await self._repo.save_pipeline_run(pipeline_run)

    def _build_output_text(
        self,
        pipeline: Pipeline,
        run_nodes: dict[str, PipelineRunNode],
    ) -> str:
        parts = [f"# {pipeline.name}", ""]
        for node in sorted(pipeline.nodes, key=lambda item: item.index):
            run_node = run_nodes.get(node.id)
            if run_node is None:
                parts.extend([f"## {node.title}", "未执行", ""])
                continue
            parts.extend(
                [
                    f"## {node.title}",
                    run_node.output_text or run_node.error or run_node.status,
                    "",
                ]
            )
        return "\n".join(parts).strip()

    async def _deliver(
        self,
        *,
        pipeline: Pipeline,
        request: PipelineRunRequest,
        text: str,
        run_id: str | None = None,
    ) -> list[Artifact]:
        targets = (
            request.delivery_targets
            if request.delivery_targets is not None
            else pipeline.delivery_defaults
        )
        artifacts: list[Artifact] = []
        for target in targets:
            if target.type == "chat":
                await self._deliver_chat(request, text)
            elif target.type == "workspace_file":
                artifacts.append(
                    await self._deliver_file(
                        pipeline,
                        target,
                        text,
                        run_id=run_id,
                    )
                )
            elif target.type == "wiki_draft":
                artifacts.append(
                    await self._deliver_wiki_draft(
                        pipeline,
                        target,
                        text,
                        run_id=run_id,
                    )
                )
        return artifacts

    async def _deliver_chat(self, request: PipelineRunRequest, text: str) -> None:
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
            meta={"source": "pipeline"},
        )

    async def _deliver_file(
        self,
        pipeline: Pipeline,
        target: DeliveryTarget,
        text: str,
        *,
        run_id: str | None = None,
    ) -> Artifact:
        relative = target.path or f"capabilities/outputs/{_safe_filename(pipeline.name)}.md"
        path = (self._workspace_dir / relative).resolve()
        workspace_root = self._workspace_dir.resolve()
        if workspace_root not in path.parents and path != workspace_root:
            raise ValueError("交付路径必须位于当前工作区内")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        artifact = artifact_from_path(
            path,
            title=target.title or pipeline.name,
            producer="pipeline",
            run_id=run_id,
            classification="user_content",
        )
        register_artifact(self._workspace_dir, artifact)
        return artifact

    async def _deliver_wiki_draft(
        self,
        pipeline: Pipeline,
        target: DeliveryTarget,
        text: str,
        *,
        run_id: str | None = None,
    ) -> Artifact:
        filename = f"{_safe_filename(target.title or pipeline.name)}.md"
        path = self._workspace_dir / ".wiki" / "drafts" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        artifact = artifact_from_path(
            path,
            title=target.title or pipeline.name,
            producer="pipeline",
            run_id=run_id,
            classification="user_content",
        )
        register_artifact(self._workspace_dir, artifact)
        return artifact
