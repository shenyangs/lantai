# -*- coding: utf-8 -*-
"""Generate a semantic chat title after the first complete turn settles.

The visible answer always finishes first. Title generation then runs as an
isolated utility-model task with only the first request and settled answer;
the deterministic helpers are a failure fallback, not the primary strategy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING

from .models import ChatUpdate
from ..utility_model import run_active_model_text_task, run_utility_text_task
from ...config import load_config

if TYPE_CHECKING:
    from ..workspace import Workspace

logger = logging.getLogger(__name__)

TITLE_PROMPT = (
    "请根据一轮已经完成的对话，为会话生成简洁、准确的语义标题。"
    "必须综合理解用户的第一次输入和助手的最终回答，命名真实讨论的主题、"
    "问题、产物或结论；不要机械截取原句开头。删除‘请、帮我、分析一下、"
    "我要’等请求套话、执行方式、Agent 数量和输出格式要求。中文优先使用"
    "6-16 个汉字，其他语言最多 6 个词。只输出标题，不要引号、Markdown、"
    "解释或句末标点。"
)

MAX_INPUT_CHARS = 800
MAX_ANSWER_CHARS = 1600
MAX_TITLE_CHARS = 60

_TITLE_TASKS: set[asyncio.Task[None]] = set()

_LITERAL_REPLY_RE = re.compile(
    r"^\s*(?:请)?(?:只|仅)(?:需|要)?回复\s*[「『“\"'`]?(.+?)[」』”\"'`]?"
    r"\s*[。.!！?？]?\s*$",
    re.IGNORECASE,
)
_REQUEST_PREFIX_RE = re.compile(
    r"^\s*(?:请帮我|(?:请|麻烦)(?:你)?|帮我|给我|替我|能否|可以(?:帮我)?|"
    r"我(?:想|要|希望)(?:让你)?)[，,:：\s]*",
    re.IGNORECASE,
)
_AGENT_ORCHESTRATION_RE = re.compile(
    r"^(?:让|用|调用)?\s*(?:\d+|[一二三四五六七八九十]+)\s*个"
    r"(?:子\s*)?(?:agent|智能体)\s*(?:并行|分别|分头|一起)?\s*",
    re.IGNORECASE,
)
_GENERIC_ANSWER_PREFIXES = (
    "好的",
    "好，",
    "当然",
    "可以",
    "没问题",
    "我来",
    "下面",
    "以下",
    "已为你",
    "已经为你",
    "收到",
)
_IMPERATIVE_TITLE_RE = re.compile(
    r"^(?:请(?:写|帮|让|生成|画|制作|分析|检查|修复|整理|总结)|"
    r"帮我|我要|我想|只回复|仅回复)",
    re.IGNORECASE,
)


def _clean_title(raw: str) -> str:
    """Normalize model output into a single-line title."""
    title = raw.strip().splitlines()[0] if raw.strip() else ""
    title = title.strip().strip("\"'`“”‘’")
    while title and title[-1] in ".,;:!?。；：！？":
        title = title[:-1].rstrip()
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rstrip()
    return title


def _clean_semantic_line(raw: str) -> str:
    """Clean one possible heading without turning prose into a title."""
    line = str(raw or "").strip()
    line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
    line = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", line)
    line = re.sub(r"^(?:标题|主题|题目)\s*[:：]\s*", "", line)
    return _clean_title(line)


def _first_answer_heading(answer: str) -> str:
    """Return a title-like first answer line, skipping conversational filler."""
    for raw_line in str(answer or "").splitlines():
        line = _clean_semantic_line(raw_line)
        if not line or line in {"```", "~~~"}:
            continue
        if line.startswith(_GENERIC_ANSWER_PREFIXES):
            continue
        if (
            len(line) <= 36
            and not re.search(r"[。！？；，,]", line)
            and not _IMPERATIVE_TITLE_RE.search(line)
        ):
            return line
    return ""


def _compact_user_intent(user_message: str) -> str:
    """Best-effort local intent compaction used when no utility model exists."""
    raw = " ".join(str(user_message or "").strip().split())
    if not raw:
        return ""

    literal = _LITERAL_REPLY_RE.match(raw)
    if literal:
        token = _clean_title(literal.group(1))
        if token:
            return _clean_title(f"{token} 响应测试")

    compact = _REQUEST_PREFIX_RE.sub("", raw, count=1)
    compact = _AGENT_ORCHESTRATION_RE.sub("", compact, count=1)

    action = ""
    action_match = re.search(
        r"(?:并行|分别|分头)?\s*(检查|排查|诊断|定位|审查|评估|分析|"
        r"修复|优化|总结|整理|解释|比较|翻译|设计|开发|实现|测试|"
        r"写|撰写|生成|制作|创建|画|绘制)",
        compact,
        re.IGNORECASE,
    )
    if action_match:
        action = action_match.group(1).lower()
        compact = compact[action_match.end() :].lstrip()
        compact = re.sub(r"^(?:一下子?|下)[，,：:\s]*", "", compact)

    compact = re.sub(
        r"^(?:一|这)?(?:篇|份|个|张|幅|段|套)\s*",
        "",
        compact,
    )
    compact = re.sub(
        r"^(?:约|大约|不少于|不超过)?\s*\d+\s*(?:字|词|页)\s*"
        r"(?:的|，|,)?\s*",
        "",
        compact,
    )
    if action in {"写", "撰写", "生成", "制作", "创建"}:
        nested_action = re.match(
            r"^(分析|评估|检查|排查|总结|整理|比较|介绍|说明)\s*",
            compact,
        )
        if nested_action:
            action = nested_action.group(1)
            compact = compact[nested_action.end() :]
    compact = re.split(
        r"[。.!！?？；;]|(?:，|,)\s*(?:然后|并(?:且)?|最后|再)",
        compact,
        maxsplit=1,
    )[0]
    compact = re.sub(
        r"(?:然后|并(?:且)?|最后|再)(?:汇总|总结|输出|给出).*$",
        "",
        compact,
    )
    if action in {"画", "绘制"}:
        compact = re.sub(
            r"[、，,\s]*(?:画面)?(?:中央|中心)(?:放置|摆放|呈现|是|有)?\s*",
            "",
            compact,
        )
        compact = re.sub(
            r"(?:的)?(?:图片|图像|插画|配图|海报|画面)$",
            "",
            compact,
        )
    compact = re.sub(
        r"(?:是|有)?(?:多少|几|什么|怎么(?:办)?|如何)$",
        "",
        compact,
    )
    compact = re.sub(
        r"(?:这个|当前|本次)?(?:问题|故障|bug)$",
        "",
        compact,
        flags=re.I,
    )
    compact = compact.strip(" ，,：:、的")

    suffixes = {
        "检查": "问题排查",
        "排查": "问题排查",
        "诊断": "问题诊断",
        "定位": "问题定位",
        "审查": "审查",
        "评估": "评估",
        "分析": "分析",
        "修复": "修复",
        "优化": "优化",
        "总结": "总结",
        "整理": "整理",
        "介绍": "介绍",
        "说明": "说明",
        "解释": "说明",
        "比较": "比较",
        "翻译": "翻译",
        "设计": "设计",
        "开发": "开发",
        "实现": "实现",
        "测试": "测试",
        "写": "写作",
        "撰写": "写作",
        "生成": "生成",
        "制作": "制作",
        "创建": "创建",
        "画": "图像创作",
        "绘制": "图像创作",
    }
    suffix = suffixes.get(action, "")
    if action == "分析" and "还是" in compact:
        suffix = ""
    if action in {"检查", "排查"} and compact.endswith("问题"):
        suffix = "排查"
    if compact and suffix and not compact.endswith(suffix):
        compact = f"{compact}{suffix}"

    title = _clean_title(compact)
    if title and not _IMPERATIVE_TITLE_RE.search(title):
        return title
    return "会话主题"


def _semantic_fallback_title(
    user_message: str,
    assistant_message: str = "",
) -> str:
    """Create a useful local title without copying the prompt prefix."""
    literal = _LITERAL_REPLY_RE.match(str(user_message or ""))
    if literal:
        token = _clean_title(literal.group(1))
        if token:
            return _clean_title(f"{token} 响应测试")

    answer_heading = _first_answer_heading(assistant_message)
    if answer_heading:
        return answer_heading
    return _compact_user_intent(user_message)


def _legacy_semantic_title_repair(
    current_name: str,
    recorded_prompt: str,
) -> str | None:
    """Upgrade only names bearing the exact legacy 10-character signature."""
    name = str(current_name or "")
    prompt = str(recorded_prompt or "")
    if not name or not prompt or name != prompt[:10]:
        return None
    semantic_name = _semantic_fallback_title(prompt)
    if not semantic_name or semantic_name == name:
        return None
    return semantic_name


def _looks_like_prompt_excerpt(title: str, user_message: str) -> bool:
    """Reject a model result that merely repeats an imperative prompt."""
    normalized_title = " ".join(str(title or "").split())
    normalized_prompt = " ".join(str(user_message or "").split())
    if not normalized_title or _IMPERATIVE_TITLE_RE.search(normalized_title):
        return True
    return (
        len(normalized_title) >= 6
        and normalized_prompt.startswith(normalized_title)
        and bool(_REQUEST_PREFIX_RE.match(normalized_prompt))
    )


async def generate_and_update_title(
    workspace: "Workspace",
    chat_id: str,
    user_message: str,
    placeholder_name: str,
    assistant_message: str = "",
) -> None:
    """Name one chat from its first settled request/answer pair.

    Persistence uses compare-and-set so a user rename that happens while this
    task is running always wins.
    """
    message = str(user_message or "").strip()[:MAX_INPUT_CHARS]
    answer = str(assistant_message or "").strip()[:MAX_ANSWER_CHARS]
    if not message:
        return

    fallback_title = _semantic_fallback_title(message, answer)
    title = ""
    should_try_primary_fallback = False
    try:
        user_prompt = f"<first_user_message>\n{message}\n</first_user_message>"
        if answer:
            user_prompt += (
                f"\n<first_assistant_answer>\n{answer}"
                "\n</first_assistant_answer>"
            )
        result = await run_utility_text_task(
            agent_id=workspace.agent_id,
            system_prompt=TITLE_PROMPT,
            user_prompt=user_prompt,
            purpose="chat_title_after_first_turn",
            allow_follow_active=True,
        )
        if result is not None:
            title = _clean_title(result.text)
            if _looks_like_prompt_excerpt(title, message):
                title = ""
        if not title:
            config = load_config().utility_intelligence
            should_try_primary_fallback = bool(
                getattr(config, "background_processing_enabled", True),
            ) and config.mode == "dedicated"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Semantic title model failed for chat %s; using local fallback",
            chat_id,
            exc_info=True,
        )
        config = load_config().utility_intelligence
        should_try_primary_fallback = bool(
            getattr(config, "background_processing_enabled", True),
        ) and config.mode == "dedicated"

    if should_try_primary_fallback:
        try:
            fallback = await run_active_model_text_task(
                agent_id=workspace.agent_id,
                system_prompt=TITLE_PROMPT,
                user_prompt=user_prompt,
                purpose="chat_title_primary_idle_fallback",
                wait_for_idle=True,
            )
            if fallback is not None:
                candidate = _clean_title(fallback.text)
                if not _looks_like_prompt_excerpt(candidate, message):
                    title = candidate
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Primary title fallback failed for chat %s",
                chat_id,
                exc_info=True,
            )

    title = title or fallback_title
    if not title:
        return
    try:
        updated = await workspace.chat_manager.patch_chat_if_name_matches(
            chat_id,
            placeholder_name,
            ChatUpdate(name=title),
        )
        if updated is None:
            logger.debug(
                "Chat %s was renamed before semantic title completed",
                chat_id,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Could not persist semantic title for chat %s",
            chat_id,
            exc_info=True,
        )


def schedule_title_generation(**kwargs) -> asyncio.Task[None]:
    """Retain one post-turn title task until it finishes."""
    workspace = kwargs.get("workspace")
    service = getattr(workspace, "background_semantics", None)
    if service is not None:
        payload = {
            "chat_id": str(kwargs.get("chat_id") or ""),
            "user_message": str(kwargs.get("user_message") or ""),
            "assistant_message": str(kwargs.get("assistant_message") or ""),
            "placeholder_name": str(kwargs.get("placeholder_name") or ""),
        }
        fingerprint = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        job = service.submit(
            kind="chat_title",
            payload=payload,
            idempotency_key="title:" + hashlib.sha256(
                fingerprint.encode("utf-8"),
            ).hexdigest(),
        )

        async def wait_for_durable_title() -> None:
            await service.wait_for_job(job.job_id)

        task = asyncio.create_task(wait_for_durable_title())
        _TITLE_TASKS.add(task)
        task.add_done_callback(_TITLE_TASKS.discard)
        return task
    task = asyncio.create_task(generate_and_update_title(**kwargs))
    _TITLE_TASKS.add(task)
    task.add_done_callback(_TITLE_TASKS.discard)
    return task
