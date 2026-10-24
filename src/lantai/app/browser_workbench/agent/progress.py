"""Bounded factual feedback for model-owned browser recovery.

Inspired by browser-use ActionLoopDetector. We derive facts from Lantai's
existing history, without a second mutable task store or generated decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from .contracts import BrowserAgentContext


def browser_progress_facts(context: BrowserAgentContext) -> dict[str, object]:
    steps = [item for item in context.history[-20:] if item.decision.action is not None]
    signatures: list[str] = []
    transitions: list[tuple[str, str]] = []
    for step in steps:
        action = step.decision.action
        assert action is not None
        signature = {
            "action": action.action,
            "url": action.url,
            "page_id": action.page_id,
            "target": action.selector
            or action.metadata.get("browser_agent_element_name")
            or action.ref,
            "frame": action.metadata.get("frame_selector", ""),
            "shadow": action.metadata.get("shadow_host_selector", ""),
            "text": action.text,
            "key": action.metadata.get("key", ""),
        }
        signatures.append(
            hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        )
        transitions.append(
            (
                step.before_progress_hash or step.before_revision,
                step.after_progress_hash or step.after_revision,
            )
        )
    counts = Counter(signatures)
    unchanged_counts = (
        Counter(signatures[-context.no_progress_count :])
        if context.no_progress_count
        else Counter()
    )
    # A cycle is a repeated pair of page transitions, not merely a repeated
    # Next button. Pagination that keeps revealing new content is progress.
    cycle_length = 0
    for width in range(2, min(5, len(transitions) // 2) + 1):
        tail = transitions[-width:]
        if transitions[-2 * width : -width] == tail and tail[-1][1] == tail[0][0]:
            cycle_length = width
            break
    return {
        "observed_steps": len(steps),
        "consecutive_unchanged_pages": context.no_progress_count,
        "most_repeated_action_count": max(counts.values(), default=0),
        "unchanged_action_repeat_count": max(unchanged_counts.values(), default=0),
        "repeated_cycle_length": cycle_length,
        "last_step_changed_page": bool(
            transitions and transitions[-1][0] != transitions[-1][1]
        ),
        "reconsider_strategy": context.no_progress_count > 0 or cycle_length > 0,
    }
