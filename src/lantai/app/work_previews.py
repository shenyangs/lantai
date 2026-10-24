# -*- coding: utf-8 -*-
"""Digest-bound, non-executable previews for managed Works outcomes."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from lantai.presentation.pdf_render import render_pdf_pages
from lantai.presentation.render import (
    presentation_renderer_version,
    render_pptx,
)
from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    write_text_atomic_no_follow,
)

from .storage_lock import exclusive_path_lock
from .works import get_managed_work_file, get_work_state


class WorkPreviewPage(BaseModel):
    number: int = Field(ge=1)
    file_name: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class WorkPreview(BaseModel):
    schema_version: int = 2
    work_id: str
    source_digest: str
    kind: str
    renderer: str
    renderer_version: str
    page_count: int = Field(ge=1)
    pages: tuple[WorkPreviewPage, ...]


def _digest_cache_key(source_digest: str) -> str:
    prefix, separator, value = source_digest.partition(":")
    if separator and prefix == "sha256" and len(value) == 64:
        try:
            int(value, 16)
        except ValueError:
            pass
        else:
            return value
    return hashlib.sha256(source_digest.encode("utf-8")).hexdigest()


def _preview_renderer_identity(kind: str) -> tuple[str, str]:
    if kind == "presentation":
        return (
            "libreoffice+pdfium",
            f"{presentation_renderer_version()}; profile=pptx-png-144dpi-v1",
        )
    try:
        pdfium_version = version("pypdfium2")
    except PackageNotFoundError:
        pdfium_version = "unknown"
    return "pdfium", f"pypdfium2 {pdfium_version}; profile=pdf-png-120dpi-1800px-v1"


def _preview_directory(
    workspace_dir: str | Path,
    work_id: str,
    source_digest: str,
    renderer: str,
    renderer_version: str,
) -> Path:
    renderer_key = hashlib.sha256(
        f"{renderer}\n{renderer_version}".encode("utf-8")
    ).hexdigest()
    return contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "previews",
        work_id,
        _digest_cache_key(source_digest),
        renderer_key,
    )


def _load_cached_preview(directory: Path) -> WorkPreview | None:
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        preview = WorkPreview.model_validate_json(read_text_no_follow(manifest))
    except Exception:
        return None
    for page in preview.pages:
        candidate = (directory / page.file_name).resolve(strict=False)
        if candidate.parent != directory.resolve(strict=False) or not candidate.is_file():
            return None
    return preview


def get_or_create_work_preview(
    workspace_dir: str | Path, work_id: str
) -> WorkPreview:
    """Render a preview only from an intact, managed Work revision.

    This function never accepts an arbitrary path and never executes the Work.
    Its cache key is the immutable source digest, so a changed managed file is
    rejected rather than silently reusing or replacing preview evidence.
    """

    state = get_work_state(workspace_dir, work_id)
    if state is None:
        raise FileNotFoundError("work not found")
    if state.availability_status != "available":
        raise FileNotFoundError("managed work is unavailable")
    if state.integrity_status == "modified":
        raise ValueError("managed work was modified")
    if state.integrity_status != "intact" or not state.source_digest:
        raise ValueError("managed work integrity is not verified")
    if state.kind not in {"pdf", "presentation"}:
        raise ValueError(f"work format is not previewable: {state.kind}")

    managed = get_managed_work_file(workspace_dir, work_id)
    if managed is None:
        raise ValueError("managed work is not intact")
    item, source = managed
    renderer, renderer_version = _preview_renderer_identity(state.kind)
    destination = _preview_directory(
        workspace_dir,
        item.id,
        state.source_digest,
        renderer,
        renderer_version,
    )
    manifest = destination / "manifest.json"

    with exclusive_path_lock(manifest):
        cached = _load_cached_preview(destination)
        if (
            cached is not None
            and cached.work_id == item.id
            and cached.source_digest == state.source_digest
            and cached.kind == state.kind
            and cached.renderer == renderer
            and cached.renderer_version == renderer_version
        ):
            return cached

        destination.mkdir(parents=True, exist_ok=True)
        if state.kind == "pdf":
            images = render_pdf_pages(
                source,
                destination,
                dpi=120,
                prefix="page-",
                zero_pad=3,
                image_format="PNG",
                max_dim=1800,
            )
        else:
            rendered = render_pptx(source, destination)
            images = rendered.slide_images
            if (
                rendered.renderer_id != renderer
                or rendered.renderer_version != presentation_renderer_version()
            ):
                raise RuntimeError("renderer identity changed during preview generation")

        pages: list[WorkPreviewPage] = []
        for number, image_path in enumerate(images, start=1):
            image_file = Path(image_path).resolve(strict=True)
            if image_file.parent != destination.resolve(strict=False):
                raise ValueError("renderer returned a preview outside its cache")
            with Image.open(image_file) as image:
                width, height = image.size
            pages.append(
                WorkPreviewPage(
                    number=number,
                    file_name=image_file.name,
                    width=width,
                    height=height,
                )
            )
        if not pages:
            raise RuntimeError("renderer produced no preview pages")

        preview = WorkPreview(
            work_id=item.id,
            source_digest=state.source_digest,
            kind=state.kind,
            renderer=renderer,
            renderer_version=renderer_version,
            page_count=len(pages),
            pages=tuple(pages),
        )
        write_text_atomic_no_follow(
            manifest,
            json.dumps(preview.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        return preview


def get_work_preview_page(
    workspace_dir: str | Path,
    work_id: str,
    *,
    page: int,
    source_digest: str,
) -> Path:
    preview = get_or_create_work_preview(workspace_dir, work_id)
    if preview.source_digest != source_digest:
        raise ValueError("preview revision does not match the requested digest")
    selected = next((entry for entry in preview.pages if entry.number == page), None)
    if selected is None:
        raise FileNotFoundError("preview page not found")
    directory = _preview_directory(
        workspace_dir,
        work_id,
        source_digest,
        preview.renderer,
        preview.renderer_version,
    )
    candidate = (directory / selected.file_name).resolve(strict=True)
    if candidate.parent != directory.resolve(strict=False):
        raise ValueError("preview page escaped its managed cache")
    return candidate
