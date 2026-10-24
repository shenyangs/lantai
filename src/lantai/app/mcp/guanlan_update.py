# -*- coding: utf-8 -*-
"""Trusted, manually-triggered Guanlan runtime updates for the desktop build.

This module deliberately does not invoke pip, discover another interpreter, or
download dependencies.  It accepts one pure-Python Guanlan wheel only after
PyPI's SHA-256 and Trusted Publisher attestation both verify, checks that the
already-bundled dependency set can satisfy it, and then atomically switches a
versioned runtime directory.  App Store builds do not call this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
import sys
import zipfile
from dataclasses import dataclass, replace
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

import httpx
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ...security.storage_keys import read_text_no_follow, write_text_atomic_no_follow

GUANLAN_PACKAGE = "guanlan"
GUANLAN_PYPI_JSON_URL = "https://pypi.org/pypi/guanlan/json"
GUANLAN_GITHUB_TAGS_URL = (
    "https://api.github.com/repos/shenyangs/Guanlan/tags?per_page=100"
)
GUANLAN_PYPI_INTEGRITY_URL = (
    "https://pypi.org/integrity/guanlan/{version}/{filename}/provenance"
)
GUANLAN_TRUSTED_PUBLISHER_REPOSITORY = "shenyangs/Guanlan"
GUANLAN_TRUSTED_PUBLISHER_WORKFLOW = "release-pypi.yml"
GUANLAN_TRUSTED_PUBLISHER_ENVIRONMENT = "pypi"
_MANIFEST_NAME = ".lantai-release.json"
_CURRENT_NAME = "current.json"
_MAX_WHEEL_BYTES = 20 * 1024 * 1024
_MAX_WHEEL_FILES = 10_000
_MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
_FETCH_ATTEMPTS = 3
_FETCH_RETRY_DELAYS_SECONDS = (0.25, 0.75)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_DIRECTORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class GuanlanUpdateError(RuntimeError):
    """Raised when a candidate Guanlan release cannot be safely activated."""


@dataclass(frozen=True)
class GuanlanRelease:
    """One immutable Guanlan wheel selected from PyPI metadata."""

    version: str
    filename: str
    download_url: str
    sha256: str
    requires_python: str
    size: int
    directory: str = ""


@dataclass(frozen=True)
class GuanlanUpdateResult:
    """Observable result of a user-initiated update check."""

    updated: bool
    version: str | None
    previous_version: str | None
    message: str
    candidate_version: str | None = None
    metadata_source: Literal["pypi", "github_tags", "unavailable"] = "pypi"
    metadata_degraded: bool = False
    update_available: bool = False


FetchBytes = Callable[[str], Awaitable[bytes]]
VerifyProvenance = Callable[[dict[str, Any], str, str, bool], None]


def _safe_directory_name(version: str, sha256: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", version).strip(".-")
    if not normalized:
        raise GuanlanUpdateError("PyPI returned an unsafe Guanlan version")
    return f"{normalized}-{sha256[:12]}"


def _json_object(data: bytes, *, source: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuanlanUpdateError(f"Invalid JSON from {source}") from exc
    if not isinstance(payload, dict):
        raise GuanlanUpdateError(f"Invalid JSON object from {source}")
    return payload


def _json_list(data: bytes, *, source: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuanlanUpdateError(f"Invalid JSON from {source}") from exc
    if not isinstance(payload, list):
        raise GuanlanUpdateError(f"Invalid JSON list from {source}")
    return [item for item in payload if isinstance(item, dict)]


def _read_regular_bytes(path: Path, *, max_bytes: int) -> bytes:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise GuanlanUpdateError("Stored Guanlan runtime is unavailable") from exc
    if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink():
        raise GuanlanUpdateError("Stored Guanlan runtime contains an unsafe file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GuanlanUpdateError("Stored Guanlan runtime cannot be read safely") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise GuanlanUpdateError("Stored Guanlan runtime cannot be read") from exc
    if len(data) > max_bytes:
        raise GuanlanUpdateError("Stored Guanlan wheel exceeds the size limit")
    return data


def verify_pypi_provenance(
    proof: dict[str, Any],
    filename: str,
    sha256: str,
    offline: bool,
) -> None:
    """Cryptographically verify a wheel against its pinned PyPI publisher."""

    try:
        from pypi_attestations import Distribution, GitHubPublisher, Provenance
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise GuanlanUpdateError(
            "Trusted update verifier is missing from this Lantai release",
        ) from exc

    try:
        provenance = Provenance.model_validate(proof)
        publisher = GitHubPublisher(
            repository=GUANLAN_TRUSTED_PUBLISHER_REPOSITORY,
            workflow=GUANLAN_TRUSTED_PUBLISHER_WORKFLOW,
            environment=GUANLAN_TRUSTED_PUBLISHER_ENVIRONMENT,
        )
        distribution = Distribution(name=filename, digest=sha256)
        for bundle in provenance.attestation_bundles:
            bundle_publisher = bundle.publisher
            if (
                bundle_publisher.repository
                != GUANLAN_TRUSTED_PUBLISHER_REPOSITORY
                or bundle_publisher.workflow
                != GUANLAN_TRUSTED_PUBLISHER_WORKFLOW
                or bundle_publisher.environment
                != GUANLAN_TRUSTED_PUBLISHER_ENVIRONMENT
            ):
                continue
            for attestation in bundle.attestations:
                attestation.verify(publisher, distribution, offline=offline)
                return
    except Exception as exc:  # noqa: BLE001 - fail closed on all verifier errors
        if isinstance(exc, GuanlanUpdateError):
            raise
        raise GuanlanUpdateError(
            "Guanlan release provenance verification failed",
        ) from exc

    raise GuanlanUpdateError(
        "Guanlan release is not signed by the trusted publisher",
    )


class GuanlanPackageUpdater:
    """Download, verify, and atomically activate a fixed Guanlan wheel."""

    def __init__(
        self,
        *,
        working_dir: Path,
        fetch: FetchBytes | None = None,
        verify_provenance: VerifyProvenance = verify_pypi_provenance,
    ) -> None:
        self._root = working_dir / "guanlan-runtime"
        self._fetch = fetch or self._fetch_from_pypi
        self._verify_provenance = verify_provenance

    async def update(self, *, current_version: str | None) -> GuanlanUpdateResult:
        """Activate the latest trusted wheel when it is newer than runtime."""

        try:
            release_metadata_bytes = await self._fetch(GUANLAN_PYPI_JSON_URL)
            release_metadata = _json_object(release_metadata_bytes, source="PyPI")
        except GuanlanUpdateError:
            return await self._github_tags_fallback(current_version=current_version)
        release = self._select_release(release_metadata)
        if self._is_not_newer(release.version, current_version):
            return GuanlanUpdateResult(
                updated=False,
                version=current_version,
                previous_version=current_version,
                message=f"Guanlan {current_version} is already the latest trusted release",
            )

        wheel = await self._fetch(release.download_url)
        if len(wheel) > _MAX_WHEEL_BYTES:
            raise GuanlanUpdateError("Guanlan wheel exceeds the size limit")
        actual_sha256 = hashlib.sha256(wheel).hexdigest()
        if actual_sha256 != release.sha256:
            raise GuanlanUpdateError("Guanlan wheel checksum does not match PyPI")

        proof_url = GUANLAN_PYPI_INTEGRITY_URL.format(
            version=release.version,
            filename=release.filename,
        )
        proof = _json_object(await self._fetch(proof_url), source="PyPI Integrity")
        self._verify_provenance(proof, release.filename, actual_sha256, False)
        self._validate_wheel(wheel, release)
        activated = self._activate(release, wheel, proof)
        return GuanlanUpdateResult(
            updated=True,
            version=activated.version,
            previous_version=current_version,
            message=(
                f"Guanlan updated from {current_version or 'bundled runtime'} "
                f"to {activated.version}"
            ),
        )

    async def _github_tags_fallback(
        self,
        *,
        current_version: str | None,
    ) -> GuanlanUpdateResult:
        """Report a GitHub tag only; never install from a fallback source."""

        try:
            tags = _json_list(
                await self._fetch(GUANLAN_GITHUB_TAGS_URL),
                source="GitHub Tags",
            )
            candidate_version = self._latest_github_tag_version(tags)
        except GuanlanUpdateError:
            return GuanlanUpdateResult(
                updated=False,
                version=current_version,
                previous_version=current_version,
                message=(
                    "Unable to reach PyPI or GitHub Tags to check Guanlan "
                    "updates. The current bundled Guanlan runtime remains "
                    "available."
                ),
                metadata_source="unavailable",
                metadata_degraded=True,
            )

        if candidate_version is None:
            return GuanlanUpdateResult(
                updated=False,
                version=current_version,
                previous_version=current_version,
                message=(
                    "PyPI could not be reached and GitHub Tags did not "
                    "provide a valid Guanlan release version. The current "
                    "bundled runtime remains available."
                ),
                metadata_source="github_tags",
                metadata_degraded=True,
            )

        update_available = not self._is_not_newer(
            candidate_version,
            current_version,
        )
        if update_available:
            message = (
                f"PyPI could not be reached. GitHub Tags reports Guanlan "
                f"v{candidate_version}, but Lantai only installs PyPI "
                "releases after checksum and publisher verification. The "
                "current runtime remains available; reconnect to PyPI to "
                "install the verified update."
            )
        else:
            message = (
                f"PyPI could not be reached. GitHub Tags reports Guanlan "
                f"v{candidate_version}; the current runtime remains "
                "available."
            )
        return GuanlanUpdateResult(
            updated=False,
            version=current_version,
            previous_version=current_version,
            message=message,
            candidate_version=candidate_version,
            metadata_source="github_tags",
            metadata_degraded=True,
            update_available=update_available,
        )

    @staticmethod
    def _latest_github_tag_version(tags: list[dict[str, Any]]) -> str | None:
        candidates: list[Version] = []
        for tag in tags:
            name = tag.get("name")
            if not isinstance(name, str) or not name.startswith("v"):
                continue
            try:
                version = Version(name[1:])
            except InvalidVersion:
                continue
            if version.is_prerelease or version.is_devrelease:
                continue
            candidates.append(version)
        return str(max(candidates)) if candidates else None

    def active_release(self) -> GuanlanRelease | None:
        """Return the selected fixed release without executing it."""

        pointer = self._read_json_file(self._root / _CURRENT_NAME)
        if pointer is None:
            return None
        directory = pointer.get("directory")
        if not isinstance(directory, str) or not _SAFE_DIRECTORY_PATTERN.fullmatch(
            directory,
        ):
            return None
        manifest = self._read_json_file(self._root / "versions" / directory / _MANIFEST_NAME)
        if manifest is None:
            return None
        try:
            return GuanlanRelease(
                version=str(manifest["version"]),
                filename=str(manifest["filename"]),
                download_url="",
                sha256=str(manifest["sha256"]),
                requires_python=str(manifest.get("requires_python", "")),
                size=int(manifest.get("size", 0)),
                directory=directory,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def verified_package_dir(self) -> Path | None:
        """Return a package dir only after rechecking wheel and provenance."""

        release = self.active_release()
        if release is None:
            return None
        directory = self._root / "versions" / release.directory
        if directory.is_symlink() or not directory.is_dir():
            return None
        manifest = self._read_json_file(directory / _MANIFEST_NAME)
        if manifest is None:
            return None
        proof = manifest.get("provenance")
        if not isinstance(proof, dict):
            return None
        wheel_path = directory / ".artifact" / release.filename
        try:
            wheel = _read_regular_bytes(wheel_path, max_bytes=_MAX_WHEEL_BYTES)
            if hashlib.sha256(wheel).hexdigest() != release.sha256:
                return None
            self._verify_provenance(proof, release.filename, release.sha256, True)
            self._verify_extracted_wheel(directory, wheel)
        except GuanlanUpdateError:
            return None
        return directory

    async def _fetch_from_pypi(self, url: str) -> bytes:
        timeout = httpx.Timeout(30.0, connect=10.0)
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    trust_env=False,
                    headers={"User-Agent": "Lantai-Guanlan-Updater/1"},
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()
            except httpx.TransportError as exc:
                if attempt + 1 >= _FETCH_ATTEMPTS:
                    raise GuanlanUpdateError(
                        self._download_error_message(url, exhausted_retries=True),
                    ) from exc
                await asyncio.sleep(_FETCH_RETRY_DELAYS_SECONDS[attempt])
                continue
            except httpx.HTTPError as exc:
                raise GuanlanUpdateError(self._download_error_message(url)) from exc
            if len(response.content) > _MAX_WHEEL_BYTES and url.endswith(".whl"):
                raise GuanlanUpdateError("Guanlan wheel exceeds the size limit")
            return response.content

        raise AssertionError("Guanlan update fetch retry loop did not return")

    @staticmethod
    def _download_error_message(url: str, *, exhausted_retries: bool = False) -> str:
        if url == GUANLAN_PYPI_JSON_URL:
            resource = "Guanlan release metadata"
        elif url == GUANLAN_GITHUB_TAGS_URL:
            resource = "Guanlan GitHub update metadata"
        elif "/integrity/" in url:
            resource = "Guanlan release provenance"
        elif url.endswith(".whl"):
            resource = "Guanlan release wheel"
        else:
            resource = "Guanlan release data"
        if exhausted_retries:
            return f"Unable to download {resource} after {_FETCH_ATTEMPTS} attempts"
        return f"Unable to download {resource}"

    def _select_release(self, payload: dict[str, Any]) -> GuanlanRelease:
        info = payload.get("info")
        if not isinstance(info, dict) or not isinstance(info.get("version"), str):
            raise GuanlanUpdateError("PyPI did not provide a Guanlan version")
        version = info["version"].strip()
        try:
            Version(version)
        except InvalidVersion as exc:
            raise GuanlanUpdateError("PyPI returned an invalid Guanlan version") from exc
        releases = payload.get("releases")
        candidates = releases.get(version) if isinstance(releases, dict) else None
        if not isinstance(candidates, list):
            raise GuanlanUpdateError("PyPI did not provide files for Guanlan")

        expected_filename = f"guanlan-{version}-py3-none-any.whl"
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("filename") != expected_filename:
                continue
            if candidate.get("packagetype") != "bdist_wheel" or candidate.get("yanked"):
                continue
            digest = candidate.get("digests", {}).get("sha256")
            url = candidate.get("url")
            requires_python = candidate.get("requires_python") or ""
            size = candidate.get("size")
            if (
                not isinstance(digest, str)
                or not _SHA256_PATTERN.fullmatch(digest)
                or not isinstance(url, str)
                or not url.startswith("https://files.pythonhosted.org/")
                or not isinstance(size, int)
                or size <= 0
                or size > _MAX_WHEEL_BYTES
            ):
                continue
            self._ensure_python_compatible(requires_python)
            return GuanlanRelease(
                version=version,
                filename=expected_filename,
                download_url=url,
                sha256=digest,
                requires_python=requires_python,
                size=size,
                directory=_safe_directory_name(version, digest),
            )
        raise GuanlanUpdateError(
            "PyPI did not provide a compatible, non-yanked pure-Python Guanlan wheel",
        )

    @staticmethod
    def _is_not_newer(candidate: str, current: str | None) -> bool:
        if not current:
            return False
        try:
            return Version(candidate) <= Version(current)
        except InvalidVersion:
            return False

    @staticmethod
    def _ensure_python_compatible(requires_python: object) -> None:
        if not isinstance(requires_python, str) or not requires_python.strip():
            return
        try:
            supported = SpecifierSet(requires_python)
        except Exception as exc:  # noqa: BLE001 - malformed registry data is unsafe
            raise GuanlanUpdateError("PyPI returned an invalid Requires-Python") from exc
        current = Version(
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
        if current not in supported:
            raise GuanlanUpdateError(
                "This Guanlan release is incompatible with the bundled Python runtime",
            )

    def _validate_wheel(self, wheel: bytes, release: GuanlanRelease) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
                members = self._safe_members(archive)
                metadata_name = next(
                    name for name in members if name.endswith(".dist-info/METADATA")
                )
                metadata = BytesParser().parsebytes(archive.read(metadata_name))
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise GuanlanUpdateError("Guanlan wheel is malformed") from exc

        if canonicalize_name(metadata.get("Name", "")) != GUANLAN_PACKAGE:
            raise GuanlanUpdateError("Guanlan wheel has an unexpected package name")
        if metadata.get("Version") != release.version:
            raise GuanlanUpdateError("Guanlan wheel version does not match PyPI")
        self._ensure_python_compatible(metadata.get("Requires-Python", ""))
        self._ensure_dependencies_available(metadata)

    @staticmethod
    def _safe_members(archive: zipfile.ZipFile) -> list[str]:
        members = archive.infolist()
        if len(members) > _MAX_WHEEL_FILES:
            raise GuanlanUpdateError("Guanlan wheel contains too many files")
        if sum(item.file_size for item in members) > _MAX_UNCOMPRESSED_BYTES:
            raise GuanlanUpdateError("Guanlan wheel expands beyond the size limit")

        names: list[str] = []
        for item in members:
            name = item.filename
            path = PurePosixPath(name)
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if (
                not name
                or "\\" in name
                or path.is_absolute()
                or ".." in path.parts
                or (kind and kind not in {stat.S_IFREG, stat.S_IFDIR})
            ):
                raise GuanlanUpdateError("Guanlan wheel contains an unsafe path")
            if not item.is_dir():
                names.append(name)
        if len(set(names)) != len(names):
            raise GuanlanUpdateError("Guanlan wheel contains duplicate files")
        if not any(name.endswith(".dist-info/METADATA") for name in names):
            raise GuanlanUpdateError("Guanlan wheel is missing package metadata")
        return names

    @staticmethod
    def _ensure_dependencies_available(metadata) -> None:
        environment = default_environment()
        required: dict[str, Requirement] = {}
        for raw_requirement in metadata.get_all("Requires-Dist", []):
            try:
                requirement = Requirement(raw_requirement)
            except Exception as exc:  # noqa: BLE001 - malformed metadata is unsafe
                raise GuanlanUpdateError("Guanlan wheel has an invalid dependency") from exc
            if requirement.marker and not any(
                requirement.marker.evaluate({**environment, "extra": extra})
                for extra in ("", "mcp")
            ):
                continue
            required[canonicalize_name(requirement.name)] = requirement

        for requirement in required.values():
            try:
                installed = importlib.metadata.version(requirement.name)
            except importlib.metadata.PackageNotFoundError as exc:
                raise GuanlanUpdateError(
                    f"Guanlan {requirement.name} dependency requires a Lantai app update",
                ) from exc
            if requirement.specifier and not requirement.specifier.contains(
                Version(installed),
                prereleases=True,
            ):
                raise GuanlanUpdateError(
                    f"Guanlan {requirement.name} dependency requires a Lantai app update",
                )

    def _activate(
        self,
        release: GuanlanRelease,
        wheel: bytes,
        proof: dict[str, Any],
    ) -> GuanlanRelease:
        release = replace(
            release,
            directory=f"{release.directory}-{uuid4().hex[:8]}",
        )
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        versions_dir = self._root / "versions"
        staging_dir = self._root / "staging"
        versions_dir.mkdir(exist_ok=True, mode=0o700)
        staging_dir.mkdir(exist_ok=True, mode=0o700)
        stage = staging_dir / uuid4().hex
        target = versions_dir / release.directory
        if target.exists() or target.is_symlink():
            raise GuanlanUpdateError("A Guanlan release directory already exists")

        try:
            stage.mkdir(mode=0o700)
            self._extract_wheel(stage, wheel)
            artifact_dir = stage / ".artifact"
            artifact_dir.mkdir(mode=0o700)
            artifact_path = artifact_dir / release.filename
            artifact_path.write_bytes(wheel)
            os.chmod(artifact_path, 0o600)
            manifest = {
                "schema_version": 1,
                "version": release.version,
                "filename": release.filename,
                "sha256": release.sha256,
                "requires_python": release.requires_python,
                "size": release.size,
                "provenance": proof,
            }
            (stage / _MANIFEST_NAME).write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(stage / _MANIFEST_NAME, 0o600)
            os.replace(stage, target)
            self._write_current(release)
        except Exception:
            if stage.exists():
                self._remove_stage(stage)
            raise

        return release

    def _extract_wheel(self, destination: Path, wheel: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
                members = self._safe_members(archive)
                for name in members:
                    destination_path = destination / PurePosixPath(name)
                    destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination_path.write_bytes(archive.read(name))
                    os.chmod(destination_path, 0o444)
        except (OSError, zipfile.BadZipFile) as exc:
            raise GuanlanUpdateError("Unable to safely extract Guanlan wheel") from exc

        for path in sorted(destination.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                os.chmod(path, 0o555)

    def _write_current(self, release: GuanlanRelease) -> None:
        write_text_atomic_no_follow(
            self._root / _CURRENT_NAME,
            json.dumps(
                {
                    "schema_version": 1,
                    "directory": release.directory,
                    "version": release.version,
                    "sha256": release.sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _verify_extracted_wheel(self, directory: Path, wheel: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
                expected = {
                    name: hashlib.sha256(archive.read(name)).hexdigest()
                    for name in self._safe_members(archive)
                }
        except (OSError, zipfile.BadZipFile) as exc:
            raise GuanlanUpdateError("Stored Guanlan wheel is malformed") from exc

        actual: dict[str, str] = {}
        for path in directory.rglob("*"):
            relative = path.relative_to(directory).as_posix()
            if relative == _MANIFEST_NAME or relative.startswith(".artifact/"):
                continue
            if path.is_symlink() or not path.is_file():
                if path.is_dir():
                    continue
                raise GuanlanUpdateError("Stored Guanlan runtime contains an unsafe path")
            actual[relative] = hashlib.sha256(
                _read_regular_bytes(path, max_bytes=_MAX_UNCOMPRESSED_BYTES),
            ).hexdigest()
        if actual != expected:
            raise GuanlanUpdateError("Stored Guanlan files do not match the verified wheel")

    @staticmethod
    def _remove_stage(path: Path) -> None:
        """Best-effort cleanup limited to a just-created staging directory."""

        for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if child.is_dir() and not child.is_symlink():
                    child.chmod(0o700)
                    child.rmdir()
                else:
                    child.chmod(0o600)
                    child.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            path.rmdir()
        except OSError:
            pass

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any] | None:
        try:
            if path.is_symlink():
                return None
            payload = json.loads(read_text_no_follow(path))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None
