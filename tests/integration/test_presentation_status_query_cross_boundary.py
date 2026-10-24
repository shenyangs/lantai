# -*- coding: utf-8 -*-
"""RUN-04: completed-job status probes stay read-only across processes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from PIL import Image

from lantai.agents.tools.presentation import create_presentation
from lantai.config.context import WorkspaceContext, set_current_workspace_context
from lantai.presentation.models import VisualCriticResult
from lantai.presentation.render import RenderResult


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_status_query_worker.py"
)
CONVERSATION_ID = "conversation-run04-read-only-query"


class _PassingVisualCritic:
    def review(self, _slides) -> VisualCriticResult:
        return VisualCriticResult(critic_id="run04-critic", issues=[])


class _NoRepairExpected:
    def repair(self, *_args, **_kwargs) -> None:
        raise AssertionError("RUN-04 fixture unexpectedly required repair")


def _render_one(_source, output_dir, *, timeout=120) -> RenderResult:
    _ = timeout
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    pdf_path = destination / "presentation.pdf"
    pdf_path.write_bytes(b"RUN-04 render evidence\n")
    image_path = destination / "slide-1.png"
    image = Image.new("RGB", (640, 360), "white")
    image.paste("black", (0, 0, 320, 360))
    image.save(image_path)
    return RenderResult(pdf_path=pdf_path, slide_images=(image_path,))


def _workspace_context(tmp_path: Path) -> WorkspaceContext:
    context = WorkspaceContext(
        agent_home=tmp_path / "agent-home",
        state_root=tmp_path / "state",
        project_root=tmp_path / "workspace",
        workspace_grant_id=CONVERSATION_ID,
        trust_state="trusted",
    )
    context.agent_home.mkdir()
    context.state_root.mkdir()
    context.project_root.mkdir()
    return context


def _tree_snapshot(root: Path) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        metadata = path.lstat()
        if path.is_symlink():
            snapshot[relative] = {
                "kind": "symlink",
                "target": os.readlink(path),
            }
        elif path.is_file():
            snapshot[relative] = {
                "kind": "file",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        elif path.is_dir():
            snapshot[relative] = {
                "kind": "directory",
                "mtime_ns": metadata.st_mtime_ns,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
    return snapshot


def _worker_command(
    context: WorkspaceContext,
    *,
    job_id: str,
    repeats: int,
) -> list[str]:
    return [
        sys.executable,
        str(WORKER_SCRIPT),
        "--agent-home",
        str(context.agent_home),
        "--state-root",
        str(context.state_root),
        "--project-root",
        str(context.project_root),
        "--conversation-id",
        CONVERSATION_ID,
        "--job-id",
        job_id,
        "--repeats",
        str(repeats),
    ]


def test_completed_job_status_query_has_zero_cross_process_side_effects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("lantai.presentation.service.render_pptx", _render_one)
    monkeypatch.setattr(
        "lantai.agents.tools.presentation.UtilityPresentationVisualCritic",
        lambda _agent_id: _PassingVisualCritic(),
    )
    monkeypatch.setattr(
        "lantai.agents.tools.presentation.UtilityPresentationRepairDesigner",
        lambda _agent_id: _NoRepairExpected(),
    )
    context = _workspace_context(tmp_path)
    set_current_workspace_context(context)
    try:
        generated = asyncio.run(
            create_presentation(
                title="RUN-04 已完成任务只读查询",
                slides=[
                    {
                        "layout_id": "cover",
                        "objects": [
                            {
                                "kind": "text",
                                "role": "title",
                                "text": "已验证成果只读查询",
                                "slot": "title",
                            },
                            {
                                "kind": "text",
                                "role": "body",
                                "text": "查询不是发布，也不是重新生成",
                                "slot": "subtitle",
                            },
                        ],
                    }
                ],
                output_filename="run04-seed-delivery.pptx",
            ),
        )
    finally:
        set_current_workspace_context(None)

    assert generated.is_error is False
    job_id = str(generated.metadata["job_id"])
    job_root = context.state_root / "presentation-jobs" / job_id
    manifest_before = json.loads(
        (job_root / "manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest_before["status"] == "completed"
    assert (context.project_root / "run04-seed-delivery.pptx").is_file()

    state_before = _tree_snapshot(context.state_root)
    project_before = _tree_snapshot(context.project_root)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (
            str(PROJECT_ROOT / "src"),
            str(PROJECT_ROOT),
            environment.get("PYTHONPATH", ""),
        )
        if part
    )

    process_results: list[dict[str, object]] = []
    for _ in range(3):
        worker = subprocess.run(
            _worker_command(context, job_id=job_id, repeats=4),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert worker.returncode == 0, (
            f"stdout={worker.stdout!r}; stderr={worker.stderr!r}"
        )
        result = json.loads(worker.stdout.strip().splitlines()[-1])
        process_results.append(result)

    assert len({int(result["pid"]) for result in process_results}) == 3
    assert sum(int(result["queries"]) for result in process_results) == 12
    assert all(result["read_only_store_inits"] == 4 for result in process_results)
    assert all(result["mutable_store_inits"] == 0 for result in process_results)
    assert all(result["side_effect_calls"] == {} for result in process_results)
    assert all(result["file_block_count"] == 0 for result in process_results)
    assert all(
        result["job_statuses"] == ["completed"] * 4
        for result in process_results
    )
    assert all(
        result["verification_statuses"] == ["visual_verified"] * 4
        for result in process_results
    )
    assert all(
        result["publish_ready"] == [True] * 4 for result in process_results
    )
    assert all(
        result["side_effect_outcomes"] == ["none"] * 4
        for result in process_results
    )
    response_digests = {
        digest
        for result in process_results
        for digest in result["response_digests"]
    }
    assert len(response_digests) == 1

    assert _tree_snapshot(context.state_root) == state_before
    assert _tree_snapshot(context.project_root) == project_before
    manifest_after = json.loads(
        (job_root / "manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest_after == manifest_before
    assert not list(context.project_root.glob("*recovered*.pptx"))
    assert not list(context.project_root.glob("*status*.pptx"))

    print(
        json.dumps(
            {
                "job_id": job_id,
                "processes": len(process_results),
                "queries": 12,
                "unique_response_digests": len(response_digests),
                "read_only_store_inits": sum(
                    int(result["read_only_store_inits"])
                    for result in process_results
                ),
                "mutable_store_inits": 0,
                "file_blocks": 0,
                "forbidden_side_effect_calls": 0,
                "state_tree_unchanged": True,
                "project_tree_unchanged": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
