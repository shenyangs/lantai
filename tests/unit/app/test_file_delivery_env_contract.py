# -*- coding: utf-8 -*-
"""The runtime prompt must define completion for generated files."""

from lantai.app.runner.utils import build_env_context


def test_env_context_requires_actual_file_attachment_before_success(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "lantai.app.runner.utils.load_config",
        lambda: type("Config", (), {"user_timezone": "Asia/Shanghai"})(),
    )

    context = build_env_context(working_dir="/tmp/topic")

    assert "send_file_to_user" in context
    assert "internal path" in context.lower()
    assert "not complete" in context.lower()


def test_env_context_treats_plain_text_as_a_valid_complete_response(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "lantai.app.runner.utils.load_config",
        lambda: type("Config", (), {"user_timezone": "Asia/Shanghai"})(),
    )

    context = build_env_context(working_dir="/tmp/topic")

    assert "A text answer is a valid complete response" in context
    assert "merely to keep a turn alive" in context
    assert "must generate a tool call" not in context
