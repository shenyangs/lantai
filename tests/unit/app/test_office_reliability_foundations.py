from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app import office_capabilities, office_conversion
from lantai.security.output_sanitizer import (
    sanitize_public_output,
    summarize_subprocess_failure,
)
from lantai.security.shell_effects import extract_shell_effects


def test_mixed_same_stem_office_conversions_never_overwrite_each_other(
    tmp_path: Path,
    monkeypatch,
) -> None:
    docx = tmp_path / "report.docx"
    pptx = tmp_path / "report.pptx"
    docx.write_bytes(b"docx")
    pptx.write_bytes(b"pptx")
    output_dir = tmp_path / "rendered"

    monkeypatch.setattr(
        office_conversion, "find_soffice", lambda: "/fake/soffice"
    )

    def fake_run(arguments, **_kwargs):
        converted_dir = Path(arguments[arguments.index("--outdir") + 1])
        source = Path(arguments[-1])
        extension = arguments[arguments.index("--convert-to") + 1]
        (converted_dir / f"{source.stem}.{extension}").write_bytes(
            source.read_bytes()
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(office_conversion.subprocess, "run", fake_run)

    docx_result = office_conversion.convert_office_document(
        docx, "pdf", output_dir
    )
    pptx_result = office_conversion.convert_office_document(
        pptx, "pdf", output_dir
    )

    assert docx_result.output_path.name == "report.docx.pdf"
    assert pptx_result.output_path.name == "report.pptx.pdf"
    assert docx_result.output_path.read_bytes() == b"docx"
    assert pptx_result.output_path.read_bytes() == b"pptx"


def test_fractional_conversion_timeout_is_not_rounded_up(tmp_path, monkeypatch):
    source = tmp_path / "local.pptx"
    source.write_bytes(b"controlled source")
    monkeypatch.setattr(office_conversion, "find_soffice", lambda: "/fake/soffice")
    observed = []

    def converter(args, **kwargs):
        observed.append(kwargs["timeout"])
        output = Path(args[args.index("--outdir") + 1])
        (output / "local.pdf").write_bytes(b"controlled PDF")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(office_conversion.subprocess, "run", converter)
    office_conversion.convert_office_document(
        source, "pdf", tmp_path / "output", timeout=.075,
    )
    assert len(observed) == 1
    assert 0 < observed[0] <= .075


@pytest.mark.parametrize("suffix, target, page", [
    (".pptx", "pdf", 0),
    (".pptx", "pdf", -1),
    (".pptx", "pdf", True),
    (".pptx", "pdf", "1"),
    (".pptx", "pdf", 1.5),
    (".pptx", "png", 1),
    (".docx", "pdf", 1),
])
def test_invalid_slide_range_fails_before_converter_or_publication(
    tmp_path, monkeypatch, suffix, target, page,
):
    source = tmp_path / f"source{suffix}"
    source.write_bytes(b"controlled source")
    output = tmp_path / "output"

    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid slide range admitted a converter")

    monkeypatch.setattr(office_conversion.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="positive PPTX slide number"):
        office_conversion.convert_office_document(
            source, target, output, pdf_page_range=page,
        )
    assert not output.exists()


def test_bundled_mac_converter_uses_private_child_fontconfig_only(tmp_path: Path, monkeypatch) -> None:
    contents = tmp_path / "native" / "libreoffice-headless" / "libreoffice" / "LibreOfficeDev.app" / "Contents"
    binary = contents / "MacOS" / "soffice"
    configuration = contents / "Resources" / "fontconfig" / "fonts.conf"
    binary.parent.mkdir(parents=True)
    configuration.parent.mkdir(parents=True)
    binary.write_bytes(b"controlled executable")
    configuration.write_bytes(b"controlled font configuration")
    source = tmp_path / "local.pptx"
    source.write_bytes(b"controlled document")
    monkeypatch.setenv("FONTCONFIG_FILE", "/controlled/parent/fonts.conf")
    monkeypatch.setenv("FONTCONFIG_PATH", "/controlled/parent")
    monkeypatch.setenv("XDG_CACHE_HOME", "/controlled/parent/cache")
    ambient = dict(os.environ)
    calls = []
    monkeypatch.setattr(office_conversion, "find_soffice", lambda: str(binary))

    def fake_run(arguments, **kwargs):
        calls.append(kwargs)
        child = kwargs.get("env")
        assert child is not None
        assert child["FONTCONFIG_FILE"] == str(configuration)
        assert child["FONTCONFIG_PATH"] == str(configuration.parent)
        profile_uri = next(argument for argument in arguments if argument.startswith("-env:UserInstallation="))
        profile = Path(profile_uri.removeprefix("-env:UserInstallation=file://"))
        assert Path(child["XDG_CACHE_HOME"]) == profile / "font-cache"
        assert Path(child["XDG_CACHE_HOME"]).is_dir()
        assert child.get("HOME") == ambient.get("HOME")
        assert child.get("PATH") == ambient.get("PATH")
        converted = Path(arguments[arguments.index("--outdir") + 1])
        (converted / "local.pdf").write_bytes(b"controlled pdf")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(office_conversion.subprocess, "run", fake_run)
    result = office_conversion.convert_office_document(source, "pdf", tmp_path / "output")
    assert result.output_path.read_bytes() == b"controlled pdf"
    assert len(calls) == 1
    assert dict(os.environ) == ambient
    assert configuration.read_bytes() == b"controlled font configuration"
    assert not Path(calls[0]["env"]["XDG_CACHE_HOME"]).exists()


@pytest.mark.parametrize("installation", ["custom", "missing_configuration", "outside_configuration"])
def test_converter_does_not_reconfigure_custom_or_unverified_fonts(tmp_path: Path, monkeypatch, installation: str) -> None:
    runtime = tmp_path / "native" / "libreoffice-headless"
    contents = (tmp_path / "CustomOffice.app" / "Contents" if installation == "custom"
                else runtime / "libreoffice" / "LibreOfficeDev.app" / "Contents")
    binary = contents / "MacOS" / "soffice"
    configuration = contents / "Resources" / "fontconfig" / "fonts.conf"
    binary.parent.mkdir(parents=True)
    configuration.parent.mkdir(parents=True)
    binary.write_bytes(b"controlled executable")
    if installation == "outside_configuration":
        outside = tmp_path / "outside.conf"
        outside.write_bytes(b"controlled outside configuration")
        configuration.symlink_to(outside)
    elif installation == "custom":
        configuration.write_bytes(b"controlled custom configuration")
    source = tmp_path / "local.pptx"
    source.write_bytes(b"controlled document")
    ambient = dict(os.environ)
    monkeypatch.setattr(office_conversion, "find_soffice", lambda: str(binary))

    def fake_run(arguments, **kwargs):
        assert "env" not in kwargs
        converted = Path(arguments[arguments.index("--outdir") + 1])
        (converted / "local.pdf").write_bytes(b"controlled pdf")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(office_conversion.subprocess, "run", fake_run)
    assert office_conversion.convert_office_document(source, "pdf", tmp_path / "output").output_path.is_file()
    assert dict(os.environ) == ambient


def test_office_capability_probe_uses_documented_kdocs_cli_keychain_status(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KDOCS_ACCESS_TOKEN", raising=False)

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/kdocs-cli" if name == "kdocs-cli" else None

    monkeypatch.setattr(office_capabilities.shutil, "which", fake_which)
    monkeypatch.setattr(
        office_capabilities.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"authenticated": true, "status": "authenticated"}',
            stderr="",
        ),
    )

    report = office_capabilities.collect_office_capabilities()

    assert report["connectors"]["kdocs"] == {
        "mode": "cli",
        "command_available": True,
        "authenticated": True,
        "auth_source": "keychain_status",
        "ready": True,
    }
    assert report["features"]["kdocs_cli"] is True


