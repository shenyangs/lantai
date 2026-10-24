# -*- coding: utf-8 -*-
"""Plugin loader for discovering and loading plugins."""

import importlib.util
from importlib import metadata
import hashlib
import inspect
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import logging

from .architecture import PluginManifest, PluginRecord
from .api import PluginApi
from .registry import PluginRegistry

logger = logging.getLogger(__name__)


class PluginLoadError(RuntimeError):
    """Base class for plugin load failures."""


class PluginSecurityError(PluginLoadError):
    """Raised when a plugin violates a load-time security gate."""


_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_REQUIREMENT_RE = re.compile(
    r"^\s*([A-Za-z0-9_.-]+)\s*(?:(==|!=|>=|<=|>|<)\s*([^;\s]+))?",
)


def plugin_config_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """Return whether a plugin config enables loading."""
    if not isinstance(config, dict):
        return True
    raw = config.get("enabled", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in _FALSE_VALUES
    return bool(raw)


def _version_parts(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in re.split(r"[^0-9]+", version):
        if segment:
            parts.append(int(segment))
    return tuple(parts or [0])


def _compare_versions(left: str, right: str) -> int:
    left_parts = list(_version_parts(left))
    right_parts = list(_version_parts(right))
    width = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (width - len(left_parts)))
    right_parts.extend([0] * (width - len(right_parts)))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def _version_satisfies(installed: str, operator: str, required: str) -> bool:
    comparison = _compare_versions(installed, required)
    if operator == "==":
        return comparison == 0
    if operator == "!=":
        return comparison != 0
    if operator == ">=":
        return comparison >= 0
    if operator == "<=":
        return comparison <= 0
    if operator == ">":
        return comparison > 0
    if operator == "<":
        return comparison < 0
    return True


def compute_plugin_source_sha256(source_path: Path) -> str:
    """Compute a deterministic SHA256 over a plugin source directory.

    Symlinks are hashed as links instead of following their targets so a
    trusted source hash cannot silently bless bytes outside the plugin root.
    """
    root = Path(source_path)
    if not root.is_dir():
        raise FileNotFoundError(f"Plugin source directory not found: {root}")

    digest = hashlib.sha256()
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current_root)
        dirnames[:] = sorted(dirnames)

        for dirname in list(dirnames):
            path = current_path / dirname
            if path.is_symlink():
                rel = path.relative_to(root).as_posix()
                digest.update(b"SYMLINK_DIR\0")
                digest.update(rel.encode("utf-8"))
                digest.update(b"\0")
                digest.update(os.readlink(path).encode("utf-8"))
                digest.update(b"\0")
                dirnames.remove(dirname)

        for filename in sorted(filenames):
            path = current_path / filename
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest.update(b"SYMLINK_FILE\0")
                digest.update(rel.encode("utf-8"))
                digest.update(b"\0")
                digest.update(os.readlink(path).encode("utf-8"))
                digest.update(b"\0")
                continue

            if not path.is_file():
                continue

            digest.update(b"FILE\0")
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def resolve_plugin_entry_path(
    source_path: Path,
    entry: str,
    *,
    kind: str,
) -> Path:
    """Resolve a plugin entry path and require it to stay in source_path."""
    source_root = Path(source_path).resolve()
    candidate = (Path(source_path) / entry).resolve(strict=False)
    if not candidate.is_relative_to(source_root):
        raise PluginSecurityError(
            f"{kind} entry escapes plugin root: {entry}",
        )
    return candidate


def _is_app_store_distribution() -> bool:
    try:
        from ..app.capabilities.policy import is_app_store_distribution

        return is_app_store_distribution()
    except Exception:
        raw = (
            os.getenv("LANTAI_RELEASE_CHANNEL")
            or os.getenv("LANTAI_DISTRIBUTION")
            or ""
        ).lower()
        return raw in {"app_store", "appstore", "mas"} or os.getenv(
            "LANTAI_APP_STORE",
            "",
        ).lower() in {"1", "true", "yes"}


