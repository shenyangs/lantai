import json
from copy import deepcopy

import lantai.config.config as config_module
import lantai.config.utils as config_utils
from lantai.config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    AgentsConfig,
    Config,
    ImageGenerationConfig,
    MCPClientConfig,
    MCPConfig,
    ModelSlotConfig,
    RuntimeConfig,
    ToolsConfig,
    _LLM_ACQUIRE_TIMEOUT_30S_MIGRATION,
    _LEGACY_TAVILY_PLACEHOLDER,
    _NATIVE_RUNTIME_DEFAULT_MIGRATION,
    _NATIVE_RUNTIME_ONLY_MIGRATION,
    _STATIC_PROMPT_BOUNDARY_MIGRATION,
    _apply_llm_acquire_timeout_migration_to_raw,
    _apply_native_runtime_default_migration_to_raw,
    _apply_static_prompt_boundary_migration_to_raw,
    _retire_untouched_tavily_placeholder_from_raw,
    load_agent_config,
)
from lantai.constant import LLM_ACQUIRE_TIMEOUT


_RUNTIME_ENV_KEYS = (
    "LANTAI_RUNTIME_ENGINE",
    "LANTAI_RUNTIME_ENGINE_DEFAULT",
    "LANTAI_HARNESS_MODE",
    "LANTAI_DISTRIBUTION_CHANNEL",
    "LANTAI_RELEASE_CHANNEL",
    "LANTAI_APP_STORE",
)


def _clear_runtime_env(monkeypatch):
    for key in _RUNTIME_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.replace("LANTAI_", "COPAW_"), raising=False)


