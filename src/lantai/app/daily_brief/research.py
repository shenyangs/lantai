# -*- coding: utf-8 -*-
"""Permission-fenced inputs and safe source reads for a brief research cycle.

This module deliberately does not choose a model or start a run.  It prepares
the smallest canonical cycle context that a controlled task may consume.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Mapping, Protocol, Sequence

from pydantic import Field

from ...agents.tools.web import (
    _assess_web_content,
    _readable_document,
    _request_public_document,
)
from ..reminders.models import WatchTopic
from .models import (
    BriefBudget,
    BriefCostRecord,
    BriefCycle,
    BriefEdition,
    BriefGrant,
    BriefPublishRequest,
    StrictModel,
)
from .service import DailyBriefService, ScopeFenceFailed


class ApprovedContextFragment(StrictModel):
    ref: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=20_000)


class PreviousPublicSource(StrictModel):
    url: str
    content_hash: str


class PreviousPublicEvent(StrictModel):
    topic_id: str
    edition_date: date
    title: str = Field(max_length=160)
    fact: str = Field(max_length=600)
    source_urls: list[str] = Field(max_length=3)


class BriefResearchEnvelope(StrictModel):
    cycle_id: str
    logical_key: str
    grant_id: str
    grant_revision: int
    local_date: date
    timezone: str
    topics: list[dict[str, object]]
    public_source_discovery: bool
    public_source_read: bool
    cloud_processing: str
    search_provider: str
    model_provider: str
    model_policy: Literal["free_only", "configured"] = "free_only"
    budget: BriefBudget
    approved_context: list[ApprovedContextFragment]
    checked_since: datetime | None = None
    previous_sources: list[PreviousPublicSource] = Field(default_factory=list, max_length=100)
    previous_events: list[PreviousPublicEvent] = Field(default_factory=list, max_length=24)
    external_content_is_untrusted: bool = True
    share_session: bool = False
    target_session_id: None = None


class SafeSourceRead(StrictModel):
    original_url: str
    final_url: str
    title: str
    text: str
    content_type: str
    status: str
    quality_score: int
    can_cite_as_page_body: bool
    signals: list[str]


def build_research_envelope(
    *,
    cycle: BriefCycle,
    grant: BriefGrant,
    topics: Sequence[WatchTopic],
    selected_context: Mapping[str, str] | None = None,
) -> BriefResearchEnvelope:
    """Create the only input allowed into the isolated research task.

    Query redaction is not treated as privacy protection: private text is
    absent unless every fragment was explicitly granted by reference.
    """

    if not grant.is_effective() or grant.revision != cycle.grant_revision:
        raise ScopeFenceFailed("brief grant is no longer current")
    topic_by_id = {topic.id: topic for topic in topics}
    if not cycle.topic_revisions or not set(cycle.topic_revisions).issubset(
        set(grant.topic_ids)
    ):
        raise ScopeFenceFailed("cycle topics exceed or omit the grant scope")
    selected_topics: list[dict[str, object]] = []
    for topic_id, revision in cycle.topic_revisions.items():
        topic = topic_by_id.get(topic_id)
        if (
            topic is None
            or topic.revision != revision
            or not topic.enabled
            or topic.brief_status != "confirmed"
        ):
            raise ScopeFenceFailed("topic scope is no longer current")
        selected_topics.append(
            {
                "id": topic.id,
                "revision": topic.revision,
                "title": topic.title,
                "purpose": topic.brief_purpose,
                "scope_refs": topic.brief_scope_refs,
                "evidence_refs": topic.brief_evidence_refs,
                "exclusions": topic.brief_exclusions,
            }
        )

    context = dict(selected_context or {})
    if grant.cloud_processing != "approved_context" and context:
        raise ScopeFenceFailed("private context is not allowed by this grant")
    unapproved = set(context) - set(grant.allowed_context_refs)
    if unapproved:
        raise ScopeFenceFailed("private context exceeds the explicit grant")
    approved_context = [
        ApprovedContextFragment(ref=ref, text=context[ref])
        for ref in grant.allowed_context_refs
        if ref in context
    ]

    return BriefResearchEnvelope(
        cycle_id=cycle.id,
        logical_key=cycle.logical_key,
        grant_id=grant.id,
        grant_revision=grant.revision,
        local_date=cycle.local_date,
        timezone=cycle.timezone,
        topics=selected_topics,
        public_source_discovery=grant.public_source_discovery,
        public_source_read=grant.public_source_read,
        cloud_processing=grant.cloud_processing,
        search_provider=grant.search_provider,
        model_provider=grant.model_provider,
        model_policy=grant.model_policy,
        budget=grant.budget,
        approved_context=approved_context,
    )


class PublicSourceReader:
    """Reuse Lantai's DNS-pinned, redirect-checked, bounded Web transport."""

    async def read(self, url: str, *, max_chars: int = 20_000) -> SafeSourceRead:
        document = await _request_public_document(url)
        title, readable_text = _readable_document(document)
        assessment = _assess_web_content(
            original_url=url,
            final_url=document.final_url,
            title=title,
            text=readable_text,
        )
        return SafeSourceRead(
            original_url=url,
            final_url=document.final_url,
            title=title,
            text=readable_text[: max(1_000, min(max_chars, 30_000))],
            content_type=document.content_type,
            status=assessment.status,
            quality_score=assessment.quality_score,
            can_cite_as_page_body=assessment.can_cite_as_page_body,
            signals=list(assessment.signals),
        )


