"""Finite, offline LTA-022 acceptance suite using the actual public pipeline.

No downloaded or synthetic binary is executed. Original RED fixtures are kept
separately and unchanged. `_resolve_release_asset` is the proposed pure trust
lookup seam; the catalog test reports its absence explicitly, not as evidence
that runtime validation of a supplied record already exists.
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import inspect
import io
import json
import os
import socket
import stat
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

import lantai.local_models.llamacpp as module
from lantai.local_models.llamacpp import LlamaCppBackend
from lantai.local_models.download_manager import DownloadTaskResult, DownloadTaskStatus
from lantai.utils.command_runner import CommandExecutionError, CommandResult


BASE_URL = "https://github.com/ggml-org/llama.cpp/releases/download"
TAG = "b8744"
HERE = Path(__file__).parent
SOURCE_COMMIT = "d7ff074c87ecacd57d5760e2f678866ba9fe7149"
SYNTHETIC_LICENSE_SECTIONS = (
    ("llama.cpp", b"Synthetic llama.cpp license fixture.\nCopyright fixture author A.\nPermission to inspect inert fixture bytes only.\nEnd llama.cpp fixture license.\n"),
    ("ggml", b"Synthetic ggml license fixture.\nCopyright fixture author B.\nNo executable program is included in these fixtures.\nEnd ggml fixture license.\n"),
    ("third-party-one", b"Synthetic dependency one license fixture.\nCopyright fixture author C.\nThis body is an offline admission input only.\nEnd dependency one fixture license.\n"),
    ("third-party-two", b"Synthetic dependency two license fixture.\nCopyright fixture author D.\nThis text is not a notice for a released product.\nEnd dependency two fixture license.\n"),
)
SYNTHETIC_LICENSE_NOTICE = b"".join(
    b"=== " + name.encode("ascii") + b" ===\n" + body
    for name, body in SYNTHETIC_LICENSE_SECTIONS
)
SYNTHETIC_LICENSE_EMBEDDED = b"\0" + b"\0".join(body for _, body in SYNTHETIC_LICENSE_SECTIONS) + b"\0"


def synthetic_reviewed_license_manifest():
    return {
        "source_commit": SOURCE_COMMIT,
        "notice_sha256": hashlib.sha256(SYNTHETIC_LICENSE_NOTICE).hexdigest(),
        "notice_size_bytes": len(SYNTHETIC_LICENSE_NOTICE),
        "sections": [dict(name=name, sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body), text=body.decode("ascii"))
                     for name, body in SYNTHETIC_LICENSE_SECTIONS],
    }


@pytest.fixture(autouse=True)
def forbid_real_network_and_execution(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("offline acceptance forbids network and binary execution")
    monkeypatch.setattr(module.httpx, "Client", denied)
    monkeypatch.setattr(module.httpx, "AsyncClient", denied)
    monkeypatch.setattr(module, "run_command_async", denied)
    monkeypatch.setattr(module, "start_command_async", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(module.system_info, "get_macos_version", lambda: (14, 0))



class _StagedHealthCommandDouble:
    """Substitute only the external process, retaining actual health admission."""
    def __init__(self):
        self.inputs = {}
        self.calls = []
        self.mode = "ok"
        self.license_assets = {}

    def register_license_asset(self, asset):
        fields = dataclasses.asdict(asset)
        self.license_assets[fields["sha256"]] = fields

    def reviewed_license_manifest(self, asset):
        fields = dataclasses.asdict(asset)
        if self.license_assets.get(fields.get("sha256")) != fields:
            raise ValueError("synthetic license catalog rejects an unadmitted asset")
        return synthetic_reviewed_license_manifest()

    def register(self, spec, transferred):
        self.inputs[Path(spec.task.payload["staging_dir"])] = (spec, transferred)

    def __call__(self, command, *, timeout, cwd=None, check=True, **kwargs):
        command = list(command)
        assert len(command) == 2 and command[1] in {"--version", "--license"}
        option = command[1]
        executable = Path(command[0])
        stage = executable.parent
        assert stage in self.inputs, "health command does not belong to an admitted fixture stage"
        spec, transferred = self.inputs[stage]
        fields = spec.task.payload["trusted_asset"]
        assert isinstance(fields, dict)
        expected_name = "llama-server.exe" if fields["os_name"] == "windows" else "llama-server"
        assert command == [str(stage / expected_name), option]
        assert str(cwd) == str(stage), "health command must use its own stage as cwd"
        assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0 < timeout <= 15
        assert check is False, "actual health function must handle a nonzero CommandResult"
        # Explicit branches avoid pytest assertion rewriting rendering env values.
        if set(kwargs) != {"env"}:
            raise AssertionError("finite health fixture permits only the declared sanitized environment option")
        environment = kwargs["env"]
        if not isinstance(environment, dict) or not all(isinstance(key, str) for key in environment):
            raise AssertionError("finite health command requires an environment mapping with string keys")
        if any(key.upper().startswith(("DYLD_", "LD_PRELOAD", "LD_LIBRARY_PATH"))
               for key in environment):
            raise AssertionError("downloaded health check inherited loader injection controls")
        assert fields["sha256"] == hashlib.sha256(transferred).hexdigest()
        assert fields["size_bytes"] == len(transferred)
        assert not stage.is_symlink() and stage.resolve() == stage
        assert stat.S_ISREG(executable.lstat().st_mode) and not executable.is_symlink()
        binary = executable.read_bytes()
        assert binary.startswith(static_header(fields["os_name"], fields["arch"], marker=b"")), "unvalidated platform/architecture reached health execution"
        if fields["os_name"] == "windows":
            with zipfile.ZipFile(io.BytesIO(transferred)) as archive:
                values = [archive.read(name) for name in archive.namelist() if Path(name).name == expected_name]
        else:
            with tarfile.open(fileobj=io.BytesIO(transferred), mode="r:gz") as archive:
                values = [archive.extractfile(item).read() for item in archive.getmembers()
                          if item.isfile() and Path(item.name).name == expected_name]
        assert values == [binary], "health executable bytes differ from the synthetic trusted archive"
        self.calls.append(dict(stage=str(stage), command=command, timeout=timeout, mode=self.mode))
        if option == "--license":
            for _, body in SYNTHETIC_LICENSE_SECTIONS:
                assert body in binary, "synthetic license catalog body is absent from admitted executable bytes"
            notice = SYNTHETIC_LICENSE_NOTICE.decode("ascii")
            if self.mode == "license_empty":
                return CommandResult(command, 0, "", "")
            if self.mode == "license_truncated":
                return CommandResult(command, 0, notice[:-37], "")
            if self.mode == "license_wrong":
                return CommandResult(command, 0, notice.replace("author A", "author Z", 1), "")
            if self.mode == "license_nonzero":
                return CommandResult(command, 9, notice, "synthetic license failure")
            return CommandResult(command, 0, notice, "")
        version = int(fields["tag"][1:])
        if self.mode == "timeout":
            raise CommandExecutionError(command, "synthetic staged version timeout") from subprocess.TimeoutExpired(command, timeout)
        if self.mode == "nonzero":
            return CommandResult(command, 7, "", f"version: {version} (synthetic failure)\n")
        if self.mode == "wrong_version":
            return CommandResult(command, 0, "", f"version: {version + 1} (synthetic wrong version)\n")
        assert self.mode == "ok" or self.mode.startswith("license_")
        return CommandResult(command, 0, "", f"version: {version} (synthetic verified fixture)\n")


@pytest.fixture(autouse=True)
def finite_staged_health_command(monkeypatch):
    substitute = _StagedHealthCommandDouble()
    # raising=False keeps the missing-health baseline observable as RED.
    monkeypatch.setattr(module, "run_command", substitute, raising=False)
    # This is only a finite synthetic trusted catalog, not a license validator.
    monkeypatch.setattr(module, "reviewed_license_manifest", substitute.reviewed_license_manifest, raising=False)
    return substitute


@dataclasses.dataclass(frozen=True)
class SyntheticTrustedAsset:
    tag: str
    os_name: str
    arch: str
    backend: str
    cuda_version: str | None
    file_name: str
    url: str
    sha256: str
    size_bytes: int

    # These aliases permit implementation choice without altering acceptance.
    @property
    def release_tag(self): return self.tag
    @property
    def os(self): return self.os_name
    @property
    def architecture(self): return self.arch
    @property
    def filename(self): return self.file_name
    def to_dict(self): return dataclasses.asdict(self)
    def model_dump(self, **kwargs): return self.to_dict()
    def __getitem__(self, name): return getattr(self, name)
    def get(self, name, default=None): return getattr(self, name, default)


class CapturingController:
    def __init__(self): self.specs = []
    def is_active(self): return False
    def start(self, spec): self.specs.append(spec)
    def cancel(self): pass


class Queue:
    def __init__(self): self.messages = []
    def put(self, value): self.messages.append(value)
    @property
    def result(self):
        values = [item["payload"] for item in self.messages if item["type"] == "result"]
        assert len(values) == 1, "worker must produce one terminal result"
        return DownloadTaskResult.from_dict(values[0])


def backend(target, os_name="linux", arch="x64", kind="cpu", cuda=None):
    instance = object.__new__(LlamaCppBackend)
    instance.os_name, instance.arch = os_name, arch
    instance.backend, instance.cuda_version = kind, cuda
    instance.target_dir = target
    instance._download_controller = CapturingController()
    return instance


def static_header(os_name, arch, marker=b"synthetic-not-executable"):
    """OS/architecture headers only: deliberately no executable program body."""
    if os_name == "linux":
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<HHI", header, 16, 2, 62 if arch == "x64" else 183, 1)
        struct.pack_into("<HHH", header, 52, 64, 56, 0)
        return bytes(header) + SYNTHETIC_LICENSE_EMBEDDED + marker
    if os_name == "macos":
        cpu = 0x01000007 if arch == "x64" else 0x0100000C
        return struct.pack("<IIIIIIII", 0xFEEDFACF, cpu, 3 if arch == "x64" else 0, 2, 0, 0, 0, 0) + SYNTHETIC_LICENSE_EMBEDDED + marker
    header = bytearray(512)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x80)
    header[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", header, 0x84, 0x8664 if arch == "x64" else 0xAA64, 0, 0, 0, 0, 240, 0x22)
    struct.pack_into("<H", header, 0x98, 0x20B)
    return bytes(header) + SYNTHETIC_LICENSE_EMBEDDED + marker


def archive_bytes(os_name, files, top_dir=""):
    output = io.BytesIO()
    if os_name == "windows":
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in files.items():
                archive.writestr(top_dir + name, value)
    else:
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, value in files.items():
                entry = tarfile.TarInfo(top_dir + name)
                entry.size, entry.mode = len(value), 0o755
                archive.addfile(entry, io.BytesIO(value))
    return output.getvalue() if os_name == "windows" else gzip.compress(output.getvalue(), mtime=0)


def good_archive(instance, marker=b"candidate", top_dir=""):
    server = "llama-server.exe" if instance.os_name == "windows" else "llama-server"
    return archive_bytes(instance.os_name, {
        server: static_header(instance.os_name, instance.arch, marker),
        "llama-cli.exe" if instance.os_name == "windows" else "llama-cli": static_header(instance.os_name, instance.arch),
    }, top_dir)


def trust_fixture(monkeypatch, instance, data):
    fields = dict(tag=TAG, os_name=instance.os_name, arch=instance.arch,
                  backend=instance.backend, cuda_version=instance.cuda_version,
                  file_name=instance._build_filename(TAG),
                  url=f"{BASE_URL}/{TAG}/{instance._build_filename(TAG)}",
                  sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    asset_type = getattr(module, "TrustedLlamaCppAsset", SyntheticTrustedAsset)
    signature = inspect.signature(asset_type)
    aliases = dict(release_tag=fields["tag"], os=fields["os_name"], architecture=fields["arch"], filename=fields["file_name"])
    available = dict(fields, **aliases)
    asset = asset_type(**{key: available[key] for key in signature.parameters if key in available})
    module.run_command.register_license_asset(asset)
    def lookup(base_url, tag):
        assert base_url == BASE_URL and tag == TAG
        return asset
    # On baseline this patch is ignored: the dedicated payload lock detects it.
    monkeypatch.setattr(instance, "_resolve_release_asset", lookup, raising=False)
    return asset, fields


def prepare(monkeypatch, instance, data):
    asset, fields = trust_fixture(monkeypatch, instance, data)
    instance.start_download(BASE_URL, TAG, chunk_size=23, timeout=1)
    return instance._download_controller.specs[-1], asset, fields


def run_worker(monkeypatch, spec, data, declared_length=None):
    module.run_command.register(spec, data)
    class Response:
        headers = {"Content-Length": str(len(data) if declared_length is None else declared_length)}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_bytes(self, chunk_size):
            for pos in range(0, len(data), chunk_size):
                yield data[pos:pos + chunk_size]
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, method, url, **kwargs):
            assert method == "GET" and url == spec.source
            return Response()
    monkeypatch.setattr(module.httpx, "Client", Client)
    queue = Queue()
    spec.task.target(spec.task.payload, queue)
    return queue.result


def install(monkeypatch, instance, data):
    spec, _, _ = prepare(monkeypatch, instance, data)
    worker = run_worker(monkeypatch, spec, data)
    result, _ = spec.task.finalize(worker)
    return spec, worker, result


def read_field(record, *names):
    for name in names:
        value = record.get(name) if isinstance(record, dict) else getattr(record, name, None)
        if value is not None: return value
    return None


def recursive_values(value):
    if dataclasses.is_dataclass(value): value = dataclasses.asdict(value)
    if hasattr(value, "model_dump"): value = value.model_dump()
    if isinstance(value, dict):
        for child in value.values(): yield from recursive_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value: yield from recursive_values(child)
    else:
        yield value


def test_builtin_trust_matches_independent_eight_asset_catalog(tmp_path):
    rows = json.loads((HERE / "official-b8744-trust-catalog.json").read_text())["supported_assets"]
    assert len(rows) == 8
    for row in rows:
        instance = backend(tmp_path / "bin", row["os"], row["architecture"], row["backend"], row["cuda_version"])
        lookup = getattr(instance, "_resolve_release_asset", None)
        assert callable(lookup), "missing pure trusted-release lookup: proposed interface gap"
        asset = lookup(BASE_URL, TAG)
        assert read_field(asset, "sha256") == row["sha256"]
        assert read_field(asset, "size_bytes", "size") == row["size_bytes"]
        assert read_field(asset, "file_name", "filename") == row["filename"]
        assert read_field(asset, "url") == row["url"]
        with pytest.raises((AttributeError, TypeError, ValueError)):
            if isinstance(asset, dict): asset["sha256"] = "0" * 64
            else: setattr(asset, "sha256", "0" * 64)


@pytest.mark.parametrize("base_url,tag", [(BASE_URL, "b999999"), ("https://release.invalid/download", TAG)])
def test_unknown_trust_is_rejected_before_starting_worker(tmp_path, base_url, tag):
    instance = backend(tmp_path / "bin")
    with pytest.raises((RuntimeError, ValueError)):
        instance.start_download(base_url, tag)
    assert not instance._download_controller.specs
    assert not list(tmp_path.glob(".llamacpp-*"))


def test_public_worker_payload_carries_independent_pin(tmp_path, monkeypatch):
    instance = backend(tmp_path / "bin")
    data = good_archive(instance)
    spec, _, fields = prepare(monkeypatch, instance, data)
    values = tuple(recursive_values(spec.task.payload))
    assert fields["sha256"] in values, "public pipeline loses trusted SHA before worker boundary"
    assert fields["size_bytes"] in values, "public pipeline loses trusted exact size before worker boundary"


@pytest.mark.parametrize("os_name,arch,top_dir", [("linux", "x64", ""), ("macos", "arm64", "llama-b8744/"), ("windows", "x64", "")])
def test_normal_offline_install_keeps_public_behavior(tmp_path, monkeypatch, os_name, arch, top_dir):
    instance = backend(tmp_path / "bin", os_name, arch)
    data = good_archive(instance, top_dir=top_dir)
    _, worker, result = install(monkeypatch, instance, data)
    assert worker.status == result.status == DownloadTaskStatus.COMPLETED
    assert result.local_path == str(instance.target_dir)
    assert instance.executable.read_bytes() == static_header(os_name, arch, b"candidate")
    assert instance.check_llamacpp_installation() == (True, "")


@pytest.mark.parametrize("mutation", ["sha", "short", "long"])
def test_integrity_failure_keeps_old_install_and_is_retryable(tmp_path, monkeypatch, mutation):
    instance = backend(tmp_path / "bin")
    original = good_archive(instance, marker=b"original")
    _, _, initial = install(monkeypatch, instance, original)
    assert initial.status == DownloadTaskStatus.COMPLETED
    old_bytes = instance.executable.read_bytes()
    trusted = good_archive(instance, marker=b"replacement")
    spec, _, _ = prepare(monkeypatch, instance, trusted)
    transfer = (good_archive(instance, marker=b"tampered") if mutation == "sha" else
                trusted[:-1] if mutation == "short" else trusted + b"trailing-untrusted-byte")
    worker = run_worker(monkeypatch, spec, transfer, declared_length=len(trusted))
    result, _ = spec.task.finalize(worker)
    assert result.status == DownloadTaskStatus.FAILED, f"{mutation} mismatch published as complete"
    assert instance.executable.read_bytes() == old_bytes
    spec.task.run_cleanup()
    _, _, retried = install(monkeypatch, instance, trusted)
    assert retried.status == DownloadTaskStatus.COMPLETED
    assert instance.executable.read_bytes() == static_header("linux", "x64", b"replacement")


def test_successful_replacement_removes_only_previous_install_files(tmp_path, monkeypatch):
    instance = backend(tmp_path / "bin")
    outside = tmp_path / "outside-owned-fixture"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"keep")
    old_data = archive_bytes("linux", {
        "llama-server": static_header("linux", "x64", b"old"),
        "llama-cli": static_header("linux", "x64"),
        "libold.so": static_header("linux", "x64", b"old-library"),
    })
    _, _, old = install(monkeypatch, instance, old_data)
    assert old.status == DownloadTaskStatus.COMPLETED
    _, _, new = install(monkeypatch, instance, good_archive(instance, marker=b"new"))
    assert new.status == DownloadTaskStatus.COMPLETED
    assert not (instance.target_dir / "libold.so").exists()
    assert (outside / "sentinel").read_bytes() == b"keep"


def _promotion_path_matches(value, expected, parent_identity, *, dir_fd=None):
    """Match a full path or a basename anchored to the original parent inode."""
    candidate = Path(os.fsdecode(value))
    if candidate.is_absolute():
        return candidate == expected
    if candidate != Path(expected.name) or dir_fd is None:
        return False
    try:
        descriptor = os.fstat(dir_fd)
    except (OSError, TypeError, ValueError):
        return False
    return (stat.S_ISDIR(descriptor.st_mode)
            and (descriptor.st_dev, descriptor.st_ino) == parent_identity)


def test_promotion_failure_restores_previous_verified_install(tmp_path, monkeypatch):
    instance = backend(tmp_path / "bin")
    _, _, old = install(monkeypatch, instance, good_archive(instance, marker=b"old"))
    assert old.status == DownloadTaskStatus.COMPLETED
    old_bytes = instance.executable.read_bytes()
    spec, _, _ = prepare(monkeypatch, instance, good_archive(instance, marker=b"new"))
    result = run_worker(monkeypatch, spec, good_archive(instance, marker=b"new"))
    stage = Path(spec.task.payload["staging_dir"])
    original_move, original_rename, original_replace = module.shutil.move, Path.rename, Path.replace
    original_os_rename, original_os_replace = os.rename, os.replace
    source_parent = stage.parent.stat(follow_symlinks=False)
    target_parent = instance.target_dir.parent.stat(follow_symlinks=False)
    source_parent_identity = (source_parent.st_dev, source_parent.st_ino)
    target_parent_identity = (target_parent.st_dev, target_parent.st_ino)
    invoked = []
    def fail_promotion(src, dst, *args, **kwargs):
        if (_promotion_path_matches(src, stage, source_parent_identity,
                                    dir_fd=kwargs.get("src_dir_fd"))
                and _promotion_path_matches(dst, instance.target_dir,
                                            target_parent_identity,
                                            dir_fd=kwargs.get("dst_dir_fd"))):
            invoked.append((src, dst))
            raise OSError("synthetic candidate promotion failure")
    def move(src, dst, *args, **kwargs):
        fail_promotion(src, dst, *args, **kwargs)
        return original_move(src, dst, *args, **kwargs)
    def rename(src, dst):
        fail_promotion(src, dst)
        return original_rename(src, dst)
    def replace(src, dst):
        fail_promotion(src, dst)
        return original_replace(src, dst)
    def os_rename(src, dst, *args, **kwargs):
        fail_promotion(src, dst, *args, **kwargs)
        return original_os_rename(src, dst, *args, **kwargs)
    def os_replace(src, dst, *args, **kwargs):
        fail_promotion(src, dst, *args, **kwargs)
        return original_os_replace(src, dst, *args, **kwargs)
    monkeypatch.setattr(module.shutil, "move", move)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(os, "rename", os_rename)
    monkeypatch.setattr(os, "replace", os_replace)
    final, _ = spec.task.finalize(result)
    assert invoked, "failure must occur at actual candidate promotion, after validation"
    assert final.status == DownloadTaskStatus.FAILED
    assert instance.executable.exists(), "previous installation was deleted before failed promotion"
    assert instance.executable.read_bytes() == old_bytes
    assert instance.check_llamacpp_installation() == (True, "")


def test_legacy_real_format_without_receipt_remains_unverified(tmp_path):
    instance = backend(tmp_path / "bin")
    instance.target_dir.mkdir()
    instance.executable.write_bytes(static_header("linux", "x64"))
    installed, message = instance.check_llamacpp_installation()
    assert not installed, "legacy bytes were blessed solely by file/header existence"
    assert message, "unverified installation needs an observable diagnostic"


def test_installed_binary_mutation_invalidates_receipt(tmp_path, monkeypatch):
    instance = backend(tmp_path / "bin")
    _, _, result = install(monkeypatch, instance, good_archive(instance))
    assert result.status == DownloadTaskStatus.COMPLETED
    assert instance.check_llamacpp_installation() == (True, "")
    instance.executable.write_bytes(static_header("linux", "x64", b"changed-after-install"))
    installed, message = instance.check_llamacpp_installation()
    assert not installed, "OS/arch-preserving local mutation still reported verified"
    assert message


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_installation_receipt_fault_does_not_bless_existing_binary(tmp_path, monkeypatch, fault):
    instance = backend(tmp_path / "bin")
    _, _, result = install(monkeypatch, instance, good_archive(instance))
    assert result.status == DownloadTaskStatus.COMPLETED
    payload_files = {instance.executable, instance.target_dir / "llama-cli"}
    # The fresh isolated fixture contains only two archive files; other files
    # must be installation metadata. This does not prescribe its filename.
    metadata = [path for path in instance.target_dir.parent.rglob("*")
                if path.is_file() and not path.is_symlink() and path not in payload_files]
    assert metadata, "successful install created no durable verification receipt"
    for path in metadata:
        if fault == "missing": path.unlink()
        else: path.write_bytes(b"{truncated-installation-receipt")
    installed, message = instance.check_llamacpp_installation()
    assert not installed, f"{fault} receipt left installation eligible for execution"
    assert message


@pytest.mark.parametrize("os_name,arch", [("linux", "x64"), ("macos", "arm64"), ("windows", "x64")])
def test_trusted_archive_with_wrong_binary_arch_cannot_replace_old(tmp_path, monkeypatch, os_name, arch):
    instance = backend(tmp_path / "bin", os_name, arch)
    _, _, old = install(monkeypatch, instance, good_archive(instance, marker=b"old"))
    assert old.status == DownloadTaskStatus.COMPLETED
    old_bytes = instance.executable.read_bytes()
    wrong_arch = "arm64" if arch == "x64" else "x64"
    candidate = archive_bytes(os_name, {instance.executable.name: static_header(os_name, wrong_arch)})
    _, _, final = install(monkeypatch, instance, candidate)
    assert final.status == DownloadTaskStatus.FAILED, "archive hash alone did not bind executable OS/arch"
    assert instance.executable.read_bytes() == old_bytes


def test_foreign_worker_result_identity_cannot_promote_candidate(tmp_path, monkeypatch):
    instance = backend(tmp_path / "bin")
    _, _, old = install(monkeypatch, instance, good_archive(instance, marker=b"old"))
    assert old.status == DownloadTaskStatus.COMPLETED
    old_bytes = instance.executable.read_bytes()
    spec, _, _ = prepare(monkeypatch, instance, good_archive(instance, marker=b"new"))
    actual = run_worker(monkeypatch, spec, good_archive(instance, marker=b"new"))
    assert actual.status == DownloadTaskStatus.COMPLETED
    foreign = tmp_path / "foreign-fixture"
    foreign.mkdir()
    (foreign / "sentinel").write_bytes(b"keep")
    forged = DownloadTaskResult(status=DownloadTaskStatus.COMPLETED, local_path=str(foreign))
    final, _ = spec.task.finalize(forged)
    assert final.status == DownloadTaskStatus.FAILED, "completion from another candidate was accepted"
    assert instance.executable.read_bytes() == old_bytes
    assert (foreign / "sentinel").read_bytes() == b"keep"


def test_target_symlink_is_rejected_before_worker_start(tmp_path):
    outside = tmp_path / "outside-fixture"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"keep")
    target = tmp_path / "bin"
    target.symlink_to(outside, target_is_directory=True)
    instance = backend(target)
    with pytest.raises((RuntimeError, ValueError)):
        instance.start_download(BASE_URL, TAG)
    assert not instance._download_controller.specs
    assert (outside / "sentinel").read_bytes() == b"keep"


def test_parent_symlink_exchange_cannot_delete_unowned_directory(tmp_path, monkeypatch):
    owned = tmp_path / "provider-owned"
    owned.mkdir()
    instance = backend(owned / "bin")
    _, _, old = install(monkeypatch, instance, good_archive(instance, marker=b"old"))
    assert old.status == DownloadTaskStatus.COMPLETED
    spec, _, _ = prepare(monkeypatch, instance, good_archive(instance, marker=b"new"))
    worker = run_worker(monkeypatch, spec, good_archive(instance, marker=b"new"))
    assert worker.status == DownloadTaskStatus.COMPLETED
    outside = tmp_path / "outside-fixture"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "sentinel").write_bytes(b"keep")
    parked = tmp_path / "original-provider"
    owned.rename(parked)
    owned.symlink_to(outside, target_is_directory=True)
    final, _ = spec.task.finalize(worker)
    assert final.status == DownloadTaskStatus.FAILED
    assert (outside / "bin" / "sentinel").exists(), "directory exchange redirected deletion outside the captured owner"
    assert (outside / "bin" / "sentinel").read_bytes() == b"keep"
    assert (parked / "bin" / "llama-server").read_bytes() == static_header("linux", "x64", b"old")


def test_cleanup_removes_only_owned_stage(tmp_path, monkeypatch):
    instance = backend(tmp_path / "bin")
    spec, _, _ = prepare(monkeypatch, instance, good_archive(instance))
    stage = Path(spec.task.payload["staging_dir"])
    assert stage.is_dir(), "public admission must create its identity-bound stage"
    (stage / "partial").write_bytes(b"partial")
    outside = tmp_path / "outside-fixture"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"keep")
    spec.task.run_cleanup()
    assert not stage.exists()
    assert (outside / "sentinel").read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["zip_parent", "zip_external_link", "tar_duplicate"])
def test_archive_type_and_ambiguous_paths_are_rejected_before_publication(tmp_path, kind):
    destination = tmp_path / "stage"
    destination.mkdir()
    if kind == "tar_duplicate":
        path = tmp_path / "duplicate.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for marker in [b"first", b"second"]:
                member = tarfile.TarInfo("llama-server")
                member.size = len(marker)
                archive.addfile(member, io.BytesIO(marker))
    else:
        path = tmp_path / "unsafe.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("llama-server.exe", static_header("windows", "x64"))
            if kind == "zip_parent":
                archive.writestr("../outside", b"must-not-write")
            else:
                member = zipfile.ZipInfo("external-link")
                member.create_system = 3
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(member, str(tmp_path / "outside-owned-fixture"))
    failure = None
    try:
        LlamaCppBackend._extract_archive(path, destination)
    except Exception as error:
        failure = error
    assert failure is not None, f"{kind} was silently accepted"
    assert not list(destination.iterdir()), "unsafe/ambiguous archive copied files before whole-archive validation"


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "wrong_version"])
def test_staged_health_failure_keeps_old_receipt_and_cleans_only_owned_stage(
    tmp_path, monkeypatch, finite_staged_health_command, failure,
):
    instance = backend(tmp_path / "bin")
    _, _, previous = install(monkeypatch, instance, good_archive(instance, marker=b"old-health-verified"))
    assert previous.status == DownloadTaskStatus.COMPLETED
    assert instance.check_llamacpp_installation() == (True, "")
    old_files = {str(path.relative_to(instance.target_dir)): path.read_bytes()
                 for path in instance.target_dir.rglob("*") if path.is_file()}
    assert any(name not in {"llama-server", "llama-cli"} for name in old_files), "initial verified installation has no receipt"
    outside = tmp_path / "unrelated-owned-fixture"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"must-retain")
    candidate = good_archive(instance, marker=b"candidate-health-failure")
    spec, _, _ = prepare(monkeypatch, instance, candidate)
    stage = Path(spec.task.payload["staging_dir"])
    finite_staged_health_command.mode = failure
    worker = run_worker(monkeypatch, spec, candidate)
    final, _ = spec.task.finalize(worker)
    assert final.status == DownloadTaskStatus.FAILED, f"{failure} staged health result was published as complete"
    assert final.error, "failed staged health requires a visible diagnostic"
    assert any(call["stage"] == str(stage) and call["mode"] == failure
               for call in finite_staged_health_command.calls), "actual staged command boundary was not exercised"
    assert {str(path.relative_to(instance.target_dir)): path.read_bytes()
            for path in instance.target_dir.rglob("*") if path.is_file()} == old_files, "health failure changed previous verified bytes or receipt"
    assert instance.check_llamacpp_installation() == (True, "")
    spec.task.run_cleanup()
    assert not stage.exists(), "failed health left its owned candidate stage"
    assert (outside / "sentinel").read_bytes() == b"must-retain"


def test_server_and_receipt_digest_co_mutation_cannot_forge_verified_install(tmp_path, monkeypatch):
    instance = backend(tmp_path / "bin")
    _, _, previous = install(monkeypatch, instance, good_archive(instance, marker=b"original-trusted"))
    assert previous.status == DownloadTaskStatus.COMPLETED
    assert instance.check_llamacpp_installation() == (True, "")
    receipts = []
    for path in instance.target_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            record = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            continue
        if isinstance(record, dict) and isinstance(record.get("files"), dict) and instance.executable.name in record["files"]:
            receipts.append((path, record))
    assert len(receipts) == 1, "normal install needs one file verification receipt"
    receipt_path, receipt = receipts[0]
    original_asset = dict(receipt["asset"])
    changed = static_header("linux", "x64", b"coordinated-receipt-tampering")
    instance.executable.write_bytes(changed)
    receipt["files"][instance.executable.name]["sha256"] = hashlib.sha256(changed).hexdigest()
    receipt["files"][instance.executable.name]["size_bytes"] = len(changed)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    assert receipt["asset"] == original_asset, "trusted source identity must remain unchanged in this forgery attempt"
    verified, message = instance.check_llamacpp_installation()
    assert not verified, "self-recorded receipt digest blessed binary bytes absent from the trusted archive"
    assert message, "coordinated file/receipt tampering requires an observable diagnostic"
