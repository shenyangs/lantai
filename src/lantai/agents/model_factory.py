# -*- coding: utf-8 -*-
"""Factory for creating chat models and formatters.

This module provides a unified factory for creating chat model instances
and their corresponding formatters based on configuration.

Example:
    >>> from lantai.agents.model_factory import create_model_and_formatter
    >>> model, formatter = create_model_and_formatter()
"""


import base64
import logging
import os
from typing import List, Sequence, Tuple, Any, Union, Optional
from urllib.parse import urlparse

from .openai_chat_formatter import LantaiOpenAIChatFormatter
from .utils.message_request_normalizer import (
    normalize_messages_for_model_request,
)
from ..exceptions import ProviderError, ModelFormatterError
from ..providers import ProviderManager
from ..providers.tokendance_authorization import (
    resolve_tokendance_authorization,
)
from ..providers.retry_chat_model import (
    RetryChatModel,
    RetryConfig,
    RateLimitConfig,
)
from ..runtime.adapters.legacy_model_client import LegacyModelClient
from ..runtime.adapters.openai_model_client import OpenAICompatibleModelClient
from ..runtime.adapters.openai_responses_model_client import (
    OpenAIResponsesModelClient,
)
from ..runtime.attachment_context import attachment_context_text
from ..runtime.model_normalizer import (
    MediaPayloadBoundary,
    file_url_to_path as _runtime_file_url_to_path,
    validate_media_payload_boundary,
)
from ..runtime.model_wrappers import (
    ModelAccessFailoverClient,
    RetryingModelClient,
    TokenDanceBoundModelClient,
    TokenRecordingModelClient,
)
from ..runtime.models import LantaiModelClient
from .routing_chat_model import (
    RoutingLantaiModelClient,
    RoutingModelClientEndpoint,
)
from ..token_usage import TokenRecordingModelWrapper


def _file_url_to_path(url: str) -> str:
    """
    Strip file:// to path. On Windows file:///C:/path -> C:/path not /C:/path.
    Percent-decodes the path so non-ASCII filenames resolve correctly.
    """
    return _runtime_file_url_to_path(url)


logger = logging.getLogger(__name__)

_SUPPORTED_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_SUPPORTED_VIDEO_EXTENSIONS: dict[str, str] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mpeg": "video/mpeg",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}


def _supports_multimodal_for_current_model() -> bool:
    """Best-effort lookup of current model multimodal support."""
    try:
        from .prompt import get_active_model_supports_multimodal

        return get_active_model_supports_multimodal()
    except Exception:  # pragma: no cover - config lookup safety
        logger.debug(
            "Falling back to multimodal=False during request-time "
            "message normalization",
            exc_info=True,
        )
        return False


def _agent_config_provider_error(
    agent_id: str,
    exc: Exception,
) -> ProviderError:
    return ProviderError(
        message=f"Failed to load agent model config for '{agent_id}'.",
        details={"agent_id": agent_id, "error_type": type(exc).__name__},
    )


def _class_family_names(cls: type[Any]) -> set[str]:
    """Return class names across the MRO without importing legacy classes."""

    return {base.__name__ for base in getattr(cls, "__mro__", (cls,))}


def _normalize_messages_for_formatter(
    msgs: list,
    base_formatter_class: type[Any],
    formatter_instance: Any | None = None,
) -> tuple[list, bool, bool]:
    """Return normalized messages and formatter-family flags.

    The returned booleans are
    ``(is_anthropic_formatter, is_gemini_formatter)``.
    All formatters receive a copied, normalized message list so
    request-time repair does not mutate stored history.
    """
    formatter_family_names = _class_family_names(base_formatter_class)
    is_anthropic_formatter = "AnthropicChatFormatter" in formatter_family_names
    is_gemini_formatter = "GeminiChatFormatter" in formatter_family_names
    supports_multimodal = getattr(
        formatter_instance, "_lantai_supports_multimodal", None,
    )
    if not isinstance(supports_multimodal, bool):
        supports_multimodal = _supports_multimodal_for_current_model()
    if getattr(formatter_instance, "_lantai_force_strip_media", False):
        supports_multimodal = False

    if is_anthropic_formatter:
        target_family = "anthropic"
    elif is_gemini_formatter:
        target_family = "gemini"
    else:
        target_family = "openai"

    normalized_msgs = normalize_messages_for_model_request(
        msgs,
        supports_multimodal=supports_multimodal,
        target_family=target_family,
    )

    return normalized_msgs, is_anthropic_formatter, is_gemini_formatter


