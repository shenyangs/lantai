"""Offline LTA-022 acceptance locks. No fixture binary is executed.

The expected source digest is held independently by the synthetic release
fixture. Current public download inputs provide no way to convey this trust.
The mutation lock records that the real worker nonetheless reports COMPLETED.
"""
from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

import lantai.local_models.llamacpp as module
from lantai.local_models.llamacpp import LlamaCppBackend
from lantai.local_models.download_manager import DownloadTaskResult, DownloadTaskStatus
from lantai.utils.command_runner import CommandResult


@pytest.fixture(autouse=True)
def forbid_execution_and_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("independent fixtures forbid real network and binary execution")
    monkeypatch.setattr(module.httpx, "Client", denied)
    monkeypatch.setattr(module, "run_command_async", denied)
    monkeypatch.setattr(module, "start_command_async", denied)


def tar_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o600
            archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


class Queue:
    def __init__(self):
        self.messages = []
    def put(self, item):
        self.messages.append(item)
    @property
    def result(self):
        return [item['payload'] for item in self.messages if item['type'] == 'result'][-1]


def worker(tmp_path, monkeypatch, data, declared_length=None):
    class Response:
        headers = {'Content-Length': str(declared_length if declared_length is not None else len(data))}
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def raise_for_status(self): return None
        def iter_bytes(self, chunk_size):
            for start in range(0, len(data), chunk_size):
                yield data[start:start + chunk_size]
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def stream(self, method, url, **kwargs):
            assert method == 'GET'
            assert url == 'https://release.invalid/b1234/llama-b1234-bin-ubuntu-x64.tar.gz'
            return Response()
    monkeypatch.setattr(module.httpx, 'Client', Client)
    queue = Queue()
    stage = tmp_path / 'stage'
    LlamaCppBackend._download_worker({
        'url': 'https://release.invalid/b1234/llama-b1234-bin-ubuntu-x64.tar.gz',
        'staging_dir': str(stage),
        'file_name': 'llama-b1234-bin-ubuntu-x64.tar.gz',
        'chunk_size': 19, 'timeout': 1, 'headers': {},
    }, queue)
    return stage, queue.result


def test_mutated_download_is_not_complete_merely_because_http_and_archive_succeed(tmp_path, monkeypatch):
    trusted = tar_bytes({'llama-server': b'trusted synthetic release'})
    tampered = tar_bytes({'llama-server': b'altered synthetic release'})
    expected_sha256 = hashlib.sha256(trusted).hexdigest()
    assert hashlib.sha256(tampered).hexdigest() != expected_sha256
    stage, result = worker(tmp_path, monkeypatch, tampered)
    assert result['status'] == 'failed', 'changed artifact from same HTTPS URL was accepted without any independent digest'
    assert not (stage / 'llama-server').exists(), 'unverified artifact was exposed in install staging'


def test_declared_content_length_mismatch_is_rejected(tmp_path, monkeypatch):
    data = tar_bytes({'llama-server': b'synthetic archive'})
    _, result = worker(tmp_path, monkeypatch, data, declared_length=len(data) + 7)
    assert result['status'] == 'failed', 'a short transfer was accepted even against declared Content-Length'


def elf_header(machine: int) -> bytes:
    data = bytearray(64)
    data[:4] = b'\x7fELF'
    data[4:7] = bytes([2, 1, 1])
    data[16:18] = (2).to_bytes(2, 'little')
    data[18:20] = machine.to_bytes(2, 'little')
    return bytes(data)


def backend(final_dir):
    instance = object.__new__(LlamaCppBackend)
    instance.os_name = 'linux'
    instance.arch = 'x64'
    instance.backend = 'cpu'
    instance.target_dir = final_dir
    return instance


@pytest.mark.parametrize('kind', ['wrong_arch', 'wrong_platform', 'missing_server'])
def test_invalid_candidate_cannot_publish_over_previous_installation(tmp_path, kind):
    stage = tmp_path / 'stage'
    installed = tmp_path / 'installed'
    stage.mkdir()
    installed.mkdir()
    old = installed / 'llama-server'
    old.write_bytes(b'previous installed fixture')
    if kind == 'wrong_arch':
        # ELF AArch64 e_machine=183 while selected target is x64 (62).
        (stage / 'llama-server').write_bytes(elf_header(183))
    elif kind == 'wrong_platform':
        (stage / 'llama-server').write_bytes(b'MZ' + b'\x00' * 126)
    else:
        (stage / 'llama-cli').write_bytes(b'candidate without server')
    result, _ = backend(installed)._finalize_download_result(
        DownloadTaskResult(status=DownloadTaskStatus.COMPLETED, local_path=str(stage)),
        staging_dir=stage, final_dir=installed,
    )
    assert result.status == DownloadTaskStatus.FAILED, f'{kind} candidate was published without static validation'
    assert old.read_bytes() == b'previous installed fixture'


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'text'])
def test_path_exists_does_not_establish_verified_installation(tmp_path, kind):
    installed = tmp_path / 'installed'
    installed.mkdir()
    executable = installed / 'llama-server'
    if kind == 'directory':
        executable.mkdir()
    elif kind == 'symlink':
        outside = tmp_path / 'outside-fixture'
        outside.write_bytes(b'unverified synthetic bytes')
        executable.symlink_to(outside)
    else:
        executable.write_bytes(b'unverified synthetic text')
    valid, _ = backend(installed).check_llamacpp_installation()
    assert not valid, f'{kind} path was reported installed without verifying an executable'


@pytest.mark.asyncio
async def test_version_identity_is_not_truncated_to_four_digits(tmp_path, monkeypatch):
    instance = backend(tmp_path / 'installed')
    monkeypatch.setattr(instance, 'check_llamacpp_installation', lambda: (True, ''))
    async def synthetic_version(command, **kwargs):
        assert command == [str(instance.executable), '--version']
        return CommandResult(command=command, returncode=0, stdout='', stderr='version: 100001 (synthetic)\n')
    monkeypatch.setattr(module, 'run_command_async', synthetic_version)
    assert await instance.get_version() == '100001', 'version identity was truncated before matching a release'


def test_malformed_archive_returns_failure_without_extracting_binary(tmp_path, monkeypatch):
    stage, result = worker(tmp_path, monkeypatch, b'not an archive')
    assert result['status'] == 'failed'
    assert not (stage / 'llama-server').exists()
