"""Dependency provenance must distinguish successful checks from skipped checks."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def gate(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        'dependency_evidence_gate', ROOT / 'scripts/check_release_dependency_locks.py',
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for relative in module.LOCK_PATHS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(module, '_validate_console_lock', lambda root: {})
    monkeypatch.setattr(module, '_validate_direct_runtime_dependencies', lambda: {})
    monkeypatch.setattr(module, '_validate_runtime_imports', lambda root: None)
    monkeypatch.setattr(module, '_git_head', lambda root: 'synthetic-sha')
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/synthetic/uv')
    return module


def test_skipping_uv_check_never_claims_environment_matches_lock(gate, monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail('skip option unexpectedly executed uv')
    monkeypatch.setattr(gate.subprocess, 'run', unexpected)
    output = tmp_path / 'manifest.json'
    assert gate.main(['--root', str(tmp_path), '--skip-uv-check', '--write-manifest', str(output)]) == 0
    runtime = json.loads(output.read_text())['runtime']
    assert runtime['environmentMatchesLock'] is None
    assert runtime['environmentLockCheck'] == 'skipped'


def test_verified_manifest_requires_both_lock_and_environment_checks(gate, monkeypatch, tmp_path):
    calls = []
    def checked(command, **kwargs):
        calls.append(command)
        assert kwargs['check'] is True
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(gate.subprocess, 'run', checked)
    output = tmp_path / 'manifest.json'
    assert gate.main(['--root', str(tmp_path), '--write-manifest', str(output)]) == 0
    assert calls == [
        ['/synthetic/uv', 'lock', '--check'],
        ['/synthetic/uv', 'sync', '--locked', '--extra', 'dev', '--check'],
    ]
    runtime = json.loads(output.read_text())['runtime']
    assert runtime['environmentMatchesLock'] is True
    assert runtime['environmentLockCheck'] == 'passed'


@pytest.mark.parametrize('failure', ['lock', 'sync'])
def test_failed_check_never_overwrites_prior_manifest(gate, monkeypatch, tmp_path, failure):
    def checked(command, **kwargs):
        if command[1] == failure:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(gate.subprocess, 'run', checked)
    output = tmp_path / 'manifest.json'
    output.write_text('prior-evidence', encoding='utf-8')
    with pytest.raises(subprocess.CalledProcessError):
        gate.main(['--root', str(tmp_path), '--write-manifest', str(output)])
    assert output.read_text() == 'prior-evidence'