# TODO: remove after the legacy Anthropic formatter catches up.
def _format_anthropic_media_block(block: dict) -> dict:
    """Format an image or video block for Anthropic API.

    If the source is a URLSource pointing to a local file it will be
    converted to base64.  Web URLs are passed through as-is.

    Args:
        block (`dict`):
            A block dict with ``type`` of ``"image"`` or ``"video"``.

    Returns:
        `dict`: Formatted block for the Anthropic API.

    Raises:
        `ModelFormatterError`:
            If the source type or media format is not supported.
    """
    typ = block["type"]
    validate_media_payload_boundary(str(typ), block)
    extensions = (
        _SUPPORTED_IMAGE_EXTENSIONS
        if typ == "image"
        else _SUPPORTED_VIDEO_EXTENSIONS
    )

    source = block["source"]

    if source["type"] == "base64":
        return {**block}

    url = source["url"]
    raw_url = _file_url_to_path(url)

    if os.path.exists(raw_url) and os.path.isfile(raw_url):
        ext = os.path.splitext(raw_url)[1].lower()
        media_type = extensions.get(ext)
        if media_type:
            with open(raw_url, "rb") as f:
                data = base64.b64encode(f.read()).decode(
                    "utf-8",
                )
            return {
                "type": typ,
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }

    parsed_url = urlparse(raw_url)
    if parsed_url.scheme not in ("", "file"):
        return {
            "type": typ,
            "source": {
                "type": "url",
                "url": url,
            },
        }

    raise ModelFormatterError(
        message=(
            f'Invalid {typ} URL: "{url}". '
            "It should be a local file or a web URL."
        ),
    )


def _format_openai_video_block(video_block: dict) -> dict:
    """Format a video block for OpenAI-compatible API.

    Local files are converted to base64 data URLs; web URLs are
    passed through directly.

    Args:
        video_block (`dict`):
            The video block to format.

    Returns:
        `dict`:
            ``{"type": "video_url", "video_url": {"url": ...}}``.

    Raises:
        `ValueError`:
            If the source type or video format is not supported.
    """
    validate_media_payload_boundary("video", video_block)
    source = video_block["source"]
    if source["type"] == "base64":
        media_type = source["media_type"]
        url = f"data:{media_type};base64,{source['data']}"
    elif source["type"] == "url":
        raw_url = _file_url_to_path(source["url"])
        if os.path.exists(raw_url) and os.path.isfile(raw_url):
            ext = os.path.splitext(raw_url)[1].lower()
            media_type = _SUPPORTED_VIDEO_EXTENSIONS.get(ext)
            if not media_type:
                raise ModelFormatterError(
                    f"Unsupported video extension: {ext}",
                )
            with open(raw_url, "rb") as f:
                data = base64.b64encode(
                    f.read(),
                ).decode("utf-8")
            url = f"data:{media_type};base64,{data}"
        else:
            parsed = urlparse(raw_url)
            if parsed.scheme not in ("", "file"):
                url = source["url"]
            else:
                raise ModelFormatterError(
                    message=(
                        f"Invalid video URL: {source['url']}. "
                        "It should be a local file or a web URL."
                    ),
                )
    else:
        raise ModelFormatterError(
            message=f"Unsupported video source type: {source['type']}",
        )

    return {
        "type": "video_url",
        "video_url": {"url": url},
    }


def _replace_video_placeholders(
    messages: list[dict],
    video_subs: dict[str, dict],
) -> None:
    """Replace video placeholder text blocks with formatted
    video blocks in OpenAI-formatted messages."""
    for fmt_msg in messages:
        content = fmt_msg.get("content")
        if not isinstance(content, list):
            continue
        new_content = []
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and item.get("text") in video_subs
            ):
                new_content.append(
                    _format_openai_video_block(
                        video_subs[item["text"]],
                    ),
                )
            else:
                new_content.append(item)
        fmt_msg["content"] = new_content


def _media_source_key(block: dict) -> str | None:
    """Extract a normalised path/URL from a media block for deduplication.

    Returns ``None`` for base64 sources (nothing to compare) or if no
    usable source URL is present.
    """
    source = block.get("source", {})
    if source.get("type") == "base64":
        return None
    url = source.get("url", "")
    if not url:
        return None
    raw = _file_url_to_path(url)
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return url


def _format_anthropic_output_items(
    output: list,
    seen_media: set[str] | None = None,
) -> list:
    """Format a list of tool_result output blocks for Anthropic API,
    converting image, video, and file blocks as needed.

    When *seen_media* is provided, media blocks whose source has already
    been encoded in a preceding top-level block are replaced with a
    lightweight text placeholder to avoid duplicating large base64 data.
    """
    result: list[dict] = []
    for item in output:
        item_type = item.get("type")

        if item_type == "file":
            # Anthropic tool_result content only supports 'text' and 'image';
            # convert file blocks to a readable text placeholder so the
            # conversation history stays intact without triggering a 400 error.
            source = item.get("source", {})
            file_url = source.get("url", "")
            filename = (
                item.get("filename")
                or file_url.rsplit("/", 1)[-1]
                or "unknown"
            )
            readable_path = file_url.removeprefix("file://")
            result.append(
                {
                    "type": "text",
                    "text": f"File '{filename}' is available at:"
                    f" {readable_path}",
                },
            )
            continue

        if item_type not in ("image", "video"):
            result.append(item)
            continue

        key = _media_source_key(item)
        if key and seen_media is not None and key in seen_media:
            result.append(
                {
                    "type": "text",
                    "text": (
                        f"[{item['type'].title()} omitted — same "
                        f"{item['type']} already visible above]"
                    ),
                },
            )
        else:
            result.append(_format_anthropic_media_block(item))
            if key and seen_media is not None:
                seen_media.add(key)

    return result


