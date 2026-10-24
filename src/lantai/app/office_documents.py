# -*- coding: utf-8 -*-
"""Office document capability powered by OfficeCLI when available."""

from __future__ import annotations

import difflib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from ..constant import WORKING_DIR
from ..utils.authoritative_store import atomic_write_bytes

logger = logging.getLogger(__name__)

OFFICECLI_REPOSITORY_URL = "https://github.com/iOfficeAI/OfficeCLI"
OFFICECLI_DOCS_URL = f"{OFFICECLI_REPOSITORY_URL}/blob/main/README_zh.md"
OFFICECLI_INSTALL_UNIX = (
    "curl -fsSL https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/"
    "install.sh | bash"
)
OFFICECLI_CLIENT_KEY = "officecli"
OFFICECLI_MCP_NAME = "Office 文档能力"
OFFICECLI_MCP_DESCRIPTION = "通过 OfficeCLI 读写、预览和校验 Word、Excel、PowerPoint 文档。"
OFFICECLI_SUPPORTED_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
OFFICECLI_TEXT_MODES = {"text", "outline", "annotated", "stats", "issues"}

_CONFIG_FILE = WORKING_DIR / "office_documents.json"
_CONFIG_LOCK = threading.RLock()


@dataclass
class OfficeCliConfig:
    """Persisted OfficeCLI preference."""

    enabled: bool = True
    officecli_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OfficeCliStatus:
    """Detected OfficeCLI runtime state."""

    enabled: bool
    available: bool
    path: str
    source: str
    version: str | None
    mcp_supported: bool
    config_path: str
    repository_url: str = OFFICECLI_REPOSITORY_URL
    docs_url: str = OFFICECLI_DOCS_URL
    install_command: str = OFFICECLI_INSTALL_UNIX
    message: str = ""
    error: str = ""
    features: list[str] = field(
        default_factory=lambda: [
            "聊天附件可直接分析 .docx / .xlsx / .pptx",
            "Wiki / 记忆可导入 Office 文件并提取正文",
            "智能体可通过 MCP 调用 OfficeCLI 读写、校验、预览文档",
            "定时任务可基于模板生成周报、销售表和汇报 PPT",
        ],
    )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OfficeCliPreview:
    """Preview/extraction result for an Office document."""

    available: bool
    method: str
    mode: str
    content_type: str
    content: str
    file_path: str
    output_path: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OfficeDocumentCompare:
    """Text-level comparison between two Office document versions."""

    before_file_path: str
    after_file_path: str
    before_method: str
    after_method: str
    content_type: str
    unified_diff: str
    added_lines: int
    removed_lines: int
    changed: bool
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _load_config() -> OfficeCliConfig:
    with _CONFIG_LOCK:
        if not _CONFIG_FILE.is_file():
            return OfficeCliConfig()
        try:
            data = json.loads(_CONFIG_FILE.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return OfficeCliConfig()
        if not isinstance(data, dict):
            return OfficeCliConfig()
        return OfficeCliConfig(
            enabled=bool(data.get("enabled", True)),
            officecli_path=str(data.get("officecli_path", "") or "").strip(),
        )


def save_officecli_config(config: OfficeCliConfig) -> OfficeCliConfig:
    with _CONFIG_LOCK:
        atomic_write_bytes(
            _CONFIG_FILE,
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return config


def update_officecli_config(
    *,
    enabled: bool | None = None,
    officecli_path: str | None = None,
) -> OfficeCliConfig:
    with _CONFIG_LOCK:
        current = _load_config()
        if enabled is not None:
            current.enabled = bool(enabled)
        if officecli_path is not None:
            current.officecli_path = str(officecli_path or "").strip()
        return save_officecli_config(current)


def _candidate_paths(config: OfficeCliConfig) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if config.officecli_path:
        candidates.append((config.officecli_path, "config"))
    env_path = os.environ.get("OFFICECLI_PATH", "").strip()
    if env_path:
        candidates.append((env_path, "env"))

    executable_dir = Path(sys.executable).resolve().parent
    for path in (
        executable_dir / "officecli",
        executable_dir / "bin" / "officecli",
        Path(getattr(sys, "_MEIPASS", "")) / "officecli",
        Path(getattr(sys, "_MEIPASS", "")) / "bin" / "officecli",
        WORKING_DIR / "bin" / "officecli",
    ):
        if str(path) and path.exists():
            candidates.append((str(path), "bundled"))

    which = shutil.which("officecli")
    if which:
        candidates.append((which, "path"))

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for raw, source in candidates:
        normalized = str(Path(raw).expanduser()) if raw else ""
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append((normalized, source))
    return unique


def _officecli_env() -> dict[str, str]:
    env = os.environ.copy()
    # These helpers are app-owned, short-lived commands. Inherited native
    # preferences must not start an updater or a detached resident here.
    env["OFFICECLI_SKIP_UPDATE"] = "1"
    env["OFFICECLI_NO_AUTO_RESIDENT"] = "1"
    return env


def _run_officecli_version(command: str) -> tuple[str | None, str]:
    try:
        proc = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env=_officecli_env(),
        )
    except Exception as exc:
        return None, str(exc)

    output = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part.strip()
    ).strip()
    if proc.returncode != 0:
        return None, output or f"officecli --version exited {proc.returncode}"
    first_line = output.splitlines()[0].strip() if output else ""
    return first_line or "unknown", ""


