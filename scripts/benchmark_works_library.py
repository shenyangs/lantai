# -*- coding: utf-8 -*-
"""Benchmark the real Works index reader in isolated disposable workspaces."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, cast
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from lantai.app import works as works_store
from lantai.app.routers import works as works_router
from lantai.app.works import WorkItem, list_works


WARM_SCOPED_P95_BUDGET_MS = 500.0
COLD_SCOPED_P95_BUDGET_MS = 1500.0
CONTROL_ADMISSION_P95_BUDGET_MS = 1000.0
SCOPED_WORK_COUNT = 10
_ONE_MIB = 1024 * 1024


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((percentile / 100.0) * (len(ordered) - 1)))),
    )
    return ordered[index]


def _git_head() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() or None


def _write_fixture(path: Path, index: int, *, large_bytes: int) -> None:
    suffix = path.suffix.lower()
    if large_bytes:
        chunk = hashlib.sha256(f"work-{index}".encode()).digest() * 2048
        remaining = large_bytes
        with path.open("wb") as handle:
            while remaining > 0:
                payload = chunk[: min(len(chunk), remaining)]
                handle.write(payload)
                remaining -= len(payload)
        return
    if suffix == ".html":
        path.write_text(
            f"<!doctype html><title>work {index}</title><img src='asset.bin'>",
            encoding="utf-8",
        )
        path.with_name("asset.bin").write_bytes(
            hashlib.sha256(f"asset-{index}".encode()).digest() * 32,
        )
        return
    if suffix in {".docx", ".xlsx", ".pptx"}:
        member = {
            ".docx": "word/document.xml",
            ".xlsx": "xl/workbook.xml",
            ".pptx": "ppt/presentation.xml",
        }[suffix]
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr(member, f"<fixture index='{index}' />")
        return
    if suffix == ".pdf":
        path.write_bytes(
            b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
            + (f"% fixture {index}\n".encode() * 16),
        )
        return
    path.write_text((f"work {index}\n" * 64), encoding="utf-8")


def _fixture_size(path: Path) -> int:
    if path.suffix.lower() != ".html":
        return path.stat().st_size
    return sum(
        candidate.stat().st_size
        for candidate in path.parent.rglob("*")
        if candidate.is_file() and not candidate.is_symlink()
    )


def _provision_dataset(
    workspace_dir: Path,
    count: int,
    *,
    large_file_bytes: int,
) -> tuple[list[WorkItem], int]:
    # This benchmark intentionally exercises the real transactional helpers.
    # pylint: disable=protected-access
    baseline = works_store._prepare_index_write(workspace_dir)
    suffixes = (".txt", ".md", ".pdf", ".docx", ".xlsx", ".pptx", ".html")
    items: list[WorkItem] = []
    total_bytes = 0
    for index in range(count):
        work_id = str(uuid4())
        suffix = suffixes[index % len(suffixes)]
        managed_dir = workspace_dir / ".lantai" / "works" / "files" / work_id
        managed_dir.mkdir(parents=True, exist_ok=True)
        path = managed_dir / f"work-{index}{suffix}"
        payload_size = large_file_bytes if index == count - 1 else 0
        _write_fixture(path, index, large_bytes=payload_size)
        digest = works_store._source_digest(path)
        size = _fixture_size(path)
        total_bytes += size
        items.append(
            WorkItem(
                id=work_id,
                artifact_id=f"artifact-benchmark-{index}",
                title=f"Benchmark work {index}",
                path=str(path),
                source_path=str(path),
                size=size,
                source_digest=digest,
                source_chat_id=(
                    "selected-chat"
                    if index < SCOPED_WORK_COUNT
                    else f"chat-{index % 32}"
                ),
                source_run_id=f"benchmark-run-{index}",
                lineage_root_id=work_id,
            ),
        )
    works_store._write_index(workspace_dir, items, baseline=baseline)
    return items, total_bytes


@contextmanager
def _count_hash_io() -> Iterator[dict[str, int]]:
    # Temporary instrumentation of the production digest seam is the metric.
    # pylint: disable=protected-access
    original = works_store._source_digest
    observed = {"calls": 0, "bytes": 0}

    def _counted(path: Path) -> str:
        observed["calls"] += 1
        observed["bytes"] += _fixture_size(path)
        return original(path)

    works_store._source_digest = _counted
    try:
        yield observed
    finally:
        works_store._source_digest = original


def _measure_list(
    workspace_dir: Path,
    *,
    chat_id: str | None,
    samples: int,
) -> dict[str, object]:
    durations: list[float] = []
    hash_calls: list[int] = []
    hash_bytes: list[int] = []
    result_counts: list[int] = []
    for _ in range(samples):
        with _count_hash_io() as observed:
            started = time.perf_counter()
            result = list_works(workspace_dir, source_chat_id=chat_id)
            durations.append((time.perf_counter() - started) * 1000)
        hash_calls.append(observed["calls"])
        hash_bytes.append(observed["bytes"])
        result_counts.append(len(result))
    return {
        "samples": samples,
        "result_counts": result_counts,
        "latency_ms": durations,
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "hash_calls": hash_calls,
        "hash_bytes": hash_bytes,
    }


async def _measure_control_admission(
    workspace_dir: Path,
    *,
    samples: int,
) -> dict[str, object]:
    original_agent_lookup = works_router.get_agent_for_request

    async def _agent_for_request(_request):
        return SimpleNamespace(workspace_dir=str(workspace_dir))

    works_router.get_agent_for_request = _agent_for_request
    latencies: list[float] = []
    try:
        for _ in range(samples):
            request_task = asyncio.create_task(
                works_router.get_works(SimpleNamespace(), chat_id=None),
            )
            started = time.perf_counter()
            await asyncio.sleep(0)
            latencies.append((time.perf_counter() - started) * 1000)
            await request_task
    finally:
        works_router.get_agent_for_request = original_agent_lookup
    return {
        "samples": samples,
        "latency_ms": latencies,
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
    }


async def _run(args: argparse.Namespace) -> dict[str, object]:
    datasets: list[dict[str, object]] = []
    counts = tuple(sorted(set(args.counts)))
    for count in counts:
        with tempfile.TemporaryDirectory(
            prefix=f"lantai-works-benchmark-{count}-",
        ) as temp_dir:
            workspace_dir = Path(temp_dir)
            large_bytes = (
                args.large_file_mib * _ONE_MIB if count == max(counts) else 0
            )
            _, total_bytes = _provision_dataset(
                workspace_dir,
                count,
                large_file_bytes=large_bytes,
            )
            cold_scoped = _measure_list(
                workspace_dir,
                chat_id="selected-chat",
                samples=1,
            )
            warm_scoped = _measure_list(
                workspace_dir,
                chat_id="selected-chat",
                samples=args.warm_samples,
            )
            full_library = _measure_list(
                workspace_dir,
                chat_id=None,
                samples=args.full_samples,
            )
            control = await _measure_control_admission(
                workspace_dir,
                samples=args.control_samples,
            )
            scoped_hash_max = max(
                cast(list[int], warm_scoped["hash_calls"]),
                default=0,
            )
            datasets.append(
                {
                    "work_count": count,
                    "total_managed_bytes": total_bytes,
                    "large_file_bytes": large_bytes,
                    "cold_scoped": cold_scoped,
                    "warm_scoped": warm_scoped,
                    "full_library": full_library,
                    "control_admission": control,
                    "assertions": {
                        "cold_scoped_under_budget": cast(
                            float,
                            cold_scoped["p95_ms"],
                        )
                        <= COLD_SCOPED_P95_BUDGET_MS,
                        "warm_scoped_under_budget": cast(
                            float,
                            warm_scoped["p95_ms"],
                        )
                        <= WARM_SCOPED_P95_BUDGET_MS,
                        "scoped_hashes_only_selected_works": scoped_hash_max
                        == min(count, SCOPED_WORK_COUNT),
                        "control_admission_under_budget": cast(
                            float,
                            control["p95_ms"],
                        )
                        <= CONTROL_ADMISSION_P95_BUDGET_MS,
                    },
                },
            )
    all_passed = all(
        all(dataset["assertions"].values()) for dataset in datasets
    )
    return {
        "schema": "lantai.stable-candidate.performance.v1",
        "record_kind": "measurement",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_sha": _git_head(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "isolation": "TemporaryDirectory per dataset; no user Works data",
            "cold_definition": "first read after fixture creation; OS cache not flushed",
        },
        "approved_resource_budget": "local isolated source benchmark; no Provider use",
        "frozen_thresholds": {
            "warm_scoped_metadata_p95_ms": WARM_SCOPED_P95_BUDGET_MS,
            "cold_scoped_metadata_p95_ms": COLD_SCOPED_P95_BUDGET_MS,
            "local_control_admission_p95_ms": CONTROL_ADMISSION_P95_BUDGET_MS,
            "scoped_work_count": SCOPED_WORK_COUNT,
        },
        "datasets": datasets,
        "all_assertions_passed": all_passed,
        "ordinary_endurance": {
            "status": "NOT_RUN",
            "candidate_id": None,
            "duration_seconds": None,
            "activity_timeline": [],
            "resource_samples": [],
            "failure_events": [],
        },
        "is_overnight_qualification": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--counts",
        type=int,
        nargs="+",
        default=[10, 100, 1000],
    )
    parser.add_argument("--warm-samples", type=int, default=10)
    parser.add_argument("--full-samples", type=int, default=3)
    parser.add_argument("--control-samples", type=int, default=5)
    parser.add_argument("--large-file-mib", type=int, default=64)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args()
    if any(count <= 0 for count in args.counts):
        parser.error("--counts must contain only positive values")
    for name in ("warm_samples", "full_samples", "control_samples"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.large_file_mib < 0 or args.large_file_mib > 1024:
        parser.error("--large-file-mib must be between 0 and 1024")
    return args


def main() -> None:
    args = _parse_args()
    result = asyncio.run(_run(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.evidence_output is not None:
        output = args.evidence_output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not result["all_assertions_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