# TODO: remove after the legacy Anthropic formatter catches up.
def _format_anthropic_messages(  # pylint: disable=too-many-branches
    msgs: list,
) -> list[dict]:
    """Format messages for Anthropic API with image/video block support.

    This replaces the default ``AnthropicChatFormatter._format`` so that
    ``_format_anthropic_media_block`` is applied to both top-level media
    blocks and media blocks nested inside ``tool_result`` outputs.

    A ``seen_media`` set tracks image/video source paths already encoded
    in top-level blocks.  When the same media appears inside a
    ``tool_result`` output (e.g. ``view_image`` called on an
    already-uploaded photo), it is replaced with a lightweight text
    placeholder to avoid duplicating large base64 payloads.
    """
    messages: list[dict] = []
    seen_media: set[str] = set()
    for index, msg in enumerate(msgs):
        content_blocks: list[dict] = []

        for block in msg.get_content_blocks():
            typ = block.get("type")
            if typ == "thinking":
                if block.get("provider_block_type") == "redacted_thinking":
                    content_blocks.append(
                        {
                            "type": "redacted_thinking",
                            "data": block.get("data", ""),
                        },
                    )
                else:
                    content_blocks.append(
                        {
                            key: value
                            for key, value in block.items()
                            if key != "provider_block_type"
                        },
                    )
            elif typ == "text":
                content_blocks.append({**block})

            elif typ in ("image", "video"):
                key = _media_source_key(block)
                if key:
                    seen_media.add(key)
                content_blocks.append(
                    _format_anthropic_media_block(block),
                )

            elif typ == "file":
                # Generic documents are not native Anthropic media blocks.
                # Project them into bounded text while retaining the canonical
                # local path.  The custom Anthropic formatter replaces the
                # base formatter entirely, so omitting this branch silently
                # discarded top-level Console attachments before MiniMax and
                # other Anthropic-compatible providers saw the user turn.
                content_blocks.append(
                    {
                        "type": "text",
                        "text": attachment_context_text(block),
                    },
                )

            elif typ == "tool_use":
                content_blocks.append(
                    {
                        "id": block.get("id"),
                        "type": "tool_use",
                        "name": block.get("name"),
                        "input": block.get("input", {}),
                    },
                )

            elif typ == "tool_result":
                output = block.get("output")
                if output is None:
                    content_value: list = [
                        {"type": "text", "text": ""},
                    ]
                elif isinstance(output, list):
                    content_value = _format_anthropic_output_items(
                        output,
                        seen_media,
                    )
                else:
                    content_value = [
                        {"type": "text", "text": str(output)},
                    ]
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "content": content_value,
                            },
                        ],
                    },
                )

        if msg.role == "system" and index != 0:
            role = "user"
        else:
            role = msg.role

        msg_anthropic: dict = {
            "role": role,
            "content": content_blocks or "",
        }

        if msg_anthropic["content"] or msg_anthropic.get(
            "tool_calls",
        ):
            messages.append(msg_anthropic)

    return messages


def _get_formatter_for_chat_model(
    chat_model_class: type[Any],
) -> type[Any]:
    """Get the appropriate formatter class for a chat model.

    Args:
        chat_model_class: The chat model class

    Returns:
        Corresponding formatter class, defaults to LantaiOpenAIChatFormatter
    """
    chat_model_name = chat_model_class.__name__
    if chat_model_name == "AnthropicChatModel":
        from .legacy_model_components import AnthropicChatFormatter

        if AnthropicChatFormatter is not None:
            return AnthropicChatFormatter
    if chat_model_name == "GeminiChatModel":
        from .legacy_model_components import GeminiChatFormatter

        if GeminiChatFormatter is not None:
            return GeminiChatFormatter
    return LantaiOpenAIChatFormatter


def _substitute_video_blocks(
    msgs: list,
) -> dict[str, dict]:
    """Replace video blocks in msgs with text placeholders.

    Returns a mapping from placeholder text to the original video
    block so they can be restored later.
    """
    video_subs: dict[str, dict] = {}
    for msg in msgs:
        if not isinstance(msg.content, list):
            continue
        for i, blk in enumerate(msg.content):
            if isinstance(blk, dict) and blk.get("type") == "video":
                ph = f"__LANTAI_VID_{id(blk)}__"
                video_subs[ph] = blk
                msg.content[i] = {
                    "type": "text",
                    "text": ph,
                }
    return video_subs


def _restore_video_blocks(
    msgs: list,
    video_subs: dict[str, dict],
) -> None:
    """Restore original video blocks in msgs after formatting."""
    for msg in msgs:
        if not isinstance(msg.content, list):
            continue
        for i, blk in enumerate(msg.content):
            if (
                isinstance(blk, dict)
                and blk.get("type") == "text"
                and blk.get("text") in video_subs
            ):
                msg.content[i] = video_subs[blk["text"]]