def get_officecli_status() -> OfficeCliStatus:
    config = _load_config()
    last_error = ""
    for command, source in _candidate_paths(config):
        version, error = _run_officecli_version(command)
        if version:
            return OfficeCliStatus(
                enabled=config.enabled,
                available=True,
                path=command,
                source=source,
                version=version,
                mcp_supported=True,
                config_path=str(_CONFIG_FILE),
                message="OfficeCLI 已就绪，可供智能体和文档工作流使用。",
            )
        last_error = error

    return OfficeCliStatus(
        enabled=config.enabled,
        available=False,
        path="",
        source="missing",
        version=None,
        mcp_supported=False,
        config_path=str(_CONFIG_FILE),
        message=(
            "未检测到 officecli。可手动安装到 PATH，或把可执行文件随 App "
            "打包/放入工作目录 bin 后再刷新。"
        ),
        error=last_error,
    )


def get_officecli_command() -> str | None:
    status = get_officecli_status()
    if status.enabled and status.available and status.path:
        return status.path
    return None


def build_officecli_mcp_client_config(enabled: bool = True):
    """Return MCP client config for OfficeCLI's stdio MCP server."""

    from ..config.config import MCPClientConfig

    command = get_officecli_status().path or "officecli"
    return MCPClientConfig(
        name=OFFICECLI_MCP_NAME,
        description=OFFICECLI_MCP_DESCRIPTION,
        enabled=enabled,
        transport="stdio",
        command=command,
        args=["mcp"],
        env={
            "OFFICECLI_SKIP_UPDATE": "1",
            "OFFICECLI_NO_AUTO_RESIDENT": "1",
            "OFFICECLI_RESIDENT_FLUSH": "each",
        },
        cwd="",
    )


def ensure_officecli_default(config):
    """Return MCP config with the built-in OfficeCLI client defaulted on.

    Existing agent choices remain authoritative, including an explicit disable.
    """

    from ..config.config import MCPConfig

    if config is None:
        config = MCPConfig(clients={})
    else:
        config = config.model_copy(deep=True)

    existing = (config.clients or {}).get(OFFICECLI_CLIENT_KEY)
    enabled = True if existing is None else existing.enabled
    config.clients[OFFICECLI_CLIENT_KEY] = build_officecli_mcp_client_config(
        enabled=enabled,
    )
    return config


