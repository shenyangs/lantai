# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lantai.plugins import loader as loader_module
from lantai.plugins.loader import PluginLoader
from lantai.plugins.api import PluginApi
from lantai.plugins.registry import PluginRegistry


def _write_manifest(
    plugin_dir: Path,
    *,
    plugin_id: str,
    backend: str = "plugin.py",
    dependencies: list[str] | None = None,
    min_version: str = "0.1.0",
    permissions: list[str] | None = None,
    include_permissions: bool = True,
) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "1.0.0",
                "entry": {"backend": backend},
                "dependencies": dependencies or [],
                "min_version": min_version,
                **(
                    {
                        "permissions": permissions
                        or [
                            "runtime:provider",
                            "runtime:lifecycle",
                            "runtime:command",
                        ]
                    }
                    if include_permissions
                    else {}
                ),
            },
        ),
        encoding="utf-8",
    )


def _write_plugin(
    root: Path,
    *,
    plugin_id: str = "demo-plugin",
    backend_body: str | None = None,
    backend: str = "plugin.py",
    dependencies: list[str] | None = None,
    min_version: str = "0.1.0",
    permissions: list[str] | None = None,
    include_permissions: bool = True,
) -> Path:
    plugin_dir = root / plugin_id
    _write_manifest(
        plugin_dir,
        plugin_id=plugin_id,
        backend=backend,
        dependencies=dependencies,
        min_version=min_version,
        permissions=permissions,
        include_permissions=include_permissions,
    )
    if backend == "plugin.py":
        (plugin_dir / "plugin.py").write_text(
            backend_body
            or """
class DemoPlugin:
    def register(self, api):
        api.register_provider("demo-provider", object, label="Demo")

plugin = DemoPlugin()
""".lstrip(),
            encoding="utf-8",
        )
    return plugin_dir


def _trusted_config(plugin_dir: Path, *, enabled: bool = True) -> dict[str, object]:
    return {
        "enabled": enabled,
        "source_sha256": loader_module.compute_plugin_source_sha256(plugin_dir),
    }


@pytest.fixture(autouse=True)
def _reset_plugin_registry(monkeypatch: pytest.MonkeyPatch):
    PluginRegistry._instance = None
    monkeypatch.delenv("LANTAI_APP_STORE", raising=False)
    monkeypatch.delenv("LANTAI_RELEASE_CHANNEL", raising=False)
    monkeypatch.delenv("LANTAI_DISTRIBUTION", raising=False)
    yield
    PluginRegistry._instance = None