def _promote_tool_result_videos(
    msgs: list,
    messages: list[dict],
) -> list[dict]:
    """Inject promoted video user messages after tool result messages.

    Mirrors the image promotion that the legacy formatter does
    for ``promote_tool_result_images``, but for video blocks.
    """
    promotions: dict[str, tuple[str, list]] = {}
    for msg in msgs:
        for block in msg.get_content_blocks():
            if block.get("type") != "tool_result":
                continue
            output = block.get("output")
            if not isinstance(output, list):
                continue
            videos = [
                (
                    item.get("source", {}).get("url", ""),
                    item,
                )
                for item in output
                if isinstance(item, dict) and item.get("type") == "video"
            ]
            if videos:
                promotions[block.get("id")] = (
                    block.get("name", ""),
                    videos,
                )

    if not promotions:
        return messages

    new_messages: list[dict] = []
    for fmt_msg in messages:
        new_messages.append(fmt_msg)
        tcid = fmt_msg.get("tool_call_id")
        if tcid not in promotions:
            continue
        tool_name, videos = promotions[tcid]
        promoted: list[dict] = [
            {
                "type": "text",
                "text": "<system-info>The following are "
                "the video contents from the tool "
                f"result of '{tool_name}':",
            },
        ]
        for url, vid_block in videos:
            promoted.append(
                {
                    "type": "text",
                    "text": f"\n- The video from '{url}': ",
                },
            )
            promoted.append(
                _format_openai_video_block(vid_block),
            )
        promoted.append(
            {"type": "text", "text": "</system-info>"},
        )
        new_messages.append(
            {"role": "user", "content": promoted},
        )
    return new_messages


def _reorder_tool_and_promoted_messages(
    messages: list[dict],
) -> list[dict]:
    """Move promoted user messages after all tool results in a sequence.

    When ``promote_tool_result_images`` is True the upstream formatter
    inserts a ``role=user`` message after each ``role=tool`` message to
    carry the promoted image.  The OpenAI / Anthropic APIs require all
    tool-result messages to appear contiguously after the assistant
    message.  This helper collects the interleaved user messages and
    appends them after the last tool message in each sequence.
    """
    result: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            result.append(msg)
            i += 1
            tool_msgs: list[dict] = []
            promoted_msgs: list[dict] = []
            while i < len(messages) and messages[i].get("role") in (
                "tool",
                "user",
            ):
                if messages[i]["role"] == "tool":
                    tool_msgs.append(messages[i])
                else:
                    promoted_msgs.append(messages[i])
                i += 1
            result.extend(tool_msgs)
            result.extend(promoted_msgs)
        else:
            result.append(msg)
            i += 1
    return result


# Mapping of non-standard MIME subtypes to their correct forms.
_MIME_FIXES: dict[str, str] = {
    "image/jpg": "image/jpeg",
}


def _fix_image_mime_types(messages: list[dict]) -> None:
    """Fix non-standard MIME types in base64 data URLs in-place.

    The legacy formatter derives MIME from the file extension literally
    (e.g. ``.jpg`` → ``image/jpg``), but ``image/jpg`` is not a
    valid IANA MIME type — the correct form is ``image/jpeg``.
    Some APIs (Bedrock via litellm) reject the non-standard form.
    """
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            url = (block.get("image_url") or {}).get("url", "")
            for wrong, right in _MIME_FIXES.items():
                if url.startswith(f"data:{wrong};"):
                    block["image_url"]["url"] = url.replace(
                        f"data:{wrong};",
                        f"data:{right};",
                        1,
                    )


_MEDIA_BLOCK_TYPES = ("image", "audio", "video")


def _fixup_media_list(items: list) -> None:
    """Normalize media blocks in a list in-place.

    - Strips ``file://`` prefixes from source URLs.
    - Replaces media blocks whose local file no longer exists with
      a text placeholder so the downstream formatter won't throw.
    - Recurses into ``tool_result`` output lists.
    """
    for i, block in enumerate(items):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in _MEDIA_BLOCK_TYPES:
            source = block.get("source")
            if not (
                isinstance(source, dict)
                and source.get("type") == "url"
                and isinstance(source.get("url"), str)
            ):
                continue
            if source["url"].startswith("file://"):
                source["url"] = _file_url_to_path(source["url"])
            url = source["url"]
            if not url.startswith(
                ("http://", "https://", "data:"),
            ) and not os.path.exists(url):
                logger.warning(
                    "Media file no longer exists, "
                    "replacing with placeholder: %s",
                    url,
                )
                items[i] = {
                    "type": "text",
                    "text": (
                        f"[{btype.title()} unavailable"
                        f" — file deleted from disk]"
                    ),
                }
        elif btype == "tool_result":
            output = block.get("output")
            if isinstance(output, list):
                _fixup_media_list(output)


def _enforce_media_boundaries_in_items(
    items: list,
    boundary: MediaPayloadBoundary,
) -> None:
    for block in items:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in _MEDIA_BLOCK_TYPES:
            validate_media_payload_boundary(str(block_type), block, boundary)
            continue
        if block_type == "tool_result" and isinstance(
            block.get("output"),
            list,
        ):
            _enforce_media_boundaries_in_items(block["output"], boundary)


def _enforce_message_media_boundaries(msgs: list) -> None:
    boundary = MediaPayloadBoundary()
    for msg in msgs:
        if isinstance(getattr(msg, "content", None), list):
            _enforce_media_boundaries_in_items(msg.content, boundary)


