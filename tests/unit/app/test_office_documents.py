# -*- coding: utf-8 -*-
"""Tests for Office document capability integration."""

import subprocess
import zipfile

import lantai.app.office_documents as office_documents
from lantai.app.office_documents import (
    OfficeCliStatus,
    build_officecli_mcp_client_config,
    compare_office_documents,
    ensure_officecli_default,
    extract_office_document_text_with_officecli,
    get_officecli_status,
    update_officecli_config,
)


def _write_docx(path, paragraphs: list[str]) -> None:
    runs = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                f'wordprocessingml/2006/main"><w:body>{runs}</w:body></w:document>'
            ),
        )


def test_officecli_status_detects_path_binary(monkeypatch, tmp_path):
    config_file = tmp_path / "office_documents.json"
    monkeypatch.setattr(office_documents, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(
        office_documents.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/officecli"
        if name == "officecli"
        else None,
    )

    def fake_run(args, **kwargs):
        assert args == ["/opt/homebrew/bin/officecli", "--version"]
        assert kwargs["env"]["OFFICECLI_SKIP_UPDATE"] == "1"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="officecli 1.0.71\n",
            stderr="",
        )

    monkeypatch.setattr(office_documents.subprocess, "run", fake_run)

    status = get_officecli_status()

    assert status.available is True
    assert status.path == "/opt/homebrew/bin/officecli"
    assert status.source == "path"
    assert status.version == "officecli 1.0.71"
    assert status.mcp_supported is True


def test_officecli_status_uses_persisted_custom_path(monkeypatch, tmp_path):
    config_file = tmp_path / "office_documents.json"
    monkeypatch.setattr(office_documents, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(office_documents.shutil, "which", lambda _: None)

    update_officecli_config(
        enabled=False,
        officecli_path="/Applications/officecli",
    )

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="1.2.3\n",
            stderr="",
        )

    monkeypatch.setattr(office_documents.subprocess, "run", fake_run)

    status = get_officecli_status()

    assert status.enabled is False
    assert status.available is True
    assert status.path == "/Applications/officecli"
    assert status.source == "config"


def test_officecli_status_detects_bundled_backend_binary(monkeypatch, tmp_path):
    config_file = tmp_path / "office_documents.json"
    backend_dir = tmp_path / "lantai-server"
    bundled = backend_dir / "bin" / "officecli"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"fake binary")
    monkeypatch.setattr(office_documents, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(
        office_documents.sys,
        "executable",
        str(backend_dir / "lantai-server"),
    )
    monkeypatch.setattr(office_documents.shutil, "which", lambda _: None)

    def fake_run(args, **kwargs):
        assert args == [str(bundled), "--version"]
        assert kwargs["env"]["OFFICECLI_SKIP_UPDATE"] == "1"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="officecli 1.0.99\n",
            stderr="",
        )

    monkeypatch.setattr(office_documents.subprocess, "run", fake_run)

    status = get_officecli_status()

    assert status.available is True
    assert status.path == str(bundled)
    assert status.source == "bundled"
    assert status.version == "officecli 1.0.99"


def test_officecli_status_missing_is_nonfatal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        office_documents,
        "_CONFIG_FILE",
        tmp_path / "office_documents.json",
    )
    monkeypatch.setattr(office_documents.shutil, "which", lambda _: None)

    status = get_officecli_status()

    assert status.available is False
    assert status.path == ""
    assert status.source == "missing"
    assert "未检测到" in status.message


def test_build_officecli_mcp_client_config_uses_detected_command(monkeypatch):
    monkeypatch.setattr(
        office_documents,
        "get_officecli_status",
        lambda: OfficeCliStatus(
            enabled=True,
            available=True,
            path="/usr/local/bin/officecli",
            source="path",
            version="1.0.71",
            mcp_supported=True,
            config_path="/tmp/office_documents.json",
        ),
    )

    cfg = build_officecli_mcp_client_config(enabled=True)

    assert cfg.enabled is True
    assert cfg.transport == "stdio"
    assert cfg.command == "/usr/local/bin/officecli"
    assert cfg.args == ["mcp"]
    assert cfg.env["OFFICECLI_SKIP_UPDATE"] == "1"
    assert cfg.env["OFFICECLI_RESIDENT_FLUSH"] == "each"


def test_officecli_mcp_defaults_enabled_for_new_agents(monkeypatch):
    monkeypatch.setattr(
        office_documents,
        "get_officecli_status",
        lambda: OfficeCliStatus(
            enabled=True,
            available=True,
            path="/usr/local/bin/officecli",
            source="path",
            version="1.0.143",
            mcp_supported=True,
            config_path="/tmp/office_documents.json",
        ),
    )

    config = ensure_officecli_default(None)

    assert config.clients["officecli"].enabled is True
    assert config.clients["officecli"].command == "/usr/local/bin/officecli"


def test_officecli_mcp_preserves_explicit_disable(monkeypatch):
    from lantai.config.config import MCPConfig

    monkeypatch.setattr(
        office_documents,
        "get_officecli_status",
        lambda: OfficeCliStatus(
            enabled=True,
            available=True,
            path="/usr/local/bin/officecli",
            source="path",
            version="1.0.143",
            mcp_supported=True,
            config_path="/tmp/office_documents.json",
        ),
    )
    existing = build_officecli_mcp_client_config(enabled=False)

    config = ensure_officecli_default(
        MCPConfig(clients={"officecli": existing}),
    )

    assert config.clients["officecli"].enabled is False


def test_extract_office_document_text_with_officecli_uses_view(monkeypatch, tmp_path):
    source = tmp_path / "brief.docx"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        office_documents,
        "get_officecli_command",
        lambda: "/usr/local/bin/officecli",
    )

    def fake_run(args, **kwargs):
        assert args == ["/usr/local/bin/officecli", "view", str(source), "text"]
        assert kwargs["env"]["OFFICECLI_SKIP_UPDATE"] == "1"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="OfficeCLI extracted text\n",
            stderr="",
        )

    monkeypatch.setattr(office_documents.subprocess, "run", fake_run)

    text = extract_office_document_text_with_officecli(str(source))

    assert text == "OfficeCLI extracted text"


def test_compare_office_documents_falls_back_to_ooxml(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        office_documents,
        "_CONFIG_FILE",
        tmp_path / "office_documents.json",
    )
    monkeypatch.setattr(office_documents.shutil, "which", lambda _: None)
    before = tmp_path / "brief-before.docx"
    after = tmp_path / "brief-after.docx"
    _write_docx(before, ["Project Alpha", "Launch Monday"])
    _write_docx(after, ["Project Alpha", "Launch Friday", "Owner: Sam"])

    result = compare_office_documents(str(before), str(after))

    assert result.changed is True
    assert result.before_method == "office_xml"
    assert result.after_method == "office_xml"
    assert result.added_lines >= 1
    assert result.removed_lines >= 1
    assert "+Launch Friday" in result.unified_diff
    assert "-Launch Monday" in result.unified_diff
