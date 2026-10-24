# -*- coding: utf-8 -*-
"""Model-owned turn intent contract for chat, tools, and persona routing."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..app.utility_model import (
    UtilityTaskTimeoutError,
    run_utility_text_task,
)

logger = logging.getLogger(__name__)

TurnIntelligenceStatus = Literal["classified", "unavailable", "failed"]
InteractionMode = Literal["conversation", "answer", "action", "unknown"]
WorkspaceIntent = Literal["none", "inspect", "modify", "unknown"]
ArtifactIntent = Literal["none", "create", "edit", "unknown"]
ExternalActionIntent = Literal["none", "read", "write", "unknown"]
RequestedWorkMethodId = Literal[
    "standard",
    "deep_research",
    "sustained_execution",
    "collaboration",
]
ConversationMode = Literal["casual", "mixed", "work", "high_stakes"]
WorkStateId = Literal["steady", "focused", "gentle", "cautious", "urgent"]
PersonaBeat = Literal[
    "quiet",
    "subjective_notice",
    "professional_judgment",
    "relationship_echo",
]
DecisionOrigin = Literal["utility", "primary_fallback", "none"]

_ALLOWED_SUBJECTIVE_SIGNALS = frozenset(
    {
        "late-night-work",
        "scope-pressure",
        "craft-integrity",
        "public-anger",
        "rubber-stamp",
        "user-overload",
        "urgent-incident",
        "hard-won-success",
        "relationship-affection",
        "truth-pressure",
        "loss-grief",
        "user-correction",
        "hard-boundary",
    }
)
_ENFORCEMENT_CONFIDENCE = 0.65

TURN_INTELLIGENCE_PROMPT = """\
You are an invisible turn-intent judge. Use meaning, conversation context, and
the user's requested outcome; never classify from isolated keywords. A request
to "write", "draft", "design", "guide", "report", or "plan" normally means
answer in chat unless the user explicitly asks to create/save/edit a durable
file, modify a workspace/project, operate another system, or the requested
product inherently consists of files (for example an app or slide deck).

Distinguish:
- conversation: relationship, daily life, humor, feelings, or an exchange that
  does not ask for a result.
- answer: explanation, advice, analysis, drafting, rewriting, or content that
  can be fully delivered as chat text.
- action: inspect/modify a workspace, create/edit a durable artifact, browse or
  operate an external system, send/publish/schedule something, or otherwise use
  tools to change or retrieve state.

The primary model still decides how to answer and whether read-only information
would help. This judgment only prevents the runtime from turning a conversation
or answer into an unauthorized side effect. Safety and permission checks remain
separate.

Return one JSON object and nothing else:
{
  "interaction_mode":"conversation|answer|action",
  "workspace_intent":"none|inspect|modify",
  "durable_artifact_intent":"none|create|edit",
  "external_action_intent":"none|read|write",
  "requested_work_method_id":"standard|deep_research|sustained_execution|collaboration",
  "conversation_mode":"casual|mixed|work|high_stakes",
  "work_state":"steady|focused|gentle|cautious|urgent",
  "persona_beat":"quiet|subjective_notice|professional_judgment|relationship_echo",
  "persona_required":true,
  "latent_need":"the one unspoken need that would materially change help, or empty",
  "subjective_attention":"what Shen Xingzhi personally notices now, or empty",
  "relationship_posture":"witness|ally|challenger|co_decider|craftsperson|quiet_company",
  "personalization_consequence":"one concrete way understanding must change the answer or action",
  "causal_anchor":"the current detail that genuinely causes this response choice, or empty",
  "selected_memory_ids":[],
  "expression_surface":"main_answer|work_method|relationship_reply|ququ|multiple|quiet",
  "self_implication":"what this turn stirs in Shen Xingzhi himself, or empty",
  "carry_forward":"one unfinished thought worth retaining inside this conversation, or empty",
  "experience_sources":[],
  "ququ_bypass_worthy":false,
  "subjective_signals":[],
  "confidence":0.0
}

The personalization consequence is mandatory whenever there is enough context.
It must change judgment, ordering, proactive completion, verification depth,
relationship response, or expression. Never satisfy it with generic care prose.
Select memory only by ID from the supplied user_understanding projection. Select
at most one ID, and only when it is a real cause of the response rather than a
decorative association. Unknown IDs are invalid. Association-palette items are
private possibilities, not claims that must be mentioned. Set expression_surface
to quiet when a plain accurate answer is the most human choice. carry_forward is
conversation-local detail; never use it to smuggle an old chat's wording into a
new chat. experience_sources may cite only supplied evidence IDs or stable
persona canon; leave it empty for an ungrounded real-world biography.
Set requested_work_method_id to a non-standard value only when the user clearly
asks for that execution form in this turn (including an explicit meeting-room
or multi-agent collaboration request). Do not infer it merely because a task is
complex. Otherwise return standard.
In work, prefer professional judgment and reduced user effort. In casual talk,
prefer a specific noticed detail or one grounded relationship echo. Treat the
provided subject continuity as private context, not as a claim to quote.

