# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Behavior locks for safe Pi-inspired plugin runtime extensions."""

import asyncio
import json
import threading
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from lantai.agents.tool_registry import build_lantai_tool_registry
from lantai.config.config import ToolsConfig
from lantai.plugins.api import PluginApi
from lantai.plugins.loader import PluginLoader, compute_plugin_source_sha256
from lantai.plugins.registry import PluginRegistry
from lantai.runtime.artifact_effects import artifact_effect_intents
from lantai.runtime.effects import (
    RuntimeEffectRecoveryCapability,
    assess_runtime_effect_replay,
)
from lantai.runtime.runs import RuntimeEffectRecord, RuntimeRunOwner
from lantai.runtime.tools import (
    ToolCall,
    ToolOutputSemantics,
    ToolSpec,
)
from lantai.runtime.events import RuntimeEvent


@pytest.fixture(autouse=True)
def reset_registry():
    PluginRegistry._instance = None
    yield
    PluginRegistry._instance = None


def test_plugin_tool_registration_requires_read_or_write_permission() -> None:
    registry = PluginRegistry()
    read_api = PluginApi(
        "reader",
        {},
        {"permissions": ["runtime:tool:read"]},
    )
    read_api.set_registry(registry)
    read_api.register_tool(
        ToolSpec(name="reader_inspect_demo", read_only=True),
        lambda: "ok",
    )

    read_registration = registry.get_tool("reader_inspect_demo")
    assert read_registration is not None
    assert read_registration.spec.output_semantics == ToolOutputSemantics()
    assert read_registration.spec.effect_recovery == (
        RuntimeEffectRecoveryCapability()
    )
    discovered = ToolsConfig().builtin_tools["reader_inspect_demo"]
    assert discovered.enabled is False
    with pytest.raises(ValueError, match="namespace"):
        read_api.register_tool(
            ToolSpec(name="read_file", read_only=True),
            lambda: "override",
        )

    with pytest.raises(PermissionError):
        read_api.register_tool(
            ToolSpec(name="reader_write_demo", destructive=True),
            lambda: "changed",
        )
    with pytest.raises(ValueError, match="Runtime-owned"):
        read_api.register_tool(
            ToolSpec(
                name="reader_inspect_reserved",
                read_only=True,
                metadata={"runtime_owned": True},
            ),
            lambda: "unsafe",
        )

    write_api = PluginApi(
        "writer",
        {},
        {"permissions": ["runtime:tool:write"]},
    )
    write_api.set_registry(registry)
    write_api.register_tool(
        ToolSpec(name="writer_write_demo", destructive=True),
        lambda: "changed",
    )
    assert registry.get_tool("writer_write_demo") is not None


@pytest.mark.parametrize(
    ("plugin_id", "tool_name"),
    [
        ("read", "read_file"),
        ("discover", "discover_tools"),
        ("declare", "declare_deliverable_contract"),
    ],
)
def test_plugin_tool_registration_rejects_reserved_full_names(
    plugin_id: str,
    tool_name: str,
) -> None:
    registry = PluginRegistry()
    api = PluginApi(
        plugin_id,
        {},
        {"permissions": ["runtime:tool:read"]},
    )
    api.set_registry(registry)

    with pytest.raises(ValueError, match="reserved"):
        api.register_tool(
            ToolSpec(name=tool_name, read_only=True),
            lambda: "spoofed",
        )
    with pytest.raises(ValueError, match="reserved"):
        registry.register_tool(
            plugin_id=plugin_id,
            spec=ToolSpec(name=tool_name, origin="plugin", read_only=True),
            executor=lambda: "spoofed",
        )
    registry.register_plugin_manifest(
        plugin_id,
        {"meta": {"tool_name": tool_name}},
    )
    assert registry.get_plugin_id_for_tool(tool_name) is None


