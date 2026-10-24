# -*- coding: utf-8 -*-
"""Read-only document boundary for the official KDocs CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..security.tool_execution_context import build_sandboxed_tool_env
from .kdocs_connection import find_kdocs_cli
from .kdocs_read_cache import store_read, read_page, cache_generation


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
CliFinder = Callable[[], Path | None]


_MARKDOWN_DATA_IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]{0,500}\]\(\s*data:image/[^)]*\)",
    re.IGNORECASE,
)
_HTML_DATA_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*([\"'])\s*data:image/[^\"']*\1[^>]*>",
    re.IGNORECASE,
)
_BARE_BASE64_IMAGE_RE = re.compile(
    r"data:image/[a-z0-9.+-]+(?:;[a-z0-9.+_=-]+)*;?base64,[a-z0-9+/=\r\n]+",
    re.IGNORECASE,
)
_BARE_ENCODED_IMAGE_RE = re.compile(
    r"data:image/[a-z0-9.+-]+,[^\s)\"'<>]+",
    re.IGNORECASE,
)


def _omit_embedded_image_payloads(content: str) -> tuple[str, bool]:
    """Replace inline image data with a stable text marker before truncation."""

    sanitized = _MARKDOWN_DATA_IMAGE_RE.sub("[图片]", content)
    sanitized = _HTML_DATA_IMAGE_RE.sub("[图片]", sanitized)
    sanitized = _BARE_BASE64_IMAGE_RE.sub("[图片]", sanitized)
    sanitized = _BARE_ENCODED_IMAGE_RE.sub("[图片]", sanitized)
    return sanitized, sanitized != content


class KdocsDocumentError(RuntimeError):
    """A safe, structured failure from a KDocs read-only operation."""

    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        status_code: int = 502,
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.status_code = status_code

    def as_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.public_message}


_ERRORS: dict[str, tuple[str, str, int]] = {
    "400006": (
        "authentication_required",
        "金山文档登录已失效，请重新连接后再试。",
        401,
    ),
    "401": (
        "authentication_required",
        "金山文档登录已失效，请重新连接后再试。",
        401,
    ),
    "403": ("permission_denied", "你暂时没有访问这个文档的权限。", 403),
    "404": ("not_found", "这个文档可能已被删除、移动或取消分享。", 404),
    "429": ("rate_limited", "金山文档请求过于频繁，请稍后重试。", 429),
    "429001": (
        "rate_limited",
        "金山文档请求过于频繁，请按提示时间后再试。",
        429,
    ),
    "429002": (
        "rate_limited",
        "金山文档暂时处于限流保护中，请稍后再试。",
        429,
    ),
}


def _error_for_code(code: object) -> KdocsDocumentError:
    normalized = str(code or "").strip()
    mapped = _ERRORS.get(normalized)
    if mapped is None:
        mapped = ("provider_error", "金山文档暂时无法完成这个请求。", 502)
    return KdocsDocumentError(mapped[0], mapped[1], status_code=mapped[2])


def _text(value: object, limit: int = 1000) -> str:
    return str(value or "").strip()[:limit]


def _owner(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    user_id = _text(value.get("id"), 256)
    name = _text(value.get("name") or value.get("nick_name"), 240)
    if not user_id and not name:
        return None
    return {"id": user_id, "name": name}


def _permission_payload(value: object) -> dict[str, bool]:
    raw = value if isinstance(value, dict) else {}
    aliases = {
        "can_read": ("can_read", "read", "view", "preview"),
        "can_edit": ("can_edit", "edit", "update"),
        "can_share": ("can_share", "share"),
        "can_download": ("can_download", "download"),
    }
    result: dict[str, bool] = {}
    for target, keys in aliases.items():
        for key in keys:
            candidate = raw.get(key)
            if isinstance(candidate, bool):
                result[target] = candidate
                break
    return result


def _normalize_item(raw_item: object, *, fallback_drive_id: str = "") -> dict[str, Any] | None:
    if not isinstance(raw_item, dict):
        return None
    raw_file = raw_item.get("file")
    item = dict(raw_file) if isinstance(raw_file, dict) else dict(raw_item)
    file_id = _text(item.get("id") or item.get("file_id"), 256)
    drive_id = _text(
        item.get("drive_id")
        or raw_item.get("drive_id")
        or fallback_drive_id,
        256,
    )
    if not file_id or not drive_id:
        return None
    item_type = _text(item.get("type") or item.get("file_type"), 80).lower()
    suffix = _text(item.get("suffix") or item.get("ext"), 40).lstrip(".").lower()
    if not suffix and item_type not in {"folder", "shortcut", "file"}:
        suffix = item_type
    source = raw_item.get("file_src")
    location = ""
    if isinstance(source, dict):
        location = _text(source.get("path") or source.get("name"), 600)
    location = location or _text(item.get("path") or item.get("parent_path"), 600)
    permissions = _permission_payload(
        item.get("permission") or item.get("permissions") or raw_item.get("permission")
    )
    link_url = _text(
        item.get("link_url")
        or item.get("url")
        or raw_item.get("link_url")
        or raw_item.get("url"),
        2048,
    )
    result: dict[str, Any] = {
        "key": f"{drive_id}:{file_id}",
        "drive_id": drive_id,
        "file_id": file_id,
        "parent_id": _text(item.get("parent_id"), 256),
        "name": _text(item.get("name") or raw_item.get("name"), 240),
        "type": item_type,
        "suffix": suffix,
        "is_folder": item_type == "folder" or bool(item.get("is_folder") is True),
        "size": item.get("size") if isinstance(item.get("size"), int) else None,
        "created_at": item.get("ctime") or item.get("created_at"),
        "modified_at": item.get("mtime") or item.get("modified_at"),
        "version": _text(item.get("version"), 120),
        "content_hash": _text(item.get("hash") or item.get("content_hash"), 256),
        "link_id": _text(item.get("link_id"), 256),
        "original_url": link_url,
        "location": location,
        "owner": _owner(item.get("created_by") or raw_item.get("created_by")),
        "modifier": _owner(item.get("modified_by") or raw_item.get("modified_by")),
        "shared": bool(item.get("shared", False)),
        "permissions": permissions,
    }
    return result


class KdocsDocumentService:
    """List, inspect, and read cloud documents without exposing CLI details."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner = subprocess.run,
        cli_finder: CliFinder = find_kdocs_cli,
        cache_scope: str = "default",
    ) -> None:
        self._command_runner = command_runner
        self._cli_finder = cli_finder
        self._cache_scope = cache_scope
        self._cache_generation = cache_generation()

    def list_documents(
        self,
        *,
        view: str = "recent",
        drive_id: str = "",
        parent_id: str = "",
        query: str = "",
        page_token: str = "",
        page_size: int = 50,
    ) -> dict[str, Any]:
        normalized_view = _text(view, 32).lower() or "recent"
        bounded_size = max(1, min(int(page_size or 50), 100))
        params: dict[str, Any] = {
            "page_size": bounded_size,
        }
        if page_token:
            params["page_token"] = _text(page_token, 2048)
        if normalized_view == "folder":
            normalized_parent = _text(parent_id, 256)
            if not normalized_parent or normalized_parent == "0":
                raise KdocsDocumentError(
                    "invalid_request",
                    "请选择要打开的文件夹。",
                    status_code=400,
                )
            action = "list-files"
            params.update(
                {
                    "drive_id": _text(drive_id, 256),
                    "parent_id": normalized_parent,
                    "with_permission": True,
                }
            )
            if not params["drive_id"]:
                params.pop("drive_id")
        elif normalized_view == "my":
            action = "list-my-files"
            params["with_permission"] = True
        elif normalized_view == "recent":
            action = "list-latest-items"
            params.update({"with_permission": True, "with_link": True})
        elif normalized_view == "starred":
            action = "list-star-items"
            params.update({"with_permission": True, "with_link": True})
        elif normalized_view in {"shared", "search"}:
            action = "search-files"
            params.update(
                {
                    "keyword": _text(query, 240),
                    "type": "file_name",
                    "with_permission": True,
                    "with_link": True,
                    "with_drive": True,
                }
            )
            if normalized_view == "shared":
                params["scope"] = ["share_to_me"]
        else:
            raise KdocsDocumentError(
                "invalid_request",
                "不支持这个金山文档视图。",
                status_code=400,
            )
        data = self._call(action, params, timeout=45)
        fallback_drive = _text(data.get("drive_id"), 256)
        items = [
            normalized
            for item in (data.get("items") or [])
            if (normalized := _normalize_item(item, fallback_drive_id=fallback_drive))
            is not None
        ]
        return {
            "view": normalized_view,
            "items": items,
            "next_page_token": _text(data.get("next_page_token"), 2048),
            "total": data.get("total") if isinstance(data.get("total"), int) else None,
            "parent": {
                "drive_id": fallback_drive or _text(drive_id, 256),
                "file_id": _text(data.get("parent_id") or parent_id or "0", 256),
            },
        }

    def file_info(self, *, file_id: str) -> dict[str, Any]:
        normalized_id = _text(file_id, 256)
        if not normalized_id:
            raise KdocsDocumentError("invalid_request", "缺少文档 ID。", status_code=400)
        data = self._call(
            "get-file-info",
            {
                "file_id": normalized_id,
                "with_permission": True,
                "with_drive": True,
            },
            timeout=30,
        )
        candidate = data.get("file") if isinstance(data.get("file"), dict) else data
        drive = data.get("drive") if isinstance(data.get("drive"), dict) else {}
        fallback_drive_id = _text(data.get("drive_id") or drive.get("id"), 256)
        normalized = _normalize_item(candidate, fallback_drive_id=fallback_drive_id)
        if normalized is None:
            raise KdocsDocumentError("invalid_response", "金山文档返回了无法识别的文件信息。")
        return normalized

    def file_link(self, *, file_id: str) -> dict[str, str]:
        normalized_id = _text(file_id, 256)
        if not normalized_id:
            raise KdocsDocumentError("invalid_request", "缺少文档 ID。", status_code=400)
        data = self._call("get-file-link", {"file_id": normalized_id}, timeout=30)
        url = _text(data.get("link_url") or data.get("url"), 2048)
        if not url and isinstance(data.get("file"), dict):
            url = _text(data["file"].get("link_url") or data["file"].get("url"), 2048)
        if not url:
            raise KdocsDocumentError("link_unavailable", "这个文档暂时没有可打开的在线地址。", status_code=404)
        return {"file_id": normalized_id, "url": url}

    def read_document(
        self,
        *,
        file_id: str,
        task_id: str = "",
        continuation: str = "",
        format: str = "markdown",
        max_chars: int = 500_000,
        sheet_name: str = "",
        sheet_range: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        normalized_id = _text(file_id, 256)
        if not normalized_id:
            raise KdocsDocumentError("invalid_request", "缺少文档 ID。", status_code=400)
        bounded_chars = max(1, min(int(max_chars or 500_000), 1_000_000))
        if continuation:
            page = read_page(self._cache_scope, normalized_id, continuation, bounded_chars)
            if page is None:
                raise KdocsDocumentError("snapshot_expired", "阅读快照已过期，请重新读取文档。", status_code=410)
            return page
        normalized_format = _text(format, 16).lower()
        if normalized_format not in {"markdown", "plain", "kdc"}:
            raise KdocsDocumentError("invalid_request", "不支持这个正文格式。", status_code=400)
        params: dict[str, Any] = {
            "file_id": normalized_id,
            "format": normalized_format,
            "enable_upload_medias": False,
        }
        if task_id:
            params["task_id"] = _text(task_id, 256)
        if sheet_name:
            params["sheet_name"] = _text(sheet_name, 240)
        if sheet_range:
            allowed = {"row_from", "row_to", "col_from", "col_to"}
            normalized_range = {
                key: max(0, int(value))
                for key, value in sheet_range.items()
                if key in allowed and isinstance(value, int)
            }
            if normalized_range:
                params["sheet_range"] = normalized_range
        data = self._call("read-file", params, timeout=120)
        status = _text(data.get("status"), 40).lower() or "done"
        if status in {"pending", "queued", "running"}:
            return {
                "status": status,
                "task_id": _text(data.get("task_id"), 256),
                "content": "",
                "content_format": "",
                "name": "",
                "truncated": False,
                "warnings": [],
            }
        raw_content = data.get("content")
        if isinstance(raw_content, str):
            content = raw_content
        elif raw_content is None:
            content = ""
        else:
            content = json.dumps(raw_content, ensure_ascii=False, separators=(",", ":"))
        content, omitted_embedded_images = _omit_embedded_image_payloads(content)
        bounded_chars = max(1, min(int(max_chars or 500_000), 1_000_000))
        truncated = bool(data.get("truncated") or data.get("has_more"))
        warnings = [
            _text(value, 1000)
            for value in (data.get("warnings") or [])
            if _text(value, 1000)
        ]
        if omitted_embedded_images:
            warnings.append("已省略文档中的内嵌图片数据。")
        limitations = []
        if omitted_embedded_images or re.search(r"!\[|<img\b", content, re.I):
            limitations.append("images")
        unknown = False
        for warning in warnings:
            if re.search(r"format|otl\.block_query|排版|格式", warning, re.I):
                limitations.append("formatting")
            elif re.search(r"image|图片", warning, re.I):
                limitations.append("images")
            else:
                unknown = True
        result = {
            "status": status,
            "task_id": _text(data.get("task_id"), 256),
            "content": content,
            "content_format": _text(data.get("content_format") or normalized_format, 40),
            "name": _text(data.get("name"), 240),
            "truncated": truncated,
            "warnings": warnings,
            "coverage": {"body": "partial" if truncated else "unknown" if unknown else "complete",
                         "limitations": sorted(set(limitations))},
            "continuation": "",
        }
        snapshot = store_read(self._cache_scope, normalized_id, result, self._cache_generation)
        if snapshot and content:
            page = read_page(self._cache_scope, normalized_id, f"{snapshot}:0", bounded_chars)
            if page is not None:
                return page
        result["content"] = content[:bounded_chars]
        if len(content) > bounded_chars:
            result["truncated"] = True
            result["coverage"]["body"] = "partial"
        return result

    def _call(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout: int,
    ) -> dict[str, Any]:
        executable = self._cli_finder()
        if executable is None:
            raise KdocsDocumentError(
                "cli_unavailable",
                "尚未安装金山文档连接组件。",
                status_code=503,
            )
        command = [
            str(executable),
            "drive",
            action,
            json.dumps(params, ensure_ascii=False, separators=(",", ":")),
            "--compact",
        ]
        env = build_sandboxed_tool_env()
        for name in ("KDOCS_CLI_CDN", "KDOCS_CLI_DIR", "KDOCS_CLI_VERSION"):
            value = os.environ.get(name)
            if value:
                env[name] = value
        try:
            result = self._command_runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise KdocsDocumentError(
                "timeout",
                "读取金山文档超时，请重试。",
                status_code=504,
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise KdocsDocumentError(
                "cli_unavailable",
                "金山文档连接组件暂时无法运行。",
                status_code=503,
            ) from exc
        try:
            payload = json.loads(result.stdout or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            if result.returncode != 0:
                raise KdocsDocumentError(
                    "provider_error",
                    "金山文档暂时无法完成这个请求。",
                ) from exc
            raise KdocsDocumentError("invalid_response", "金山文档返回了无法识别的结果。") from exc
        if not isinstance(payload, dict):
            if result.returncode != 0:
                raise KdocsDocumentError(
                    "provider_error",
                    "金山文档暂时无法完成这个请求。",
                )
            raise KdocsDocumentError("invalid_response", "金山文档返回了无法识别的结果。")
        outer_code = payload.get("code")
        if str(outer_code) not in {"0", "None", ""}:
            raise _error_for_code(outer_code)
        data = payload.get("data")
        if isinstance(data, dict) and "code" in data and isinstance(data.get("data"), dict):
            nested_code = data.get("code")
            if str(nested_code) not in {"0", "None", ""}:
                raise _error_for_code(nested_code)
            data = data["data"]
        if result.returncode != 0:
            raise KdocsDocumentError("provider_error", "金山文档暂时无法完成这个请求。")
        if not isinstance(data, dict):
            raise KdocsDocumentError("invalid_response", "金山文档返回了无法识别的结果。")
        return data
