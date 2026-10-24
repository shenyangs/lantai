from __future__ import annotations

import importlib.util
import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    script = ROOT / "scripts" / "packaged_skill_runtime_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "packaged_skill_runtime_smoke", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_skill_runner_uses_pyinstaller_bundle_resources(
    tmp_path: Path,
) -> None:
    module = _load_module()
    backend_app = tmp_path / "Lantai Backend.app"

    assert module._packaged_skill_runner(
        backend_app,
        "pptx-zh",
        "run_presentation_v3.py",
    ) == (
        backend_app
        / "Contents"
        / "Resources"
        / "lantai"
        / "agents"
        / "skills"
        / "pptx-zh"
        / "scripts"
        / "run_presentation_v3.py"
    )


def test_frozen_backend_collects_the_stable_presentation_runtime() -> None:
    spec = ast.unparse(ast.parse((ROOT / "lantai-server.spec").read_text(encoding="utf-8")))

    assert "collect_submodules('lantai.presentation')" in spec
    assert "discover_runtime_imports" in spec
    assert "discover_skill_imports" in spec
    assert "'pypdfium2'" in spec
    assert "add_skill_data_files" in spec
    assert "'__pycache__' in relative.parts" in spec
    assert "path.suffix.lower() == '.pyc'" in spec
    assert "('src/lantai/agents/skills', 'lantai/agents/skills')" not in spec


def test_packaged_smoke_runs_only_the_v3_stable_presentation_entrypoint() -> None:
    smoke = (ROOT / "scripts" / "packaged_skill_runtime_smoke.py").read_text(
        encoding="utf-8",
    )

    stable = smoke.index("stable_presentations:")
    assert stable >= 0
    assert "for locale, language, user_request, fixture_material in (" in smoke
    assert '"pptx-zh"' in smoke
    assert '"pptx-en"' in smoke
    assert '"stable_presentations"' in smoke
    assert 'for locale in ("pdf-zh", "pdf-en")' in smoke
    assert '"pdf_renderers"' in smoke
    assert '"run_presentation_v3.py"' in smoke
    assert '"generate.py"' not in smoke
    assert '"render_pages.py"' in smoke
    assert '"--path"' in smoke
    assert '"--output-dir"' in smoke
    assert 'render_dir / "page-001.png"' in smoke
    assert '"ELECTRON_RUN_AS_NODE": "1"' in smoke
    assert '"LANTAI_NODE_BIN": str(electron)' in smoke
    assert '"LANTAI_NODE_MODULES": str(modules)' in smoke
    assert '"planning_mode": "deterministic_fixture"' in smoke
    assert '"visual_verification_required": True' in smoke
    assert '"vision_critique_required": False' in smoke
    assert '"content": fixture_material' in smoke
    assert '"media_type": "text/markdown"' in smoke
    assert "render_pptxgenjs.py" not in smoke
    assert "convert_pdf_to_images.py" not in smoke
    assert 'source = workspace / "deck.cjs"' not in smoke


def test_skill_resource_parity_detects_missing_and_mismatched_files(
    tmp_path: Path,
) -> None:
    module = _load_module()
    source = tmp_path / "source"
    packaged = tmp_path / "packaged"
    (source / "demo" / "scripts").mkdir(parents=True)
    (packaged / "demo" / "scripts").mkdir(parents=True)
    (source / "demo" / "SKILL.md").write_text("source", encoding="utf-8")
    (packaged / "demo" / "SKILL.md").write_text("stale", encoding="utf-8")
    (source / "demo" / "scripts" / "run.py").write_text(
        "print('ok')\n",
        encoding="utf-8",
    )
    packaged_cache = packaged / "demo" / "scripts" / "__pycache__"
    packaged_cache.mkdir()
    (packaged_cache / "run.cpython-310.pyc").write_bytes(b"stale-bytecode")

    report = module._compare_skill_resources(source, packaged)

    assert report["status"] == "failed"
    assert report["missing"] == ["demo/scripts/run.py"]
    assert report["mismatched"] == ["demo/SKILL.md"]
    assert report["unexpected"] == ["demo/scripts/__pycache__/run.cpython-310.pyc"]


def test_packaged_probes_remove_host_dependency_injection(
    monkeypatch,
) -> None:
    module = _load_module()
    for key in module._HOST_ENV_KEYS:
        monkeypatch.setenv(key, f"host-{key}")

    env = module._sanitized_env()

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert all(key not in env for key in module._HOST_ENV_KEYS)


def test_isolated_probe_cannot_read_the_users_runtime_or_secret_roots(
    monkeypatch,
) -> None:
    module = _load_module()
    monkeypatch.setenv("LANTAI_WORKING_DIR", "/host/private/working")
    monkeypatch.setenv("LANTAI_SECRET_DIR", "/host/private/working.secret")
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env, **kwargs):
        captured.update({"command": command, "cwd": cwd, "env": env})
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module._run_isolated(
        ["packaged-backend", "--probe"],
        env=module._sanitized_env(),
        timeout=10,
    )

    probe_cwd = Path(captured["cwd"])
    probe_env = captured["env"]
    assert probe_env["LANTAI_WORKING_DIR"] == str(probe_cwd / "working")
    assert probe_env["LANTAI_SECRET_DIR"] == str(probe_cwd / "working.secret")
    assert probe_env["LANTAI_DESKTOP_USER_DATA_DIR"] == str(
        probe_cwd / "desktop-data"
    )
    assert probe_env["LANTAI_DISABLE_KEYRING"] == "1"


def test_runtime_import_probe_uses_the_bounded_release_timeout() -> None:
    smoke = (ROOT / "scripts" / "packaged_skill_runtime_smoke.py").read_text(
        encoding="utf-8",
    )

    assert "per_import_timeout = max(10, min(60, probe_timeout - 5))" in smoke
    assert smoke.count('str(per_import_timeout)') == 2
    assert '"per_import_timeout_seconds": per_import_timeout' in smoke


def test_release_smoke_executes_research_exports_in_the_packaged_backend() -> None:
    smoke = (ROOT / 'scripts/packaged_skill_runtime_smoke.py').read_text()
    assert 'packaged_research_export_probe.py' in smoke
    assert '"LANTAI_RUN_SKILL_SCRIPT_PATH": str(research_probe_script)' in smoke
    assert '"research_export_probe": research_report' in smoke
