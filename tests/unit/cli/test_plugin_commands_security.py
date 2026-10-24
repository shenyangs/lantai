# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from lantai.cli import plugin_commands
from lantai.cli.main import cli
from lantai.plugins import loader as loader_module


def test_plugin_install_does_not_import_backend_and_records_source_hash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    plugins_dir = tmp_path / "installed-plugins"
    source_dir = tmp_path / "source-plugin"
    source_dir.mkdir()
    marker = tmp_path / "executed.txt"
    (source_dir / "plugin.json").write_text(
        json.dumps(
            {
                "id": "demo-plugin",
                "name": "Demo Plugin",
                "version": "1.0.0",
                "entry": {"backend": "plugin.py"},
            },
        ),
        encoding="utf-8",
    )
    (source_dir / "plugin.py").write_text(
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("executed", encoding="utf-8")

class DemoPlugin:
    def register(self, api):
        pass

plugin = DemoPlugin()
""".lstrip(),
        encoding="utf-8",
    )

    config = SimpleNamespace(plugins={}, last_api=SimpleNamespace(host=None, port=None))
    saved_configs: list[SimpleNamespace] = []

    import lantai.config.utils as config_utils

    monkeypatch.setattr(plugin_commands, "_check_lantai_not_running", lambda: None)
    monkeypatch.setattr(plugin_commands, "_sync_tool_plugin_to_agents", lambda _m: None)
    monkeypatch.setattr(config_utils, "get_plugins_dir", lambda: plugins_dir)
    monkeypatch.setattr(config_utils, "load_config", lambda *_a, **_k: config)
    monkeypatch.setattr(
        config_utils,
        "save_config",
        lambda updated, *_a, **_k: saved_configs.append(updated),
    )

    result = CliRunner().invoke(cli, ["plugin", "install", str(source_dir)])

    assert result.exit_code == 0, result.output
    assert not marker.exists()
    installed_dir = plugins_dir / "demo-plugin"
    assert installed_dir.exists()
    assert saved_configs == [config]
    assert config.plugins["demo-plugin"]["enabled"] is True
    assert config.plugins["demo-plugin"]["source_sha256"] == (
        loader_module.compute_plugin_source_sha256(installed_dir)
    )
