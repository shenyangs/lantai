# -*- coding: utf-8 -*-
"""Deterministic convergence policy for Lantai agent loops.

The model remains responsible for deciding how to solve the user's task.  This
module owns the narrower runtime question: whether another model/tool round is
still earning meaningful progress.  It intentionally uses bounded, auditable
signals and asks for a single model review only when those signals become
suspicious; hard budgets remain the final safety net.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Sequence

from .tools import ToolCall, ToolResult


ConvergenceAction = Literal["continue", "review", "synthesize", "checkpoint"]
ConvergenceMode = Literal["off", "shadow", "enforce"]

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", flags=re.IGNORECASE)
_QUERY_KEYS = (
    "query",
    "q",
    "search_query",
    "keyword",
    "keywords",
    "question",
    "topic",
)
_SOURCE_ARGUMENT_KEYS = (
    "path",
    "file_path",
    "image_path",
    "source_path",
    "url",
    "uri",
    "ref_id",
)
_SOURCE_QUALIFIER_KEYS = ("pageno", "page", "sheet", "range")
_PROGRESS_METADATA_KEYS = (
    "progress_keys",
    "resolved_acceptance",
    "acceptance_updates",
    "completed_items",
    "evidence_ids",
)
_RESOURCE_VERSION_METADATA_KEYS = (
    "resource_version",
    "resource_revision",
    "content_sha256",
    "etag",
    "mtime_ns",
)
_RESEARCH_MARKERS = ("search", "research", "wiki", "retriev", "lookup")
_READ_MARKERS = ("read", "fetch", "open", "view", "inspect", "list", "glob", "grep")
_MUTATION_MARKERS = (
    "write",
    "edit",
    "create",
    "delete",
    "remove",
    "update",
    "patch",
    "execute",
    "finalize",
    "send",
)
_COLLABORATION_MARKERS = ("agent", "collaborat", "delegate", "handoff")


def convergence_mode(value: str | None = None) -> ConvergenceMode:
    """Return the rollout mode, defaulting to the user-facing enforcement path."""

    raw = str(
        value if value is not None else os.getenv("LANTAI_CONVERGENCE_MODE", "enforce")
    ).strip().lower()
    return raw if raw in {"off", "shadow", "enforce"} else "enforce"  # type: ignore[return-value]


@dataclass(frozen=True)
class ConvergencePolicy:
    """One work-method-specific set of soft convergence boundaries."""

    method_id: str
    search_family_limit: int
    read_family_limit: int
    collaboration_family_limit: int
    low_novelty_window: int
    max_no_progress_rounds: int
    soft_model_ratio: float = 0.85
    soft_token_ratio: float = 0.90
    reserve_synthesis_rounds: int = 1


def policy_for_work_method(
    method_id: str,
    *,
    max_no_progress_rounds: int | None = None,
    collaboration_member: bool = False,
    overrides: Mapping[str, Any] | None = None,
) -> ConvergencePolicy:
    """Compile conservative soft limits without changing persisted hard budgets."""

    method = str(method_id or "standard").strip().lower() or "standard"
    settings = {
        "standard": (8, 12, 6, 4),
        "deep_research": (18, 24, 8, 5),
        "sustained_execution": (40, 48, 12, 6),
        "collaboration": (10, 16, 4, 4),
    }
    search_limit, read_limit, collaboration_limit, novelty_window = settings.get(
        method,
        settings["standard"],
    )
    if collaboration_member:
        # A member returns bounded evidence to a parent host.  It must not turn
        # a consultation into an unbounded independent research programme.
        search_limit = min(search_limit, 10)
        read_limit = min(read_limit, 14)
        collaboration_limit = 1
        novelty_window = min(novelty_window, 4)
    override_values = dict(overrides or {})
    if override_values:
        try:
            search_limit = min(
                40,
                max(1, int(override_values.get("max_research_calls", search_limit))),
            )
            read_limit = min(
                48,
                max(1, int(override_values.get("max_read_calls", read_limit))),
            )
            max_no_progress_rounds = min(
                4,
                max(
                    1,
                    int(
                        override_values.get(
                            "max_no_progress_rounds",
                            max_no_progress_rounds or 3,
                        )
                    ),
                ),
            )
        except (TypeError, ValueError):
            # Invalid optional policy metadata must not disable the safe,
            # work-method-owned defaults.
            pass
    return ConvergencePolicy(
        method_id=method,
        search_family_limit=search_limit,
        read_family_limit=read_limit,
        collaboration_family_limit=collaboration_limit,
        low_novelty_window=novelty_window,
        max_no_progress_rounds=max(1, int(max_no_progress_rounds or 3)),
    )


@dataclass(frozen=True)
class ConvergenceObservation:
    tool_name: str
    family: str
    ok: bool
    intent_fingerprint: str
    result_fingerprint: str
    explicit_progress: bool | None
    new_sources: int
    new_progress_keys: int
    novel_result: bool
    made_progress: bool


@dataclass(frozen=True)
class ConvergenceDecision:
    action: ConvergenceAction = "continue"
    reason: str = ""
    summary: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def should_intervene(self) -> bool:
        return self.action != "continue"


def classify_tool_family(tool_name: str) -> str:
    """Project provider/plugin-specific names into stable convergence families."""

    folded = str(tool_name or "").strip().lower()
    if any(marker in folded for marker in _COLLABORATION_MARKERS):
        return "collaboration"
    if any(marker in folded for marker in _RESEARCH_MARKERS):
        return "research"
    if any(marker in folded for marker in _MUTATION_MARKERS):
        return "mutation"
    if any(marker in folded for marker in _READ_MARKERS):
        return "read"
    return "other"


def _normalized_text(value: object, *, limit: int = 12_000) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()[:limit]
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _argument_intent(arguments: Mapping[str, Any]) -> str:
    for key in _QUERY_KEYS:
        if key in arguments and arguments[key] not in (None, "", [], {}):
            return _normalized_text(arguments[key], limit=2_000)
    public_shape = {
        str(key): value
        for key, value in arguments.items()
        if str(key).lower() not in {"token", "api_key", "password", "secret"}
    }
    return _normalized_text(
        json.dumps(public_shape, ensure_ascii=False, sort_keys=True, default=str),
        limit=2_000,
    )


def _argument_source_fingerprint(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Identify local/read-only evidence even when a tool returns no URL.

    Image and file readers often return the same short status payload for
    every successful call.  Their source lives in the call arguments, so the
    convergence controller must include that source when measuring progress.
    """

    source = {
        key: arguments[key]
        for key in _SOURCE_ARGUMENT_KEYS
        if arguments.get(key) not in (None, "", [], {})
    }
    if not source:
        return None
    qualifiers = {
        key: arguments[key]
        for key in _SOURCE_QUALIFIER_KEYS
        if arguments.get(key) not in (None, "", [], {})
    }
    return _stable_hash(
        {
            "tool": str(tool_name or "").strip().lower(),
            "source": source,
            "qualifiers": qualifiers,
        },
    )