def test_plugin_tool_cannot_forge_artifact_or_effect_recovery_semantics(
    tmp_path: Path,
) -> None:
    registry = PluginRegistry()
    api = PluginApi(
        "writer",
        {},
        {"permissions": ["runtime:tool:write"]},
    )
    api.set_registry(registry)
    api.register_tool(
        ToolSpec(
            name="writer_export",
            read_only=False,
            destructive=False,
            output_semantics=ToolOutputSemantics(
                artifact_role="primary",
                argument_path_fields=("path",),
            ),
            effect_recovery=RuntimeEffectRecoveryCapability(
                external_side_effect=False,
                idempotency="read_only",
                probe="not_required",
                compensation="not_required",
                on_ambiguity="safe_to_retry",
            ),
        ),
        lambda **_kwargs: "changed",
    )

    registered = registry.get_tool("writer_export")
    assert registered is not None
    spec = registered.spec
    assert spec.output_semantics == ToolOutputSemantics()
    assert spec.destructive is True
    assert spec.read_only is False
    assert spec.effect_recovery.external_side_effect is True
    assert spec.effect_recovery.idempotency == "not_idempotent"
    assert spec.effect_recovery.probe == "none"
    assert spec.effect_recovery.compensation == "none"
    assert spec.effect_recovery.on_ambiguity == "manual_disambiguation"

    source_effect = RuntimeEffectRecord(
        run_id="run-plugin",
        attempt_id="attempt-1",
        step_id="step-1",
        effect_id="effect-1",
        source="chat",
        owner=RuntimeRunOwner(agent_id="default"),
        status="running",
        tool_name="writer_export",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="plugin-effect-1",
        release_edition="official",
    )
    assert not artifact_effect_intents(
        source_effect=source_effect,
        tool_call=ToolCall(
            id="call-1",
            name="writer_export",
            arguments={"path": str(tmp_path / "forged.txt")},
        ),
        tool_spec=spec,
        workspace_dir=tmp_path,
    )
    replay = assess_runtime_effect_replay(
        source_effect,
        spec.effect_recovery,
    )
    assert replay.automatic_external_call_allowed is False
    assert replay.requires_human is True

    registry.register_tool(
        plugin_id="writer",
        spec=ToolSpec(
            name="writer_direct",
            origin="plugin",
            read_only=False,
            output_semantics=ToolOutputSemantics(artifact_role="delivery"),
            effect_recovery=RuntimeEffectRecoveryCapability(),
        ),
        executor=lambda: "changed",
    )
    direct = registry.get_tool("writer_direct")
    assert direct is not None
    assert direct.spec.output_semantics == ToolOutputSemantics()
    assert direct.spec.effect_recovery.on_ambiguity == "manual_disambiguation"


def test_plugin_tool_enters_normal_runtime_policy_registry() -> None:
    plugin_registry = PluginRegistry()
    api = PluginApi(
        "reader",
        {},
        {"permissions": ["runtime:tool:read"]},
    )
    api.set_registry(plugin_registry)
    api.register_tool(
        ToolSpec(name="reader_inspect_demo", read_only=True),
        lambda: "ok",
    )
    agent_config = SimpleNamespace(
        tools=SimpleNamespace(
            builtin_tools={
                "reader_inspect_demo": SimpleNamespace(
                    enabled=True,
                    async_execution=False,
                ),
            },
        ),
    )

    runtime_registry = build_lantai_tool_registry(
        agent_config=agent_config,
        request_context={"distribution": "developer_id"},
        is_coding_mode_enabled_fn=lambda _context: False,
    )

    registered = runtime_registry.get_tool("reader_inspect_demo")
    assert registered is not None
    spec, executor = registered
    assert spec.origin == "plugin"
    assert spec.read_only is True
    assert executor() == "ok"
    assert runtime_registry.policy_middlewares()


def test_plugin_tool_cannot_spoof_assistant_artifact_projection() -> None:
    plugin_registry = PluginRegistry()
    api = PluginApi(
        "projection",
        {},
        {"permissions": ["runtime:tool:read"]},
    )
    api.set_registry(plugin_registry)
    api.register_tool(
        ToolSpec(
            name="projection_spoof_demo",
            origin="builtin",
            read_only=True,
            metadata={
                "assistant_artifact_projection": (
                    "analysis_chart_reference"
                ),
            },
        ),
        lambda: "ok",
    )
    agent_config = SimpleNamespace(
        tools=SimpleNamespace(
            builtin_tools={
                "projection_spoof_demo": SimpleNamespace(
                    enabled=True,
                    async_execution=False,
                ),
            },
        ),
    )

    runtime_registry = build_lantai_tool_registry(
        agent_config=agent_config,
        request_context={"distribution": "developer_id"},
        is_coding_mode_enabled_fn=lambda _context: False,
    )

    registered = runtime_registry.get_tool("projection_spoof_demo")
    assert registered is not None
    spec, _executor = registered
    assert spec.origin == "plugin"
    assert "assistant_artifact_projection" not in spec.metadata


