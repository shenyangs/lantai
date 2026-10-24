"""Optional visual enrichment must not hold source imports indefinitely."""

import asyncio

import pytest

from lantai.app.wiki import core


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["discovery", "generation"])
async def test_visual_enrichment_deadline_cancels_pending_stage(
    monkeypatch, tmp_path, blocked_stage,
):
    monkeypatch.setattr(core, "MAX_WIKI_SEMANTIC_TIMEOUT", 0.01)
    cancelled = []
    attempted = []

    async def block():
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            cancelled.append(blocked_stage)
            raise

    async def targets(_agent):
        if blocked_stage == "discovery":
            await block()
        return [{"model_id": "first"}, {"model_id": "second"}]

    async def generate(target, *_args):
        attempted.append(target["model_id"])
        await block()
        return {"status": "completed", "summary": "visual summary"}

    monkeypatch.setattr(core, "_resolve_visual_model_targets", targets)
    monkeypatch.setattr(core, "_generate_semantic_understanding_async", generate)
    result = await core._generate_semantic_understanding_with_fallbacks(
        tmp_path, tmp_path / "source.pdf", {}, "default",
    )
    assert result["status"] == "failed"
    assert "超时" in result["message"]
    assert not result["summary"]
    assert cancelled == [blocked_stage]
    assert attempted == ([] if blocked_stage == "discovery" else ["first"])


@pytest.mark.asyncio
async def test_visual_enrichment_preserves_successful_fallback(monkeypatch, tmp_path):
    expected = {"status": "completed", "summary": "available visual evidence"}
    attempted = []

    async def targets(_agent):
        return [{"model_id": "first"}, {"model_id": "second"}]

    async def generate(target, *_args):
        attempted.append(target["model_id"])
        return expected if len(attempted) == 2 else {"status": "failed"}

    monkeypatch.setattr(core, "_resolve_visual_model_targets", targets)
    monkeypatch.setattr(core, "_generate_semantic_understanding_async", generate)
    result = await core._generate_semantic_understanding_with_fallbacks(
        tmp_path, tmp_path / "source.pdf", {}, "default",
    )
    assert result == expected
    assert attempted == ["first", "second"]


@pytest.mark.parametrize("local_summary", ["", "Local layout summary"])
def test_pdf_upload_preserves_raw_text_and_reports_visual_timeout(monkeypatch, tmp_path, local_summary):
    monkeypatch.setattr(core, "MAX_WIKI_SEMANTIC_TIMEOUT", 0.01)

    async def slow_discovery(_agent):
        await asyncio.sleep(0.1)
        return []

    monkeypatch.setattr(core, "_resolve_visual_model_targets", slow_discovery)
    monkeypatch.setattr(core, "extract_source_text_detail", lambda *_args, **_kw: {
        "status": "text_extracted", "method": "pypdf", "text": "Full source text",
        "text_chars": 16, "page_count": 40,
        "understanding_summary": local_summary,
    })
    result = core.import_wiki_source_upload(
        tmp_path, "source.pdf", b"test pdf source", agent_id="default",
    )
    detail = result["details"][0]
    assert detail["semantic_status"] == "failed"
    assert "超时" in detail["semantic_message"]
    assert detail["extraction_status"] == "text_extracted"
    assert (tmp_path / result["imported"][0]["path"]).read_bytes() == b"test pdf source"
    page = next((tmp_path / ".wiki/pages/sources").glob("*.md")).read_text()
    assert "Full source text" in page
    assert "超时" in page
