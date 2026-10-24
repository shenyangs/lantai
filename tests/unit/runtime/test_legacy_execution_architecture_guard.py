# -*- coding: utf-8 -*-
"""Static locks that keep diagnostic fallback code out of product wiring."""

from __future__ import annotations

from pathlib import Path

from lantai.agents.legacy_agent_components import LegacyAgentLoop
from lantai.agents.legacy_lantai_agent import LegacyLantaiAgent


ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src" / "lantai"
def test_r6_legacy_execution_adapters_are_physically_absent() -> None:
    adapters = SOURCE_ROOT / "runtime" / "adapters"

    assert not (adapters / "fallback.py").exists()
    assert not (adapters / "legacy_runtime_adapter.py").exists()
    assert not (SOURCE_ROOT / "agents" / "tool_guard_mixin.py").exists()

    exports = (adapters / "__init__.py").read_text(encoding="utf-8")
    for forbidden in (
        "EarlyFallbackRuntimeAdapter",
        "LegacyRuntimeAdapter",
        "create_legacy_runtime_adapter",
        "LEGACY_RUNTIME_FRAMEWORK_TYPE",
    ):
        assert forbidden not in exports


def test_standard_runner_never_imports_early_fallback_adapter() -> None:
    runner = (SOURCE_ROOT / "app" / "runner" / "runner.py").read_text(
        encoding="utf-8",
    )

    assert "EarlyFallbackRuntimeAdapter" not in runner
    assert "create_legacy_runtime_adapter" not in runner
    assert "LEGACY_RUNTIME_FRAMEWORK_TYPE" not in runner
    assert 'self.framework_type = "lantai"' in runner


def test_product_entrypoints_have_no_direct_legacy_execution_escape_hatch() -> None:
    runner = (SOURCE_ROOT / "app" / "runner" / "runner.py").read_text(
        encoding="utf-8",
    )
    mission = (SOURCE_ROOT / "agents" / "mission" / "mission_runner.py").read_text(
        encoding="utf-8",
    )
    cli = (SOURCE_ROOT / "cli" / "task_cmd.py").read_text(encoding="utf-8")

    assert "create_legacy_runtime_adapter" not in runner
    assert "agent(msgs)" not in mission
    assert "agent.reply(" not in cli
    assert '"runtime_engine": "native"' in cli


def test_r6_intelligence_facade_defines_no_execution_perimeter() -> None:
    forbidden = {
        "__call__",
        "reply",
        "_reasoning",
        "_acting",
        "interrupt",
        "register_instance_hook",
        "remove_instance_hook",
        "clear_instance_hooks",
        "set_console_output_enabled",
        "set_msg_queue_enabled",
    }

    assert forbidden.isdisjoint(LegacyAgentLoop.__dict__)
    assert forbidden.isdisjoint(LegacyLantaiAgent.__dict__)
    assert all(base.__name__ != "ToolGuardMixin" for base in LegacyLantaiAgent.__mro__)
