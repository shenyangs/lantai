"""Finite installed-inventory fixtures; no actual Windows build/signature claim."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def evidence(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/pack'))
    spec = importlib.util.spec_from_file_location('finite_windows_evidence', ROOT / 'scripts/pack/write_windows_release_evidence.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def pe(path, machine=0x8664):
    data = bytearray(256)
    data[:2] = b'MZ'
    data[0x3c:0x40] = (128).to_bytes(4, 'little')
    data[128:132] = b'PE\0\0'
    data[132:134] = machine.to_bytes(2, 'little')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def runtime(tmp_path, evidence, monkeypatch):
    payload, inputs = tmp_path / 'payload', tmp_path / 'inputs'
    pe(payload / 'python.exe')
    write(payload / 'conda-meta/python.json', {
        'name': 'python', 'version': '3.10.20', 'url': 'https://example.invalid/python.conda',
        'sha256': 'a' * 64, 'license': 'Python-2.0', 'files': ['python.exe'],
    })
    for label in ('tools', 'runtime', 'lantai', 'repair'):
        write(inputs / f'{label}-install.json', {'install': []})
    monkeypatch.setattr(evidence.sys, 'prefix', str(payload))
    monkeypatch.setattr(evidence.sys, 'platform', 'win32')
    monkeypatch.setattr(evidence.importlib.metadata, 'distributions', lambda: [])
    return payload, inputs


def origin(inputs, label='runtime', version='1.0', url='https://example.invalid/example.whl', digest='b' * 64):
    write(inputs / f'{label}-install.json', {'install': [{
        'metadata': {'name': 'example', 'version': version},
        'download_info': {'url': url, 'archive_info': {'hashes': {'sha256': digest}}},
    }]})


def test_repair_origin_is_actual_selected_archive(tmp_path, evidence, monkeypatch):
    _, inputs = runtime(tmp_path, evidence, monkeypatch)
    origin(inputs)
    origin(inputs, 'repair', digest='c' * 64)
    assert evidence.pip_origins(inputs)['example']['sha256'] == 'c' * 64


@pytest.mark.parametrize('url', ['https://user:secret@example.invalid/file.whl', 'https://example.invalid/file.whl?credential=secret'])
def test_installed_origin_never_transcribes_credentials(tmp_path, evidence, monkeypatch, url):
    _, inputs = runtime(tmp_path, evidence, monkeypatch)
    origin(inputs, url=url)
    with pytest.raises(ValueError, match='Credential-bearing'):
        evidence.pip_origins(inputs)


def test_unowned_foreign_binary_cannot_inherit_application_license(tmp_path, evidence, monkeypatch):
    payload, inputs = runtime(tmp_path, evidence, monkeypatch)
    pe(payload / 'libomp140.x86_64.dll')
    _, binaries = evidence.installed_inventory(payload, inputs)
    known = next(value for value in binaries if value['name'] == 'python.exe')
    unknown = next(value for value in binaries if value['name'] == 'libomp140.x86_64.dll')
    assert known['releasable'] and known['sha256'] == evidence.sha256(payload / 'python.exe')
    assert unknown['releasable'] is False and unknown['license'] == 'UNVERIFIED'
    assert unknown['source']['type'] == 'unverified-bundled-binary'


def test_inventory_rejects_a_distribution_directory_link(tmp_path, evidence, monkeypatch):
    payload, inputs = runtime(tmp_path, evidence, monkeypatch)
    pe(payload / 'real/example.dll')
    (payload / 'alias').symlink_to(payload / 'real', target_is_directory=True)
    origin(inputs)
    dist = SimpleNamespace(metadata={'Name': 'example', 'License-Expression': 'MIT'}, version='1.0',
                           files=[Path('alias/example.dll')], locate_file=lambda value: payload / value)
    monkeypatch.setattr(evidence.importlib.metadata, 'distributions', lambda: [dist])
    with pytest.raises(ValueError, match='link|reparse'):
        evidence.installed_inventory(payload, inputs)


def test_actual_installed_version_must_match_report(tmp_path, evidence, monkeypatch):
    payload, inputs = runtime(tmp_path, evidence, monkeypatch)
    origin(inputs)
    dist = SimpleNamespace(metadata={'Name': 'example'}, version='2.0', files=[], locate_file=lambda value: payload / value)
    monkeypatch.setattr(evidence.importlib.metadata, 'distributions', lambda: [dist])
    with pytest.raises(ValueError, match='version differs'):
        evidence.installed_inventory(payload, inputs)


@pytest.mark.parametrize('digest', ['a' * 63, 'not-a-source-digest'])
def test_incomplete_conda_source_digest_cannot_admit_binary(tmp_path, evidence, monkeypatch, digest):
    payload, inputs = runtime(tmp_path, evidence, monkeypatch)
    path = payload / 'conda-meta/python.json'
    meta = json.loads(path.read_text())
    meta['sha256'] = digest
    write(path, meta)
    _, binaries = evidence.installed_inventory(payload, inputs)
    assert binaries[0]['releasable'] is False


@pytest.mark.parametrize('fault', ['frontend-sha', 'frontend-version', 'payload-identity', 'python-arch'])
def test_installed_payload_identity_is_bound(tmp_path, evidence, fault):
    pe(tmp_path / 'python.exe', 0x14c if fault == 'python-arch' else 0x8664)
    expected = {'gitSha': 'a' * 40, 'version': '1.2.3'}
    frontend = {**expected}
    if fault == 'frontend-sha':
        frontend['gitSha'] = 'b' * 40
    if fault == 'frontend-version':
        frontend['version'] = '1.2.4'
    write(tmp_path / 'Lib/site-packages/lantai/console/release-provenance.json', frontend)
    write(tmp_path / 'release-provenance.json', {**expected, **({'other': True} if fault == 'payload-identity' else {})})
    with pytest.raises(ValueError):
        evidence.validate_installed_identity(tmp_path, 'a' * 40, '1.2.3')
