# -*- coding: utf-8 -*-
"""Optional, explicitly configured AI cover generation for Works."""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from lantai.agents.tools.image_generation import (
    _extract_generated_sources,
    _find_model,
    _image_block,
)
from lantai.config.config import ImageGenerationConfig
from lantai.providers.provider_manager import ProviderManager

from .works import WorkItem, ensure_works_index_writable, set_work_cover


def cover_prompt(work: WorkItem) -> str:
    """Build a visual-only prompt; the UI overlays the real Unicode title."""

    return (
        "Create a refined 16:10 editorial cover artwork for a digital work. "
        f"The work is titled {work.title!r}; its category is "
        f"{work.kind!r}. "
        "Summarize the subject through one clear visual metaphor, restrained "
        "composition, useful negative space, and polished lighting. Do not "
        "render any letters, words, numbers, symbols, logos, watermarks, UI, "
        "or fake document text. The application will typeset the exact title "
        "separately to guarantee readable text."
    )


async def generate_ai_cover(
    workspace_dir: str | Path,
    work: WorkItem,
    config: ImageGenerationConfig,
) -> WorkItem:
    if not config.works_cover_enabled:
        raise RuntimeError("AI Works covers are disabled")
    # Refuse before the paid provider call when the result cannot be safely
    # registered. set_work_cover repeats this check under its commit lock.
    ensure_works_index_writable(workspace_dir)
    slot = config.works_cover_model
    manager = ProviderManager.get_instance()
    provider = manager.get_provider(slot.provider_id)
    model = _find_model(provider, slot.model) if provider else None
    generator = getattr(provider, "generate_image", None) if provider else None
    if (
        provider is None
        or model is None
        or not manager.is_model_slot_available(slot)
        or not callable(generator)
    ):
        raise RuntimeError("The selected Works cover model is unavailable")

    payload = await generator(
        model_id=model.id,
        prompt=cover_prompt(work),
        n=1,
        size="1536x1024",
        quality="auto",
        response_format=None,
        timeout=float(config.timeout_seconds),
    )
    sources = _extract_generated_sources(payload)
    if not sources:
        raise RuntimeError("The selected model returned no displayable cover")

    managed_root = Path(work.path).resolve(strict=True).parent
    temporary_root = managed_root / ".cover-generation"
    temporary_root.mkdir(parents=True, exist_ok=True)
    try:
        source = sources[0]
        (
            _,
            generated_url,
            durable,
            _managed_path,
            _workspace_path,
        ) = await _image_block(
            source,
            media_dir=temporary_root,
            index=1,
            metadata={"work_id": work.id, "purpose": "works_cover"},
        )
        parsed = urlparse(generated_url)
        if not durable or (parsed.scheme and parsed.scheme != "file"):
            raise RuntimeError(
                "The generated cover could not be saved locally",
            )
        generated_path = Path(
            parsed.path if parsed.scheme == "file" else generated_url,
        )
        generated_path = generated_path.expanduser().resolve(strict=True)
        if temporary_root.resolve() not in generated_path.parents:
            raise RuntimeError(
                "The generated cover is outside managed storage",
            )
        with Image.open(generated_path) as image:
            image.verify()
            mime_type = Image.MIME.get(image.format or "", source.mime_type)
        return set_work_cover(
            workspace_dir,
            work.id,
            source_path=generated_path,
            mime_type=mime_type or "image/png",
        )
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