@pytest.mark.asyncio
async def test_disabled_plugin_never_imports_backend_module(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    plugin_dir = _write_plugin(
        tmp_path / "plugins",
        backend_body=f"""
from pathlib import Path
Path({str(marker)!r}).write_text("executed", encoding="utf-8")

class DemoPlugin:
    def register(self, api):
        api.register_provider("demo-provider", object, label="Demo")

plugin = DemoPlugin()
""".lstrip(),
    )

    loader = PluginLoader([plugin_dir.parent], allow_external_plugins=True)
    records = await loader.load_all_plugins(
        configs={"demo-plugin": _trusted_config(plugin_dir, enabled=False)},
    )

    assert not marker.exists()
    assert records["demo-plugin"].enabled is False
    assert loader.registry.get_all_providers() == {}
    assert "plugin_demo_plugin" not in sys.modules


@pytest.mark.asyncio
async def test_backend_entry_cannot_escape_plugin_root_with_parent_path(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "escaped.txt"
    outside = tmp_path / "outside.py"
    outside.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    plugin_dir = _write_plugin(
        tmp_path / "plugins",
        plugin_id="escape-plugin",
        backend="../outside.py",
    )

    loader = PluginLoader([plugin_dir.parent], allow_external_plugins=True)
    records = await loader.load_all_plugins(
        configs={"escape-plugin": _trusted_config(plugin_dir)},
    )

    assert records == {}
    assert not marker.exists()
    assert "plugin_escape_plugin" not in sys.modules


@pytest.mark.asyncio
async def test_backend_entry_symlink_escape_is_rejected_before_import(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "symlink-executed.txt"
    outside = tmp_path / "outside.py"
    outside.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    plugin_dir = _write_plugin(tmp_path / "plugins", plugin_id="symlink-plugin")
    (plugin_dir / "plugin.py").unlink()
    (plugin_dir / "plugin.py").symlink_to(outside)

    loader = PluginLoader([plugin_dir.parent], allow_external_plugins=True)
    records = await loader.load_all_plugins(
        configs={"symlink-plugin": _trusted_config(plugin_dir)},
    )

    assert records == {}
    assert not marker.exists()
    assert "plugin_symlink_plugin" not in sys.modules


@pytest.mark.asyncio
async def test_min_version_gate_runs_before_import(tmp_path: Path) -> None:
    marker = tmp_path / "min-version-executed.txt"
    plugin_dir = _write_plugin(
        tmp_path / "plugins",
        plugin_id="future-plugin",
        min_version="999.0.0",
        backend_body=f"""
from pathlib import Path
Path({str(marker)!r}).write_text("executed", encoding="utf-8")
plugin = object()
""".lstrip(),
    )

    loader = PluginLoader([plugin_dir.parent], allow_external_plugins=True)
    records = await loader.load_all_plugins(
        configs={"future-plugin": _trusted_config(plugin_dir)},
    )

    assert records == {}
    assert not marker.exists()
    assert "plugin_future_plugin" not in sys.modules


@pytest.mark.asyncio
async def test_missing_python_dependency_gate_runs_before_import(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "dependency-executed.txt"
    plugin_dir = _write_plugin(
        tmp_path / "plugins",
        plugin_id="dependency-plugin",
        dependencies=["definitely-missing-lantai-test-package>=1.0.0"],
        backend_body=f"""
from pathlib import Path
Path({str(marker)!r}).write_text("executed", encoding="utf-8")
plugin = object()
""".lstrip(),
    )

    loader = PluginLoader([plugin_dir.parent], allow_external_plugins=True)
    records = await loader.load_all_plugins(
        configs={"dependency-plugin": _trusted_config(plugin_dir)},
    )

    assert records == {}
    assert not marker.exists()
    assert "plugin_dependency_plugin" not in sys.modules


@pytest.mark.asyncio
async def test_plugin_dependency_cycle_is_rejected_before_import(
    tmp_path: Path,
) -> None:
    marker_a = tmp_path / "a.txt"
    marker_b = tmp_path / "b.txt"
    plugin_a = _write_plugin(
        tmp_path / "plugins",
        plugin_id="plugin-a",
        dependencies=["plugin:plugin-b"],
        backend_body=f"""
from pathlib import Path
Path({str(marker_a)!r}).write_text("executed", encoding="utf-8")
plugin = object()
""".lstrip(),
    )
    plugin_b = _write_plugin(
        tmp_path / "plugins",
        plugin_id="plugin-b",
        dependencies=["plugin:plugin-a"],
        backend_body=f"""
from pathlib import Path
Path({str(marker_b)!r}).write_text("executed", encoding="utf-8")
plugin = object()
""".lstrip(),
    )

    loader = PluginLoader([plugin_a.parent], allow_external_plugins=True)
    records = await loader.load_all_plugins(
        configs={
            "plugin-a": _trusted_config(plugin_a),
            "plugin-b": _trusted_config(plugin_b),
        },
    )

    assert records == {}
    assert not marker_a.exists()
    assert not marker_b.exists()
    assert "plugin_plugin_a" not in sys.modules
    assert "plugin_plugin_b" not in sys.modules


@pytest.mark.asyncio
async def test_failed_register_rolls_back_registry_and_sys_modules(
    tmp_path: Path,
) -> None:
    plugin_dir = _write_plugin(
        tmp_path / "plugins",
        plugin_id="rollback-plugin",
        backend_body="""
class Handler:
    command_name = "broken"

class RollbackPlugin:
    def register(self, api):
        api.register_provider("rollback-provider", object, label="Rollback")
        api.register_startup_hook("rollback-startup", lambda: None)
        api.register_shutdown_hook("rollback-shutdown", lambda: None)
        api.register_control_command(Handler())
        raise RuntimeError("registration failed")

plugin = RollbackPlugin()
""".lstrip(),
    )

    loader = PluginLoader([plugin_dir.parent], allow_external_plugins=True)
    manifest, source_path = loader.discover_plugins()[0]

    with pytest.raises(RuntimeError, match="registration failed"):
        await loader.load_plugin(manifest, source_path, _trusted_config(plugin_dir))

    assert loader.registry.get_all_providers() == {}
    assert loader.registry.get_startup_hooks() == []
    assert loader.registry.get_shutdown_hooks() == []
    assert loader.registry.get_control_commands() == []
    assert loader.registry.get_plugin_manifest("rollback-plugin") is None
    assert "plugin_rollback_plugin" not in sys.modules


@pytest.mark.asyncio
async def test_duplicate_plugin_ids_are_rejected_before_import(
    tmp_path: Path,
) -> None:
    marker_a = tmp_path / "a.txt"
    marker_b = tmp_path / "b.txt"
    plugins_root = tmp_path / "plugins"
    first = plugins_root / "first"
    second = plugins_root / "second"
    _write_manifest(first, plugin_id="duplicate-plugin")
    (first / "plugin.py").write_text(
        f"""
from pathlib import Path
Path({str(marker_a)!r}).write_text("executed", encoding="utf-8")
plugin = object()
""".lstrip(),
        encoding="utf-8",
    )
    _write_manifest(second, plugin_id="duplicate-plugin")
    (second / "plugin.py").write_text(
        f"""
from pathlib import Path
Path({str(marker_b)!r}).write_text("executed", encoding="utf-8")
plugin = object()
""".lstrip(),
        encoding="utf-8",
    )

    loader = PluginLoader([plugins_root], allow_external_plugins=True)
    records = await loader.load_all_plugins(
        configs={"duplicate-plugin": _trusted_config(first)},
    )

    assert records == {}
    assert not marker_a.exists()
    assert not marker_b.exists()
    assert "plugin_duplicate_plugin" not in sys.modules
    assert second.exists()


@pytest.mark.asyncio
async def test_app_store_distribution_rejects_external_plugin_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LANTAI_APP_STORE", "1")
    marker = tmp_path / "mas-executed.txt"
    plugin_dir = _write_plugin(
        tmp_path / "plugins",
        plugin_id="mas-plugin",
        backend_body=f"""
from pathlib import Path
Path({str(marker)!r}).write_text("executed", encoding="utf-8")
plugin = object()
""".lstrip(),
    )

    loader = PluginLoader([plugin_dir.parent])
    records = await loader.load_all_plugins(
        configs={"mas-plugin": _trusted_config(plugin_dir)},
    )

    assert records == {}
    assert not marker.exists()
    assert "plugin_mas_plugin" not in sys.modules


@pytest.mark.asyncio
async def test_external_plugin_without_permission_declaration_never_imports(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "permission-executed.txt"
    plugin_dir = _write_plugin(
        tmp_path / "plugins",
        plugin_id="permissionless-plugin",
        include_permissions=False,
        backend_body=f"""
from pathlib import Path
Path({str(marker)!r}).write_text("executed", encoding="utf-8")
plugin = object()
""".lstrip(),
    )

    loader = PluginLoader([plugin_dir.parent], allow_external_plugins=True)
    records = await loader.load_all_plugins(
        configs={"permissionless-plugin": _trusted_config(plugin_dir)},
    )

    assert records == {}
    assert not marker.exists()
    assert "plugin_permissionless_plugin" not in sys.modules


def test_plugin_api_denies_undeclared_registration_capability() -> None:
    registry = PluginRegistry()
    api = PluginApi(
        "read-only-plugin",
        {},
        {"permissions": ["config:read"]},
    )
    api.set_registry(registry)

    with pytest.raises(PermissionError, match="runtime:provider"):
        api.register_provider("forbidden-provider", object)

    assert registry.get_all_providers() == {}


@pytest.mark.asyncio
async def test_unload_plugin_cleans_every_registration_and_module(
    tmp_path: Path,
) -> None:
    plugin_dir = _write_plugin(
        tmp_path / "plugins",
        plugin_id="cleanup-plugin",
        backend_body="""
class Handler:
    command_name = "cleanup"

class CleanupPlugin:
    def register(self, api):
        api.register_provider("cleanup-provider", object, label="Cleanup")
        api.register_startup_hook("cleanup-start", lambda: None)
        api.register_shutdown_hook("cleanup-stop", lambda: None)
        api.register_control_command(Handler())

plugin = CleanupPlugin()
""".lstrip(),
    )
    loader = PluginLoader([plugin_dir.parent], allow_external_plugins=True)
    manifest, source_path = loader.discover_plugins()[0]
    await loader.load_plugin(manifest, source_path, _trusted_config(plugin_dir))

    assert "plugin_cleanup_plugin" in sys.modules
    assert loader.registry.get_provider("cleanup-provider") is not None

    assert await loader.unload_plugin("cleanup-plugin") is True
    assert loader.registry.get_provider("cleanup-provider") is None
    assert loader.registry.get_startup_hooks() == []
    assert loader.registry.get_shutdown_hooks() == []
    assert loader.registry.get_control_commands() == []
    assert loader.registry.get_plugin_manifest("cleanup-plugin") is None
    assert "plugin_cleanup_plugin" not in sys.modules
