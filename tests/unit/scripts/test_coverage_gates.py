# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from scripts.check_coverage_gates import (
    DEFAULT_GATES,
    CoverageGate,
    evaluate_coverage_gates,
)


def _write_xml(path: Path, classes: str) -> Path:
    path.write_text(
        f"""<?xml version="1.0" ?>
<coverage>
  <packages>
    <package name="lantai">
      <classes>
{classes}
      </classes>
    </package>
  </packages>
</coverage>
""",
        encoding="utf-8",
    )
    return path


def test_default_gates_cover_high_risk_modules() -> None:
    assert {gate.name for gate in DEFAULT_GATES} == {
        "browser_workbench",
        "runtime",
        "security",
        "runner",
        "plugins",
        "mcp_cli_control",
    }


def test_gate_passes_when_directory_line_and_branch_coverage_meet_thresholds(
    tmp_path: Path,
) -> None:
    coverage_xml = _write_xml(
        tmp_path / "coverage.xml",
        """        <class filename="app/browser_workbench/broker.py">
          <lines>
            <line number="1" hits="1" branch="true" condition-coverage="100% (2/2)" />
            <line number="2" hits="1" />
          </lines>
        </class>
        <class filename="app/browser_workbench/tasks.py">
          <lines>
            <line number="1" hits="1" branch="true" condition-coverage="50% (1/2)" />
            <line number="2" hits="0" />
          </lines>
        </class>""",
    )

    report = evaluate_coverage_gates(
        coverage_xml,
        gates=(
            CoverageGate(
                name="browser_workbench",
                path_prefixes=("app/browser_workbench/",),
                min_line=70.0,
                min_branch=70.0,
            ),
        ),
    )

    assert report.passed is True
    assert report.results[0].line_percent == 75.0
    assert report.results[0].branch_percent == 75.0


def test_gate_fails_when_key_module_is_below_threshold(tmp_path: Path) -> None:
    coverage_xml = _write_xml(
        tmp_path / "coverage.xml",
        """        <class filename="runtime/events.py">
          <lines>
            <line number="1" hits="1" branch="true" condition-coverage="50% (1/2)" />
            <line number="2" hits="0" />
          </lines>
        </class>""",
    )

    report = evaluate_coverage_gates(
        coverage_xml,
        gates=(
            CoverageGate(
                name="runtime",
                path_prefixes=("runtime/",),
                min_line=60.0,
                min_branch=60.0,
            ),
        ),
    )

    assert report.passed is False
    assert report.results[0].passed is False
    assert "runtime" in "\n".join(report.summary_lines())


def test_gate_requires_branch_coverage_data(tmp_path: Path) -> None:
    coverage_xml = _write_xml(
        tmp_path / "coverage.xml",
        """        <class filename="security/tool_policy.py">
          <lines>
            <line number="1" hits="1" />
            <line number="2" hits="1" />
          </lines>
        </class>""",
    )

    report = evaluate_coverage_gates(
        coverage_xml,
        gates=(
            CoverageGate(
                name="security",
                path_prefixes=("security/",),
                min_line=80.0,
                min_branch=1.0,
            ),
        ),
    )

    assert report.passed is False
    assert report.results[0].branch_total == 0
    assert "no branch data" in "\n".join(report.summary_lines())
