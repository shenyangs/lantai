# -*- coding: utf-8 -*-
"""Legacy agent classes used by the compatibility LegacyLantaiAgent path."""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from ..runtime.adapters.legacy_toolkit_components import LegacyToolkit
from ..runtime.converters import (
    LegacyInMemoryMemory,
    LegacyMemoryMark,
    LegacyMsg,
    is_legacy_msg_like,
)

logger = logging.getLogger(__name__)

_REPEAT_SENSITIVE_SUBJECTIVE_SIGNALS = frozenset(
    {"late-night-work", "user-overload", "hard-won-success"},
)
_DETERMINISTIC_PERSONA_REPAIR_SIGNALS = frozenset({"user-correction"})


class LegacyAgentLoop:
    """Minimal Lantai ReAct base compatible with the old caller surface."""

    # State initialization is explicit because native and legacy runtimes
    # share it.
    # pylint: disable-next=too-many-statements
    def __init__(
        self,
        name: str,
        sys_prompt: str,
        model: Any,
        formatter: Any,
        toolkit: LegacyToolkit | None = None,
        memory: Any | None = None,
        long_term_memory: Any | None = None,
        long_term_memory_mode: Literal[
            "agent_control",
            "static_control",
            "both",
        ] = "both",
        enable_meta_tool: bool = False,
        parallel_tool_calls: bool = False,
        knowledge: Any | list[Any] | None = None,
        enable_rewrite_query: bool = True,
        plan_notebook: Any | None = None,
        print_hint_msg: bool = False,
        max_iters: int = 10,
        tts_model: Any | None = None,
        compression_config: Any | None = None,
        persona_context_provider: Callable[
            [],
            tuple[Any, str],
        ]
        | None = None,
        user_understanding_provider: Callable[[Any], Any] | None = None,
        subject_continuity_provider: Callable[[Any], Any] | None = None,
        persona_mode_provider: Callable[[], str] | None = None,
    ) -> None:
        if long_term_memory_mode not in {
            "agent_control",
            "static_control",
            "both",
        }:
            raise AssertionError("Invalid long_term_memory_mode.")

        self.id = uuid4().hex
        self.name = name
        self._sys_prompt = ""
        self._persona_state_enabled = False
        self._turn_intelligence_decision: Any | None = None
        self._turn_intelligence_prompt = ""
        self._temperament_enforcement = ""
        self._work_state_prompt = ""
        self._subjective_reaction_prompt = ""
        self._continuity_prompt = ""
        self._user_understanding_prompt = ""
        self._subject_continuity_prompt = ""
        self._perceptible_presence_prompt = ""
        self._ququ_spirit_prompt = ""
        self._prompt_manifest: dict[str, Any] | None = None
        from .persona.continuity import ContinuityState
        from .persona.ququ import QuquSpiritState
        from .persona.perceptible_presence import PresenceState

        self._continuity_state = ContinuityState()
        self._ququ_spirit_state = QuquSpiritState()
        self._perceptible_presence_state = PresenceState()
        self._persona_turn_plan = None
        self._persona_preparation_receipt = None
        self.replace_system_prompt(sys_prompt)
        self._persona_context_provider = persona_context_provider
        self._user_understanding_provider = user_understanding_provider
        self._prepared_understanding_override: Any | None = None
        self._subject_continuity_provider = subject_continuity_provider
        self._prepared_subject_continuity_override: Any | None = None
        self._subject_turn_projection: Any | None = None
        self._persona_mode_provider = persona_mode_provider
        self._recent_subjective_signal_turns: deque[tuple[str, ...]] = deque(
            maxlen=3,
        )
        self._last_persona_turn_key: tuple[tuple[str, str], ...] | None = None
        self.max_iters = max_iters
        self.model = model
        self.formatter = formatter
        self.tts_model = tts_model
        self.compression_config = compression_config
        self.memory = memory or LegacyInMemoryMemory()
        self.long_term_memory = long_term_memory
        self._static_control = long_term_memory and long_term_memory_mode in [
            "static_control",
            "both",
        ]
        self._agent_control = long_term_memory and long_term_memory_mode in [
            "agent_control",
            "both",
        ]

        self.toolkit = toolkit or LegacyToolkit()
        if self._agent_control:
            self.toolkit.register_tool_function(
                long_term_memory.record_to_memory,
            )
            self.toolkit.register_tool_function(
                long_term_memory.retrieve_from_memory,
            )
        if enable_meta_tool:
            self.toolkit.register_tool_function(
                self.toolkit.reset_equipped_tools,
            )
        self.parallel_tool_calls = parallel_tool_calls
        # Kept as a diagnostic facade for callers that need the exact payload
        # accounting without changing the legacy model-call signature.
        self.last_context_manifest: dict[str, Any] | None = None

        if isinstance(knowledge, list):
            self.knowledge = knowledge
        elif knowledge is None:
            self.knowledge = []
        else:
            self.knowledge = [knowledge]
        self.enable_rewrite_query = enable_rewrite_query

        self.plan_notebook = None
        if plan_notebook:
            self.plan_notebook = plan_notebook
            if enable_meta_tool:
                self.toolkit.create_tool_group(
                    "plan_related",
                    description=self.plan_notebook.description,
                )
                for tool in plan_notebook.list_tools():
                    self.toolkit.register_tool_function(
                        tool,
                        group_name="plan_related",
                    )
            else:
                for tool in plan_notebook.list_tools():
                    self.toolkit.register_tool_function(tool)

        self.print_hint_msg = print_hint_msg

    def replace_system_prompt(self, sys_prompt: str) -> None:
        """Install a rebuilt base prompt and keep enforcement truly final."""

        self._sys_prompt = sys_prompt
        self._persona_state_enabled = (
            "constitution_id=shen-xingzhi-default" in sys_prompt
        )
        self._ququ_state_enabled = (
            '<ququ-route baseline_selected="' in sys_prompt
            or "【蛐蛐 · 内心独白】" in sys_prompt
        )
        self._ququ_baseline_selected = (
            '<ququ-route baseline_selected="true" hard_required="true" />'
            in sys_prompt
            # Compatibility only: pre-2.0 persisted sessions used this exact
            # route title before the structured machine marker existed.
            or "【本轮蛐蛐路由：基础爆率命中】" in sys_prompt
        )
        self._temperament_enforcement = ""
        enforcement_marker = "<temperament-constitution-enforcement"
        enforcement_index = sys_prompt.rfind(enforcement_marker)
        if self._persona_state_enabled and enforcement_index >= 0:
            enforcement_close = "</temperament-constitution-enforcement>"
            enforcement_end = sys_prompt.find(
                enforcement_close,
                enforcement_index,
            )
            if enforcement_end >= 0:
                enforcement_end += len(enforcement_close)
                prefix = sys_prompt[:enforcement_index].strip()
                suffix = sys_prompt[enforcement_end:].strip()
                self._sys_prompt = "\n\n".join(
                    part for part in (prefix, suffix) if part
                )
                self._temperament_enforcement = sys_prompt[
                    enforcement_index:enforcement_end
                ].strip()
            else:
                self._sys_prompt = sys_prompt[:enforcement_index].rstrip()
                self._temperament_enforcement = sys_prompt[
                    enforcement_index:
                ].strip()
        if not self._persona_state_enabled:
            self._turn_intelligence_prompt = ""
            self._work_state_prompt = ""
            self._subjective_reaction_prompt = ""
            self._continuity_prompt = ""
            self._user_understanding_prompt = ""
            self._subject_continuity_prompt = ""
            self._perceptible_presence_prompt = ""
            self._ququ_spirit_prompt = ""
        if not self._ququ_state_enabled:
            self._ququ_spirit_prompt = ""

    @property
    def sys_prompt(self) -> str:
        from .language_policy import canonical_prompt_language
        from .prompt_system import PromptRegistry, PromptSection

        registry = PromptRegistry(
            language=canonical_prompt_language(
                getattr(self, "_language", "zh"),
            ),
        )
        section_specs = (
            ("compiled.base", self._sys_prompt, "agent_runtime", "stable"),
            (
                "turn.intent",
                self._turn_intelligence_prompt,
                "turn_intelligence",
                "turn",
            ),
            ("turn.work_state", self._work_state_prompt, "persona", "turn"),
            (
                "turn.subjective_reaction",
                self._subjective_reaction_prompt,
                "persona",
                "turn",
            ),
            (
                "session.continuity",
                self._continuity_prompt,
                "persona",
                "session",
            ),
            (
                "session.user_understanding",
                self._user_understanding_prompt,
                "persona",
                "session",
            ),
            (
                "session.subject_continuity",
                self._subject_continuity_prompt,
                "persona",
                "session",
            ),
            (
                "turn.perceptible_presence",
                self._perceptible_presence_prompt,
                "persona",
                "turn",
            ),
            ("turn.ququ_spirit", self._ququ_spirit_prompt, "persona", "turn"),
        )
        for section_id, content, owner, cache_tier in section_specs:
            registry.add(
                PromptSection(
                    id=section_id,
                    content=content,
                    owner=owner,
                    required=section_id == "compiled.base",
                    dynamic=cache_tier in {"session", "turn"},
                    cache_tier=cache_tier,
                    source_kind=(
                        "runtime_projection"
                        if cache_tier in {"session", "turn"}
                        else "platform_policy"
                    ),
                ),
            )
        agent_skill_prompt = self.toolkit.get_agent_skill_prompt()
        registry.add(
            PromptSection(
                id="skills.active_index",
                content=agent_skill_prompt or "",
                owner="skills",
                dynamic=True,
                cache_tier="tool",
                source_kind="skill_projection",
            ),
        )
        registry.add(
            PromptSection(
                id="kernel.temperament_enforcement",
                content=self._temperament_enforcement,
                owner="persona",
                version="1.6.1",
                required=bool(self._temperament_enforcement),
                cache_tier="kernel",
                source_kind="platform_canon",
            ),
        )
        active_tools = tuple(
            tool_name
            for schema in self.toolkit.get_json_schemas()
            if isinstance(schema, dict)
            for tool_name in (
                str(
                    schema.get("function", {}).get("name")
                    or schema.get("name")
                    or "",
                ),
            )
            if tool_name
        )
        build = registry.compile(active_tools=active_tools)
        self._prompt_manifest = build.manifest
        return build.prompt

    @property
    def prompt_manifest(self) -> dict[str, Any] | None:
        """Return the latest exact system-prompt projection manifest."""
        _ = self.sys_prompt
        return self._prompt_manifest

    def set_turn_intelligence_decision(self, decision: Any) -> None:
        """Install one auxiliary-model judgment for the upcoming turn."""

        from .turn_intelligence import TurnIntentDecision

        if isinstance(decision, TurnIntentDecision):
            normalized = decision
        elif isinstance(decision, dict):
            try:
                normalized = TurnIntentDecision.model_validate(decision)
            except (TypeError, ValueError):
                normalized = TurnIntentDecision.failed()
        else:
            normalized = TurnIntentDecision.unavailable()
        self._turn_intelligence_decision = normalized
        self._turn_intelligence_prompt = normalized.compile_prompt()

    def set_subject_continuity_projection(self, projection: Any) -> None:
        """Install the runner-prepared projection for the upcoming turn."""

        from .persona.subject_continuity import SubjectTurnProjection

        self._prepared_subject_continuity_override = (
            projection
            if isinstance(projection, SubjectTurnProjection)
            else None
        )

    def set_user_understanding_turn_context(self, context: Any) -> None:
        """Install the runner-prepared evidence projection for this turn."""

        from .persona.user_understanding import UserUnderstandingTurnContext

        self._prepared_understanding_override = (
            context
            if isinstance(context, UserUnderstandingTurnContext)
            else None
        )

    @property
    def turn_intelligence_decision(self) -> Any | None:
        return self._turn_intelligence_decision

    def state_dict(self) -> dict[str, Any]:
        prompt_manifest = self.prompt_manifest
        return {
            "name": self.name,
            # Keep the legacy field for schema compatibility, but never write
            # raw platform/persona instructions into ordinary session files.
            # The active prompt is rebuilt from its authoritative sources on
            # every runner turn; the content-free manifest is sufficient for
            # provenance and diagnosis.
            "_sys_prompt": "",
            "prompt_manifest": prompt_manifest,
            # This is diagnostic state, not an input source. Keeping the last
            # manifest makes a restored session explainable without treating a
            # stale measurement as the next model call's budget.
            "last_context_manifest": self.last_context_manifest,
            "recent_subjective_signal_turns": list(
                self._recent_subjective_signal_turns,
            ),
            "persona_continuity": self._continuity_state.to_dict(),
            "ququ_spirit": self._ququ_spirit_state.to_dict(),
            "perceptible_presence": self._perceptible_presence_state.to_dict(),
        }

    def load_state_dict(
        self,
        state_dict: dict[str, Any],
        strict: bool = True,
    ) -> None:
        for key in ("name", "_sys_prompt"):
            if key in state_dict:
                value = state_dict[key]
                if key != "_sys_prompt" or (
                    isinstance(value, str) and value.strip()
                ):
                    setattr(self, key, value)
            elif strict:
                raise KeyError(
                    f"Key '{key}' not found in state_dict. Ensure that the "
                    "state_dict contains all required keys.",
                )
        manifest = state_dict.get("last_context_manifest")
        self.last_context_manifest = (
            manifest if isinstance(manifest, dict) else None
        )
        raw_signal_turns = state_dict.get("recent_subjective_signal_turns")
        if isinstance(raw_signal_turns, list):
            restored_signal_turns = [
                tuple(str(signal_id) for signal_id in signal_turn)
                for signal_turn in raw_signal_turns
                if isinstance(signal_turn, (list, tuple))
            ]
            self._recent_subjective_signal_turns = deque(
                restored_signal_turns,
                maxlen=3,
            )
        from .persona.continuity import ContinuityState

        self._continuity_state = ContinuityState.from_dict(
            state_dict.get("persona_continuity"),
        )
        from .persona.ququ import QuquSpiritState

        self._ququ_spirit_state = QuquSpiritState.from_dict(
            state_dict.get("ququ_spirit"),
        )
        from .persona.perceptible_presence import PresenceState

        self._perceptible_presence_state = PresenceState.from_dict(
            state_dict.get("perceptible_presence"),
        )

    @property
    def persona_turn_plan(self) -> Any | None:
        """Return the current observable contract for runtime validation."""

        return self._persona_turn_plan

    @property
    def persona_preparation_receipt(self) -> Any | None:
        """Return detached evidence from the latest successful preparation."""

        return self._persona_preparation_receipt

    @property
    def subject_turn_projection(self) -> Any | None:
        """Return the exact subject projection consumed by this turn."""

        return self._subject_turn_projection

    def _observe_persisted_ququ(self) -> None:
        """Absorb unseen assistant Ququ lines from the shared chat memory."""

        if not self._ququ_state_enabled:
            return
        content = getattr(getattr(self, "memory", None), "content", None)
        if not isinstance(content, list):
            return
        for item in content:
            message = (
                item[0] if isinstance(item, (list, tuple)) and item else item
            )
            if str(getattr(message, "role", "") or "") != "assistant":
                continue
            get_text = getattr(message, "get_text_content", None)
            text = (
                str(get_text() or "") if callable(get_text) else str(message)
            )
            self._ququ_spirit_state = (
                self._ququ_spirit_state.observe_assistant(
                    text,
                    message_id=str(getattr(message, "id", "") or ""),
                )
            )

    @staticmethod
    def _persona_turn_key(message: Any) -> tuple[tuple[str, str], ...]:
        messages = message if isinstance(message, list) else [message]
        key: list[tuple[str, str]] = []
        for item in messages:
            if item is None:
                continue
            role = str(getattr(item, "role", "") or "")
            if hasattr(item, "get_text_content"):
                text = str(item.get_text_content() or "")
            else:
                text = str(item)
            key.append((role, text))
        return tuple(key)

    # This is the protected persona state transition; keep its gates visible.
    # pylint: disable-next=too-many-branches,too-many-statements
    def prepare_persona_turn(self, message: Any) -> None:
        """Project persona context for native and legacy runtimes."""

        turn_key = self._persona_turn_key(message)
        if turn_key and turn_key == self._last_persona_turn_key:
            return
        # Invalidate before starting a distinct turn so a preparation failure
        # can never expose the previous turn's diagnostic receipt.
        self._persona_preparation_receipt = None
        self._last_persona_turn_key = turn_key
        if not self._persona_state_enabled:
            self._work_state_prompt = ""
            self._subjective_reaction_prompt = ""
            self._continuity_prompt = ""
            self._user_understanding_prompt = ""
            self._subject_continuity_prompt = ""
            self._perceptible_presence_prompt = ""
            self._persona_turn_plan = None
            self._ququ_spirit_prompt = ""
            try:
                from .persona_receipt import (
                    capture_persona_preparation_receipt,
                )

                self._persona_preparation_receipt = (
                    capture_persona_preparation_receipt(
                        source_mode="disabled",
                        language="",
                        turn_key=turn_key,
                        subject_context={},
                        subject_continuity_prompt="",
                        perceptible_presence_prompt="",
                    )
                )
            except Exception:
                logger.warning(
                    "Persona preparation receipt capture failed open",
                )
            return

        from .persona import (
            SubjectiveReaction,
            WorkState,
            current_user_local_time,
            project_subjective_reaction,
            project_work_state_prompt,
        )

        intelligence = self._turn_intelligence_decision
        if intelligence is not None:
            if intelligence.status == "classified":
                self._work_state_prompt = WorkState(
                    intelligence.work_state,
                ).compile_prompt(
                    "zh"
                    if re.search(r"[\u4e00-\u9fff]", str(turn_key))
                    else "en",
                )
                if self._persona_context_provider is not None:
                    now, timezone_name = self._persona_context_provider()
                else:
                    now, timezone_name = current_user_local_time()
                reaction = SubjectiveReaction(
                    signal_ids=tuple(intelligence.subjective_signals),
                    local_time=(
                        f"{now.strftime('%Y-%m-%d %H:%M')} {timezone_name}"
                    ),
                    language=(
                        "zh"
                        if re.search(r"[\u4e00-\u9fff]", str(turn_key))
                        else "en"
                    ),
                )
            else:
                self._work_state_prompt = ""
                reaction = SubjectiveReaction(
                    signal_ids=(),
                    local_time="",
                    language=(
                        "zh"
                        if re.search(r"[\u4e00-\u9fff]", str(turn_key))
                        else "en"
                    ),
                )
        else:
            # Compatibility for direct legacy-loop callers outside the normal
            # Runner seam. Production turns always install model intelligence.
            self._work_state_prompt = project_work_state_prompt(message)
            reaction = project_subjective_reaction(
                message,
                context_provider=self._persona_context_provider,
            )
        # Tool ceilings and semantic intent remain model-owned. An explicit
        # correction is a local relationship fact rather than an inferred task
        # category, so it alone keeps a deterministic repair cue. Safety and
        # relationship posture still constrain outcomes, but local regexes do
        # not dictate the voice or force a boundary recital into the answer.
        grounded_reaction = project_subjective_reaction(
            message,
            context_provider=self._persona_context_provider,
        )
        repair_signals = tuple(
            signal_id
            for signal_id in grounded_reaction.signal_ids
            if signal_id in _DETERMINISTIC_PERSONA_REPAIR_SIGNALS
            and signal_id not in reaction.signal_ids
        )
        if repair_signals:
            reaction = SubjectiveReaction(
                signal_ids=(*reaction.signal_ids, *repair_signals),
                local_time=reaction.local_time or grounded_reaction.local_time,
                language=reaction.language,
            )
        user_text = "\n".join(text for _role, text in turn_key if text)
        if self._ququ_state_enabled:
            from .persona.ququ import prepare_ququ_turn

            self._observe_persisted_ququ()
            ququ_turn = prepare_ququ_turn(
                self._ququ_spirit_state,
                user_text=user_text,
                baseline_selected=self._ququ_baseline_selected,
                language=reaction.language,
            )
            self._ququ_spirit_state = ququ_turn.state
            self._ququ_spirit_prompt = ququ_turn.prompt
        else:
            self._ququ_spirit_prompt = ""
        self._continuity_state = self._continuity_state.advance(
            signal_ids=reaction.signal_ids,
            user_text=user_text,
        )
        self._continuity_prompt = self._continuity_state.compile_prompt(
            reaction.language,
        )
        from .persona.user_understanding import (
            TurnUnderstandingProjection,
            UserUnderstandingTurnContext,
        )

        understanding_projection = TurnUnderstandingProjection()
        if self._prepared_understanding_override is not None:
            understanding = self._prepared_understanding_override
            self._prepared_understanding_override = None
            if isinstance(understanding, UserUnderstandingTurnContext):
                self._user_understanding_prompt = understanding.prompt.strip()
                understanding_projection = understanding.projection
        elif self._user_understanding_provider is not None:
            try:
                understanding = self._user_understanding_provider(message)
                if isinstance(understanding, UserUnderstandingTurnContext):
                    self._user_understanding_prompt = (
                        understanding.prompt.strip()
                    )
                    understanding_projection = understanding.projection
                else:
                    self._user_understanding_prompt = str(
                        understanding or "",
                    ).strip()
            except Exception:
                logger.warning(
                    "User understanding projection failed open",
                    exc_info=True,
                )
                self._user_understanding_prompt = ""
        from .persona.subject_continuity import SubjectTurnProjection

        subject_projection = None
        subject_projection_source = "absent"
        if isinstance(
            self._prepared_subject_continuity_override,
            SubjectTurnProjection,
        ):
            subject_projection = self._prepared_subject_continuity_override
            subject_projection_source = "runner_override"
            self._prepared_subject_continuity_override = None
        elif self._subject_continuity_provider is not None:
            try:
                candidate = self._subject_continuity_provider(message)
                if isinstance(candidate, SubjectTurnProjection):
                    subject_projection = candidate
                    subject_projection_source = "agent_provider"
            except Exception:
                logger.warning(
                    "Subject continuity projection failed open",
                    exc_info=True,
                )
        self._subject_turn_projection = subject_projection
        self._subject_continuity_prompt = (
            subject_projection.compile_prompt(reaction.language)
            if subject_projection is not None
            else ""
        )
        from .persona.perceptible_presence import (
            PersonaTurnPlan,
            plan_persona_turn,
        )

        persona_mode = "full"
        if self._persona_mode_provider is not None:
            try:
                candidate_mode = str(self._persona_mode_provider() or "full")
                if candidate_mode in {"full", "balanced", "quiet", "off"}:
                    persona_mode = candidate_mode
            except Exception:
                logger.warning(
                    "Persona mode projection failed open",
                    exc_info=True,
                )
        self._persona_turn_plan = plan_persona_turn(
            user_text,
            projection=understanding_projection,
            state=self._perceptible_presence_state,
            persona_mode=persona_mode,
            intent_decision=intelligence,
            subject_projection=subject_projection,
        )
        self._perceptible_presence_state = self._persona_turn_plan.next_state
        self._perceptible_presence_prompt = (
            self._persona_turn_plan.compile_prompt(
                reaction.language,
            )
        )
        if type(self._persona_turn_plan) is PersonaTurnPlan and (
            subject_projection is None
            or type(subject_projection) is SubjectTurnProjection
        ):
            try:
                from .persona_receipt import (
                    capture_persona_preparation_receipt,
                )

                self._persona_preparation_receipt = (
                    capture_persona_preparation_receipt(
                        source_mode=subject_projection_source,
                        language=reaction.language,
                        turn_key=turn_key,
                        subject_context=(
                            self._persona_turn_plan.subject_context
                        ),
                        subject_continuity_prompt=(
                            self._subject_continuity_prompt
                        ),
                        perceptible_presence_prompt=(
                            self._perceptible_presence_prompt
                        ),
                    )
                )
            except Exception:
                logger.warning(
                    "Persona preparation receipt capture failed open",
                )
        recent_signal_ids = {
            signal_id
            for signal_turn in self._recent_subjective_signal_turns
            for signal_id in signal_turn
        }
        suppressed_signal_ids = (
            recent_signal_ids & _REPEAT_SENSITIVE_SUBJECTIVE_SIGNALS
        )
        self._subjective_reaction_prompt = reaction.without_signals(
            suppressed_signal_ids,
        ).compile_prompt()
        self._recent_subjective_signal_turns.append(reaction.signal_ids)

    async def prepare_persona_turn_async(self, message: Any) -> None:
        """Run the optional live judge before compiling the turn."""

        self.prepare_persona_turn(message)


__all__ = [
    "LegacyInMemoryMemory",
    "LegacyMemoryMark",
    "LegacyMsg",
    "LegacyAgentLoop",
    "LegacyToolkit",
    "is_legacy_msg_like",
]
