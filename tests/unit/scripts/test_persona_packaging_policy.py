# -*- coding: utf-8 -*-
"""Release packaging locks for Shen Xingzhi Persona Canon V2."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_frozen_backend_and_wheel_include_all_persona_assets() -> None:
    spec = (ROOT / "lantai-server.spec").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "('src/lantai/agents/md_files', 'lantai/agents/md_files')" in spec
    assert '"agents/md_files/**"' in pyproject


def test_v21_persona_assets_are_bilingual_versioned_and_complete() -> None:
    md_root = ROOT / "src" / "lantai" / "agents" / "md_files"
    required = {
        "AGENTS.md",
        "SOUL.md",
        "PROFILE.md",
        "BACKSTORY.md",
        "SELF_CANON.md",
        "WORKBENCH.md",
        "VISUAL_CANON.md",
    }

    for language in ("zh", "en"):
        language_dir = md_root / language
        assert required <= {path.name for path in language_dir.glob("*.md")}
        for filename in required:
            content = (language_dir / filename).read_text(encoding="utf-8")
            assert 'persona_version: "2.1.0"' in content


def test_constitution_and_packaged_benchmarks_share_release_version() -> None:
    md_root = ROOT / "src" / "lantai" / "agents" / "md_files"
    constitutions = md_root / "constitutions"
    expected = "1.6.1"

    for language in ("zh", "en"):
        constitution = (
            constitutions / f"shen_xingzhi-{language}.yaml"
        ).read_text(
            encoding="utf-8",
        )
        scenarios = (
            constitutions / f"shen_xingzhi-scenarios-{language}.yaml"
        ).read_text(encoding="utf-8")
        assert f"version: {expected}" in constitution
        assert f"constitution_version: {expected}" in scenarios

    for script_name in (
        "run_packaged_persona_benchmark.py",
        "review_packaged_persona_evidence.py",
        "check_packaged_persona_release_evidence.py",
    ):
        script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert expected in script
