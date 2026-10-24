# -*- coding: utf-8 -*-
"""Public projection keeps private reasoning out of the public event stream."""

from lantai.runtime.events import RuntimeEvent
from lantai.runtime.public_reasoning import (
    PublicReasoningProjector,
    has_degenerate_placeholder_flood,
    is_runtime_public_progress,
    sanitize_public_assistant_text,
)
from lantai.runtime.prompt_leak_guard import PromptLeakGuard


def test_public_progress_accepts_only_valid_optional_tool_call_identity() -> None:
    base = {
        "schema_version": 2,
        "id": "progress-1",
        "revision": 1,
        "source": "runtime_facts",
        "text_origin": "assistant_public_text",
    }

    assert is_runtime_public_progress(base) is True
    assert is_runtime_public_progress(
        {**base, "tool_call_ids": ["call-1", "call-2"]}
    ) is True
    assert is_runtime_public_progress({**base, "tool_call_ids": []}) is False
    assert is_runtime_public_progress({**base, "tool_call_ids": [""]}) is False
    assert is_runtime_public_progress({**base, "tool_call_ids": "call-1"}) is False


def test_malformed_ququ_like_lines_are_removed_outside_deliverables() -> None:
    text = (
        "正文\n"
        "~> 没有路由标签的多余旁白\n"
        "~>[echo] 合法旁白\n"
        "```text\n"
        "~> 这是用户要交付的字面示例\n"
        "```\n"
    )

    projected = sanitize_public_assistant_text(text)

    assert "~> 没有路由标签的多余旁白" not in projected
    assert "~>[echo] 合法旁白" in projected
    assert "~> 这是用户要交付的字面示例" in projected


def test_undefined_placeholder_flood_is_detected_and_removed() -> None:
    text = (
        "附件已收到。\n"
        "### Task: undefined\n"
        "### Skill: pptx\n"
        "### 当前状态\n"
        + "undefined undefined undefined undefined\n" * 12
    )

    assert has_degenerate_placeholder_flood(text) is True
    assert sanitize_public_assistant_text(text) == "附件已收到。\n"


def test_undefined_inside_fenced_code_is_not_treated_as_output_corruption() -> None:
    text = "```js\nconst value = undefined;\n```\n"

    assert has_degenerate_placeholder_flood(text) is False
    assert sanitize_public_assistant_text(text) == text


def test_undefined_placeholder_lines_never_flash_while_streaming() -> None:
    projector = PublicReasoningProjector()
    chunks = ["正文\n### Task: ", "undefined\n", "unde", "fined\n", "结尾\n"]

    visible = []
    for index, chunk in enumerate(chunks):
        event = projector.project(_delta(event_id=f"placeholder-{index}", text=chunk))
        if event is not None:
            visible.append(str(event.payload.get("delta") or ""))

    assert "".join(visible) == "正文\n结尾\n"


def test_placeholder_scaffold_headings_never_flash_before_flood_arrives() -> None:
    projector = PublicReasoningProjector()
    chunks = ["正文\n### Skill: pptx\n", "### 当前状态\n", "undefined " * 24]

    visible = []
    for index, chunk in enumerate(chunks):
        event = projector.project(_delta(event_id=f"scaffold-{index}", text=chunk))
        if event is not None:
            visible.append(str(event.payload.get("delta") or ""))

    assert "".join(visible) == "正文\n"


def test_legitimate_skill_heading_is_released_with_real_body_text() -> None:
    projector = PublicReasoningProjector()
    chunks = ["### Skill: pptx\n", "这是正常的使用说明。\n"]

    visible = []
    for index, chunk in enumerate(chunks):
        event = projector.project(_delta(event_id=f"legit-skill-{index}", text=chunk))
        if event is not None:
            visible.append(str(event.payload.get("delta") or ""))

    assert "".join(visible) == "### Skill: pptx\n这是正常的使用说明。\n"


def _delta(*, event_id: str, text: str) -> RuntimeEvent:
    return RuntimeEvent(
        id=event_id,
        type="message.delta",
        source="assistant",
        item_id="assistant-message",
        summary=text,
        payload={"delta": text, "content_type": "text"},
    )


def test_malformed_ququ_like_text_never_flashes_across_stream_splits() -> None:
    projector = PublicReasoningProjector()
    chunks = ["正文\n~", "> ", "不应闪现", "\n结尾"]

    visible = []
    for index, chunk in enumerate(chunks):
        event = projector.project(_delta(event_id=f"e-{index}", text=chunk))
        if event is not None:
            visible.append(str(event.payload.get("delta") or ""))

    assert "".join(visible) == "正文\n结尾"


