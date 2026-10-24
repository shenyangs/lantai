"""Auditable boundary for automatic global-memory capture."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def should_capture_global_memory(request_context: Mapping[str, Any]) -> bool:
    """Keep private-editor manuscript state out of global automatic memory.

    Private-editor continuity lives in its project record and transcript. This
    leaves explicit memory tools and ordinary conversation capture unchanged.
    """

    work_method_id = str(request_context.get("work_method_id") or "").strip()
    task_contract = request_context.get("task_contract")
    if not work_method_id and isinstance(task_contract, Mapping):
        work_method_id = str(task_contract.get("work_method_id") or "").strip()
    return work_method_id != "private_editor"


__all__ = ["should_capture_global_memory"]
