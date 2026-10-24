# -*- coding: utf-8 -*-
"""Simple, consented activation for proactive daily briefs.

The UI submits one product-level choice.  This boundary may turn a bounded set
of current, non-sensitive understanding atoms into public research directions,
then provisions the existing WatchTopic, grant, preview, and subscription
authorities.  It does not search the Web itself.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field

from ...agents.persona.user_understanding import UserUnderstandingStore
from ..reminders.models import WatchTopic, WatchTopicBriefUseRequest, WatchTopicCreateRequest
from ..reminders.repo import ReminderRepository
from ..reminders.service import ReminderService
from ..structured_utility import run_structured_utility_task
from .model_task import run_brief_model_task, require_brief_model_capability
from .models import (
    BriefActivationRequest,
    BriefActivationResult,
    BriefBudget,
    BriefGrantCreateRequest,
    BriefSubscriptionCreateRequest,
    StrictModel,
)
from .repo import DailyBriefRepository
from .service import ConsentRequired, DailyBriefService, ScopeFenceFailed


StructuredTaskCallable = Callable[..., Awaitable[Any]]


class MemoryTopicCandidate(StrictModel):
    title: str = Field(min_length=2, max_length=80)
    purpose: str = Field(min_length=2, max_length=240)
    source_clue_ids: list[str] = Field(min_length=1, max_length=3)


class MemoryTopicDiscovery(StrictModel):
    topics: list[MemoryTopicCandidate] = Field(default_factory=list, max_length=3)


_DISCOVERY_PROMPT = """\
你为澜台简报把少量、当前有效且非敏感的用户理解线索转换为公开研究方向。
这些线索只用于理解什么变化可能对用户有用，不是可直接搜索或公开复述的事实。
输出一到三个长期可复用的公开主题：标题应是公开领域或问题，purpose 说明要查证的变化。
不要把姓名、关系、健康、私人经历、内部项目名、文件名或线索原句写入标题和 purpose；
不要猜测线索之外的事实。没有适合公开研究的方向时返回空 topics。
每个方向只能引用输入中存在的 clue_id。"""


class DailyBriefActivationService:
    """Provision proactive briefs without exposing provider plumbing to users."""

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        agent_id: str,
        structured_task: StructuredTaskCallable = run_structured_utility_task,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.agent_id = agent_id
        self._structured_task = structured_task
        self._briefs = DailyBriefService(self.workspace_dir)
        self._reminders = ReminderService(ReminderRepository(self.workspace_dir))

    async def activate(self, request: BriefActivationRequest) -> BriefActivationResult:
        if not request.explicit_consent:
            raise ConsentRequired("开启自动挖掘与整理需要明确同意")
        async with DailyBriefRepository(self.workspace_dir).activation_guard(request.action_id):
            return await self._activate_locked(request)

    async def _activate_locked(self, request: BriefActivationRequest) -> BriefActivationResult:
        prior = await self._briefs.activation_result(request)
        if prior is not None:
            return prior
        snapshot = await self._briefs.get_workspace_snapshot()
        if not snapshot.feature_flags.research:
            raise ConsentRequired("当前构建尚未开放简报研究门禁")
        if self._structured_task is run_structured_utility_task:
            require_brief_model_capability(self.agent_id,
                allow_configured_model=request.configured_model_consent)

        topic_ids = list(dict.fromkeys(request.topic_ids))
        discovered_topic_ids: list[str] = []
        if request.use_memory_clues:
            if not request.memory_context_processing_consent:
                raise ConsentRequired(
                    "请先明确允许把选中的非敏感记忆线索交给当前模型提炼"
                )
            discovered = await self._discover_topics(request.action_id,
                configured_model_consent=request.configured_model_consent)
            discovered_topic_ids = [topic.id for topic in discovered]
            topic_ids.extend(discovered_topic_ids)
        if not topic_ids:
            raise ConsentRequired("没有可用的记忆线索，请先添加一个关注方向")

        return await self._briefs.activate_scope(request, topic_ids=topic_ids,
            discovered_topic_ids=discovered_topic_ids)

    async def _discover_topics(self, action_id: str, *, configured_model_consent: bool = False) -> list[WatchTopic]:
        atoms = UserUnderstandingStore(self.workspace_dir).association_palette(max_items=6)
        if not atoms:
            return []
        clue_ids = {atom.id for atom in atoms}
        existing = await self._reminders.list_watch_topics()
        previously_created = [
            topic
            for topic in existing
            if topic.metadata.get("daily_brief_activation_id") == action_id
        ]
        if previously_created:
            recovered: list[WatchTopic] = []
            for topic in previously_created:
                source_ids = list(
                    dict.fromkeys(topic.metadata.get("source_clue_ids") or [])
                )
                if not source_ids or not set(source_ids).issubset(clue_ids):
                    raise ScopeFenceFailed("自动发现的记忆线索已失效，请重新开启")
                recovered.append(
                    await self._confirm_topic(
                        topic,
                        purpose=topic.description or topic.title,
                        source_ids=source_ids,
                    )
                )
            return recovered

        payload = {
            "clues": [
                {
                    "clue_id": atom.id,
                    "statement": atom.statement,
                    "future_relevance": atom.future_relevance,
                }
                for atom in atoms
            ]
        }
        structured = await run_brief_model_task(
            self._structured_task,
            agent_id=self.agent_id,
            system_prompt=_DISCOVERY_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False),
            purpose="daily_brief_topic_discovery",
            result_model=MemoryTopicDiscovery,
            expected_provider_id=None,
            require_no_api_cost=not configured_model_consent,
            allow_model_fallback=not configured_model_consent,
            max_output_tokens=1_200,
            usage_session_id=f"daily-brief:activation:{action_id}",
        )
        created: list[WatchTopic] = []
        for index, candidate in enumerate(structured.value.topics):
            source_ids = list(dict.fromkeys(candidate.source_clue_ids))
            if not source_ids or not set(source_ids).issubset(clue_ids):
                raise ScopeFenceFailed("记忆选题引用了未授权或已失效的线索")
            topic_key = f"{action_id}:{index}"
            topic = next(
                (
                    item
                    for item in existing
                    if item.metadata.get("daily_brief_activation_key") == topic_key
                ),
                None,
            )
            if topic is None:
                topic = await self._reminders.create_watch_topic(
                    WatchTopicCreateRequest(
                        title=candidate.title,
                        description=candidate.purpose,
                        origin="daily_brief_memory_discovery",
                        metadata={
                            "daily_brief_activation_key": topic_key,
                            "daily_brief_activation_id": action_id,
                            "memory_topic_policy": "v1",
                            "memory_context_processing_scope": (
                                "selected_non_sensitive_clue_snippets"
                            ),
                            "source_clue_ids": source_ids,
                        },
                    )
                )
            created.append(
                await self._confirm_topic(
                    topic,
                    purpose=candidate.purpose,
                    source_ids=source_ids,
                )
            )
        return created

    async def _confirm_topic(
        self,
        topic: WatchTopic,
        *,
        purpose: str,
        source_ids: list[str],
    ) -> WatchTopic:
        """Finish a partially completed activation without rediscovering topics."""

        if topic.brief_status == "confirmed" and topic.enabled:
            return topic
        configured = await self._reminders.configure_watch_topic_for_brief(
            topic.id,
            WatchTopicBriefUseRequest(
                expected_revision=topic.revision,
                explicit_confirmation=True,
                status="confirmed",
                purpose=purpose,
                scope_refs=["public:web", "public:primary-documents"],
                evidence_refs=[f"understanding:{item}" for item in source_ids],
                exclusions=["private:ungranted", "unverified:rumor"],
            ),
        )
        if configured is None:
            raise ScopeFenceFailed("自动发现的关注方向未能保存")
        return configured
