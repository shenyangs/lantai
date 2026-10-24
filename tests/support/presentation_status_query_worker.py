# -*- coding: utf-8 -*-
"""Independent read-only status probe used by the RUN-04 boundary test."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from lantai.agents.tools import presentation as presentation_tool
from lantai.config.context import WorkspaceContext, set_current_workspace_context
from lantai.presentation.jobs import PresentationJobStore


def _forbidden_sync(
    counters: Counter[str],
    name: str,
) -> Callable[..., Any]:
    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        counters[name] += 1
        raise AssertionError(f"read-only status query invoked forbidden side effect: {name}")

    return blocked


def _forbidden_async(
    counters: Counter[str],
    name: str,
) -> Callable[..., Any]:
    async def blocked(*_args: Any, **_kwargs: Any) -> Any:
        counters[name] += 1
        raise AssertionError(f"read-only status query invoked forbidden side effect: {name}")

    return blocked


async def _query_repeatedly(
    *,
    job_id: str,
    repeats: int,
) -> dict[str, object]:
    response_digests: list[str] = []
    job_statuses: list[str] = []
    verification_statuses: list[str] = []
    publish_ready: list[bool] = []
    side_effect_outcomes: list[str] = []
    file_block_count = 0

    for _ in range(repeats):
        response = await presentation_tool.get_presentation_generation_status(
            job_id,
        )
        payload = {
            "content": response.content,
            "metadata": response.metadata,
            "is_error": response.is_error,
        }
        response_digests.append(
            hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            ).hexdigest(),
        )
        metadata = response.metadata or {}
        job_statuses.append(str(metadata.get("job_status")))
        verification_statuses.append(str(metadata.get("verification_status")))
        publish_ready.append(metadata.get("publish_ready") is True)
        side_effect_outcomes.append(str(metadata.get("side_effect_outcome")))
        file_block_count += sum(
            block.get("type") == "file" for block in response.content
        )

    return {
        "queries": repeats,
        "response_digests": response_digests,
        "job_statuses": job_statuses,
        "verification_statuses": verification_statuses,
        "publish_ready": publish_ready,
        "side_effect_outcomes": side_effect_outcomes,
        "file_block_count": file_block_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-home", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--repeats", type=int, default=4)
    args = parser.parse_args()

    side_effect_calls: Counter[str] = Counter()
    store_modes: list[bool] = []
    original_store_init = PresentationJobStore.__init__

    def tracked_store_init(
        self: PresentationJobStore,
        *store_args: Any,
        **store_kwargs: Any,
    ) -> None:
        store_modes.append(store_kwargs.get("read_only") is True)
        original_store_init(self, *store_args, **store_kwargs)

    PresentationJobStore.__init__ = tracked_store_init  # type: ignore[method-assign]
    presentation_tool.PresentationService = _forbidden_sync(
        side_effect_calls,
        "generation_service",
    )
    presentation_tool.UtilityPresentationVisualCritic = _forbidden_sync(
        side_effect_calls,
        "visual_critic_model",
    )
    presentation_tool.UtilityPresentationRepairDesigner = _forbidden_sync(
        side_effect_calls,
        "repair_model",
    )
    presentation_tool._finalize_presentation_job_delivery = _forbidden_async(  # pylint: disable=protected-access
        side_effect_calls,
        "finalize",
    )
    presentation_tool._delivery_response = _forbidden_async(  # pylint: disable=protected-access
        side_effect_calls,
        "delivery_response",
    )
    presentation_tool.send_file_to_user = _forbidden_async(
        side_effect_calls,
        "send_file",
    )
    presentation_tool.shutil.copy = _forbidden_sync(side_effect_calls, "copy")
    presentation_tool.shutil.copy2 = _forbidden_sync(side_effect_calls, "copy2")
    presentation_tool.shutil.copyfile = _forbidden_sync(
        side_effect_calls,
        "copyfile",
    )

    context = WorkspaceContext(
        agent_home=args.agent_home,
        state_root=args.state_root,
        project_root=args.project_root,
        workspace_grant_id=args.conversation_id,
        trust_state="trusted",
    )
    set_current_workspace_context(context)
    try:
        result = asyncio.run(
            _query_repeatedly(job_id=args.job_id, repeats=args.repeats),
        )
    finally:
        set_current_workspace_context(None)

    result.update(
        {
            "pid": os.getpid(),
            "read_only_store_inits": sum(store_modes),
            "mutable_store_inits": len(store_modes) - sum(store_modes),
            "side_effect_calls": dict(side_effect_calls),
        }
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