def _write_legacy_agent_profile(tmp_path, monkeypatch, *, engine: str):
    workspace_dir = tmp_path / "workspaces" / "default"
    workspace_dir.mkdir(parents=True)
    monkeypatch.setattr(config_module, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(config_utils, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(config_utils, "_config_cache", None)
    monkeypatch.setattr(config_utils, "_config_mtime", None)
    monkeypatch.setattr(config_utils, "_agent_config_cache", {})

    root_config = Config(
        agents=AgentsConfig(
            active_agent="default",
            profiles={
                "default": AgentProfileRef(
                    id="default",
                    workspace_dir=str(workspace_dir),
                ),
            },
        ),
    )
    config_utils.save_config(root_config)

    raw_profile = AgentProfileConfig(
        id="default",
        name="Default",
        description="Runtime migration test profile",
    ).model_dump(exclude_none=True)
    raw_profile["runtime"] = {"engine": engine}
    agent_path = workspace_dir / "agent.json"
    agent_path.write_text(json.dumps(raw_profile), encoding="utf-8")
    return agent_path


def test_runtime_engine_default_is_native_without_channel(monkeypatch):
    _clear_runtime_env(monkeypatch)

    assert RuntimeConfig().engine == "native"


def test_coherence_harness_defaults_to_compatible_shadow_mode(monkeypatch):
    _clear_runtime_env(monkeypatch)

    runtime = RuntimeConfig()

    assert runtime.harness_mode == "shadow"
    assert runtime.health_recovery == "safe"
    assert runtime.world_ledger_mode == "read_only"


def test_coherence_harness_mode_can_be_disabled_or_enforced(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_HARNESS_MODE", "enforced")

    assert RuntimeConfig().harness_mode == "enforced"
    assert RuntimeConfig(harness_mode="disabled").harness_mode == "off"


def test_tools_config_discards_retired_image_generation_tool() -> None:
    tools = ToolsConfig.model_validate(
        {
            "builtin_tools": {
                "generate_image_with_provider": {
                    "name": "generate_image_with_provider",
                    "enabled": True,
                },
            },
        },
    )

    assert "generate_image_with_provider" not in tools.builtin_tools
    assert tools.builtin_tools["generate_image"].enabled is True


def _legacy_tavily_placeholder() -> dict:
    return deepcopy(_LEGACY_TAVILY_PLACEHOLDER)


def test_mcp_config_has_no_external_service_placeholder_by_default() -> None:
    assert MCPConfig().clients == {}
    assert Config().mcp.clients == {}


def test_tavily_placeholder_retirement_preserves_any_customization() -> None:
    for replacement in (
        {"enabled": True},
        {"env": {"TAVILY_API_KEY": "configured"}},
        {"args": ["-y", "tavily-mcp@0.2.21"]},
        {"description": "My Tavily research connector"},
        {"custom_field": "keep-me"},
    ):
        candidate = _legacy_tavily_placeholder()
        candidate.update(replacement)
        data = {"mcp": {"clients": {"tavily_search": candidate}}}

        assert _retire_untouched_tavily_placeholder_from_raw(data) is False
        assert data["mcp"]["clients"]["tavily_search"] == candidate


def test_root_config_retires_only_untouched_tavily_placeholder(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    raw = Config().model_dump(exclude_none=True)
    raw["mcp"] = {
        "clients": {
            "tavily_search": _legacy_tavily_placeholder(),
            "custom": MCPClientConfig(
                name="Custom",
                command="custom-mcp",
            ).model_dump(exclude_none=True),
        },
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(config_utils, "_config_cache", None)
    monkeypatch.setattr(config_utils, "_config_mtime", None)

    loaded = config_utils.load_config(config_path)
    persisted = json.loads(config_path.read_text(encoding="utf-8"))

    assert set(loaded.mcp.clients) == {"custom"}
    assert set(persisted["mcp"]["clients"]) == {"custom"}
    assert len(list(tmp_path.glob("config.*.tavily-placeholder.bak"))) == 1

    monkeypatch.setattr(config_utils, "_config_cache", None)
    monkeypatch.setattr(config_utils, "_config_mtime", None)
    config_utils.load_config(config_path)
    assert len(list(tmp_path.glob("config.*.tavily-placeholder.bak"))) == 1


def test_agent_config_retires_untouched_tavily_placeholder_once(
    tmp_path,
    monkeypatch,
) -> None:
    _clear_runtime_env(monkeypatch)
    agent_path = _write_legacy_agent_profile(tmp_path, monkeypatch, engine="native")
    raw = json.loads(agent_path.read_text(encoding="utf-8"))
    raw["mcp"] = {
        "clients": {
            "tavily_search": _legacy_tavily_placeholder(),
            "custom": MCPClientConfig(
                name="Custom",
                command="custom-mcp",
            ).model_dump(exclude_none=True),
        },
    }
    agent_path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = load_agent_config("default")
    persisted = json.loads(agent_path.read_text(encoding="utf-8"))

    assert set(loaded.mcp.clients) == {"custom"}
    assert set(persisted["mcp"]["clients"]) == {"custom"}
    assert len(
        list(agent_path.parent.glob("agent.*.tavily-placeholder.bak")),
    ) == 1

    config_utils._agent_config_cache.clear()
    load_agent_config("default")
    assert len(
        list(agent_path.parent.glob("agent.*.tavily-placeholder.bak")),
    ) == 1


def test_image_generation_config_deduplicates_and_trims_model_slots() -> None:
    config = ImageGenerationConfig(
        enabled=True,
        models=[
            ModelSlotConfig(provider_id=" openai ", model=" gpt-image-2 "),
            ModelSlotConfig(provider_id="openai", model="gpt-image-2"),
            ModelSlotConfig(provider_id="", model="ignored"),
        ],
        timeout_seconds=90,
    )

    assert config.models == [
        ModelSlotConfig(provider_id="openai", model="gpt-image-2"),
    ]
    assert config.timeout_seconds == 90


def test_image_generation_config_round_trips_with_root_config(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config = Config(
        image_generation=ImageGenerationConfig(
            enabled=True,
            models=[
                ModelSlotConfig(
                    provider_id="image-provider",
                    model="image-model",
                ),
            ],
            timeout_seconds=75,
        ),
    )

    config_utils.save_config(config, config_path)
    loaded = config_utils.load_config(config_path)

    assert loaded.image_generation.enabled is True
    assert loaded.image_generation.models == [
        ModelSlotConfig(
            provider_id="image-provider",
            model="image-model",
        ),
    ]
    assert loaded.image_generation.timeout_seconds == 75


def test_video_generation_config_deduplicates_and_round_trips(tmp_path) -> None:
    from lantai.config.config import VideoGenerationConfig

    config_path = tmp_path / "config.json"
    config = Config(
        video_generation=VideoGenerationConfig(
            enabled=True,
            models=[
                ModelSlotConfig(
                    provider_id=" tokendance ",
                    model=" seedance-2.0 ",
                ),
                ModelSlotConfig(
                    provider_id="tokendance",
                    model="seedance-2.0",
                ),
            ],
            timeout_seconds=900,
            legacy_model_flags_migrated=True,
        ),
    )

    config_utils.save_config(config, config_path)
    loaded = config_utils.load_config(config_path)

    assert loaded.video_generation.enabled is True
    assert loaded.video_generation.models == [
        ModelSlotConfig(
            provider_id="tokendance",
            model="seedance-2.0",
        ),
    ]
    assert loaded.video_generation.timeout_seconds == 900
    assert loaded.video_generation.legacy_model_flags_migrated is True


def test_runtime_engine_defaults_native_for_dev_canary_distribution(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_RELEASE_CHANNEL", "canary")

    assert RuntimeConfig().engine == "native"


def test_runtime_engine_default_env_flag_enables_native(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE_DEFAULT", "native")

    assert RuntimeConfig().engine == "native"


def test_runtime_engine_app_store_accepts_desktop_native_default(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_APP_STORE", "1")
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE_DEFAULT", "native")
    monkeypatch.setenv("LANTAI_RELEASE_CHANNEL", "canary")

    assert RuntimeConfig().engine == "native"


def test_runtime_engine_explicit_override_wins_for_manual_smoke(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_APP_STORE", "1")
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE", "native")

    assert RuntimeConfig().engine == "native"


def test_native_only_migration_retires_explicit_legacy_override(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE_DEFAULT", "native")
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE", "legacy")
    data = {"runtime": {"engine": "legacy"}}

    assert _apply_native_runtime_default_migration_to_raw(data) is True
    assert data["runtime"]["engine"] == "native"
    assert _NATIVE_RUNTIME_ONLY_MIGRATION in data["runtime_migrations"]


def test_runtime_engine_normalizes_previous_legacy_alias_to_native(monkeypatch):
    _clear_runtime_env(monkeypatch)
    previous_alias = "agent" + "scope"

    assert RuntimeConfig(engine=previous_alias).engine == "native"

    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE", previous_alias)
    assert RuntimeConfig().engine == "native"


def test_native_runtime_default_migration_promotes_generated_agents(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE_DEFAULT", "native")
    data = {"runtime": {"engine": "agent" + "scope"}}

    changed = _apply_native_runtime_default_migration_to_raw(data)

    assert changed is True
    assert data["runtime"]["engine"] == "native"
    assert _NATIVE_RUNTIME_DEFAULT_MIGRATION in data["runtime_migrations"]
    assert _NATIVE_RUNTIME_ONLY_MIGRATION in data["runtime_migrations"]


def test_native_runtime_only_migration_supersedes_old_default_marker(monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE_DEFAULT", "native")
    data = {
        "runtime": {"engine": "legacy"},
        "runtime_migrations": [_NATIVE_RUNTIME_DEFAULT_MIGRATION],
    }

    assert _apply_native_runtime_default_migration_to_raw(data) is True
    assert data["runtime"]["engine"] == "native"
    assert _NATIVE_RUNTIME_ONLY_MIGRATION in data["runtime_migrations"]


def test_native_runtime_only_migration_is_one_shot(monkeypatch):
    _clear_runtime_env(monkeypatch)
    data = {
        "runtime": {"engine": "native"},
        "runtime_migrations": [_NATIVE_RUNTIME_ONLY_MIGRATION],
    }

    assert _apply_native_runtime_default_migration_to_raw(data) is False
    assert data["runtime"]["engine"] == "native"


def test_llm_acquire_timeout_migration_replaces_only_old_default() -> None:
    old_default = {"running": {"llm_acquire_timeout": 300.0}}
    custom = {"running": {"llm_acquire_timeout": 45.0}}

    assert _apply_llm_acquire_timeout_migration_to_raw(old_default) is True
    assert old_default["running"]["llm_acquire_timeout"] == LLM_ACQUIRE_TIMEOUT
    assert _LLM_ACQUIRE_TIMEOUT_30S_MIGRATION in old_default["runtime_migrations"]

    assert _apply_llm_acquire_timeout_migration_to_raw(custom) is True
    assert custom["running"]["llm_acquire_timeout"] == 45.0


def test_llm_acquire_timeout_migration_is_one_shot() -> None:
    data = {
        "running": {"llm_acquire_timeout": 300.0},
        "runtime_migrations": [_LLM_ACQUIRE_TIMEOUT_30S_MIGRATION],
    }

    assert _apply_llm_acquire_timeout_migration_to_raw(data) is False
    assert data["running"]["llm_acquire_timeout"] == 300.0


def test_static_prompt_boundary_removes_transient_workspace_authority() -> None:
    data = {
        "system_prompt_files": [
            "AGENTS.md",
            "STATE.md",
            "CUSTOM.md",
            "WORKBENCH.md",
        ],
    }

    assert _apply_static_prompt_boundary_migration_to_raw(data) is True
    assert data["system_prompt_files"] == ["AGENTS.md", "CUSTOM.md"]
    assert _STATIC_PROMPT_BOUNDARY_MIGRATION in data["runtime_migrations"]


def test_static_prompt_boundary_migration_is_one_shot() -> None:
    data = {
        "system_prompt_files": ["AGENTS.md", "WORKBENCH.md"],
        "runtime_migrations": [_STATIC_PROMPT_BOUNDARY_MIGRATION],
    }

    assert _apply_static_prompt_boundary_migration_to_raw(data) is False
    assert data["system_prompt_files"] == ["AGENTS.md", "WORKBENCH.md"]


def test_load_agent_config_migrates_persisted_legacy_profile_once(tmp_path, monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE_DEFAULT", "native")
    agent_path = _write_legacy_agent_profile(tmp_path, monkeypatch, engine="legacy")

    loaded = load_agent_config("default")
    migrated = json.loads(agent_path.read_text(encoding="utf-8"))
    backups = list(agent_path.parent.glob("agent.*.native-runtime-default.bak"))

    assert loaded.runtime.engine == "native"
    assert migrated["runtime"]["engine"] == "native"
    assert _NATIVE_RUNTIME_DEFAULT_MIGRATION in migrated["runtime_migrations"]
    assert _NATIVE_RUNTIME_ONLY_MIGRATION in migrated["runtime_migrations"]
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["runtime"]["engine"] == "legacy"

    config_utils._agent_config_cache.clear()
    reloaded = load_agent_config("default")

    assert reloaded.runtime.engine == "native"
    assert len(list(agent_path.parent.glob("agent.*.native-runtime-default.bak"))) == 1


def test_load_agent_config_retires_explicit_legacy_runtime_override(tmp_path, monkeypatch):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE_DEFAULT", "native")
    monkeypatch.setenv("LANTAI_RUNTIME_ENGINE", "legacy")
    agent_path = _write_legacy_agent_profile(tmp_path, monkeypatch, engine="legacy")

    loaded = load_agent_config("default")
    persisted = json.loads(agent_path.read_text(encoding="utf-8"))

    assert loaded.runtime.engine == "native"
    assert persisted["runtime"]["engine"] == "native"
    assert _NATIVE_RUNTIME_ONLY_MIGRATION in persisted["runtime_migrations"]
    assert len(list(agent_path.parent.glob("agent.*.native-runtime-default.bak"))) == 1
