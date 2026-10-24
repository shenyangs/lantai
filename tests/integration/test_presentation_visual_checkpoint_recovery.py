# -*- coding: utf-8 -*-
"""REC-02: resume semantic visual review after a real process crash."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lantai.presentation.jobs import PresentationExecutionLimits
from lantai.presentation.models import (
    JobStage,
    JobStatus,
    PresentationJobRequest,
    VisualCriticResult,
    VisualReviewCheckpoint,
)
from lantai.presentation.service import PresentationService
from tests.support.presentation_visual_checkpoint_worker import (
    CRITIC_CACHE_IDENTITY,
    CRITIC_RESULT_ID,
    SHARED_DEPENDENCIES,
    build_slides,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT
    / "tests"
    / "support"
    / "presentation_visual_checkpoint_worker.py"
)


class _ResumeCritic:
    def __init__(self) -> None:
        self.slide_calls: list[str] = []
        self.deck_calls = 0
        self._lock = threading.Lock()

    def cache_identity(self) -> str:
        return CRITIC_CACHE_IDENTITY

    def review_slide(self, slide) -> VisualCriticResult:
        with self._lock:
            self.slide_calls.append(slide.slide_id)
        return VisualCriticResult(critic_id=CRITIC_RESULT_ID, issues=[])

    def review_deck(self, _slides) -> VisualCriticResult:
        with self._lock:
            self.deck_calls += 1
        return VisualCriticResult(critic_id=CRITIC_RESULT_ID, issues=[])


def _wait_for_slide_checkpoints(
    checkpoint_dir: Path,
    worker: subprocess.Popen[str],
    *,
    expected: int,
    timeout: float = 10,
) -> list[Path]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        paths = sorted(checkpoint_dir.glob("slide-*.json"))
        if len(paths) == expected:
            return paths
        if worker.poll() is not None:
            stdout, stderr = worker.communicate(timeout=2)
            raise AssertionError(
                (
                    "critic worker exited before "
                    f"{expected}"
                    " checkpoints; exit="
                    f"{worker.returncode}"
                    "; stdout="
                    f"{stdout!r}"
                    "; stderr="
                    f"{stderr!r}"
                ),
            )
        time.sleep(0.02)
    raise AssertionError(
        f"timed out waiting for {expected} visual checkpoints; "
        f"found={len(list(checkpoint_dir.glob('slide-*.json')))}",
    )


def _read_call_ids(path: Path) -> list[str]:
    return [
        str(json.loads(line)["slide_id"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _wait_for_started_calls(
    path: Path,
    worker: subprocess.Popen[str],
    *,
    expected: int,
    timeout: float = 10,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if len(_read_call_ids(path)) == expected:
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if worker.poll() is not None:
            stdout, stderr = worker.communicate(timeout=2)
            raise AssertionError(
                (
                    "critic worker exited before "
                    f"{expected}"
                    " started calls; exit="
                    f"{worker.returncode}"
                    "; stdout="
                    f"{stdout!r}"
                    "; stderr="
                    f"{stderr!r}"
                ),
            )
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {expected} started calls")


# pylint: disable=R0915
@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_visual_critic_resumes_after_seven_durable_page_checkpoints(
    tmp_path: Path,
) -> None:
    """Kill after seven pages; restart must reuse them and finish the deck."""

    state_root = tmp_path / "presentation-jobs"
    setup = PresentationService(state_root)
    record = setup.create_job(
        PresentationJobRequest(
            conversation_id="rec02-conversation",
            user_request="生成并逐页审查一份 19 页演示文稿",
            slide_count=19,
            cost_budget_units=100,
        ),
    )
    setup.store.append_event(
        record.job_id,
        stage=JobStage.QUALITY,
        status=JobStatus.RUNNING,
        message="开始逐页语义视觉审查",
    )

    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath = [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)]
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
    with subprocess.Popen(
        [
            sys.executable,
            str(WORKER_SCRIPT),
            "--state-root",
            str(state_root),
            "--job-id",
            record.job_id,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as worker:
        checkpoint_dir = (
            setup.store.job_dir(record.job_id)
            / "quality"
            / "visual-checkpoints"
        )
        try:
            first_checkpoints = _wait_for_slide_checkpoints(
                checkpoint_dir,
                worker,
                expected=7,
            )
            # All three remaining critic slots must be blocked inside their
            # actual
            # provider call before the kill. This closes the scheduling gap
            # between
            # a conservative budget debit and the fixture's durable call
            # witness.
            _wait_for_started_calls(
                state_root / "first-run-calls.jsonl",
                worker,
                expected=10,
            )
            os.killpg(worker.pid, signal.SIGKILL)
            assert worker.wait(timeout=5) == -signal.SIGKILL
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=5)

        slides = build_slides(state_root)
        slide_id_by_sha = {
            slide.rendered_sha256: slide.slide_id for slide in slides
        }
        committed_slide_ids = {
            slide_id_by_sha[
                VisualReviewCheckpoint.model_validate_json(
                    path.read_text(encoding="utf-8"),
                ).rendered_sha256
            ]
            for path in first_checkpoints
        }
        assert len(committed_slide_ids) == 7
        first_run_calls = _read_call_ids(state_root / "first-run-calls.jsonl")
        assert committed_slide_ids.issubset(first_run_calls)
        assert 7 <= len(first_run_calls) <= 10
        assert not (checkpoint_dir / "deck.json").exists()

        critic = _ResumeCritic()
        resumed = PresentationService(
            state_root,
            visual_critic=critic,
            execution_limits=PresentationExecutionLimits(critics=3),
        )
        result = getattr(resumed, "_review_visuals_with_checkpoints")(
            slides,
            job_id=record.job_id,
            repair_rounds=0,
            shared_dependencies=SHARED_DEPENDENCIES,
        )

        assert result is not None
        combined, evidence = result
        assert combined.critic_id == CRITIC_RESULT_ID
        slide_evidence = [item for item in evidence if item.scope == "slide"]
        deck_evidence = [item for item in evidence if item.scope == "deck"]
        assert len(slide_evidence) == 19
        assert len(deck_evidence) == 1
        assert {
            item.slide_id for item in slide_evidence if item.cache_hit
        } == (committed_slide_ids)
        remaining_slide_ids = {
            slide.slide_id for slide in slides
        } - committed_slide_ids
        assert set(critic.slide_calls) == remaining_slide_ids
        assert committed_slide_ids.isdisjoint(critic.slide_calls)
        assert critic.deck_calls == 1
        assert deck_evidence[0].cache_hit is False
        assert len(list(checkpoint_dir.glob("slide-*.json"))) == 19
        assert (checkpoint_dir / "deck.json").is_file()

        final_record = resumed.store.get(record.job_id)
        assert final_record.status == JobStatus.RUNNING
        assert final_record.cost_used_units == (
            len(first_run_calls) + len(critic.slide_calls) + critic.deck_calls
        )
        critic_metrics = [
            metric
            for metric in final_record.stage_metrics
            if metric.operation == "critic"
        ]
        assert sum(metric.cost_units for metric in critic_metrics) == 20
        assert (
            len(
                [
                    metric
                    for metric in critic_metrics
                    if metric.cost_units == 0
                ],
            )
            == 7
        )
        assert not list(checkpoint_dir.glob(".*.tmp"))

        print(
            json.dumps(
                {
                    "sigkill_exit": worker.returncode,
                    "durable_page_checkpoints_before_kill": len(
                        committed_slide_ids,
                    ),
                    "provider_calls_started_before_kill": len(first_run_calls),
                    "cached_pages_after_restart": len(committed_slide_ids),
                    "page_calls_after_restart": len(critic.slide_calls),
                    "deck_calls_after_restart": critic.deck_calls,
                    "final_page_checkpoints": 19,
                    "final_deck_checkpoint": True,
                    "charged_cost_units": final_record.cost_used_units,
                    "completed_review_cost_units": sum(
                        metric.cost_units for metric in critic_metrics
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )


# pylint: enable=R0915
