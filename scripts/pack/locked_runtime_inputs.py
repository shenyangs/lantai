#!/usr/bin/env python3
"""Build and repair from hash-bound inputs without dependency resolution."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path


def canonical_name(value):
    return re.sub(r'[-_.]+', '-', value).lower()


def select_requirements(text, names):
    wanted = {canonical_name(name) for name in names}
    selected, found = [], set()
    # uv exports continuations and environment markers. Preserve each locked
    # alternative verbatim; pip evaluates markers for the actual interpreter.
    for line in re.sub(r'\\\r?\n\s*', ' ', text).splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s|;|$)', line)
        leading = re.match(r'^([A-Za-z0-9_.-]+)', line)
        name = canonical_name(leading.group(1)) if leading else ''
        if name not in wanted:
            continue
        if not match or not re.search(r'--hash=sha256:[a-f0-9]{64}(?:\s|$)', line):
            raise ValueError(f'Repair requirement is not pinned and hashed: {name}')
        if any(not re.fullmatch(r'[a-f0-9]{64}', value)
               for value in re.findall(r'--hash=sha256:(\S+)', line)):
            raise ValueError(f'Invalid repair digest: {name}')
        selected.append(line)
        found.add(name)
    if found != wanted:
        raise ValueError('Missing locked repair packages: ' + ', '.join(sorted(wanted - found)))
    return '\n'.join(selected) + '\n'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for value in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(value)
    return digest.hexdigest()


def verify_wheel(wheel, expected_sha, expected_version):
    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(metadata) != 1:
            raise ValueError('Wheel must have exactly one distribution metadata record')
        package = Parser().parsestr(archive.read(metadata[0]).decode('utf-8'))
        if canonical_name(package['Name']) != 'lantai' or package['Version'] != expected_version:
            raise ValueError('Wheel distribution identity differs from independently expected version')
        identity = json.loads(archive.read('lantai/console/release-provenance.json'))
        if identity.get('gitSha') != expected_sha or identity.get('version') != expected_version:
            raise ValueError('Wheel embedded build identity differs from expected source/version')
    return {'path': str(wheel), 'sha256': sha256(wheel), 'sourceSha': expected_sha, 'version': expected_version}


def bootstrap(uv, requirements, report):
    # The env was created without conda pip/setuptools/wheel. Seed only pip
    # using uv, then use that pip to record every exact packaging-tool input.
    seed = select_requirements(requirements.read_text(encoding='utf-8'), ('pip',))
    with tempfile.TemporaryDirectory(prefix='lantai-pack-bootstrap-') as directory:
        path = Path(directory) / 'pip.lock.txt'
        path.write_text(seed, encoding='utf-8')
        subprocess.run([uv, 'pip', 'install', '--python', sys.executable,
                        '--require-hashes', '--no-deps', '-r', str(path)], check=True)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--require-hashes', '--no-deps',
                    '--only-binary=:all:', '--force-reinstall', '-r', str(requirements),
                    '--report', str(report)], check=True)


def build_wheel(root, output, uv, requirements):
    if output.exists():
        raise FileExistsError('Wheel output must be a new owned directory')
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix='lantai-wheel-tools-') as temporary:
        env_root = Path(temporary) / 'env'
        subprocess.run([uv, 'venv', '--python', sys.executable, '--no-python-downloads',
                        str(env_root)], check=True)
        python = env_root / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        subprocess.run([str(python), str(Path(__file__).resolve()), 'bootstrap',
                        '--uv', uv, '--requirements', str(requirements),
                        '--report', str(output / 'packaging-tools-install.json')], check=True)
        # Calling the pinned backend directly prevents an implicit PEP 517
        # online build environment from resolving a second set of tools.
        code = ('import setuptools.build_meta as b; import sys; '
                'b.build_wheel(sys.argv[1]); b.build_sdist(sys.argv[1])')
        subprocess.run([str(python), '-c', code, str(output)], cwd=root, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    select = sub.add_parser('select-repair')
    select.add_argument('--requirements', type=Path, required=True)
    select.add_argument('--output', type=Path, required=True)
    select.add_argument('--package', action='append', required=True)
    seed = sub.add_parser('bootstrap')
    seed.add_argument('--uv', required=True)
    seed.add_argument('--requirements', type=Path, required=True)
    seed.add_argument('--report', type=Path, required=True)
    build = sub.add_parser('build-wheel')
    build.add_argument('--root', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--uv', required=True)
    build.add_argument('--requirements', type=Path, required=True)
    verify = sub.add_parser('verify-wheel')
    verify.add_argument('--wheel', type=Path, required=True)
    verify.add_argument('--source-sha', required=True)
    verify.add_argument('--version', required=True)
    args = parser.parse_args()
    if args.command == 'select-repair':
        value = select_requirements(args.requirements.read_text(encoding='utf-8'), args.package)
        args.output.write_text(value, encoding='utf-8')
    elif args.command == 'bootstrap':
        bootstrap(args.uv, args.requirements, args.report)
    elif args.command == 'build-wheel':
        build_wheel(args.root.resolve(), args.output.resolve(), args.uv, args.requirements.resolve())
    else:
        print(json.dumps(verify_wheel(args.wheel, args.source_sha, args.version)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