# pylint: disable-next=too-many-statements
def _create_file_block_support_formatter(
    base_formatter_class: type[Any],
) -> type[Any]:
    """Create a formatter class with file block support.

    This factory function extends any Formatter class to support file blocks
    in tool results, which are not natively supported by the legacy formatter.

    Args:
        base_formatter_class: Base formatter class to extend

    Returns:
        Enhanced formatter class with file block support
    """

    class FileBlockSupportFormatter(base_formatter_class):
        """Formatter with file block support for tool results."""

        # pylint: disable=too-many-branches
        async def _format(self, msgs):
            """Override to sanitize tool messages, handle thinking blocks,
            and relay ``extra_content`` (Gemini thought_signature).

            This prevents OpenAI API errors from improperly paired
            tool messages, preserves reasoning_content from "thinking"
            blocks that the base formatter skips, and ensures
            ``extra_content`` on tool_use blocks (e.g. Gemini
            thought_signature) is carried through to the API request.
            """
            (
                normalized_msgs,
                is_anthropic_formatter,
                _is_gemini_formatter,
            ) = _normalize_messages_for_formatter(
                msgs,
                base_formatter_class,
                self,
            )
            _enforce_message_media_boundaries(normalized_msgs)

            reasoning_contents = {}
            extra_contents: dict[str, Any] = {}
            for msg in normalized_msgs:
                if msg.role != "assistant":
                    continue
                for block in msg.get_content_blocks():
                    if block.get("type") == "thinking":
                        thinking = block.get("thinking", "")
                        if thinking:
                            reasoning_contents[id(msg)] = thinking
                        break
                for block in msg.get_content_blocks():
                    if (
                        block.get("type") == "tool_use"
                        and "extra_content" in block
                    ):
                        extra_contents[block["id"]] = block["extra_content"]

            # Convert file:// URLs to paths for all media blocks,
            # and replace deleted local files with text placeholders.
            # TODO: remove after the legacy formatter handles this natively.
            for msg in normalized_msgs:
                if isinstance(msg.content, list):
                    _fixup_media_list(msg.content)

            # For Anthropic, fully override formatting to handle
            # media blocks (top-level & inside tool_result output).
            # TODO: remove after the legacy Anthropic formatter catches up.
            if is_anthropic_formatter:
                messages = _format_anthropic_messages(normalized_msgs)
            else:
                # Gemini handles video natively; for others
                # (OpenAI) we inject it via placeholders.
                _needs_video = not _is_gemini_formatter
                video_subs: dict[str, dict] = {}
                if _needs_video:
                    video_subs = _substitute_video_blocks(
                        normalized_msgs,
                    )

                messages = await super()._format(normalized_msgs)

                if video_subs:
                    _replace_video_placeholders(
                        messages,
                        video_subs,
                    )
                    _restore_video_blocks(normalized_msgs, video_subs)

                if _needs_video and getattr(
                    self,
                    "promote_tool_result_images",
                    False,
                ):
                    messages = _promote_tool_result_videos(
                        normalized_msgs,
                        messages,
                    )

            # Image promotion inserts user messages between tool
            # results, violating the API's contiguity requirement.
            messages = _reorder_tool_and_promoted_messages(messages)

            # Normalize non-standard MIME types (e.g. image/jpg → image/jpeg)
            _fix_image_mime_types(messages)

            if extra_contents and _is_gemini_formatter:
                for message in messages:
                    for tc in message.get("tool_calls", []):
                        ec = extra_contents.get(tc.get("id"))
                        if ec:
                            tc["extra_content"] = ec

            if reasoning_contents and not is_anthropic_formatter:
                # Anthropic passes thinking blocks natively through
                # _format_anthropic_messages; injecting reasoning_content
                # would be redundant and the API doesn't use this field.
                # OpenAI/Gemini (OpenAI-compat) formatters drop thinking
                # blocks, so we re-inject the content as reasoning_content.
                #
                # Build a list of reasoning values aligned with surviving
                # assistant messages.  The parent formatter drops
                # thinking-only messages (no content/tool_calls), so we
                # predict survivors and collect reasoning only for those.
                aligned_reasoning = []
                for m in (
                    msg for msg in normalized_msgs if msg.role == "assistant"
                ):
                    is_thinking_only = (
                        isinstance(m.content, list)
                        and m.content
                        and all(b.get("type") == "thinking" for b in m.content)
                    )
                    if not is_thinking_only:
                        aligned_reasoning.append(
                            reasoning_contents.get(id(m)),
                        )

                out_assistant = [
                    m for m in messages if m.get("role") == "assistant"
                ]

                if len(aligned_reasoning) != len(out_assistant):
                    logger.warning(
                        "Assistant message count mismatch after formatting "
                        "(%d expected survivors, %d actual). "
                        "Skipping reasoning_content injection.",
                        len(aligned_reasoning),
                        len(out_assistant),
                    )
                else:
                    for i, out_msg in enumerate(out_assistant):
                        if aligned_reasoning[i]:
                            out_msg["reasoning_content"] = aligned_reasoning[i]

            return _strip_top_level_message_name(messages)

        @staticmethod
        def convert_tool_result_to_string(
            output: Union[str, List[dict]],
        ) -> tuple[str, Sequence[Tuple[str, dict]]]:
            """Extend parent class to support file blocks.

            Uses try-first strategy for compatibility with parent class.

            Args:
                output: Tool result output (string or list of blocks)

            Returns:
                Tuple of (text_representation, multimodal_data)
            """
            if isinstance(output, str):
                return output, []

            # Try parent class method first
            try:
                return base_formatter_class.convert_tool_result_to_string(
                    output,
                )
            except ValueError as e:
                if "Unsupported block type: file" not in str(e):
                    raise ModelFormatterError(
                        message=str(e),
                    ) from e

                # Handle output containing file blocks
                textual_output = []
                multimodal_data = []

                for block in output:
                    if not isinstance(block, dict) or "type" not in block:
                        raise ModelFormatterError(
                            message=(
                                f"Invalid block: {block}, "
                                "expected a dict with 'type' key"
                            ),
                        ) from e

                    if block["type"] == "file":
                        file_path = block.get("path", "") or block.get(
                            "url",
                            "",
                        )
                        file_name = block.get("name", file_path)

                        textual_output.append(
                            f"The returned file '{file_name}' "
                            f"can be found at: {file_path}",
                        )
                        multimodal_data.append((file_path, block))
                    else:
                        # Delegate other block types to parent class
                        (
                            text,
                            data,
                        ) = base_formatter_class.convert_tool_result_to_string(
                            [block],
                        )
                        textual_output.append(text)
                        multimodal_data.extend(data)

                if len(textual_output) == 0:
                    return "", multimodal_data
                elif len(textual_output) == 1:
                    return textual_output[0], multimodal_data
                else:
                    return (
                        "\n".join("- " + _ for _ in textual_output),
                        multimodal_data,
                    )

    FileBlockSupportFormatter.__name__ = (
        f"FileBlockSupport{base_formatter_class.__name__}"
    )
    return FileBlockSupportFormatter


