# -*- coding: utf-8 -*-
"""Behavior locks for local multimodal paths used by the desktop app."""

from __future__ import annotations

from pathlib import Path

import pytest

from lantai.agents import model_factory
from lantai.agents.legacy_model_components import (
    AnthropicChatFormatter,
    GeminiChatFormatter,
)
from lantai.agents.openai_chat_formatter import LantaiOpenAIChatFormatter
from lantai.app.channels.runtime_schema import (
    ImageContent as ConsoleImageContent,
    Message as ConsoleMessage,
    TextContent as ConsoleTextContent,
)
from lantai.runtime.converters import (
    LegacyMsg,
    legacy_query_messages_to_runtime_messages,
    runtime_messages_to_legacy_msgs,
)


def _image_message(image_path: Path) -> LegacyMsg:
    return LegacyMsg(
        name="user",
        role="user",
        content=[
            {"type": "text", "text": "请描述图片"},
            {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": image_path.as_uri(),
                },
            },
        ],
    )


@pytest.mark.asyncio
async def test_openai_formatter_accepts_encoded_local_image_uri(tmp_path: Path) -> None:
    image_path = tmp_path / "中文 目录" / "测试 图片.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"not-a-real-png-but-valid-path")

    formatted = await LantaiOpenAIChatFormatter().format(
        [_image_message(image_path)],
    )

    image_block = formatted[0]["content"][1]
    assert image_block["type"] == "image_url"
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_legacy_kimi_path_preserves_console_clipboard_image(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "clipboard.png"
    image_path.write_bytes(b"clipboard-image-bytes")
    runtime_messages = legacy_query_messages_to_runtime_messages(
        [
            ConsoleMessage(
                role="user",
                content=[
                    ConsoleTextContent(text="翻译"),
                    ConsoleImageContent(image_url=str(image_path)),
                ],
            ),
        ],
    )

    formatted = await LantaiOpenAIChatFormatter().format(
        runtime_messages_to_legacy_msgs(runtime_messages),
    )

    assert formatted[0]["content"][0] == {"type": "text", "text": "翻译"}
    assert formatted[0]["content"][1]["type"] == "image_url"
    assert formatted[0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,",
    )


@pytest.mark.asyncio
async def test_anthropic_formatter_accepts_encoded_local_image_uri(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "中文 目录" / "测试 图片.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"not-a-real-png-but-valid-path")

    formatted = await AnthropicChatFormatter().format(
        [_image_message(image_path)],
    )

    image_block = formatted[0]["content"][1]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_openai_formatter_preserves_file_name_and_infers_mime() -> None:
    message = LegacyMsg(
        name="user",
        role="user",
        content=[
            {"type": "text", "text": "请读取附件"},
            {
                "type": "file",
                "filename": "README.md",
                "source": {
                    "type": "base64",
                    "media_type": "application/octet-stream",
                    "data": "IyDmuY/lrablj7A=",
                },
            },
        ],
    )

    formatted = await LantaiOpenAIChatFormatter().format([message])

    file_block = formatted[0]["content"][1]
    assert file_block["type"] == "text"
    assert "README.md" in file_block["text"]
    assert "text/markdown" in file_block["text"]
    assert "# 湏学台" in file_block["text"]
    assert "data:text/markdown;base64," not in file_block["text"]


@pytest.mark.asyncio
async def test_openai_formatter_inlines_text_attachment_content() -> None:
    message = LegacyMsg(
        name="user",
        role="user",
        content=[
            {"type": "text", "text": "总结附件"},
            {
                "type": "file",
                "filename": "notes.txt",
                "source": {
                    "type": "base64",
                    "media_type": "text/plain",
                    "data": "SGVsbG8gZnJvbSBhdHRhY2htZW50Lg==",
                },
            },
        ],
    )

    formatted = await LantaiOpenAIChatFormatter().format([message])

    file_text = formatted[0]["content"][1]["text"]
    assert "Hello from attachment." in file_text
    assert "notes.txt" in file_text
    assert "data:text/plain;base64" not in file_text


def _text_file_message() -> LegacyMsg:
    return LegacyMsg(
        name="user",
        role="user",
        content=[
            {"type": "text", "text": "读取"},
            {
                "type": "file",
                "filename": "provider-notes.txt",
                "source": {
                    "type": "base64",
                    "media_type": "text/plain",
                    "data": "5r6c5Y+w5paH5qGj5qCH6K6w77ya5rWB5YWJ5a65",
                },
            },
        ],
    )


@pytest.mark.asyncio
async def test_anthropic_formatter_inlines_readable_file_content() -> None:
    formatted = await AnthropicChatFormatter().format([_text_file_message()])

    file_block = formatted[0]["content"][1]
    assert file_block["type"] == "text"
    assert "provider-notes.txt" in file_block["text"]
    assert "澜台文档标记：流光容" in file_block["text"]


@pytest.mark.asyncio
async def test_gemini_formatter_inlines_readable_file_content() -> None:
    formatted = await GeminiChatFormatter().format([_text_file_message()])

    file_part = formatted[0]["parts"][1]
    assert "provider-notes.txt" in file_part["text"]
    assert "澜台文档标记：流光容" in file_part["text"]


@pytest.mark.asyncio
async def test_every_provider_formatter_keeps_exact_local_file_handoff(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "中文 目录" / "客户文件（A B）.txt"
    attachment.parent.mkdir()
    attachment.write_text(
        "CROSS_PROVIDER_ATTACHMENT_SENTINEL 本轮原文",
        encoding="utf-8",
    )
    message = LegacyMsg(
        name="user",
        role="user",
        content=[
            {"type": "text", "text": "请直接读取附件"},
            {
                "type": "file",
                "filename": "用户显示名（终版 A B）.txt",
                "source": {
                    "type": "url",
                    "url": str(attachment.resolve()),
                },
            },
        ],
    )
    anthropic_formatter = model_factory._create_file_block_support_formatter(  # noqa: SLF001
        AnthropicChatFormatter,
    )()

    openai_payload = await LantaiOpenAIChatFormatter().format([message])
    gemini_payload = await GeminiChatFormatter().format([message])
    anthropic_payload = await anthropic_formatter.format([message])
    projections = [
        openai_payload[0]["content"][1]["text"],
        gemini_payload[0]["parts"][1]["text"],
        anthropic_payload[0]["content"][1]["text"],
    ]

    for projection in projections:
        assert "用户显示名（终版 A B）.txt" in projection
        assert str(attachment.resolve()) in projection
        assert "CROSS_PROVIDER_ATTACHMENT_SENTINEL 本轮原文" in projection
