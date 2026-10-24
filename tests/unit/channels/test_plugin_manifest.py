# -*- coding: utf-8 -*-
"""Contract tests for declarative channel plugins."""

from __future__ import annotations

import json
import sys

import pytest
from pydantic import ValidationError

from lantai.app.channels import registry
from lantai.app.channels.plugin_manifest import ChannelPluginManifest


def _manifest(**overrides):
    result = {
        "schemaVersion": 1,
        "key": "demo_plugin",
        "name": "Demo Plugin",
        "module": "channel",
        "className": "DemoChannel",
        "description": "A test transport",
        "requires": [],
        "configFields": [
            {
                "name": "token",
                "label": "Token",
                "type": "password",
                "required": True,
                "secret": True,
            },
        ],
        "capabilities": {
            "inboundMedia": ["text", "image"],
            "outboundMedia": ["text"],
            "streaming": True,
        },
    }
    result.update(overrides)
    return result


def test_manifest_rejects_path_traversal_module():
    with pytest.raises(ValidationError):
        ChannelPluginManifest.model_validate(
            _manifest(module="../../untrusted"),
        )


def test_manifest_plugin_is_loaded_and_described(tmp_path, monkeypatch):
    package = tmp_path / "demo_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "channel.json").write_text(
        json.dumps(_manifest()),
        encoding="utf-8",
    )
    (package / "channel.py").write_text(
        """
from lantai.app.channels.base import BaseChannel

class DemoChannel(BaseChannel):
    channel = "demo_plugin"
    async def start(self): pass
    async def stop(self): pass
    async def send(self, to_handle, text, meta=None): pass
    def build_agent_request_from_native(self, native_payload):
        raise NotImplementedError
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "CUSTOM_CHANNELS_DIR", tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("demo_package", None)
    sys.modules.pop("demo_package.channel", None)

    channels = registry._discover_custom_channels()
    descriptors = registry.get_channel_descriptors()

    assert channels["demo_plugin"].capabilities.streaming is True
    descriptor = next(item for item in descriptors if item["key"] == "demo_plugin")
    assert descriptor["available"] is True
    assert descriptor["configFields"][0]["secret"] is True
    assert descriptor["capabilities"]["inboundMedia"] == ["text", "image"]


def test_manifest_missing_requirement_remains_configurable(
    tmp_path,
    monkeypatch,
):
    package = tmp_path / "missing_package"
    package.mkdir()
    (package / "channel.json").write_text(
        json.dumps(
            _manifest(
                key="missing_plugin",
                requires=["definitely_missing_lantai_test_dependency"],
            ),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "CUSTOM_CHANNELS_DIR", tmp_path)

    assert "missing_plugin" in registry.get_configurable_channel_keys()
    descriptor = next(
        item
        for item in registry.get_channel_descriptors()
        if item["key"] == "missing_plugin"
    )
    assert descriptor["available"] is False
