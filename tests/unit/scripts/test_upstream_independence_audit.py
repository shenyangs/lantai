from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _load_audit_module():
    script_path = ROOT / "scripts" / "audit_upstream_independence.py"
    spec = importlib.util.spec_from_file_location(
        "audit_upstream_independence",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_upstream_product_and_runtime_names_are_blockers_in_product_code(
    tmp_path: Path,
):
    module = _load_audit_module()
    runtime_package = "".join(("agent", "scope"))
    upstream_product = "".join(("qwen", "paw"))
    source = tmp_path / "src" / "lantai"
    source.mkdir(parents=True)
    (source / "bridge.py").write_text(
        f"from {runtime_package} import Agent\n"
        f"UPSTREAM_APP = '{upstream_product}'\n",
        encoding="utf-8",
    )

    report = module.build_report(tmp_path)

    assert report["blocker_occurrences"] == 2
    assert {item["pattern"].lower() for item in report["blockers"]} == {
        runtime_package,
        upstream_product,
    }


def test_historical_test_mentions_are_reported_but_do_not_block(tmp_path: Path):
    module = _load_audit_module()
    upstream_product = "".join(("qwen", "paw"))
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_migration.py").write_text(
        f"RETIRED_PRODUCT = '{upstream_product}'\n",
        encoding="utf-8",
    )

    report = module.build_report(tmp_path)

    assert report["occurrences"] == 1
    assert report["blocker_occurrences"] == 0
    assert report["blockers"] == []


def test_fail_on_blocker_returns_non_zero_for_current_product_tree(monkeypatch):
    module = _load_audit_module()
    monkeypatch.setattr(
        module,
        "build_report",
        lambda _root: {
            "repo": "/tmp/project",
            "scanned_files": 1,
            "matched_files": 1,
            "occurrences": 1,
            "blocker_occurrences": 1,
            "by_category": {},
            "top_files": [],
            "references": [],
            "blockers": [{"path": "src/lantai/bridge.py"}],
        },
    )

    assert module.main(["--fail-on-blocker"]) == 1


def test_main_test_workflow_enforces_independence_audit() -> None:
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8",
    )

    assert (
        "python scripts/audit_upstream_independence.py --fail-on-blocker"
        in workflow
    )
    for watched_path in (
        "'.flake8'",
        "'docs/**'",
        "'console/src/**'",
        "'console/package.json'",
        "'scripts/audit_upstream_independence.py'",
        "'uv.lock'",
    ):
        assert watched_path in workflow
