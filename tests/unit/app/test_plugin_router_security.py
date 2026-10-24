# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.routers import plugins as plugin_router
from lantai.plugins.loader import compute_plugin_source_sha256


def _write_frontend_plugin(root: Path, plugin_id: str = "ui-plugin") -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "1.0.0",
                "entry": {"frontend": "ui.js"},
            },
        ),
        encoding="utf-8",
    )
    (plugin_dir / "ui.js").write_text("export default {};\n", encoding="utf-8")
    return plugin_dir


def test_disk_fallback_marks_disabled_plugins_without_frontend_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugins_root = tmp_path / "plugins"
    plugin_dir = _write_frontend_plugin(plugins_root)
    config = SimpleNamespace(
        plugins={
            "ui-plugin": {
                "enabled": False,
                "source_sha256": compute_plugin_source_sha256(plugin_dir),
            },
        },
    )

    import lantai.config.utils as config_utils

    monkeypatch.setattr(
        plugin_router,
        "_plugin_network_automatic_allowed",
        lambda: True,
    )
    monkeypatch.setattr(config_utils, "get_plugins_dir", lambda: plugins_root)
    monkeypatch.setattr(config_utils, "load_config", lambda *_a, **_k: config)

    assert plugin_router._list_plugins_from_disk() == [
        {
            "id": "ui-plugin",
            "name": "ui-plugin",
            "version": "1.0.0",
            "description": "",
            "enabled": False,
            "frontend_entry": None,
            "permissions": [],
        },
    ]


@pytest.mark.asyncio
async def test_disk_fallback_refuses_untrusted_plugin_static_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugins_root = tmp_path / "plugins"
    _write_frontend_plugin(plugins_root)
    config = SimpleNamespace(plugins={"ui-plugin": {"enabled": True}})

    import lantai.config.utils as config_utils

    monkeypatch.setattr(
        plugin_router,
        "_plugin_network_automatic_allowed",
        lambda: True,
    )
    monkeypatch.setattr(config_utils, "get_plugins_dir", lambda: plugins_root)
    monkeypatch.setattr(config_utils, "load_config", lambda *_a, **_k: config)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(plugin_loader=None)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await plugin_router.serve_plugin_ui_file(
            "ui-plugin",
            "ui.js",
            request,
        )

    assert exc_info.value.status_code == 404


def test_disk_fallback_uses_capability_matrix_to_hide_plugins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugins_root = tmp_path / "plugins"
    _write_frontend_plugin(plugins_root)

    import lantai.config.utils as config_utils

    monkeypatch.setattr(
        plugin_router,
        "_plugin_network_automatic_allowed",
        lambda: False,
    )
    monkeypatch.setattr(config_utils, "get_plugins_dir", lambda: plugins_root)

    assert plugin_router._list_plugins_from_disk() == []
