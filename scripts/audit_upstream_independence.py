#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit forbidden upstream identities in product-owned source.

This static, dependency-free guard intentionally excludes generated bundles and
vendor directories so it measures source-owned coupling rather than artifacts.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path


_FORBIDDEN_RUNTIME_PACKAGE = "".join(("agent", "scope"))
_FORBIDDEN_FRONTEND_NAMESPACE = "".join(("@agent", "scope-ai"))
_FORBIDDEN_PRODUCT = "".join(("qwen", "paw"))
_FORBIDDEN_APP = "".join(("paw", "app"))
PATTERN_RE = re.compile(
    "|".join(
        (
            rf"\b{re.escape(_FORBIDDEN_RUNTIME_PACKAGE)}(?:[-_]runtime)?\b",
            re.escape(_FORBIDDEN_FRONTEND_NAMESPACE),
            rf"\b{re.escape(_FORBIDDEN_PRODUCT)}\b",
            rf"\b{re.escape(_FORBIDDEN_APP)}\b",
            rf"\b{re.escape(_FORBIDDEN_RUNTIME_PACKAGE)}Runtime(?:Request|Response)Card\b",
        )
    ),
    re.IGNORECASE,
)

SCAN_ROOTS = (
    Path(".flake8"),
    Path(".github"),
    Path("README.md"),
    Path("pyproject.toml"),
    Path("console/package.json"),
    Path("docs"),
    Path("scripts"),
    Path("src/lantai"),
    Path("console/src"),
    Path("tests"),
)

TEXT_SUFFIXES = {
    ".cjs",
    ".js",
    ".jsx",
    ".json",
    ".less",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}

EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "electron-dist",
    "node_modules",
    "release",
}

EXCLUDED_PREFIXES = (
    Path("console/dist"),
    Path("console/electron-dist"),
    Path("src/lantai/console"),
)


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    pattern: str
    category: str
    excerpt: str


def is_blocker(reference: Reference) -> bool:
    """Return whether a reference can couple the shipped product upstream."""

    return reference.category != "tests"


def is_excluded(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDED_PARTS:
        return True
    return any(path == prefix or prefix in path.parents for prefix in EXCLUDED_PREFIXES)


def is_text_file(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in {"package.json", "pyproject.toml"}


def iter_candidate_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        absolute = repo_root / root
        if not absolute.exists():
            continue
        if absolute.is_file():
            if not is_excluded(root) and is_text_file(absolute):
                files.append(root)
            continue
        for candidate in absolute.rglob("*"):
            relative = candidate.relative_to(repo_root)
            if candidate.is_file() and not is_excluded(relative) and is_text_file(candidate):
                files.append(relative)
    return sorted(set(files), key=lambda item: item.as_posix())


def category_for(path: Path, line: str) -> str:
    path_text = path.as_posix()

    if path_text in {"pyproject.toml", "console/package.json"}:
        return "dependency_declaration"

    if path.parts and path.parts[0] == "tests":
        return "tests"

    if path_text == ".flake8" or path_text.startswith(".github/"):
        return "project_configuration"

    if (
        path_text == "README.md"
        or path_text.startswith("docs/")
        or path_text.startswith("scripts/")
    ):
        return "project_governance"

    if path_text.startswith("src/lantai/runtime/adapters/") or path_text.startswith(
        "src/lantai/runtime/converters/",
    ):
        return "python_runtime_adapter"

    if path_text.startswith("src/lantai/agents/tools/"):
        return "python_tool_contract"

    if path_text.startswith("src/lantai/app/channels/") or path_text.startswith(
        "src/lantai/cli/",
    ):
        return "python_channel_schema"

    if path_text.startswith("src/lantai/"):
        return "python_runtime_core"

    if path_text.startswith("console/src/"):
        if (
            f"{_FORBIDDEN_FRONTEND_NAMESPACE}/design" in line
            or f"{_FORBIDDEN_FRONTEND_NAMESPACE}/icons" in line
        ):
            return "frontend_design_system"
        if "pages/Chat/" in path_text or "sessionApi" in path_text:
            return "frontend_chat_runtime"
        return "frontend_app"

    return "other"


def scan_file(repo_root: Path, relative_path: Path) -> list[Reference]:
    try:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = (repo_root / relative_path).read_text(encoding="utf-8", errors="replace")

    references: list[Reference] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        for match in PATTERN_RE.finditer(line):
            excerpt = " ".join(line.strip().split())
            references.append(
                Reference(
                    path=relative_path.as_posix(),
                    line=line_number,
                    pattern=match.group(0),
                    category=category_for(relative_path, line),
                    excerpt=excerpt[:220],
                ),
            )
    return references


def build_report(repo_root: Path) -> dict[str, object]:
    files = iter_candidate_files(repo_root)
    references: list[Reference] = []
    for relative_path in files:
        references.extend(scan_file(repo_root, relative_path))

    by_category: dict[str, dict[str, int]] = {}
    category_occurrences = Counter(reference.category for reference in references)
    category_files: dict[str, set[str]] = defaultdict(set)
    for reference in references:
        category_files[reference.category].add(reference.path)

    for category in sorted(category_occurrences):
        by_category[category] = {
            "files": len(category_files[category]),
            "occurrences": category_occurrences[category],
        }

    per_file = Counter(reference.path for reference in references)
    blockers = [reference for reference in references if is_blocker(reference)]
    return {
        "repo": str(repo_root),
        "scanned_files": len(files),
        "matched_files": len(per_file),
        "occurrences": len(references),
        "blocker_occurrences": len(blockers),
        "by_category": by_category,
        "top_files": per_file.most_common(),
        "references": [asdict(reference) for reference in references],
        "blockers": [asdict(reference) for reference in blockers],
    }


def print_text_report(report: dict[str, object], *, top: int) -> None:
    print("Upstream independence audit")
    print(f"Repo: {report['repo']}")
    print(f"Scanned files: {report['scanned_files']}")
    print(f"Matched files: {report['matched_files']}")
    print(f"Occurrences: {report['occurrences']}")
    print(f"Blocking occurrences: {report['blocker_occurrences']}")

    print("\nBy category:")
    by_category = report["by_category"]
    if isinstance(by_category, dict):
        for category, counts in by_category.items():
            print(
                f"  {category:<28} files={counts['files']:<4} "
                f"occurrences={counts['occurrences']}",
            )

    print(f"\nTop {top} files:")
    top_files = report["top_files"]
    if isinstance(top_files, list):
        for path, count in top_files[:top]:
            print(f"  {count:<4} {path}")

    print("\nNotes:")
    print("  Product source, configuration, governance and dependencies are blocking.")
    print("  Historical compatibility tests are reported but do not block.")
    print("  Generated bundles, electron-dist, node_modules and embedded console assets are excluded.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report as JSON.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of top files to print in text mode.",
    )
    parser.add_argument(
        "--fail-on-blocker",
        action="store_true",
        help="Exit with status 1 when a shipped-source or dependency blocker exists.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    report = build_report(repo_root)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report, top=args.top)

    return 1 if args.fail_on_blocker and report["blocker_occurrences"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
