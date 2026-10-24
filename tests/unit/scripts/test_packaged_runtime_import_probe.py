from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    script = ROOT / "scripts" / "packaged_runtime_import_probe.py"
    spec = importlib.util.spec_from_file_location(
        "packaged_runtime_import_probe",
        script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discovers_literal_lazy_cli_and_builtin_channel_imports(
    tmp_path: Path,
) -> None:
    module = _load_module()
    source_root = tmp_path / "lantai"
    cli = source_root / "cli" / "main.py"
    channels = source_root / "app" / "channels" / "registry.py"
    feature = source_root / "feature.py"
    cli.parent.mkdir(parents=True)
    channels.parent.mkdir(parents=True)
    cli.write_text(
        """@click.group(lazy_subcommands={
    "doctor": ("lantai.cli.doctor_cmd", "doctor_cmd", ".doctor_cmd"),
})
def cli():
    pass
""",
        encoding="utf-8",
    )
    channels.write_text(
        """_BUILTIN_SPECS = {
    "console": (".console", "ConsoleChannel"),
}
""",
        encoding="utf-8",
    )
    feature.write_text(
        'importlib.import_module("modelscope.hub.api")\n',
        encoding="utf-8",
    )

    discovered = module.discover_runtime_imports(source_root)

    assert discovered == {
        "lantai.app.channels.console": {
            "lantai/app/channels/registry.py:built-in-channel",
        },
        "lantai.cli.doctor_cmd": {"lantai/cli/main.py:lazy-cli"},
        "modelscope.hub.api": {"lantai/feature.py:literal-dynamic-import"},
    }
    assert module.discover_lazy_cli_commands(source_root) == {
        "doctor": "lantai.cli.doctor_cmd",
    }


def test_probe_reports_missing_discovered_module(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    source_root = tmp_path / "lantai"
    source_root.mkdir()
    (source_root / "feature.py").write_text(
        'importlib.import_module("missing.runtime.module")\n',
        encoding="utf-8",
    )

    def import_module(name: str):
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(module.importlib, "import_module", import_module)
    report = module.probe_runtime_imports(source_root)

    assert report["module_count"] == 1
    assert report["failed_count"] == 1
    assert report["failures"][0]["module"] == "missing.runtime.module"
    assert report["failures"][0]["missing_module"] == "missing.runtime.module"


def test_source_probe_discovers_the_acp_server_command() -> None:
    module = _load_module()

    commands = module.discover_lazy_cli_commands(ROOT / "src" / "lantai")
    imports = module.discover_runtime_imports(ROOT / "src" / "lantai")

    assert commands["acp"] == "lantai.cli.acp_server_cmd"
    assert "lantai.cli.acp_server_cmd" in imports


def test_release_smoke_executes_the_runtime_import_probe() -> None:
    smoke = (ROOT / "scripts" / "packaged_skill_runtime_smoke.py").read_text(
        encoding="utf-8",
    )

    assert "packaged_runtime_import_probe.py" in smoke
    assert '"runtime_import_probe"' in smoke
    assert '"cli_help_count"' in smoke