def _resolve_input_file(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Office 文件不存在：{resolved}")
    if resolved.suffix.lower() not in OFFICECLI_SUPPORTED_EXTENSIONS:
        raise ValueError(
            "当前仅支持 .docx、.xlsx、.pptx 的 Office 文档预览。",
        )
    return resolved


def _fallback_extract_text(path: Path) -> str:
    from .wiki import core as wiki_core

    suffix = path.suffix.lower()
    if suffix == ".docx":
        return wiki_core._extract_docx_text(path)
    if suffix == ".pptx":
        return wiki_core._extract_pptx_text(path)
    if suffix == ".xlsx":
        return wiki_core._extract_xlsx_text(path)
    return ""


def preview_office_document(
    file_path: str,
    *,
    mode: Literal[
        "text",
        "outline",
        "annotated",
        "stats",
        "issues",
        "html",
    ] = "text",
    timeout: int = 45,
) -> OfficeCliPreview:
    """Return a text or HTML preview for a local Office document."""

    path = _resolve_input_file(file_path)
    normalized_mode = (
        mode if mode in {*OFFICECLI_TEXT_MODES, "html"} else "text"
    )
    command = get_officecli_command()

    # Text extraction is a product contract, not an optional OfficeCLI
    # implementation detail.  The bundled OOXML readers preserve tables,
    # slide/layout/note boundaries, and formulas consistently across source
    # installs and packaged builds.  Keep OfficeCLI for richer non-text modes
    # and as a fallback for documents the bundled reader cannot interpret.
    if normalized_mode == "text":
        structured_text = _fallback_extract_text(path)
        if structured_text:
            return OfficeCliPreview(
                available=bool(command),
                # Preserve the public method identifier used by existing
                # comparison/report consumers.  The richer structured output
                # is an implementation upgrade, not a wire-contract rename.
                method="office_xml",
                mode="text",
                content_type="text/plain",
                content=structured_text,
                file_path=str(path),
                message="已通过内置 Office Open XML 结构化读取正文。",
            )

    if command:
        if normalized_mode == "html":
            out_dir = Path(tempfile.mkdtemp(prefix="lantai_office_preview_"))
            out_path = out_dir / f"{path.stem}_{int(time.time())}.html"
            proc_args = [
                command,
                "view",
                str(path),
                "html",
                "-o",
                str(out_path),
            ]
            content_type = "text/html"
        else:
            out_path = None
            proc_args = [command, "view", str(path), normalized_mode]
            content_type = "text/plain"

        try:
            proc = subprocess.run(
                proc_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=_officecli_env(),
            )
            if proc.returncode == 0:
                if out_path is not None and out_path.is_file():
                    return OfficeCliPreview(
                        available=True,
                        method="officecli",
                        mode=normalized_mode,
                        content_type=content_type,
                        content=out_path.read_text("utf-8", errors="replace"),
                        file_path=str(path),
                        output_path=str(out_path),
                        message="OfficeCLI HTML 预览已生成。",
                    )
                return OfficeCliPreview(
                    available=True,
                    method="officecli",
                    mode=normalized_mode,
                    content_type=content_type,
                    content=(proc.stdout or "").strip(),
                    file_path=str(path),
                    message="OfficeCLI 文本预览已生成。",
                )
            logger.warning(
                "OfficeCLI preview failed for %s: %s",
                path,
                (proc.stderr or proc.stdout or "").strip(),
            )
        except Exception:
            logger.warning("OfficeCLI preview crashed for %s", path, exc_info=True)

    text = _fallback_extract_text(path)
    return OfficeCliPreview(
        available=bool(command),
        method="office_xml",
        mode="text",
        content_type="text/plain",
        content=text,
        file_path=str(path),
        message=(
            "未使用 OfficeCLI，已通过内置 Office Open XML 提取正文。"
            if text
            else "未能提取到正文。"
        ),
    )


def extract_office_document_text_with_officecli(
    file_path: str,
    *,
    mode: Literal["text", "outline", "annotated", "stats", "issues"] = "text",
    timeout: int = 45,
) -> str:
    """Extract Office document text with OfficeCLI only.

    Callers that need a built-in fallback should use ``preview_office_document``.
    """

    path = _resolve_input_file(file_path)
    normalized_mode = mode if mode in OFFICECLI_TEXT_MODES else "text"
    command = get_officecli_command()
    if not command:
        return ""
    try:
        proc = subprocess.run(
            [command, "view", str(path), normalized_mode],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_officecli_env(),
        )
    except Exception:
        logger.warning("OfficeCLI extraction crashed for %s", path, exc_info=True)
        return ""
    if proc.returncode != 0:
        logger.warning(
            "OfficeCLI extraction failed for %s: %s",
            path,
            (proc.stderr or proc.stdout or "").strip(),
        )
        return ""
    return (proc.stdout or "").strip()


def compare_office_documents(
    before_file_path: str,
    after_file_path: str,
    *,
    timeout: int = 45,
) -> OfficeDocumentCompare:
    """Compare two Office documents using their extracted text representation."""

    before_path = _resolve_input_file(before_file_path)
    after_path = _resolve_input_file(after_file_path)
    before = preview_office_document(
        str(before_path),
        mode="text",
        timeout=timeout,
    )
    after = preview_office_document(
        str(after_path),
        mode="text",
        timeout=timeout,
    )
    before_lines = before.content.splitlines()
    after_lines = after.content.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=before_path.name,
            tofile=after_path.name,
            lineterm="",
            n=3,
        ),
    )
    added_lines = sum(
        1
        for line in diff_lines
        if line.startswith("+") and not line.startswith("+++")
    )
    removed_lines = sum(
        1
        for line in diff_lines
        if line.startswith("-") and not line.startswith("---")
    )
    changed = bool(diff_lines)
    if changed:
        message = (
            f"检测到 {added_lines} 行新增、{removed_lines} 行删除；"
            "可据此复核 Agent 修改后的 Office 文档。"
        )
    else:
        message = "两份 Office 文档抽取出的文本内容一致。"
    return OfficeDocumentCompare(
        before_file_path=str(before_path),
        after_file_path=str(after_path),
        before_method=before.method,
        after_method=after.method,
        content_type="text/x-diff",
        unified_diff="\n".join(diff_lines),
        added_lines=added_lines,
        removed_lines=removed_lines,
        changed=changed,
        message=message,
    )
