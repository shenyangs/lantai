# -*- coding: utf-8 -*-
"""Safe synchronization for user-owned persona workspace templates."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PersonaWorkspaceSyncResult:
    """Observable result of a non-destructive persona template sync."""

    copied: tuple[str, ...]
    upgraded: tuple[str, ...]
    preserved: tuple[str, ...]
    backups: tuple[str, ...]


@dataclass(frozen=True)
class PersonaWorkspaceSyncPlanItem:
    """One read-only decision in a persona workspace migration plan."""

    filename: str
    action: str
    reason: str
    source_sha256: str
    destination_sha256: str | None
    target_version: str | None
    backup_name: str | None


@dataclass(frozen=True)
class PersonaWorkspaceRollbackResult:
    """Result of an explicit, compare-before-restore template rollback."""

    restored: bool
    reason: str


_TEMPLATE_VERSION_RE = re.compile(
    r'^(?:persona_version|template_version):\s*["\']?([^"\'\s]+)["\']?\s*$',
    re.MULTILINE,
)

# Exact hashes of official, user-unmodified templates from earlier releases.
# Only these files may be replaced automatically.
# A single user edit changes the hash and therefore keeps the file user-owned.
_LEGACY_OFFICIAL_TEMPLATE_SHA256: dict[str, dict[str, frozenset[str]]] = {
    "zh": {
        "AGENTS.md": frozenset(
            {
                "0bc4d61fe89c52673ef846f810f586a3681527866015712d7049a318f3fc7ebe",
                "024bba380908185d29cb731f45f93430d804f80ede487281af9b237c68501c72",
                "4e969bf4260e2533f244aa4422491d9eb29632fc9652da1b063bf80728a451ab",
            },
        ),
        "SOUL.md": frozenset(
            {
                "0f34d7a2be41ff774070fc6ddbb0fdc8e7e6887eef9d2f437a0ea5a4cfb73e29",
                "345b8a379bab2c8f004452e03cdc51a2a469834c0b7345da3f79288c1a600207",
                "951c6bb27d422890b9f40d5452035df77fed53756474aac0e6f93cb884456f54",
                "51e1be7b0cdfe64f838351367cee1ee902cc456df11eaaebf7b67e98b8c5e19b",
            },
        ),
        "PROFILE.md": frozenset(
            {
                "dd0f9b36e37dd5d942db6ef6aaeff472d9e75395d15c50b37978517c825ff9f5",
                "9419ebe165c953c4279352d616c4f95200b58b083ed3a24b0db715dc079fb8c9",
                "06a609b8645bc0495ece008ad25d1d6837db1072e19930b5039b316fc5a839a3",
            },
        ),
        "BACKSTORY.md": frozenset(
            {
                "03da08f9a63bb18af61a7d621d4d83f056fe427b8f5505aef6b13de39ca3ed35",
                "1a12baf081a4905afaa70f67b3fda16d3cb25b3ac1f5690ef01711a7284a7699",
                "7d7c6b11051d7359c5ad621f6ab60f298df21ad026faf4472b969975d52caa0b",
            },
        ),
        "WORKBENCH.md": frozenset(
            {
                "bc8699cab3b44685ccaf1f846135c3649978eda12f323e1c66dda7318765b226",
                "6864180be98970f44bc853e949b3bf7f1244c50985d2bd47be062e28b8f56253",
                "c9d7293b3f6da34c5319536bf503abafa0ddaee69ab681a045081bb5dcf89a08",
                "a74d29b83983a67698368843ee899139a48554787368c0bbb98cc344c960abf3",
            },
        ),
        "SELF_CANON.md": frozenset(
            {
                "207b00a353631d34039520071db496235342b26f990525afa4d2b9839b20d927",
                "e3b1a8cb9b90c2a4354288e2bda6adddfc995dd935527d4d68677ac4bdd0808a",
            },
        ),
        "VISUAL_CANON.md": frozenset(
            {
                "2e9e478abd6a796cdc08427e5001d32a44e821b9ed754b0b3e0ff8cb383626df",
                "0e48d4381fd2130604c451e945b778a7c81fb67b3806bb8edb3077ecaa782fde",
            },
        ),
        "PRODUCT_GUIDE.md": frozenset(
            {"0edde0860512a4d499404cf59a0a720de7f75158ed7692cd1b45372a3e662937"},
        ),
    },
    "en": {
        "AGENTS.md": frozenset(
            {
                "b8526bdcadd6e7270afc7020229fe5eb34bed7a6e9f3bb1b23d5145289d7ddcb",
                "925fb6474bf2fefda1e5118729a954e20ce1ab78e3c4bc65dc7ddeaf3681da1f",
                "1d6c99499baff4857519cbcec9ab0fb246214dd12d99db065ae5eae9249d1572",
            },
        ),
        "SOUL.md": frozenset(
            {
                "2efe349466b3622ab2cc62c0a82c2eaa6c67aa11341bcb7b6c97d9c6162b3c90",
                "7b947dfd9d19a75e6ace34051ab35331e528d0026b5adf4ace8c4cb0bd0ed7d0",
                "27b028a0519ee447cadad9c38a4e6f8e30dec2bca818b9e5200e95ea22fa383d",
                "5cb2e8623cb38d78bd2f723b7c9f4b5a54f11fa969339c38f6ec158066070b10",
            },
        ),
        "PROFILE.md": frozenset(
            {
                "904df69c1f80f2abe69982eaaa0518942ad2430b9ff483af772a13f583ce91f7",
                "291a786a741c340b48d78ca06173246cb2cedef412252dda608a8ed59663057b",
                "4174b74a89ef70b8718adab05e707fd0f57d14223a2d30dbda50277f373003bc",
            },
        ),
        "BACKSTORY.md": frozenset(
            {
                "4d4724cd9fafdbff191fccb176a37a4ea3046741f1f8266027bf2975b1fb6998",
                "a8fd0b9516b84605191761a222928fe289dab62a8a60e391e6d4eeeba6eab86d",
            },
        ),
        "WORKBENCH.md": frozenset(
            {
                "212403ee61b0d27da97646adfd30f4f030d0f66ce4e67b0c267a46c307baaba6",
                "d832bef3756a5cbd710857eb4eba1ef1311cc998773946e64af2c64d25a0dab8",
            },
        ),
        "SELF_CANON.md": frozenset(
            {
                "f691055aa20d77fa389a762434be9342133e439e8da20366457f942c515a2dfc",
                "2bf9ee84e99129eb77055d099b9dec6b528dc0946e7c45634fb06635f382e0ce",
            },
        ),
        "VISUAL_CANON.md": frozenset(
            {
                "072139a00502812a5c9ce53ec9ae8b8f343df765c477dac3708e3ea0dc3e0e63",
                "9180c0dce600c18dba7a467bee661eab138f51ee119790532f93f10cd1352014",
            },
        ),
    },
}


def _persona_version(content: str) -> str | None:
    match = _TEMPLATE_VERSION_RE.search(content)
    return match.group(1) if match else None


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_copy(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
    expect_missing: bool = False,
) -> bool:
    """Stage a copy, then install only if the destination still matches."""

    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if expect_missing:
            if destination.exists() or destination.is_symlink():
                return False
        elif expected_sha256 is not None:
            if (
                destination.is_symlink()
                or not destination.is_file()
                or _sha256(destination.read_bytes()) != expected_sha256
            ):
                return False
        os.replace(temporary, destination)
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_backup_once(content: bytes, destination: Path) -> bool:
    """Publish a complete immutable-by-convention backup without replacing one."""

    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def rollback_persona_workspace_template(
    workspace_dir: str | Path,
    filename: str,
    *,
    installed_sha256: str,
    backup_name: str,
) -> PersonaWorkspaceRollbackResult:
    """Explicitly restore one backup only if the installed file is unchanged."""

    workspace = Path(workspace_dir)
    if Path(filename).name != filename or Path(backup_name).name != backup_name:
        return PersonaWorkspaceRollbackResult(False, "invalid-name")
    destination = workspace / filename
    backup = workspace / backup_name
    if (
        destination.is_symlink()
        or backup.is_symlink()
        or not destination.is_file()
        or not backup.is_file()
    ):
        return PersonaWorkspaceRollbackResult(False, "missing-or-unsafe-file")
    if _sha256(destination.read_bytes()) != installed_sha256:
        return PersonaWorkspaceRollbackResult(False, "destination-changed")
    if not _atomic_copy(
        backup,
        destination,
        expected_sha256=installed_sha256,
    ):
        return PersonaWorkspaceRollbackResult(False, "destination-changed")
    return PersonaWorkspaceRollbackResult(True, "restored")


def plan_persona_workspace_templates(
    source_dir: str | Path,
    workspace_dir: str | Path,
) -> tuple[PersonaWorkspaceSyncPlanItem, ...]:
    """Classify template changes without creating or modifying workspace files."""

    source = Path(source_dir)
    workspace = Path(workspace_dir)
    language = source.name
    known_hashes = _LEGACY_OFFICIAL_TEMPLATE_SHA256.get(language, {})
    plan: list[PersonaWorkspaceSyncPlanItem] = []

    for template in sorted(source.glob("*.md"), key=lambda path: path.name):
        source_bytes = template.read_bytes()
        source_sha256 = _sha256(source_bytes)
        packaged_content = source_bytes.decode("utf-8", errors="replace")
        target_version = _persona_version(packaged_content)
        destination = workspace / template.name

        if not destination.exists() and not destination.is_symlink():
            plan.append(
                PersonaWorkspaceSyncPlanItem(
                    filename=template.name,
                    action="copy",
                    reason="missing",
                    source_sha256=source_sha256,
                    destination_sha256=None,
                    target_version=target_version,
                    backup_name=None,
                ),
            )
            continue

        if destination.is_symlink() or not destination.is_file():
            plan.append(
                PersonaWorkspaceSyncPlanItem(
                    filename=template.name,
                    action="preserve",
                    reason="unsafe-destination",
                    source_sha256=source_sha256,
                    destination_sha256=None,
                    target_version=target_version,
                    backup_name=None,
                ),
            )
            continue

        existing_bytes = destination.read_bytes()
        existing_sha256 = _sha256(existing_bytes)
        existing_version = _persona_version(
            existing_bytes.decode("utf-8", errors="replace"),
        )
        eligible_hashes = known_hashes.get(template.name, frozenset())
        if (
            target_version
            and existing_version != target_version
            and existing_sha256 in eligible_hashes
        ):
            safe_version = re.sub(r"[^0-9A-Za-z._-]", "_", target_version)
            version_kind = (
                "template" if "template_version:" in packaged_content else "persona"
            )
            plan.append(
                PersonaWorkspaceSyncPlanItem(
                    filename=template.name,
                    action="upgrade",
                    reason="known-official-template",
                    source_sha256=source_sha256,
                    destination_sha256=existing_sha256,
                    target_version=target_version,
                    backup_name=(
                        f".{template.name}.pre-{version_kind}-{safe_version}.bak"
                    ),
                ),
            )
            continue

        plan.append(
            PersonaWorkspaceSyncPlanItem(
                filename=template.name,
                action="preserve",
                reason="current-or-user-owned",
                source_sha256=source_sha256,
                destination_sha256=existing_sha256,
                target_version=target_version,
                backup_name=None,
            ),
        )

    return tuple(plan)


def sync_persona_workspace_templates(
    source_dir: str | Path,
    workspace_dir: str | Path,
) -> PersonaWorkspaceSyncResult:
    """Fill missing markdown templates without overwriting user-owned files.

    The protected Shen Xingzhi constitution is loaded from package resources,
    not from these mutable workspace files. Existing workspace prose can
    therefore evolve under user control without weakening the built-in core.

    Versioned packaged templates may replace only byte-for-byte known official
    legacy templates. Every other existing file is preserved as user-owned.
    Before an official legacy template is upgraded, its exact bytes are kept
    in a hidden adjacent backup.
    """

    source = Path(source_dir)
    workspace = Path(workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    upgraded: list[str] = []
    preserved: list[str] = []
    backups: list[str] = []
    plan = plan_persona_workspace_templates(source, workspace)

    for item in plan:
        template = source / item.filename
        destination = workspace / item.filename
        if _sha256(template.read_bytes()) != item.source_sha256:
            preserved.append(item.filename)
            continue
        if item.action == "preserve":
            preserved.append(item.filename)
            continue
        if item.action == "copy":
            if _atomic_copy(template, destination, expect_missing=True):
                copied.append(item.filename)
            else:
                preserved.append(item.filename)
            continue

        if (
            item.action != "upgrade"
            or item.destination_sha256 is None
            or item.backup_name is None
            or destination.is_symlink()
            or not destination.is_file()
        ):
            preserved.append(item.filename)
            continue
        existing_bytes = destination.read_bytes()
        if _sha256(existing_bytes) != item.destination_sha256:
            preserved.append(item.filename)
            continue
        backup = workspace / item.backup_name
        if _write_backup_once(existing_bytes, backup):
            backups.append(backup.name)
        elif (
            backup.is_symlink()
            or not backup.is_file()
            or _sha256(backup.read_bytes()) != item.destination_sha256
        ):
            preserved.append(item.filename)
            continue
        if _atomic_copy(
            template,
            destination,
            expected_sha256=item.destination_sha256,
        ):
            upgraded.append(item.filename)
        else:
            preserved.append(item.filename)

    return PersonaWorkspaceSyncResult(
        copied=tuple(copied),
        upgraded=tuple(upgraded),
        preserved=tuple(preserved),
        backups=tuple(backups),
    )