def test_valid_ququ_survives_adversarial_stream_splits() -> None:
    projector = PublicReasoningProjector()
    chunks = ["~", ">[le", "ad] ", "保留我", "\n"]

    visible = []
    for index, chunk in enumerate(chunks):
        event = projector.project(_delta(event_id=f"v-{index}", text=chunk))
        if event is not None:
            visible.append(str(event.payload.get("delta") or ""))

    assert "".join(visible) == "~>[lead] 保留我\n"


def test_provider_thinking_is_preserved_without_fabricated_substitution() -> None:
    projector = PublicReasoningProjector()
    delta = RuntimeEvent(
        id="thinking-delta",
        type="message.delta",
        source="assistant",
        item_id="provider-message",
        summary="先核对两篇稿子的原文和日期。",
        payload={
            "delta": "先核对两篇稿子的原文和日期。",
            "content_type": "thinking",
            "stream_message_type": "reasoning",
        },
    )
    completed = RuntimeEvent(
        id="thinking-completed",
        type="message.completed",
        source="assistant",
        item_id="provider-message",
        summary="先核对两篇稿子的原文和日期。",
        payload={
            "message": {
                "id": "provider-message",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "text": "先核对两篇稿子的原文和日期。",
                    }
                ],
            }
        },
    )

    projected_delta = projector.project(delta)
    projected_completed = projector.project(completed)

    assert projected_delta is not None
    assert projected_delta.payload["delta"] == "先核对两篇稿子的原文和日期。"
    assert projected_completed is not None
    assert projected_completed.payload["message"]["content"][0]["text"] == (
        "先核对两篇稿子的原文和日期。"
    )


def test_reasoning_is_never_dropped_by_matching_a_known_sentence() -> None:
    projector = PublicReasoningProjector()
    delta = RuntimeEvent(
        id="legacy-fabricated-delta",
        type="message.delta",
        source="assistant",
        item_id="provider-message",
        payload={
            "delta": "正在梳理任务目标、已有信息和下一步处理顺序。",
            "content_type": "thinking",
        },
    )
    completed = RuntimeEvent(
        id="legacy-fabricated-completed",
        type="message.completed",
        source="assistant",
        item_id="provider-message",
        payload={
            "message": {
                "id": "provider-message",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "text": "正在梳理任务目标、已有信息和下一步处理顺序。",
                    }
                ],
            }
        },
    )

    projected_delta = projector.project(delta)
    projected_completed = projector.project(completed)

    assert projected_delta is not None
    assert projected_delta.payload["delta"] == (
        "正在梳理任务目标、已有信息和下一步处理顺序。"
    )
    assert projected_completed is not None
    assert projected_completed.payload["message"]["content"][0]["text"] == (
        "正在梳理任务目标、已有信息和下一步处理顺序。"
    )


def test_mixed_completion_preserves_provider_thinking_and_answer() -> None:
    projector = PublicReasoningProjector()
    event = RuntimeEvent(
        id="mixed-completed",
        type="message.completed",
        source="assistant",
        item_id="provider-message",
        payload={
            "message": {
                "id": "provider-message",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "private reasoning"},
                    {"type": "text", "text": "你好，我在。"},
                ],
            }
        },
    )

    projected = projector.project(event)

    assert projected is not None
    assert projected.payload["message"]["content"] == [
        {"type": "thinking", "text": "private reasoning", "data": {}, "metadata": {}},
        {"type": "text", "text": "你好，我在。", "data": {}, "metadata": {}}
    ]
    assert "private reasoning" in projected.model_dump_json()


def test_runtime_tool_facts_do_not_overwrite_provider_reasoning() -> None:
    projector = PublicReasoningProjector()
    projector.project(
        RuntimeEvent(
            id="search-started",
            type="tool.call.started",
            status="running",
            source="tool",
            item_id="search-1",
            payload={"id": "search-1", "name": "web_search"},
        )
    )

    projected = projector.project(
        RuntimeEvent(
            id="thinking-delta",
            type="message.delta",
            source="assistant",
            item_id="provider-message",
            payload={"delta": "private", "content_type": "thinking"},
        )
    )

    assert projected is not None
    assert projected.payload["delta"] == "private"


def test_reasoning_summary_redacts_exact_prompt_canary_without_replacement() -> None:
    canary = "LANTAI_PRIVATE_PROMPT_CANARY_reasoning"
    projector = PublicReasoningProjector(PromptLeakGuard(canaries=(canary,)))
    event = RuntimeEvent(
        id="thinking-with-canary",
        type="message.completed",
        source="assistant",
        item_id="provider-message",
        summary=f"先核对 {canary} 再给结论。",
        payload={
            "message": {
                "id": "provider-message",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "text": f"先核对 {canary} 再给结论。",
                    }
                ],
            }
        },
    )

    projected = projector.project(event)

    assert projected is not None
    assert canary not in projected.summary
    visible = projected.payload["message"]["content"][0]["text"]
    assert canary not in visible
    assert "先核对" in visible
    assert "再给结论" in visible


