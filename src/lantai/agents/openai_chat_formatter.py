# -*- coding: utf-8 -*-
"""Lantai-owned OpenAI-compatible formatter for legacy agent messages."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from copy import deepcopy
from typing import Any, List, Sequence, Tuple, Union
from urllib.parse import unquote, urlparse

import requests

from ..runtime.attachment_context import attachment_context_text
from ..runtime.instruction_trust import (
    project_untrusted_tool_evidence,
    untrusted_tool_evidence_notice,
)


class _FormatterMessage:
    def __init__(self, *, name: str, role: str, content: Any) -> None:
        self.name = name
        self.role = role
        self.content = content

    def get_content_blocks(self) -> list[dict[str, Any]]:
        if isinstance(self.content, str):
            return [{"type": "text", "text": self.content}]
        if isinstance(self.content, list):
            return [
                block
                for block in self.content
                if isinstance(block, dict)
            ]
        return [{"type": "text", "text": str(self.content)}]


def _save_base64_data(media_type: str, base64_data: str) -> str:
    extension = "." + media_type.split("/")[-1]
    with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as temp_file:
        temp_file.write(base64.b64decode(base64_data))
        return temp_file.name


def _to_openai_image_url(url: str) -> str:
    try:
        import filetype
    except ImportError:  # pragma: no cover - optional dependency fallback
        filetype = None

    support_image_extensions = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    )

    parsed_url = urlparse(url)
    if parsed_url.scheme == "file":
        raw_url = unquote(parsed_url.path)
    elif parsed_url.scheme == "":
        raw_url = unquote(url)
    else:
        raw_url = url
    if os.path.exists(raw_url) and os.path.isfile(raw_url):
        if any(raw_url.endswith(extension) for extension in support_image_extensions):
            with open(raw_url, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode("utf-8")
            extension = raw_url.lower().split(".")[-1]
            mime_type = f"image/{extension}"
            return f"data:{mime_type};base64,{base64_image}"

        if filetype is not None:
            kind = filetype.guess(raw_url)
            if kind is not None and kind.mime.startswith("image/"):
                with open(raw_url, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode(
                        "utf-8",
                    )
                return f"data:{kind.mime};base64,{base64_image}"

    if parsed_url.scheme not in ["", "file"]:
        return url

    raise ValueError(
        f'Invalid image URL: "{url}". It should be a local file or a web URL.',
    )


def _format_openai_image_block(image_block: dict[str, Any]) -> dict[str, Any]:
    source = image_block["source"]
    if source["type"] == "url":
        url = _to_openai_image_url(source["url"])
    elif source["type"] == "base64":
        data = source["data"]
        media_type = source["media_type"]
        url = f"data:{media_type};base64,{data}"
    else:
        raise ValueError(f"Unsupported image source type: {source['type']}")

    return {
        "type": "image_url",
        "image_url": {"url": url},
    }


def _format_openai_file_block(file_block: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "text",
        "text": attachment_context_text(file_block),
    }


def _to_openai_audio_data(source: dict[str, Any]) -> dict[str, str]:
    if source["type"] == "url":
        raw_url = source["url"].removeprefix("file://")
        extension = raw_url.split(".")[-1].lower()
        if extension not in ["wav", "mp3"]:
            raise TypeError(
                f"Unsupported audio file extension: {extension}, "
                "wav and mp3 are supported.",
            )

        parsed_url = urlparse(raw_url)
        if os.path.exists(raw_url):
            with open(raw_url, "rb") as audio_file:
                data = base64.b64encode(audio_file.read()).decode("utf-8")
        elif parsed_url.scheme not in ["", "file"]:
            response = requests.get(source["url"], timeout=30)
            response.raise_for_status()
            data = base64.b64encode(response.content).decode("utf-8")
        else:
            raise ValueError(
                f"Unsupported audio source: {source['url']}, "
                "it should be a local file or a web URL.",
            )

        return {"data": data, "format": extension}

    if source["type"] == "base64":
        data = source["data"]
        media_type = source["media_type"]
        if media_type not in ["audio/wav", "audio/mp3"]:
            raise TypeError(
                f"Unsupported audio media type: {media_type}, "
                "only audio/wav and audio/mp3 are supported.",
            )
        return {"data": data, "format": media_type.split("/")[-1]}

    raise TypeError(f"Unsupported audio source: {source['type']}.")


class LantaiOpenAIChatFormatter:
    """OpenAI-compatible formatter independent from provider framework classes."""

    support_tools_api: bool = True
    support_multiagent: bool = True
    support_vision: bool = True
    supported_blocks: list[type] = []

    def __init__(
        self,
        promote_tool_result_images: bool = False,
        token_counter: Any | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.promote_tool_result_images = promote_tool_result_images
        self.token_counter = token_counter
        self.max_tokens = max_tokens

    async def format(self, msgs: list[Any], **_kwargs: Any) -> list[dict[str, Any]]:
        self.assert_list_of_msgs(msgs)
        return await self._format(deepcopy(msgs))

    @staticmethod
    def assert_list_of_msgs(msgs: list[Any]) -> None:
        if not isinstance(msgs, list):
            raise TypeError("Input must be a list of message objects.")

        for msg in msgs:
            if not hasattr(msg, "get_content_blocks"):
                raise TypeError(
                    f"Expected message object, got {type(msg)} instead.",
                )

    @staticmethod
    def convert_tool_result_to_string(
        output: Union[str, List[dict[str, Any]]],
    ) -> tuple[str, Sequence[Tuple[str, dict[str, Any]]]]:
        if isinstance(output, str):
            return output, []

        textual_output: list[str] = []
        multimodal_data: list[tuple[str, dict[str, Any]]] = []
        for block in output:
            if not isinstance(block, dict) or "type" not in block:
                raise ValueError(
                    f"Invalid block: {block}, a text/image/audio/video block "
                    "is expected.",
                )
            if block["type"] == "text":
                textual_output.append(block["text"])
            elif block["type"] in ["image", "audio", "video"]:
                if "source" not in block:
                    raise ValueError(
                        f"Invalid {block['type']} block: {block}, "
                        "'source' key is required.",
                    )
                source = block["source"]
                if source["type"] == "url":
                    path_multimodal_file = source["url"]
                elif source["type"] == "base64":
                    path_multimodal_file = _save_base64_data(
                        source["media_type"],
                        source["data"],
                    )
                else:
                    raise ValueError(
                        f"Invalid media source: {block['source']}, "
                        "expected 'url' or 'base64'.",
                    )
                textual_output.append(
                    f"The returned {block['type']} can be found "
                    f"at: {path_multimodal_file}",
                )
                multimodal_data.append((path_multimodal_file, block))
            elif block["type"] == "file":
                textual_output.append(_format_openai_file_block(block)["text"])
            else:
                raise ValueError(
                    f"Unsupported block type: {block['type']}, "
                    "expected 'text', 'image', 'audio', 'video', or 'file'.",
                )

        if len(textual_output) == 1:
            return textual_output[0], multimodal_data
        return "\n".join("- " + text for text in textual_output), multimodal_data

    async def _format(self, msgs: list[Any]) -> list[dict[str, Any]]:
        self.assert_list_of_msgs(msgs)

        messages: list[dict[str, Any]] = []
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            content_blocks: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []

            for block in msg.get_content_blocks():
                typ = block.get("type")
                if typ == "text":
                    content_blocks.append({**block})
                elif typ == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(
                                    block.get("input", {}),
                                    ensure_ascii=False,
                                ),
                            },
                        },
                    )
                elif typ == "tool_result":
                    textual_output, multimodal_data = (
                        self.convert_tool_result_to_string(block["output"])
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("id"),
                            "content": project_untrusted_tool_evidence(
                                str(block.get("name") or ""),
                                textual_output,
                            ),
                            "name": block.get("name"),
                        },
                    )

                    promoted_blocks: list[dict[str, Any]] = []
                    for url, multimodal_block in multimodal_data:
                        if (
                            multimodal_block["type"] == "image"
                            and self.promote_tool_result_images
                        ):
                            promoted_blocks.extend(
                                [
                                    {
                                        "type": "text",
                                        "text": f"\n- The image from '{url}': ",
                                    },
                                    {
                                        "type": "image",
                                        "source": {"type": "url", "url": url},
                                    },
                                ],
                            )

                    if promoted_blocks:
                        promoted_blocks = [
                            {
                                "type": "text",
                                "text": (
                                    untrusted_tool_evidence_notice(
                                        str(block.get("name") or ""),
                                    )
                                    + "\nThe following media came from this "
                                    "tool result:"
                                ),
                            },
                            *promoted_blocks,
                            {
                                "type": "text",
                                "text": "[END_LANTAI_TOOL_EVIDENCE]",
                            },
                        ]
                        msgs.insert(
                            i + 1,
                            _FormatterMessage(
                                name="user",
                                content=promoted_blocks,
                                role="user",
                            ),
                        )
                elif typ == "image":
                    content_blocks.append(_format_openai_image_block(block))
                elif typ == "file":
                    content_blocks.append(_format_openai_file_block(block))
                elif typ == "audio":
                    if getattr(msg, "role", None) == "assistant":
                        continue
                    content_blocks.append(
                        {
                            "type": "input_audio",
                            "input_audio": _to_openai_audio_data(block["source"]),
                        },
                    )

            msg_openai: dict[str, Any] = {
                "role": msg.role,
                "name": msg.name,
                "content": content_blocks or None,
            }
            if tool_calls:
                msg_openai["tool_calls"] = tool_calls

            if msg_openai["content"] or msg_openai.get("tool_calls"):
                messages.append(msg_openai)

            i += 1

        return messages


__all__ = ["LantaiOpenAIChatFormatter"]
