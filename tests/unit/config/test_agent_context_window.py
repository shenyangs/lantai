from types import SimpleNamespace

from lantai.config.config import (
    DEFAULT_AGENT_MAX_INPUT_LENGTH,
    LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH,
    AgentsRunningConfig,
    ReMeLightMemoryConfig,
    _AGENT_CONTEXT_WINDOW_256K_MIGRATION,
    _KIMI_CONTEXT_WINDOW_256K_MIGRATION,
    _apply_agent_context_window_migration_to_raw,
    _apply_kimi_context_window_migration_to_raw,
)
from lantai.config import utils as config_utils


def test_agent_running_config_defaults_to_256k_context():
    config = AgentsRunningConfig()

    assert config.max_input_length == DEFAULT_AGENT_MAX_INPUT_LENGTH
    assert config.context_policy_mode == "smart"


def test_agent_context_policy_mode_round_trips_for_advanced_users():
    config = AgentsRunningConfig(context_policy_mode="max")

    reloaded = AgentsRunningConfig.model_validate(config.model_dump())

    assert reloaded.context_policy_mode == "max"


def test_legacy_auto_memory_interval_is_readable_but_not_default_scheduled_work():
    assert ReMeLightMemoryConfig().auto_memory_interval is None
    assert ReMeLightMemoryConfig(auto_memory_interval=2).auto_memory_interval == 2


def test_governed_dreaming_is_opt_in_with_safe_product_defaults():
    config = ReMeLightMemoryConfig()

    assert config.dreaming_enabled is False
    assert config.dream_cron == "0 3 * * *"
    assert config.dreaming_rem_enabled is True
    assert config.dreaming_min_score == 0.68


def test_dream_schedule_is_registered_only_after_explicit_opt_in(
    monkeypatch,
):
    dreaming = ReMeLightMemoryConfig()
    profile = SimpleNamespace(
        running=SimpleNamespace(reme_light_memory_config=dreaming),
    )
    monkeypatch.setattr(
        config_utils,
        "_should_defer_agent_config_io",
        lambda _agent_id: False,
    )
    monkeypatch.setattr(
        config_utils,
        "load_agent_config",
        lambda _agent_id: profile,
    )

    assert config_utils.get_dream_cron("agent") == ""

    dreaming.dreaming_enabled = True
    assert config_utils.get_dream_cron("agent") == "0 3 * * *"


def test_kimi_context_window_migration_promotes_legacy_default():
    data = {
        "active_model": {
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
        },
        "running": {"max_input_length": LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH},
    }

    assert _apply_kimi_context_window_migration_to_raw(data) is True
    assert data["running"]["max_input_length"] == DEFAULT_AGENT_MAX_INPUT_LENGTH
    assert _KIMI_CONTEXT_WINDOW_256K_MIGRATION in data["runtime_migrations"]


def test_all_legacy_agent_defaults_migrate_to_256k_context():
    data = {
        "active_model": {
            "provider_id": "unknown-provider",
            "model": "future-agent-model",
        },
        "running": {"max_input_length": LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH},
    }

    assert _apply_agent_context_window_migration_to_raw(data) is True
    assert data["running"]["max_input_length"] == DEFAULT_AGENT_MAX_INPUT_LENGTH
    assert _AGENT_CONTEXT_WINDOW_256K_MIGRATION in data["runtime_migrations"]


def test_agent_context_migration_preserves_custom_context():
    custom_context = 192 * 1024
    data = {
        "running": {"max_input_length": custom_context},
    }

    assert _apply_agent_context_window_migration_to_raw(data) is True
    assert data["running"]["max_input_length"] == custom_context


def test_kimi_context_window_migration_marks_custom_context_without_overwrite():
    custom_context = 192 * 1024
    data = {
        "active_model": {
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
        },
        "running": {"max_input_length": custom_context},
    }

    assert _apply_kimi_context_window_migration_to_raw(data) is True
    assert data["running"]["max_input_length"] == custom_context
    assert _KIMI_CONTEXT_WINDOW_256K_MIGRATION in data["runtime_migrations"]


def test_kimi_context_window_migration_skips_non_kimi_agents():
    data = {
        "active_model": {
            "provider_id": "deepseek",
            "model": "deepseek-chat",
        },
        "running": {"max_input_length": LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH},
    }

    assert _apply_kimi_context_window_migration_to_raw(data) is False
    assert data["running"]["max_input_length"] == LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH
    assert "runtime_migrations" not in data


def test_kimi_context_window_migration_is_one_shot():
    data = {
        "active_model": {
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
        },
        "running": {"max_input_length": LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH},
        "runtime_migrations": [_KIMI_CONTEXT_WINDOW_256K_MIGRATION],
    }

    assert _apply_kimi_context_window_migration_to_raw(data) is False
    assert data["running"]["max_input_length"] == LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH
