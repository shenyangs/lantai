# -*- coding: utf-8 -*-
"""Isolated, best-effort intelligence for non-conversational UI tasks."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import load_config
from .utility_model import (
    ResolvedUtilityModel as _ResolvedModel,
    consume_model_response as _consume_model_response,
    resolve_utility_model as _resolve_model,
    resolve_active_model as _resolve_active_model,
)
from .background_semantics import run_primary_when_idle

logger = logging.getLogger(__name__)

ArtifactKind = Literal["creative", "correspondence", "copy", "script"]
_ALLOWED_KINDS = {"creative", "correspondence", "copy", "script"}

ARTIFACT_CLASSIFIER_PROMPT = """\
You are an invisible JSON presentation analyst, not an assistant. Inspect one
completed response and do both tasks below.

1. Find only finished, standalone deliverables:
- creative: poem, lyrics, story, fiction, literary prose, couplet
- correspondence: email, letter, invitation, notice, announcement, statement
- copy: marketing, social, product, brand, advertising, press copy
- script: speech, host/video/audio script, dialogue, screenplay

Ignore conversation, explanations, analysis, advice, lists, questions, code,
tables, reports, documents, quoted input, introductions, notes and follow-ups.
2. Optionally add one short advisory only when the response is a substantive
research report, evidence-backed comparison, market study, or technical
investigation. The advisory must identify the single most useful, specific
boundary for interpreting or using this exact report: source coverage,
freshness, scope, method, uncertainty, or a decision risk. It must be grounded
in details already visible in the response. Do not add new facts. Do not use a
generic disclaimer such as "for reference only", mention AI, repeat the
conclusion, offer more work, or invent a warning just to fill the field. Use
label "注意" for a material boundary/risk and "提示" for a useful
interpretation/application note. Return null for ordinary chat, advice,
creative work, general explanations, and reports with no worthwhile note.

Return only:
{"artifacts":[{"kind":"creative","title":"work title","start_marker":"short exact opening fragment","end_marker":"short exact closing fragment"}],"advisory":{"label":"注意","body":"one specific plain-text note"}}

