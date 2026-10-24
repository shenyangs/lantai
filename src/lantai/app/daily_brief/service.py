# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from ..reminders.models import RemindersFile, WatchTopic
from ..reminders.repo import ReminderRepository
from .models import (
    BriefActivationRequest,
    BriefActivationResult,
    BriefBudget,
    BriefContextRef,
    BriefCostRecord,
    BriefCycle,
    BriefCycleView,
    BriefDeliveryReceipt,
    BriefDiscussionDraft,
    BriefDiscussionDraftCreateRequest,
    BriefDiscussionDraftView,
    BriefEdition,
    BriefFeedback,
    BriefFeedbackCreateRequest,
    BriefGrant,
    BriefGrantCreateRequest,
    BriefPublishRequest,
    BriefSubscription,
    BriefSubscriptionCreateRequest,
    BriefSubscriptionUpdateRequest,
    BriefSavedItem,
    BriefStorageStatus,
    BriefWorkspaceSnapshot,
    DailyBriefHomeCard,
    DailyBriefHomeSnapshot,
    DailyBriefStore,
    new_id,
    utc_now,
)
from .repo import DailyBriefRepository


class DailyBriefError(RuntimeError):
    pass


class ConsentRequired(DailyBriefError):
    pass


class ScopeFenceFailed(DailyBriefError):
    pass


class BriefNotFound(DailyBriefError):
    pass


class BriefConflict(DailyBriefError):
    pass


class BriefModelPolicyRejected(DailyBriefError):
    pass


class BriefModelUnavailable(DailyBriefError):
    pass


def _topics_by_id(data: RemindersFile) -> dict[str, WatchTopic]:
    return {topic.id: topic for topic in data.watch_topics}


def _find_grant(data: DailyBriefStore, grant_id: str) -> BriefGrant | None:
    return next((grant for grant in data.grants if grant.id == grant_id), None)


def _find_subscription(
    data: DailyBriefStore, subscription_id: str
) -> BriefSubscription | None:
    return next(
        (item for item in data.subscriptions if item.id == subscription_id),
        None,
    )


def _grant_allows_read(grant: BriefGrant, edition: BriefEdition) -> bool:
    return (
        grant.status in {"active", "consumed"}
        and grant.revision == edition.grant_revision
        and (grant.expires_at is None or grant.expires_at > utc_now())
    )


def _published_editions(data: DailyBriefStore) -> list[BriefEdition]:
    return sorted(
        (item for item in data.editions if item.status == "published"),
        key=lambda item: (
            item.published_at or item.checked_finished_at,
            item.revision,
        ),
        reverse=True,
    )


def _replace_by_id(items: Iterable, replacement) -> list:
    return [replacement if item.id == replacement.id else item for item in items]


def _tombstone_edition(edition: BriefEdition) -> BriefEdition:
    """Retain audit identity while erasing all potentially derived content."""

    return edition.model_copy(
        update={
            "status": "withdrawn",
            "title": "澜台简报",
            "summary": "",
            "coverage": [],
            "evidence": [],
            "items": [],
            "trends": [],
            "private_dependency_refs": [],
            "correction_note": "",
            "content_hash": "",
        }
    )


def _topic_scope_is_current(
    topics: dict[str, WatchTopic],
    expected: dict[str, int],
) -> bool:
    return bool(expected) and all(
        topic_id in topics
        and topics[topic_id].revision == revision
        and topics[topic_id].enabled
        and topics[topic_id].brief_status == "confirmed"
        and (
            topics[topic_id].brief_valid_until is None
            or topics[topic_id].brief_valid_until > utc_now()
        )
        for topic_id, revision in expected.items()
    )


