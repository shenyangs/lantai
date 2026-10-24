from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_node_probe_enumerates_scripts_and_rejects_missing_dependencies(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "demo" / "scripts"
    modules = tmp_path / "node_modules"
    scripts.mkdir(parents=True)
    modules.mkdir()
    (scripts / "good.cjs").write_text(
        "require('fs'); require('./helper.cjs');\n",
        encoding="utf-8",
    )
    (scripts / "helper.cjs").write_text("module.exports = 1;\n", encoding="utf-8")
    (scripts / "missing.cjs").write_text(
        "require('definitely-missing-lantai-node-probe');\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            shutil.which("node"),
            str(ROOT / "scripts" / "packaged_skill_node_probe.cjs"),
            "--skills-root",
            str(tmp_path),
            "--node-modules",
            str(modules),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    report = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert report["scriptCount"] == 3
    assert report["dependencyCount"] == 3
    assert report["failedCount"] == 1
    assert report["failures"][0]["script"] == "demo/scripts/missing.cjs"


def test_release_smoke_executes_node_and_shell_skill_probes() -> None:
    smoke = (ROOT / "scripts" / "packaged_skill_runtime_smoke.py").read_text(
        encoding="utf-8",
    )

    assert "packaged_skill_node_probe.cjs" in smoke
    assert '"node_import_probe"' in smoke
    assert '"shell_syntax_count"' in smoke