Markers must be short, verbatim, unique fragments from the first and last
content lines. Exclude separators and title headings. Never paraphrase markers
or repeat the whole work. Example: for "然后 quietly 把活干完的搭档。",
use end_marker "把活干完的搭档。". There is no work-length limit. Return
{"artifacts":[],"advisory":null} when nothing qualifies.
"""


class ArtifactClassificationRequest(BaseModel):
    text: str = Field(min_length=1)


class ClassifiedArtifact(BaseModel):
    kind: ArtifactKind
    title: str
    source_text: str
    start: int
    end: int


class ResearchAdvisory(BaseModel):
    label: Literal["注意", "提示"]
    body: str


class ArtifactClassificationResponse(BaseModel):
    artifacts: list[ClassifiedArtifact] = Field(default_factory=list)
    advisory: ResearchAdvisory | None = None
    status: Literal["classified", "disabled", "unavailable", "failed"]
    model: str = ""


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None

    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _validate_artifacts(
    source: str,
    payload: dict[str, Any] | None,
) -> list[ClassifiedArtifact]:
    raw_artifacts = payload.get("artifacts") if payload else None
    if not isinstance(raw_artifacts, list):
        return []

    accepted: list[ClassifiedArtifact] = []
    occupied: list[tuple[int, int]] = []
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        title = str(raw.get("title") or "").strip()
        source_text = str(raw.get("source_text") or "")
        if kind not in _ALLOWED_KINDS or not title:
            continue

        if source_text.strip():
            if (
                source.count(source_text) != 1
                or "```" in source_text
                or "~~~" in source_text
            ):
                continue
            start = source.index(source_text)
            end = start + len(source_text)
        else:
            start_marker = str(raw.get("start_marker") or "")
            end_marker = str(raw.get("end_marker") or "")
            if (
                not start_marker.strip()
                or not end_marker.strip()
                or "```" in start_marker
                or "~~~" in start_marker
                or "```" in end_marker
                or "~~~" in end_marker
            ):
                continue
            resolved_start = _resolve_unique_marker(
                source,
                start_marker,
                edge="start",
            )
            resolved_end = _resolve_unique_marker(
                source,
                end_marker,
                edge="end",
            )
            if resolved_start is None or resolved_end is None:
                continue
            start, _ = resolved_start
            end_marker_start, resolved_end_marker = resolved_end
            if end_marker_start < start:
                continue
            end = end_marker_start + len(resolved_end_marker)
            source_text = source[start:end]

        overlaps_existing = any(
            start < used_end and end > used_start
            for used_start, used_end in occupied
        )
        if overlaps_existing:
            continue

        occupied.append((start, end))
        accepted.append(
            ClassifiedArtifact(
                kind=kind,
                title=title,
                source_text=source_text,
                start=start,
                end=end,
            ),
        )

    return sorted(accepted, key=lambda item: item.start)


def _validate_research_advisory(
    payload: dict[str, Any] | None,
) -> ResearchAdvisory | None:
    raw = payload.get("advisory") if payload else None
    if not isinstance(raw, dict):
        return None

    label = str(raw.get("label") or "").strip()
    body = " ".join(str(raw.get("body") or "").split())
    if label not in {"注意", "提示"}:
        return None
    if len(body) < 12 or len(body) > 360:
        return None
    if any(marker in body for marker in ("```", "~~~", "<script", "</")):
        return None

    generic = body.rstrip("。！! ").lower()
    if generic in {
        "以上内容仅供参考",
        "以上信息仅供参考",
        "本报告仅供参考",
        "请根据实际情况自行判断",
        "请自行核实相关信息",
    }:
        return None
    return ResearchAdvisory(label=label, body=body)


def _resolve_unique_marker(
    source: str,
    marker: str,
    *,
    edge: Literal["start", "end"],
) -> tuple[int, str] | None:
    """Resolve an exact marker or a still-exact unique edge fragment."""

    if source.count(marker) == 1:
        return source.index(marker), marker

    minimum_fallback_length = 4
    for length in range(len(marker) - 1, minimum_fallback_length - 1, -1):
        candidate = (
            marker[:length]
            if edge == "start"
            else marker[len(marker) - length :]
        )
        if not candidate.strip() or source.count(candidate) != 1:
            continue
        return source.index(candidate), candidate
    return None


async def classify_text_artifacts(
    text: str,
    *,
    agent_id: str,
) -> ArtifactClassificationResponse:
    """Classify completed text without changing or blocking the chat runtime."""

    config = load_config().utility_intelligence
    if not bool(getattr(config, "background_processing_enabled", True)):
        return ArtifactClassificationResponse(status="disabled")

    uses_primary = config.mode == "follow_active"
    try:
        resolved = _resolve_model(config, agent_id)
    except Exception as exc:
        logger.info("Utility model unavailable: %s", exc)
        return ArtifactClassificationResponse(status="unavailable")
    if resolved is None:
        try:
            resolved = _resolve_active_model(agent_id)
        except Exception as exc:
            logger.info("Active classification fallback unavailable: %s", exc)
            resolved = None
        uses_primary = resolved is not None
    if resolved is None:
        return ArtifactClassificationResponse(status="unavailable")

    messages = [
        {"role": "system", "content": ARTIFACT_CLASSIFIER_PROMPT},
        {"role": "user", "content": text},
    ]
    async def consume(candidate: _ResolvedModel, primary: bool) -> str:
        async def invoke() -> str:
            return await asyncio.wait_for(
                _consume_model_response(candidate.model, messages),
                timeout=config.timeout_seconds,
            )

        return (
            await run_primary_when_idle(agent_id, invoke)
            if primary
            else await invoke()
        )

    model_label = resolved.label
    try:
        raw = await consume(resolved, uses_primary)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if uses_primary:
            logger.info("Utility artifact classification failed: %s", exc)
            return ArtifactClassificationResponse(
                status="failed",
                model=model_label,
            )
        logger.info(
            "Artifact accelerator failed; deferring to active model: %s",
            exc,
        )
        try:
            fallback = _resolve_active_model(agent_id)
            if fallback is None:
                raise RuntimeError("active classification model unavailable")
            raw = await consume(fallback, True)
            model_label = fallback.label
        except asyncio.CancelledError:
            raise
        except Exception as fallback_exc:
            logger.info(
                "Active artifact classification fallback failed: %s",
                fallback_exc,
            )
            return ArtifactClassificationResponse(
                status="failed",
                model=model_label,
            )

    try:
        payload = _extract_json_object(raw)
        artifacts = _validate_artifacts(text, payload)
        advisory = _validate_research_advisory(payload)
        return ArtifactClassificationResponse(
            artifacts=artifacts,
            advisory=advisory,
            status="classified",
            model=model_label,
        )
    except Exception as exc:
        logger.info("Utility artifact classification failed: %s", exc)
        return ArtifactClassificationResponse(
            status="failed",
            model=model_label,
        )
