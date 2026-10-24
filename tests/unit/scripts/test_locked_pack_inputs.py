"""Finite locks for exact packing inputs; no conda, downloads or real build."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_repair_inputs_preserve_exact_version_hash_and_platform_marker():
    helper = module('locked_pack_inputs', 'scripts/pack/locked_runtime_inputs.py')
    text = ('huggingface-hub==0.30.0 ; python_version >= "3.10" \\\n'
            '    --hash=sha256:' + 'a' * 64 + '\n'
            'discord-py==2.4.0 --hash=sha256:' + 'b' * 64 + '\n'
            'unrelated==9.9.9 --hash=sha256:' + 'c' * 64 + '\n')
    selected = helper.select_requirements(text, ('huggingface_hub', 'discord.py'))
    assert 'huggingface-hub==0.30.0' in selected and 'python_version' in selected
    assert 'discord-py==2.4.0' in selected and 'b' * 64 in selected
    assert 'unrelated' not in selected


@pytest.mark.parametrize('bad', [
    'huggingface-hub>=0.1 --hash=sha256:' + 'a' * 64,
    'huggingface-hub==0.1',
    'huggingface-hub==0.1 --hash=sha256:' + 'a' * 63,
    'unrelated==0.1 --hash=sha256:' + 'a' * 64,
])
def test_missing_or_unlocked_repair_package_is_an_error(bad):
    helper = module('locked_pack_inputs', 'scripts/pack/locked_runtime_inputs.py')
    with pytest.raises(ValueError):
        helper.select_requirements(bad, ('huggingface_hub',))


def test_implicit_wheel_selection_rejects_multiple_versions(tmp_path, monkeypatch):
    common = module('locked_pack_common', 'scripts/pack/build_common.py')
    monkeypatch.setattr(common, 'REPO_ROOT', tmp_path)
    (tmp_path / 'dist').mkdir()
    for version in ('1.2.3', '1.2.4'):
        (tmp_path / 'dist' / f'lantai-{version}-py3-none-any.whl').write_bytes(b'w')
    with pytest.raises(ValueError):
        common._pick_wheel(None)


def test_pack_refuses_to_overwrite_an_existing_archive(tmp_path, monkeypatch):
    import sys
    common = module('locked_pack_common', 'scripts/pack/build_common.py')
    monkeypatch.setattr(common, 'REPO_ROOT', tmp_path)
    archive = tmp_path / 'env.zip'
    archive.write_bytes(b'previous accepted archive')
    wheel = tmp_path / 'lantai-1.2.3-py3-none-any.whl'
    wheel.write_bytes(b'w')
    calls = []
    monkeypatch.setattr(common, '_run', lambda *args, **kw: calls.append(args))
    monkeypatch.setattr(sys, 'argv', ['build_common.py', '--output', str(archive), '--wheel', str(wheel)])
    with pytest.raises(FileExistsError):
        common.main()
    assert archive.read_bytes() == b'previous accepted archive' and not calls
