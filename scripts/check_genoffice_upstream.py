#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify that Lantai's vendored GenOffice pin matches upstream main."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_REPOSITORY = "https://github.com/genspark-ai/genoffice.git"
DEFAULT_BRANCH = "main"
PIN_PATTERN = re.compile(r"固定提交(?:：|仍为\s+)`([0-9a-f]{40})`")


@dataclass(frozen=True)
class UpstreamCheck:
    repository: str
    branch: str
    pinned_commit: str
    upstream_commit: str | None
    status: str
    checked_at: str
    detail: str = ""


def parse_source_pin(text: str) -> str:
    match = PIN_PATTERN.search(text)
    if not match:
        raise ValueError("SOURCE.md 中缺少 40 位 GenOffice 固定提交")
    return match.group(1)


def read_source_pin(source_file: Path) -> str:
    if not source_file.is_file():
        raise ValueError(f"GenOffice 来源文件不存在：{source_file}")
    return parse_source_pin(source_file.read_text(encoding="utf-8"))


def fetch_upstream_commit(repository: str, branch: str, timeout: float) -> str:
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--exit-code", repository, f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法查询 GenOffice 上游：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git ls-remote failed").strip()
        raise RuntimeError(f"无法查询 GenOffice 上游：{detail}")
    commit = proc.stdout.split(maxsplit=1)[0].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("GenOffice 上游返回了无效提交号")
    return commit


def compare_pin(pinned_commit: str, upstream_commit: str) -> str:
    return "current" if pinned_commit == upstream_commit else "stale"


def _report_payload(check: UpstreamCheck) -> dict[str, object]:
    return {
        "schema": "com.lantai.genoffice-upstream-check.v1",
        **asdict(check),
    }


def write_report(path: Path | None, check: UpstreamCheck) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_report_payload(check), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_recent_report(
    path: Path | None,
    *,
    repository: str,
    branch: str,
    pinned_commit: str,
    max_age_hours: float,
) -> UpstreamCheck | None:
    """Return a compatible, successful freshness result inside the interval.

    A stale upstream is still a successfully *checked* upstream.  We deliberately
    reuse that information for the configured interval so local builds do not
    turn every invocation into an upstream-release decision.
    """
    if path is None or max_age_hours <= 0 or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        check = UpstreamCheck(
            repository=str(payload["repository"]),
            branch=str(payload["branch"]),
            pinned_commit=str(payload["pinned_commit"]),
            upstream_commit=(
                str(payload["upstream_commit"])
                if payload.get("upstream_commit")
                else None
            ),
            status=str(payload["status"]),
            checked_at=str(payload["checked_at"]),
            detail=str(payload.get("detail") or ""),
        )
        checked_at = datetime.fromisoformat(check.checked_at.replace("Z", "+00:00"))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if checked_at.tzinfo is None:
        return None
    if (
        check.repository != repository
        or check.branch != branch
        or check.pinned_commit != pinned_commit
        or check.status not in {"current", "stale"}
        or check.upstream_commit is None
    ):
        return None
    age_hours = (datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc)).total_seconds() / 3600
    if age_hours < 0 or age_hours > max_age_hours:
        return None
    return check


def _github_compare_url(repository: str, pinned: str, upstream: str) -> str:
    base = repository.removesuffix(".git")
    return f"{base}/compare/{pinned}...{upstream}"


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-file",
        type=Path,
        default=root / "console/vendor/lantai-docx-editor/SOURCE.md",
    )
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--write-report", type=Path)
    parser.add_argument("--allow-network-failure", action="store_true")
    parser.add_argument(
        "--check-interval-hours",
        type=float,
        default=0.0,
        help="Reuse a compatible successful report for this many hours.",
    )
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Record and report an upstream change without failing this build.",
    )
    args = parser.parse_args(argv)

    try:
        pinned = read_source_pin(args.source_file)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    cached = read_recent_report(
        args.write_report,
        repository=args.repository,
        branch=args.branch,
        pinned_commit=pinned,
        max_age_hours=args.check_interval_hours,
    )
    if cached is not None:
        if cached.status == "stale" and not args.allow_stale:
            compare_url = _github_compare_url(
                cached.repository,
                cached.pinned_commit,
                cached.upstream_commit,
            )
            raise SystemExit(
                "GenOffice 上游已有新提交，发版已停止。\n"
                f"澜台固定版本：{cached.pinned_commit}\n"
                f"上游 {cached.branch}：{cached.upstream_commit}\n"
                f"请审查并合并差异：{compare_url}"
            )
        print(
            "复用 GenOffice 上游检查（仍在检查间隔内）："
            f"{cached.status}，上次检查 {cached.checked_at}"
        )
        return 0

    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        upstream = fetch_upstream_commit(args.repository, args.branch, args.timeout)
    except RuntimeError as exc:
        check = UpstreamCheck(
            repository=args.repository,
            branch=args.branch,
            pinned_commit=pinned,
            upstream_commit=None,
            status="unverifiable",
            checked_at=checked_at,
            detail=str(exc),
        )
        write_report(args.write_report, check)
        if args.allow_network_failure:
            print(f"Warning: {exc}", file=sys.stderr)
            return 0
        raise SystemExit(str(exc)) from exc

    status = compare_pin(pinned, upstream)
    check = UpstreamCheck(
        repository=args.repository,
        branch=args.branch,
        pinned_commit=pinned,
        upstream_commit=upstream,
        status=status,
        checked_at=checked_at,
    )
    write_report(args.write_report, check)
    if status == "stale" and not args.allow_stale:
        compare_url = _github_compare_url(args.repository, pinned, upstream)
        raise SystemExit(
            "GenOffice 上游已有新提交，发版已停止。\n"
            f"澜台固定版本：{pinned}\n"
            f"上游 {args.branch}：{upstream}\n"
            f"请审查并合并差异：{compare_url}"
        )

    if status == "stale":
        print(
            "GenOffice 上游有新提交，已记录但按当前构建策略不阻断："
            f"{upstream} ({args.branch})",
            file=sys.stderr,
        )
    else:
        print(f"GenOffice 上游检查通过：{upstream} ({args.branch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
