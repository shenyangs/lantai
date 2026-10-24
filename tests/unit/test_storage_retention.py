from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from lantai.storage_retention import (
    clean_storage_retention,
    get_storage_retention_policy,
    update_storage_retention_policy,
)


def test_generated_images_default_to_thirty_day_retention(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({}), "utf-8")

    policy = get_storage_retention_policy(tmp_path)

    assert policy["generated_images_days"] == 30


def test_invalid_persisted_generated_image_retention_falls_back_to_default(
    tmp_path,
):
    (tmp_path / "settings.json").write_text(
        json.dumps({"storage_retention": {"generated_images_days": 14}}),
        "utf-8",
    )

    policy = get_storage_retention_policy(tmp_path)

    assert policy["generated_images_days"] == 30


def test_generated_image_retention_round_trips_through_settings_file(tmp_path):
    updated = update_storage_retention_policy(
        {"generated_images_days": 15}, root=tmp_path
    )

    reloaded = get_storage_retention_policy(tmp_path)

    assert updated["generated_images_days"] == 15
    assert reloaded["generated_images_days"] == 15


def test_generated_image_cleanup_only_removes_expired_managed_images(tmp_path):
    generated = tmp_path / "media" / "generated_images"
    generated.mkdir(parents=True)
    expired = generated / "expired.png"
    current = generated / "current.png"
    unrelated = tmp_path / "media" / "user_attachment.png"
    expired.write_bytes(b"expired")
    current.write_bytes(b"current")
    unrelated.write_bytes(b"attachment")
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
    os.utime(expired, (old_timestamp, old_timestamp))
    (tmp_path / "settings.json").write_text(
        json.dumps({"storage_retention": {"generated_images_days": 30}}),
        "utf-8",
    )

    result = clean_storage_retention(dry_run=False, root=tmp_path)

    assert result["generated_images"]["files_removed"] == 1
    assert not expired.exists()
    assert current.exists()
    assert unrelated.exists()


def test_generated_image_cleanup_covers_task_workspace_staging_dir(tmp_path):
    generated = (
        tmp_path
        / "workspaces"
        / "default"
        / "topic-workspaces"
        / "topic-1"
        / ".lantai"
        / "generated_images"
    )
    generated.mkdir(parents=True)
    expired = generated / "expired.png"
    unrelated = generated.parent.parent / "notes.md"
    expired.write_bytes(b"expired")
    unrelated.write_text("keep", "utf-8")
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
    os.utime(expired, (old_timestamp, old_timestamp))
    os.utime(unrelated, (old_timestamp, old_timestamp))
    (tmp_path / "settings.json").write_text(
        json.dumps({"storage_retention": {"generated_images_days": 30}}),
        "utf-8",
    )

    result = clean_storage_retention(dry_run=False, root=tmp_path)

    assert result["generated_images"]["files_removed"] == 1
    assert not expired.exists()
    assert unrelated.exists()


def test_permanent_generated_image_retention_disables_cleanup(tmp_path):
    generated = tmp_path / "media" / "generated_images"
    generated.mkdir(parents=True)
    expired = generated / "old.png"
    expired.write_bytes(b"old")
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=365)).timestamp()
    os.utime(expired, (old_timestamp, old_timestamp))
    (tmp_path / "settings.json").write_text(
        json.dumps({"storage_retention": {"generated_images_days": 0}}),
        "utf-8",
    )

    result = clean_storage_retention(dry_run=False, root=tmp_path)

    assert result["generated_images"]["files_removed"] == 0
    assert expired.exists()
