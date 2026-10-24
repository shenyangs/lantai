from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lantai.presentation.compiler import PresentationCompiler
from lantai.presentation.models import (
    DeckIR, DeckSlide, NativeMetricText, ObjectFrame, ShapeObject, TextObject,
)
from lantai.presentation.versioning import (
    PresentationVersionStore, StalePresentationVersionError,
    deck_ir_matches_version, deck_ir_version,
)


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _payload(*, older_native_defaults: bool = False) -> dict:
    deck = DeckIR(id="pre-metric-checkpoint", title="版本兼容", slides=[
        DeckSlide(id="page-1", layout_id="cover", objects=[
            TextObject(id="title-1", role="title", text="原有标题", slot="title"),
            ShapeObject(id="shape-1", frame=ObjectFrame(x=1, y=3, w=2, h=1)),
        ]),
    ])
    payload = deck.model_dump(mode="json")
    payload["slides"][0]["objects"][0].pop("native_metric_text")
    if older_native_defaults:
        payload.pop("template_binding")
        slide = payload["slides"][0]
        slide.pop("template_anchors")
        slide.pop("semantic_layout")
        shape = slide["objects"][1]
        shape.pop("runs")
        shape.pop("native_style")
    return payload


def _owned_legacy(tmp_path: Path, payload: dict):
    deck = DeckIR.model_validate(payload)
    output = tmp_path / "presentation.pptx"
    PresentationCompiler().compile(deck, output)
    store = PresentationVersionStore(tmp_path)
    canonical = store.bootstrap(output, deck, writer="legacy_python_pptx")
    legacy = canonical.model_copy(update={"deck_ir_version": _hash(payload)})
    # This is an external pre-M20 ledger fixture, not a migration performed
    # by the implementation under test. Native compilation remains real.
    store.current_path.write_text(legacy.model_dump_json(indent=2), encoding="utf-8")
    store.history_path.write_text(legacy.model_dump_json() + "\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in (output, store.current_path, store.history_path)}
    return deck, output, store, legacy, canonical, before


@pytest.mark.parametrize("older_native_defaults", [False, True])
def test_pre_metric_writer_ownership_is_accepted_without_rewriting(
    tmp_path: Path, older_native_defaults: bool,
) -> None:
    payload = _payload(older_native_defaults=older_native_defaults)
    deck, output, store, legacy, canonical, before = _owned_legacy(tmp_path, payload)
    assert "native_metric_text" not in deck.slides[0].objects[0].model_fields_set
    assert deck_ir_version(deck) == canonical.deck_ir_version != legacy.deck_ir_version

    assert store.assert_writer_base(
        output, deck, expected_pptx_sha256=legacy.pptx_sha256,
    ) == legacy
    assert {p: p.read_bytes() for p in before} == before
    assert deck_ir_version(deck) == canonical.deck_ir_version


@pytest.mark.parametrize("field,value", [
    ("text", "真的改了正文"),
    ("font_size", 44),
    ("color", "112233"),
    ("frame", {"x": 1, "y": 1, "w": 3, "h": 1}),
    ("runs", [{"text": "改变原生样式", "bold": True}]),
    ("native_metric_text", {"value": "12", "label": "数量", "detail": "合成测试"}),
    ("native_metric_text", None),
])
def test_pre_metric_compatibility_never_erases_changed_or_explicit_state(
    tmp_path: Path, field: str, value,
) -> None:
    payload = _payload()
    _, output, store, _, _, before = _owned_legacy(tmp_path, payload)
    payload["slides"][0]["objects"][0][field] = value
    changed = DeckIR.model_validate(payload)

    with pytest.raises(StalePresentationVersionError, match="DeckIR base hash"):
        store.assert_writer_base(output, changed)
    assert {p: p.read_bytes() for p in before} == before


def test_current_explicit_null_metric_state_keeps_its_existing_hash() -> None:
    payload = _payload()
    payload["slides"][0]["objects"][0]["native_metric_text"] = None
    loaded = DeckIR.model_validate(payload)
    assert "native_metric_text" in loaded.slides[0].objects[0].model_fields_set
    assert deck_ir_version(loaded) == _hash(payload)
    assert deck_ir_matches_version(loaded, _hash(payload))
    payload["slides"][0]["objects"][0].pop("native_metric_text")
    assert not deck_ir_matches_version(loaded, _hash(payload))


@pytest.mark.parametrize("field", ["value", "label", "detail"])
def test_nonempty_metric_identity_remains_part_of_current_semantic_hash(field: str) -> None:
    deck = DeckIR.model_validate(_payload())
    deck.slides[0].objects.append(TextObject(
        id="metric-1", role="metric", text="12\n数量\n合成测试",
        native_metric_text=NativeMetricText(value="12", label="数量", detail="合成测试"),
    ))
    before = deck_ir_version(deck)
    changed = deck.model_copy(deep=True)
    setattr(changed.slides[0].objects[-1].native_metric_text, field, "实际改变")
    assert deck_ir_version(changed) != before
    assert not deck_ir_matches_version(changed, before)


def test_pre_metric_current_native_history_records_a_real_new_parent(
    tmp_path: Path,
) -> None:
    deck, output, store, legacy, _, _ = _owned_legacy(tmp_path, _payload())
    history_prefix = store.history_path.read_bytes()
    parent = store.assert_writer_base(output, deck)
    changed = deck.model_copy(deep=True)
    changed.slides[0].objects[0].text = "经过作者编辑的新标题"
    PresentationCompiler().compile(changed, output)
    recorded = store.record(output, changed, writer="lantai_ai_edit", parent=parent)
    assert recorded.parent_version_id == legacy.version_id
    assert recorded.deck_ir_version == deck_ir_version(changed)
    assert recorded.pptx_sha256 != legacy.pptx_sha256
    assert store.history_path.read_bytes().startswith(history_prefix)
    assert store.history() == [legacy, recorded]
    assert store.assert_writer_base(output, changed) == recorded
