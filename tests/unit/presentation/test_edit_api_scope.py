"""Explicit page permissions survive the API boundary without widening defaults."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lantai.app.routers import presentations as api


@pytest.mark.parametrize("additional", [[], ["slide-b", "slide-c"]])
@pytest.mark.parametrize("alias", [False, True])
def test_edit_api_forwards_only_explicit_additional_pages(monkeypatch, additional, alias):
    calls = []

    def redo(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "completed"}

    async def runtime(_request):
        return SimpleNamespace(redo_slide=redo)

    monkeypatch.setattr(api, "_runtime", runtime)
    payload = api.SlideRedo(
        conversation_id="owner", slide_id="slide-a", instruction="修改明确指定的页面",
        base_output_sha256="a" * 64, additional_slide_ids=additional,
    )
    route = api.edit_slide if alias else api.redo_slide
    result = asyncio.run(route("job", payload, SimpleNamespace()))
    assert result == {"status": "completed"}
    assert calls == [(
        ("job", "owner", "slide-a", "修改明确指定的页面", "a" * 64),
        {"additional_slide_ids": additional} if additional else {},
    )]


@pytest.mark.parametrize("additional", [
    ["slide-a"], ["slide-b", "slide-b"], [f"slide-{i}" for i in range(20)],
])
def test_edit_api_rejects_duplicate_or_unbounded_page_permission(additional):
    with pytest.raises(ValidationError):
        api.SlideRedo(
            conversation_id="owner", slide_id="slide-a", instruction="有界修改",
            base_output_sha256="a" * 64, additional_slide_ids=additional,
        )


@pytest.mark.parametrize("alias", [False, True])
def test_edit_api_forwards_explicit_required_pages_without_widening_authority(monkeypatch, alias):
    calls = []

    async def runtime(_request):
        return SimpleNamespace(redo_slide=lambda *a, **k: calls.append((a, k)) or {"status": "completed"})

    monkeypatch.setattr(api, "_runtime", runtime)
    payload = api.SlideRedo(
        conversation_id="owner", slide_id="slide-a", instruction="两页均须完成修改",
        base_output_sha256="a" * 64, additional_slide_ids=["slide-b"],
        required_slide_ids=["slide-a", "slide-b"],
    )
    route = api.edit_slide if alias else api.redo_slide
    assert asyncio.run(route("job", payload, SimpleNamespace())) == {"status": "completed"}
    assert calls[0][1] == {
        "additional_slide_ids": ["slide-b"], "required_slide_ids": ["slide-a", "slide-b"],
    }


@pytest.mark.parametrize("required", [["slide-c"], ["slide-a", "slide-a"], [""], [f"slide-{i}" for i in range(11)]])
def test_edit_api_rejects_invalid_required_pages(required):
    with pytest.raises(ValidationError):
        api.SlideRedo(
            conversation_id="owner", slide_id="slide-a", instruction="不能扩大必改范围",
            base_output_sha256="a" * 64, additional_slide_ids=["slide-b"], required_slide_ids=required,
        )
