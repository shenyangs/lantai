# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from .draft_builder import build_capability_draft
from .evolution import build_evolution_proposals
from .executor import CapabilityExecutor
from .models import (
    Capability,
    CapabilityDraft,
    CapabilityRun,
    CapabilityRunRequest,
    EvaluationReport,
    EvolutionProposal,
    EvolutionProposalStatus,
    Pipeline,
    PipelineRun,
    PipelineRunRequest,
    TaskRun,
    utc_now,
)
from .pipeline_executor import PipelineExecutor
from .policy import ensure_capability_allowed, is_app_store_distribution
from .repo import CapabilityRepository


class CapabilityService:
    def __init__(self, repo: CapabilityRepository):
        self._repo = repo

    async def list_capabilities(self) -> list[Capability]:
        return await self._repo.list_capabilities()

    async def get_capability(self, capability_id: str) -> Capability | None:
        return await self._repo.get_capability(capability_id)

    async def create_capability(self, capability: Capability) -> Capability:
        ensure_capability_allowed(capability)
        now = utc_now()
        capability.created_at = capability.created_at or now
        capability.updated_at = now
        return await self._repo.upsert_capability(capability)

    async def update_capability(self, capability_id: str, capability: Capability) -> Capability:
        ensure_capability_allowed(capability)
        existing = await self._repo.get_capability(capability_id)
        if existing is not None:
            capability.created_at = existing.created_at
            capability.version = max(existing.version + 1, capability.version)
        capability.id = capability_id
        capability.updated_at = utc_now()
        return await self._repo.upsert_capability(capability)

    async def delete_capability(self, capability_id: str) -> bool:
        return await self._repo.delete_capability(capability_id)

    async def run_capability(
        self,
        capability_id: str,
        request: CapabilityRunRequest,
        *,
        runner: Any,
        workspace_dir: str,
        agent_id: str,
        channel_manager: Any | None = None,
        source: str = "capability",
    ) -> CapabilityRun:
        capability = await self._repo.get_capability(capability_id)
        if capability is None:
            raise KeyError("capability not found")
        executor = CapabilityExecutor(
            repo=self._repo,
            runner=runner,
            workspace_dir=workspace_dir,
            agent_id=agent_id,
            channel_manager=channel_manager,
        )
        return await executor.run(capability, request, source=source)

    async def list_task_runs(self) -> list[TaskRun]:
        return await self._repo.list_task_runs()

    async def get_task_run(self, run_id: str) -> TaskRun | None:
        return await self._repo.get_task_run(run_id)

    async def list_evaluation_reports(self, run_id: str) -> list[EvaluationReport]:
        return await self._repo.list_evaluation_reports(run_id)

    async def save_task_run(self, run: TaskRun) -> TaskRun:
        return await self._repo.upsert_task_run(run)

    async def build_draft(self, task_run: TaskRun) -> CapabilityDraft:
        return build_capability_draft(task_run)

    async def list_evolution_proposals(
        self,
        task_runs: list[TaskRun],
    ) -> list[EvolutionProposal]:
        fresh = build_evolution_proposals(
            task_runs,
            await self.list_capabilities(),
        )
        persisted = {item.id: item for item in await self._repo.list_evolution_proposals()}
        merged: list[EvolutionProposal] = []
        for proposal in fresh:
            saved = persisted.pop(proposal.id, None)
            if saved is not None:
                proposal = proposal.model_copy(
                    update={
                        "status": saved.status,
                        "capability_id": saved.capability_id,
                        "created_at": saved.created_at,
                        "reviewed_at": saved.reviewed_at,
                    },
                )
            merged.append(proposal)
        merged.extend(persisted.values())
        return sorted(merged, key=lambda item: item.created_at, reverse=True)

    async def build_evolution_draft(
        self,
        proposal: EvolutionProposal,
        task_runs: list[TaskRun],
    ) -> CapabilityDraft:
        runs_by_id = {run.id: run for run in task_runs}
        source_run = next(
            (runs_by_id[item.run_id] for item in proposal.evidence if item.run_id in runs_by_id),
            None,
        )
        if source_run is None:
            raise KeyError("evolution proposal source run not found")
        draft = build_capability_draft(source_run)
        if proposal.matched_capability_id:
            existing = await self.get_capability(proposal.matched_capability_id)
            if existing is not None:
                draft.capability = existing
                draft.warnings.append("该建议将扩展现有能力；保存后会产生新版本。")
        draft.evolution_proposal_id = proposal.id
        draft.matched_capability_id = proposal.matched_capability_id
        draft.evidence.extend(
            [f"重复运行证据：{item.title}（{item.run_id}）" for item in proposal.evidence]
        )
        draft.warnings.append("建议仅生成草稿，必须人工复核后保存，且不会自动启用新能力。")
        return draft

    async def review_evolution_proposal(
        self,
        proposal: EvolutionProposal,
        *,
        status: EvolutionProposalStatus,
        capability_id: str | None = None,
    ) -> EvolutionProposal:
        linked_capability_id = capability_id or proposal.capability_id
        if status == "rolled_back" and linked_capability_id:
            capability = await self.get_capability(linked_capability_id)
            if capability is not None and capability.enabled:
                await self.update_capability(
                    capability.id,
                    capability.model_copy(update={"enabled": False}),
                )
        reviewed = proposal.model_copy(
            update={
                "status": status,
                "capability_id": linked_capability_id,
                "reviewed_at": utc_now(),
            },
        )
        return await self._repo.upsert_evolution_proposal(reviewed)

    async def get_capability_run(self, run_id: str) -> CapabilityRun | None:
        return await self._repo.get_capability_run(run_id)

    async def list_pipelines(self) -> list[Pipeline]:
        return await self._repo.list_pipelines()

    async def get_pipeline(self, pipeline_id: str) -> Pipeline | None:
        return await self._repo.get_pipeline(pipeline_id)

    async def create_pipeline(self, pipeline: Pipeline) -> Pipeline:
        await self._validate_pipeline_allowed(pipeline)
        now = utc_now()
        pipeline.created_at = pipeline.created_at or now
        pipeline.updated_at = now
        return await self._repo.upsert_pipeline(pipeline)

    async def update_pipeline(self, pipeline_id: str, pipeline: Pipeline) -> Pipeline:
        await self._validate_pipeline_allowed(pipeline)
        existing = await self._repo.get_pipeline(pipeline_id)
        if existing is not None:
            pipeline.created_at = existing.created_at
            pipeline.version = max(existing.version + 1, pipeline.version)
        pipeline.id = pipeline_id
        pipeline.updated_at = utc_now()
        return await self._repo.upsert_pipeline(pipeline)

    async def delete_pipeline(self, pipeline_id: str) -> bool:
        return await self._repo.delete_pipeline(pipeline_id)

    async def run_pipeline(
        self,
        pipeline_id: str,
        request: PipelineRunRequest,
        *,
        runner: Any,
        workspace_dir: str,
        agent_id: str,
        channel_manager: Any | None = None,
        source: str = "pipeline",
    ) -> PipelineRun:
        pipeline = await self._repo.get_pipeline(pipeline_id)
        if pipeline is None:
            raise KeyError("pipeline not found")
        executor = PipelineExecutor(
            repo=self._repo,
            runner=runner,
            workspace_dir=workspace_dir,
            agent_id=agent_id,
            channel_manager=channel_manager,
        )
        return await executor.run(pipeline, request, source=source)

    async def get_pipeline_run(self, run_id: str) -> PipelineRun | None:
        return await self._repo.get_pipeline_run(run_id)

    async def _validate_pipeline_allowed(self, pipeline: Pipeline) -> None:
        capability_ids = {node.capability_id for node in pipeline.nodes}
        capabilities = [
            capability
            for capability_id in capability_ids
            if (capability := await self._repo.get_capability(capability_id)) is not None
        ]
        if is_app_store_distribution():
            for capability in capabilities:
                ensure_capability_allowed(capability)
