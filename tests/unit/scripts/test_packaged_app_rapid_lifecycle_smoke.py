from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
LIFECYCLE_SCRIPT = SCRIPTS_DIR / "packaged_app_lifecycle_smoke.py"
RAPID_SCRIPT = SCRIPTS_DIR / "packaged_app_rapid_lifecycle_smoke.py"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_module():
    _load_script("packaged_app_lifecycle_smoke", LIFECYCLE_SCRIPT)
    return _load_script("packaged_app_rapid_lifecycle_smoke", RAPID_SCRIPT)


def test_descendant_pids_walks_the_entire_process_tree() -> None:
    module = _load_module()
    snapshot = {
        10: {"ppid": 1, "command": "app"},
        11: {"ppid": 10, "command": "backend"},
        12: {"ppid": 10, "command": "renderer"},
        13: {"ppid": 11, "command": "worker"},
        99: {"ppid": 1, "command": "unrelated"},
    }

    assert module._descendant_pids(snapshot, 10) == {11, 12, 13}


def test_backend_process_pids_only_matches_the_packaged_backend(tmp_path: Path) -> None:
    module = _load_module()
    backend = (
        tmp_path
        / "澜台.app/Contents/Resources/lantai-server/Lantai Backend.app/Contents/MacOS/lantai-server"
    )
    snapshot = {
        10: {"ppid": 1, "command": str(backend)},
        11: {"ppid": 1, "command": f"{backend} app --host 127.0.0.1"},
        12: {"ppid": 10, "command": f"{backend} guanlan-mcp"},
        13: {"ppid": 1, "command": f"{backend} --duplicate"},
        14: {"ppid": 1, "command": "/other/lantai-server app"},
    }

    assert module._backend_process_pids(snapshot, backend) == {10, 11}


def test_clean_process_baseline_rejects_candidate_orphans(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_matching_processes",
        lambda *markers: {44: "/candidate/lantai-server guanlan-mcp"},
    )

    try:
        module._assert_clean_process_baseline("/candidate/lantai-server")
    except AssertionError as exc:
        assert "candidate processes" in str(exc)
        assert "guanlan-mcp" in str(exc)
    else:
        raise AssertionError("dirty candidate process baseline was accepted")


def test_wait_for_process_cleanup_reports_new_marker_matched_orphans(
    monkeypatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_process_snapshot",
        lambda: {44: {"ppid": 1, "command": "/App --user-data=/tmp/lantai"}},
    )
    monkeypatch.setattr(
        module,
        "_matching_processes",
        lambda *markers: {44: "/App --user-data=/tmp/lantai"},
    )

    cleanup = module._wait_for_process_cleanup(
        tracked_pids=set(),
        markers=("/tmp/lantai",),
        timeout=0.01,
    )

    assert cleanup["tracked_alive"] == {}
    assert cleanup["matching_alive"] == {
        "44": "/App --user-data=/tmp/lantai"
    }


def test_wait_for_process_cleanup_accepts_a_clean_snapshot(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_process_snapshot", lambda: {})
    monkeypatch.setattr(module, "_matching_processes", lambda *markers: {})

    cleanup = module._wait_for_process_cleanup(
        tracked_pids={10, 11},
        markers=("/tmp/lantai",),
        timeout=0.1,
    )

    assert cleanup == {"tracked_alive": {}, "matching_alive": {}}