def test_office_capability_probe_rejects_stale_undecryptable_credentials(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KDOCS_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        office_capabilities.shutil,
        "which",
        lambda name: "/usr/local/bin/kdocs-cli" if name == "kdocs-cli" else None,
    )
    monkeypatch.setattr(
        office_capabilities.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"authenticated": true, "status": "authenticated"}',
            stderr=(
                "Failed to decrypt stored secret; blocking use until "
                "credentials are reconfigured"
            ),
        ),
    )

    report = office_capabilities.collect_office_capabilities()

    assert report["connectors"]["kdocs"]["authenticated"] is False
    assert report["connectors"]["kdocs"]["auth_source"] == "not_authenticated"
    assert report["features"]["kdocs_cli"] is False


def test_public_output_removes_paths_controls_secrets_and_traceback() -> None:
    raw = (
        "\x1b[31mAuthorization: Bearer super-secret\x1b[0m\n"
        "GET https://example.test/?access_token=abc123&ok=1\n"
        "Traceback (most recent call last):\n"
        '  File "/Users/example/private/run.py", line 9\n'
        "ValueError: invalid request\n"
    )

    projected = sanitize_public_output(raw)
    summary = summarize_subprocess_failure(raw)

    assert "\x1b" not in projected
    assert "/Users/sam" not in projected
    assert "super-secret" not in projected
    assert "abc123" not in projected
    assert "ValueError: invalid request" in summary
    assert "Traceback" not in summary
    assert "run.py" not in summary


def test_shell_effects_separate_reads_writes_and_read_write_redirection() -> (
    None
):
    readonly = extract_shell_effects("ls /Applications 2>/dev/null")
    mutation = extract_shell_effects(
        "cp source.txt /System/Library/target.txt"
    )
    read_write = extract_shell_effects("tool <> state.db")

    assert readonly.read_paths == ("/Applications",)
    assert readonly.write_paths == ()
    assert mutation.read_paths == ("source.txt",)
    assert mutation.write_paths == ("/System/Library/target.txt",)
    assert read_write.read_paths == ("state.db",)
    assert read_write.write_paths == ("state.db",)
