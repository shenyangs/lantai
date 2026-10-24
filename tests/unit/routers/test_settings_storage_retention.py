from __future__ import annotations

import pytest
from fastapi import HTTPException

from lantai.app.routers import settings


@pytest.mark.asyncio
async def test_generated_image_retention_accepts_only_user_facing_choices(monkeypatch):
    saved: list[dict[str, int]] = []

    def fake_update(patch):
        saved.append(patch)
        return patch

    monkeypatch.setattr(settings, "update_storage_retention_policy", fake_update)

    for days in (0, 3, 7, 15, 30):
        result = await settings.put_storage_retention(
            {"generated_images_days": days}
        )
        assert result == {"generated_images_days": days}

    assert saved == [
        {"generated_images_days": 0},
        {"generated_images_days": 3},
        {"generated_images_days": 7},
        {"generated_images_days": 15},
        {"generated_images_days": 30},
    ]

    with pytest.raises(HTTPException) as error:
        await settings.put_storage_retention({"generated_images_days": 14})

    assert error.value.status_code == 400
