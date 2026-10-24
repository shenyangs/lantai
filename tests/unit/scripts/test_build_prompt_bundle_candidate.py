from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_prompt_bundle_candidate_script_stages_canary_without_user_overlay(
    tmp_path: Path,
) -> None:
    gate = tmp_path / "gate.json"
    gate.write_text('{"status":"pass"}\n', encoding="utf-8")
    registry = tmp_path / "registry"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/build_prompt_bundle_candidate.py",
            "--registry-dir",
            str(registry),
            "--gate-evidence",
            str(gate),
            "--rollout",
            "canary",
            "--promote",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["rollout"] == "canary"
    bundle = Path(result["bundle_path"]).read_text(encoding="utf-8")
    assert '"user_overlay_persisted": false' in bundle
    assert "workspace.prompt_files" not in bundle
