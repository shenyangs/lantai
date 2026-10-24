"""Crash-resistant local persistence for customization profiles."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .impact import preview_plan
from .models import (
    ChangePlan,
    CustomizationProfile,
    CustomizationState,
    ProfileScope,
    ProfileSlot,
    RevisionRecord,
    TransactionResult,
    prefixed_id,
    utc_now,
)
from .registry import CustomizationRegistry
from .resolver import resolve_effective_state


class CustomizationStoreError(RuntimeError):
    """Base class for customization persistence errors."""


class ProfileNotFoundError(CustomizationStoreError):
    pass


class RevisionConflictError(CustomizationStoreError):
    pass


def _json_bytes(value: Any) -> bytes:
    value = _jsonable(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _content_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class CustomizationStore:
    """Owns Profile, revision, slot, snapshot, LKG, and audit state."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.profiles_dir = self.root / "profiles"
        self.revisions_dir = self.root / "revisions"
        self.snapshots_dir = self.root / "snapshots"
        self.transactions_dir = self.root / "transactions"
        self.plans_dir = self.root / "plans"
        self.confirmations_dir = self.root / "confirmations"
        self.state_path = self.root / "state.json"
        self.slots_path = self.root / "slots.json"
        self.journal_path = self.root / "journal.jsonl"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover_interrupted_transactions()

    def _profile_path(self, profile_id: str) -> Path:
        if not profile_id.startswith("profile_") or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
            for char in profile_id
        ):
            raise ProfileNotFoundError(f"Invalid Profile ID: {profile_id!r}")
        return self.profiles_dir / f"{profile_id}.json"

    def _revision_path(self, profile_id: str, revision: int) -> Path:
        return self.revisions_dir / profile_id / f"{revision:08d}.json"

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return deepcopy(default)
        except json.JSONDecodeError:
            quarantine = self.root / "quarantine" / "corrupt"
            quarantine.mkdir(parents=True, exist_ok=True)
            destination = quarantine / (
                f"{path.name}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
            )
            try:
                os.replace(path, destination)
                self._append_journal(
                    {
                        "event": "corrupt_json_quarantined",
                        "source": str(path),
                        "quarantine": str(destination),
                        "created_at": utc_now().isoformat(),
                    }
                )
            except OSError:
                pass
            return deepcopy(default)
        except (OSError, TypeError) as exc:
            raise CustomizationStoreError(f"Unable to read {path.name}: {exc}") from exc

    def _recover_interrupted_transactions(self) -> None:
        """Restore before-snapshots for transactions left in prepared state."""

        if not self.transactions_dir.exists():
            return
        for transaction_path in self.transactions_dir.glob("tx_*.json"):
            payload = self._read_json(transaction_path, {})
            if not isinstance(payload, dict) or payload.get("status") != "prepared":
                continue
            transaction_id = str(payload.get("id") or "")
            profile_id = str(payload.get("profile_id") or "")
            if not transaction_id.startswith("tx_") or not profile_id.startswith(
                "profile_"
            ):
                continue
            snapshot_path = self.snapshots_dir / f"{transaction_id}.json"
            snapshot = self._read_json(snapshot_path, {})
            try:
                profile = CustomizationProfile.model_validate(snapshot)
                if profile.id != profile_id:
                    continue
                _atomic_write(self._profile_path(profile_id), profile)
                payload.update(
                    {
                        "status": "rolled_back",
                        "recovered_after_interruption": True,
                        "recovered_at": utc_now().isoformat(),
                    }
                )
                _atomic_write(transaction_path, payload)
                self._append_journal(
                    {
                        "event": "interrupted_transaction_recovered",
                        "transaction_id": transaction_id,
                        "profile_id": profile_id,
                        "created_at": utc_now().isoformat(),
                    }
                )
            except (ValueError, CustomizationStoreError):
                continue

    def get_state(self) -> CustomizationState:
        payload = self._read_json(self.state_path, {})
        return CustomizationState.model_validate(payload)

    def _save_state(self, state: CustomizationState) -> None:
        state.updated_at = utc_now()
        _atomic_write(self.state_path, state)

    def set_mode(self, mode: str) -> CustomizationState:
        with self._lock:
            state = self.get_state()
            state.mode = mode  # type: ignore[assignment]
            self._save_state(state)
            self._append_journal(
                {
                    "event": "mode_changed",
                    "mode": mode,
                    "created_at": utc_now().isoformat(),
                }
            )
            return state

    def list_slots(self) -> list[ProfileSlot]:
        payload = self._read_json(self.slots_path, [])
        existing: dict[int, ProfileSlot] = {}
        if isinstance(payload, list):
            for value in payload:
                try:
                    slot = ProfileSlot.model_validate(value)
                except (TypeError, ValueError):
                    continue
                existing[slot.index] = slot
        return [existing.get(index, ProfileSlot(index=index)) for index in range(1, 6)]

    def set_slot(
        self,
        index: int,
        *,
        profile_id: str | None,
        label: str | None = None,
        icon: str | None = None,
        color: str | None = None,
    ) -> ProfileSlot:
        with self._lock:
            if profile_id is not None:
                self.get_profile(profile_id)
            slots = {slot.index: slot for slot in self.list_slots()}
            slots[index] = ProfileSlot(
                index=index,
                profile_id=profile_id,
                label=label,
                icon=icon,
                color=color,
            )
            _atomic_write(
                self.slots_path,
                [slots[current] for current in range(1, 6)],
            )
            return slots[index]

    def create_profile(
        self,
        *,
        name: str,
        description: str = "",
        icon: str = "sparkles",
        color: str = "#4b6581",
        scope: ProfileScope | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> CustomizationProfile:
        with self._lock:
            profile = CustomizationProfile(
                id=prefixed_id("profile"),
                name=name.strip() or "我的澜台",
                description=description.strip(),
                icon=icon,
                color=color,
                scope=scope or ProfileScope(),
                overrides=deepcopy(overrides or {}),
            )
            _atomic_write(self._profile_path(profile.id), profile)
            self._append_journal(
                {
                    "event": "profile_created",
                    "profile_id": profile.id,
                    "revision": 0,
                    "created_at": profile.created_at.isoformat(),
                }
            )
            return profile

    def list_profiles(self) -> list[CustomizationProfile]:
        if not self.profiles_dir.exists():
            return []
        profiles: list[CustomizationProfile] = []
        for path in sorted(self.profiles_dir.glob("profile_*.json")):
            try:
                profiles.append(
                    CustomizationProfile.model_validate(
                        self._read_json(path, {}),
                    )
                )
            except (CustomizationStoreError, ValueError):
                continue
        return sorted(profiles, key=lambda profile: profile.updated_at, reverse=True)

    def get_profile(self, profile_id: str) -> CustomizationProfile:
        path = self._profile_path(profile_id)
        if not path.exists():
            raise ProfileNotFoundError(f"Profile not found: {profile_id}")
        try:
            return CustomizationProfile.model_validate(self._read_json(path, {}))
        except ValueError as exc:
            raise ProfileNotFoundError(
                f"Profile is unavailable or corrupt: {profile_id}"
            ) from exc

    def save_profile_metadata(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
    ) -> CustomizationProfile:
        with self._lock:
            profile = self.get_profile(profile_id)
            if name is not None:
                profile.name = name.strip() or profile.name
            if description is not None:
                profile.description = description.strip()
            if icon is not None:
                profile.icon = icon
            if color is not None:
                profile.color = color
            profile.updated_at = utc_now()
            _atomic_write(self._profile_path(profile.id), profile)
            return profile

    def copy_profile(
        self,
        profile_id: str,
        *,
        name: str | None = None,
    ) -> CustomizationProfile:
        with self._lock:
            source = self.get_profile(profile_id)
            copied = self.create_profile(
                name=(name or f"{source.name} 副本")[:80],
                description=source.description,
                icon=source.icon,
                color=source.color,
                scope=source.scope.model_copy(deep=True),
                overrides=deepcopy(source.overrides),
            )
            copied.references = deepcopy(source.references)
            _atomic_write(self._profile_path(copied.id), copied)
            self._append_journal(
                {
                    "event": "profile_copied",
                    "profile_id": copied.id,
                    "source_profile_id": source.id,
                    "created_at": utc_now().isoformat(),
                }
            )
            return copied

    def activate_profile(self, profile_id: str | None) -> CustomizationState:
        with self._lock:
            if profile_id is not None:
                self.get_profile(profile_id)
            state = self.get_state()
            state.active_profile_id = profile_id
            state.mode = "normal" if profile_id else "factory"
            self._save_state(state)
            self._append_journal(
                {
                    "event": "profile_activated",
                    "profile_id": profile_id,
                    "mode": state.mode,
                    "created_at": utc_now().isoformat(),
                }
            )
            return state

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            state = self.get_state()
            if state.active_profile_id == profile_id:
                raise CustomizationStoreError(
                    "Active Profile must be deactivated before deletion"
                )
            path = self._profile_path(profile_id)
            if not path.exists():
                raise ProfileNotFoundError(f"Profile not found: {profile_id}")
            path.unlink()
            slots = self.list_slots()
            changed = False
            for slot in slots:
                if slot.profile_id == profile_id:
                    slot.profile_id = None
                    changed = True
            if changed:
                _atomic_write(self.slots_path, slots)
            revision_dir = self.revisions_dir / profile_id
            if revision_dir.exists():
                for revision_path in revision_dir.glob("*.json"):
                    revision_path.unlink(missing_ok=True)
                try:
                    revision_dir.rmdir()
                except OSError:
                    pass
            for transaction_path in self.transactions_dir.glob("tx_*.json"):
                transaction = self._read_json(transaction_path, {})
                if not isinstance(transaction, dict):
                    continue
                if transaction.get("profile_id") != profile_id:
                    continue
                transaction_id = str(transaction.get("id") or "")
                transaction_path.unlink(missing_ok=True)
                if transaction_id.startswith("tx_"):
                    (self.snapshots_dir / f"{transaction_id}.json").unlink(
                        missing_ok=True
                    )
            for plan_path in self.plans_dir.glob("plan_*.json"):
                plan_payload = self._read_json(plan_path, {})
                if (
                    isinstance(plan_payload, dict)
                    and isinstance(plan_payload.get("plan"), dict)
                    and plan_payload["plan"].get("target_profile_id") == profile_id
                ):
                    plan_path.unlink(missing_ok=True)
            for confirmation_path in self.confirmations_dir.glob("confirm_*.json"):
                receipt = self._read_json(confirmation_path, {})
                if (
                    isinstance(receipt, dict)
                    and receipt.get("profile_id") == profile_id
                ):
                    confirmation_path.unlink(missing_ok=True)
            state.last_known_good.pop(profile_id, None)
            state.pending_boot_validation.pop(profile_id, None)
            state.active_scoped_profiles = [
                item for item in state.active_scoped_profiles if item != profile_id
            ]
            self._save_state(state)
            self._append_journal(
                {
                    "event": "profile_deleted",
                    "profile_id": profile_id,
                    "created_at": utc_now().isoformat(),
                }
            )

    def list_revisions(self, profile_id: str) -> list[RevisionRecord]:
        directory = self.revisions_dir / profile_id
        if not directory.exists():
            return []
        records: list[RevisionRecord] = []
        for path in sorted(directory.glob("*.json"), reverse=True):
            try:
                records.append(
                    RevisionRecord.model_validate(self._read_json(path, {}))
                )
            except ValueError:
                continue
        return records

    def save_plan(self, plan: ChangePlan, preview: Any) -> dict[str, Any]:
        with self._lock:
            payload = {
                "plan": plan.model_dump(mode="json"),
                "preview": preview.model_dump(mode="json"),
                "plan_hash": _content_hash(plan),
                "created_at": utc_now().isoformat(),
            }
            _atomic_write(self.plans_dir / f"{plan.id}.json", payload)
            return payload

    def get_saved_plan(self, plan_id: str) -> tuple[ChangePlan, dict[str, Any]]:
        if not plan_id.startswith("plan_"):
            raise CustomizationStoreError("Invalid Change Plan ID")
        path = self.plans_dir / f"{plan_id}.json"
        if not path.exists():
            raise CustomizationStoreError(f"Change Plan not found: {plan_id}")
        payload = self._read_json(path, {})
        return ChangePlan.model_validate(payload.get("plan")), payload

    def confirm_plan(self, plan_id: str, *, actor: str) -> dict[str, Any]:
        with self._lock:
            plan, payload = self.get_saved_plan(plan_id)
            profile = self.get_profile(plan.target_profile_id)
            if profile.revision != plan.base_revision:
                raise RevisionConflictError(
                    "Profile changed after the preview; generate a new plan"
                )
            token = prefixed_id("confirm")
            expires_at = utc_now() + timedelta(minutes=10)
            receipt = {
                "token": token,
                "plan_id": plan.id,
                "plan_hash": payload.get("plan_hash"),
                "profile_id": profile.id,
                "base_revision": profile.revision,
                "actor": actor,
                "expires_at": expires_at.isoformat(),
                "consumed": False,
            }
            _atomic_write(self.confirmations_dir / f"{token}.json", receipt)
            return receipt

    def _consume_confirmation(
        self,
        *,
        plan: ChangePlan,
        plan_hash: str,
        token: str | None,
        actor: str,
    ) -> None:
        if not token or not token.startswith("confirm_"):
            raise CustomizationStoreError("This Change Plan requires confirmation")
        path = self.confirmations_dir / f"{token}.json"
        if not path.exists():
            raise CustomizationStoreError("Confirmation receipt not found")
        receipt = self._read_json(path, {})
        try:
            expires_at = datetime.fromisoformat(str(receipt.get("expires_at")))
        except ValueError as exc:
            raise CustomizationStoreError("Invalid confirmation receipt") from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        valid = (
            receipt.get("consumed") is False
            and receipt.get("plan_id") == plan.id
            and receipt.get("plan_hash") == plan_hash
            and receipt.get("profile_id") == plan.target_profile_id
            and receipt.get("base_revision") == plan.base_revision
            and receipt.get("actor") == actor
            and expires_at >= utc_now()
        )
        if not valid:
            raise CustomizationStoreError("Confirmation receipt is stale or invalid")
        receipt["consumed"] = True
        receipt["consumed_at"] = utc_now().isoformat()
        _atomic_write(path, receipt)

    def apply_saved_plan(
        self,
        plan_id: str,
        registry: CustomizationRegistry,
        *,
        actor: str,
        confirmation_token: str | None = None,
        source: str = "api",
        health_check: Callable[[Any], dict[str, Any]] | None = None,
    ) -> TransactionResult:
        with self._lock:
            plan, payload = self.get_saved_plan(plan_id)
            profile = self.get_profile(plan.target_profile_id)
            if profile.revision != plan.base_revision:
                raise RevisionConflictError(
                    f"Profile revision changed: expected {plan.base_revision}, "
                    f"got {profile.revision}"
                )
            preview = preview_plan(profile, plan, registry)
            if preview.requires_confirmation:
                self._consume_confirmation(
                    plan=plan,
                    plan_hash=str(payload.get("plan_hash") or ""),
                    token=confirmation_token,
                    actor=actor,
                )
            return self.apply_plan(
                plan,
                registry,
                actor=actor,
                source=source,
                confirmed=True,
                health_check=health_check,
            )

    def get_effective_state(
        self,
        registry: CustomizationRegistry,
    ):
        state = self.get_state()
        if state.mode in {"factory", "safe"}:
            return resolve_effective_state(registry, [])
        profile_ids = [
            profile_id
            for profile_id in [
                state.active_profile_id,
                *state.active_scoped_profiles,
            ]
            if profile_id
        ]
        profiles: list[CustomizationProfile] = []
        for profile_id in profile_ids:
            try:
                profiles.append(self.get_profile(profile_id))
            except ProfileNotFoundError:
                continue
        return resolve_effective_state(registry, profiles)

    def apply_plan(
        self,
        plan: ChangePlan,
        registry: CustomizationRegistry,
        *,
        actor: str = "user",
        source: str = "customization",
        confirmed: bool = True,
        health_check: Callable[[Any], dict[str, Any]] | None = None,
    ) -> TransactionResult:
        with self._lock:
            profile = self.get_profile(plan.target_profile_id)
            if profile.revision != plan.base_revision:
                raise RevisionConflictError(
                    f"Profile revision changed: expected {plan.base_revision}, "
                    f"got {profile.revision}"
                )
            preview = preview_plan(profile, plan, registry)
            if preview.requires_confirmation and not confirmed:
                raise CustomizationStoreError("Change Plan requires confirmation")

            before = profile.model_copy(deep=True)
            before_hash = _content_hash(before)
            next_profile = profile.model_copy(deep=True)
            next_profile.overrides = deepcopy(preview.after)
            next_profile.revision += 1
            next_profile.updated_at = utc_now()
            after_hash = _content_hash(next_profile)
            tx_id = prefixed_id("tx")
            snapshot_path = self.snapshots_dir / f"{tx_id}.json"
            _atomic_write(snapshot_path, before)
            prepared = {
                "id": tx_id,
                "plan_id": plan.id,
                "profile_id": profile.id,
                "status": "prepared",
                "before_revision": profile.revision,
                "result_revision": next_profile.revision,
                "created_at": utc_now().isoformat(),
            }
            _atomic_write(self.transactions_dir / f"{tx_id}.json", prepared)

            _atomic_write(self._profile_path(profile.id), next_profile)
            effective = resolve_effective_state(registry, [next_profile])
            health = (
                health_check(effective)
                if health_check is not None
                else {"ok": True, "checks": ["schema"]}
            )
            ok = bool(health.get("ok"))
            status = "committed" if ok else "rolled_back"
            record = RevisionRecord(
                profile_id=profile.id,
                revision=next_profile.revision,
                parent_revision=profile.revision,
                actor=actor,
                source=source,
                intent=plan.intent,
                plan_id=plan.id,
                before_hash=before_hash,
                after_hash=after_hash,
                risk=preview.risk,
                before_overrides=deepcopy(before.overrides),
                overrides=deepcopy(next_profile.overrides),
                health=health,
                status=status,
            )
            _atomic_write(
                self._revision_path(profile.id, next_profile.revision),
                record,
            )
            if not ok:
                _atomic_write(self._profile_path(profile.id), before)
            else:
                state = self.get_state()
                state.last_known_good.setdefault(profile.id, profile.revision)
                state.pending_boot_validation[profile.id] = next_profile.revision
                self._save_state(state)

            result = TransactionResult(
                id=tx_id,
                plan_id=plan.id,
                profile_id=profile.id,
                before_revision=profile.revision,
                result_revision=next_profile.revision if ok else profile.revision,
                status=status,
                health=health,
                warnings=[*preview.warnings, *effective.warnings],
            )
            _atomic_write(self.transactions_dir / f"{tx_id}.json", result)
            self._append_journal(
                {
                    "event": "transaction",
                    "transaction_id": tx_id,
                    "plan_id": plan.id,
                    "profile_id": profile.id,
                    "status": status,
                    "before_revision": profile.revision,
                    "result_revision": result.result_revision,
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "actor": actor,
                    "source": source,
                    "risk": preview.risk,
                    "created_at": utc_now().isoformat(),
                }
            )
            return result

    def accept_boot_health(self) -> CustomizationState:
        """Promote only revisions that survived a complete desktop boot."""

        with self._lock:
            state = self.get_state()
            promoted: dict[str, int] = {}
            for profile_id, revision in list(state.pending_boot_validation.items()):
                try:
                    profile = self.get_profile(profile_id)
                except ProfileNotFoundError:
                    state.pending_boot_validation.pop(profile_id, None)
                    continue
                if profile.revision == revision:
                    state.last_known_good[profile_id] = revision
                    promoted[profile_id] = revision
                state.pending_boot_validation.pop(profile_id, None)
            self._save_state(state)
            if promoted:
                self._append_journal(
                    {
                        "event": "boot_health_accepted",
                        "promoted": promoted,
                        "created_at": utc_now().isoformat(),
                    }
                )
            return state

    def restore_revision(
        self,
        profile_id: str,
        revision: int,
        registry: CustomizationRegistry,
        *,
        actor: str = "user",
    ) -> TransactionResult:
        record_path = self._revision_path(profile_id, revision)
        target_overrides: dict[str, Any]
        if record_path.exists():
            record = RevisionRecord.model_validate(self._read_json(record_path, {}))
            target_overrides = record.overrides
        elif revision == 0:
            revisions = list(reversed(self.list_revisions(profile_id)))
            origin = next(
                (record for record in revisions if record.parent_revision == 0),
                None,
            )
            if origin is None:
                raise CustomizationStoreError(
                    f"Revision {revision} not found for {profile_id}"
                )
            target_overrides = origin.before_overrides
        else:
            raise CustomizationStoreError(
                f"Revision {revision} not found for {profile_id}"
            )
        current = self.get_profile(profile_id)
        operations = []
        from .models import ChangeOperation

        for item_id in sorted(set(current.overrides) | set(target_overrides)):
            if item_id in target_overrides:
                operations.append(
                    ChangeOperation(
                        op="set",
                        path=item_id,
                        value=target_overrides[item_id],
                    )
                )
            else:
                operations.append(ChangeOperation(op="unset", path=item_id))
        if not operations:
            raise CustomizationStoreError("Profile already matches this revision")
        return self.apply_plan(
            ChangePlan(
                target_profile_id=profile_id,
                base_revision=current.revision,
                scope=current.scope,
                intent=f"恢复到 revision {revision}",
                operations=operations,
            ),
            registry,
            actor=actor,
            source="revision_restore",
        )

    def undo_last(
        self,
        profile_id: str,
        registry: CustomizationRegistry,
        *,
        actor: str = "user",
    ) -> TransactionResult:
        current = self.get_profile(profile_id)
        if current.revision <= 0:
            raise CustomizationStoreError("This Profile has no change to undo")
        current_record_path = self._revision_path(profile_id, current.revision)
        if not current_record_path.exists():
            raise CustomizationStoreError("Current revision audit record is missing")
        current_record = RevisionRecord.model_validate(
            self._read_json(current_record_path, {})
        )
        target_revision = current_record.parent_revision
        if target_revision is None:
            raise CustomizationStoreError("Current revision has no parent")
        return self.restore_revision(
            profile_id,
            target_revision,
            registry,
            actor=actor,
        )

    def restore_last_known_good(
        self,
        profile_id: str,
        registry: CustomizationRegistry,
    ) -> TransactionResult:
        revision = self.get_state().last_known_good.get(profile_id)
        if revision is None:
            raise CustomizationStoreError("No Last Known Good revision exists")
        return self.restore_revision(profile_id, revision, registry, actor="system")

    def _append_journal(self, payload: dict[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
