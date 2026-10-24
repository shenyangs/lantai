"""Actual admission with synthetic bytes; no process or network execution."""
from __future__ import annotations

import hashlib
import io
import socket
import struct
import subprocess
import tarfile
from pathlib import Path

import pytest

import lantai.local_models.llamacpp as module
from lantai.local_models.llamacpp_assets import TrustedLlamaCppAsset
from lantai.local_models.llamacpp_install import DirectoryAnchor
from lantai.utils.command_runner import CommandResult


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("independent audit forbids network and binary execution")
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(module.httpx, "Client", denied)
    monkeypatch.setattr(module.httpx, "AsyncClient", denied)


def admitted_fixture(stage: Path) -> TrustedLlamaCppAsset:
    stage.mkdir(mode=0o700)
    binary = bytearray(64)
    binary[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", binary, 16, 2, 62)
    binary = bytes(binary) + b"SYNTHETIC AUDIT; NOT AN EXECUTABLE"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("llama-server")
        entry.size = len(binary)
        entry.mode = 0o755
        archive.addfile(entry, io.BytesIO(binary))
    data = buffer.getvalue()
    name = "llama-b8744-bin-ubuntu-x64.tar.gz"
    (stage / name).write_bytes(data)
    (stage / "llama-server").write_bytes(binary)
    (stage / "llama-server").chmod(0o755)
    return TrustedLlamaCppAsset(
        tag="b8744", os_name="linux", arch="x64", backend="cpu",
        cuda_version=None, file_name=name,
        url="https://github.com/ggml-org/llama.cpp/releases/download/b8744/" + name,
        sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
    )


@pytest.mark.parametrize("control", [None, "GGML_BACKEND_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH"])
def test_staged_health_excludes_unverified_loader_controls(tmp_path, monkeypatch, control):
    stage = tmp_path / "owned-stage"
    asset = admitted_fixture(stage)
    controls = ("GGML_BACKEND_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH")
    for name in controls:
        monkeypatch.delenv(name, raising=False)
    if control:
        # This is an environment string only; no plugin is written or read.
        monkeypatch.setenv(control, str(tmp_path / "unverified-outside-stage.so"))
    calls = []

    def observe_process(command, *, timeout, cwd, env, check):
        assert command == [str(stage / "llama-server"), "--version"]
        assert timeout == 15 and cwd == str(stage) and check is False
        assert isinstance(env, dict)
        # Report names only, never the inherited environment or its values.
        inherited = sorted(name for name in controls if name in env)
        assert not inherited, "unverified loader controls reached staged execution: " + ", ".join(inherited)
        calls.append(command)
        return CommandResult(command, 0, "", "version: 8744 (synthetic audit)\n")

    monkeypatch.setattr(module, "run_command", observe_process)
    with DirectoryAnchor(stage) as candidate:
        assert module.LlamaCppBackend._check_staged_health(candidate, asset) == {
            "version": "8744", "returncode": 0,
        }
    assert len(calls) == 1
