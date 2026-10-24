# -*- coding: utf-8 -*-
"""Behavior tests for the trusted Guanlan runtime updater."""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile

import httpx
import pytest

from lantai.app.mcp import guanlan_update
from lantai.app.mcp.guanlan_update import (
    GUANLAN_GITHUB_TAGS_URL,
    GUANLAN_PYPI_JSON_URL,
    GuanlanPackageUpdater,
    GuanlanUpdateError,
)


def _wheel_bytes(version: str) -> bytes:
    """Build the smallest valid pure-Python Guanlan wheel fixture."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("guanlan/__init__.py", "__version__ = 'fixture'\n")
        archive.writestr(
            f"guanlan-{version}.dist-info/METADATA",
            "Metadata-Version: 2.1\n"
            "Name: guanlan\n"
            f"Version: {version}\n"
            "Requires-Python: >=3.10\n",
        )
        archive.writestr(
            f"guanlan-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(
            f"guanlan-{version}.dist-info/RECORD",
            "",
        )
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_manual_update_activates_a_verified_new_release(tmp_path):
    """A successful manual update changes the active runtime version."""

    version = "0.8.3"
    filename = f"guanlan-{version}-py3-none-any.whl"
    wheel = _wheel_bytes(version)
    digest = hashlib.sha256(wheel).hexdigest()
    release = {
        "info": {"version": version},
        "releases": {
            version: [
                {
                    "filename": filename,
                    "packagetype": "bdist_wheel",
                    "python_version": "py3",
                    "requires_python": ">=3.10",
                    "size": len(wheel),
                    "yanked": False,
                    "url": "https://files.pythonhosted.org/packages/guanlan.whl",
                    "digests": {"sha256": digest},
                },
            ],
        },
    }

    async def fetch(url: str) -> bytes:
        if url.endswith("/json"):
            return json.dumps(release).encode()
        if url == "https://files.pythonhosted.org/packages/guanlan.whl":
            return wheel
        return b'{"version": 1, "attestation_bundles": []}'

    verified: list[tuple[str, str]] = []
    requested_urls: list[str] = []

    async def tracked_fetch(url: str) -> bytes:
        requested_urls.append(url)
        return await fetch(url)

    updater = GuanlanPackageUpdater(
        working_dir=tmp_path,
        fetch=tracked_fetch,
        verify_provenance=lambda _proof, name, sha256, _offline: verified.append(
            (name, sha256),
        ),
    )

    result = await updater.update(current_version="0.6.13")

    assert result.updated is True
    assert result.previous_version == "0.6.13"
    assert result.version == version
    assert updater.active_release().version == version
    package_dir = updater.verified_package_dir()
    assert package_dir is not None
    assert verified == [(filename, digest), (filename, digest)]

    source = package_dir / "guanlan" / "__init__.py"
    os.chmod(source, 0o600)
    source.write_text("__version__ = 'tampered'\n", encoding="utf-8")

    assert updater.verified_package_dir() is None
    assert requested_urls == [
        "https://pypi.org/pypi/guanlan/json",
        "https://files.pythonhosted.org/packages/guanlan.whl",
        "https://pypi.org/integrity/guanlan/0.8.3/"
        "guanlan-0.8.3-py3-none-any.whl/provenance",
    ]


@pytest.mark.asyncio
async def test_manual_update_reports_latest_without_downloading_a_wheel(tmp_path):
    """An unchanged manual check never claims to have updated Guanlan."""

    version = "0.8.3"
    release = {
        "info": {"version": version},
        "releases": {
            version: [
                {
                    "filename": f"guanlan-{version}-py3-none-any.whl",
                    "packagetype": "bdist_wheel",
                    "yanked": False,
                    "url": "https://files.pythonhosted.org/packages/guanlan.whl",
                    "requires_python": ">=3.10",
                    "size": 1,
                    "digests": {"sha256": hashlib.sha256(b"x").hexdigest()},
                },
            ],
        },
    }

    async def fetch(url: str) -> bytes:
        assert url == "https://pypi.org/pypi/guanlan/json"
        return json.dumps(release).encode()

    result = await GuanlanPackageUpdater(
        working_dir=tmp_path,
        fetch=fetch,
    ).update(current_version=version)

    assert result.updated is False
    assert result.version == version
    assert "already the latest" in result.message


@pytest.mark.asyncio
async def test_manual_update_retries_transient_pypi_metadata_connection_failures(
    tmp_path,
    monkeypatch,
):
    """A brief PyPI connection interruption does not fail a manual check."""

    release = {
        "info": {"version": "0.8.3"},
        "releases": {
            "0.8.3": [
                {
                    "filename": "guanlan-0.8.3-py3-none-any.whl",
                    "packagetype": "bdist_wheel",
                    "yanked": False,
                    "url": "https://files.pythonhosted.org/packages/guanlan.whl",
                    "requires_python": ">=3.10",
                    "size": 1,
                    "digests": {"sha256": hashlib.sha256(b"x").hexdigest()},
                },
            ],
        },
    }
    attempts = 0

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError(
                    "temporary connection interruption",
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                content=json.dumps(release).encode(),
                request=httpx.Request("GET", url),
            )

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "lantai.app.mcp.guanlan_update.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    monkeypatch.setattr(guanlan_update.asyncio, "sleep", no_wait)

    result = await GuanlanPackageUpdater(working_dir=tmp_path).update(
        current_version="0.8.3",
    )

    assert attempts == 3
    assert result.metadata_degraded is False
    assert result.version == "0.8.3"
    assert "already the latest" in result.message


@pytest.mark.asyncio
async def test_manual_update_identifies_wheel_download_failure_after_retries(
    tmp_path,
    monkeypatch,
):
    """A failed wheel download is not mislabeled as a metadata failure."""

    version = "0.8.5"
    filename = f"guanlan-{version}-py3-none-any.whl"
    release = {
        "info": {"version": version},
        "releases": {
            version: [
                {
                    "filename": filename,
                    "packagetype": "bdist_wheel",
                    "yanked": False,
                    "url": "https://files.pythonhosted.org/packages/guanlan.whl",
                    "requires_python": ">=3.10",
                    "size": 1,
                    "digests": {"sha256": hashlib.sha256(b"x").hexdigest()},
                },
            ],
        },
    }
    wheel_attempts = 0

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            nonlocal wheel_attempts
            if url == GUANLAN_PYPI_JSON_URL:
                return httpx.Response(
                    200,
                    content=json.dumps(release).encode(),
                    request=httpx.Request("GET", url),
                )
            wheel_attempts += 1
            raise httpx.ConnectError(
                "temporary connection interruption",
                request=httpx.Request("GET", url),
            )

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "lantai.app.mcp.guanlan_update.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    monkeypatch.setattr(guanlan_update.asyncio, "sleep", no_wait)

    with pytest.raises(
        GuanlanUpdateError,
        match="Unable to download Guanlan release wheel after 3 attempts",
    ):
        await GuanlanPackageUpdater(working_dir=tmp_path).update(
            current_version="0.8.3",
        )

    assert wheel_attempts == 3


@pytest.mark.asyncio
async def test_pypi_metadata_failure_uses_github_tags_for_degraded_update_check(
    tmp_path,
):
    """A reachable GitHub tag informs the user without becoming an installer."""

    requested_urls: list[str] = []

    async def fetch(url: str) -> bytes:
        requested_urls.append(url)
        if url == GUANLAN_PYPI_JSON_URL:
            raise GuanlanUpdateError("Unable to download Guanlan release metadata")
        if url == GUANLAN_GITHUB_TAGS_URL:
            return json.dumps(
                [
                    {"name": "not-a-release"},
                    {"name": "v0.9.0"},
                    {"name": "v0.8.3"},
                ],
            ).encode()
        raise AssertionError(f"Unexpected download: {url}")

    result = await GuanlanPackageUpdater(
        working_dir=tmp_path,
        fetch=fetch,
    ).update(current_version="0.8.3")

    assert result.updated is False
    assert result.version == "0.8.3"
    assert result.candidate_version == "0.9.0"
    assert result.metadata_source == "github_tags"
    assert result.metadata_degraded is True
    assert result.update_available is True
    assert "GitHub Tags" in result.message
    assert "PyPI" in result.message
    assert requested_urls == [GUANLAN_PYPI_JSON_URL, GUANLAN_GITHUB_TAGS_URL]


@pytest.mark.asyncio
async def test_invalid_pypi_metadata_uses_github_tags_for_degraded_update_check(
    tmp_path,
):
    """A malformed PyPI response is no more installable than an outage."""

    async def fetch(url: str) -> bytes:
        if url == GUANLAN_PYPI_JSON_URL:
            return b"not valid JSON"
        if url == GUANLAN_GITHUB_TAGS_URL:
            return json.dumps([{"name": "v0.9.0"}]).encode()
        raise AssertionError(f"Unexpected download: {url}")

    result = await GuanlanPackageUpdater(
        working_dir=tmp_path,
        fetch=fetch,
    ).update(current_version="0.8.3")

    assert result.updated is False
    assert result.candidate_version == "0.9.0"
    assert result.metadata_source == "github_tags"
    assert result.metadata_degraded is True


@pytest.mark.asyncio
async def test_update_check_keeps_current_runtime_when_all_metadata_sources_fail(
    tmp_path,
):
    """An update check failure is informative and never disables Guanlan."""

    requested_urls: list[str] = []

    async def fetch(url: str) -> bytes:
        requested_urls.append(url)
        raise GuanlanUpdateError("network unavailable")

    result = await GuanlanPackageUpdater(
        working_dir=tmp_path,
        fetch=fetch,
    ).update(current_version="0.8.3")

    assert result.updated is False
    assert result.version == "0.8.3"
    assert result.candidate_version is None
    assert result.metadata_source == "unavailable"
    assert result.metadata_degraded is True
    assert result.update_available is False
    assert "current bundled Guanlan runtime remains available" in result.message
    assert requested_urls == [GUANLAN_PYPI_JSON_URL, GUANLAN_GITHUB_TAGS_URL]