def _result_payload(result: ToolResult) -> str:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    payload = {
        "content": result.content,
        "structured": metadata.get("structured_content"),
        "resource_version": {
            key: metadata[key]
            for key in _RESOURCE_VERSION_METADATA_KEYS
            if metadata.get(key) not in (None, "")
        },
        "error": result.error,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)[:80_000]


def _metadata_progress_keys(metadata: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field_name in _PROGRESS_METADATA_KEYS:
        raw = metadata.get(field_name)
        if isinstance(raw, Mapping):
            values: Sequence[Any] = [
                f"{key}:{value}"
                for key, value in sorted(
                    raw.items(),
                    key=lambda item: str(item[0]),
                )
            ]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            values = list(raw)
        elif raw not in (None, ""):
            values = [raw]
        else:
            values = []
        for value in values:
            normalized = _normalized_text(value, limit=1_000)
            if normalized:
                keys.add(f"{field_name}:{_stable_hash(normalized)}")
    return keys


def _char_ngrams(text: str, size: int = 2) -> set[str]:
    if not text:
        return set()
    if len(text) <= size:
        return {text}
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def intent_similarity(left: str, right: str) -> float:
    """Return a language-agnostic similarity suitable for short search intents."""

    left_grams = _char_ngrams(left)
    right_grams = _char_ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


class ConvergenceController:
    """Track one run's progress and recommend a bounded next action."""

    def __init__(
        self,
        *,
        objective: str,
        acceptance: Sequence[str] = (),
        policy: ConvergencePolicy,
        mode: ConvergenceMode = "enforce",
        snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        self.objective = str(objective or "").strip()
        self.acceptance = tuple(str(item).strip() for item in acceptance if str(item).strip())
        self.policy = policy
        self.mode = convergence_mode(mode)
        state = dict(snapshot or {})
        raw_family_counts = state.get("family_counts") or {}
        if isinstance(raw_family_counts, Mapping):
            family_items = raw_family_counts.items()
        elif isinstance(raw_family_counts, Sequence) and not isinstance(
            raw_family_counts,
            (str, bytes),
        ):
            family_items = (
                (item.get("family"), item.get("count"))
                for item in raw_family_counts
                if isinstance(item, Mapping)
            )
        else:
            family_items = ()
        self.family_counts: dict[str, int] = {
            str(key): max(0, int(value or 0))
            for key, value in family_items
            if str(key or "").strip()
        }
        self.seen_sources: set[str] = {
            str(value) for value in state.get("seen_sources") or () if str(value)
        }
        self.seen_results: set[str] = {
            str(value) for value in state.get("seen_results") or () if str(value)
        }
        self.seen_progress_keys: set[str] = {
            str(value) for value in state.get("seen_progress_keys") or () if str(value)
        }
        self.consecutive_no_progress_rounds = max(
            0,
            int(state.get("consecutive_no_progress_rounds") or 0),
        )
        self.review_count = max(0, int(state.get("review_count") or 0))
        self._recent: list[ConvergenceObservation] = []
        for item in state.get("recent") or ():
            if not isinstance(item, Mapping):
                continue
            try:
                self._recent.append(ConvergenceObservation(**dict(item)))
            except (TypeError, ValueError):
                continue
        self._recent = self._recent[-24:]
        self._round_observations: list[ConvergenceObservation] = []
        self._round_progress = False

    def start_round(self) -> None:
        self._round_observations = []
        self._round_progress = False

    def revise_intent(
        self,
        *,
        objective: str,
        acceptance: Sequence[str] = (),
    ) -> None:
        """Adopt a newer durable task revision without losing real evidence."""

        self.objective = str(objective or "").strip()
        self.acceptance = tuple(
            str(item).strip() for item in acceptance if str(item).strip()
        )
        self.consecutive_no_progress_rounds = 0
        self.review_count = 0
        self._round_observations = []
        self._round_progress = False

    def observe(self, tool_call: ToolCall, result: ToolResult) -> ConvergenceObservation:
        family = classify_tool_family(tool_call.name)
        self.family_counts[family] = self.family_counts.get(family, 0) + 1
        intent = _argument_intent(tool_call.arguments)
        intent_fingerprint = _stable_hash({"family": family, "intent": intent})
        result_payload = _result_payload(result)
        result_fingerprint = _stable_hash(result_payload)
        novel_result = result_fingerprint not in self.seen_results
        if novel_result:
            self.seen_results.add(result_fingerprint)

        source_fingerprints = {
            _stable_hash(url.rstrip(".,);]"))
            for url in _URL_RE.findall(result_payload)
        }
        if result.ok and family in {"read", "research"}:
            argument_source = _argument_source_fingerprint(
                tool_call.name,
                tool_call.arguments,
            )
            if argument_source:
                source_fingerprints.add(argument_source)
        new_sources = len(source_fingerprints - self.seen_sources)
        self.seen_sources.update(source_fingerprints)

        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        progress_keys = _metadata_progress_keys(metadata)
        new_progress_keys = len(progress_keys - self.seen_progress_keys)
        self.seen_progress_keys.update(progress_keys)
        raw_explicit = metadata.get("progress_made")
        explicit_progress = raw_explicit if isinstance(raw_explicit, bool) else None

        if explicit_progress is False or not result.ok:
            made_progress = False
        elif explicit_progress is True:
            made_progress = True
        elif new_progress_keys or new_sources:
            made_progress = True
        elif family == "mutation":
            # Successful mutations are meaningful unless the tool explicitly
            # reports that no state changed.  This preserves build/edit loops.
            made_progress = result.ok and novel_result
        else:
            made_progress = result.ok and novel_result

        observation = ConvergenceObservation(
            tool_name=tool_call.name,
            family=family,
            ok=result.ok,
            intent_fingerprint=intent_fingerprint,
            result_fingerprint=result_fingerprint,
            explicit_progress=explicit_progress,
            new_sources=new_sources,
            new_progress_keys=new_progress_keys,
            novel_result=novel_result,
            made_progress=made_progress,
        )
        self._recent.append(observation)
        self._recent = self._recent[-24:]
        self._round_observations.append(observation)
        self._round_progress = self._round_progress or made_progress
        return observation

    def finish_round(
        self,
        *,
        model_round_count: int,
        max_model_rounds: int | None,
        used_tokens: int,
        max_tokens: int | None,
    ) -> ConvergenceDecision:
        if self.mode == "off":
            return ConvergenceDecision()

        if self._round_progress:
            self.consecutive_no_progress_rounds = 0
            # Healthy work is never stopped because it crossed a resource or
            # tool-family counter.  Those counters remain diagnostic evidence,
            # not user-visible quotas.
            return ConvergenceDecision()
        else:
            self.consecutive_no_progress_rounds += 1

        if self.consecutive_no_progress_rounds >= self.policy.max_no_progress_rounds:
            return ConvergenceDecision(
                action="review" if self.review_count == 0 else "synthesize",
                reason="semantic_no_progress",
                summary=(
                    "连续多轮没有改变任务验收状态，需要判断是否应当收尾。"
                ),
                evidence=self.public_state(),
            )

        family_reason = self._family_saturation_reason()
        if family_reason:
            return ConvergenceDecision(
                action="review" if self.review_count == 0 else "synthesize",
                reason=family_reason,
                summary=(
                    "同类工具调用已经趋于饱和，需要判断继续执行是否仍有价值。"
                ),
                evidence=self.public_state(),
            )

        novelty_reason = self._low_novelty_reason()
        if novelty_reason:
            return ConvergenceDecision(
                action="review" if self.review_count == 0 else "synthesize",
                reason=novelty_reason,
                summary="最近的工具结果没有带来新的来源或任务进展。",
                evidence=self.public_state(),
            )
        return ConvergenceDecision()

    def record_review(self, action: str, *, reason: str = "") -> ConvergenceDecision:
        """Record the one-shot semantic review and return its enforceable action."""

        self.review_count += 1
        normalized = str(action or "").strip().lower()
        if normalized == "continue":
            return ConvergenceDecision(
                action="continue",
                reason=reason or "review_allows_one_more_round",
                summary="收敛检查确认仍有明确的新进展空间。",
                evidence=self.public_state(),
            )
        if normalized in {"ask_user", "checkpoint"}:
            return ConvergenceDecision(
                action="checkpoint",
                reason=reason or "review_requests_checkpoint",
                summary=(
                    "任务已保存当前成果，并把是否继续的选择交还给用户。"
                ),
                evidence=self.public_state(),
            )
        return ConvergenceDecision(
            action="synthesize",
            reason=reason or "review_requests_synthesis",
            summary="现有信息已经足够，或继续执行不会带来相称的新价值。",
            evidence=self.public_state(),
        )

    def _family_saturation_reason(self) -> str:
        limits = {
            "research": self.policy.search_family_limit,
            "read": self.policy.read_family_limit,
            "collaboration": self.policy.collaboration_family_limit,
        }
        for family, limit in limits.items():
            if self.family_counts.get(family, 0) >= limit:
                return f"{family}_family_saturation"
        return ""

    def _low_novelty_reason(self) -> str:
        window = self.policy.low_novelty_window
        recent = [item for item in self._recent if item.family in {"research", "read"}]
        if len(recent) < window:
            return ""
        tail = recent[-window:]
        if any(
            item.explicit_progress is True
            or item.new_sources > 0
            or item.new_progress_keys > 0
            for item in tail
        ):
            return ""
        if sum(1 for item in tail if item.novel_result) > 1:
            return ""
        return "low_result_novelty"

    def public_state(self) -> dict[str, Any]:
        """Return bounded diagnostics that never expose queries or tool output."""

        resolved_acceptance_count = sum(
            1
            for key in self.seen_progress_keys
            if key.startswith("resolved_acceptance:")
        )
        return {
            "method_id": self.policy.method_id,
            "family_counts": dict(sorted(self.family_counts.items())),
            "consecutive_no_progress_rounds": self.consecutive_no_progress_rounds,
            "unique_sources": len(self.seen_sources),
            "unique_progress_keys": len(self.seen_progress_keys),
            "resolved_acceptance_count": resolved_acceptance_count,
            "review_count": self.review_count,
            "mode": self.mode,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a resumable but bounded state projection for RuntimeCheckpoint."""

        return {
            **self.public_state(),
            "seen_sources": sorted(self.seen_sources)[-256:],
            "seen_results": sorted(self.seen_results)[-256:],
            "seen_progress_keys": sorted(self.seen_progress_keys)[-256:],
            "recent": [asdict(item) for item in self._recent[-24:]],
        }


def build_convergence_review_prompt(
    *,
    objective: str,
    acceptance: Sequence[str],
    trigger: ConvergenceDecision,
) -> str:
    """Build the sole, tool-free semantic review prompt for a suspicious run."""

    payload = {
        "objective": str(objective or "")[:4_000],
        "acceptance": [str(item)[:800] for item in acceptance[:20]],
        "trigger": trigger.reason,
        "runtime_evidence": trigger.evidence,
    }
    return (
        "You are Lantai's one-shot convergence reviewer. Decide whether one more "
        "agent/tool round has a concrete chance to change an unmet acceptance "
        "condition. Do not solve the task, call tools, or request another review. "
        "Return JSON only: {\"action\":\"continue|synthesize|ask_user\","
        "\"reason\":\"short factual reason\"}. Choose continue only when a specific "
        "next step and its expected new evidence are clear. Choose synthesize when "
        "the trajectory is saturated but a best-effort answer is possible. Choose "
        "ask_user only when a material scope choice blocks honest continuation.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def parse_convergence_review(text: str) -> tuple[str, str]:
    """Parse a strict review response; malformed output fails toward synthesis."""

    candidate = str(text or "").strip()
    fenced = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
    if fenced is not None:
        candidate = fenced.group(0)
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "synthesize", "convergence_review_invalid"
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"continue", "synthesize", "ask_user"}:
        return "synthesize", "convergence_review_invalid"
    reason = str(payload.get("reason") or "").strip()[:500]
    return action, reason or f"convergence_review_{action}"


__all__ = [
    "ConvergenceAction",
    "ConvergenceController",
    "ConvergenceDecision",
    "ConvergenceMode",
    "ConvergenceObservation",
    "ConvergencePolicy",
    "build_convergence_review_prompt",
    "classify_tool_family",
    "convergence_mode",
    "intent_similarity",
    "parse_convergence_review",
    "policy_for_work_method",
]
