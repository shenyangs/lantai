"""No-follow installation boundaries and static receipt validation.

POSIX operations are anchored to an opened directory, including rollback and
cleanup. On platforms without dirfd support we also reject reparse points and
check the captured ancestor identities at each boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import zipfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .llamacpp_assets import TrustedLlamaCppAsset

RECEIPT_NAME = ".lantai-llamacpp-install.json"
NOTICES_NAME = ".lantai-llamacpp-notices.txt"
MAX_FILES = 4096
MAX_UNPACKED_BYTES = 2 * 1024**3
_DIRFD = os.name == "posix" and hasattr(os, "O_NOFOLLOW")


def identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _plain_directory(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def absolute_path(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    # macOS owns these system aliases. No user-controlled symlink is resolved.
    if os.name == "posix" and path.parts[1:2] in [("var",), ("tmp",), ("etc",)]:
        prefix = Path("/") / path.parts[1]
        if prefix.is_symlink() and os.path.realpath(prefix) == "/private" + str(prefix):
            path = Path("/private") / path.relative_to("/")
    return path


class DirectoryAnchor:
    """An exact parent directory and the identities of all its ancestors."""

    def __init__(self, path: Path, *, expected: dict[str, Any] | None = None,
                 create: bool = False):
        self.path = absolute_path(path)
        self.fd: int | None = None
        self.chain: list[list[int]] = []
        try:
            current = Path(self.path.anchor)
            if _DIRFD:
                self.fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                self.chain.append(list(identity(os.fstat(self.fd))))
                for part in self.path.parts[1:]:
                    if create:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=self.fd)
                        except FileExistsError:
                            pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=self.fd)
                    os.close(self.fd)
                    self.fd = child
                    self.chain.append(list(identity(os.fstat(child))))
            else:
                self.chain.append(list(identity(current.lstat())))
                for part in self.path.parts[1:]:
                    current /= part
                    if create and not os.path.lexists(current):
                        current.mkdir(mode=0o700)
                    info = current.lstat()
                    if not _plain_directory(info):
                        raise ValueError("Installation ancestors must be real directories")
                    self.chain.append(list(identity(info)))
            if expected is not None and self.snapshot() != expected:
                raise ValueError("Installation parent directory identity changed")
        except BaseException:
            self.close()
            raise

    def snapshot(self) -> dict[str, Any]:
        return {"path": str(self.path), "chain": self.chain}

    def assert_current(self) -> None:
        with DirectoryAnchor(self.path, expected=self.snapshot()):
            pass

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> DirectoryAnchor:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @staticmethod
    def _name(name: str) -> str:
        if name in {"", ".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Expected one installation path component")
        return name

    def info(self, name: str) -> os.stat_result | None:
        name = self._name(name)
        try:
            if self.fd is not None:
                return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return (self.path / name).lstat()
        except FileNotFoundError:
            return None

    def require_dir(self, name: str, expected: tuple[int, int] | None = None) -> tuple[int, int]:
        info = self.info(name)
        if info is None or not _plain_directory(info):
            raise ValueError("Installation entry is not a real directory")
        found = identity(info)
        if expected is not None and found != expected:
            raise ValueError("Installation entry identity changed")
        return found

    def mkdir(self, name: str) -> None:
        self.assert_current()
        if self.fd is not None:
            os.mkdir(self._name(name), mode=0o700, dir_fd=self.fd)
        else:
            (self.path / self._name(name)).mkdir(mode=0o700)

    def rename(self, src: str, dst: str, expected: tuple[int, int], *, rollback: bool = False) -> None:
        if not rollback:
            self.assert_current()
        self.require_dir(src, expected)
        if self.info(dst) is not None:
            raise ValueError("Installation publication destination is occupied")
        if self.fd is not None:
            os.rename(self._name(src), self._name(dst), src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            self.assert_current()
            os.rename(self.path / self._name(src), self.path / self._name(dst))
        self.sync()

    def sync(self) -> None:
        if self.fd is not None:
            os.fsync(self.fd)

    @contextmanager
    def file(self, name: str, mode: str = "rb") -> Iterator[Any]:
        self.assert_current()
        name = self._name(name)
        flags = os.O_RDONLY if mode == "rb" else os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(name if self.fd is not None else self.path / name, flags,
                     0o600, dir_fd=self.fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Installation file is not regular")
            with os.fdopen(fd, mode) as stream:
                fd = -1
                yield stream
        finally:
            if fd != -1:
                os.close(fd)

    def unlink(self, name: str) -> None:
        self.assert_current()
        if self.fd is not None:
            os.unlink(self._name(name), dir_fd=self.fd)
        else:
            (self.path / self._name(name)).unlink()
        self.sync()

    def remove_dir(self, name: str, expected: tuple[int, int]) -> None:
        self.assert_current()
        self.require_dir(name, expected)
        if self.fd is None:
            shutil.rmtree(self.path / self._name(name))
        else:
            _remove_tree_at(self.fd, self._name(name), expected)
        self.sync()


def _remove_tree_at(parent_fd: int, name: str, expected: tuple[int, int]) -> None:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        if identity(os.fstat(fd)) != expected:
            raise ValueError("Cleanup directory identity changed")
        for child in os.listdir(fd):
            info = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if _plain_directory(info):
                _remove_tree_at(fd, child, identity(info))
            else:
                # Unlink directory entries, never traverse their targets.
                os.unlink(child, dir_fd=fd)
        if identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != expected:
            raise ValueError("Cleanup directory identity changed")
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(fd)


def _member_name(name: str) -> str:
    if not name or "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        raise ValueError("Unsafe archive path")
    parts = name.rstrip("/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError("Unsafe archive parent path")
    result = str(PurePosixPath(name))
    if result == ".":
        return result
    if any(part in {"", ".", ".."} for part in result.split("/")):
        raise ValueError("Ambiguous archive path")
    return result


def _validated_archive_files(archive: Any, is_zip: bool, *,
                             allow_internal_aliases: bool = False) -> list[tuple[str, Any, int, int]]:
    entries = archive.infolist() if is_zip else archive.getmembers()
    if len(entries) > MAX_FILES:
        raise ValueError("Too many files in llama.cpp archive")
    members: dict[str, Any] = {}
    names: set[str] = set()
    for entry in entries:
        name = _member_name(entry.filename if is_zip else entry.name)
        directory = entry.is_dir() if is_zip else entry.isdir()
        mode = (entry.external_attr >> 16) if is_zip else entry.mode
        kind = stat.S_IFMT(mode) if is_zip else 0
        if is_zip:
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or entry.flag_bits & 1:
                raise ValueError("Archive links, special files and encryption are forbidden")
        elif not (entry.isfile() or directory or
                  (allow_internal_aliases and (entry.issym() or entry.islnk()))):
            raise ValueError("Archive links and special files are forbidden")
        if name == "." and directory:
            continue
        key = name.casefold()
        if key in names:
            raise ValueError("Duplicate archive member")
        names.add(key)
        if not directory:
            members[name] = entry

    files: list[tuple[str, Any, int, int]] = []
    total = 0
    for name, entry in members.items():
        source = entry
        if not is_zip:
            visited = {name}
            source_name = name
            while source.issym() or source.islnk():
                # Never create a filesystem link. Resolve the bounded member
                # graph and copy the regular source bytes under the alias name.
                link = _member_name(source.linkname)
                target = (str(PurePosixPath(source_name).parent / link)
                          if source.issym() else link)
                if target in visited or target not in members:
                    raise ValueError("Archive alias has a cyclic or missing internal target")
                visited.add(target)
                source_name, source = target, members[target]
            if not source.isfile():
                raise ValueError("Archive alias must resolve to a regular archive file")
        size = source.file_size if is_zip else source.size
        mode = (source.external_attr >> 16) if is_zip else source.mode
        total += size
        if size < 0 or total > MAX_UNPACKED_BYTES:
            raise ValueError("llama.cpp archive is too large")
        files.append((name, source, size, mode))
    if not files:
        raise ValueError("llama.cpp archive contains no files")
    parts = [name.split("/") for name, *_ in files]
    flat = all(len(item) == 1 for item in parts)
    nested = all(len(item) == 2 and item[0] == parts[0][0] for item in parts)
    if not (flat or nested):
        raise ValueError("Unexpected directory structure in llama.cpp archive")
    destinations = [item[-1] for item in parts]
    if {RECEIPT_NAME.casefold(), NOTICES_NAME.casefold()} & {name.casefold() for name in destinations}:
        raise ValueError("Archive cannot provide installation verification records")
    return [(target, entry, size, mode)
            for (_name, entry, size, mode), target in zip(files, destinations)]


def _verify_archive_source(source: Any, asset: TrustedLlamaCppAsset) -> os.stat_result:
    before = os.fstat(source.fileno())
    digest, downloaded = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        downloaded += len(chunk)
        if downloaded > asset.size_bytes:
            raise ValueError("Retained release archive exceeds the trusted size")
        digest.update(chunk)
    if downloaded != asset.size_bytes or digest.hexdigest() != asset.sha256:
        raise ValueError("Retained release archive does not match the reviewed SHA256 and size")
    source.seek(0)
    return before


def _assert_archive_unchanged(source: Any, before: os.stat_result) -> None:
    after = os.fstat(source.fileno())
    if (identity(before), before.st_size, before.st_mtime_ns) != (
        identity(after), after.st_size, after.st_mtime_ns):
        raise ValueError("Release archive changed while verifying")


def extract_archive(archive_path: Path, dest_dir: Path, *,
                    trusted_asset: TrustedLlamaCppAsset | None = None) -> None:
    """Validate the whole member table before copying any archive contents."""
    with DirectoryAnchor(dest_dir) as output:
        source_parent = DirectoryAnchor(archive_path.parent)
        with source_parent, source_parent.file(archive_path.name) as source:
            before = None
            if trusted_asset is not None:
                if archive_path.name != trusted_asset.file_name:
                    raise ValueError("Archive name does not match the trusted release")
                before = _verify_archive_source(source, trusted_asset)
            is_zip = archive_path.name.endswith(".zip")
            archive = zipfile.ZipFile(source) if is_zip else tarfile.open(fileobj=source, mode="r:gz")
            with archive:
                files = _validated_archive_files(archive, is_zip,
                                                 allow_internal_aliases=trusted_asset is not None)
                if any(output.info(target) is not None for target, *_ in files):
                    raise ValueError("Archive destination is occupied")
                for target, entry, size, mode in files:
                    reader = archive.open(entry) if is_zip else archive.extractfile(entry)
                    if reader is None:
                        raise ValueError("Archive file could not be read")
                    copied = 0
                    with reader, output.file(target, "wb") as writer:
                        while chunk := reader.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > size:
                                raise ValueError("Archive member exceeds its declared size")
                            writer.write(chunk)
                        if copied != size:
                            raise ValueError("Archive member is truncated")
                        writer.flush()
                        # Windows executability is encoded in the PE header.
                        if hasattr(os, "fchmod"):
                            os.fchmod(writer.fileno(), 0o755 if mode & 0o111 else 0o644)
                        os.fsync(writer.fileno())
                output.sync()
                if before is not None:
                    _assert_archive_unchanged(source, before)


def verified_archive_inventory(directory: DirectoryAnchor, asset: TrustedLlamaCppAsset) -> dict[str, Any]:
    """Derive file trust from the pinned archive, never from its local receipt."""
    with directory.file(asset.file_name) as source:
        before = _verify_archive_source(source, asset)
        is_zip = asset.file_name.endswith(".zip")
        archive = zipfile.ZipFile(source) if is_zip else tarfile.open(fileobj=source, mode="r:gz")
        result = {asset.file_name: {"sha256": asset.sha256, "size_bytes": asset.size_bytes}}
        with archive:
            for name, entry, size, _mode in _validated_archive_files(archive, is_zip, allow_internal_aliases=True):
                if name.casefold() == asset.file_name.casefold():
                    raise ValueError("Archive cannot replace its retained source")
                reader = archive.open(entry) if is_zip else archive.extractfile(entry)
                if reader is None:
                    raise ValueError("Archive file could not be read")
                count, file_digest = 0, hashlib.sha256()
                with reader:
                    while chunk := reader.read(1024 * 1024):
                        count += len(chunk)
                        if count > size:
                            raise ValueError("Archive member exceeds its declared size")
                        file_digest.update(chunk)
                if count != size:
                    raise ValueError("Archive member is truncated")
                result[name] = {"sha256": file_digest.hexdigest(), "size_bytes": size}
        _assert_archive_unchanged(source, before)
    return result


def validate_candidate_files(directory: DirectoryAnchor, asset: TrustedLlamaCppAsset) -> dict[str, Any]:
    expected = verified_archive_inventory(directory, asset)
    if file_inventory(directory, asset) != expected:
        raise ValueError("Installed files do not match the pinned release archive")
    directory.assert_current()
    return expected


def validate_server_header(data: bytes, asset: TrustedLlamaCppAsset) -> None:
    """Read platform and CPU identity without executing downloaded code."""
    valid = False
    if asset.os_name == "linux" and len(data) >= 64 and data[:7] == b"\x7fELF\x02\x01\x01":
        valid = int.from_bytes(data[16:18], "little") in {2, 3} and int.from_bytes(
            data[18:20], "little") == (62 if asset.arch == "x64" else 183)
    elif asset.os_name == "macos" and len(data) >= 32:
        cpu = 0x01000007 if asset.arch == "x64" else 0x0100000C
        if data[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}:
            endian = "little" if data[:4] == b"\xcf\xfa\xed\xfe" else "big"
            valid = int.from_bytes(data[4:8], endian) == cpu and int.from_bytes(data[12:16], endian) == 2
        elif data[:4] == b"\xca\xfe\xba\xbe":
            count = int.from_bytes(data[4:8], "big")
            valid = 0 < count <= 32 and len(data) >= 8 + count * 20 and any(
                int.from_bytes(data[8 + i * 20:12 + i * 20], "big") == cpu for i in range(count))
    elif asset.os_name == "windows" and len(data) >= 64 and data[:2] == b"MZ":
        offset = int.from_bytes(data[60:64], "little")
        valid = (64 <= offset <= len(data) - 26 and data[offset:offset + 4] == b"PE\0\0"
                 and int.from_bytes(data[offset + 4:offset + 6], "little") ==
                 (0x8664 if asset.arch == "x64" else 0xAA64)
                 and int.from_bytes(data[offset + 24:offset + 26], "little") == 0x20B
                 and not int.from_bytes(data[offset + 22:offset + 24], "little") & 0x2000)
    if not valid:
        raise ValueError("llama-server executable platform or architecture does not match")


def file_inventory(directory: DirectoryAnchor, asset: TrustedLlamaCppAsset) -> dict[str, Any]:
    directory.assert_current()
    names = os.listdir(directory.fd) if directory.fd is not None else os.listdir(directory.path)
    if len(names) > MAX_FILES:
        raise ValueError("Too many installed files")
    result = {}
    server = "llama-server.exe" if asset.os_name == "windows" else "llama-server"
    for name in sorted(names):
        if name in {RECEIPT_NAME, NOTICES_NAME}:
            continue
        digest = hashlib.sha256()
        size = 0
        with directory.file(name) as stream:
            before = os.fstat(stream.fileno())
            if name == server:
                validate_server_header(stream.read(65536), asset)
                stream.seek(0)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
            if (identity(before), before.st_size, before.st_mtime_ns) != (
                identity(after), after.st_size, after.st_mtime_ns):
                raise ValueError("Installed file changed while verifying")
        result[name] = {"sha256": digest.hexdigest(), "size_bytes": size}
    if server not in result:
        raise ValueError("llama.cpp archive does not contain llama-server")
    return result


def write_receipt(directory: DirectoryAnchor, asset: TrustedLlamaCppAsset, nonce: str,
                  *, health: dict[str, Any], license_manifest: dict[str, Any]) -> None:
    files = validate_candidate_files(directory, asset)
    licenses = validate_installed_notices(directory, asset, license_manifest, files)
    receipt = {"schema_version": 1, "asset": asdict(asset), "files": files,
               "directory_identity": directory.chain[-1], "job_nonce": nonce, "health": health,
               "licenses": licenses}
    with directory.file(RECEIPT_NAME, "wb") as stream:
        stream.write((json.dumps(receipt, sort_keys=True) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    directory.sync()


def read_receipt(directory: DirectoryAnchor) -> dict[str, Any]:
    with directory.file(RECEIPT_NAME) as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Installation receipt is too large")
    receipt = json.loads(data)
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("Invalid installation receipt")
    return receipt


def validate_receipt(directory: DirectoryAnchor, asset: TrustedLlamaCppAsset,
                     *, nonce: str | None = None, license_manifest: dict[str, Any]) -> None:
    receipt = read_receipt(directory)
    if receipt.get("asset") != asdict(asset) or receipt.get("directory_identity") != directory.chain[-1]:
        raise ValueError("Installation receipt does not match the trusted asset or directory")
    if nonce is not None and receipt.get("job_nonce") != nonce:
        raise ValueError("Installation receipt belongs to a different download")
    health = receipt.get("health")
    if health != {"version": asset.tag.removeprefix("b"), "returncode": 0}:
        raise ValueError("Installation has no matching staged health evidence")
    files = validate_candidate_files(directory, asset)
    if receipt.get("files") != files:
        raise ValueError("Installed files do not match the verification receipt")
    if receipt.get("licenses") != validate_installed_notices(directory, asset, license_manifest, files):
        raise ValueError("Installation license evidence does not match the trusted binary")



def validate_license_output(directory: DirectoryAnchor, asset: TrustedLlamaCppAsset,
                            manifest: dict[str, Any], notice: bytes) -> list[dict[str, Any]]:
    """Bind complete stdout and original notice bodies to admitted server bytes."""
    if (len(notice) != manifest["notice_size_bytes"] or not 0 < len(notice) <= 64 * 1024
            or hashlib.sha256(notice).hexdigest() != manifest["notice_sha256"]):
        raise ValueError("Staged llama-server license output does not match the reviewed notice")
    bodies, sections = [], []
    for section in manifest["sections"]:
        body = section["text"].encode("utf8")
        if (not body or len(body) != section["size_bytes"]
                or hashlib.sha256(body).hexdigest() != section["sha256"] or body not in notice):
            raise ValueError("Reviewed complete license text does not match the notice")
        bodies.append(body)
        sections.append({key: section[key] for key in ("name", "sha256", "size_bytes")})
    if not bodies:
        raise ValueError("Reviewed notice has no complete license bodies")
    # Keep a bounded overlap when scanning large executables. Every source body
    # must be complete; a title or excerpt does not establish attribution.
    pending = set(range(len(bodies)))
    overlap, previous = max(map(len, bodies)) - 1, b""
    server = "llama-server.exe" if asset.os_name == "windows" else "llama-server"
    with directory.file(server) as stream:
        before = os.fstat(stream.fileno())
        while chunk := stream.read(1024 * 1024):
            data = previous + chunk
            pending -= {index for index in pending if bodies[index] in data}
            previous = data[-overlap:] if overlap else b""
        after = os.fstat(stream.fileno())
        if (identity(before), before.st_size, before.st_mtime_ns) != (
            identity(after), after.st_size, after.st_mtime_ns):
            raise ValueError("License-bound executable changed while verifying")
    if pending:
        raise ValueError("Complete reviewed license text is absent from the admitted executable")
    directory.assert_current()
    return sections


def write_notices(directory: DirectoryAnchor, asset: TrustedLlamaCppAsset,
                  manifest: dict[str, Any], notice: bytes) -> None:
    validate_license_output(directory, asset, manifest, notice)
    with directory.file(NOTICES_NAME, "wb") as stream:
        stream.write(notice)
        stream.flush()
        os.fsync(stream.fileno())
    directory.sync()


def validate_installed_notices(directory: DirectoryAnchor, asset: TrustedLlamaCppAsset,
                               manifest: dict[str, Any], files: dict[str, Any]) -> dict[str, Any]:
    with directory.file(NOTICES_NAME) as stream:
        notice = stream.read(64 * 1024 + 1)
    sections = validate_license_output(directory, asset, manifest, notice)
    server = "llama-server.exe" if asset.os_name == "windows" else "llama-server"
    return {"source_commit": manifest["source_commit"], "archive_sha256": asset.sha256,
            "server_sha256": files[server]["sha256"], "notice_file": NOTICES_NAME,
            "notice_sha256": manifest["notice_sha256"], "notice_size_bytes": len(notice),
            "sections": sections, "command": "--license", "returncode": 0}
