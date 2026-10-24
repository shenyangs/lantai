# -*- coding: utf-8 -*-
"""Check module-level coverage gates for high-risk packages."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


_CONDITION_COVERAGE_RE = re.compile(r"\((\d+)\s*/\s*(\d+)\)")


@dataclass(frozen=True)
class CoverageGate:
    name: str
    path_prefixes: tuple[str, ...]
    min_line: float
    min_branch: float


@dataclass(frozen=True)
class CoverageGateResult:
    gate: CoverageGate
    file_count: int
    line_covered: int
    line_total: int
    branch_covered: int
    branch_total: int

    @property
    def line_percent(self) -> float:
        if self.line_total == 0:
            return 0.0
        return self.line_covered / self.line_total * 100

    @property
    def branch_percent(self) -> float:
        if self.branch_total == 0:
            return 0.0
        return self.branch_covered / self.branch_total * 100

    @property
    def passed(self) -> bool:
        return (
            self.file_count > 0
            and self.line_percent >= self.gate.min_line
            and self.branch_total > 0
            and self.branch_percent >= self.gate.min_branch
        )

    def summary_line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        if self.file_count == 0:
            detail = "no files matched"
        elif self.branch_total == 0:
            detail = "no branch data"
        else:
            detail = (
                f"lines {self.line_percent:.2f}% >= {self.gate.min_line:.2f}%, "
                f"branches {self.branch_percent:.2f}% >= {self.gate.min_branch:.2f}%"
            )
        return f"{status} {self.gate.name}: {detail}"


@dataclass(frozen=True)
class CoverageGateReport:
    results: tuple[CoverageGateResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    def summary_lines(self) -> list[str]:
        return [result.summary_line() for result in self.results]


DEFAULT_GATES: tuple[CoverageGate, ...] = (
    CoverageGate(
        name="browser_workbench",
        path_prefixes=("app/browser_workbench/",),
        min_line=80.0,
        min_branch=60.0,
    ),
    CoverageGate(
        name="runtime",
        path_prefixes=("runtime/",),
        min_line=68.0,
        min_branch=48.0,
    ),
    CoverageGate(
        name="security",
        path_prefixes=("security/",),
        min_line=50.0,
        min_branch=30.0,
    ),
    CoverageGate(
        name="runner",
        path_prefixes=("app/runner/",),
        min_line=58.0,
        min_branch=40.0,
    ),
    CoverageGate(
        name="plugins",
        path_prefixes=("plugins/",),
        min_line=72.0,
        min_branch=55.0,
    ),
    CoverageGate(
        name="mcp_cli_control",
        path_prefixes=(
            "app/mcp/",
            "app/routers/mcp.py",
            "cli/mcp_cmd.py",
            "runtime/mcp_tools.py",
        ),
        min_line=55.0,
        min_branch=35.0,
    ),
)


def _normalize_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    marker = "/src/lantai/"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1]
    elif normalized.startswith("src/lantai/"):
        normalized = normalized[len("src/lantai/") :]
    elif normalized.startswith("lantai/"):
        normalized = normalized[len("lantai/") :]
    return normalized.lstrip("./")


def _iter_class_elements(root: ET.Element) -> Iterable[ET.Element]:
    return root.findall(".//class")


def _branch_counts(line: ET.Element) -> tuple[int, int]:
    if line.get("branch") != "true":
        return (0, 0)
    condition_coverage = line.get("condition-coverage", "")
    match = _CONDITION_COVERAGE_RE.search(condition_coverage)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def evaluate_coverage_gates(
    coverage_xml: Path | str,
    *,
    gates: Sequence[CoverageGate] = DEFAULT_GATES,
) -> CoverageGateReport:
    root = ET.parse(coverage_xml).getroot()
    results: list[CoverageGateResult] = []

    for gate in gates:
        file_count = 0
        line_covered = 0
        line_total = 0
        branch_covered = 0
        branch_total = 0

        for class_element in _iter_class_elements(root):
            filename = _normalize_filename(class_element.get("filename", ""))
            if not any(filename.startswith(prefix) for prefix in gate.path_prefixes):
                continue

            file_count += 1
            for line in class_element.findall("./lines/line"):
                line_total += 1
                if int(line.get("hits", "0")) > 0:
                    line_covered += 1
                covered, total = _branch_counts(line)
                branch_covered += covered
                branch_total += total

        results.append(
            CoverageGateResult(
                gate=gate,
                file_count=file_count,
                line_covered=line_covered,
                line_total=line_total,
                branch_covered=branch_covered,
                branch_total=branch_total,
            ),
        )

    return CoverageGateReport(results=tuple(results))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "coverage_xml",
        nargs="?",
        default="coverage.xml",
        help="Cobertura coverage XML generated by coverage.py / pytest-cov.",
    )
    args = parser.parse_args(argv)

    report = evaluate_coverage_gates(Path(args.coverage_xml))
    for line in report.summary_lines():
        print(line)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
