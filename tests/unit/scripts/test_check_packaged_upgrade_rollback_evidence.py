from __future__ import annotations

import hashlib
import importlib.util
import plistlib
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts/check_packaged_upgrade_rollback_evidence.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _app(root: Path, *, version: str, content: bytes) -> tuple[Path, str]:
    app = root / f"Lantai-{version}.app"
    executable = app / "Contents/MacOS/Lantai"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(content)
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {"CFBundleExecutable": "Lantai", "CFBundleShortVersionString": version},
            handle,
        )
    return app, hashlib.sha256(content).hexdigest()


def _payload(module, previous: Path, current: Path, old_digest: str, new_digest: str):
    snapshot = {
        "profile_id": "profile_v1_upgrade_fixture",
        "profile_revision": 3,
        "active_profile_id": "profile_v1_upgrade_fixture",
        "slot_1_profile_id": "profile_v1_upgrade_fixture",
        "customization_mode": "normal",
        "effective_values": {
            "appearance.palette": "warm",
            "persona.presence": "quiet",
        },
        "chat_event_count": 11,
        "task_status": "completed",
        "task_stdout": "upgrade-rollback-task-sentinel",
    }
    phases = []
    for index, (name, version, app, digest) in enumerate(
        zip(
            module.EXPECTED_PHASES,
            ("1.0.48", "2.0.0", "1.0.48", "2.0.0"),
            (previous, current, previous, current),
            (old_digest, new_digest, old_digest, new_digest),
        ),
        1,
    ):
        phases.append(
            {
                "phase": name,
                "version": version,
                "app_path": str(app),
                "executable_sha256": digest,
                "desktop_instance_nonce": f"nonce-{index}",
                "snapshot": dict(snapshot),
            }
        )
    return {
        "schema": module.SCHEMA,
        "passed": True,
        "failure": None,
        "previous_app": {"artifact_sha256": {"main_executable": f"sha256:{old_digest}"}},
        "current_app": {"artifact_sha256": {"main_executable": f"sha256:{new_digest}"}},
        "phases": phases,
    }


def test_exact_four_phase_transition_passes(tmp_path: Path) -> None:
    module = _module()
    previous, old_digest = _app(tmp_path, version="1.0.48", content=b"old")
    current, new_digest = _app(tmp_path, version="2.0.0", content=b"new")

    assert module.audit(
        _payload(module, previous, current, old_digest, new_digest),
        previous,
        current,
    ) == []


def test_changed_package_or_personalization_fails(tmp_path: Path) -> None:
    module = _module()
    previous, old_digest = _app(tmp_path, version="1.0.48", content=b"old")
    current, new_digest = _app(tmp_path, version="2.0.0", content=b"new")
    payload = _payload(module, previous, current, old_digest, new_digest)
    payload["current_app"]["artifact_sha256"]["main_executable"] = "sha256:" + "0" * 64
    payload["phases"][2]["snapshot"]["active_profile_id"] = None

    errors = module.audit(payload, previous, current)

    assert "current app executable digest does not match evidence" in errors
    assert "durable state changed across upgrade or rollback" in errors
