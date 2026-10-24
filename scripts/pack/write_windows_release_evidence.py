#!/usr/bin/env python3
"""Record the actual repaired runtime and final signed Windows installer."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from locked_runtime_inputs import canonical_name, sha256
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_build_identity import identity_digest, validate_identity


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def file_record(path):
    reject_links(path)
    require(path.is_file() and not path.is_symlink(), f'Missing regular file: {path}')
    return {'name': path.name, 'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha256(path)}


def reject_links(path):
    # Keep the original path until checking every ancestor. Resolving first
    # would hide directory symlinks or Windows junction/reparse redirection.
    for item in (path, *path.parents):
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) &
                getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)),
                f'Runtime file or ancestor is a link/reparse point: {item}')


def pe_arch(path):
    with path.open('rb') as handle:
        require(handle.read(2) == b'MZ', f'Not a PE binary: {path}')
        handle.seek(0x3c)
        offset = handle.read(4)
        require(len(offset) == 4, 'Truncated PE offset')
        handle.seek(struct.unpack('<I', offset)[0])
        require(handle.read(4) == b'PE\0\0', f'Malformed PE header: {path}')
        machine = struct.unpack('<H', handle.read(2))[0]
    require(machine in {0x14c, 0x8664, 0xaa64}, f'Unknown PE machine: {machine}')
    return {0x14c: 'x86', 0x8664: 'x64', 0xaa64: 'arm64'}[machine]


def safe_origin(url):
    value = urlsplit(url)
    require(not value.username and not value.password and not value.query, 'Credential-bearing origin URL is prohibited')
    require(value.scheme in {'https', 'file'}, 'Origin URL must be local or HTTPS')
    return url


def pip_origins(inputs):
    result = {}
    for label in ('tools', 'runtime', 'lantai', 'repair'):
        report = read_json(inputs / f'{label}-install.json')
        require(isinstance(report.get('install'), list), f'Missing actual pip install report: {label}')
        for entry in report['install']:
            meta, download = entry['metadata'], entry['download_info']
            digest = download.get('archive_info', {}).get('hashes', {}).get('sha256')
            require(bool(re.fullmatch('[a-f0-9]{64}', str(digest))), f'Missing installed source digest: {meta["name"]}')
            result[canonical_name(meta['name'])] = {
                'version': meta['version'], 'type': 'python-distribution',
                'url': safe_origin(download['url']), 'sha256': digest,
            }
    return result


def validate_installed_identity(payload, expected_sha, version):
    require(pe_arch(payload / 'python.exe') == 'x64', 'Packed Python must be x64')
    identity_path = payload / 'Lib/site-packages/lantai/console/release-provenance.json'
    identity = read_json(identity_path)
    require(identity.get('gitSha') == expected_sha and identity.get('version') == version,
            'Installed frontend provenance does not match source/version')
    top = read_json(payload / 'release-provenance.json')
    require(top == identity, 'Payload and embedded frontend provenance differ')
    return identity


def installed_inventory(payload, inputs):
    require(Path(sys.prefix).resolve() == payload.resolve(), 'Inventory must run inside the actual packed interpreter')
    require(sys.platform == 'win32' and struct.calcsize('P') == 8, 'Actual x64 Windows interpreter required')
    origins = pip_origins(inputs)
    components, owners = [], {}

    def actual_files(paths, owner):
        records = []
        for path in paths:
            # A distribution record may mention absent optional bytecode, but
            # missing runtime/license/source files must stop packaging.
            if not path.exists() and path.suffix == '.pyc':
                continue
            reject_links(path)
            path = Path(os.path.abspath(path))
            require(path.resolve().is_relative_to(payload.resolve()) and not path.is_symlink(),
                    'Distribution file escaped packed prefix')
            record = file_record(path)
            record['path'] = path.relative_to(payload).as_posix()
            owners.setdefault(record['path'], []).append(owner)
            records.append(record)
        return records

    for path in sorted((payload / 'conda-meta').glob('*.json')):
        meta = read_json(path)
        origin = {'type': 'conda-package', 'url': safe_origin(meta['url']),
                  'sha256': meta.get('sha256'), 'md5': meta.get('md5')}
        license_value = meta.get('license') or 'UNVERIFIED'
        component = {'type': 'library', 'name': meta['name'], 'version': meta['version'],
                     'source': origin, 'license': license_value,
                     'releasable': license_value != 'UNVERIFIED' and bool(re.fullmatch('[a-f0-9]{64}', str(meta.get('sha256', ''))))}
        component['files'] = actual_files([payload / value.replace('\\', '/') for value in meta['files']], component)
        components.append(component)
    for dist in sorted(importlib.metadata.distributions(), key=lambda d: canonical_name(d.metadata['Name'])):
        name, version = canonical_name(dist.metadata['Name']), dist.version
        # Conda distributions have their own explicit origins and file lists.
        if name not in origins:
            require(any(c['name'] == name and c['version'] == version for c in components),
                    f'Installed Python distribution has no bound source: {name} {version}')
            continue
        origin = origins[name]
        require(origin['version'] == version, f'Installed version differs from actual input report: {name}')
        files = [Path(dist.locate_file(value)) for value in (dist.files or [])]
        license_paths = [path for path in files if re.search(r'(?i)(license|copying|notice)', path.name)]
        license_value = dist.metadata.get('License-Expression') or dist.metadata.get('License') or (
            'SEE-INCLUDED-LICENSE-FILES' if license_paths else 'UNVERIFIED')
        component = {'type': 'library', 'name': name, 'version': version, 'source': origin,
                     'license': license_value, 'releasable': license_value != 'UNVERIFIED'}
        component['files'] = actual_files(files, component)
        component['licenseFiles'] = [Path(os.path.abspath(path)).relative_to(payload).as_posix() for path in license_paths]
        components.append(component)
    binaries = []
    for path in sorted(payload.rglob('*')):
        if path.suffix.lower() not in {'.exe', '.dll', '.pyd'}:
            continue
        relative = path.relative_to(payload).as_posix()
        owner_list = owners.get(relative, [])
        # First-party Python metadata cannot establish redistribution rights
        # for a bundled foreign binary (including the OfficeCLI libomp DLL).
        owner = owner_list[0] if len(owner_list) == 1 and owner_list[0]['name'] != 'lantai' else None
        binaries.append({
            **file_record(path), 'path': relative, 'arch': pe_arch(path),
            'version': owner['version'] if owner else 'UNVERIFIED',
            'license': owner['license'] if owner else 'UNVERIFIED',
            'source': owner['source'] if owner else {'type': 'unverified-bundled-binary'},
            'releasable': bool(owner and owner['releasable']),
        })
    require(bool(binaries), 'Actual runtime has no binary inventory')
    return components, binaries


def write_evidence(args):
    require(sys.platform == 'win32', 'Windows release evidence requires actual Windows')
    source_sha, version = args.source_sha, args.version
    root, payload, inputs, output = args.root.resolve(), args.payload.resolve(), args.inputs.resolve(), args.output.resolve()
    require(subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip() == source_sha,
            'Source changed during Windows build')
    dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True).strip())
    require(not dirty, 'Source became dirty during Windows build')
    identity = validate_installed_identity(payload, source_sha, version)
    validate_identity(identity, source_sha, version, public=not args.artifact_only)
    require(identity.get('artifactOnly') is args.artifact_only and identity.get('candidateId') == args.candidate_id,
            'Actual payload candidate identity differs from requested build mode')
    build_identity_sha = identity_digest(identity)
    require(importlib.metadata.version('lantai') == version, 'Installed Python distribution version differs')
    receipt = read_json(output / 'lantai-windows-x64-signing-receipt.json')
    installer = output / f'Lantai-Setup-{version}.exe'
    require(receipt['artifact'] == file_record(installer), 'Final signed installer changed before manifest')
    require(receipt['source']['sha'] == source_sha and receipt['release']['version'] == version and
            receipt['status'] == 'Valid' and receipt['verificationExitCode'] == 0 and receipt['timestampPresent'] is True,
            'Actual signing receipt is not complete')
    require(receipt.get('buildIdentitySha256') == build_identity_sha,
            'Signed PE identity does not bind the actual payload provenance')
    components, binaries = installed_inventory(payload, inputs)
    if not args.artifact_only:
        require(identity.get('canonicalMain') is True and not args.candidate_id, 'Official Windows build lacks main authority')
        require(all(item['releasable'] for item in binaries), 'Unverified Windows binary redistribution prevents official release')
    sbom_path = output / 'lantai-windows-x64-sbom.cdx.json'
    sbom = {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
            'metadata': {'timestamp': datetime.now(timezone.utc).isoformat(),
                         'component': {'type': 'application', 'name': 'Lantai', 'version': version}},
            'components': []}
    for component in components:
        files = component['files']
        sbom['components'].append({
            'type': component['type'], 'name': component['name'], 'version': component['version'],
            'properties': [{'name': 'lantai:installed-files', 'value': json.dumps(files, sort_keys=True)},
                           {'name': 'lantai:actual-source', 'value': json.dumps(component['source'], sort_keys=True)},
                           {'name': 'lantai:license', 'value': component['license']}],
        })
    write_json(sbom_path, sbom)
    supply = {'schema': 'com.lantai.release-supply-chain-manifest.v1', 'source': {'sha': source_sha},
              'release': {'version': version, 'arch': 'x64', 'platform': 'win32'},
              'sbom': file_record(sbom_path), 'binarySources': binaries,
              'inputLocks': [file_record(root / 'uv.lock'), file_record(root / 'scripts/pack/packaging-tools.lock.txt'),
                             file_record(inputs / 'runtime.lock.txt'), file_record(inputs / 'repair.lock.txt')],
              'installedDistributionCount': len(components), 'provenance': identity,
              'provenanceSha256': build_identity_sha}
    supply_path = output / 'lantai-windows-x64-supply-chain-manifest.json'
    write_json(supply_path, supply)
    manifest = {'schema': 'com.lantai.release-artifact-manifest.v1',
                'source': {'sha': source_sha, 'tag': args.tag, 'dirty': False,
                           'sourceBranch': identity['sourceBranch'], 'canonicalMain': identity['canonicalMain']},
                'release': {**supply['release'], 'artifactOnly': args.artifact_only, 'candidateId': args.candidate_id},
                'artifacts': {'installer': file_record(installer)}, 'supplyChainManifest': supply,
                'signing': {'mode': 'authenticode', 'evidence': file_record(output / 'lantai-windows-x64-signing-receipt.json')}}
    write_json(output / 'lantai-windows-x64-artifact-manifest.json', manifest)


def write_json(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'payload', 'inputs', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--candidate-id')
    parser.add_argument('--artifact-only', action='store_true')
    write_evidence(parser.parse_args())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
