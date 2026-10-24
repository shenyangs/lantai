# -*- coding: utf-8 -*-
"""Worker used to prove visual-review checkpoint recovery after SIGKILL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from lantai.presentation.jobs import PresentationExecutionLimits
from lantai.presentation.models import (
    VisualCriticResult,
    VisualCriticSlideInput,
)
from lantai.presentation.service import PresentationService
from lantai.utils.file_lock import locked_file_descriptor


CRITIC_CACHE_IDENTITY = "rec02-checkpoint-critic/v1"
CRITIC_RESULT_ID = "rec02-checkpoint-critic/model-a"
SHARED_DEPENDENCIES = {
    "renderer": {"id": "rec02-renderer", "version": "1.0"},
    "ruleset": "rec02-process-fault/v1",
}


def build_slides(root: Path) -> list[VisualCriticSlideInput]:
    return [
        VisualCriticSlideInput(
            slide_id=f"rec02-slide-{index:02d}",
            slide_index=index,
            rendered_path=str(root / f"slide-{index:02d}.png"),
            rendered_sha256=hashlib.sha256(
                f"rec02-rendered-slide-{index:02d}".encode("utf-8"),
            ).hexdigest(),
            key_message=f"第 {index} 页必须完成语义视觉审查",
            full_deck_context=["19 页必须全部检查，已完成证据可以复用"],
        )
        for index in range(1, 20)
    ]


class SevenThenBlockCritic:
    """Return seven reviews, then keep the remaining calls in flight."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.counter_path = root / "completed-review-count.txt"
        self.calls_path = root / "first-run-calls.jsonl"
        self.lock_path = root / "critic.lock"

    def cache_identity(self) -> str:
        return CRITIC_CACHE_IDENTITY

    def _append_call(self, slide_id: str) -> None:
        with self.lock_path.open("a+b") as lock:
            with locked_file_descriptor(lock.fileno()):
                with self.calls_path.open("a", encoding="utf-8") as calls:
                    calls.write(json.dumps({"slide_id": slide_id}) + "\n")
                    calls.flush()
                    os.fsync(calls.fileno())

    def _claim_completion(self) -> bool:
        with self.lock_path.open("a+b") as lock:
            with locked_file_descriptor(lock.fileno()):
                try:
                    count = int(self.counter_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    count = 0
                if count >= 7:
                    return False
                self.counter_path.write_text(str(count + 1), encoding="utf-8")
                return True

    def review_slide(
        self,
        slide: VisualCriticSlideInput,
    ) -> VisualCriticResult:
        self._append_call(slide.slide_id)
        if self._claim_completion():
            return VisualCriticResult(critic_id=CRITIC_RESULT_ID, issues=[])
        while True:
            time.sleep(0.05)

    def review_deck(
        self,
        _slides: list[VisualCriticSlideInput],
    ) -> VisualCriticResult:
        raise AssertionError(
            "deck review must not start before the process kill",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    arguments = parser.parse_args()

    service = PresentationService(
        arguments.state_root,
        visual_critic=SevenThenBlockCritic(arguments.state_root),
        execution_limits=PresentationExecutionLimits(critics=3),
    )
    getattr(service, "_review_visuals_with_checkpoints")(
        build_slides(arguments.state_root),
        job_id=arguments.job_id,
        repair_rounds=0,
        shared_dependencies=SHARED_DEPENDENCIES,
    )


if __name__ == "__main__":
    main()