async def test_runtime_observer_permission_and_safe_copy() -> None:
    registry = PluginRegistry()
    denied = PluginApi("denied", {}, {"permissions": []})
    denied.set_registry(registry)
    with pytest.raises(PermissionError):
        denied.register_runtime_event_observer(
            "denied-observer",
            lambda _event: None,
        )

    observed = []
    api = PluginApi(
        "observer",
        {},
        {"permissions": ["runtime:event:read"]},
    )
    api.set_registry(registry)
    api.register_runtime_event_observer(
        "terminal-observer",
        observed.append,
        event_types={"turn.completed"},
    )
    source = RuntimeEvent(
        id="event-1",
        seq=1,
        type="turn.completed",
        status="completed",
        payload={
            "outcome_status": "delivered",
            "details": {"api_key": "secret", "text": "visible result"},
        },
    )

    await registry.notify_runtime_event(source)

    assert len(observed) == 1
    assert observed[0].payload["details"]["api_key"]["redacted"] is True
    assert "secret" not in observed[0].model_dump_json()
    assert observed[0].payload["data_classification"] == "sensitive"
    assert source.payload["details"]["api_key"] == "secret"


async def test_runtime_observers_receive_independent_nested_payloads() -> None:
    registry = PluginRegistry()
    api = PluginApi(
        "observer",
        {},
        {"permissions": ["runtime:event:read"]},
    )
    api.set_registry(registry)
    observed = []

    def mutate(event):
        event.payload["details"]["value"] = "changed"

    api.register_runtime_event_observer("mutator", mutate, priority=1)
    api.register_runtime_event_observer("reader", observed.append, priority=2)
    source = RuntimeEvent(
        id="event-1",
        type="turn.completed",
        payload={"details": {"value": "original"}},
    )

    await registry.notify_runtime_event(source)

    assert observed[0].payload["details"]["value"] == "original"
    assert source.payload["details"]["value"] == "original"


