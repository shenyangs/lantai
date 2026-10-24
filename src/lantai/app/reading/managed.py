# -*- coding: utf-8 -*-
"""Version-locked, lazily prepared local RSSHub with owned process lifetime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import signal
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

import httpx

from ...security.tool_execution_context import build_sandboxed_tool_env

NODE_VERSION = "24.15.0"
RSSHUB_VERSION = "1.0.0-master.85c7a8a"
NODE_SHA256 = {
    "arm64": (
        "372331b969779ab5d15b949884fc6eaf88d5afe87bde8ba881d6400b" "9100ffc4"
    ),
    "x64": "ffd5ee293467927f3ee731a553eb88fd1f48cf74eebc2d74a6babe4af228673b",
}
ASSETS = Path(__file__).parent / "data" / "runtime"


class ReadingRuntimeError(ValueError):
    pass


def extract_node_archive(archive: Path, destination: Path) -> None:
    """Extract the pinned Node archive without traversal, links, or bombs."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as handle:
        members = handle.getmembers()
        if (
            len(members) > 15000
            or sum(m.size for m in members) > 250 * 1024 * 1024
        ):
            raise ReadingRuntimeError("运行环境压缩包超过大小限制")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ReadingRuntimeError("运行环境压缩包包含无效路径")
            if not member.isfile() or len(path.parts) < 2:
                continue
            target = destination.joinpath(*path.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = handle.extractfile(member)
            if stream is None:
                raise ReadingRuntimeError("运行环境压缩包不完整")
            with stream, target.open("xb") as output:
                shutil.copyfileobj(stream, output)
            target.chmod(0o700 if member.mode & 0o111 else 0o600)


async def run_owned_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict,
    timeout: int,
) -> bytes:
    """Run one reading-runtime child and always reap its process group."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise ReadingRuntimeError("组件准备失败，请检查网络后重试")
        return output
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 3)
            except (ProcessLookupError, asyncio.TimeoutError):
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()


class ManagedRSSHub:
    def __init__(self, workspace, *, cache_root=None):
        cache = (
            Path(cache_root)
            if cache_root
            else Path.home() / "Library/Caches/lantai/reading"
        )
        identity = hashlib.sha256(
            str(Path(workspace).resolve()).encode(),
        ).hexdigest()[:24]
        self.root = cache / "workspaces" / identity
        self.install_root = cache / "engines"
        self.directory = self.install_root / (
            "rsshub-"
            f"{RSSHUB_VERSION}"
            "-node-"
            f"{NODE_VERSION}"
            "-"
            f"{platform.machine()}"
        )
        self.process = None
        self.task = None
        self.lock = asyncio.Lock()
        self.state = "idle"
        self.message = "首次使用会下载本机阅读组件，无需安装 Node 或 Docker。"
        self.progress = None
        self.config = {}
        self.active_request = None
        self.stopping = False
        if self.installed:
            self.message = "本机组件已准备，首次读取会启动服务。"

    @property
    def node(self):
        return self.directory / "node" / "bin" / "node"

    @property
    def installed(self):
        return (
            self.node.is_file()
            and (self.directory / "complete").is_file()
            and (
                self.directory / "engine/node_modules/rsshub/dist-lib/pkg.mjs"
            ).is_file()
        )

    def chrome(self):
        for path in (
            Path(
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ),
            Path.home()
            / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ):
            if path.is_file():
                return str(path)
        return ""

    def snapshot(self):
        return {
            "state": self.state,
            "installed": self.installed,
            "running": bool(self.process and self.process.returncode is None),
            "message": self.message,
            "progress": self.progress,
            "version": RSSHUB_VERSION,
            "node_version": NODE_VERSION,
            "browser_available": bool(
                self.chrome()
                or any(
                    path.is_file()
                    for path in (self.root / "browsers").glob(
                        (
                            "chromium_headless_shell-*/chrome-headless-shell-"
                            "*/chrome-headless-shell"
                        ),
                    )
                ),
            ),
        }

    def update(self, message, progress=None):
        self.message, self.progress = message, progress

    def environment(self, directory=None):
        directory = directory or self.directory
        env = build_sandboxed_tool_env()
        # Isolate cache and npm configuration from user/global
        # package managers.
        env.update(
            {
                "PATH": str(directory / "node/bin") + os.pathsep + os.defpath,
                "npm_config_cache": str(self.root / "npm-cache"),
                "npm_config_userconfig": str(directory / "user.npmrc"),
                "npm_config_globalconfig": str(directory / "global.npmrc"),
                "PLAYWRIGHT_BROWSERS_PATH": str(self.root / "browsers"),
            },
        )
        return env

    async def download_node(self, stage):
        arch = "arm64" if platform.machine() == "arm64" else "x64"
        name = f"node-v{NODE_VERSION}-darwin-{arch}.tar.gz"
        archive = stage / "node.tgz"
        # Both sources must match the same pinned upstream SHA256.
        for base in (
            "https://npmmirror.com/mirrors/node",
            "https://nodejs.org/dist",
        ):
            self.update("正在下载并校验本机运行环境", 5)
            try:
                async with httpx.AsyncClient(
                    timeout=30,
                    follow_redirects=True,
                ) as client:
                    async with client.stream(
                        "GET",
                        f"{base}/v{NODE_VERSION}/{name}",
                    ) as response:
                        response.raise_for_status()
                        size, digest = 0, hashlib.sha256()
                        with archive.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > 100 * 1024 * 1024:
                                    raise ReadingRuntimeError("运行环境下载超过大小限制")
                                output.write(chunk)
                                digest.update(chunk)
                if digest.hexdigest() != NODE_SHA256[arch]:
                    raise ReadingRuntimeError("运行环境校验失败")
                extraction = asyncio.create_task(
                    asyncio.to_thread(
                        extract_node_archive,
                        archive,
                        stage / "node",
                    ),
                )
                try:
                    await asyncio.shield(extraction)
                except asyncio.CancelledError:
                    await extraction
                    raise
                archive.unlink()
                return
            except (httpx.HTTPError, ReadingRuntimeError):
                archive.unlink(missing_ok=True)
        raise ReadingRuntimeError("运行环境下载失败，请检查网络后重新准备")

    async def install(self):
        if self.installed:
            return
        if platform.system() != "Darwin" or platform.machine() not in {
            "arm64",
            "x86_64",
        }:
            raise ReadingRuntimeError(
                "本机托管目前支持 Apple 芯片与 Intel Mac，其他平台请连接自建实例",
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.install_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Hosted reading installs are guarded as Mac-only above; importing
        # this module must still work for self-hosted instances on Windows.
        import fcntl  # pylint: disable=import-outside-toplevel

        with (self.install_root / "install.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReadingRuntimeError("另一个进程正在准备阅读服务，请稍后检查") from exc
            with tempfile.TemporaryDirectory(
                prefix="prepare-",
                dir=self.install_root,
            ) as temporary:
                stage = Path(temporary)
                await self.download_node(stage)
                engine = stage / "engine"
                engine.mkdir()
                for name in ("package.json", "package-lock.json"):
                    shutil.copyfile(ASSETS / name, engine / name)
                for name in ("user.npmrc", "global.npmrc"):
                    (stage / name).touch(mode=0o600)
                self.update("正在安装固定版本的 RSSHub 频道引擎", 55)
                await run_owned_process(
                    [
                        str(stage / "node/bin/node"),
                        str(
                            stage / "node/lib/node_modules/npm/bin/npm-cli.js",
                        ),
                        "ci",
                        "--ignore-scripts",
                        "--no-audit",
                        "--no-fund",
                        "--registry=https://registry.npmmirror.com",
                    ],
                    cwd=engine,
                    env=self.environment(stage),
                    timeout=600,
                )
                await run_owned_process(
                    [
                        str(stage / "node/bin/node"),
                        str(ASSETS / "bridge.mjs"),
                        str(engine),
                        "--check",
                    ],
                    cwd=engine,
                    env=self.environment(stage),
                    timeout=30,
                )
                (stage / "complete").write_text(RSSHUB_VERSION)
                if self.directory.exists():
                    # Preserve damaged installations for
                    # diagnosis and clean retry.
                    self.directory.rename(
                        self.install_root / ("incomplete-" + uuid4().hex),
                    )
                stage.rename(self.directory)

    def prepare(self, *, browser=False):
        if self.task and not self.task.done():
            return self.snapshot()

        async def work():
            self.state = "preparing"
            try:
                await self.install()
                if browser and not self.chrome():
                    self.update("正在准备频道所需的浏览器组件", 85)
                    await run_owned_process(
                        [
                            str(self.node),
                            str(
                                self.directory
                                / "engine/node_modules/patchright/cli.js",
                            ),
                            "install",
                            "chromium",
                        ],
                        cwd=self.directory / "engine",
                        env=self.environment(),
                        timeout=600,
                    )
                    (self.root / "browser-ready").write_text(RSSHUB_VERSION)
                await self.start()
                self.state = "ready"
                self.update("本机阅读服务已就绪，可以验证并订阅频道。", 100)
            except asyncio.CancelledError:
                self.state = "idle"
                self.update("准备已取消，可以重新开始")
                raise
            except Exception as exc:
                self.state = "error"
                self.update(
                    str(exc)
                    if isinstance(exc, ReadingRuntimeError)
                    else "本机组件准备失败，请检查网络后重试",
                )

        self.task = asyncio.create_task(work())
        self.state = "preparing"
        return self.snapshot()

    async def _exchange(self, message, timeout=45):
        process = self.process
        if process is None or process.returncode is not None:
            raise ReadingRuntimeError("本机阅读服务已停止，请重新启动")
        request_id = uuid4().hex
        process.stdin.write(
            (json.dumps({"id": request_id, **message}) + "\n").encode(),
        )
        await process.stdin.drain()
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout)
            value = json.loads(line)
            if value.get("id") != request_id:
                raise ValueError("response identity mismatch")
            if not value.get("ok"):
                messages = {
                    "browser_required": "此频道需要浏览器组件，请点击“准备浏览器组件”后重试",
                    "configuration_required": "此频道需要账号配置，请填写下方列出的配置项后重试",
                    "upstream_unavailable": "本机服务已运行，但上游网站暂时无法连接，请检查网络后重试",
                    "route_not_found": "当前频道引擎尚不支持此路由，请核对路径与版本",
                    "route_failed": "上游未返回有效频道内容，请核对参数或查看官方路由说明",
                }
                raise ReadingRuntimeError(
                    messages.get(value.get("error"), "频道读取失败"),
                )
            return value["data"]
        except (asyncio.TimeoutError, ValueError) as exc:
            if isinstance(exc, ReadingRuntimeError):
                raise
            await self._stop()
            raise ReadingRuntimeError("频道读取超时或服务响应异常，已停止该次读取；可以重试") from exc
        except asyncio.CancelledError:
            await self._stop()
            raise

    async def _start(self):
        if self.stopping:
            raise ReadingRuntimeError("本机服务正在停止，请稍后重新读取")
        if self.process and self.process.returncode is None:
            return
        if not self.installed:
            raise ReadingRuntimeError("请先点击“准备本机服务”，完成首次组件安装")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.process = await asyncio.create_subprocess_exec(
            str(self.node),
            str(ASSETS / "bridge.mjs"),
            str(self.directory / "engine"),
            cwd=self.directory / "engine",
            env=self.environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=10 * 1024 * 1024,
            start_new_session=True,
        )
        configuration = dict(self.config)
        if self.chrome():
            configuration["CHROMIUM_EXECUTABLE_PATH"] = self.chrome()
        try:
            await self._exchange(
                {"action": "init", "config": configuration},
                timeout=30,
            )
        except BaseException:
            await self._stop()
            raise
        self.state = "ready"
        self.update("本机阅读服务正在运行，文章和收藏保存在本机。", 100)

    async def start(self):
        async with self.lock:
            await self._start()
        self.state = "ready"
        self.update("本机阅读服务已启动，可以订阅频道。", 100)

    async def request(self, path):
        async with self.lock:
            self.active_request = asyncio.current_task()
            try:
                await self._start()
                return await self._exchange(
                    {"action": "request", "path": path},
                )
            except asyncio.CancelledError:
                if self.stopping:
                    raise ReadingRuntimeError("读取已停止，已保存的文章保持不变") from None
                raise
            finally:
                self.active_request = None

    async def _stop(self):
        process, self.process = self.process, None
        if not process or process.returncode is not None:
            return
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 3)
            except (ProcessLookupError, asyncio.TimeoutError):
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()

    async def stop(self):
        self.stopping = True
        try:
            if (
                self.task
                and not self.task.done()
                and self.task is not asyncio.current_task()
            ):
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            if (
                self.active_request
                and self.active_request is not asyncio.current_task()
            ):
                task = self.active_request
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            async with self.lock:
                await self._stop()
            self.state = "idle"
            self.update("本机阅读服务已停止，保存的文章仍可阅读")
        finally:
            self.stopping = False

    async def configure(self, values):
        await self.stop()
        self.config = values

    async def close(self):
        await self.stop()
