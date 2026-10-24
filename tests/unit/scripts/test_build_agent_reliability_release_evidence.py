from __future__ import annotations

import hashlib
import importlib.util
import json
import plistlib
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "build_agent_reliability_release_evidence.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _app(tmp_path: Path, content: bytes = b"app") -> Path:
    app = tmp_path / "Lantai.app"
    executable = app / "Contents" / "MacOS" / "Lantai"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(content)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleExecutable": "Lantai"}, handle)
    return app


def _child_app(app: Path) -> dict:
    executable = app / "Contents" / "MacOS" / "Lantai"
    return {
        "path": str(app),
        "artifact_sha256": {
            "main_executable": f"sha256:{hashlib.sha256(executable.read_bytes()).hexdigest()}"
        },
    }


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sustained(module, path: Path, app: Path, run_id: str) -> Path:
    return _write(
        path,
        {
            "schema": module.SUSTAINED_SCHEMA,
            "passed": True,
            "app": _child_app(app),
            "light_records": [{"run_id": run_id}],
            "standard_records": [{"run_id": run_id}],
            "effect_records": [{"run_id": run_id}],
            "works_records": [{"run_id": run_id}],
            "ui_records": [{"run_id": run_id}],
            "long_run": {"run_id": run_id},
        },
    )


def _fault(module, path: Path, app: Path) -> Path:
    return _write(
        path,
        {
            "schema": module.FAULT_SCHEMA,
            "passed": True,
            "app": _child_app(app),
            "records": [{"scenario": "network_disconnect"}],
        },
    )


def test_assemble_preserves_detailed_child_records(tmp_path: Path) -> None:
    module = _module()
    app = _app(tmp_path)
    first = _sustained(module, tmp_path / "first.json", app, "first")
    second = _sustained(module, tmp_path / "second.json", app, "second")
    fault = _fault(module, tmp_path / "fault.json", app)

    result = module.assemble(
        app_path=app,
        sustained_paths=[first, second],
        fault_path=fault,
    )

    assert result["schema"] == module.OUTPUT_SCHEMA
    assert [item["run_id"] for item in result["long_runs"]["records"]] == [
        "first",
        "second",
    ]
    assert result["package"]["packaged_app_tested"] is True


def test_assemble_rejects_failed_or_different_package_child(tmp_path: Path) -> None:
    module = _module()
    app = _app(tmp_path / "official", b"official")
    other = _app(tmp_path / "other", b"other")
    first = _sustained(module, tmp_path / "first.json", app, "first")
    second = _sustained(module, tmp_path / "second.json", other, "second")
    fault = _fault(module, tmp_path / "fault.json", app)

    try:
        module.assemble(
            app_path=app,
            sustained_paths=[first, second],
            fault_path=fault,
        )
    except ValueError as exc:
        assert "different app" in str(exc)
    else:
        raise AssertionError("different package evidence must fail closed")