def _purge_module_tree(module_name: str) -> None:
    for name in list(sys.modules):
        if name == module_name or name.startswith(f"{module_name}."):
            sys.modules.pop(name, None)


class PluginLoader:
    """Plugin loader for discovering and loading plugins."""

    RUNTIME_EVENT_DRAIN_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        plugin_dirs: List[Path],
        *,
        app_version: Optional[str] = None,
        allow_external_plugins: Optional[bool] = None,
    ):
        """Initialize plugin loader.

        Args:
            plugin_dirs: List of directories to search for plugins
        """
        self.plugin_dirs = [Path(d).expanduser() for d in plugin_dirs]
        self._plugin_roots = [
            path.resolve()
            for path in self.plugin_dirs
            if path.exists()
        ]
        project_root = Path(__file__).resolve().parents[3]
        self._compile_time_plugin_roots = [
            (project_root / "plugins").resolve(),
        ]
        if app_version is None:
            from ..__version__ import __version__

            app_version = __version__
        self.app_version = app_version
        self.allow_external_plugins = (
            not _is_app_store_distribution()
            if allow_external_plugins is None
            else allow_external_plugins
        )
        self.registry = PluginRegistry()
        self._loaded_plugins: Dict[str, PluginRecord] = {}

    def _is_under_roots(self, path: Path, roots: Iterable[Path]) -> bool:
        resolved = path.resolve()
        return any(resolved.is_relative_to(root) for root in roots)

    def _is_compile_time_source(self, source_path: Path) -> bool:
        return self._is_under_roots(
            source_path,
            self._compile_time_plugin_roots,
        )

    def _source_allowed(self, source_path: Path) -> bool:
        if not self._is_under_roots(source_path, self._plugin_roots):
            return False
        if self.allow_external_plugins:
            return True
        return self._is_compile_time_source(source_path)

    def _validate_source_allowed(self, source_path: Path) -> Path:
        resolved = Path(source_path).resolve()
        if not self._source_allowed(Path(source_path)):
            raise PluginSecurityError(
                f"Plugin source is not in an allowed root: {source_path}",
            )
        return resolved

    def _validate_source_integrity(
        self,
        manifest: PluginManifest,
        source_path: Path,
        config: Optional[Dict[str, Any]],
    ) -> None:
        if not isinstance(config, dict):
            config = {}

        expected_hash = str(
            config.get("source_sha256")
            or config.get("source_hash")
            or "",
        ).strip().lower()
        if not expected_hash and not self._is_compile_time_source(source_path):
            raise PluginSecurityError(
                f"Plugin '{manifest.id}' requires a pinned source_sha256",
            )

        if expected_hash:
            actual_hash = compute_plugin_source_sha256(source_path)
            if actual_hash.lower() != expected_hash:
                raise PluginSecurityError(
                    f"Plugin '{manifest.id}' source hash mismatch",
                )

    def _manifest_dict(self, manifest: PluginManifest) -> Dict[str, Any]:
        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "dependencies": manifest.dependencies,
            "permissions": manifest.permissions,
            "min_version": manifest.min_version,
            "meta": manifest.meta,
        }

    def _validate_permissions(
        self,
        manifest: PluginManifest,
        source_path: Path,
    ) -> None:
        if (
            not manifest.permissions_declared
            and not self._is_compile_time_source(source_path)
        ):
            raise PluginSecurityError(
                f"Plugin '{manifest.id}' must declare permissions",
            )
        if manifest.entry.frontend and not {
            "ui:route",
            "ui:tool-renderer",
        }.intersection(manifest.permissions):
            raise PluginSecurityError(
                f"Frontend plugin '{manifest.id}' requires a ui:* permission",
            )

    def _validate_min_version(self, manifest: PluginManifest) -> None:
        if _compare_versions(self.app_version, manifest.min_version) < 0:
            raise PluginLoadError(
                f"Plugin '{manifest.id}' requires Lantai "
                f"{manifest.min_version} or newer; current is "
                f"{self.app_version}",
            )

    def _plugin_dependency_id(
        self,
        dependency: str,
        known_plugin_ids: Set[str],
    ) -> Optional[str]:
        raw = dependency.strip()
        if raw.startswith("plugin:"):
            return raw[len("plugin:") :].strip()
        if raw in known_plugin_ids:
            return raw
        return None

    def _validate_python_dependencies(
        self,
        manifest: PluginManifest,
        known_plugin_ids: Set[str],
    ) -> None:
        for dependency in manifest.dependencies:
            if self._plugin_dependency_id(dependency, known_plugin_ids):
                continue

            match = _REQUIREMENT_RE.match(dependency)
            if not match:
                raise PluginLoadError(
                    f"Plugin '{manifest.id}' has invalid dependency "
                    f"declaration: {dependency}",
                )
            package_name, operator, required_version = match.groups()
            try:
                installed_version = metadata.version(package_name)
            except metadata.PackageNotFoundError as exc:
                raise PluginLoadError(
                    f"Plugin '{manifest.id}' dependency missing: "
                    f"{package_name}",
                ) from exc

            if operator and required_version and not _version_satisfies(
                installed_version,
                operator,
                required_version,
            ):
                raise PluginLoadError(
                    f"Plugin '{manifest.id}' dependency {dependency} "
                    f"is not satisfied by {installed_version}",
                )

    def _plugin_dependency_graph(
        self,
        candidates: Dict[str, Tuple[PluginManifest, Path, Dict[str, Any]]],
    ) -> Tuple[Dict[str, Set[str]], Set[str]]:
        known_ids = set(candidates)
        graph: Dict[str, Set[str]] = {}
        invalid: Set[str] = set()
        for plugin_id, (manifest, _source_path, _config) in candidates.items():
            graph[plugin_id] = set()
            for dependency in manifest.dependencies:
                dependency_id = self._plugin_dependency_id(
                    dependency,
                    known_ids,
                )
                if not dependency_id:
                    continue
                if dependency_id not in known_ids:
                    logger.error(
                        "Plugin '%s' dependency missing: %s",
                        plugin_id,
                        dependency_id,
                    )
                    invalid.add(plugin_id)
                    continue
                graph[plugin_id].add(dependency_id)
        return graph, invalid

    def _dependency_cycles(self, graph: Dict[str, Set[str]]) -> Set[str]:
        visiting: Set[str] = set()
        visited: Set[str] = set()
        cycle_nodes: Set[str] = set()

        def visit(plugin_id: str, stack: list[str]) -> None:
            if plugin_id in visited:
                return
            if plugin_id in visiting:
                if plugin_id in stack:
                    cycle_nodes.update(stack[stack.index(plugin_id) :])
                else:
                    cycle_nodes.add(plugin_id)
                return

            visiting.add(plugin_id)
            stack.append(plugin_id)
            for dependency_id in graph.get(plugin_id, set()):
                visit(dependency_id, stack)
            stack.pop()
            visiting.remove(plugin_id)
            visited.add(plugin_id)

        for plugin_id in graph:
            visit(plugin_id, [])
        return cycle_nodes

    def _topological_order(self, graph: Dict[str, Set[str]]) -> list[str]:
        ordered: list[str] = []
        visited: Set[str] = set()

        def visit(plugin_id: str) -> None:
            if plugin_id in visited:
                return
            visited.add(plugin_id)
            for dependency_id in sorted(graph.get(plugin_id, set())):
                visit(dependency_id)
            ordered.append(plugin_id)

        for plugin_id in sorted(graph):
            visit(plugin_id)
        return ordered

    def _disabled_record(
        self,
        manifest: PluginManifest,
        source_path: Path,
        reason: str,
    ) -> PluginRecord:
        record = PluginRecord(
            manifest=manifest,
            source_path=source_path,
            enabled=False,
            instance=None,
            diagnostics=[reason],
        )
        self._loaded_plugins[manifest.id] = record
        logger.info("Plugin '%s' disabled before import: %s", manifest.id, reason)
        return record

    def discover_plugins(self) -> List[Tuple[PluginManifest, Path]]:
        """Discover all plugins in plugin directories.

        Returns:
            List of (manifest, plugin_dir) tuples
        """
        discovered = []

        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.exists():
                logger.debug(f"Plugin directory not found: {plugin_dir}")
                continue

            logger.info(f"Scanning plugin directory: {plugin_dir}")

            for item in plugin_dir.iterdir():
                if not item.is_dir():
                    continue

                if not self._source_allowed(item):
                    logger.warning(
                        "Skipping plugin source outside allowed roots: %s",
                        item,
                    )
                    continue

                manifest_path = item / "plugin.json"
                if not manifest_path.exists():
                    continue

                try:
                    manifest = self._load_manifest(manifest_path)
                    discovered.append((manifest, item))
                    logger.info(f"Discovered plugin: {manifest.id}")
                except Exception as e:
                    logger.error(
                        f"Failed to load manifest from {manifest_path}: {e}",
                        exc_info=True,
                    )

        return discovered

    def _load_manifest(self, manifest_path: Path) -> PluginManifest:
        """Load plugin manifest from JSON file.

        Args:
            manifest_path: Path to plugin.json

        Returns:
            PluginManifest instance

        Raises:
            json.JSONDecodeError: If manifest is invalid JSON
            KeyError: If required fields are missing
        """
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return PluginManifest.from_dict(data)

    async def load_plugin(
        self,
        manifest: PluginManifest,
        source_path: Path,
        config: Optional[Dict] = None,
    ) -> PluginRecord:
        """Load a single plugin.

        Args:
            manifest: Plugin manifest
            source_path: Path to plugin directory
            config: Optional plugin configuration

        Returns:
            PluginRecord instance

        Raises:
            FileNotFoundError: If entry point not found
            AttributeError: If plugin module doesn't export required objects
            Exception: If plugin registration fails
        """
        plugin_id = manifest.id

        if plugin_id in self._loaded_plugins:
            logger.warning(f"Plugin '{plugin_id}' already loaded")
            return self._loaded_plugins[plugin_id]

        source_root = self._validate_source_allowed(source_path)
        if not plugin_config_enabled(config):
            return self._disabled_record(
                manifest,
                source_root,
                "config.enabled=false",
            )

        self._validate_source_integrity(manifest, source_root, config)
        self._validate_permissions(manifest, source_root)
        self._validate_min_version(manifest)
        self._validate_python_dependencies(manifest, set())

        # Load backend module (if declared and exists)
        backend_entry = manifest.entry.backend
        frontend_entry = manifest.entry.frontend
        backend_entry_file = (
            resolve_plugin_entry_path(
                source_root,
                backend_entry,
                kind="backend",
            )
            if backend_entry
            else None
        )
        frontend_entry_file = (
            resolve_plugin_entry_path(
                source_root,
                frontend_entry,
                kind="frontend",
            )
            if frontend_entry
            else None
        )
        plugin_def = None

        if backend_entry_file is None and frontend_entry_file is None:
            raise FileNotFoundError(
                f"Plugin '{plugin_id}' has no entry points declared "
                f"(entry.backend or entry.frontend)",
            )

        backend_exists = (
            backend_entry_file is not None and backend_entry_file.exists()
        )
        frontend_exists = (
            frontend_entry_file is not None and frontend_entry_file.exists()
        )

        if not backend_exists and not frontend_exists:
            raise FileNotFoundError(
                f"Plugin '{plugin_id}' entry point files not found: "
                + (f"{backend_entry_file}" if backend_entry_file else "")
                + (f", {frontend_entry_file}" if frontend_entry_file else ""),
            )

        if not backend_exists:
            # Frontend-only plugin — skip backend loading
            logger.info(
                f"Plugin '{plugin_id}' has no backend entry point "
                f"— loading as frontend-only plugin",
            )
        else:
            module_name = f"plugin_{plugin_id.replace('-', '_')}"
            registry_snapshot = self.registry.snapshot()
            try:
                # Dynamic import of plugin module
                # Use unique module name to avoid conflicts
                plugin_dir_str = str(source_root)

                # submodule_search_locations enables relative imports
                # within plugin without polluting global sys.path
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    backend_entry_file,
                    submodule_search_locations=[plugin_dir_str],
                )
                if spec is None or spec.loader is None:
                    raise ImportError(
                        f"Failed to load module spec for {backend_entry_file}",
                    )

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module

                # Set __package__ and __path__ to enable relative imports
                module.__package__ = module_name
                module.__path__ = [plugin_dir_str]

                spec.loader.exec_module(module)

                # Get plugin definition
                if not hasattr(module, "plugin"):
                    raise AttributeError(
                        "Plugin module must export 'plugin' object",
                    )

                plugin_def = module.plugin

                # Create plugin API instance with manifest
                manifest_dict = self._manifest_dict(manifest)
                api = PluginApi(plugin_id, config or {}, manifest_dict)
                api.set_registry(self.registry)

                # Register plugin manifest to registry
                self.registry.register_plugin_manifest(
                    plugin_id,
                    manifest_dict,
                )

                # Call plugin's register method (support both sync and async)
                if hasattr(plugin_def, "register"):
                    result = plugin_def.register(api)
                    if inspect.iscoroutine(result) or inspect.isawaitable(
                        result,
                    ):
                        await result
                else:
                    raise AttributeError(
                        "Plugin must implement 'register(api)' method",
                    )

            except Exception as e:
                self.registry.restore(registry_snapshot)
                _purge_module_tree(module_name)
                logger.error(
                    f"Failed to load plugin '{plugin_id}': {e}",
                    exc_info=True,
                )
                raise

        # Create plugin record
        record = PluginRecord(
            manifest=manifest,
            source_path=source_root,
            enabled=plugin_config_enabled(config),
            instance=plugin_def,
        )

        self._loaded_plugins[plugin_id] = record
        logger.info(f"✓ Loaded plugin '{plugin_id}' successfully")

        return record

    async def load_all_plugins(
        self,
        configs: Optional[Dict[str, Dict]] = None,
    ) -> Dict[str, PluginRecord]:
        """Discover and load all plugins.

        Args:
            configs: Optional dictionary of plugin_id -> config

        Returns:
            Dictionary of plugin_id -> PluginRecord
        """
        discovered = self.discover_plugins()
        configs = configs or {}

        by_id: Dict[str, list[Tuple[PluginManifest, Path]]] = {}
        for manifest, plugin_dir in discovered:
            by_id.setdefault(manifest.id, []).append((manifest, plugin_dir))

        duplicate_ids = {
            plugin_id
            for plugin_id, entries in by_id.items()
            if len(entries) > 1
        }
        for plugin_id in sorted(duplicate_ids):
            logger.error("Skipping duplicate plugin id before import: %s", plugin_id)

        candidates: Dict[str, Tuple[PluginManifest, Path, Dict[str, Any]]] = {}
        known_plugin_ids = {
            plugin_id
            for plugin_id in by_id
            if plugin_id not in duplicate_ids
        }

        for plugin_id, entries in by_id.items():
            if plugin_id in duplicate_ids:
                continue
            manifest, plugin_dir = entries[0]
            config = configs.get(manifest.id) or {}

            try:
                source_root = self._validate_source_allowed(plugin_dir)
                if not plugin_config_enabled(config):
                    self._disabled_record(
                        manifest,
                        source_root,
                        "config.enabled=false",
                    )
                    continue
                self._validate_source_integrity(manifest, source_root, config)
                self._validate_permissions(manifest, source_root)
                self._validate_min_version(manifest)
                self._validate_python_dependencies(manifest, known_plugin_ids)
                candidates[plugin_id] = (manifest, source_root, config)
            except Exception as e:
                logger.error(f"Failed to preflight plugin '{manifest.id}': {e}")

        graph, invalid_dependencies = self._plugin_dependency_graph(candidates)
        cycle_nodes = self._dependency_cycles(graph)
        skipped = invalid_dependencies | cycle_nodes
        for plugin_id in sorted(cycle_nodes):
            logger.error(
                "Skipping plugin '%s' because plugin dependencies contain a cycle",
                plugin_id,
            )

        for plugin_id in self._topological_order(graph):
            if plugin_id in skipped:
                continue
            manifest, plugin_dir, config = candidates[plugin_id]
            missing_loaded_dep = [
                dep
                for dep in graph.get(plugin_id, set())
                if dep not in self._loaded_plugins
            ]
            if missing_loaded_dep:
                logger.error(
                    "Skipping plugin '%s' because dependencies failed: %s",
                    plugin_id,
                    ", ".join(sorted(missing_loaded_dep)),
                )
                continue

            try:
                await self.load_plugin(manifest, plugin_dir, config)
            except Exception as e:
                logger.error(f"Failed to load plugin '{manifest.id}': {e}")

        return self._loaded_plugins

    async def unload_plugin(self, plugin_id: str) -> bool:
        """Unload one plugin and remove all runtime registrations."""

        record = self._loaded_plugins.get(plugin_id)
        if record is None:
            return False

        self.registry.disable_runtime_event_observers(plugin_id)
        drained = await self.registry.drain_runtime_event_observers(
            plugin_id=plugin_id,
            timeout_seconds=self.RUNTIME_EVENT_DRAIN_TIMEOUT_SECONDS,
        )
        if not drained:
            diagnostics = getattr(record, "diagnostics", None)
            if isinstance(diagnostics, list):
                diagnostics.append(
                    "Runtime observer callbacks are still in flight; plugin "
                    "is quarantined and remains loaded.",
                )
            logger.error(
                "Plugin '%s' remains loaded in observer quarantine; retry "
                "unload after its in-flight callbacks settle",
                plugin_id,
            )
            return False

        shutdown_hooks = [
            hook
            for hook in self.registry.get_shutdown_hooks()
            if hook.plugin_id == plugin_id
        ]
        for hook in reversed(shutdown_hooks):
            try:
                result = hook.callback()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning(
                    "Plugin '%s' shutdown hook '%s' failed",
                    plugin_id,
                    hook.hook_name,
                    exc_info=True,
                )

        plugin_def = record.instance
        if plugin_def is not None and hasattr(plugin_def, "unregister"):
            try:
                manifest_dict = self._manifest_dict(record.manifest)
                api = PluginApi(plugin_id, {}, manifest_dict)
                api.set_registry(self.registry)
                result = plugin_def.unregister(api)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning(
                    "Plugin '%s' unregister failed",
                    plugin_id,
                    exc_info=True,
                )

        self.registry.unregister_plugin(plugin_id)
        self._loaded_plugins.pop(plugin_id, None)
        _purge_module_tree(f"plugin_{plugin_id.replace('-', '_')}")
        logger.info("Unloaded plugin '%s'", plugin_id)
        return True

    def get_loaded_plugin(self, plugin_id: str) -> Optional[PluginRecord]:
        """Get loaded plugin record.

        Args:
            plugin_id: Plugin identifier

        Returns:
            PluginRecord or None if not found
        """
        return self._loaded_plugins.get(plugin_id)

    def get_all_loaded_plugins(self) -> Dict[str, PluginRecord]:
        """Get all loaded plugin records.

        Returns:
            Dictionary of plugin_id -> PluginRecord
        """
        return self._loaded_plugins.copy()