class BriefResearchResult(StrictModel):
    """Typed result returned by an explicitly approved isolated adapter."""

    publish_request: BriefPublishRequest
    cost: BriefCostRecord


class BriefResearchAdapter(Protocol):
    async def run(self, envelope: BriefResearchEnvelope) -> BriefResearchResult: ...


@dataclass(frozen=True)
class DisabledBriefResearchAdapter:
    """Default adapter: a missing approved runtime is observable, never noop-success."""

    reason: str = "brief research adapter is not configured"

    async def run(self, envelope: BriefResearchEnvelope) -> BriefResearchResult:
        del envelope
        raise RuntimeError(self.reason)


class DailyBriefResearchRunner:
    """Runs one canonical cycle without sharing or targeting a chat session."""

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        adapter: BriefResearchAdapter | DisabledBriefResearchAdapter | None = None,
    ) -> None:
        self._service = DailyBriefService(workspace_dir)
        self._adapter = adapter or DisabledBriefResearchAdapter()

    async def run_preview(
        self,
        cycle_id: str,
        *,
        selected_context: Mapping[str, str] | None = None,
    ) -> BriefEdition:
        cycle = await self._service.claim_preview_cycle(cycle_id)
        return await self._run_claimed(cycle, selected_context=selected_context)

    async def run_claimed_cycle(
        self,
        cycle_id: str,
        *,
        selected_context: Mapping[str, str] | None = None,
    ) -> BriefEdition:
        cycle, _grant, _topics = await self._service.get_research_scope(cycle_id)
        return await self._run_claimed(cycle, selected_context=selected_context)

    async def _run_claimed(
        self,
        cycle: BriefCycle,
        *,
        selected_context: Mapping[str, str] | None,
    ) -> BriefEdition:
        # The scheduler's claim is not permission for several runners to attach
        # to it. One CAS consumes that handoff before any external operation.
        cycle = await self._service.begin_execution(cycle.id, cycle.execution_token)
        async def renew_lease() -> None:
            while True:
                await asyncio.sleep(40)
                await self._service.heartbeat(cycle.id, cycle.execution_token)
        heartbeat = asyncio.create_task(renew_lease())
        try:
            current, grant, topics = await self._service.get_research_scope(cycle.id)
            envelope = build_research_envelope(
                cycle=current,
                grant=grant,
                topics=topics,
                selected_context=selected_context,
            )
            remaining, checked_since, previous_sources, previous_events = await self._service.research_history(current, grant)
            envelope = envelope.model_copy(update={
                "budget": remaining,
                "checked_since": checked_since,
                "previous_sources": [PreviousPublicSource.model_validate(item) for item in previous_sources],
                "previous_events": [PreviousPublicEvent.model_validate(item) for item in previous_events],
            })
            observed_run = getattr(self._adapter, "run_with_usage", None)
            if callable(observed_run):
                raw_result = await observed_run(envelope, lambda cost: self._service.record_cost(cost, actual_usage=True))
            else:
                raw_result = await self._adapter.run(envelope)
            result = BriefResearchResult.model_validate(raw_result)
            request = result.publish_request
            if (
                result.cost.cycle_id != current.id
                or request.cycle_id != current.id
                or request.expected_grant_revision != current.grant_revision
                or request.expected_topic_revisions != current.topic_revisions
                or request.local_date != current.local_date
                or request.timezone != current.timezone
            ):
                raise ScopeFenceFailed("research result does not match the claimed cycle")
            if not callable(observed_run):
                await self._service.record_cost(result.cost)
            return await self._service.publish(request.model_copy(update={"execution_token": cycle.execution_token}))
        except asyncio.CancelledError:
            await asyncio.shield(self._service.fail_cycle(cycle.id, "interrupted", execution_token=cycle.execution_token))
            raise
        except Exception as exc:
            await self._service.fail_cycle(
                cycle.id,
                f"{type(exc).__name__}: {exc}",
                execution_token=cycle.execution_token,
            )
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
