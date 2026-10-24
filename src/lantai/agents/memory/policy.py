# -*- coding: utf-8 -*-
"""Deterministic guardrails for durable memory admission.

The model may propose a concise atom, but this module owns the final admission
decision.  It intentionally prefers false negatives: raw conversation remains
searchable as evidence, while a bad long-term memory changes future behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


MemoryDecision = Literal["reject", "observe", "suggest", "confirm"]
MemoryScope = Literal["turn", "thread", "project", "global"]
MemorySubject = Literal["user", "assistant", "project", "third_party", "unknown"]
MemorySensitivity = Literal["normal", "personal", "sensitive"]

_ASSISTANT_ROLE_RE = re.compile(
    r"^\s*(?:请你|你要|你是|作为|扮演|act\s+as|you\s+are|please\s+act)",
    re.I,
)
_ASSISTANT_PRINCIPLE_RE = re.compile(
    r"(?:不确定.{0,12}(?:说明|核实)|不要.{0,8}(?:编造|捏造|幻觉)|"
    r"注明(?:信源|来源)|给出(?:信源|来源)|先核实|如实回答)",
    re.I,
)
_QUESTION_RE = re.compile(
    r"(?:[?？]\s*$|^(?:能不能|哪些|什么|如何|为什么)|"
    r"^(?:请|帮我|能否|可以).{0,80}(?:吗|么)[?？]?$)",
)
_SENSITIVE_RE = re.compile(
    r"(?:出生(?:日期|时间|地点|地|年月|时辰)|生辰|八字|身份证|护照|"
    r"银行卡|住址|家庭地址|病史|疾病|诊断|药物|性取向|宗教|政治立场|"
    r"api[_ -]?key|password|密码|token|secret)",
    re.I,
)
_THIRD_PARTY_RE = re.compile(r"(?:父亲|母亲|父母|孩子|宝宝|妻子|丈夫|同事|客户|朋友)")
_EXPLICIT_RE = re.compile(r"(?:记住|请记得|长期记住|保存为记忆|remember)", re.I)
_DURABLE_RE = re.compile(
    r"(?:每次|一直|始终|默认|长期|一贯|我(?:很)?(?:喜欢|不喜欢|偏好)|"
    r"(?:你|用户)(?:喜欢|不喜欢|偏好|希望|倾向)|我的(?:习惯|偏好)|"
    r"prefer|always|never|by default)",
    re.I,
)
_FUTURE_DIRECTIVE_RE = re.compile(
    r"^(?:以后|今后).{0,24}(?:请|希望|回答|回复|使用|采用|保持|不要|要)",
    re.I,
)
_PROJECT_RE = re.compile(
    r"(?:这个项目|当前项目|本项目|工作区|仓库|代码库|repo|repository|project|"
    r"构建|部署|单测|pytest|npm|文件夹|目录)",
    re.I,
)
_NAMING_TASK_RE = re.compile(r"(?:取名|起名|姓名|名字|诗词|生辰|八字)")
_ONE_OFF_RE = re.compile(
    r"(?:这次|本次|本轮|当前任务|今天|稍后|下一步|待办|todo|先帮我|请分析|"
    r"当前(?:输出|回答|回复|结果)|请写|请生成|请总结|请说明|"
    r"报告|模板|方案|截图)",
    re.I,
)
_STYLE_RE = re.compile(
    r"(?:语气|口吻|表达|回答|回复|称呼|格式|简洁|直接|自然|克制|详细|"
    r"不要用[“\"']?.{1,18}[”\"']?(?:收尾|开头)|有事直接说)",
    re.I,
)


def is_explicitly_one_off_memory(content: str) -> bool:
    """Return whether text names a concrete current/one-case context.

    This is intentionally narrower than a general low-utility policy result.
    Historical confirmed facts must not be quarantined merely because their
    wording lacks an explicit word such as "long-term".
    """

    clean = re.sub(r"\s+", " ", content or "").strip()
    return bool(clean and _ONE_OFF_RE.search(clean))


@dataclass(frozen=True)
class MemoryPolicyResult:
    decision: MemoryDecision
    scope: MemoryScope
    subject: MemorySubject
    utility_score: float
    reason: str
    sensitivity: MemorySensitivity = "normal"


class MemoryPolicyEngine:
    """Classify whether a proposed atom deserves durable memory state."""

    def evaluate(
        self,
        content: str,
        *,
        memory_type: str = "",
        explicit_user_request: bool = False,
        recurrence_count: int = 1,
        distinct_session_count: int = 1,
    ) -> MemoryPolicyResult:
        clean = re.sub(r"\s+", " ", content or "").strip()
        if not clean:
            return self._result("reject", "turn", "unknown", 0.0, "内容为空")
        stripped = re.sub(
            r"^(?:默认规则|用户指令|澜台准备记住)\s*[:：]\s*",
            "",
            clean,
        )
        if stripped and stripped != clean:
            return self.evaluate(
                stripped,
                memory_type=memory_type,
                explicit_user_request=explicit_user_request,
                recurrence_count=recurrence_count,
                distinct_session_count=distinct_session_count,
            )

        sensitivity: MemorySensitivity = (
            "sensitive" if _SENSITIVE_RE.search(clean) else "normal"
        )
        if _ASSISTANT_ROLE_RE.search(clean):
            return self._result(
                "reject", "turn", "assistant", 0.0, "这是助手角色或任务提示，不是用户记忆",
                sensitivity,
            )
        if _ASSISTANT_PRINCIPLE_RE.search(clean):
            return self._result(
                "reject", "turn", "assistant", 0.05, "这是通用回答原则，不应写入用户画像",
                sensitivity,
            )

        subject: MemorySubject = "user"
        if _THIRD_PARTY_RE.search(clean):
            subject = "third_party"
        elif _PROJECT_RE.search(clean) or _NAMING_TASK_RE.search(clean):
            subject = "project"

        if sensitivity == "sensitive" and not explicit_user_request:
            return self._result(
                "reject", "turn", subject, 0.0, "包含敏感个人信息，未经明确要求不保存",
                sensitivity,
            )

        explicit = explicit_user_request or bool(_EXPLICIT_RE.search(clean))
        durable = bool(
            _DURABLE_RE.search(clean)
            or _FUTURE_DIRECTIVE_RE.search(clean)
        )

        scope: MemoryScope = "global"
        if _PROJECT_RE.search(clean) or _NAMING_TASK_RE.search(clean):
            scope = "project"
        elif not durable and (_ONE_OFF_RE.search(clean) or _QUESTION_RE.search(clean)):
            scope = "thread"

        if explicit:
            if sensitivity == "sensitive":
                return self._result(
                    "suggest", scope, subject, 0.82,
                    "用户明确要求记住，但敏感信息仍需在记忆页确认",
                    sensitivity,
                )
            return self._result(
                "confirm", scope, subject, 0.98, "用户明确要求长期记住", sensitivity,
            )

        if _QUESTION_RE.search(clean):
            return self._result(
                "reject", "turn", subject, 0.05, "这是问题或请求，不是可复用事实",
                sensitivity,
            )

        recurrent = recurrence_count >= 2 and distinct_session_count >= 2
        style = memory_type == "style" or bool(_STYLE_RE.search(clean))

        if scope in {"thread", "project"} and not recurrent:
            return self._result(
                "observe", scope, subject, 0.35,
                "看起来与当前任务相关，先观察是否会再次出现",
                sensitivity,
            )
        if recurrent:
            score = min(0.92, 0.68 + 0.06 * min(distinct_session_count, 4))
            return self._result(
                "suggest", scope, subject, score,
                f"已在 {distinct_session_count} 个会话中重复出现，建议确认",
                sensitivity,
            )
        if durable and style:
            return self._result(
                "suggest", "global", subject, 0.82,
                "这是明确且可跨会话复用的表达偏好",
                sensitivity,
            )
        if durable:
            return self._result(
                "suggest", scope, subject, 0.74, "包含长期或默认约定，建议确认",
                sensitivity,
            )

        return self._result(
            "observe", scope if scope != "global" else "thread", subject, 0.25,
            "长期复用价值尚不明确，保留为对话证据但不形成建议",
            sensitivity,
        )

    @staticmethod
    def _result(
        decision: MemoryDecision,
        scope: MemoryScope,
        subject: MemorySubject,
        utility_score: float,
        reason: str,
        sensitivity: MemorySensitivity = "normal",
    ) -> MemoryPolicyResult:
        return MemoryPolicyResult(
            decision=decision,
            scope=scope,
            subject=subject,
            utility_score=max(0.0, min(1.0, utility_score)),
            reason=reason,
            sensitivity=sensitivity,
        )