def _strip_top_level_message_name(
    messages: list[dict],
) -> list[dict]:
    """Strip top-level `name` from OpenAI chat messages.

    Some strict OpenAI-compatible backends reject `messages[*].name`
    (especially for assistant/tool roles) and may return 500/400 on
    follow-up turns. Keep function/tool names unchanged.
    """
    for message in messages:
        message.pop("name", None)
    return messages


def create_model_and_formatter(
    agent_id: Optional[str] = None,
) -> Tuple[Any, Any]:
    """Factory method to create model and formatter instances.

    This method handles both local and remote models, selecting the
    appropriate chat model class and formatter based on configuration.

    Args:
        agent_id: Optional agent ID to load agent-specific model config.
            If None, tries to get from context, then falls back to global.

    Returns:
        Tuple of (model_instance, formatter_instance)

    Example:
        >>> model, formatter = create_model_and_formatter()
    """
    from ..app.agent_context import get_current_agent_id
    from ..config.config import load_agent_config

    # Determine agent_id (parameter > context > None)
    if agent_id is None:
        try:
            agent_id = get_current_agent_id()
        except Exception:
            pass

    # Try to get agent-specific model first
    model_slot = None
    retry_config = None
    rate_limit_config = None
    if agent_id:
        try:
            agent_config = load_agent_config(agent_id)
            model_slot = agent_config.active_model
            retry_config = RetryConfig(
                enabled=agent_config.running.llm_retry_enabled,
                max_retries=agent_config.running.llm_max_retries,
                backoff_base=agent_config.running.llm_backoff_base,
                backoff_cap=agent_config.running.llm_backoff_cap,
            )
            rate_limit_config = RateLimitConfig(
                max_concurrent=agent_config.running.llm_max_concurrent,
                max_qpm=agent_config.running.llm_max_qpm,
                pause_seconds=agent_config.running.llm_rate_limit_pause,
                jitter_range=agent_config.running.llm_rate_limit_jitter,
                acquire_timeout=agent_config.running.llm_acquire_timeout,
            )
        except Exception as exc:
            raise _agent_config_provider_error(agent_id, exc) from exc

    # Create chat model from agent-specific or global config
    if model_slot and model_slot.provider_id and model_slot.model:
        # Use agent-specific model
        manager = ProviderManager.get_instance()
        provider = manager.get_provider(model_slot.provider_id)
        if provider is None:
            raise ProviderError(
                message=f"Provider '{model_slot.provider_id}' not found.",
            )

        model = provider.get_chat_model_instance(model_slot.model)
        provider_id = model_slot.provider_id
    else:
        # Fallback to global active model
        model = ProviderManager.get_active_chat_model()
        global_model = ProviderManager.get_instance().get_active_model()
        if not global_model:
            raise ProviderError(
                message=(
                    "No active model configured. "
                    "Please configure a model using 'lantai models config' "
                    "or set an agent-specific model."
                ),
            )
        provider_id = global_model.provider_id

    # Create the formatter based on the real model class
    formatter = _create_formatter_instance(model.__class__)

    # Wrap with retry logic for transient LLM API errors
    wrapped_model = TokenRecordingModelWrapper(provider_id, model)
    wrapped_model = RetryChatModel(
        wrapped_model,
        retry_config=retry_config,
        rate_limit_config=rate_limit_config,
    )

    return wrapped_model, formatter


