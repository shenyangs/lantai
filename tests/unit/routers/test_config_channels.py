# -*- coding: utf-8 -*-
"""Tests for channel configuration endpoints."""

from lantai.app.channels.registry import (
    BUILTIN_CHANNEL_ORDER,
    get_configurable_channel_keys,
)
from lantai.app.routers import config as config_router
from lantai.config.config import (
    ChannelConfigUnion,
    OneBotConfig,
    SlackConfig,
    YuanbaoConfig,
)


def test_configurable_channels_include_all_builtin_channels_first():
    keys = get_configurable_channel_keys()

    assert keys[: len(BUILTIN_CHANNEL_ORDER)] == BUILTIN_CHANNEL_ORDER
    assert "dingtalk" in keys
    assert "feishu" in keys
    assert "wecom" in keys
    assert "wechat" in keys
    assert "voice" in keys


async def test_list_channel_types_uses_configurable_channels(monkeypatch):
    monkeypatch.setattr(
        config_router,
        "get_configurable_channel_keys",
        lambda: ("console", "dingtalk", "feishu", "wecom"),
    )

    assert await config_router.list_channel_types() == [
        "console",
        "dingtalk",
        "feishu",
        "wecom",
    ]


async def test_list_channel_descriptors_uses_registry(monkeypatch):
    expected = [{"key": "console", "isBuiltin": True}]
    monkeypatch.setattr(
        config_router,
        "get_channel_descriptors",
        lambda: expected,
    )

    assert await config_router.list_channel_descriptors() == expected


def test_onebot_config_is_part_of_channel_config_union():
    assert OneBotConfig in ChannelConfigUnion.__args__


def test_new_channel_configs_are_part_of_channel_config_union():
    assert SlackConfig in ChannelConfigUnion.__args__
    assert YuanbaoConfig in ChannelConfigUnion.__args__