async def test_runtime_event_publish_is_non_blocking_and_drainable() -> None:
    registry = PluginRegistry()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_observer(_event):
        started.set()
        await release.wait()

    api = PluginApi(
        "observer",
        {},
        {"permissions": ["runtime:event:read"]},
    )
    api.set_registry(registry)
    api.register_runtime_event_observer("slow", slow_observer)
    registry.MAX_PENDING_RUNTIME_EVENT_TASKS = 1

    registry.publish_runtime_event(
        RuntimeEvent(id="event-1", type="turn.started"),
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    registry.publish_runtime_event(
        RuntimeEvent(id="event-2", type="turn.started"),
    )
    assert registry.runtime_event_drop_count == 1

    release.set()
    await asyncio.wait_for(
        registry.drain_runtime_event_observers(),
        timeout=1,
    )


async def test_runtime_event_publish_preserves_source_order() -> None:
    registry = PluginRegistry()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    observed = []

    async def ordered_observer(event):
        if event.id == "event-1":
            first_started.set()
            await release_first.wait()
        observed.append(event.id)

    api = PluginApi(
        "observer",
        {},
        {"permissions": ["runtime:event:read"]},
    )
    api.set_registry(registry)
    api.register_runtime_event_observer("ordered", ordered_observer)

    registry.publish_runtime_event(
        RuntimeEvent(id="event-1", type="turn.started"),
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    registry.publish_runtime_event(
        RuntimeEvent(id="event-2", type="turn.completed"),
    )
    await asyncio.sleep(0)
    assert not observed

    release_first.set()
    await registry.drain_runtime_event_observers()
    assert observed == ["event-1", "event-2"]


async def test_runtime_event_publish_snapshot_failure_is_fail_open() -> None:
    class HostileCopy:
        def __deepcopy__(self, _memo):
            raise RuntimeError("hostile deepcopy")

    registry = PluginRegistry()
    observed = []
    api = PluginApi(
        "observer",
        {},
        {"permissions": ["runtime:event:read"]},
    )
    api.set_registry(registry)
    api.register_runtime_event_observer("observer", observed.append)
    source = RuntimeEvent(id="event-hostile", type="turn.started")
    hostile = HostileCopy()
    source.payload["details"] = {"value": hostile}

    registry.publish_runtime_event(source)
    await asyncio.sleep(0)

    assert not observed
    assert registry.runtime_event_drop_count == 1
    assert source.payload["details"]["value"] is hostile


async def test_unload_closes_observer_admission_before_drain() -> None:
    loader = PluginLoader([])
    registry = loader.registry
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def observer(event):
        started.set()
        await release.wait()
        observed.append(event.id)

    api = PluginApi(
        "observer",
        {},
        {"permissions": ["runtime:event:read"]},
    )
    api.set_registry(registry)
    api.register_runtime_event_observer("observer", observer)
    loader._loaded_plugins["observer"] = SimpleNamespace(instance=None)

    registry.publish_runtime_event(
        RuntimeEvent(id="event-before-unload", type="turn.started"),
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    unloading = asyncio.create_task(loader.unload_plugin("observer"))
    await asyncio.sleep(0)
    assert not registry.get_runtime_event_observers()

    registry.publish_runtime_event(
        RuntimeEvent(id="event-during-unload", type="turn.completed"),
    )
    release.set()

    assert await asyncio.wait_for(unloading, timeout=1) is True
    assert observed == ["event-before-unload"]


async def test_sync_observer_timeout_quarantines_instead_of_clean_unload() -> (  # noqa: E501
    None
):
    loader = PluginLoader([])
    loader.RUNTIME_EVENT_DRAIN_TIMEOUT_SECONDS = 0.01
    registry = loader.registry
    started = threading.Event()
    release = threading.Event()
    teardown = []

    def observer(_event):
        started.set()
        release.wait(timeout=2)

    api = PluginApi(
        "observer",
        {},
        {"permissions": ["runtime:event:read"]},
    )
    api.set_registry(registry)
    api.register_runtime_event_observer("observer", observer)
    registry.register_shutdown_hook(
        plugin_id="observer",
        hook_name="shutdown",
        callback=lambda: teardown.append("shutdown"),
    )
    record = SimpleNamespace(instance=None)
    loader._loaded_plugins["observer"] = record
    registry.publish_runtime_event(
        RuntimeEvent(id="event-sync", type="turn.started"),
    )
    assert await asyncio.to_thread(started.wait, 1)

    assert await loader.unload_plugin("observer") is False
    assert loader._loaded_plugins["observer"] is record
    assert registry.is_runtime_event_plugin_quarantined("observer") is True
    assert not registry.get_runtime_event_observers()
    assert not teardown

    release.set()
    assert await registry.drain_runtime_event_observers(
        plugin_id="observer",
        timeout_seconds=1,
    )
    assert await loader.unload_plugin("observer") is True
    assert teardown == ["shutdown"]


async def test_loader_registers_and_unloads_runtime_extensions(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugins" / "runtime-demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "id": "runtime-demo",
                "name": "Runtime Demo",
                "version": "1.0.0",
                "entry": {"backend": "plugin.py"},
                "permissions": [
                    "runtime:tool:read",
                    "runtime:event:read",
                ],
            },
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        dedent(
            """
            from lantai.runtime.tools import ToolSpec

            class RuntimeDemo:
                def register(self, api):
                    api.register_tool(
                        ToolSpec(
                            name="runtime_demo_inspect_runtime",
                            read_only=True,
                        ),
                        lambda: "ok",
                    )
                    api.register_runtime_event_observer(
                        "terminal",
                        lambda _event: None,
                        event_types={"turn.completed"},
                    )

            plugin = RuntimeDemo()
            """,
        ).lstrip(),
        encoding="utf-8",
    )
    loader = PluginLoader(
        [plugin_dir.parent],
        allow_external_plugins=True,
    )

    records = await loader.load_all_plugins(
        configs={
            "runtime-demo": {
                "enabled": True,
                "source_sha256": compute_plugin_source_sha256(plugin_dir),
            },
        },
    )

    assert records["runtime-demo"].enabled is True
    assert loader.registry.get_tool("runtime_demo_inspect_runtime") is not None
    assert len(loader.registry.get_runtime_event_observers()) == 1
    assert await loader.unload_plugin("runtime-demo") is True
    assert loader.registry.get_tool("runtime_demo_inspect_runtime") is None
    assert not loader.registry.get_runtime_event_observers()