def _agent_id_from_context(agent_id: Optional[str]) -> Optional[str]:
    """Return explicit or context agent id without raising."""

    if agent_id is not None:
        return agent_id
    try:
        from ..app.agent_context import get_current_agent_id

        return get_current_agent_id()
    except Exception:
        return None


def _agent_retry_configs(agent_id: Optional[str]):
    """Return agent-specific retry/rate-limit configs, if available."""

    if not agent_id:
        return None, None, None
    try:
        from ..config.config import load_agent_config

        agent_config = load_agent_config(agent_id)
        retry_config = RetryConfig(
            enabled=agent_config.running.llm_retry_enabled,
            max_retries=agent_config.running.llm_max_retries,
            backoff_base=agent_config.running.llm_backoff_base,
            backoff_cap=agent_config.running.llm_backoff_cap,
        )
        rate_limit_config = RateLimitConfig(
            max_concurrent=agent_config.running.llm_max_concurrent,
            max_qpm=agent_config.running.llm_max_qpm,
            pause_seconds=agent_config.running.llm_rate_limit_pause,
            jitter_range=agent_config.running.llm_rate_limit_jitter,
            acquire_timeout=agent_config.running.llm_acquire_timeout,
        )
        return agent_config, retry_config, rate_limit_config
    except Exception as exc:
        raise _agent_config_provider_error(agent_id, exc) from exc


def _model_client_for_slot(model_slot) -> LantaiModelClient:
    """Create an unwrapped LantaiModelClient for one model slot."""

    manager = ProviderManager.get_instance()
    provider = manager.get_provider(model_slot.provider_id)
    if provider is None:
        raise ProviderError(
            message=f"Provider '{model_slot.provider_id}' not found.",
        )
    authorization = None
    if model_slot.provider_id == "tokendance":
        authorization = resolve_tokendance_authorization(
            manager,
            use_kind="model",
        )
    endpoint = provider.resolve_model_endpoint(model_slot.model)
    if authorization is not None and endpoint.api_key != authorization.api_key:
        raise ProviderError(
            message="TokenDance authorization changed during model routing.",
            details={"provider_id": "tokendance"},
        )
    if endpoint.native_kind == "openai_compatible":
        client: LantaiModelClient = OpenAICompatibleModelClient(endpoint)
    elif endpoint.native_kind == "openai_responses":
        client = OpenAIResponsesModelClient(endpoint)
    else:
        model = provider.get_chat_model_instance(model_slot.model)
        formatter = _create_formatter_instance(model.__class__)
        client = LegacyModelClient(
            endpoint=endpoint,
            model=model,
            formatter=formatter,
        )
    if authorization is None:
        return client
    route = dict(endpoint.metadata.get("routing_snapshot") or {})
    route.update(
        {
            "authorization_context_id": authorization.context_id,
            "owner_binding_id": authorization.payment_binding_id,
            "binding_revision": authorization.binding_revision,
            "credential_ref": authorization.credential_ref,
        }
    )
    return TokenDanceBoundModelClient(
        client,
        manager=manager,
        authorization=authorization,
        routing_snapshot=route,
    )


def resolve_effective_model_slot(
    agent_config: Any | None,
    *,
    manager: ProviderManager | None = None,
) -> Any | None:
    """Resolve an agent model only while it remains locally selectable."""

    manager = manager or ProviderManager.get_instance()
    model_slot = (
        getattr(agent_config, "active_model", None)
        if agent_config is not None
        else None
    )
    if model_slot is not None and model_slot.provider_id and model_slot.model:
        selectable = getattr(manager, "is_chat_model_slot_selectable", None)
        if not callable(selectable) or selectable(model_slot):
            return model_slot
        logger.warning(
            "Agent model %s/%s is no longer selectable; using global model",
            model_slot.provider_id,
            model_slot.model,
        )
    return manager.get_active_model()


def _wrap_model_client(
    inner: LantaiModelClient,
    *,
    retry_config: RetryConfig | None,
    rate_limit_config: RateLimitConfig | None,
    dispatch_wrapper=None,
) -> LantaiModelClient:
    client = RetryingModelClient(
        TokenRecordingModelClient(inner),
        retry_config=retry_config,
        rate_limit_config=rate_limit_config,
    )

    return dispatch_wrapper(client) if dispatch_wrapper is not None else client


def _global_active_model_slot():
    """Return global active model slot or raise the existing provider error."""

    global_model = ProviderManager.get_instance().get_active_model()
    if not global_model:
        raise ProviderError(
            message=(
                "No active model configured. "
                "Please configure a model using 'lantai models config' "
                "or set an agent-specific model."
            ),
        )
    return global_model


