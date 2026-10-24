# -*- coding: utf-8 -*-
"""Local-first asset validation for Presentation Skill v3.

The composer never downloads or uploads an asset.  Every referenced image is
resolved from the DeckIR asset catalog, hash-checked, and materialized as a
local path before a writer is allowed to run.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from .models import DeckIR, ImageObject


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PresentationAssetError(ValueError):
    """Raised when a deck would compose from an unverified visual asset."""


def resolve_deck_assets(deck: DeckIR) -> DeckIR:
    """Return a copy whose image objects point at verified local files."""

    catalog = {asset.asset_id: asset for asset in deck.assets}
    slides = []
    for slide in deck.slides:
        objects = []
        used_asset_ids = set(slide.asset_ids)
        for obj in slide.objects:
            if not isinstance(obj, ImageObject):
                objects.append(obj)
                continue
            asset_id = obj.asset_id
            if asset_id is None and obj.path:
                requested = Path(obj.path).expanduser().resolve()
                matches = [
                    asset.asset_id
                    for asset in deck.assets
                    if Path(asset.local_path).expanduser().resolve()
                    == requested
                ]
                if len(matches) != 1:
                    raise PresentationAssetError(
                        "every image path must resolve to exactly one audited asset",
                    )
                asset_id = matches[0]
            if asset_id is None:
                raise PresentationAssetError(
                    "image has no audited asset identity"
                )
            asset = catalog.get(asset_id)
            if asset is None:
                raise PresentationAssetError(
                    f"image references unknown asset: {asset_id}",
                )
            if asset.ingestion_status not in {"ready", "recovered"}:
                raise PresentationAssetError(
                    f"asset is not ready: {asset.asset_id}",
                )
            if asset.rights_status == "review":
                raise PresentationAssetError(
                    f"asset rights require review: {asset.asset_id}",
                )
            path = Path(asset.local_path).expanduser().resolve()
            if not path.is_file():
                raise PresentationAssetError(
                    f"asset file does not exist: {asset.asset_id}",
                )
            if _file_sha256(path) != asset.sha256:
                raise PresentationAssetError(
                    f"asset hash mismatch: {asset.asset_id}",
                )
            if asset.dimensions is not None and asset.kind == "image":
                try:
                    with Image.open(path) as image:
                        actual_dimensions = image.size
                except (OSError, ValueError) as exc:
                    raise PresentationAssetError(
                        f"asset image cannot be inspected: {asset.asset_id}",
                    ) from exc
                if actual_dimensions != asset.dimensions:
                    raise PresentationAssetError(
                        f"asset dimensions mismatch: {asset.asset_id}",
                    )
            used_asset_ids.add(asset.asset_id)
            objects.append(
                obj.model_copy(
                    update={
                        "asset_id": asset.asset_id,
                        "path": str(path),
                        "focal_point": obj.focal_point or asset.focal_point,
                    },
                ),
            )
        slides.append(
            slide.model_copy(
                update={
                    "objects": objects,
                    "asset_ids": sorted(used_asset_ids),
                },
            ),
        )
    return deck.model_copy(update={"slides": slides})
