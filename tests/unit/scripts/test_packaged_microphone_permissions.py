"""Behavior lock for removal of the packaged microphone permission gate."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_packaged_microphone_permission_gate_is_removed() -> None:
    gate = ROOT / "scripts/check_packaged_microphone_permissions.py"
    release_script = (ROOT / "build_and_deploy.sh").read_text(encoding="utf-8")

    assert not gate.exists()
    assert gate.name not in release_script
