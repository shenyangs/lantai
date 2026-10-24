# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from lantai.app.runtime_legacy_writers import assert_legacy_runtime_writer_allowed
from lantai.runtime.sanitizer import sanitize_runtime_payload

from .models import (
    Artifact,
    CapabilitiesFile,
    Capability,
    CapabilityRun,
    EvaluationFinding,
    EvaluationReport,
    EvolutionProposal,
    EvolutionProposalsFile,
    Pipeline,
    PipelineRun,
    PipelinesFile,
    TaskRun,
    TaskRunsFile,
)
from .runtime_runs import record_task_run_runtime_contract


class CapabilityRepository:
    """File-backed repository scoped to one agent workspace."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = self.workspace_dir / "capabilities"
        self.capabilities_path = self.root / "capabilities.json"
        self.pipelines_path = self.root / "pipelines.json"
        self.task_runs_path = self.root / "task-runs.json"
        self.evolution_proposals_path = self.root / "evolution-proposals.json"
        self.runs_dir = self.root / "runs"
        self.pipeline_runs_dir = self.root / "pipeline-runs"
        self.replays_dir = self.root / "replays"
        self.evaluations_dir = self.root / "evaluations"
        self._lock = asyncio.Lock()

    async def _ensure_dirs(self) -> None:
        await asyncio.to_thread(self.runs_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            self.pipeline_runs_dir.mkdir,
            parents=True,
            exist_ok=True,
        )
        await asyncio.to_thread(self.replays_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            self.evaluations_dir.mkdir,
            parents=True,
            exist_ok=True,
        )

    @staticmethod
    def _read_model(path: Path, model_cls: type[Any], default: Any) -> Any:
        if not path.exists():
            return default
        payload = json.loads(path.read_text(encoding="utf-8"))
        return model_cls.model_validate(payload)

    @staticmethod
    def _write_model(path: Path, model: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                model.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _safe_file_stem(value: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip())
        text = text.strip(".-")
        return text or "unknown"

    def _evaluation_history_path(self, task_run_id: str, report_id: str) -> Path:
        return (
            self.evaluations_dir
            / self._safe_file_stem(task_run_id)
            / f"{self._safe_file_stem(report_id)}.json"
        )

    async def load_capabilities(self) -> CapabilitiesFile:
        await self._ensure_dirs()
        return await asyncio.to_thread(
            self._read_model,
            self.capabilities_path,
            CapabilitiesFile,
            CapabilitiesFile(),
        )

    async def save_capabilities(self, data: CapabilitiesFile) -> None:
        await self._ensure_dirs()
        await asyncio.to_thread(self._write_model, self.capabilities_path, data)

    async def list_capabilities(self) -> list[Capability]:
        data = await self.load_capabilities()
        return sorted(data.capabilities, key=lambda item: item.updated_at, reverse=True)

    async def get_capability(self, capability_id: str) -> Capability | None:
        data = await self.load_capabilities()
        return next((item for item in data.capabilities if item.id == capability_id), None)

    async def upsert_capability(self, capability: Capability) -> Capability:
        async with self._lock:
            data = await self.load_capabilities()
            replaced = False
            capabilities: list[Capability] = []
            for item in data.capabilities:
                if item.id == capability.id:
                    capabilities.append(capability)
                    replaced = True
                else:
                    capabilities.append(item)
            if not replaced:
                capabilities.append(capability)
            await self.save_capabilities(
                data.model_copy(update={"capabilities": capabilities})
            )
            return capability

    async def delete_capability(self, capability_id: str) -> bool:
        async with self._lock:
            data = await self.load_capabilities()
            kept = [item for item in data.capabilities if item.id != capability_id]
            if len(kept) == len(data.capabilities):
                return False
            await self.save_capabilities(data.model_copy(update={"capabilities": kept}))
            return True

    async def load_pipelines(self) -> PipelinesFile:
        await self._ensure_dirs()
        return await asyncio.to_thread(
            self._read_model,
            self.pipelines_path,
            PipelinesFile,
            PipelinesFile(),
        )

    async def save_pipelines(self, data: PipelinesFile) -> None:
        await self._ensure_dirs()
        await asyncio.to_thread(self._write_model, self.pipelines_path, data)

    async def list_pipelines(self) -> list[Pipeline]:
        data = await self.load_pipelines()
        return sorted(data.pipelines, key=lambda item: item.updated_at, reverse=True)

    async def get_pipeline(self, pipeline_id: str) -> Pipeline | None:
        data = await self.load_pipelines()
        return next((item for item in data.pipelines if item.id == pipeline_id), None)

    async def upsert_pipeline(self, pipeline: Pipeline) -> Pipeline:
        async with self._lock:
            data = await self.load_pipelines()
            replaced = False
            pipelines: list[Pipeline] = []
            for item in data.pipelines:
                if item.id == pipeline.id:
                    pipelines.append(pipeline)
                    replaced = True
                else:
                    pipelines.append(item)
            if not replaced:
                pipelines.append(pipeline)
            await self.save_pipelines(data.model_copy(update={"pipelines": pipelines}))
            return pipeline

    async def delete_pipeline(self, pipeline_id: str) -> bool:
        async with self._lock:
            data = await self.load_pipelines()
            kept = [item for item in data.pipelines if item.id != pipeline_id]
            if len(kept) == len(data.pipelines):
                return False
            await self.save_pipelines(data.model_copy(update={"pipelines": kept}))
            return True

    async def load_task_runs(self) -> TaskRunsFile:
        await self._ensure_dirs()
        return await asyncio.to_thread(
            self._read_model,
            self.task_runs_path,
            TaskRunsFile,
            TaskRunsFile(),
        )

    async def save_task_runs(self, data: TaskRunsFile) -> None:
        assert_legacy_runtime_writer_allowed(
            "legacy_task_run_snapshot_writer",
            path=self.task_runs_path,
        )
        await self._ensure_dirs()
        sanitized = data.model_copy(
            update={
                "runs": [
                    self._sanitize_task_run_for_storage(run)
                    for run in data.runs
                ],
            }
        )
        await asyncio.to_thread(self._write_model, self.task_runs_path, sanitized)

    async def list_task_runs(self) -> list[TaskRun]:
        data = await self.load_task_runs()
        return sorted(data.runs, key=lambda item: item.started_at, reverse=True)

    async def load_evolution_proposals(self) -> EvolutionProposalsFile:
        await self._ensure_dirs()
        return await asyncio.to_thread(
            self._read_model,
            self.evolution_proposals_path,
            EvolutionProposalsFile,
            EvolutionProposalsFile(),
        )

    async def list_evolution_proposals(self) -> list[EvolutionProposal]:
        data = await self.load_evolution_proposals()
        return sorted(data.proposals, key=lambda item: item.created_at, reverse=True)

    async def upsert_evolution_proposal(
        self,
        proposal: EvolutionProposal,
    ) -> EvolutionProposal:
        async with self._lock:
            data = await self.load_evolution_proposals()
            proposals = [item for item in data.proposals if item.id != proposal.id]
            proposals.append(proposal)
            await asyncio.to_thread(
                self._write_model,
                self.evolution_proposals_path,
                data.model_copy(update={"proposals": proposals}),
            )
        return proposal

    async def get_task_run(self, run_id: str) -> TaskRun | None:
        data = await self.load_task_runs()
        return next((item for item in data.runs if item.id == run_id), None)

    async def upsert_task_run(self, run: TaskRun, *, limit: int = 500) -> TaskRun:
        async with self._lock:
            assert_legacy_runtime_writer_allowed(
                "legacy_task_run_snapshot_writer",
                path=self.task_runs_path,
            )
            data = await self.load_task_runs()
            runs = [item for item in data.runs if item.id != run.id]
            runs.append(run)
            runs = sorted(runs, key=lambda item: item.started_at, reverse=True)[:limit]
            await self.save_task_runs(data.model_copy(update={"runs": runs}))
            await record_task_run_runtime_contract(self.workspace_dir, run)
            return run

    async def save_capability_run(self, run: CapabilityRun) -> CapabilityRun:
        await self._ensure_dirs()
        path = self.runs_dir / f"{run.id}.json"
        await asyncio.to_thread(
            self._write_model,
            path,
            self._sanitize_capability_run_for_storage(run),
        )
        return run

    async def get_capability_run(self, run_id: str) -> CapabilityRun | None:
        await self._ensure_dirs()
        path = self.runs_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return await asyncio.to_thread(
            self._read_model,
            path,
            CapabilityRun,
            None,
        )

    async def save_pipeline_run(self, run: PipelineRun) -> PipelineRun:
        await self._ensure_dirs()
        path = self.pipeline_runs_dir / f"{run.id}.json"
        await asyncio.to_thread(
            self._write_model,
            path,
            self._sanitize_pipeline_run_for_storage(run),
        )
        return run

    async def get_pipeline_run(self, run_id: str) -> PipelineRun | None:
        await self._ensure_dirs()
        path = self.pipeline_runs_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return await asyncio.to_thread(
            self._read_model,
            path,
            PipelineRun,
            None,
        )

    async def save_evaluation_report(
        self,
        report: EvaluationReport,
    ) -> EvaluationReport:
        path = self._evaluation_history_path(report.task_run_id, report.id)
        assert_legacy_runtime_writer_allowed(
            "legacy_evaluation_report_writer",
            path=path,
        )
        await self._ensure_dirs()
        await asyncio.to_thread(
            self._write_model,
            path,
            self._sanitize_evaluation_report_for_storage(report),
        )
        return report

    async def list_evaluation_reports(self, task_run_id: str) -> list[EvaluationReport]:
        await self._ensure_dirs()
        reports: dict[str, EvaluationReport] = {}
        history_dir = self.evaluations_dir / self._safe_file_stem(task_run_id)
        if history_dir.exists() and history_dir.is_dir():
            for path in sorted(history_dir.glob("*.json")):
                report = await asyncio.to_thread(
                    self._read_model,
                    path,
                    EvaluationReport,
                    None,
                )
                if report is not None and report.task_run_id == task_run_id:
                    expected_path = self._evaluation_history_path(
                        task_run_id,
                        report.id,
                    )
                    if path.resolve(strict=False) != expected_path.resolve(strict=False):
                        continue
                    reports[report.id] = report

        legacy_path = self.evaluations_dir / f"{task_run_id}.json"
        if legacy_path.exists():
            legacy_report = await asyncio.to_thread(
                self._read_model,
                legacy_path,
                EvaluationReport,
                None,
            )
            if legacy_report is not None:
                reports.setdefault(legacy_report.id, legacy_report)

        return sorted(
            reports.values(),
            key=lambda report: (report.created_at, report.id),
            reverse=True,
        )

    def replay_path(self, run_id: str, step_id: str) -> Path:
        safe_run = "".join(c if c.isalnum() or c in "-_" else "-" for c in run_id)
        safe_step = "".join(c if c.isalnum() or c in "-_" else "-" for c in step_id)
        return self.replays_dir / safe_run / f"{safe_step}.jsonl"

    async def append_replay_event(
        self,
        *,
        run_id: str,
        step_id: str,
        event: dict[str, Any],
    ) -> Path:
        await self._ensure_dirs()
        path = self.replay_path(run_id, step_id)

        def write_line() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            sanitized_event = sanitize_runtime_payload(dict(event))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sanitized_event, ensure_ascii=False) + "\n")

        await asyncio.to_thread(write_line)
        return path

    @staticmethod
    def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        return sanitize_runtime_payload(dict(metadata or {})) if metadata else {}

    @classmethod
    def _sanitize_artifact_for_storage(cls, artifact: Artifact) -> Artifact:
        return artifact.model_copy(
            update={"metadata": cls._sanitize_metadata(artifact.metadata)},
        )

    @classmethod
    def _sanitize_task_run_for_storage(cls, run: TaskRun) -> TaskRun:
        return run.model_copy(
            update={
                "metadata": cls._sanitize_metadata(run.metadata),
                "steps": [
                    step.model_copy(
                        update={"metadata": cls._sanitize_metadata(step.metadata)},
                    )
                    for step in run.steps
                ],
                "artifacts": [
                    cls._sanitize_artifact_for_storage(artifact)
                    for artifact in run.artifacts
                ],
            },
        )

    @classmethod
    def _sanitize_evaluation_report_for_storage(
        cls,
        report: EvaluationReport,
    ) -> EvaluationReport:
        findings: list[EvaluationFinding] = [
            finding.model_copy(
                update={"metadata": cls._sanitize_metadata(finding.metadata)},
            )
            for finding in report.findings
        ]
        return report.model_copy(update={"findings": findings})

    @classmethod
    def _sanitize_capability_run_for_storage(
        cls,
        run: CapabilityRun,
    ) -> CapabilityRun:
        return run.model_copy(
            update={
                "parameters": sanitize_runtime_payload(dict(run.parameters)),
                "artifacts": [
                    cls._sanitize_artifact_for_storage(artifact)
                    for artifact in run.artifacts
                ],
            },
        )

    @classmethod
    def _sanitize_pipeline_run_for_storage(cls, run: PipelineRun) -> PipelineRun:
        return run.model_copy(
            update={
                "parameters": sanitize_runtime_payload(dict(run.parameters)),
                "nodes": [
                    node.model_copy(
                        update={
                            "artifacts": [
                                cls._sanitize_artifact_for_storage(artifact)
                                for artifact in node.artifacts
                            ],
                        },
                    )
                    for node in run.nodes
                ],
                "artifacts": [
                    cls._sanitize_artifact_for_storage(artifact)
                    for artifact in run.artifacts
                ],
            },
        )
