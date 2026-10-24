# -*- coding: utf-8 -*-
"""One ordered context assembly path backed by ``ContextBudgetPlanner``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .context_budget import ContextBudgetPlanner, ContextCandidate, ContextManifest
from .model_context_profile import ModelContextProfile


@dataclass(frozen=True)
class ContextAssemblyInput:
    system_rules: str
    tool_schemas: str
    current_message: str
    stable_memory: str = ""
    recent_history: str = ""
    run_state: str = ""
    dynamic_memory: Iterable[ContextCandidate] = ()
    wiki_evidence: Iterable[ContextCandidate] = ()
    tool_results: Iterable[ContextCandidate] = ()


class ContextAssembler:
    """Preserve mandatory ordering while planner chooses optional evidence."""

    def __init__(self, planner: ContextBudgetPlanner) -> None:
        self.planner = planner

    def assemble(
        self,
        *,
        profile: ModelContextProfile,
        requested_output_tokens: int,
        input: ContextAssemblyInput,
    ) -> tuple[list[ContextCandidate], ContextManifest]:
        required = [
            ContextCandidate("system", input.system_rules, required=True, identifier="system"),
            ContextCandidate("tool_schema", input.tool_schemas, required=True, identifier="tools"),
        ]
        if input.stable_memory:
            required.append(ContextCandidate("stable_memory", input.stable_memory, required=True, identifier="stable-memory"))
        if input.recent_history or input.run_state:
            required.append(ContextCandidate("history", "\n".join(filter(None, [input.recent_history, input.run_state])), required=True, identifier="history"))
        required.append(ContextCandidate("current_message", input.current_message, required=True, identifier="current-message"))
        # Current user request must appear last in the final payload. Plan it as
        # required, then restore the documented ordering after selection.
        selected, manifest = self.planner.plan(
            profile=profile, requested_output_tokens=requested_output_tokens,
            required=required,
            optional=[*input.dynamic_memory, *input.wiki_evidence, *input.tool_results],
        )
        order = {
            "system": 0, "tool_schema": 1, "stable_memory": 2, "history": 3,
            "dynamic_memory": 4, "wiki_evidence": 5, "tool_result": 6,
            "current_message": 7,
        }
        selected.sort(key=lambda block: order[block.category])
        return selected, manifest
