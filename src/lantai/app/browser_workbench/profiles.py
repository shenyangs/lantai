# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any

from ...constant import WORKING_DIR
from ...runtime.sanitizer import sanitize_runtime_payload
from .models import BrowserProfile, BrowserProfilePatch, utc_now_iso
from .profile_ids import canonicalize_profile_id


class BrowserProfileService:
    """Persistent named profiles for the browser workbench."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.root_dir = Path(root_dir or WORKING_DIR) / "browser_workbench"
        self.profile_root = self.root_dir / "profiles"
        self.store_path = self.root_dir / "profiles.json"
        self._lock = threading.Lock()

    def list_profiles(self) -> list[BrowserProfile]:
        with self._lock:
            profiles = self._load()
            if "default" not in profiles:
                profiles["default"] = self._default_profile()
                self._save(profiles)
            return sorted(profiles.values(), key=lambda item: item.created_at)

    def get_profile(self, profile_id: str = "default") -> BrowserProfile | None:
        profiles = {profile.id: profile for profile in self.list_profiles()}
        return profiles.get(self._normalize_id(profile_id))

    def require_profile(self, profile_id: str = "default") -> BrowserProfile:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise KeyError(f"Browser profile not found: {profile_id}")
        return profile

    def upsert_profile(
        self,
        profile: BrowserProfile | dict[str, Any],
    ) -> BrowserProfile:
        with self._lock:
            profiles = self._load()
            item = (
                profile
                if isinstance(profile, BrowserProfile)
                else BrowserProfile.model_validate(profile)
            )
            item = self._prepare_profile_for_storage(item)
            item.user_data_dir = item.user_data_dir or self.profile_data_dir(item.id)
            now = utc_now_iso()
            if item.id in profiles:
                item.created_at = profiles[item.id].created_at
            item.updated_at = now
            profiles[item.id] = item
            self._save(profiles)
            return item

    def patch_profile(
        self,
        profile_id: str,
        patch: BrowserProfilePatch,
    ) -> BrowserProfile:
        with self._lock:
            profiles = self._load()
            key = self._normalize_id(profile_id)
            if key not in profiles:
                if key == "default":
                    profiles[key] = self._default_profile()
                else:
                    raise KeyError(f"Browser profile not found: {profile_id}")
            current = profiles[key]
            data = current.model_dump(mode="json")
            patch_data = {
                key: value
                for key, value in patch.model_dump(exclude_unset=True).items()
                if value is not None
            }
            data.update(patch_data)
            data["updated_at"] = utc_now_iso()
            data["user_data_dir"] = data.get("user_data_dir") or self.profile_data_dir(
                key,
            )
            updated = BrowserProfile.model_validate(data)
            updated = self._prepare_profile_for_storage(updated)
            profiles[key] = updated
            self._save(profiles)
            return updated

    def delete_profile(self, profile_id: str) -> bool:
        key = self._normalize_id(profile_id)
        if key == "default":
            raise ValueError("default browser profile cannot be deleted")
        with self._lock:
            profiles = self._load()
            removed = profiles.pop(key, None) is not None
            if removed:
                self._save(profiles)
            return removed

    def delete_profile_storage_dir(self, profile_id: str) -> bool:
        key = self._normalize_id(profile_id)
        if key == "default":
            raise ValueError("default browser profile cannot be deleted")
        root = self.profile_root.resolve()
        target = (self.profile_root / key).resolve()
        if root == target or root not in target.parents:
            raise ValueError("browser profile storage path is outside profile root")
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True

    def normalize_profile_id(self, profile_id: str) -> str:
        return self._normalize_id(profile_id)

    def touch_profile(self, profile_id: str) -> None:
        with self._lock:
            profiles = self._load()
            key = self._normalize_id(profile_id)
            if key in profiles:
                profiles[key].last_used_at = utc_now_iso()
                profiles[key].updated_at = utc_now_iso()
                self._save(profiles)

    def profile_data_dir(self, profile_id: str) -> str:
        return str(self.profile_root / self._normalize_id(profile_id) / "user_data")

    def _default_profile(self) -> BrowserProfile:
        return BrowserProfile(
            id="default",
            name="Default",
            description="Default Lantai browser profile.",
            allowed_domains=[],
            agent_control_enabled=False,
            risk_policy="write",
            user_data_dir=self.profile_data_dir("default"),
        )

    def _load(self) -> dict[str, BrowserProfile]:
        if not self.store_path.is_file():
            return {}
        try:
            raw = json.loads(self.store_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        items = raw.get("profiles", raw if isinstance(raw, dict) else {})
        if not isinstance(items, dict):
            return {}
        profiles: dict[str, BrowserProfile] = {}
        for key, value in items.items():
            if not isinstance(value, dict):
                continue
            profile = self._profile_from_storage(key, value)
            if profile is not None and profile.id not in profiles:
                profiles[profile.id] = profile
        return profiles

    def _save(self, profiles: dict[str, BrowserProfile]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.profile_root.mkdir(parents=True, exist_ok=True)
        prepared = {
            profile.id: profile
            for profile in (
                self._prepare_profile_for_storage(profile)
                for profile in profiles.values()
            )
        }
        payload = {
            "version": 1,
            "profiles": {
                key: profile.model_dump(mode="json")
                for key, profile in sorted(prepared.items())
            },
        }
        self.store_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            "utf-8",
        )

    def _profile_from_storage(
        self,
        storage_key: str,
        value: dict[str, Any],
    ) -> BrowserProfile | None:
        storage_id = self._normalize_id(storage_key)
        payload_id = value.get("id")
        if payload_id is not None and self._normalize_id(str(payload_id)) != storage_id:
            return None
        try:
            profile = BrowserProfile.model_validate(
                {
                    **value,
                    "id": storage_id,
                    "metadata": self._sanitize_metadata(value.get("metadata")),
                },
            )
        except Exception:
            return None
        profile.user_data_dir = profile.user_data_dir or self.profile_data_dir(
            storage_id,
        )
        return profile

    def _prepare_profile_for_storage(
        self,
        profile: BrowserProfile,
    ) -> BrowserProfile:
        profile_id = self._normalize_id(profile.id)
        return profile.model_copy(
            update={
                "id": profile_id,
                "user_data_dir": profile.user_data_dir
                or self.profile_data_dir(profile_id),
                "metadata": self._sanitize_metadata(profile.metadata),
            },
        )

    @staticmethod
    def _sanitize_metadata(metadata: Any) -> dict[str, Any]:
        if not metadata:
            return {}
        if not isinstance(metadata, dict):
            raise ValueError("browser profile metadata must be an object")
        return sanitize_runtime_payload(dict(metadata))

    @staticmethod
    def _normalize_id(profile_id: str) -> str:
        return canonicalize_profile_id(profile_id)
