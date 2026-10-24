from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pptx import Presentation
from pydantic import ValidationError

from lantai.presentation import cli
from lantai.presentation.compiler import PresentationCompiler
from lantai.presentation.models import (
    DeckIR,
    DeckSlide,
    PresentationJobRequest,
    RenderedSlideEvidence,
    TextObject,
)
from lantai.presentation.quality import file_sha256, inspect_pptx


class _CompletedFuture:
    timeout_seen: int | None = None

    def result(self, timeout: int) -> None:
        self.timeout_seen = timeout


class _FakeRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._futures = {"job-1": _CompletedFuture()}
        self.deck = DeckIR(
            id="deck-cli",
            title="会写代码的办公 Agent 来了",
            slides=[
                DeckSlide(
                    id="slide-cli",
                    layout_id="cover",
                    objects=[
                        TextObject(
                            id="title-cli",
                            role="title",
                            text="会写代码的办公 Agent 来了",
                            slot="title",
                        )
                    ],
                )
            ],
        )
        self.output = root / "job-1" / "output" / "presentation.pptx"
        PresentationCompiler().compile(self.deck, self.output)
        self.render_path = (
            root / "job-1" / "renders" / "round-0" / "slide-1.png"
        )
        self.render_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (640, 360), "white")
        image.paste("black", (0, 0, 320, 360))
        image.save(self.render_path)
        self.report = inspect_pptx(
            self.deck,
            self.output,
            rendered_slide_count=1,
        ).model_copy(
            update={
                "renderer_id": "test-renderer",
                "renderer_version": "1.0",
                "font_manifest": ["Arial"],
                "rendered_slides": [
                    RenderedSlideEvidence(
                        slide_id="slide-cli",
                        slide_index=1,
                        sha256=file_sha256(self.render_path),
                        width=640,
                        height=360,
                    )
                ],
            }
        )

    def create_and_start(self, request: PresentationJobRequest):
        assert request.conversation_id == "conversation-1"
        self.request = request
        return SimpleNamespace(job_id="job-1")

    def get(self, job_id: str, conversation_id: str):
        assert (job_id, conversation_id) == ("job-1", "conversation-1")
        return SimpleNamespace(
            status=SimpleNamespace(value="completed"), events=[]
        )

    def output_path(self, job_id: str, conversation_id: str) -> Path:
        return self.output

    def artifacts(
        self, job_id: str, conversation_id: str
    ) -> dict[str, object]:
        return {
            "deck_ir": self.deck.model_dump(mode="json"),
            "quality": self.report.model_dump(mode="json"),
        }

    def shutdown(self) -> None:
        return None


class _FailedRuntime(_FakeRuntime):
    def get(self, job_id: str, conversation_id: str):
        assert (job_id, conversation_id) == ("job-1", "conversation-1")
        return SimpleNamespace(
            status=SimpleNamespace(value="failed"),
            events=[SimpleNamespace(message="quality failed")],
        )


def test_generate_uses_the_final_deck_topic_as_the_default_filename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        PresentationJobRequest(
            conversation_id="conversation-1",
            user_request="把这个做成 PPT 吧",
            slide_count=6,
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "PresentationJobRuntime", _FakeRuntime)

    result = cli.generate(
        request_path,
        tmp_path / "jobs",
        output_dir=tmp_path / "delivery",
    )

    delivered = Path(str(result["output_path"]))
    assert delivered.name == "会写代码的办公 Agent 来了.pptx"
    assert len(Presentation(delivered).slides) == 1


def test_generate_wait_tracks_job_deadline_instead_of_fixed_five_minutes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        PresentationJobRequest(
            conversation_id="conversation-1",
            user_request="按任务期限完成演示",
            timeout_seconds=120,
        ).model_dump_json(),
        encoding="utf-8",
    )
    runtime = _FakeRuntime(tmp_path / "jobs")
    monkeypatch.setattr(cli, "PresentationJobRuntime", lambda _root: runtime)
    cli.generate(request_path, tmp_path / "jobs")
    assert runtime._futures["job-1"].timeout_seen == 150


def test_generate_repairs_captured_model_material_aliases(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "conversation_id": "conversation-1",
                "user_request": "把材料做成 PPT",
                "materials": [
                    {
                        "path": str(source),
                        "role": "source_document",
                        "description": "唯一事实来源",
                    },
                    {
                        "type": "file",
                        "path": str(source),
                        "title": "第二份来源",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime = _FakeRuntime(tmp_path / "jobs")
    monkeypatch.setattr(cli, "PresentationJobRuntime", lambda _root: runtime)

    cli.generate(
        request_path, tmp_path / "jobs", output_dir=tmp_path / "delivery"
    )

    assert [item.name for item in runtime.request.materials] == [
        "source.md",
        "第二份来源",
    ]
    assert [item.path for item in runtime.request.materials] == [
        str(source),
        str(source),
    ]


def test_generate_does_not_copy_a_failed_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        PresentationJobRequest(
            conversation_id="conversation-1",
            user_request="失败候选不能交付",
            slide_count=1,
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "PresentationJobRuntime", _FailedRuntime)

    result = cli.generate(
        request_path,
        tmp_path / "jobs",
        output_dir=tmp_path / "delivery",
    )

    manifest = json.loads(
        Path(str(result["manifest"])).read_text(encoding="utf-8")
    )
    assert result["status"] == "failed"
    assert result["output_path"] is None
    assert not list((tmp_path / "delivery").glob("*.pptx"))
    assert manifest["visual_verified"] is False
    assert manifest["artifacts"]["presentation"]["available"] is False


def test_material_alias_repair_keeps_unknown_fields_strict(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        """{
  "schema_version": "1.0",
  "conversation_id": "conversation-1",
  "user_request": "把材料做成 PPT",
  "materials": [
    {"path": "source.md", "title": "来源", "unexpected": true}
  ]
}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unexpected"):
        cli._load_request(request_path)


@pytest.mark.parametrize("wrapper", ["user_request", "presentation_request"])
def test_load_request_repairs_serialized_or_nested_presentation_controls(
    tmp_path: Path,
    wrapper: str,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("## 第1页 结论\n真实材料", encoding="utf-8")
    controls = {
        "language": "zh-CN",
        "slide_count": 1,
        "theme": "lantai-ink",
        "speaker_notes": True,
        "materials": [{"type": "text", "path": "source.md"}],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "conversation_id": "conversation-1",
                wrapper: (
                    json.dumps(controls, ensure_ascii=False)
                    if wrapper == "user_request"
                    else controls
                ),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    request = cli._load_request(request_path)

    assert request.user_request == "根据提供的材料制作演示文稿"
    assert request.slide_count == 1
    assert request.theme_id == "lantai-ink"
    assert len(request.materials) == 1
    assert request.materials[0].path == str(source.resolve())


def test_load_request_normalizes_model_authored_theme_alias(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "conversation_id": "conversation-1",
                "user_request": "制作一份明亮、干净的商务演示文稿",
                "theme_id": "business",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    request = cli._load_request(request_path)

    assert request.theme_id == "lantai-ink"
