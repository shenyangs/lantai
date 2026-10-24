# -*- coding: utf-8 -*-
"""Model output-wire profiles and incremental reasoning normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReasoningSegmentKind = Literal["thinking", "text"]
ReasoningTagPair = tuple[str, str]

_COMMON_REASONING_TAGS: tuple[ReasoningTagPair, ...] = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<reason>", "</reason>"),
    ("<reasoning>", "</reasoning>"),
    ("<thought>", "</thought>"),
)


@dataclass(frozen=True, slots=True)
class ReasoningOutputProfile:
    """How one model exposes reasoning in response content.

    This profile intentionally describes response parsing only. Request-time
    controls live in ``providers.reasoning`` and can evolve independently.
    """

    inline_tag_pairs: tuple[ReasoningTagPair, ...] = ()
    history_tag_pair: ReasoningTagPair | None = None
    request_reasoning_split: bool = False


@dataclass(frozen=True, slots=True)
class ReasoningSegment:
    """One ordered, provider-independent response segment."""

    kind: ReasoningSegmentKind
    text: str


def resolve_reasoning_output_profile(
    provider_id: str,
    model_id: str,
    *,
    base_url: str = "",
    chat_model: str = "",
) -> ReasoningOutputProfile:
    """Resolve output parsing without coupling it to a provider runtime."""

    del provider_id, base_url, chat_model
    model = (model_id or "").strip().lower()
    is_minimax_m_series = "minimax" in model and any(
        marker in model
        for marker in ("m2", "m-2", "m_2", "m3", "m-3", "m_3")
    )
    uses_inline_tags = (
        is_minimax_m_series
        or "qwen3" in model
        or "qwq" in model
        or "deepseek-r1" in model
        or "deepseek-reasoner" in model
        or "k2-thinking" in model
        or "thinking-turbo" in model
    )
    if uses_inline_tags:
        return ReasoningOutputProfile(
            inline_tag_pairs=_COMMON_REASONING_TAGS,
            history_tag_pair=("<think>", "</think>"),
            request_reasoning_split=is_minimax_m_series,
        )
    return ReasoningOutputProfile()


class ReasoningContentParser:
    """Split inline reasoning tags into ordered canonical segments."""

    def __init__(self, profile: ReasoningOutputProfile) -> None:
        self._tag_pairs = profile.inline_tag_pairs
        self._buffer = ""
        self._thinking = False
        self._active_end_tag = ""

    def feed(self, delta: str) -> list[ReasoningSegment]:
        """Normalize one content delta without exposing partial tags."""

        if not delta:
            return []
        if not self._tag_pairs:
            return [ReasoningSegment(kind="text", text=delta)]

        segments: list[ReasoningSegment] = []
        remaining = self._buffer + delta
        self._buffer = ""

        while remaining:
            if self._thinking:
                end_index = remaining.find(self._active_end_tag)
                if end_index >= 0:
                    self._append_segment(
                        segments,
                        kind="thinking",
                        text=remaining[:end_index],
                    )
                    remaining = remaining[
                        end_index + len(self._active_end_tag) :
                    ]
                    self._thinking = False
                    self._active_end_tag = ""
                    continue

                hold = self._partial_tag_suffix_len(
                    remaining,
                    (self._active_end_tag,),
                )
                visible_end = len(remaining) - hold
                self._append_segment(
                    segments,
                    kind="thinking",
                    text=remaining[:visible_end],
                )
                self._buffer = remaining[visible_end:]
                break

            found = [
                (remaining.find(start), start, end)
                for start, end in self._tag_pairs
                if remaining.find(start) >= 0
            ]
            if found:
                start_index, start_tag, end_tag = min(
                    found,
                    key=lambda item: item[0],
                )
                self._append_segment(
                    segments,
                    kind="text",
                    text=remaining[:start_index],
                )
                remaining = remaining[start_index + len(start_tag) :]
                self._thinking = True
                self._active_end_tag = end_tag
                continue

            hold = self._partial_tag_suffix_len(
                remaining,
                tuple(start for start, _end in self._tag_pairs),
            )
            visible_end = len(remaining) - hold
            self._append_segment(
                segments,
                kind="text",
                text=remaining[:visible_end],
            )
            self._buffer = remaining[visible_end:]
            break
        return segments

    def flush(self) -> list[ReasoningSegment]:
        """Return buffered segments at end-of-stream."""

        if not self._buffer:
            return []
        kind: ReasoningSegmentKind = "thinking" if self._thinking else "text"
        trailing = self._buffer
        self._buffer = ""
        return [ReasoningSegment(kind=kind, text=trailing)]

    @staticmethod
    def _append_segment(
        segments: list[ReasoningSegment],
        *,
        kind: ReasoningSegmentKind,
        text: str,
    ) -> None:
        if not text:
            return
        segments.append(ReasoningSegment(kind=kind, text=text))

    @staticmethod
    def _partial_tag_suffix_len(
        text: str,
        tags: tuple[str, ...],
    ) -> int:
        max_tag_length = max((len(tag) for tag in tags), default=0)
        max_suffix = min(len(text), max_tag_length - 1)
        for size in range(max_suffix, 0, -1):
            suffix = text[-size:]
            if any(tag.startswith(suffix) for tag in tags):
                return size
        return 0
