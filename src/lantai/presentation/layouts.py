"""Semantic layout registry and deterministic capacity policies."""

from __future__ import annotations

from dataclasses import dataclass

from .models import DeckSlide, LayoutSlot, LayoutTemplate, NativeGroupObject, TextObject
from .native_group import validate_native_carrier


CORE_LAYOUTS: dict[str, LayoutTemplate] = {
    "freeform": LayoutTemplate(
        id="freeform",
        semantic_role="freeform",
        slots=[
            LayoutSlot(
                id="canvas",
                accepts={"text", "image", "metric", "table", "chart", "shape", "native_group"},
                max_items=80,
            )
        ],
        hard_constraints=[
            "every object must have an explicit frame",
            "all frames must stay within the 16:9 canvas",
        ],
    ),
    "cover": LayoutTemplate(
        id="cover",
        semantic_role="cover",
        slots=[
            LayoutSlot(id="title", accepts={"text"}, max_chars=54),
            LayoutSlot(id="subtitle", accepts={"text"}, max_chars=120),
            LayoutSlot(id="meta", accepts={"text"}, max_items=2, max_chars=120),
            LayoutSlot(id="cover_visual", accepts={"image"}),
        ],
        fallback_layout_id="key_judgment",
    ),
    "section": LayoutTemplate(id="section", semantic_role="section", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=36), LayoutSlot(id="body", accepts={"text"}, max_chars=180)], fallback_layout_id="key_judgment"),
    "key_judgment": LayoutTemplate(id="key_judgment", semantic_role="judgment", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="body", accepts={"text"}, max_items=6, max_chars=520)], fallback_layout_id="two_column"),
    "two_column": LayoutTemplate(id="two_column", semantic_role="two_column", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="left", accepts={"text", "image", "chart"}, max_items=5, max_chars=320), LayoutSlot(id="right", accepts={"text", "image", "chart"}, max_items=5, max_chars=320)]),
    "three_cards": LayoutTemplate(id="three_cards", semantic_role="cards", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="card_1", accepts={"text", "metric"}, max_items=3, max_chars=150), LayoutSlot(id="card_2", accepts={"text", "metric"}, max_items=3, max_chars=150), LayoutSlot(id="card_3", accepts={"text", "metric"}, max_items=3, max_chars=150)], fallback_layout_id="two_column"),
    "comparison": LayoutTemplate(id="comparison", semantic_role="comparison", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="left", accepts={"text", "metric"}, max_items=5, max_chars=300), LayoutSlot(id="right", accepts={"text", "metric"}, max_items=5, max_chars=300)], fallback_layout_id="two_column"),
    "process": LayoutTemplate(id="process", semantic_role="process", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), *[LayoutSlot(id=f"step_{index}", accepts={"text"}, max_items=2, max_chars=120) for index in range(1, 6)]], fallback_layout_id="three_cards"),
    "data_conclusion": LayoutTemplate(id="data_conclusion", semantic_role="data", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="chart", accepts={"chart", "table"}), LayoutSlot(id="metric_1", accepts={"metric"}), LayoutSlot(id="metric_2", accepts={"metric"}), LayoutSlot(id="conclusion", accepts={"text"}, max_chars=200)], fallback_layout_id="key_judgment"),
    "agenda": LayoutTemplate(id="agenda", semantic_role="agenda", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="items", accepts={"text"}, max_items=8, max_chars=520)], fallback_layout_id="key_judgment"),
    "matrix": LayoutTemplate(id="matrix", semantic_role="matrix", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), *[LayoutSlot(id=f"cell_{index}", accepts={"text", "metric"}, max_items=3, max_chars=160) for index in range(1, 5)]], fallback_layout_id="three_cards"),
    "hierarchy": LayoutTemplate(id="hierarchy", semantic_role="hierarchy", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="root", accepts={"text"}, max_chars=100), *[LayoutSlot(id=f"node_{index}", accepts={"text"}, max_items=2, max_chars=130) for index in range(1, 5)]], fallback_layout_id="process"),
    "table": LayoutTemplate(id="table", semantic_role="table", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="table", accepts={"table"}), LayoutSlot(id="conclusion", accepts={"text"}, max_chars=180)], fallback_layout_id="data_conclusion"),
    "chart": LayoutTemplate(id="chart", semantic_role="chart", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="chart", accepts={"chart"}), LayoutSlot(id="conclusion", accepts={"text", "metric"}, max_items=2, max_chars=180)], fallback_layout_id="data_conclusion"),
    "image_focus": LayoutTemplate(id="image_focus", semantic_role="image", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="hero", accepts={"image"}), LayoutSlot(id="caption", accepts={"text"}, max_chars=160)], fallback_layout_id="two_column"),
    "quote": LayoutTemplate(
        id="quote",
        semantic_role="quote",
        slots=[
            LayoutSlot(id="quote", accepts={"text"}, max_chars=260),
            LayoutSlot(id="attribution", accepts={"text"}, max_chars=80),
            LayoutSlot(id="quote_visual", accepts={"image"}),
        ],
        fallback_layout_id="key_judgment",
    ),
    "closing": LayoutTemplate(id="closing", semantic_role="closing", slots=[LayoutSlot(id="title", accepts={"text"}, max_chars=48), LayoutSlot(id="body", accepts={"text"}, max_items=4, max_chars=360), LayoutSlot(id="contact", accepts={"text"}, max_chars=120)], fallback_layout_id="section"),
}


@dataclass(frozen=True)
class CapacityViolation:
    slot: str
    message: str


def get_layout(layout_id: str) -> LayoutTemplate:
    try:
        return CORE_LAYOUTS[layout_id]
    except KeyError as exc:
        raise ValueError(f"unknown semantic layout: {layout_id}") from exc


def check_capacity(slide: DeckSlide) -> list[CapacityViolation]:
    layout = get_layout(slide.layout_id)
    slots = {slot.id: slot for slot in layout.slots}
    violations: list[CapacityViolation] = []
    grouped: dict[str, int] = {}
    for obj in slide.objects:
        # Freeform objects keep semantic slots (title/body/hero) for reasoning,
        # provenance and downstream editing.  Capacity belongs to the single
        # canvas rather than to fixed layout slots.
        slot_id = "canvas" if layout.id == "freeform" else obj.slot
        # A read-only group is not a loophole in the existing primitive budget.
        # Validate its carrier and charge every native child, not one container.
        count = validate_native_carrier(obj)["child_count"] if isinstance(obj, NativeGroupObject) else 1
        grouped[slot_id] = grouped.get(slot_id, 0) + count
        slot = slots.get(slot_id)
        if slot is None:
            violations.append(CapacityViolation(obj.slot, f"slot {obj.slot!r} is not defined by {layout.id}"))
            continue
        if obj.kind not in slot.accepts:
            violations.append(CapacityViolation(obj.slot, f"{obj.kind} is not accepted by slot {obj.slot}"))
        if layout.id == "freeform" and obj.frame is None:
            violations.append(
                CapacityViolation(
                    obj.slot,
                    f"freeform object {obj.id} requires an explicit frame",
                )
            )
        if isinstance(obj, TextObject) and slot.max_chars and len(obj.text) > slot.max_chars:
            violations.append(CapacityViolation(slot_id, f"text exceeds {slot.max_chars} characters"))
    for slot_id, object_count in grouped.items():
        if slot_id in slots and object_count > slots[slot_id].max_items:
            violations.append(CapacityViolation(slot_id, f"slot accepts at most {slots[slot_id].max_items} objects"))
    return violations
