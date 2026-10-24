from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    script = ROOT / "scripts" / "packaged_skill_import_probe.py"
    spec = importlib.util.spec_from_file_location(
        "packaged_skill_import_probe",
        script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_enumerates_every_python_skill_script_and_reports_import_failures(
    tmp_path: Path,
) -> None:
    module = _load_module()
    scripts = tmp_path / "demo" / "scripts"
    helpers = scripts / "helpers"
    helpers.mkdir(parents=True)
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    (helpers / "__init__.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
    (scripts / "local_import.py").write_text(
        "from helpers import VALUE\n",
        encoding="utf-8",
    )
    (scripts / "missing_import.py").write_text(
        "import definitely_missing_lantai_probe_dependency\n",
        encoding="utf-8",
    )

    report = module.probe_skill_scripts(tmp_path)

    assert report["script_count"] == 4
    assert report["passed_count"] == 3
    assert report["failed_count"] == 1
    assert report["failures"] == [
        {
            "script": "demo/scripts/missing_import.py",
            "module": "definitely_missing_lantai_probe_dependency",
            "error_type": "ModuleNotFoundError",
            "missing_module": "definitely_missing_lantai_probe_dependency",
            "message": ("No module named 'definitely_missing_lantai_probe_dependency'"),
        }
    ]
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("*.pyc"))


def test_release_smoke_executes_the_exhaustive_import_probe() -> None:
    smoke = (ROOT / "scripts" / "packaged_skill_runtime_smoke.py").read_text(
        encoding="utf-8",
    )

    assert "packaged_skill_import_probe.py" in smoke
    assert '"skill_import_probe"' in smoke
