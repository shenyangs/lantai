# -*- coding: utf-8 -*-
"""Unit tests for settled-turn semantic chat-title generation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.app.runner.manager import ChatManager
from lantai.app.runner.models import ChatSpec, ChatUpdate
from lantai.app.runner.repo.json_repo import JsonChatRepository
from lantai.app.runner.title_generator import (
    MAX_TITLE_CHARS,
    _clean_title,
    _legacy_semantic_title_repair,
    _semantic_fallback_title,
    generate_and_update_title,
)
from lantai.app.utility_model import UtilityTextResult


class TestCleanTitle:
    def test_normalizes_one_line_without_damaging_internal_punctuation(self) -> None:
        assert _clean_title('  "Meeting, Notes!"  ') == "Meeting, Notes"
        assert _clean_title("Line one\nLine two") == "Line one"

    def test_truncates_and_handles_empty_input(self) -> None:
        assert _clean_title("a" * (MAX_TITLE_CHARS * 2)) == "a" * MAX_TITLE_CHARS
        assert _clean_title("...") == ""


class TestSemanticFallbackTitle:
    def test_prefers_semantic_heading_from_settled_answer(self) -> None:
        assert _semantic_fallback_title(
            "请写一篇约2000字，分析生成式 AI 对高校设计教育的影响。",
            "生成式 AI 如何重塑高校设计教育\n\n以下是正文……",
        ) == "生成式 AI 如何重塑高校设计教育"

    def test_compacts_request_locally_without_a_model_call(self) -> None:
        assert _semantic_fallback_title(
            "请让4个子Agent并行检查滚动锁死问题，然后汇总结论。",
        ) == "滚动锁死问题排查"
        assert _semantic_fallback_title("新生儿奶粉冲泡温度多少？") == (
            "新生儿奶粉冲泡温度"
        )

    def test_literal_reply_and_image_request_get_semantic_names(self) -> None:
        assert _semantic_fallback_title("只回复 CONCUR", "CONCUR") == (
            "CONCUR 响应测试"
        )
        assert _semantic_fallback_title(
            "画一张纯白背景、中央放置蓝色玻璃球的图片。",
        ) == "纯白背景蓝色玻璃球图像创作"

    def test_repairs_only_exact_legacy_prompt_excerpt(self) -> None:
        prompt = "请让4个子Agent并行检查滚动锁死问题，然后汇总结论。"
        assert _legacy_semantic_title_repair(prompt[:10], prompt) == (
            "滚动锁死问题排查"
        )
        assert _legacy_semantic_title_repair("滚动问题复盘", prompt) is None

    def test_analysis_question_does_not_keep_filler_or_append_analysis(self) -> None:
        assert _semantic_fallback_title(
            "分析一下先有鸡还是先有蛋。",
            "经典问题。拆两层说清楚。",
        ) == "先有鸡还是先有蛋"


@pytest.mark.asyncio
async def test_model_title_uses_first_request_and_settled_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-title",
            name="先有鸡还是先有蛋",
            session_id="console:title",
            user_id="default",
            channel="console",
        ),
    )
    workspace = SimpleNamespace(agent_id="default", chat_manager=manager)
    model_task = AsyncMock(
        return_value=UtilityTextResult(
            text="鸡与蛋的演化起源",
            model_label="test/model",
        ),
    )
    monkeypatch.setattr(
        "lantai.app.runner.title_generator.run_utility_text_task",
        model_task,
    )

    await generate_and_update_title(
        workspace=workspace,
        chat_id=chat.id,
        user_message="分析一下先有鸡还是先有蛋。",
        assistant_message="从演化角度看，先有蛋；第一只鸡来自非鸡祖先产下的蛋。",
        placeholder_name=chat.name,
    )

    prompt = model_task.await_args.kwargs["user_prompt"]
    assert "分析一下先有鸡还是先有蛋。" in prompt
    assert "第一只鸡来自非鸡祖先产下的蛋" in prompt
    saved = await manager.get_chat(chat.id)
    assert saved is not None
    assert saved.name == "鸡与蛋的演化起源"


@pytest.mark.asyncio
async def test_model_title_never_overwrites_manual_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-manual-title",
            name="先有鸡还是先有蛋",
            session_id="console:manual-title",
            user_id="default",
            channel="console",
        ),
    )
    await manager.patch_chat(chat.id, ChatUpdate(name="我自己的标题"))
    monkeypatch.setattr(
        "lantai.app.runner.title_generator.run_utility_text_task",
        AsyncMock(
            return_value=UtilityTextResult(
                text="鸡与蛋的演化起源",
                model_label="test/model",
            ),
        ),
    )

    await generate_and_update_title(
        workspace=SimpleNamespace(agent_id="default", chat_manager=manager),
        chat_id=chat.id,
        user_message="分析一下先有鸡还是先有蛋。",
        assistant_message="从演化角度看，先有蛋。",
        placeholder_name=chat.name,
    )

    saved = await manager.get_chat(chat.id)
    assert saved is not None
    assert saved.name == "我自己的标题"
