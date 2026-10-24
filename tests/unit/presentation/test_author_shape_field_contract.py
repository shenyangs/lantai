# -*- coding: utf-8 -*-
"""Page-author wire guidance, with the real strict structured boundary intact."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lantai.app.structured_utility import StructuredTaskValidationError
from lantai.presentation.intelligence import (
    UtilityPresentationPageAuthor,
    _AuthoredPageScene,
)
from lantai.presentation.models import InputMaterial, PresentationJobRequest
from lantai.presentation.planner import (
    build_brief,
    build_deck_ir,
    build_deterministic_fixture_plan,
)


def _case():
    request = PresentationJobRequest(
        conversation_id="author-shape-field-contract-test",
        user_request="制作收入判断页",
        slide_count=1,
        materials=[InputMaterial(name="合成事实.md", content="甲为42万元。")],
    )
    brief = build_brief(request)
    plan = build_deterministic_fixture_plan(request, brief)
    scaffold = build_deck_ir(request, plan).slides[0]
    scene = {
        "objects": [
            *[obj.model_dump(mode="json") for obj in scaffold.objects],
            {
                "kind": "shape",
                "id": "editable-structure-line",
                "shape_type": "line",
                "frame": {"x": 1, "y": 6.5, "w": 10, "h": 0},
                "native_style": {"fill_none": True, "line_none": False},
            },
        ],
        "speaker_notes": "",
        "object_groups": [],
        "relationships": [],
    }
    return request, brief, plan, scaffold, scene


def test_public_page_author_explains_shape_field_ownership_on_initial_and_retry_calls(
    monkeypatch,
) -> None:
    request, brief, plan, scaffold, scene = _case()
    calls = []

    async def transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text=json.dumps(scene, ensure_ascii=False),
            model_label="approved-provider/approved-model",
        )

    # Only replace the external text transport. Authoring, schema generation,
    # strict parsing, and conversion into the native scene remain production code.
    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", transport,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    for directive in (None, "只修正对象字段归属，不改事实或模板锚点。"):
        result = author.author(
            request, brief, plan, plan.slides[0], scaffold,
            retry_directive=directive,
        )
        shape = result.objects[-1]
        assert shape.kind == "shape"
        assert shape.native_style.fill_none is True
        assert shape.native_style.line_none is False
        assert result.id == scaffold.id

    dependencies = author.cache_dependencies(
        request, brief, plan, plan.slides[0], scaffold,
    )
    for call in calls:
        guidance = call["system_prompt"].split("\n\n只输出一个符合该 schema")[0]
        assert "ShapeObject 不支持 role" in guidance
        assert "native_style.fill_none" in guidance
        assert "native_style.line_none" in guidance
        assert "template_anchors.uses[].purpose" in guidance
        assert dependencies["system_prompt"].strip() == guidance
        assert call["timeout_seconds"] == 360
        assert call["first_output_timeout_seconds"] == 120
        assert call["idle_timeout_seconds"] == 60


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("shape", "role", "body"),
        ("shape", "fill_none", True),
        ("shape", "line_none", True),
        ("shape", "purpose", "static_brand"),
        ("root", "runtime_contract", {"id": "untrusted-slide"}),
        ("root", "quality_passed", True),
    ],
)
def test_public_page_author_still_rejects_misplaced_shape_or_unknown_root_fields(
    monkeypatch, location, field, value,
) -> None:
    request, brief, plan, scaffold, scene = _case()
    target = scene["objects"][-1] if location == "shape" else scene
    target[field] = value

    async def transport(**kwargs):
        return SimpleNamespace(
            text=json.dumps(scene, ensure_ascii=False),
            model_label="approved-provider/approved-model",
        )

    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", transport,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    with pytest.raises(StructuredTaskValidationError) as captured:
        author.author(request, brief, plan, plan.slides[0], scaffold)
    path = (
        f"$.objects[{len(scene['objects']) - 1}].shape.{field}"
        if location == "shape" else f"$.{field}"
    )
    assert f"extra_forbidden@{path}" in captured.value.validation_issues


def test_public_author_sends_schema_derived_kind_and_group_fields_on_every_call(
    monkeypatch,
) -> None:
    request, brief, plan, scaffold, scene = _case()
    calls = []

    async def transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(scene), model_label="test/model")

    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", transport,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    for directive in (None, "只修复原生对象字段，不改变事实。"):
        author.author(
            request, brief, plan, plan.slides[0], scaffold,
            retry_directive=directive,
        )
    dependencies = author.cache_dependencies(
        request, brief, plan, plan.slides[0], scaffold,
    )
    for call in calls:
        schema = call["structured_output_schema"]
        assert schema == _AuthoredPageScene.model_json_schema()
        contract = json.loads(call["user_prompt"])["native_scene_contract"]
        mapping = schema["properties"]["objects"]["items"]["discriminator"]["mapping"]
        assert set(contract["object_types_by_kind"]) == set(mapping)
        for kind, reference in mapping.items():
            definition = schema["$defs"][reference.rsplit("/", 1)[-1]]
            fields = contract["object_types_by_kind"][kind]
            assert fields["required_fields"] == list(
                dict.fromkeys(["kind", *definition.get("required", [])]),
            )
            assert fields["allowed_fields"] == list(definition["properties"])
            assert fields["unknown_fields_allowed"] is False
        group = schema["$defs"]["ObjectGroup"]
        assert contract["object_group_contract"] == {
            "required_fields": group["required"],
            "allowed_fields": list(group["properties"]),
            "unknown_fields_allowed": False,
        }
        assert contract == dependencies["user_payload"]["native_scene_contract"]
        assert "role" in contract["object_types_by_kind"]["text"]["required_fields"]
        assert "role" not in contract["object_types_by_kind"]["shape"]["allowed_fields"]
        assert "shape_type" not in contract["object_types_by_kind"]["text"]["allowed_fields"]


def test_public_author_exposes_schema_literal_values_without_accepting_aliases(
    monkeypatch,
) -> None:
    request, brief, plan, scaffold, scene = _case()
    text_index = next(
        index for index, obj in enumerate(scene["objects"])
        if obj["kind"] == "text"
    )
    scene["objects"][text_index]["valign"] = "mid"
    calls = []

    async def transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(scene), model_label="test/model")

    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", transport,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    result = author.author(request, brief, plan, plan.slides[0], scaffold)
    assert result.objects[text_index].valign == "mid"

    # Guidance must not turn an Office-style alias into an accepted value.
    scene["objects"][text_index]["valign"] = "middle"
    with pytest.raises(StructuredTaskValidationError) as captured:
        author.author(
            request, brief, plan, plan.slides[0], scaffold,
            retry_directive="只修复 valign 的非法取值，保留事实和构图。",
        )
    assert (
        f"literal_error@$.objects[{text_index}].text.valign"
        in captured.value.validation_issues
    )

    dependencies = author.cache_dependencies(
        request, brief, plan, plan.slides[0], scaffold,
    )
    for call in calls:
        schema = call["structured_output_schema"]
        assert schema == _AuthoredPageScene.model_json_schema()
        contract = json.loads(call["user_prompt"])["native_scene_contract"]
        mapping = schema["properties"]["objects"]["items"]["discriminator"]["mapping"]
        for kind, reference in mapping.items():
            properties = schema["$defs"][reference.rsplit("/", 1)[-1]]["properties"]
            expected = {}
            for field, definition in properties.items():
                if "enum" in definition:
                    expected[field] = definition["enum"]
                elif "const" in definition:
                    expected[field] = [definition["const"]]
            assert contract["object_types_by_kind"][kind]["literal_values"] == expected
        text_values = contract["object_types_by_kind"]["text"]["literal_values"]
        assert text_values["kind"] == ["text"]
        assert text_values["valign"] == ["top", "mid", "bottom"]
        assert "middle" not in text_values["valign"]
        assert contract == dependencies["user_payload"]["native_scene_contract"]


def test_public_author_exposes_point_margin_atomicity_without_changing_schema(
    monkeypatch,
) -> None:
    request, brief, plan, scaffold, scene = _case()
    calls = []

    async def transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(scene), model_label="test/model")

    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", transport,
    )
    author = UtilityPresentationPageAuthor("agent-1")
    for directive in (None, "修复 native_style，不改变边距单位。"):
        author.author(
            request, brief, plan, plan.slides[0], scaffold,
            retry_directive=directive,
        )
    for call in calls:
        native_style = json.loads(call["user_prompt"])["native_scene_contract"][
            "native_shape_style_contract"
        ]
        definition = call["structured_output_schema"]["$defs"]["NativeShapeStyle"]
        assert native_style["allowed_fields"] == list(definition["properties"])
        assert native_style["unknown_fields_allowed"] is False
        assert native_style["margin_contract"] == {
            "fields": ["margin_top", "margin_right", "margin_bottom", "margin_left"],
            "unit": "points",
            "minimum": 0,
            "maximum": 144,
            "all_non_null_or_all_absent_or_null": True,
        }


@pytest.mark.parametrize("invalid", ["text_role", "group_role", "text_paint", "partial_margin"])
def test_public_author_still_rejects_missing_roles_mixed_kinds_and_partial_margins(
    monkeypatch, invalid,
) -> None:
    request, brief, plan, scaffold, scene = _case()
    text_index = next(index for index, obj in enumerate(scene["objects"]) if obj["kind"] == "text")
    if invalid == "text_role":
        scene["objects"][text_index].pop("role")
        expected = f"missing@$.objects[{text_index}].text.role"
    elif invalid == "group_role":
        scene["object_groups"] = [{"object_ids": [scene["objects"][text_index]["id"]]}]
        expected = "missing@$.object_groups[0].role"
    elif invalid == "text_paint":
        scene["objects"][text_index]["fill_color"] = "FFFFFF"
        expected = f"extra_forbidden@$.objects[{text_index}].text.fill_color"
    else:
        scene["objects"][-1]["native_style"]["margin_top"] = 4
        expected = f"value_error@$.objects[{len(scene['objects']) - 1}].shape.native_style"

    async def transport(**kwargs):
        return SimpleNamespace(text=json.dumps(scene), model_label="test/model")

    monkeypatch.setattr(
        "lantai.app.structured_utility.run_utility_text_task", transport,
    )
    with pytest.raises(StructuredTaskValidationError) as captured:
        UtilityPresentationPageAuthor("agent-1").author(
            request, brief, plan, plan.slides[0], scaffold,
        )
    assert expected in captured.value.validation_issues
