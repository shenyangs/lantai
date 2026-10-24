# -*- coding: utf-8 -*-
"""Process worker for the RUN-05 lost-final-response recovery boundary."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from lantai.agents.tools import presentation as presentation_tool
from lantai.config.context import WorkspaceContext, set_current_workspace_context
from lantai.presentation import service as presentation_service
from lantai.presentation.models import VisualCriticResult
from lantai.presentation.render import RenderResult


TITLE = "RUN-05 已交付但最后响应丢失"
OUTPUT_FILENAME = "run05-receipt-retry.pptx"
SLIDES = [
    {
        "layout_id": "cover",
        "objects": [
            {
                "kind": "text",
                "role": "title",
                "text": "一次发布，可恢复回执",
                "slot": "title",
            },
            {
                "kind": "text",
                "role": "body",
                "text": "响应丢失后复用同一份已验证 PPTX",
                "slot": "subtitle",
            },
        ],
    }
]


class _PassingVisualCritic:
    def review(self, _slides) -> VisualCriticResult:
        return VisualCriticResult(critic_id="run05-critic", issues=[])


class _NoRepairExpected:
    def repair(self, *_args, **_kwargs) -> None:
        raise AssertionError("RUN-05 fixture unexpectedly required repair")


def _render_one(_source, output_dir, *, timeout=120) -> RenderResult:
    _ = timeout
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    pdf_path = destination / "presentation.pdf"
    pdf_path.write_bytes(b"RUN-05 render evidence\n")
    image_path = destination / "slide-1.png"
    image = Image.new("RGB", (640, 360), "white")
    image.paste("black", (0, 0, 320, 360))
    image.save(image_path)
    return RenderResult(pdf_path=pdf_path, slide_images=(image_path,))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _forbidden_sync(
    counters: Counter[str],
    name: str,
) -> Callable[..., Any]:
    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        counters[name] += 1
        raise AssertionError(f"receipt reuse invoked forbidden side effect: {name}")

    return blocked


async def _create() -> Any:
    return await presentation_tool.create_presentation(
        title=TITLE,
        slides=SLIDES,
        output_filename=OUTPUT_FILENAME,
    )


async def _prepare_then_wait(marker: Path) -> None:
    actual_delivery_response = presentation_tool._delivery_response  # pylint: disable=protected-access

    async def lose_final_response(*args: Any, **kwargs: Any) -> Any:
        response = await actual_delivery_response(*args, **kwargs)
        destination = Path(args[0])
        receipt_path = Path(kwargs["receipt_path"])
        marker_payload = {
            "pid": os.getpid(),
            "job_id": kwargs["job_id"],
            "destination": str(destination),
            "destination_sha256": _sha256(destination),
            "receipt_path": str(receipt_path),
            "receipt_sha256": _sha256(receipt_path),
            "response_prepared": response.is_error is False,
            "file_block_count": sum(
                block.get("type") == "file" for block in response.content
            ),
        }
        marker.write_text(
            json.dumps(marker_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        await asyncio.Event().wait()
        raise AssertionError("unreachable after lost final response")

    presentation_service.render_pptx = _render_one
    presentation_tool.UtilityPresentationVisualCritic = (
        lambda _agent_id: _PassingVisualCritic()
    )
    presentation_tool.UtilityPresentationRepairDesigner = (
        lambda _agent_id: _NoRepairExpected()
    )
    presentation_tool._delivery_response = lose_final_response  # type: ignore[assignment]  # pylint: disable=protected-access
    await _create()


async def _resume() -> dict[str, object]:
    forbidden_calls: Counter[str] = Counter()
    presentation_tool.PresentationService = _forbidden_sync(
        forbidden_calls,
        "generation_service",
    )
    presentation_tool.UtilityPresentationVisualCritic = _forbidden_sync(
        forbidden_calls,
        "visual_critic_model",
    )
    presentation_tool.UtilityPresentationRepairDesigner = _forbidden_sync(
        forbidden_calls,
        "repair_model",
    )
    presentation_tool.shutil.copy2 = _forbidden_sync(
        forbidden_calls,
        "copy2",
    )

    response = await _create()
    return {
        "pid": os.getpid(),
        "is_error": response.is_error,
        "job_id": response.metadata.get("job_id"),
        "reused": response.metadata.get("reused"),
        "verification_receipt": response.metadata.get("verification_receipt"),
        "output_path": response.metadata.get("output_path"),
        "file_block_count": sum(
            block.get("type") == "file" for block in response.content
        ),
        "forbidden_calls": dict(forbidden_calls),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("prepare", "resume"), required=True)
    parser.add_argument("--agent-home", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--marker", type=Path)
    args = parser.parse_args()

    context = WorkspaceContext(
        agent_home=args.agent_home,
        state_root=args.state_root,
        project_root=args.project_root,
        workspace_grant_id=args.conversation_id,
        trust_state="trusted",
    )
    set_current_workspace_context(context)
    try:
        if args.mode == "prepare":
            if args.marker is None:
                parser.error("--marker is required in prepare mode")
            asyncio.run(_prepare_then_wait(args.marker))
        else:
            result = asyncio.run(_resume())
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        set_current_workspace_context(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
