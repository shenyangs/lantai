# -*- coding: utf-8 -*-
"""Persistent rollout registry for presentation pipeline capabilities."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from pydantic import Field

from ..utils.file_lock import locked_file_descriptor
from .models import PresentationJobRequest, StrictModel


class PipelineCapability(StrictModel):
    version: str
    status: Literal["available", "disabled"] = "available"
    composition_engine: str
    deck_ir_versions: list[str] = Field(default_factory=lambda: ["2.0"])
    supports_visual_qa: bool = True
    supports_object_edit: bool = True
    supports_version_rollback: bool = True


class PipelineTransition(StrictModel):
    previous_default: str
    next_default: str
    action: Literal["activate", "rollback"]
    reason: str
    metrics: dict[str, float | int | str]
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class PipelineControlState(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    default_version: str = "lantai-presentation-v3"
    capabilities: dict[str, PipelineCapability] = Field(default_factory=dict)
    transitions: list[PipelineTransition] = Field(default_factory=list)


def _initial_state() -> PipelineControlState:
    version = "lantai-presentation-v3"
    return PipelineControlState(
        default_version=version,
        capabilities={
            version: PipelineCapability(
                version=version,
                composition_engine="native_pptx",
            ),
        },
    )


class PresentationPipelineRegistry:
    """Pin each job while allowing controlled defaults for new jobs only."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "_pipeline-control.json"
        self.lock_path = self.root / "_pipeline-control.lock"
        if not self.path.is_file():
            with self._lock():
                if not self.path.is_file():
                    self._write(_initial_state())

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as handle:
            with locked_file_descriptor(handle.fileno()):
                yield

    def state(self) -> PipelineControlState:
        return PipelineControlState.model_validate_json(
            self.path.read_text(encoding="utf-8"),
        )

    def bind_new_request(
        self,
        request: PresentationJobRequest,
    ) -> PresentationJobRequest:
        state = self.state()
        requested = (
            request.pipeline_version
            if "pipeline_version" in request.model_fields_set
            else state.default_version
        )
        self._assert_available(state, requested)
        return request.model_copy(update={"pipeline_version": requested})

    def assert_pinned_available(self, pipeline_version: str) -> None:
        self._assert_available(self.state(), pipeline_version)

    def register(self, capability: PipelineCapability) -> PipelineControlState:
        with self._lock():
            state = self.state()
            capabilities = dict(state.capabilities)
            capabilities[capability.version] = capability
            state = state.model_copy(update={"capabilities": capabilities})
            self._write(state)
            return state

    def activate_for_new_jobs(
        self,
        pipeline_version: str,
        *,
        reason: str,
        metrics: dict[str, float | int | str],
    ) -> PipelineControlState:
        return self._change_default(
            pipeline_version,
            action="activate",
            reason=reason,
            metrics=metrics,
        )

    def rollback_default(
        self,
        pipeline_version: str,
        *,
        reason: str,
        metrics: dict[str, float | int | str],
    ) -> PipelineControlState:
        return self._change_default(
            pipeline_version,
            action="rollback",
            reason=reason,
            metrics=metrics,
        )

    def _change_default(
        self,
        pipeline_version: str,
        *,
        action: Literal["activate", "rollback"],
        reason: str,
        metrics: dict[str, float | int | str],
    ) -> PipelineControlState:
        if not reason.strip() or not metrics:
            raise ValueError("pipeline transition requires reason and metrics")
        with self._lock():
            state = self.state()
            self._assert_available(state, pipeline_version)
            transition = PipelineTransition(
                previous_default=state.default_version,
                next_default=pipeline_version,
                action=action,
                reason=reason.strip(),
                metrics=metrics,
            )
            state = state.model_copy(
                update={
                    "default_version": pipeline_version,
                    "transitions": [*state.transitions, transition],
                },
            )
            self._write(state)
            return state

    @staticmethod
    def _assert_available(
        state: PipelineControlState,
        pipeline_version: str,
    ) -> None:
        capability = state.capabilities.get(pipeline_version)
        if capability is None:
            raise ValueError(
                f"unregistered presentation pipeline: {pipeline_version}",
            )
        if capability.status != "available":
            raise RuntimeError(
                f"presentation pipeline is disabled: {pipeline_version}",
            )

    def _write(self, state: PipelineControlState) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
