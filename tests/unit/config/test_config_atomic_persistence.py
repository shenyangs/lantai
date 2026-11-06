"""A failed save or startup migration must retain the last usable config."""

import builtins
import errno
import json
import os
import stat
from pathlib import Path

import pytest

import lantai.config.config as config_module
import lantai.config.utils as config_utils


@pytest.fixture
def config_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(config_utils, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(config_utils, "_config_cache", None)
    monkeypatch.setattr(config_utils, "_config_mtime", None)
    monkeypatch.setattr(config_utils, "_config_cache_path", None)
    monkeypatch.setattr(config_utils, "_agent_config_cache", {})
    workspace = tmp_path / "workspaces" / "default"
    root = config_module.Config(
        agents=config_module.AgentsConfig(
            active_agent="default",
            profiles={
                "default": config_module.AgentProfileRef(
                    id="default",
                    workspace_dir=str(workspace),
                ),
            },
        ),
        user_timezone="Pacific/Honolulu",
    )
    config_utils.save_config(root)
    config_module.save_agent_config(
        "default",
        config_module.AgentProfileConfig(id="default", name="原配置"),
    )
    return config_utils.get_config_path(), workspace / "agent.json"


def _save_changed(kind):
    if kind == "root":
        config = config_utils.load_config()
        config.user_timezone = "Asia/Shanghai"
        config.plugins["atomic-test"] = {"label": "新配置"}
        config_utils.save_config(config)
    else:
        config_module.save_agent_config(
            "default",
            config_module.AgentProfileConfig(id="default", name="新配置"),
        )


def _intercept_writes(monkeypatch, target, on_write):
    """Inject real write faults for direct and temporary-file writers."""
    original_open = builtins.open
    original_fdopen = os.fdopen

    class Writer:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, value):
            return on_write(self.handle, value)

    def open_file(file, mode="r", *args, **kwargs):
        handle = original_open(file, mode, *args, **kwargs)
        if "w" in mode and not isinstance(file, int) and Path(file) == target:
            return Writer(handle)
        return handle

    def open_descriptor(fd, mode="r", *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)
        # Restore fences use a+b; only the atomic output uses wb here.
        return Writer(handle) if mode == "wb" else handle

    monkeypatch.setattr(builtins, "open", open_file)
    monkeypatch.setattr(os, "fdopen", open_descriptor)


@pytest.mark.parametrize("kind", ["root", "agent"])
def test_failed_save_keeps_previous_config_and_can_retry(
    config_files,
    monkeypatch,
    kind,
):
    target = config_files[kind == "agent"]
    before = target.read_bytes()
    attempts = []

    def disk_full(handle, value):
        attempts.append(True)
        handle.write(value[:1])
        handle.flush()
        raise OSError(errno.ENOSPC, "injected disk full")

    with monkeypatch.context() as patch:
        _intercept_writes(patch, target, disk_full)
        with pytest.raises(OSError, match="injected disk full"):
            _save_changed(kind)

    assert attempts
    assert target.read_bytes() == before
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))
    _save_changed(kind)
    assert target.read_bytes() != before
    json.loads(target.read_bytes())


@pytest.mark.parametrize("kind", ["root", "agent"])
def test_serialization_failure_keeps_previous_config_and_can_retry(
    config_files,
    monkeypatch,
    kind,
):
    target = config_files[kind == "agent"]
    before = target.read_bytes()

    def fail_encoding(self, value, _one_shot=False):
        yield "{"
        raise TypeError("injected serialization failure")

    with monkeypatch.context() as patch:
        patch.setattr(json.JSONEncoder, "iterencode", fail_encoding)
        with pytest.raises(TypeError, match="injected serialization failure"):
            _save_changed(kind)

    assert target.read_bytes() == before
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))
    _save_changed(kind)
    assert target.read_bytes() != before
    json.loads(target.read_bytes())


@pytest.mark.parametrize("kind", ["root", "agent"])
def test_replace_failure_keeps_previous_config_and_can_retry(
    config_files,
    monkeypatch,
    kind,
):
    target = config_files[kind == "agent"]
    before = target.read_bytes()
    original_replace = os.replace

    def fail_replace(source, destination, *args, **kwargs):
        if Path(destination) == target:
            raise OSError(errno.EACCES, "injected replace failure")
        return original_replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected replace failure"):
            _save_changed(kind)

    assert target.read_bytes() == before
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))
    _save_changed(kind)
    assert target.read_bytes() != before
    json.loads(target.read_bytes())


@pytest.mark.parametrize("kind", ["root", "agent"])
def test_readers_see_complete_config_while_saving(
    config_files,
    monkeypatch,
    kind,
):
    target = config_files[kind == "agent"]
    before = target.read_bytes()
    observations = []

    def observe_write(handle, value):
        observations.append(target.read_bytes())
        assert observations[-1] == before
        return handle.write(value)

    _intercept_writes(monkeypatch, target, observe_write)
    _save_changed(kind)
    assert observations
    assert target.read_bytes() != before


@pytest.mark.parametrize("kind", ["root", "agent"])
def test_save_uses_private_mode_and_preserves_json_format(config_files, kind):
    target = config_files[kind == "agent"]
    target.chmod(0o640)
    _save_changed(kind)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    raw = target.read_bytes()
    assert "新配置".encode("utf-8") in raw
    expected = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    assert raw == expected.encode("utf-8")


@pytest.mark.parametrize("kind", ["root", "agent"])
def test_failed_startup_migration_retains_valid_config(
    config_files,
    monkeypatch,
    kind,
):
    target = config_files[kind == "agent"]
    data = json.loads(target.read_bytes())
    data["channels"] = {"weixin": {"enabled": False}}
    if kind == "agent":
        # Exercise the legacy migration sequence, including later rewrites.
        data["runtime"] = {"engine": "legacy"}
        data["runtime_migrations"] = []
        data["running"] = {
            "max_input_length": 128000,
            "llm_acquire_timeout": 300,
        }
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    before = target.read_bytes()
    attempts = []

    def disk_full(handle, value):
        attempts.append(True)
        handle.write(value[:1])
        handle.flush()
        raise OSError(errno.ENOSPC, "injected disk full")

    _intercept_writes(monkeypatch, target, disk_full)
    error = None
    try:
        if kind == "root":
            config_utils.load_config()
        else:
            config_module.load_agent_config("default")
    except (OSError, json.JSONDecodeError) as exc:
        error = exc
    assert attempts
    assert target.read_bytes() == before
    assert error is None
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))