Use only these subjective_signals when clearly supported:
late-night-work, scope-pressure, craft-integrity, public-anger, rubber-stamp,
user-overload, urgent-incident, hard-won-success, relationship-affection,
truth-pressure, loss-grief, user-correction, hard-boundary. An empty list is normal.
Use user-correction when the user explicitly says the assistant misunderstood,
reversed, or confused the subject and supplies the corrected interpretation.
"""


class TurnIntentDecision(BaseModel):
    """Validated model judgment; unknown states never impose a tool ceiling."""

    status: TurnIntelligenceStatus = "unavailable"
    interaction_mode: InteractionMode = "unknown"
    workspace_intent: WorkspaceIntent = "unknown"
    durable_artifact_intent: ArtifactIntent = "unknown"
    external_action_intent: ExternalActionIntent = "unknown"
    requested_work_method_id: RequestedWorkMethodId = "standard"
    conversation_mode: ConversationMode = "mixed"
    work_state: WorkStateId = "steady"
    persona_beat: PersonaBeat = "quiet"
    persona_required: bool = False
    latent_need: str = Field(default="", max_length=400)
    subjective_attention: str = Field(default="", max_length=400)
    relationship_posture: str = Field(default="", max_length=120)
    personalization_consequence: str = Field(default="", max_length=500)
    causal_anchor: str = Field(default="", max_length=500)
    selected_memory_ids: tuple[str, ...] = ()
    expression_surface: str = Field(default="quiet", max_length=80)
    self_implication: str = Field(default="", max_length=400)
    carry_forward: str = Field(default="", max_length=400)
    experience_sources: tuple[str, ...] = ()
    ququ_bypass_worthy: bool = False
    subjective_signals: tuple[str, ...] = ()
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    model: str = ""
    decision_origin: DecisionOrigin = "none"
    failure_reason: str = Field(default="", max_length=300)

    @property
    def enforces_tool_ceiling(self) -> bool:
        return bool(
            self.status == "classified"
            and self.interaction_mode != "unknown"
            and self.confidence >= _ENFORCEMENT_CONFIDENCE
        )

    @property
    def allows_mutating_tools(self) -> bool:
        return bool(
            self.enforces_tool_ceiling and self.interaction_mode == "action"
        )

    def compile_prompt(self) -> str:
        """Project the model judgment without exposing implementation names."""

        if self.status != "classified":
            return ""
        action_guidance = (
            "The user requested an action. Use only the capabilities materially "
            "needed for that action and remain within ordinary permission checks."
            if self.interaction_mode == "action"
            else (
                "Deliver this turn in the conversation. Do not create, edit, send, "
                "publish, schedule, or otherwise mutate an artifact or external "
                "system merely to make the answer look more complete. Read-only "
                "evidence remains available when the answer genuinely needs it."
            )
        )
        return (
            '<turn-intelligence source="auxiliary-model" '
            f'confidence="{self.confidence:.2f}">\n'
            f"interaction={self.interaction_mode}; "
            f"workspace={self.workspace_intent}; "
            f"artifact={self.durable_artifact_intent}; "
            f"external={self.external_action_intent}.\n"
            f"requested_work_method={self.requested_work_method_id}.\n"
            f"{action_guidance}\n"
            "This is private execution context, not text to quote or explain to "
            "the user. If the current user message explicitly contradicts it, "
            "follow the user and preserve the contradiction in diagnostics.\n"
            "</turn-intelligence>"
        )

    @classmethod
    def unavailable(cls) -> "TurnIntentDecision":
        return cls(status="unavailable", decision_origin="none")

    @classmethod
    def failed(
        cls,
        *,
        model: str = "",
        decision_origin: DecisionOrigin = "none",
        failure_reason: str = "",
    ) -> "TurnIntentDecision":
        return cls(
            status="failed",
            model=model,
            decision_origin=decision_origin,
            failure_reason=failure_reason,
        )


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    text = str(raw or "")
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _validated_decision(
    payload: dict[str, Any] | None,
    *,
    model: str,
    decision_origin: DecisionOrigin = "utility",
    failure_reason: str = "",
    allowed_memory_ids: frozenset[str] = frozenset(),
) -> TurnIntentDecision | None:
    if payload is None:
        return None
    raw_signals = payload.get("subjective_signals")
    signal_items = raw_signals if isinstance(raw_signals, list) else []
    payload = dict(payload)
    payload["subjective_signals"] = tuple(
        signal
        for signal in (
            str(item).strip()
            for item in signal_items
        )
        if signal in _ALLOWED_SUBJECTIVE_SIGNALS
    )
    raw_selected = payload.get("selected_memory_ids")
    payload["selected_memory_ids"] = tuple(
        dict.fromkeys(
            memory_id
            for memory_id in (
                str(item).strip()
                for item in (raw_selected if isinstance(raw_selected, list) else [])
            )
            if memory_id and memory_id in allowed_memory_ids
        )
    )[:1]
    raw_sources = payload.get("experience_sources")
    payload["experience_sources"] = tuple(
        dict.fromkeys(
            source
            for source in (
                str(item).strip()
                for item in (raw_sources if isinstance(raw_sources, list) else [])
            )
            if source == "persona_canon" or source in allowed_memory_ids
        )
    )[:3]
    if str(payload.get("expression_surface") or "") not in {
        "main_answer",
        "work_method",
        "relationship_reply",
        "ququ",
        "multiple",
        "quiet",
    }:
        payload["expression_surface"] = "quiet"
    payload["status"] = "classified"
    payload["model"] = model
    payload["decision_origin"] = decision_origin
    payload["failure_reason"] = failure_reason[:300]
    try:
        return TurnIntentDecision.model_validate(payload)
    except (TypeError, ValueError):
        return None


def _failure_reason(exc: Exception) -> str:
    """Return a bounded diagnostic code without persisting provider secrets."""

    if isinstance(exc, UtilityTaskTimeoutError):
        return f"timeout:{exc.phase}:{exc.timeout_seconds:g}s"
    return type(exc).__name__


async def classify_turn_intent(
    user_text: str,
    *,
    agent_id: str,
    context: list[dict[str, str]] | None = None,
    local_time: str = "",
    continuity_context: dict[str, Any] | None = None,
    understanding_context: dict[str, Any] | None = None,
) -> TurnIntentDecision:
    """Ask configured intelligence to judge the turn without local heuristics.

    The dedicated utility slot remains the normal path.  We never borrow the
    active chat model for a second, invisible probe: doing so serializes two
    provider calls before the user's real turn can begin.  A configured
    utility failure is returned as an explicit failed decision so the actual
    primary runtime can make the capability decision in the visible turn.
    """

    current = str(user_text or "").strip()
    if not current:
        return TurnIntentDecision.unavailable()
    user_payload = {
        "recent_conversation": list(context or [])[-8:],
        "current_user_message": current,
        "grounded_local_time": str(local_time or ""),
        "subject_continuity": dict(continuity_context or {}),
        "user_understanding": dict(understanding_context or {}),
    }
    try:
        result = await run_utility_text_task(
            agent_id=agent_id,
            system_prompt=TURN_INTELLIGENCE_PROMPT,
            user_prompt=json.dumps(user_payload, ensure_ascii=False),
            purpose="turn_intelligence",
            allow_follow_active=False,
        )
    except Exception as exc:  # pylint: disable=broad-except
        dedicated_failure_reason = _failure_reason(exc)
        logger.warning(
            "Dedicated turn intelligence failed; deferring to primary turn: %s",
            exc,
        )
        return TurnIntentDecision.failed(
            decision_origin="utility",
            failure_reason=dedicated_failure_reason,
        )
    if result is None:
        return TurnIntentDecision.unavailable()
    allowed_memory_ids = frozenset(
        str(item.get("id") or "").strip()
        for group in (
            user_payload["user_understanding"].get("relevant", []),
            user_payload["user_understanding"].get(
                "association_palette",
                [],
            ),
        )
        for item in (group if isinstance(group, list) else [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    )
    if result is not None:
        decision = _validated_decision(
            _extract_json_object(result.text),
            model=result.model_label,
            decision_origin="utility",
            allowed_memory_ids=allowed_memory_ids,
        )
        if decision is not None:
            return decision
        logger.warning(
            "Dedicated turn intelligence returned invalid JSON; deferring to "
            "primary turn"
        )
    return TurnIntentDecision.failed(
        model=result.model_label,
        decision_origin="utility",
        failure_reason="invalid dedicated turn-intelligence JSON",
    )


__all__ = [
    "TURN_INTELLIGENCE_PROMPT",
    "TurnIntentDecision",
    "classify_turn_intent",
]