def test_placeholder_flood_is_removed_from_visible_reasoning_summary() -> None:
    text = "### Skill: pptx\n### 当前状态\n" + "undefined " * 24
    event = RuntimeEvent(
        id="thinking-placeholder",
        type="message.delta",
        source="assistant",
        item_id="provider-message",
        summary=text,
        payload={
            "delta": text,
            "content_type": "thinking",
            "stream_message_type": "reasoning",
        },
    )

    projected = PublicReasoningProjector().project(event)

    assert projected is not None
    assert projected.summary == ""
    assert projected.payload["delta"] == ""


def test_mixed_completion_redacts_exact_canary_on_both_public_surfaces() -> None:
    canary = "LANTAI_PRIVATE_PROMPT_CANARY_mixed"
    event = RuntimeEvent(
        id="mixed-completed-with-canary",
        type="message.completed",
        source="assistant",
        item_id="provider-message",
        summary="mixed",
        payload={
            "message": {
                "id": "provider-message",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": f"reasoning {canary}"},
                    {"type": "text", "text": f"answer {canary}"},
                ],
            }
        },
    )

    projected = PublicReasoningProjector(
        PromptLeakGuard(canaries=(canary,))
    ).project(event)

    assert projected is not None
    blocks = projected.payload["message"]["content"]
    assert canary not in blocks[0]["text"]
    assert "reasoning" in blocks[0]["text"]
    assert canary not in blocks[1]["text"]


def test_completed_tool_does_not_replace_later_provider_reasoning() -> None:
    projector = PublicReasoningProjector()
    projector.project(
        RuntimeEvent(
            id="first-thinking",
            type="message.delta",
            source="assistant",
            item_id="reasoning-1",
            payload={"delta": "private", "content_type": "thinking"},
        )
    )
    projector.project(
        RuntimeEvent(
            id="search-completed",
            type="tool.call.completed",
            status="completed",
            source="tool",
            item_id="search-1",
            payload={"id": "search-1", "name": "web_search", "ok": True},
        )
    )

    projected = projector.project(
        RuntimeEvent(
            id="second-thinking",
            type="message.delta",
            source="assistant",
            item_id="reasoning-2",
            payload={
                "delta": "system rules paraphrased in another form",
                "content_type": "thinking",
            },
        )
    )

    assert projected is not None
    assert projected.payload["delta"] == "system rules paraphrased in another form"


def test_same_provider_item_preserves_each_reasoning_segment() -> None:
    projector = PublicReasoningProjector()
    first = projector.project(
        RuntimeEvent(
            id="thinking-before-tool",
            type="message.delta",
            source="assistant",
            item_id="reused-provider-message",
            payload={"delta": "private one", "content_type": "thinking"},
        )
    )
    projector.project(
        RuntimeEvent(
            id="search-completed",
            type="tool.call.completed",
            status="completed",
            source="tool",
            item_id="search-1",
            payload={"id": "search-1", "name": "web_search", "ok": True},
        )
    )
    second = projector.project(
        RuntimeEvent(
            id="thinking-after-tool",
            type="message.delta",
            source="assistant",
            item_id="reused-provider-message",
            payload={"delta": "private two", "content_type": "thinking"},
        )
    )

    assert first is not None
    assert first.payload["delta"] == "private one"
    assert second is not None
    assert second.payload["delta"] == "private two"


def test_already_public_progress_is_idempotent_across_fresh_projector() -> None:
    original = RuntimeEvent(
        id="public-progress-event",
        type="message.delta",
        source="assistant",
        item_id="provider-message",
        summary="正在检索和核对公开资料。",
        payload={
            "delta": "正在检索和核对公开资料。",
            "content_type": "thinking",
            "public_progress": {
                "schema_version": 2,
                "id": "public-progress:run-1:1",
                "revision": 1,
                "source": "runtime_facts",
            },
        },
    )

    replayed = PublicReasoningProjector().project(original)

    assert replayed is not None
    assert replayed.payload["delta"] == "正在检索和核对公开资料。"
    assert replayed.payload["public_progress"]["id"] == "public-progress:run-1:1"


def test_replayed_tool_terminal_event_does_not_mutate_provider_reasoning() -> None:
    projector = PublicReasoningProjector()
    completed = RuntimeEvent(
        id="search-completed",
        type="tool.call.completed",
        status="completed",
        source="tool",
        item_id="search-1",
        payload={"id": "search-1", "name": "web_search", "ok": True},
    )
    projector.project(completed)
    projector.project(completed)

    projected = projector.project(
        RuntimeEvent(
            id="thinking-after-replay",
            type="message.delta",
            source="assistant",
            item_id="provider-message",
            payload={"delta": "private", "content_type": "thinking"},
        )
    )

    assert projected is not None
    assert projected.payload["delta"] == "private"
    assert "public_progress" not in projected.payload