class DailyBriefService:
    """Brief domain boundary. No method performs network or model calls."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self._repo = DailyBriefRepository(self.workspace_dir)
        self._reminders = ReminderRepository(self.workspace_dir)

    async def _load_authorities(self) -> tuple[DailyBriefStore, RemindersFile]:
        return await self._repo.load_with_topics(self._reminders.path)

    async def get_home_snapshot(self) -> DailyBriefHomeSnapshot:
        """Return compact metadata only; this never queues or resumes work."""

        data, reminders = await self._load_authorities()
        if not data.feature_flags.homepage_entry or data.invitation_hidden:
            return DailyBriefHomeSnapshot(
                state="hidden",
                visible=False,
                card=DailyBriefHomeCard(status_line="已隐藏"),
            )

        topics = _topics_by_id(reminders)
        active_subscriptions = [
            item for item in data.subscriptions if item.status == "active"
        ]
        paused_subscriptions = [
            item for item in data.subscriptions if item.status == "paused"
        ]
        if data.subscriptions and not any(
            item.home_visible and item.status != "archived"
            for item in data.subscriptions
        ):
            return DailyBriefHomeSnapshot(
                state="hidden",
                visible=False,
                card=DailyBriefHomeCard(status_line="已隐藏"),
            )

        current_editions = [
            edition
            for edition in _published_editions(data)
            if (grant := _find_grant(data, edition.grant_id)) is not None
            and _grant_allows_read(grant, edition)
            and _topic_scope_is_current(topics, edition.topic_revisions)
        ]
        latest = current_editions[0] if current_editions else None
        latest_cycle = max(
            data.cycles,
            key=lambda cycle: cycle.requested_at,
            default=None,
        )

        def card(
            status_line: str,
            hint: str = "",
            *,
            has_update: bool = False,
            stale: bool = False,
        ) -> DailyBriefHomeCard:
            return DailyBriefHomeCard(
                status_line=status_line,
                hint=hint,
                edition_id=latest.id if latest else None,
                edition_revision=latest.revision if latest else None,
                edition_date=latest.local_date if latest else None,
                item_count=len(latest.items) if latest else None,
                has_update=has_update,
                is_stale=stale,
            )

        if latest_cycle and latest_cycle.status in {"queued", "running"}:
            hint = (
                f"上一期 {latest.local_date.isoformat()} 仍可阅读"
                if latest
                else "可稍后回来查看"
            )
            return DailyBriefHomeSnapshot(
                state="generating", card=card("正在生成", hint)
            )

        if latest_cycle and latest_cycle.status == "failed":
            hint = (
                f"未完成更新，上一期为 {latest.local_date.isoformat()}"
                if latest
                else "未完成更新，点击查看原因"
            )
            return DailyBriefHomeSnapshot(
                state="failed", card=card("本次未完成", hint, stale=bool(latest))
            )

        if latest:
            if latest.outcome == "no_material_update":
                return DailyBriefHomeSnapshot(
                    state="no_update",
                    card=card(
                        f"{latest.local_date.isoformat()} 已检查",
                        "没有值得新增的发现",
                    ),
                )
            if latest.outcome == "partial":
                return DailyBriefHomeSnapshot(
                    state="partial",
                    card=card(
                        f"{latest.local_date.isoformat()} 部分完成",
                        "部分来源未能完成查证",
                        has_update=data.read_revisions.get(latest.id, 0) < latest.revision,
                    ),
                )
            return DailyBriefHomeSnapshot(
                state="ready",
                card=card(
                    latest.local_date.isoformat(),
                    "有新内容可阅读" if data.read_revisions.get(latest.id, 0) < latest.revision else "已阅读，可继续查看",
                    has_update=data.read_revisions.get(latest.id, 0) < latest.revision,
                ),
            )

        if paused_subscriptions:
            return DailyBriefHomeSnapshot(
                state="paused",
                card=card("已暂停", "点击查看当前设置"),
            )
        if active_subscriptions:
            return DailyBriefHomeSnapshot(
                state="not_run",
                card=card("尚未完成首次检查", "点击查看调度状态"),
            )
        return DailyBriefHomeSnapshot()

    async def get_workspace_snapshot(self) -> BriefWorkspaceSnapshot:
        data, reminders = await self._load_authorities()
        topic_authority = _topics_by_id(reminders)
        history = [
            edition
            for edition in _published_editions(data)
            if (grant := _find_grant(data, edition.grant_id)) is not None
            and _grant_allows_read(grant, edition)
            and _topic_scope_is_current(topic_authority, edition.topic_revisions)
        ]
        topics = [
            {
                "id": topic.id,
                "title": topic.title,
                "description": topic.description,
                "enabled": topic.enabled,
                "revision": topic.revision,
                "brief_status": topic.brief_status,
                "brief_purpose": topic.brief_purpose,
                "brief_scope_refs": topic.brief_scope_refs,
                "brief_evidence_refs": topic.brief_evidence_refs,
                "brief_exclusions": topic.brief_exclusions,
                "brief_valid_until": topic.brief_valid_until,
            }
            for topic in sorted(
                reminders.watch_topics, key=lambda item: item.updated_at, reverse=True
            )
        ]
        cycles = sorted(data.cycles, key=lambda item: item.requested_at, reverse=True)
        views = []
        for cycle in cycles[:50]:
            grant = _find_grant(data, cycle.grant_id)
            code = ""
            message = ""
            reason = cycle.failure_reason
            if cycle.status in {"failed", "cancelled", "invalidated"}:
                if "interrupted" in reason or "TimeoutError" in reason:
                    code, message = "interrupted", "生成中断或超时，可在检查连接后重试"
                elif "Policy" in reason or "budget" in reason:
                    code, message = "policy_or_budget", "模型条件或剩余预算不满足，请检查模型设置或重新授权"
                elif "Scope" in reason or "revoked" in reason:
                    code, message = "scope_changed", "关注方向或授权已变化，请重新开启"
                else:
                    code, message = "research_failed", "来源查证或模型生成未完成，可检查网络和模型后重试"
            has_successor = any(item.supersedes_cycle_id == cycle.id for item in cycles)
            remaining = self._remaining_budget(data, cycle, grant) if grant else None
            retryable = bool(cycle.status in {"failed", "partial", "cancelled"}
                and not has_successor and grant and grant.is_effective()
                and data.feature_flags.research
                and _topic_scope_is_current(topic_authority, cycle.topic_revisions)
                and remaining and remaining.max_searches >= len(cycle.topic_revisions)
                and remaining.max_source_reads > 0 and remaining.max_model_tokens >= 1500)
            costs = [item for item in data.costs if item.cycle_id == cycle.id]
            usage_uncertain = any(item.model_usage_status == "unknown" for item in costs)
            if usage_uncertain:
                code, message = "usage_unknown", "模型耗用尚未确认，继续生成前需要重新授权"
            views.append(BriefCycleView(id=cycle.id, subscription_id=cycle.subscription_id,
                status=cycle.status, stage=cycle.stage, local_date=cycle.local_date,
                requested_at=cycle.requested_at, started_at=cycle.started_at,
                finished_at=cycle.finished_at, supersedes_cycle_id=cycle.supersedes_cycle_id,
                failure_code=code, message=message, retryable=retryable,
                usage_uncertain=usage_uncertain, cost_cny=sum(item.cost_cny for item in costs),
                usage=BriefBudget(max_searches=min(100, sum(item.search_count for item in costs)),
                    max_source_reads=min(100, sum(item.source_read_count for item in costs)),
                    max_model_tokens=min(2_000_000, sum(item.model_tokens for item in costs)),
                    max_cost_usd=min(1000, sum(item.cost_usd for item in costs)))))
        issues = {}
        for subscription in data.subscriptions:
            grant = _find_grant(data, subscription.grant_id)
            if subscription.status == "active" and (not grant or not grant.is_effective()
                or grant.revision != subscription.grant_revision
                or not _topic_scope_is_current(topic_authority, subscription.topic_revisions)):
                issues[subscription.id] = "关注方向或授权已变化，请重新开启"
        if not data.feature_flags.research:
            activation_state = "disabled"
        elif cycles and cycles[0].status in {"queued", "running", "failed", "partial"}:
            activation_state = cycles[0].status
        elif history:
            activation_state = "ready"
        elif any(item.status == "active" for item in data.subscriptions):
            activation_state = "scheduled"
        elif topics:
            activation_state = "topics_only"
        else:
            activation_state = "not_configured"
        available = {(item.id, item.revision) for item in history}
        return BriefWorkspaceSnapshot(
            grants=data.grants,
            subscriptions=data.subscriptions,
            latest_edition=history[0] if history else None,
            history=history[:50],
            topics=topics,
            feature_flags=data.feature_flags,
            invitation_hidden=data.invitation_hidden,
            cycles=views,
            activation_state=activation_state,
            history_total=len(history),
            saved_items=[item for item in data.saved_items
                if (item.edition_id, item.edition_revision) in available],
            subscription_issues=issues,
            storage=BriefStorageStatus(
                store_bytes=self._repo.path.stat().st_size if self._repo.path.exists() else 0,
                edition_count=len(data.editions), cycle_count=len(data.cycles), cost_count=len(data.costs),
                capacity_warning=self._repo.path.exists() and self._repo.path.stat().st_size >= 20 * 1024 * 1024),
        )

    async def get_edition(self, edition_id: str, revision: int | None = None) -> BriefEdition:
        data, reminders = await self._load_authorities()
        if revision is not None:
            edition_id = data.edition_aliases.get(f"{edition_id}:{revision}", edition_id)
        topics = _topics_by_id(reminders)
        matches = [
            item
            for item in data.editions
            if item.id == edition_id and item.status in ({"published", "superseded"} if revision else {"published"})
            and (revision is None or item.revision == revision)
            and (grant := _find_grant(data, item.grant_id)) is not None
            and _grant_allows_read(grant, item)
            and _topic_scope_is_current(topics, item.topic_revisions)
        ]
        if not matches:
            raise BriefNotFound("edition not found")
        return max(matches, key=lambda item: item.revision)

    async def mark_edition_read(self, edition_id: str, revision: int) -> None:
        def updater(data: DailyBriefStore, reminders: RemindersFile) -> DailyBriefStore:
            resolved_id = data.edition_aliases.get(f"{edition_id}:{revision}", edition_id)
            edition = next((item for item in data.editions if item.id == resolved_id
                and item.revision == revision and item.status in {"published", "superseded"}), None)
            grant = _find_grant(data, edition.grant_id) if edition else None
            if edition is None or grant is None or not _grant_allows_read(grant, edition) or not _topic_scope_is_current(
                _topics_by_id(reminders), edition.topic_revisions):
                raise BriefNotFound("edition not found")
            return data.model_copy(update={"read_revisions": {
                **data.read_revisions, resolved_id: max(data.read_revisions.get(resolved_id, 0), revision)}})
        await self._repo.update_with_topics(self._reminders.path, updater)

    async def get_delivery_receipt(self, receipt_id: str) -> BriefDeliveryReceipt:
        data = await self._repo.load()
        receipt = next((item for item in data.delivery_receipts if item.id == receipt_id), None)
        if receipt is None:
            raise BriefNotFound("delivery receipt not found")
        await self.get_edition(receipt.edition_id, receipt.edition_revision)
        return receipt

    async def list_saved_items(self) -> list[BriefSavedItem]:
        return (await self.get_workspace_snapshot()).saved_items

    async def remove_saved_item(self, saved_id: str) -> None:
        await self._repo.update(lambda data: data.model_copy(update={"saved_items": [
            item for item in data.saved_items if item.id != saved_id]}))

    @staticmethod
    def _remaining_budget(data: DailyBriefStore, cycle: BriefCycle, grant: BriefGrant) -> BriefBudget:
        ids = {item.id for item in data.cycles if item.logical_key == cycle.logical_key}
        usage = [item for item in data.costs if item.cycle_id in ids]
        cny_limit = grant.budget.max_cost_cny if grant.model_policy == "configured" else 0
        spent_usd = sum(item.cost_usd for item in usage)
        spent_cny = sum(item.cost_cny for item in usage)
        uncertain = any(item.model_usage_status == "unknown" or item.cost_cny > cny_limit
            or item.cost_usd > grant.budget.max_cost_usd for item in usage)
        exhausted = ((grant.budget.max_cost_usd > 0 and spent_usd >= grant.budget.max_cost_usd)
            or (cny_limit > 0 and spent_cny >= cny_limit))
        return BriefBudget(
            max_searches=max(0, grant.budget.max_searches - sum(item.search_count for item in usage)),
            max_source_reads=max(0, grant.budget.max_source_reads - sum(item.source_read_count for item in usage)),
            max_model_tokens=0 if uncertain or exhausted else max(0, grant.budget.max_model_tokens - sum(item.model_tokens for item in usage)),
            max_cost_usd=max(0, grant.budget.max_cost_usd - spent_usd),
            max_cost_cny=max(0, cny_limit - spent_cny))

    async def research_history(self, cycle: BriefCycle, grant: BriefGrant):
        data = await self._repo.load()
        earlier = [item for item in data.editions if item.status in {"published", "superseded"}
            and item.subscription_id == cycle.subscription_id
            and (cycle.subscription_id is not None or item.grant_id == cycle.grant_id)
            and item.local_date < cycle.local_date]
        earlier.sort(key=lambda item: item.checked_finished_at, reverse=True)
        start = max((item.checked_finished_at for item in earlier), default=None)
        prior_by_url = {}
        for item in earlier[:30]:
            for source in item.evidence:
                if not source.retracted:
                    prior_by_url.setdefault(source.canonical_url, source)
        prior = list(prior_by_url.values())
        events = []
        for edition in earlier[:30]:
            sources = {item.id: item for item in edition.evidence if not item.retracted}
            for item in edition.items:
                # History is deduplication context, never fresh evidence. Do not
                # carry private analysis or stale topic scopes into a new call.
                if (item.private_dependency_refs or item.topic_id not in cycle.topic_revisions
                    or edition.topic_revisions.get(item.topic_id) != cycle.topic_revisions[item.topic_id]):
                    continue
                urls = [sources[key].canonical_url for key in item.evidence_ids if key in sources]
                if not urls:
                    continue
                events.append({"topic_id": item.topic_id, "edition_date": edition.local_date,
                    "title": item.title[:160], "fact": item.fact[:600], "source_urls": urls[:3]})
                if len(events) == 24:
                    break
            if len(events) == 24:
                break
        return self._remaining_budget(data, cycle, grant), start, [
            {"url": item.canonical_url, "content_hash": item.content_hash} for item in prior[:100]], events

    async def activation_result(self, request: BriefActivationRequest) -> BriefActivationResult | None:
        data = await self._repo.load()
        fingerprint = self._activation_fingerprint(request)
        if request.action_id in data.action_fingerprints and data.action_fingerprints[request.action_id] != fingerprint:
            raise BriefConflict("同一操作的选项已改变，请重新发起开启")
        return data.activation_results.get(request.action_id)

    @staticmethod
    def _activation_fingerprint(request: BriefActivationRequest) -> str:
        # Preserve pre-policy-v2 action identities. A new cost consent must still
        # change the fingerprint, so retries cannot silently broaden a grant.
        excluded = set() if request.configured_model_consent else {"configured_model_consent"}
        return hashlib.sha256(request.model_dump_json(exclude=excluded).encode()).hexdigest()

    async def activate_scope(self, request: BriefActivationRequest, *, topic_ids: list[str],
        discovered_topic_ids: list[str]) -> BriefActivationResult:
        """Commit grant and its executable intent as one atomic authority change."""
        result = None
        fingerprint = self._activation_fingerprint(request)
        def updater(data: DailyBriefStore, reminders: RemindersFile) -> DailyBriefStore:
            nonlocal result
            previous = data.activation_results.get(request.action_id)
            if previous:
                if data.action_fingerprints.get(request.action_id) != fingerprint:
                    raise BriefConflict("同一操作的选项已改变，请重新发起开启")
                result = previous
                return data
            if not request.explicit_consent or not data.feature_flags.research:
                raise ConsentRequired("需要明确开启简报研究")
            if request.native_notifications and not data.feature_flags.native_notification:
                raise ConsentRequired("native notifications are not enabled for this build")
            topics = _topics_by_id(reminders)
            selected = list(dict.fromkeys(topic_ids))
            revisions = {key: topics[key].revision for key in selected if key in topics}
            if not selected or not _topic_scope_is_current(topics, revisions) or len(revisions) != len(selected):
                raise ScopeFenceFailed("关注方向已经变化，请重新确认")
            grant = BriefGrant(one_time=request.mode == "preview", topic_ids=selected,
                public_source_discovery=True, public_source_read=True,
                cloud_processing="public_only", search_provider="lantai_auto", model_provider="auto",
                model_policy="configured" if request.configured_model_consent else "free_only",
                budget=BriefBudget(max_searches=max(6, len(selected)),
                    max_source_reads=max(6, len(selected) * 2), max_model_tokens=4000,
                    max_cost_usd=1 if request.configured_model_consent else 0,
                    max_cost_cny=10 if request.configured_model_consent else 0))
            cycle = None
            subscription = None
            if grant.one_time:
                cycle = BriefCycle(logical_key=f"preview:{grant.id}", grant_id=grant.id,
                    grant_revision=grant.revision, topic_revisions=revisions,
                    occurrence_id=f"manual:{request.action_id}",
                    local_date=utc_now().astimezone(ZoneInfo(request.timezone)).date(),
                    timezone=request.timezone, manual=True)
            else:
                subscription = BriefSubscription(grant_id=grant.id, grant_revision=grant.revision,
                    topic_revisions=revisions, timezone=request.timezone, local_time=request.local_time,
                    home_visible=request.home_visible, native_notifications=request.native_notifications,
                    quiet_hours_start=request.quiet_hours_start, quiet_hours_end=request.quiet_hours_end)
            result = BriefActivationResult(grant=grant, topic_ids=selected,
                discovered_topic_ids=discovered_topic_ids, cycle=cycle, subscription=subscription,
                execution_state="queued" if cycle else "scheduled")
            return data.model_copy(update={"grants": [grant, *data.grants],
                "cycles": ([cycle] if cycle else []) + data.cycles,
                "subscriptions": ([subscription] if subscription else []) + data.subscriptions,
                "activation_results": {**data.activation_results, request.action_id: result},
                "action_fingerprints": {**data.action_fingerprints, request.action_id: fingerprint}})
        await self._repo.update_with_topics(self._reminders.path, updater)
        assert result is not None
        return result

    async def list_editions(self, *, limit: int = 50, offset: int = 0) -> list[BriefEdition]:
        data, reminders = await self._load_authorities()
        topics = _topics_by_id(reminders)
        visible = [
            edition
            for edition in _published_editions(data)
            if (grant := _find_grant(data, edition.grant_id)) is not None
            and _grant_allows_read(grant, edition)
            and _topic_scope_is_current(topics, edition.topic_revisions)
        ]
        return visible[max(0, offset):max(0, offset) + min(100, max(1, limit))]

    async def set_invitation_hidden(self, hidden: bool) -> None:
        await self._repo.update(
            lambda data: data.model_copy(update={"invitation_hidden": hidden})
        )

    async def create_grant(self, request: BriefGrantCreateRequest) -> BriefGrant:
        if not request.explicit_consent:
            raise ConsentRequired("explicit brief authorization is required")
        if not request.topic_ids:
            raise ConsentRequired("at least one explicitly selected topic is required")
        if request.cloud_processing == "approved_context" and not request.allowed_context_refs:
            raise ConsentRequired("approved_context requires explicit context references")
        if request.cloud_processing != "approved_context" and request.allowed_context_refs:
            raise ConsentRequired(
                "private context references require approved_context authorization"
            )
        if request.expires_at is not None and request.expires_at <= utc_now():
            raise ConsentRequired("brief authorization must not already be expired")
        created: BriefGrant | None = None

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal created
            prior_id = data.action_results.get(request.action_id)
            if prior_id:
                created = _find_grant(data, prior_id)
                if created is None:
                    raise BriefConflict("idempotency record is inconsistent")
                return data
            topics = _topics_by_id(reminders)
            selected = {topic_id: topics.get(topic_id) for topic_id in request.topic_ids}
            if any(topic is None for topic in selected.values()):
                raise ScopeFenceFailed("selected topic does not exist")
            if any(
                topic.brief_status != "confirmed"
                or not topic.enabled
                or (
                    topic.brief_valid_until is not None
                    and topic.brief_valid_until <= utc_now()
                )
                for topic in selected.values()
                if topic
            ):
                raise ConsentRequired(
                    "every topic must be enabled and explicitly confirmed for briefs"
                )
            created = BriefGrant(
                one_time=request.one_time,
                topic_ids=list(dict.fromkeys(request.topic_ids)),
                public_source_discovery=request.public_source_discovery,
                public_source_read=request.public_source_read,
                cloud_processing=request.cloud_processing,
                allowed_context_refs=list(dict.fromkeys(request.allowed_context_refs)),
                search_provider=request.search_provider,
                model_provider=request.model_provider,
                budget=request.budget,
                expires_at=request.expires_at,
            )
            return data.model_copy(
                update={
                    "grants": [created, *data.grants],
                    "processed_action_ids": [
                        request.action_id,
                        *data.processed_action_ids,
                    ],
                    "action_results": {
                        **data.action_results,
                        request.action_id: created.id,
                    },
                }
            )

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert created is not None
        return created

    async def revoke_grant(
        self,
        grant_id: str,
        *,
        expected_revision: int,
        action_id: str,
    ) -> BriefGrant:
        revoked: BriefGrant | None = None
        now = utc_now()

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal revoked
            if action_id in data.action_results:
                prior_id = data.action_results[action_id]
                revoked = _find_grant(data, prior_id)
                if revoked is None:
                    raise BriefConflict("idempotency record is inconsistent")
                return data
            grant = _find_grant(data, grant_id)
            if grant is None:
                raise BriefNotFound("grant not found")
            if grant.revision != expected_revision:
                raise BriefConflict("grant revision changed")
            revoked = grant.model_copy(
                update={
                    "status": "revoked",
                    "revision": grant.revision + 1,
                    "revoked_at": now,
                }
            )
            cycles = [
                cycle.model_copy(
                    update={
                        "status": "invalidated",
                        "finished_at": now,
                        "failure_reason": "grant_revoked",
                    }
                )
                if cycle.grant_id == grant_id
                and cycle.status in {"queued", "running"}
                else cycle
                for cycle in data.cycles
            ]
            affected_edition_ids = {
                edition.id for edition in data.editions if edition.grant_id == grant_id
            }
            editions = [
                _tombstone_edition(edition)
                if edition.id in affected_edition_ids
                else edition
                for edition in data.editions
            ]
            edition_ids = {
                edition.id
                for edition in editions
                if edition.id in affected_edition_ids
            }
            drafts = [
                draft.model_copy(
                    update={
                        "status": "invalidated",
                        "text": "",
                        "invalidated_at": now,
                    }
                )
                if draft.context_ref.edition_id in edition_ids
                and draft.status == "draft"
                else draft
                for draft in data.discussion_drafts
            ]
            deliveries = [
                receipt.model_copy(
                    update={"state": "suppressed", "error": "grant_revoked"}
                )
                if receipt.edition_id in edition_ids
                and receipt.state in {"pending", "failed"}
                else receipt
                for receipt in data.delivery_receipts
            ]
            return data.model_copy(
                update={
                    "grants": _replace_by_id(data.grants, revoked),
                    "cycles": cycles,
                    "editions": editions,
                    "discussion_drafts": drafts,
                    "delivery_receipts": deliveries,
                    "processed_action_ids": [action_id, *data.processed_action_ids],
                    "action_results": {**data.action_results, action_id: revoked.id},
                }
            )

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert revoked is not None
        return revoked

    async def create_subscription(
        self, request: BriefSubscriptionCreateRequest
    ) -> BriefSubscription:
        created: BriefSubscription | None = None

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal created
            prior_id = data.action_results.get(request.action_id)
            if prior_id:
                created = _find_subscription(data, prior_id)
                if created is None:
                    raise BriefConflict("idempotency record is inconsistent")
                return data
            grant = _find_grant(data, request.grant_id)
            if grant is None or not grant.is_effective():
                raise ConsentRequired("an active brief grant is required")
            if grant.one_time:
                raise ConsentRequired("a one-time preview grant cannot create a subscription")
            topics = _topics_by_id(reminders)
            topic_ids = list(dict.fromkeys(request.topic_ids))
            if not set(topic_ids).issubset(set(grant.topic_ids)):
                raise ScopeFenceFailed("subscription topics exceed the grant")
            topic_revisions = {
                topic_id: topics[topic_id].revision
                for topic_id in topic_ids
                if topic_id in topics
                and topics[topic_id].enabled
                and topics[topic_id].brief_status == "confirmed"
            }
            if len(topic_revisions) != len(topic_ids):
                raise ScopeFenceFailed("topic scope is missing, disabled, or unconfirmed")
            if request.native_notifications and not data.feature_flags.native_notification:
                raise ConsentRequired("native notifications are not enabled for this build")
            created = BriefSubscription(
                grant_id=grant.id,
                grant_revision=grant.revision,
                topic_revisions=topic_revisions,
                timezone=request.timezone,
                local_time=request.local_time,
                home_visible=request.home_visible,
                native_notifications=request.native_notifications,
                quiet_hours_start=request.quiet_hours_start,
                quiet_hours_end=request.quiet_hours_end,
            )
            return data.model_copy(
                update={
                    "subscriptions": [created, *data.subscriptions],
                    "processed_action_ids": [request.action_id, *data.processed_action_ids],
                    "action_results": {
                        **data.action_results,
                        request.action_id: created.id,
                    },
                }
            )

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert created is not None
        return created

    async def update_subscription(
        self,
        subscription_id: str,
        request: BriefSubscriptionUpdateRequest,
    ) -> BriefSubscription:
        updated: BriefSubscription | None = None
        now = utc_now()

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal updated
            prior_id = data.action_results.get(request.action_id)
            if prior_id:
                updated = _find_subscription(data, prior_id)
                if updated is None:
                    raise BriefConflict("idempotency record is inconsistent")
                return data
            item = _find_subscription(data, subscription_id)
            if item is None:
                raise BriefNotFound("subscription not found")
            if item.revision != request.expected_revision:
                raise BriefConflict("subscription revision changed")
            raw_changes = request.model_dump(
                exclude={"expected_revision", "action_id"}, exclude_unset=True
            )
            changes = {
                key: value
                for key, value in raw_changes.items()
                if value is not None
                or key in {"quiet_hours_start", "quiet_hours_end"}
            }
            next_status = changes.get("status", item.status)
            grant = _find_grant(data, item.grant_id)
            if next_status == "active":
                if (
                    grant is None
                    or not grant.is_effective(now)
                    or grant.revision != item.grant_revision
                ):
                    raise ScopeFenceFailed("subscription grant is no longer current")
                if not _topic_scope_is_current(
                    _topics_by_id(reminders), item.topic_revisions
                ):
                    raise ScopeFenceFailed("subscription topics are no longer current")
            next_native = changes.get(
                "native_notifications", item.native_notifications
            )
            if next_native and not data.feature_flags.native_notification:
                raise ConsentRequired(
                    "native notifications are not enabled for this build"
                )
            schedule_changed = bool({"timezone", "local_time"}.intersection(changes))
            updated = item.model_copy(
                update={
                    **changes,
                    "revision": item.revision + 1,
                    "schedule_revision": item.schedule_revision
                    + (1 if schedule_changed else 0),
                    "updated_at": now,
                }
            )
            cycles = data.cycles
            deliveries = data.delivery_receipts
            if item.status == "active" and next_status in {"paused", "archived"}:
                cycles = [
                    cycle.model_copy(
                        update={
                            "status": "invalidated",
                            "finished_at": now,
                            "failure_reason": "subscription_paused",
                        }
                    )
                    if cycle.subscription_id == item.id
                    and cycle.status in {"queued", "running"}
                    else cycle
                    for cycle in cycles
                ]
                edition_ids = {
                    edition.id
                    for edition in data.editions
                    if edition.subscription_id == item.id
                }
                deliveries = [
                    receipt.model_copy(
                        update={
                            "state": "suppressed",
                            "error": "subscription_paused",
                        }
                    )
                    if receipt.edition_id in edition_ids
                    and receipt.state in {"pending", "failed"}
                    else receipt
                    for receipt in deliveries
                ]
            return data.model_copy(
                update={
                    "subscriptions": _replace_by_id(data.subscriptions, updated),
                    "cycles": cycles,
                    "delivery_receipts": deliveries,
                    "processed_action_ids": [request.action_id, *data.processed_action_ids],
                    "action_results": {
                        **data.action_results,
                        request.action_id: updated.id,
                    },
                }
            )

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert updated is not None
        return updated

    async def create_preview_cycle(
        self,
        *,
        grant_id: str,
        local_date: date,
        timezone: str = "Asia/Shanghai",
        action_id: str,
    ) -> BriefCycle:
        created: BriefCycle | None = None

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal created
            prior_id = data.action_results.get(action_id)
            if prior_id:
                created = next(
                    (cycle for cycle in data.cycles if cycle.id == prior_id), None
                )
                if created is None:
                    raise BriefConflict("idempotency record is inconsistent")
                return data
            grant = _find_grant(data, grant_id)
            if grant is None or not grant.is_effective() or not grant.one_time:
                raise ConsentRequired("an active one-time preview grant is required")
            topics = _topics_by_id(reminders)
            topic_revisions = {
                topic_id: topics[topic_id].revision
                for topic_id in grant.topic_ids
                if topic_id in topics
                and topics[topic_id].enabled
                and topics[topic_id].brief_status == "confirmed"
            }
            if len(topic_revisions) != len(grant.topic_ids):
                raise ScopeFenceFailed("preview topic scope is no longer current")
            # A one-time grant represents one research attempt, not one attempt per
            # calendar day.  Keep the first cycle's date/timezone as its audit
            # identity when a later click arrives with a new action id.
            logical_key = f"preview:{grant.id}"
            existing = next(
                (cycle for cycle in data.cycles if cycle.logical_key == logical_key),
                None,
            )
            if existing:
                created = existing
                return data.model_copy(
                    update={
                        "processed_action_ids": [action_id, *data.processed_action_ids],
                        "action_results": {**data.action_results, action_id: existing.id},
                    }
                )
            created = BriefCycle(
                logical_key=logical_key,
                grant_id=grant.id,
                grant_revision=grant.revision,
                topic_revisions=topic_revisions,
                occurrence_id=f"manual:{action_id}",
                local_date=local_date,
                timezone=timezone,
                manual=True,
            )
            return data.model_copy(
                update={
                    "cycles": [created, *data.cycles],
                    "processed_action_ids": [action_id, *data.processed_action_ids],
                    "action_results": {**data.action_results, action_id: created.id},
                }
            )

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert created is not None
        return created

    async def claim_occurrence(
        self,
        *,
        subscription_id: str,
        occurrence_id: str,
        local_date: date,
    ) -> BriefCycle:
        """Create one logical daily cycle; missed days collapse to the latest date."""

        claimed: BriefCycle | None = None
        now = utc_now()

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal claimed
            subscription = _find_subscription(data, subscription_id)
            if subscription is None or subscription.status != "active":
                raise ScopeFenceFailed("subscription is not active")
            grant = _find_grant(data, subscription.grant_id)
            if (
                grant is None
                or not grant.is_effective(now)
                or grant.revision != subscription.grant_revision
            ):
                raise ScopeFenceFailed("subscription grant is no longer current")
            if not _topic_scope_is_current(
                _topics_by_id(reminders), subscription.topic_revisions
            ):
                raise ScopeFenceFailed("subscription topics are no longer current")
            logical_key = f"subscription:{subscription.id}:{local_date.isoformat()}"
            existing = next(
                (cycle for cycle in data.cycles if cycle.logical_key == logical_key),
                None,
            )
            if existing:
                raise BriefConflict("occurrence already has an attempt; explicit retry is required")

            older_pending = [
                cycle.model_copy(
                    update={
                        "status": "cancelled",
                        "finished_at": now,
                        "failure_reason": "coalesced_to_latest_occurrence",
                    }
                )
                if cycle.subscription_id == subscription_id
                and cycle.local_date < local_date
                and cycle.status in {"queued", "failed"}
                else cycle
                for cycle in data.cycles
            ]
            claimed = BriefCycle(
                logical_key=logical_key,
                subscription_id=subscription.id,
                grant_id=grant.id,
                grant_revision=grant.revision,
                topic_revisions=subscription.topic_revisions,
                occurrence_id=occurrence_id,
                local_date=local_date,
                timezone=subscription.timezone,
                schedule_revision=subscription.schedule_revision,
                status="running",
                attempt_count=1,
                started_at=now,
                execution_token=new_id("execution"),
                lease_expires_at=now + timedelta(seconds=180),
                stage="claimed",
            )
            return data.model_copy(update={"cycles": [claimed, *older_pending]})

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert claimed is not None
        return claimed

    async def claim_preview_cycle(self, cycle_id: str) -> BriefCycle:
        return await self.claim_queued_cycle(cycle_id, preview_only=True)

    async def claim_queued_cycle(self, cycle_id: str, *, preview_only: bool = False) -> BriefCycle:
        claimed: BriefCycle | None = None
        now = utc_now()

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal claimed
            cycle = next((item for item in data.cycles if item.id == cycle_id), None)
            if cycle is None or (preview_only and not cycle.manual):
                raise BriefNotFound("preview cycle not found")
            if not data.feature_flags.research:
                raise ConsentRequired("brief research is not enabled")
            if cycle.status != "queued":
                raise BriefConflict("preview cycle has already been claimed")
            grant = _find_grant(data, cycle.grant_id)
            if (
                grant is None
                or not grant.is_effective(now)
                or grant.revision != cycle.grant_revision
            ):
                raise ScopeFenceFailed("preview grant is no longer current")
            if not _topic_scope_is_current(
                _topics_by_id(reminders), cycle.topic_revisions
            ):
                raise ScopeFenceFailed("preview topics are no longer current")
            if cycle.subscription_id:
                subscription = _find_subscription(data, cycle.subscription_id)
                if subscription is None or subscription.status != "active":
                    raise ScopeFenceFailed("subscription is not active")
            claimed = cycle.model_copy(
                update={
                    "status": "running",
                    "attempt_count": cycle.attempt_count + 1,
                    "started_at": now,
                    "execution_token": new_id("execution"),
                    "lease_expires_at": now + timedelta(seconds=180),
                    "stage": "claimed",
                }
            )
            return data.model_copy(
                update={"cycles": _replace_by_id(data.cycles, claimed)}
            )

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert claimed is not None
        return claimed

    async def begin_execution(self, cycle_id: str, execution_token: str) -> BriefCycle:
        """CAS the handoff from scheduler to runner, including across processes."""
        current: BriefCycle | None = None
        def updater(data: DailyBriefStore) -> DailyBriefStore:
            nonlocal current
            cycle = next((item for item in data.cycles if item.id == cycle_id), None)
            if (cycle is None or cycle.status != "running"
                or cycle.execution_token != execution_token
                or cycle.execution_started_at is not None
                or (cycle.lease_expires_at and cycle.lease_expires_at <= utc_now())):
                raise BriefConflict("attempt is no longer available to this runner")
            current = cycle.model_copy(update={"execution_started_at": utc_now(), "stage": "research"})
            return data.model_copy(update={"cycles": _replace_by_id(data.cycles, current)})
        await self._repo.update(updater)
        assert current is not None
        return current

    async def heartbeat(self, cycle_id: str, execution_token: str) -> None:
        def updater(data: DailyBriefStore) -> DailyBriefStore:
            cycle = next((item for item in data.cycles if item.id == cycle_id), None)
            now = utc_now()
            if (cycle is None or cycle.status != "running"
                or cycle.execution_token != execution_token
                or cycle.lease_expires_at is None or cycle.lease_expires_at <= now):
                raise ScopeFenceFailed("attempt execution was revoked")
            updated = cycle.model_copy(update={"lease_expires_at": now + timedelta(seconds=180)})
            return data.model_copy(update={"cycles": _replace_by_id(data.cycles, updated)})
        await self._repo.update(updater)

    async def recover_interrupted_cycles(self) -> int:
        """Make a crashed attempt visible; never replay a possibly charged call."""
        recovered = 0
        now = utc_now()
        def updater(data: DailyBriefStore) -> DailyBriefStore:
            nonlocal recovered
            cycles = []
            for cycle in data.cycles:
                deadline = cycle.lease_expires_at or (
                    cycle.started_at + timedelta(seconds=180) if cycle.started_at else now
                )
                if cycle.status == "running" and deadline <= now:
                    recovered += 1
                    cycle = cycle.model_copy(update={"status": "failed", "finished_at": now,
                        "failure_reason": "interrupted", "stage": "failed", "execution_token": ""})
                cycles.append(cycle)
            return data.model_copy(update={"cycles": cycles}) if recovered else data
        await self._repo.update(updater)
        return recovered

    async def retry_cycle(self, cycle_id: str, *, action_id: str, explicit_consent: bool) -> BriefCycle:
        if not explicit_consent:
            raise ConsentRequired("explicit retry consent is required")
        created: BriefCycle | None = None
        def updater(data: DailyBriefStore, reminders: RemindersFile) -> DailyBriefStore:
            nonlocal created
            prior_id = data.action_results.get(action_id)
            if prior_id:
                created = next((item for item in data.cycles if item.id == prior_id), None)
                if created is None:
                    raise BriefConflict("idempotency record is inconsistent")
                return data
            prior = next((item for item in data.cycles if item.id == cycle_id), None)
            if prior is None:
                raise BriefNotFound("cycle not found")
            if prior.status not in {"failed", "partial", "cancelled"}:
                raise BriefConflict("only a terminal unsuccessful attempt can be retried")
            if any(item.logical_key == prior.logical_key and item.status in {"queued", "running"} for item in data.cycles):
                raise BriefConflict("another attempt is already pending")
            grant = _find_grant(data, prior.grant_id)
            if (grant is None or not grant.is_effective() or grant.revision != prior.grant_revision
                or not _topic_scope_is_current(_topics_by_id(reminders), prior.topic_revisions)):
                raise ScopeFenceFailed("renew authorization before retrying this scope")
            attempt_ids = {item.id for item in data.cycles if item.logical_key == prior.logical_key}
            costs = [item for item in data.costs if item.cycle_id in attempt_ids]
            cny_limit = grant.budget.max_cost_cny if grant.model_policy == "configured" else 0
            if any(item.model_usage_status == "unknown" or item.cost_cny > cny_limit
                or item.cost_usd > grant.budget.max_cost_usd for item in costs):
                raise ScopeFenceFailed("brief usage unknown or unauthorized cost; renew authorization")
            if (sum(item.search_count for item in costs) >= grant.budget.max_searches
                or sum(item.source_read_count for item in costs) >= grant.budget.max_source_reads
                or sum(item.model_tokens for item in costs) >= grant.budget.max_model_tokens
                or (grant.budget.max_cost_usd > 0 and sum(item.cost_usd for item in costs) >= grant.budget.max_cost_usd)
                or (cny_limit > 0 and sum(item.cost_cny for item in costs) >= cny_limit)):
                raise ScopeFenceFailed("brief budget exhausted; renew authorization")
            created = BriefCycle(logical_key=prior.logical_key, subscription_id=prior.subscription_id,
                grant_id=prior.grant_id, grant_revision=prior.grant_revision,
                topic_revisions=prior.topic_revisions, occurrence_id=f"retry:{action_id}",
                local_date=prior.local_date, timezone=prior.timezone, schedule_revision=prior.schedule_revision,
                manual=prior.manual, attempt_count=prior.attempt_count, supersedes_cycle_id=prior.id)
            return data.model_copy(update={"cycles": [created, *data.cycles],
                "processed_action_ids": [action_id, *data.processed_action_ids],
                "action_results": {**data.action_results, action_id: created.id}})
        await self._repo.update_with_topics(self._reminders.path, updater)
        assert created is not None
        return created

    async def get_research_scope(
        self,
        cycle_id: str,
    ) -> tuple[BriefCycle, BriefGrant, list[WatchTopic]]:
        """Read the current, permission-fenced input authority for one run."""

        data, reminders = await self._repo.load_with_topics(self._reminders.path)
        if not data.feature_flags.research:
            raise ConsentRequired("brief research is not enabled")
        cycle = next((item for item in data.cycles if item.id == cycle_id), None)
        if cycle is None:
            raise BriefNotFound("cycle not found")
        if cycle.status != "running":
            raise BriefConflict("cycle is not running")
        grant = _find_grant(data, cycle.grant_id)
        if (
            grant is None
            or not grant.is_effective()
            or grant.revision != cycle.grant_revision
        ):
            raise ScopeFenceFailed("brief grant is no longer current")
        topics = _topics_by_id(reminders)
        if not _topic_scope_is_current(topics, cycle.topic_revisions):
            raise ScopeFenceFailed("brief topics are no longer current")
        return cycle, grant, [topics[topic_id] for topic_id in cycle.topic_revisions]

    async def fail_cycle(self, cycle_id: str, reason: str, *, execution_token: str | None = None) -> BriefCycle:
        failed: BriefCycle | None = None
        now = utc_now()

        def updater(data: DailyBriefStore) -> DailyBriefStore:
            nonlocal failed
            cycle = next((item for item in data.cycles if item.id == cycle_id), None)
            if cycle is None:
                raise BriefNotFound("cycle not found")
            if execution_token is not None and cycle.execution_token != execution_token:
                raise ScopeFenceFailed("attempt execution was revoked")
            if cycle.status not in {"queued", "running"} and not (
                cycle.status == "failed" and cycle.failure_reason == "interrupted" and reason.startswith("TimeoutError")
            ):
                failed = cycle
                return data
            failed = cycle.model_copy(
                update={
                    "status": "failed",
                    "finished_at": now,
                    "failure_reason": reason[:500],
                    "stage": "failed",
                }
            )
            return data.model_copy(
                update={"cycles": _replace_by_id(data.cycles, failed)}
            )

        await self._repo.update(updater)
        assert failed is not None
        return failed

    async def publish(self, request: BriefPublishRequest) -> BriefEdition:
        """Durably publish, advance successful watermarks, then enqueue delivery."""

        published: BriefEdition | None = None
        reminders_path = self._reminders.path
        now = utc_now()

        def updater(data: DailyBriefStore, reminders: RemindersFile) -> DailyBriefStore:
            nonlocal published
            cycle = next(
                (item for item in data.cycles if item.id == request.cycle_id), None
            )
            if cycle is None or cycle.status not in {"running", "succeeded", "partial"}:
                raise ScopeFenceFailed("cycle is not publishable")
            if request.execution_token and (request.execution_token != cycle.execution_token
                or (cycle.status == "running" and cycle.lease_expires_at and cycle.lease_expires_at <= now)):
                raise ScopeFenceFailed("attempt execution was revoked before publication")
            if request.local_date != cycle.local_date or request.timezone != cycle.timezone:
                raise ScopeFenceFailed("publication date does not match its cycle")
            grant = _find_grant(data, cycle.grant_id)
            if (
                grant is None
                or not grant.is_effective(now)
                or grant.revision != request.expected_grant_revision
                or grant.revision != cycle.grant_revision
            ):
                raise ScopeFenceFailed("grant changed before publication")
            private_dependency_refs = set(request.private_dependency_refs)
            private_dependency_refs.update(
                dependency_ref
                for item in request.items
                for dependency_ref in item.private_dependency_refs
            )
            if private_dependency_refs and grant.cloud_processing != "approved_context":
                raise ScopeFenceFailed(
                    "private derivatives require approved_context authorization"
                )
            if private_dependency_refs - set(grant.allowed_context_refs):
                raise ScopeFenceFailed(
                    "private derivatives exceed the explicit context grant"
                )
            topics = _topics_by_id(reminders)
            if (
                request.expected_topic_revisions != cycle.topic_revisions
                or not _topic_scope_is_current(topics, request.expected_topic_revisions)
            ):
                raise ScopeFenceFailed("topic scope changed before publication")

            canonical = request.model_dump_json(exclude={"cycle_id"})
            content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            same_cycle = [
                edition for edition in data.editions if edition.cycle_id == cycle.id
            ]
            if same_cycle:
                existing = max(same_cycle, key=lambda item: item.revision)
                if existing.content_hash == content_hash and existing.status == "published":
                    published = existing
                    return data

            same_logical_edition = [
                edition
                for edition in data.editions
                if edition.subscription_id == cycle.subscription_id
                and edition.local_date == cycle.local_date
                and (cycle.subscription_id is not None or edition.grant_id == cycle.grant_id)
            ]
            prior = (
                max(same_logical_edition, key=lambda item: item.revision)
                if same_logical_edition
                else None
            )
            published = BriefEdition(
                id=prior.id if prior else new_id("edition"),
                revision=(prior.revision + 1) if prior else 1,
                cycle_id=cycle.id,
                subscription_id=cycle.subscription_id,
                grant_id=grant.id,
                grant_revision=grant.revision,
                topic_revisions=cycle.topic_revisions,
                status="published",
                outcome=request.outcome,
                local_date=request.local_date,
                timezone=request.timezone,
                title=request.title,
                summary=request.summary,
                coverage=request.coverage,
                evidence=request.evidence,
                items=request.items,
                trends=request.trends,
                private_dependency_refs=list(
                    dict.fromkeys(request.private_dependency_refs)
                ),
                checked_started_at=request.checked_started_at,
                checked_finished_at=request.checked_finished_at,
                published_at=now,
                supersedes_id=request.supersedes_id,
                correction_note=request.correction_note,
                content_hash=content_hash,
            )
            editions = [
                edition.model_copy(update={"status": "superseded"})
                if prior is not None
                and edition.id == prior.id
                and edition.revision == prior.revision
                and edition.status == "published"
                else edition
                for edition in data.editions
            ]
            editions = [published, *editions]
            cycle_status = "partial" if request.outcome == "partial" else "succeeded"
            completed_cycle = cycle.model_copy(
                update={
                    "status": cycle_status,
                    "finished_at": request.checked_finished_at,
                    "edition_id": published.id,
                    "failure_reason": "",
                    "stage": "published",
                }
            )
            subscriptions = data.subscriptions
            subscription = (
                _find_subscription(data, cycle.subscription_id)
                if cycle.subscription_id
                else None
            )
            checks_complete = all(
                coverage.status == "complete" for coverage in request.coverage
            )
            if subscription is not None and checks_complete:
                next_subscription = subscription.model_copy(
                    update={
                        "last_successful_check_at": request.checked_finished_at,
                        "last_successful_local_date": cycle.local_date,
                        "updated_at": now,
                    }
                )
                subscriptions = _replace_by_id(subscriptions, next_subscription)

            deliveries = list(data.delivery_receipts)
            deliveries = [
                receipt.model_copy(
                    update={
                        "state": "suppressed",
                        "error": "edition_superseded",
                    }
                )
                if receipt.edition_id == published.id
                and receipt.edition_revision < published.revision
                and receipt.state in {"pending", "failed"}
                else receipt
                for receipt in deliveries
            ]
            if subscription and subscription.home_visible:
                deliveries.insert(
                    0,
                    BriefDeliveryReceipt(
                        edition_id=published.id,
                        edition_revision=published.revision,
                        subscription_id=subscription.id,
                        channel="home",
                        state="submitted",
                    ),
                )
            if (
                subscription
                and subscription.native_notifications
                and data.feature_flags.native_notification
            ):
                deliveries.insert(
                    0,
                    BriefDeliveryReceipt(
                        edition_id=published.id,
                        edition_revision=published.revision,
                        subscription_id=subscription.id,
                        channel="native",
                        state="pending",
                    ),
                )
            if grant.one_time:
                consumed = grant.model_copy(
                    update={"status": "consumed"}
                )
                grants = _replace_by_id(data.grants, consumed)
            else:
                grants = data.grants
            return data.model_copy(
                update={
                    "grants": grants,
                    "subscriptions": subscriptions,
                    "cycles": _replace_by_id(data.cycles, completed_cycle),
                    "editions": editions,
                    "delivery_receipts": deliveries,
                }
            )

        await self._repo.update_with_topics(reminders_path, updater)
        assert published is not None
        return published

    async def record_cost(self, record: BriefCostRecord, *, actual_usage: bool = False) -> BriefCostRecord:
        def updater(data: DailyBriefStore) -> DailyBriefStore:
            cycle = next((item for item in data.cycles if item.id == record.cycle_id), None)
            if cycle is None:
                raise BriefNotFound("cycle not found")
            grant = _find_grant(data, cycle.grant_id)
            if grant is None:
                raise BriefNotFound("grant not found")
            if not actual_usage and any(item.id == record.id for item in data.costs):
                return data
            totals = [item for item in data.costs if item.cycle_id == cycle.id and item.id != record.id]
            search_count = sum(item.search_count for item in totals) + record.search_count
            reads = sum(item.source_read_count for item in totals) + record.source_read_count
            tokens = sum(item.model_tokens for item in totals) + record.model_tokens
            cost = sum(item.cost_usd for item in totals) + record.cost_usd
            cost_cny = sum(item.cost_cny for item in totals) + record.cost_cny
            if not actual_usage and (
                search_count > grant.budget.max_searches
                or reads > grant.budget.max_source_reads
                or tokens > grant.budget.max_model_tokens
                or cost > grant.budget.max_cost_usd
                or cost_cny > grant.budget.max_cost_cny
            ):
                raise ScopeFenceFailed("brief budget exceeded")
            return data.model_copy(update={"costs": [record.model_copy(deep=True),
                *(item for item in data.costs if item.id != record.id)]})

        await self._repo.update(updater)
        return record

    async def list_pending_native_deliveries(self) -> list[BriefDeliveryReceipt]:
        data = await self._repo.load()
        return [
            receipt
            for receipt in data.delivery_receipts
            if receipt.channel == "native" and receipt.state == "pending"
        ]

    async def suppress_delivery(
        self,
        receipt_id: str,
        reason: str,
    ) -> BriefDeliveryReceipt:
        return await self._transition_delivery(
            receipt_id,
            expected_states={"pending"},
            state="suppressed",
            error=reason,
        )

    async def claim_native_delivery(
        self,
        receipt_id: str,
    ) -> BriefDeliveryReceipt:
        """Fence authority, then enter the unknown-effect window before I/O."""

        claimed: BriefDeliveryReceipt | None = None
        reminders_path = self._reminders.path
        now = utc_now()

        def updater(data: DailyBriefStore, reminders: RemindersFile) -> DailyBriefStore:
            nonlocal claimed
            receipt = next(
                (entry for entry in data.delivery_receipts if entry.id == receipt_id),
                None,
            )
            if receipt is None:
                raise BriefNotFound("delivery receipt not found")
            if receipt.channel != "native" or receipt.state != "pending":
                raise BriefConflict("delivery is not pending")
            edition = next(
                (
                    entry
                    for entry in data.editions
                    if entry.id == receipt.edition_id
                    and entry.revision == receipt.edition_revision
                    and entry.status == "published"
                ),
                None,
            )
            subscription = _find_subscription(data, receipt.subscription_id)
            grant = _find_grant(data, edition.grant_id) if edition else None
            topics = _topics_by_id(reminders)
            eligible = (
                data.feature_flags.native_notification
                and edition is not None
                and subscription is not None
                and edition.subscription_id == subscription.id
                and subscription.status == "active"
                and subscription.native_notifications
                and grant is not None
                and grant.is_effective(now)
                and subscription.grant_id == grant.id
                and subscription.grant_revision == edition.grant_revision
                and grant.revision == edition.grant_revision
                and _topic_scope_is_current(topics, edition.topic_revisions)
            )
            if not eligible:
                claimed = receipt.model_copy(
                    update={
                        "state": "suppressed",
                        "error": "delivery_scope_no_longer_current",
                    }
                )
            else:
                # Once an adapter call can begin, a crash is an unknown-effect
                # window.  Unknown deliveries are never automatically retried.
                claimed = receipt.model_copy(
                    update={
                        "state": "unknown",
                        "attempt_count": receipt.attempt_count + 1,
                        "last_attempt_at": now,
                        "error": "adapter_outcome_not_yet_known",
                    }
                )
            return data.model_copy(
                update={
                    "delivery_receipts": _replace_by_id(
                        data.delivery_receipts, claimed
                    )
                }
            )

        await self._repo.update_with_topics(reminders_path, updater)
        assert claimed is not None
        return claimed

    async def resolve_delivery(
        self,
        receipt_id: str,
        *,
        state: str,
        adapter_message_id: str | None = None,
        error: str = "",
    ) -> BriefDeliveryReceipt:
        if state not in {"submitted", "acknowledged", "failed"}:
            raise ValueError("adapter may only resolve to a known delivery outcome")
        return await self._transition_delivery(
            receipt_id,
            expected_states={"unknown"},
            state=state,
            adapter_message_id=adapter_message_id,
            error=error,
        )

    async def mark_delivery_acknowledged(
        self,
        receipt_id: str,
    ) -> BriefDeliveryReceipt:
        return await self._transition_delivery(
            receipt_id,
            expected_states={"submitted", "unknown"},
            state="acknowledged",
            acknowledged_at=utc_now(),
            error="",
        )

    async def mark_delivery_read(self, receipt_id: str) -> BriefDeliveryReceipt:
        now = utc_now()
        read: BriefDeliveryReceipt | None = None

        def updater(data: DailyBriefStore, reminders: RemindersFile) -> DailyBriefStore:
            nonlocal read
            receipt = next((item for item in data.delivery_receipts if item.id == receipt_id), None)
            if receipt is None:
                raise BriefNotFound("delivery receipt not found")
            edition = next((item for item in data.editions if item.id == receipt.edition_id
                and item.revision == receipt.edition_revision
                and item.status in {"published", "superseded"}), None)
            grant = _find_grant(data, edition.grant_id) if edition else None
            if edition is None or grant is None or not _grant_allows_read(grant, edition) or not _topic_scope_is_current(
                _topics_by_id(reminders), edition.topic_revisions):
                raise ScopeFenceFailed("edition authority is no longer current")
            if receipt.state not in {"pending", "submitted", "unknown", "acknowledged", "read"}:
                raise BriefConflict("delivery is not readable")
            read = receipt if receipt.state == "read" else receipt.model_copy(update={
                "state": "read", "acknowledged_at": receipt.acknowledged_at or now,
                "read_at": now, "error": ""})
            return data.model_copy(update={
                "delivery_receipts": _replace_by_id(data.delivery_receipts, read),
                "read_revisions": {**data.read_revisions, edition.id:
                    max(data.read_revisions.get(edition.id, 0), edition.revision)}})
        await self._repo.update_with_topics(self._reminders.path, updater)
        assert read is not None
        return read

    async def _transition_delivery(
        self,
        receipt_id: str,
        *,
        expected_states: set[str],
        state: str,
        adapter_message_id: str | None = None,
        acknowledged_at: datetime | None = None,
        read_at: datetime | None = None,
        error: str = "",
    ) -> BriefDeliveryReceipt:
        transitioned: BriefDeliveryReceipt | None = None

        def updater(data: DailyBriefStore) -> DailyBriefStore:
            nonlocal transitioned
            receipt = next(
                (entry for entry in data.delivery_receipts if entry.id == receipt_id),
                None,
            )
            if receipt is None:
                raise BriefNotFound("delivery receipt not found")
            if receipt.state == state:
                transitioned = receipt
                return data
            if receipt.state not in expected_states:
                raise BriefConflict(
                    f"delivery state is {receipt.state}, expected one of "
                    f"{sorted(expected_states)}"
                )
            updates: dict[str, object] = {"state": state, "error": error[:500]}
            if adapter_message_id is not None:
                updates["adapter_message_id"] = adapter_message_id
            if acknowledged_at is not None:
                updates["acknowledged_at"] = acknowledged_at
            if read_at is not None:
                updates["read_at"] = read_at
            transitioned = receipt.model_copy(update=updates)
            return data.model_copy(
                update={
                    "delivery_receipts": _replace_by_id(
                        data.delivery_receipts, transitioned
                    )
                }
            )

        await self._repo.update(updater)
        assert transitioned is not None
        return transitioned

    async def add_feedback(
        self,
        edition_id: str,
        item_id: str | None,
        request: BriefFeedbackCreateRequest,
    ) -> BriefFeedback:
        created: BriefFeedback | None = None

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal created
            prior_id = data.action_results.get(request.action_id)
            if prior_id:
                created = next(
                    (entry for entry in data.feedback if entry.id == prior_id), None
                )
                if created is None:
                    raise BriefConflict("idempotency record is inconsistent")
                return data
            edition = next(
                (
                    entry
                    for entry in data.editions
                    if entry.id == edition_id and entry.status == "published"
                ),
                None,
            )
            if edition is None:
                raise BriefNotFound("edition not found")
            grant = _find_grant(data, edition.grant_id)
            if (
                grant is None
                or not _grant_allows_read(grant, edition)
                or not _topic_scope_is_current(
                    _topics_by_id(reminders), edition.topic_revisions
                )
            ):
                raise ScopeFenceFailed("edition authority is no longer current")
            if item_id and all(item.id != item_id for item in edition.items):
                raise BriefNotFound("brief item not found")
            created = BriefFeedback(
                edition_id=edition_id,
                item_id=item_id,
                kind=request.kind,
                note=request.note,
            )
            saved = data.saved_items
            if request.kind == "save" and item_id and not any(
                item.edition_id == edition.id and item.edition_revision == edition.revision
                and item.item_id == item_id for item in saved):
                saved = [BriefSavedItem(edition_id=edition.id, edition_revision=edition.revision,
                    item_id=item_id), *saved]
            return data.model_copy(
                update={
                    "feedback": [created, *data.feedback],
                    "saved_items": saved,
                    "processed_action_ids": [request.action_id, *data.processed_action_ids],
                    "action_results": {
                        **data.action_results,
                        request.action_id: created.id,
                    },
                }
            )

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert created is not None
        return created

    async def create_discussion_draft(
        self,
        edition_id: str,
        request: BriefDiscussionDraftCreateRequest,
    ) -> BriefDiscussionDraft:
        """Create context only. Sending remains a separate one-Command action."""

        created: BriefDiscussionDraft | None = None

        def updater(
            data: DailyBriefStore,
            reminders: RemindersFile,
        ) -> DailyBriefStore:
            nonlocal created
            prior_id = data.action_results.get(request.action_id)
            if prior_id:
                created = next(
                    (entry for entry in data.discussion_drafts if entry.id == prior_id),
                    None,
                )
                if created is None:
                    raise BriefConflict("idempotency record is inconsistent")
                return data
            edition = next(
                (
                    item
                    for item in data.editions
                    if item.id == edition_id
                    and item.revision == request.edition_revision
                    and item.status == "published"
                ),
                None,
            )
            if edition is None:
                raise BriefConflict("edition revision changed or is unavailable")
            grant = _find_grant(data, edition.grant_id)
            if (
                grant is None
                or not _grant_allows_read(grant, edition)
                or not _topic_scope_is_current(
                    _topics_by_id(reminders), edition.topic_revisions
                )
            ):
                raise ScopeFenceFailed("edition authority is no longer current")
            selected_item = next(
                (item for item in edition.items if item.id == request.item_id), None
            )
            if request.item_id and selected_item is None:
                raise BriefNotFound("brief item not found")
            evidence_ids = selected_item.evidence_ids if selected_item else []
            dependencies = list(
                dict.fromkeys(
                    [
                        *edition.private_dependency_refs,
                        *(selected_item.private_dependency_refs if selected_item else []),
                    ]
                )
            )
            created = BriefDiscussionDraft(
                context_ref=BriefContextRef(
                    edition_id=edition.id,
                    edition_revision=edition.revision,
                    item_id=request.item_id,
                    evidence_ids=evidence_ids,
                ),
                text=request.text,
                private_dependency_refs=dependencies,
            )
            return data.model_copy(
                update={
                    "discussion_drafts": [created, *data.discussion_drafts],
                    "processed_action_ids": [request.action_id, *data.processed_action_ids],
                    "action_results": {
                        **data.action_results,
                        request.action_id: created.id,
                    },
                }
            )

        await self._repo.update_with_topics(self._reminders.path, updater)
        assert created is not None
        return created

    async def get_discussion_draft(self, draft_id: str) -> BriefDiscussionDraftView:
        data, reminders = await self._load_authorities()
        draft = next((item for item in data.discussion_drafts if item.id == draft_id), None)
        if draft is None:
            raise BriefNotFound("discussion draft not found")
        ref = draft.context_ref
        edition = next((item for item in data.editions if item.id == ref.edition_id
            and item.revision == ref.edition_revision
            and item.status in {"published", "superseded"}), None)
        grant = _find_grant(data, edition.grant_id) if edition else None
        if draft.status != "draft" or edition is None or grant is None or not _grant_allows_read(grant, edition) or not _topic_scope_is_current(
            _topics_by_id(reminders), edition.topic_revisions):
            raise ScopeFenceFailed("discussion authority is no longer current")
        items = [item for item in edition.items if ref.item_id is None or item.id == ref.item_id]
        if ref.item_id and not items:
            raise BriefNotFound("brief item not found")
        lines = [draft.text, "", f"简报上下文：{edition.title}（{edition.local_date}，第{edition.revision}版）"]
        for item in items:
            lines.extend([item.title, item.fact, item.relevance, item.analysis, item.limitations])
        evidence_ids = {evidence_id for item in items for evidence_id in item.evidence_ids}
        lines.extend(f"来源：{source.canonical_url}" for source in edition.evidence
            if source.id in evidence_ids and not source.retracted)
        return BriefDiscussionDraftView(**draft.model_dump(), composer_text="\n".join(line for line in lines if line))

    async def invalidate_dependency(self, dependency_ref: str) -> None:
        """Erase pending/derived private material when its authority disappears."""

        now = utc_now()

        def updater(data: DailyBriefStore) -> DailyBriefStore:
            grants = [
                grant.model_copy(
                    update={
                        "revision": grant.revision + 1,
                        "allowed_context_refs": [
                            ref
                            for ref in grant.allowed_context_refs
                            if ref != dependency_ref
                        ],
                    }
                )
                if dependency_ref in grant.allowed_context_refs
                else grant
                for grant in data.grants
            ]
            affected_editions: set[str] = set()
            editions: list[BriefEdition] = []
            for edition in data.editions:
                depends_on_ref = (
                    dependency_ref in edition.private_dependency_refs
                    or any(
                        dependency_ref in item.private_dependency_refs
                        for item in edition.items
                    )
                )
                if depends_on_ref:
                    affected_editions.add(edition.id)
                    editions.append(_tombstone_edition(edition))
                else:
                    editions.append(edition)
            drafts = [
                draft.model_copy(
                    update={
                        "status": "invalidated",
                        "text": "",
                        "invalidated_at": now,
                    }
                )
                if dependency_ref in draft.private_dependency_refs
                or draft.context_ref.edition_id in affected_editions
                else draft
                for draft in data.discussion_drafts
            ]
            cycles = [
                cycle.model_copy(
                    update={
                        "status": "invalidated",
                        "finished_at": now,
                        "failure_reason": "dependency_invalidated",
                    }
                )
                if cycle.status in {"queued", "running"}
                and any(
                    grant.id == cycle.grant_id
                    and dependency_ref in grant.allowed_context_refs
                    for grant in data.grants
                )
                else cycle
                for cycle in data.cycles
            ]
            deliveries = [
                receipt.model_copy(
                    update={"state": "suppressed", "error": "dependency_invalidated"}
                )
                if receipt.edition_id in affected_editions
                and receipt.state in {"pending", "failed"}
                else receipt
                for receipt in data.delivery_receipts
            ]
            return data.model_copy(
                update={
                    "grants": grants,
                    "editions": editions,
                    "discussion_drafts": drafts,
                    "cycles": cycles,
                    "delivery_receipts": deliveries,
                }
            )

        await self._repo.update(updater)

    async def invalidate_topic_scope(
        self,
        topic_id: str,
        *,
        current_revision: int | None,
    ) -> None:
        """Invalidate all brief derivatives created from an older topic authority."""

        now = utc_now()

        def stale(revisions: dict[str, int]) -> bool:
            return topic_id in revisions and (
                current_revision is None
                or revisions[topic_id] != current_revision
            )

        def updater(data: DailyBriefStore) -> DailyBriefStore:
            affected_edition_ids = {
                edition.id
                for edition in data.editions
                if stale(edition.topic_revisions)
            }
            editions = [
                _tombstone_edition(edition)
                if edition.id in affected_edition_ids
                else edition
                for edition in data.editions
            ]
            cycles = [
                cycle.model_copy(
                    update={
                        "status": "invalidated",
                        "finished_at": now,
                        "failure_reason": "topic_scope_changed",
                    }
                )
                if stale(cycle.topic_revisions)
                and cycle.status in {"queued", "running"}
                else cycle
                for cycle in data.cycles
            ]
            drafts = [
                draft.model_copy(
                    update={
                        "status": "invalidated",
                        "text": "",
                        "invalidated_at": now,
                    }
                )
                if draft.context_ref.edition_id in affected_edition_ids
                and draft.status == "draft"
                else draft
                for draft in data.discussion_drafts
            ]
            deliveries = [
                receipt.model_copy(
                    update={"state": "suppressed", "error": "topic_scope_changed"}
                )
                if receipt.edition_id in affected_edition_ids
                and receipt.state in {"pending", "failed"}
                else receipt
                for receipt in data.delivery_receipts
            ]
            return data.model_copy(
                update={
                    "editions": editions,
                    "cycles": cycles,
                    "discussion_drafts": drafts,
                    "delivery_receipts": deliveries,
                }
            )

        await self._repo.update(updater)


# Backward import name retained for the B1 API and tests.
DailyBriefHomeService = DailyBriefService
