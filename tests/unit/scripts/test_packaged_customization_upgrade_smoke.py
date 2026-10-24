from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_module():
    _load_script(
        "packaged_app_lifecycle_smoke",
        SCRIPTS_DIR / "packaged_app_lifecycle_smoke.py",
    )
    return _load_script(
        "packaged_customization_upgrade_smoke",
        SCRIPTS_DIR / "packaged_customization_upgrade_smoke.py",
    )


def test_json_bytes_excludes_mutable_boot_state(tmp_path: Path) -> None:
    module = _load_module()
    root = tmp_path / "customization"
    root.mkdir()
    state = root / "state.json"
    state.write_text("{}", encoding="utf-8")
    profile = root / "profiles" / "profile.json"
    profile.parent.mkdir()
    profile.write_text('{"revision":3}', encoding="utf-8")

    snapshot = module._json_bytes(root, excluding={state})

    assert snapshot == {"profiles/profile.json": b'{"revision":3}'}


def test_wait_for_boot_acceptance_requires_lkg_promotion(monkeypatch) -> None:
    module = _load_module()
    states = iter(
        [
            {
                "pending_boot_validation": {module.PROFILE_ID: 3},
                "last_known_good": {module.PROFILE_ID: 2},
            },
            {
                "pending_boot_validation": {},
                "last_known_good": {module.PROFILE_ID: 3},
            },
        ]
    )
    monkeypatch.setattr(module, "_get_json", lambda *_args, **_kwargs: next(states))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    accepted = module._wait_for_boot_acceptance({}, timeout=1)

    assert accepted["last_known_good"][module.PROFILE_ID] == 3


def test_generated_legacy_migration_marker_is_allowed_when_inert(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = tmp_path / "customization"
    root.mkdir()
    (root / "legacy-migration.json").write_text(
        json.dumps(
            {
                "completed": True,
                "profile_id": None,
                "settings_source": "/isolated/working.secret/settings.json",
                "legacy_css_archive": None,
            }
        ),
        encoding="utf-8",
    )

    module._assert_generated_metadata(root, ["legacy-migration.json"])


def test_generated_legacy_migration_marker_rejects_profile_replacement(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = tmp_path / "customization"
    root.mkdir()
    (root / "legacy-migration.json").write_text(
        json.dumps(
            {
                "completed": True,
                "profile_id": "unexpected-migrated-profile",
                "legacy_css_archive": None,
            }
        ),
        encoding="utf-8",
    )

    try:
        module._assert_generated_metadata(root, ["legacy-migration.json"])
    except AssertionError as exc:
        assert "Unsafe legacy migration marker" in str(exc)
    else:
        raise AssertionError("unsafe generated migration marker was accepted")