def create_model_client(
    agent_id: Optional[str] = None,
    *,
    model_slot: Any | None = None,
    dispatch_wrapper=None,
) -> LantaiModelClient:
    """Create a Lantai-owned model client for Native Runtime.

    This is parallel to ``create_model_and_formatter`` and does not change
    the default legacy execution path.
    """

    resolved_agent_id = _agent_id_from_context(agent_id)
    agent_config, retry_config, rate_limit_config = _agent_retry_configs(
        resolved_agent_id,
    )

    routing_cfg = getattr(agent_config, "llm_routing", None)
    inner: LantaiModelClient
    allow_access_failover = model_slot is None
    if model_slot is not None:
        # A caller may deliberately pin one authenticated model for the
        # lifetime of an isolated surface (for example, a local Office
        # editor).  This must not write agent/global configuration or silently
        # drift when the main chat changes its active model later.
        inner = _model_client_for_slot(model_slot)
    elif (
        routing_cfg is not None
        and getattr(routing_cfg, "enabled", False)
        and getattr(routing_cfg.local, "provider_id", "")
        and getattr(routing_cfg.local, "model", "")
    ):
        local_client = _wrap_model_client(
            _model_client_for_slot(routing_cfg.local),
            retry_config=retry_config,
            rate_limit_config=rate_limit_config,
            dispatch_wrapper=dispatch_wrapper,
        )
        cloud_slot = routing_cfg.cloud or _global_active_model_slot()
        cloud_client = _wrap_model_client(
            _model_client_for_slot(cloud_slot),
            retry_config=retry_config,
            rate_limit_config=rate_limit_config,
            dispatch_wrapper=dispatch_wrapper,
        )
        return RoutingLantaiModelClient(
            local_endpoint=RoutingModelClientEndpoint(
                provider_id=routing_cfg.local.provider_id,
                model_name=routing_cfg.local.model,
                client=local_client,
            ),
            cloud_endpoint=RoutingModelClientEndpoint(
                provider_id=cloud_slot.provider_id,
                model_name=cloud_slot.model,
                client=cloud_client,
            ),
            routing_cfg=routing_cfg,
        )
    else:
        manager = ProviderManager.get_instance()
        model_slot = resolve_effective_model_slot(agent_config, manager=manager)
        if model_slot is None:
            model_slot = _global_active_model_slot()
        inner = _model_client_for_slot(model_slot)

    primary = _wrap_model_client(
        inner,
        retry_config=retry_config,
        rate_limit_config=rate_limit_config,
        dispatch_wrapper=dispatch_wrapper,
    )
    if (
        not allow_access_failover
        or isinstance(inner, RoutingLantaiModelClient)
        or inner.provider_id == "tokendance"
    ):
        return primary

    manager = ProviderManager.get_instance()
    fallback_resolver = getattr(manager, "get_model_fallback_slot", None)
    fallback_slot = (
        fallback_resolver(model_slot) if callable(fallback_resolver) else None
    )
    if fallback_slot is None:
        return primary
    try:
        fallback = _wrap_model_client(
            _model_client_for_slot(fallback_slot),
            retry_config=retry_config,
            rate_limit_config=rate_limit_config,
            dispatch_wrapper=dispatch_wrapper,
        )
    except Exception:
        logger.warning(
            "Unable to prepare fallback model %s/%s",
            fallback_slot.provider_id,
            fallback_slot.model,
            exc_info=True,
        )
        return primary
    return ModelAccessFailoverClient(primary, fallback)


def create_model_diagnostic_client(*, model_slot: Any) -> LantaiModelClient:
    """Create one raw adapter client for bounded, non-conversational probes.

    Diagnostic calls deliberately skip Lantai's retry and token-recording
    wrappers so a probe cannot silently multiply requests or persist response
    metadata. Normal chat and Agent execution must continue to use
    :func:`create_model_client`.
    """

    return _model_client_for_slot(model_slot)


def _create_formatter_instance(
    chat_model_class: type[Any],
) -> Any:
    """Create a formatter instance for the given chat model class.

    The formatter is enhanced with file block support for handling
    file outputs in tool results.

    Args:
        chat_model_class: The chat model class

    Returns:
        Formatter instance with file block support
    """
    base_formatter_class = _get_formatter_for_chat_model(chat_model_class)
    formatter_class = _create_file_block_support_formatter(
        base_formatter_class,
    )
    kwargs: dict[str, Any] = {}
    formatter_family_names = _class_family_names(base_formatter_class)
    if (
        issubclass(
            base_formatter_class,
            LantaiOpenAIChatFormatter,
        )
        or "GeminiChatFormatter" in formatter_family_names
    ):
        kwargs["promote_tool_result_images"] = True
    return formatter_class(**kwargs)


def create_formatter_for_chat_model(
    chat_model: Any, *, supports_multimodal: bool | None = None,
) -> Any:
    """Create a formatter, optionally binding its resolved model capability.

    Isolated utility models must not inherit another chat's media capability.
    Omitted metadata preserves the normal request-context lookup.
    """

    chat_model_class = (
        chat_model if isinstance(chat_model, type) else chat_model.__class__
    )
    formatter = _create_formatter_instance(chat_model_class)
    if isinstance(supports_multimodal, bool):
        formatter._lantai_supports_multimodal = supports_multimodal
    return formatter


__all__ = [
    "create_formatter_for_chat_model",
    "create_model_and_formatter",
    "create_model_client",
    "resolve_effective_model_slot",
    "create_model_diagnostic_client",
]
