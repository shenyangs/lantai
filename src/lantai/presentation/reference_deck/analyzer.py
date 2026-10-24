# -*- coding: utf-8 -*-
"""High-level optional reference-learning entry point for PPTX, PDF and images."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable

from PIL import Image

from ..models import DesignQualityFloor, TemplateMode
from ..pdf_render import render_pdf_pages
from ..render import render_pptx
from .models import ReferenceDeckMeasurement, ReferenceLearningResult
from .object_parser import PptxObjectParser
from .profile_builder import ReferenceProfileBuilder
from .visual_analyzer import RenderedSlideAnalyzer


def _combined_image_hash(paths: list[Path]) -> str:
    digest = sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def reference_source_sha256(source: Path) -> str:
    """Return the source identity used by a single-file reference analysis."""

    if source.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}:
        return _combined_image_hash([source])
    return sha256(source.read_bytes()).hexdigest()


def _visual_only_measurement(
    source_name: str,
    source_format: str,
    source_hash: str,
    image_paths: list[Path],
) -> ReferenceDeckMeasurement:
    with Image.open(image_paths[0]) as image:
        ratio = image.width / image.height
    width = 13.333
    height = width / ratio
    return ReferenceDeckMeasurement(
        source_name=source_name,
        source_format=source_format,
        source_sha256=source_hash,
        slide_width=round(width, 4),
        slide_height=round(height, 4),
        slide_count=len(image_paths),
        aspect_ratio=round(ratio, 4),
    )


class ReferenceDeckAnalyzer:
    """Analyze a reference when present; no-reference generation bypasses this class."""

    def analyze(
        self,
        source: str | Path | Iterable[str | Path],
        *,
        output_dir: str | Path | None = None,
        profile_id: str = "custom-reference",
        template_mode: TemplateMode = TemplateMode.BRAND_PRESERVE_OPTIMIZE,
        explicit_hard_constraints: Iterable[str] = (),
        quality_floor: DesignQualityFloor | None = None,
        generated_for_request: str = "",
        variation_seed: int = 0,
    ) -> ReferenceLearningResult:
        if isinstance(source, (str, Path)):
            source_path = Path(source).expanduser().resolve()
            sources = [source_path]
        else:
            sources = [Path(item).expanduser().resolve() for item in source]
            if not sources:
                raise ValueError("reference image analysis requires at least one input")
            source_path = sources[0]
        if any(not path.is_file() for path in sources):
            missing = next(path for path in sources if not path.is_file())
            raise FileNotFoundError(missing)

        suffix = source_path.suffix.casefold()
        with TemporaryDirectory(prefix="lantai-reference-") if output_dir is None else _null_directory(output_dir) as raw_dir:
            destination = Path(raw_dir).resolve()
            destination.mkdir(parents=True, exist_ok=True)
            if len(sources) == 1 and suffix == ".pptx":
                measurement = PptxObjectParser().parse(source_path)
                rendered = render_pptx(source_path, destination / "render")
                image_paths = list(rendered.slide_images)
            elif len(sources) == 1 and suffix == ".pdf":
                image_paths = list(
                    render_pdf_pages(source_path, destination / "render", dpi=144, prefix="slide-"),
                )
                measurement = _visual_only_measurement(
                    source_path.name,
                    "pdf",
                    sha256(source_path.read_bytes()).hexdigest(),
                    image_paths,
                )
            else:
                supported = {".png", ".jpg", ".jpeg", ".webp"}
                if any(path.suffix.casefold() not in supported for path in sources):
                    raise ValueError("reference images must be PNG, JPEG or WebP")
                image_paths = sources
                measurement = _visual_only_measurement(
                    sources[0].name if len(sources) == 1 else f"{len(sources)} reference images",
                    "images",
                    _combined_image_hash(sources),
                    image_paths,
                )
            visual = RenderedSlideAnalyzer().analyze(image_paths)
            return ReferenceProfileBuilder().build(
                measurement,
                visual,
                profile_id=profile_id,
                template_mode=template_mode,
                explicit_hard_constraints=tuple(str(item) for item in explicit_hard_constraints),
                quality_floor=quality_floor,
                generated_for_request=generated_for_request,
                variation_seed=variation_seed,
                render_paths=(
                    tuple(str(path) for path in image_paths)
                    if output_dir is not None
                    else ()
                ),
            )


class _null_directory:
    """Small context manager matching TemporaryDirectory without deleting output."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path).expanduser())

    def __enter__(self) -> str:
        return self.path

    def __exit__(self, *_args: object) -> None:
        return None
