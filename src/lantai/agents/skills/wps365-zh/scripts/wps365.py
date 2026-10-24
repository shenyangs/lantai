"""wps365.py — WPS 365 V7 非【文件】API 客户端（聊天、邮件、身份、AI知识库等）。

用法：
    import wps365
    result = wps365.im_recent_chats()
"""

import hashlib
import os
import struct
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests


_TIME_FIELDS = frozenset(
    (
        "ctime",
        "mtime",
        "create_time",
        "update_time",
        "start_time",
        "end_time",
        "regtime",
    )
)


def _ts_to_iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    try:
        t = int(ts)
    except:
        return None
    if t <= 0:
        return None
    if t > 1e12:
        t /= 1000.0
    return (
        datetime.utcfromtimestamp(t)
        .replace(tzinfo=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _normalize_times(data: Any) -> Any:
    if isinstance(data, dict):
        for k, v in list(data.items()):
            if k in _TIME_FIELDS and v is not None:
                converted = _ts_to_iso(v)
                if converted is not None:
                    # 数值时间戳 → ISO 字符串
                    data[k] = converted
                else:
                    # 非数值时间戳（如日历接口的嵌套 dict {"datetime": "..."}），
                    # 保留原值并递归处理内部字段，不强行覆盖为 None
                    data[k] = _normalize_times(v)
            else:
                data[k] = _normalize_times(v)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            data[i] = _normalize_times(item)
    return data


def _wrap(resp: Any) -> dict:
    if not isinstance(resp, dict):
        return {}
    out = dict(resp)
    if out.get("data") is not None:
        out["data"] = _normalize_times(out["data"])
    return out


def _make_result(
    success: bool, data: Any = None, message: str = "", error: str = ""
) -> dict:
    r: dict = {"success": success}
    if data is not None:
        r["data"] = data
    if message:
        r["message"] = message
    if error:
        r["error"] = error
    return r


def _iso_to_ts(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except:
        return None


def _to_ts(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return _iso_to_ts(str(v))


def _format_time_china(iso_time: str) -> str:
    if not iso_time:
        return ""
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except:
        return iso_time


def _extract_rich_text(elems: list) -> str:
    """从 rich_text elements 中提取纯文本。"""
    parts = []
    for e in elems:
        et = e.get("type", "")
        if et == "text":
            parts.append((e.get("style_content") or {}).get("text", ""))
        elif et == "emoji":
            parts.append((e.get("text_content") or {}).get("content", ""))
        elif et == "image":
            parts.append("[图片]")
        elif et == "doc":
            parts.append((e.get("doc_content") or {}).get("text", "[文档]"))
        elif et in ("nl", "ol", "ul"):
            sub = _extract_rich_text(e.get("elements", []))
            if sub:
                parts.append(sub)
            parts.append("\n")
    return "".join(parts).rstrip("\n")


def _format_msg_text(msg: dict) -> str:
    """将消息对象格式化为易读文本行：[时间] 发送者: 内容"""
    ctime = _format_time_china(msg.get("ctime", ""))
    sender = (msg.get("sender") or {}).get("name", "未知")
    content = msg.get("content") or {}
    mt = msg.get("type", "")
    if mt == "text":
        text = (content.get("text") or {}).get("content", "")
    elif mt == "rich_text":
        text = _extract_rich_text((content.get("rich_text") or {}).get("elements", []))
    elif mt == "file":
        fi = content.get("file") or {}
        fname = (fi.get("local") or fi.get("p2p") or {}).get("name") or "文件"
        cid = (fi.get("cloud") or {}).get("id", "")
        text = f"[文件: {fname}, ID: {cid}]" if cid else f"[文件: {fname}]"
    elif mt == "image":
        text = "[图片]"
    else:
        text = f"[{mt}消息]"
    return f"[{ctime}] {sender}: {text}"


def _format_mail_item(mail: dict) -> str:
    """将邮件对象格式化为易读文本行：[时间] 发件人 <email>: 主题（附件标记）"""
    ctime = _format_time_china(mail.get("ctime", ""))
    sender = mail.get("from") or {}
    sender_name = sender.get("name", "")
    sender_email = sender.get("email_address", "")
    sender_str = (
        f"{sender_name} <{sender_email}>" if sender_email else sender_name or "未知"
    )
    subject = mail.get("subject") or "（无主题）"
    has_att = "📎 " if mail.get("has_attachments") else ""
    preview = mail.get("body_preview", "")
    line = f"[{ctime}] {sender_str}: {has_att}{subject}"
    if preview:
        line += f" — {preview[:60]}{'...' if len(preview) > 60 else ''}"
    return line


def _sub_path(path: str, **kw: str) -> str:
    for k, v in kw.items():
        path = path.replace("{" + k + "}", str(v))
    if "{" in path:
        raise ValueError("未替换占位符: " + path)
    return path


def _get_image_dims(data: bytes) -> Tuple[int, int, str]:
    """解析图片，返回 (width, height, mime_type)。支持 PNG/JPEG/GIF/WebP。"""
    if len(data) < 12:
        raise ValueError("图片大小不足")
    # PNG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24:
            raise ValueError("PNG 大小不足")
        w = struct.unpack(">I", data[16:20])[0]
        h = struct.unpack(">I", data[20:24])[0]
        return w, h, "image/png"
    # JPEG
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 4 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                if i + 9 <= len(data):
                    h = struct.unpack(">H", data[i + 5 : i + 7])[0]
                    w = struct.unpack(">H", data[i + 7 : i + 9])[0]
                    return w, h, "image/jpeg"
            if i + 4 > len(data):
                break
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + seg_len
        raise ValueError("无法解析 JPEG 尺寸")
    # GIF
    if data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) < 10:
            raise ValueError("GIF 大小不足")
        w = struct.unpack("<H", data[6:8])[0]
        h = struct.unpack("<H", data[8:10])[0]
        return w, h, "image/gif"
    # WebP
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        chunk = data[12:16] if len(data) >= 16 else b""
        if chunk == b"VP8 " and len(data) >= 30:
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return w, h, "image/webp"
        elif chunk == b"VP8L" and len(data) >= 25:
            bits = struct.unpack("<I", data[21:25])[0]
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return w, h, "image/webp"
        elif chunk == b"VP8X" and len(data) >= 30:
            w = struct.unpack("<I", data[24:27] + b"\x00")[0] + 1
            h = struct.unpack("<I", data[27:30] + b"\x00")[0] + 1
            return w, h, "image/webp"
        return 0, 0, "image/webp"
    raise ValueError("不支持的图片格式（仅支持 PNG/JPEG/GIF/WebP）")


class _Session:
    _HDR = {
        "Origin": "https://365.kdocs.cn",
        "Referer": "https://365.kdocs.cn/woa/im/messages",
    }

    def __init__(self, base_url: str, sid: str):
        self._base = base_url.rstrip("/")
        self._sid = sid
        self._http = requests.Session()
        self._http.cookies.set("wps_sid", sid)
        self._http.cookies.set("csrf", sid)
        self._http.headers.update(self._HDR)

    def _parse(self, r: requests.Response) -> dict:
        if not r.content:
            return {}
        try:
            return r.json()
        except:
            # 非 JSON（如 text/vtt）：强制 UTF-8 解码，避免 requests 默认 latin-1 乱码
            text = r.content.decode("utf-8", errors="replace")
            return {"code": -1, "msg": "非JSON响应，解析失败", "text": text}

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._parse(
            self._http.get(
                f"{self._base}{path}",
                headers={"Content-Type": "application/json"},
                params=params,
                timeout=30,
            )
        )

    def post(
        self, path: str, body: Optional[dict] = None, params: Optional[dict] = None
    ) -> dict:
        return self._parse(
            self._http.post(
                f"{self._base}{path}",
                headers={"Content-Type": "application/json"},
                params=params,
                json=body,
                timeout=30,
            )
        )

    def post_form(
        self, path: str, files: Optional[dict] = None, params: Optional[dict] = None
    ) -> dict:
        return self._parse(
            self._http.post(
                f"{self._base}{path}", files=files, params=params, timeout=60
            )
        )



class Wps365Client:
    """WPS 365 V7 非【文件】类 API 客户端。base_url 可通过 WPS_API_BASE 配置，默认 https://api.wps.cn。"""

    _instance: Optional["Wps365Client"] = None

    def __new__(cls, base_url: Optional[str] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, base_url: Optional[str] = None):
        if hasattr(self, "_session"):
            return
        base = base_url or os.environ.get("WPS_API_BASE") or "https://api.wps.cn"
        resolved_sid = (
            os.environ.get("TMP_LX_UUID")
            or os.environ.get("WPS_SID")
        )
        if not resolved_sid:
            raise ValueError("缺少认证凭证")
        self._session = _Session(base, resolved_sid)
        self._session_kdocs: Optional[_Session] = None
        self._resolved_sid = resolved_sid
        self._user_cache: Dict[str, dict] = {}
        self._primary_mailbox_id: Optional[str] = None
        self._folder_id_cache: Dict[str, str] = {}
        self._current_user_id: Optional[str] = None

    def _req(
        self,
        method: str,
        path_template: str,
        path_params: Optional[Dict[str, str]] = None,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> dict:
        path = _sub_path(path_template, **(path_params or {}))
        if method.upper() == "GET":
            return _wrap(self._session.get(path, params=params))
        return _wrap(self._session.post(path, body=body, params=params or None))

    def _get_kdocs_session(self) -> "_Session":
        """返回指向 https://365.kdocs.cn 的会话（惰性初始化）。"""
        if self._session_kdocs is None:
            self._session_kdocs = _Session("https://365.kdocs.cn", self._resolved_sid)
        return self._session_kdocs

    # ── IM 便捷方法 ──────────────────────────────────────────────────────────

    def _upload_image(self, image_path: str) -> dict:
        """上传图片到 IM 会话资源存储，返回 message_image 所需数据。

        调用 POST /v7/chats/resources/upload，
        获取 storage_key 以及 upload_entry（上传地址、方法、头部、参数），
        然后按 upload_entry 实际上传文件。upload_entry 为空表示秒传，无需上传。

        返回: {"storage_key": str, "type": str, "width": int, "height": int, "size": int, "name": str}
        """
        with open(image_path, "rb") as f:
            data = f.read()

        size = len(data)
        sha256_hex = hashlib.sha256(data).hexdigest()
        name = os.path.basename(image_path)
        width, height, mime_type = _get_image_dims(data)

        r = _wrap(
            self._session.post(
                "/v7/chats/resources/upload",
                {"checksum": sha256_hex, "file_name": name, "file_size": size},
            )
        )
        if r.get("code") is not None and r.get("code") != 0:
            raise RuntimeError(
                f"获取图片上传地址失败: {r.get('msg', '未知错误')} | 原始响应: {r}"
            )

        d = r.get("data") or {}
        storage_key = d.get("storage_key", "")
        if not storage_key:
            raise RuntimeError(f"未获取到 storage_key | 原始响应: {r}")

        # 上传（upload_entry 为空表示秒传，跳过上传）
        upload_entry = d.get("upload_entry") or {}
        upload_url = upload_entry.get("url", "")
        if upload_url:
            upload_method = (upload_entry.get("method") or "PUT").upper()
            upload_headers = dict(upload_entry.get("headers") or {})
            upload_params = upload_entry.get("params") or {}
            resp = requests.request(
                upload_method, upload_url,
                data=data,
                headers=upload_headers,
                params=upload_params or None,
                timeout=120,
            )
            if resp.status_code not in (200, 201, 204):
                raise RuntimeError(f"图片文件上传失败，HTTP 状态码: {resp.status_code}")

        return {
            "storage_key": storage_key,
            "type": mime_type,
            "width": width,
            "height": height,
            "size": size,
            "name": name,
        }

    def _extract_chat_info(self, item: dict, include_unread: bool = True) -> dict:
        if "chat" in item:
            chat, unread_count = item["chat"], item.get("unread_count", 0)
        else:
            chat, unread_count = item, 0
        result = {
            "id": chat.get("id", ""),
            "name": chat.get("name", ""),
            "type": chat.get("type", ""),
        }
        if include_unread:
            result["unread_count"] = unread_count
        return result

    def _get_user_info(self, user_id: str) -> dict:
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            r = _wrap(self._session.get(f"/v7/users/{user_id}"))
            if r.get("code") == 0 and r.get("data"):
                d = r["data"]
                info: dict = {
                    "name": d.get("name")
                    or d.get("nick_name")
                    or d.get("user_name")
                    or f"User_{user_id[:8]}",
                    "avatar": d.get("avatar", ""),
                    "email": d.get("email", ""),
                }
                self._user_cache[user_id] = info
                return info
        except Exception:
            pass
        default: dict = {"name": f"User_{user_id[:8]}", "avatar": "", "email": ""}
        self._user_cache[user_id] = default
        return default

    def _get_current_user_id(self) -> Optional[str]:
        """懒加载获取当前登录用户的 ID，优先使用缓存。通过 GET /v7/users/current 获取。"""
        if self._current_user_id:
            return self._current_user_id
        try:
            r = _wrap(self._session.get("/v7/users/current"))
            if r.get("code") == 0 and r.get("data"):
                uid = r["data"].get("id")
                if uid:
                    self._current_user_id = str(uid)
                    return self._current_user_id
        except Exception:
            pass
        return None

    def _enrich_messages_with_user_info(self, messages: List[dict]) -> List[dict]:
        for msg in messages:
            if "sender" not in msg:
                continue
            s = msg["sender"]
            uid = s.get("id")
            if uid and "name" not in s:
                u = self._get_user_info(uid)
                s["name"] = u["name"]
                s["avatar"] = u["avatar"]
                if u.get("email"):
                    s["email"] = u["email"]
        return messages

    def im_recent_chats(
        self,
        page_size: int = 50,
        page_token: Optional[str] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        filter_unread: bool = False,
        filter_mention_me: bool = False,
    ) -> dict:
        """获取最近会话列表（含未读数）。GET /v7/recent_chats
        返回格式：
        {
            "success": True/False,
            "data": {
                "chats": [
                    {
                        "id": "会话ID",
                        "name": "会话名称",
                        "type": "p2p/group",
                        "unread_count": 0
                    }
                ],
                "count": 3,
                "total_unread": 5,
                "next_page_token": ""
            },
            "message": "获取到 3 个会话，共 5 条未读"
        }
        """
        try:
            params: dict = {
                "page_size": min(100, max(1, page_size)),
                "with_chat": True,
                "with_unread_count": True,
            }
            if page_token:
                params["page_token"] = page_token
            if start_time is not None:
                ts = _to_ts(start_time)
                if ts:
                    params["start_time"] = ts
            if end_time is not None:
                ts = _to_ts(end_time)
                if ts:
                    params["end_time"] = ts
            if filter_unread:
                params["filter_unread"] = True
            if filter_mention_me:
                params["filter_mention_me"] = True
            r = _wrap(self._session.get("/v7/recent_chats", params))
            if r.get("code") != 0:
                return _make_result(False, error=r.get("msg", "未知错误"))
            items = (r.get("data") or {}).get("items", [])
            chats = [self._extract_chat_info(x) for x in items]
            total_unread = sum(c["unread_count"] for c in chats)
            return _make_result(
                True,
                data={
                    "chats": chats,
                    "count": len(chats),
                    "total_unread": total_unread,
                    "next_page_token": (r.get("data") or {}).get("next_page_token", ""),
                },
                message=f"获取到 {len(chats)} 个会话，共 {total_unread} 条未读",
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def im_get_history(
        self,
        chat_id: str,
        page_size: int = 20,
        page_token: Optional[str] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
    ) -> dict:
        """获取指定会话历史消息，格式化为易读文本（自动填充发送者名称，正序）。GET /v7/chats/{chat_id}/messages
        start_time/end_time 接受 ISO 日期字符串（如 "2026-03-23T00:00:00+08:00"）。
        返回格式：
        {
            "success": True/False,
            "data": {
                "messages": [{"id": "消息ID", "text": "[2026-03-23 10:00:00] 张三: 你好"}, ...],
                "count": 20,
                "next_page_token": ""
            },
            "message": "获取到 20 条消息"
        }
        """
        try:
            use_default_time = start_time is None and end_time is None
            params: dict = {
                "page_size": min(50, max(1, page_size)),
                # 不传时间时用降序取最新的 N 条，再反转为正序展示
                "order": "desc" if use_default_time else "asc",
            }
            if page_token:
                params["page_token"] = page_token
            if start_time is not None:
                ts = _to_ts(start_time)
                if ts:
                    params["start_time"] = ts
            if end_time is not None:
                ts = _to_ts(end_time)
                if ts:
                    params["end_time"] = ts
            if use_default_time:
                now = datetime.now(timezone.utc)
                params["start_time"] = int((now - timedelta(hours=24)).timestamp())
                params["end_time"] = int(now.timestamp())
            r = _wrap(self._session.get(f"/v7/chats/{chat_id}/messages", params))
            if r.get("code") != 0:
                return _make_result(False, error=r.get("msg", "未知错误"))
            items = (r.get("data") or {}).get("items", [])
            if use_default_time:
                items = list(reversed(items))
            self._enrich_messages_with_user_info(items)
            formatted = [
                {"id": msg.get("id", ""), "text": _format_msg_text(msg)}
                for msg in items
            ]
            return _make_result(
                True,
                data={
                    "messages": formatted,
                    "count": len(formatted),
                    "next_page_token": (r.get("data") or {}).get("next_page_token", ""),
                },
                message=f"获取到 {len(formatted)} 条消息",
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def im_get_chat_members(
        self,
        chat_id: str,
        member_type: str = "user",
    ) -> dict:
        """获取指定会话的全部成员列表（自动翻页）。GET /v7/chats/{chat_id}/members
        返回格式：
        {
            "success": True/False,
            "data": {
                "members": [
                    {"id": "用户ID", "name": "张三"},
                    {"id": "用户ID", "name": "李四"},
                    ...
                ],
                "count": 2
            },
            "message": "获取到 2 位成员"
        }
        注意：id 为 identity.id，即用户身份ID，与 search_user 返回的用户 id 一致，可直接用于比对。
        """
        try:
            members: List[dict] = []
            page_token: Optional[str] = None
            while True:
                params: dict = {
                    "page_size": 100,
                    "type": member_type,
                    "with_member_detail": True,
                }
                if page_token:
                    params["page_token"] = page_token
                r = _wrap(self._session.get(f"/v7/chats/{chat_id}/members", params))
                if r.get("code") != 0:
                    return _make_result(False, error=r.get("msg", "未知错误"))
                items = (r.get("data") or {}).get("items", [])
                for item in items:
                    identity = item.get("identity") or {}
                    user_id = identity.get("id") or ""
                    name = identity.get("name") or ""
                    if user_id:
                        members.append({"id": user_id, "name": name})
                page_token = (r.get("data") or {}).get("next_page_token", "")
                if not page_token:
                    break
            return _make_result(
                True,
                data={"members": members, "count": len(members)},
                message=f"获取到 {len(members)} 位成员",
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def im_recall(self, chat_id: str, message_id: str) -> dict:
        """撤回指定会话消息。POST /v7/chats/{chat_id}/messages/{message_id}/recall
        返回格式：
        {
            "success": True/False,
            "message": "消息撤回成功",
            "error": "错误信息"
        }
        """
        try:
            r = _wrap(
                self._session.post(
                    f"/v7/chats/{chat_id}/messages/{message_id}/recall", {}
                )
            )
            if r.get("code") != 0:
                return _make_result(False, error=r.get("msg", "未知错误"))
            return _make_result(True, message="消息撤回成功")
        except Exception as e:
            return _make_result(False, error=str(e))

    def im_create_chat(
        self,
        account_id_list: List[str],
        chat_type: str = "group",
        name: Optional[str] = None,
    ) -> dict:
        """创建会话（单聊或群聊）。POST /v7/chats/create
        chat_type: "p2p"（单聊，account_id_list 须含自己和对方共 2 个 ID）
                   "group"（群聊，account_id_list 至少 1 个）
        返回格式：
        {
            "success": True/False,
            "data": {"chat_id": "...", "name": "...", "type": "group"},
            "message": "会话创建成功"
        }
        """
        try:
            current_user_id = self._get_current_user_id()
            if current_user_id:
                account_id_list = list(account_id_list)
                account_id_list.append(current_user_id)
                account_id_list = list(set(account_id_list))
            if chat_type == "p2p" and len(account_id_list) == 1:
                account_id_list = account_id_list * 2
            body: dict = {
                "type": chat_type,
                "account_id_list": [
                    {"id": uid, "type": "user"} for uid in account_id_list
                ],
            }
            if name:
                body["name"] = name
            if current_user_id:
                body["owner_id"] = current_user_id
            r = _wrap(self._session.post("/v7/chats/create", body))
            if r.get("code") != 0:
                return _make_result(False, error=r.get("msg", "未知错误"))
            chat = r.get("data") or {}
            return _make_result(
                True,
                data={
                    "chat_id": chat.get("id", ""),
                    "name": chat.get("name", ""),
                    "type": chat.get("type", ""),
                },
                message="会话创建成功",
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def im_send(
        self,
        chat_id: str,
        promo_text: str,
        text: Optional[str] = None,
        file_id: Optional[str] = None,
        mentions: Optional[List[str]] = None,
        image_paths: Optional[List[str]] = None,
        blocks: Optional[List[dict]] = None,
    ) -> dict:
        """向指定会话发送消息（文本 / 图片 / 云文档）。POST /v7/chats/{chat_id}/messages/create
        mentions: 要 @ 的列表，每项为用户 ID 或特殊值 "all"（@所有人），如 ["all", "17650706"]。
                会自动在消息末尾插入 <at> 标签并填充 API 的 mentions 字段。
        image_paths: 多张图片路径列表，可与 text 混排。
        blocks: 自由组合内容块列表，每项为 {"type": "text"/"image", "content"/"path": ...}，
                指定后忽略 text / image_paths 参数，允许任意顺序交叉排列。
                示例: [{"type": "text", "content": "说明"}, {"type": "image", "path": "/tmp/a.png"}]

        Returns:
            {
                "success": True/False,
                "message": "消息发送成功",
                "error": "错误信息（成功时不含此字段）"
            }
        """
        try:
            # ── 规范化内容块 ──
            if blocks is not None:
                raw_blocks = blocks
            else:
                raw_blocks = []
                # 将 image_paths / text 按顺序追加
                effective_image_paths: List[str] = []
                if image_paths:
                    effective_image_paths = list(image_paths)

                for p in effective_image_paths:
                    raw_blocks.append({"type": "image", "path": p})
                if text:
                    raw_blocks.append({"type": "text", "content": text})

            if not raw_blocks and not file_id:
                return _make_result(
                    False, error="必须提供 text、file_id、image_paths"
                )

            # ── 构建 mentions 列表 ──
            mentions_list: List[dict] = []
            for idx, item in enumerate(mentions or []):
                if item == "all":
                    mentions_list.append({"id": str(idx), "type": "all"})
                else:
                    uname = self._get_user_info(item).get("name") or item
                    mentions_list.append(
                        {
                            "id": str(idx),
                            "type": "user",
                            "identity": {"id": item, "name": uname, "type": "user"},
                        }
                    )

            _promo = (
                "\n\n---\n\n由灵犀 claw 发送" if not promo_text else "\n\n---" + promo_text
            )

            elements: List[dict] = []
            for blk in raw_blocks:
                if blk.get("type") == "image":
                    elements.append({"type": "image", "meta": self._upload_image(blk["path"])})
                elif blk.get("type") == "text":
                    elements.append({"type": "text", "content": blk["content"]})

            if file_id:
                r = _wrap(self._session.get(f"/v7/files/{file_id}/meta"))
                if r.get("code") != 0 or not r.get("data"):
                    return _make_result(False, error=r.get("msg") or "获取文件信息失败")
                file_meta = r["data"]
                elements.append(
                    {
                        "type": "file",
                        "link_url": file_meta.get("link_url", ""),
                        "link_id": file_meta.get("link_id", ""),
                    }
                )

            # ── 辅助：将文本 elem 构建为 rich_text 的 nl 节点 ──
            def _text_elem_to_nl(
                elem: dict,
                nl_index: int,
                with_mentions: bool = False,
                with_promo: bool = False,
            ) -> dict:
                """将 type=text 的 elem 转换为 rich_text nl 节点。"""
                inline: List[dict] = [
                    {
                        "type": "text",
                        "indent": 0,
                        "index": 0,
                        "alt_text": elem["content"],
                        "style_content": {
                            "text": elem["content"],
                            "style": {"bold": False, "italic": False, "color": "#000000FF"},
                        },
                    }
                ]
                if with_mentions:
                    for midx, m in enumerate(mentions_list):
                        if m["type"] == "all":
                            mc: dict = {"type": "all", "text": "@所有人"}
                        else:
                            identity = m.get("identity", {})
                            mc = {
                                "type": "user",
                                "text": f'@{identity.get("name", identity.get("id", ""))}',
                                "identity": {"id": identity["id"], "type": "user"},
                            }
                        inline.append(
                            {
                                "type": "mention",
                                "indent": 0,
                                "index": midx + 1,
                                "alt_text": mc["text"],
                                "mention_content": mc,
                            }
                        )
                if with_promo:
                    inline.append(
                        {
                            "type": "text",
                            "indent": 0,
                            "index": len(inline),
                            "alt_text": _promo,
                            "style_content": {
                                "text": _promo,
                                "style": {"bold": False, "italic": False, "color": "#000000FF"},
                            },
                        }
                    )
                return {
                    "type": "nl",
                    "indent": 0,
                    "index": nl_index,
                    "alt_text": elem["content"],
                    "elements": inline,
                }

            # ── 分离 file 元素与非 file 元素 ──
            non_file_elems = [e for e in elements if e["type"] != "file"]
            file_elems = [e for e in elements if e["type"] == "file"]

            last_text_idx = next(
                (i for i in range(len(non_file_elems) - 1, -1, -1) if non_file_elems[i]["type"] == "text"),
                None,
            )

            bodies: List[dict] = []
            if len(non_file_elems) == 0 and file_elems:
                # 仅 file
                elem = file_elems[0]
                bodies.append(
                    {
                        "type": "file",
                        "file": {
                            "type": "cloud",
                            "cloud": {
                                "id": file_id,
                                "link_url": elem["link_url"],
                                "link_id": elem["link_id"],
                            },
                        },
                    }
                )
            elif len(non_file_elems) == 1 and not file_elems:
                # 单一非 file 元素 → 发送对应类型消息
                elem = non_file_elems[0]
                if elem["type"] == "image":
                    bodies.append({"type": "image", "image": elem["meta"]})
                elif elem["type"] == "text":
                    at_tags = ""
                    for idx, m in enumerate(mentions_list):
                        if m["type"] == "all":
                            at_tags += f' <at id="{idx}">所有人</at>'
                        else:
                            uname = (m.get("identity") or {}).get("name") or m["identity"]["id"]
                            at_tags += f' <at id="{idx}">{uname}</at>'
                    text_body: dict = {
                        "type": "text",
                        "text": {"content": elem["content"] + at_tags + _promo, "type": "markdown"},
                    }
                    if mentions_list:
                        text_body["mentions"] = mentions_list
                    bodies.append(text_body)
            else:
                # 多元素混排：image / text 组成 rich_text，file 独占单独消息
                rich_nl: List[dict] = []

                for elem in non_file_elems:
                    if elem["type"] == "text":
                        rich_nl.append(
                            _text_elem_to_nl(
                                elem,
                                nl_index=len(rich_nl),
                                with_mentions=False,
                                with_promo=False,
                            )
                        )
                    elif elem["type"] == "image":
                        rich_nl.append(
                            {
                                "type": "nl",
                                "indent": 0,
                                "index": len(rich_nl),
                                "alt_text": "[图片]",
                                "elements": [
                                    {
                                        "type": "image",
                                        "indent": 0,
                                        "index": 0,
                                        "alt_text": "[图片]",
                                        "image_content": elem["meta"],
                                    }
                                ],
                            }
                        )

                # promo（和 mentions）始终作为独立行追加在所有内容块末尾
                promo_inline: List[dict] = []
                if mentions_list:
                    for midx, m in enumerate(mentions_list):
                        if m["type"] == "all":
                            mc: dict = {"type": "all", "text": "@所有人"}
                        else:
                            identity = m.get("identity", {})
                            mc = {
                                "type": "user",
                                "text": f'@{identity.get("name", identity.get("id", ""))}',
                                "identity": {"id": identity["id"], "type": "user"},
                            }
                        promo_inline.append(
                            {
                                "type": "mention",
                                "indent": 0,
                                "index": midx,
                                "alt_text": mc["text"],
                                "mention_content": mc,
                            }
                        )
                promo_inline.append(
                    {
                        "type": "text",
                        "indent": 0,
                        "index": len(promo_inline),
                        "alt_text": _promo,
                        "style_content": {
                            "text": _promo,
                            "style": {"bold": False, "italic": False, "color": "#000000FF"},
                        },
                    }
                )
                rich_nl.append(
                    {
                        "type": "nl",
                        "indent": 0,
                        "index": len(rich_nl),
                        "alt_text": _promo,
                        "elements": promo_inline,
                    }
                )

                if not rich_nl:
                    raise ValueError("消息为空")
                rich_body: dict = {"type": "rich_text", "rich_text": {"elements": rich_nl}}
                if mentions_list:
                    rich_body["mentions"] = mentions_list
                bodies.append(rich_body)

                for fe in file_elems:
                    bodies.append(
                        {
                            "type": "file",
                            "file": {
                                "type": "cloud",
                                "cloud": {
                                    "id": file_id,
                                    "link_url": fe["link_url"],
                                    "link_id": fe["link_id"],
                                },
                            },
                        }
                    )

            for body in bodies:
                res = _wrap(
                    self._session.post(f"/v7/chats/{chat_id}/messages/create", body)
                )
                if res.get("code") != 0:
                    return _make_result(False, error=res.get("msg", "消息发送失败"))

            return _make_result(True, message="消息发送成功")
        except Exception as e:
            return _make_result(False, error=str(e))

    def im_send_me(
        self,
        promo_text: str,
        text: Optional[str] = None,
        file_id: Optional[str] = None,
        mentions: Optional[List[str]] = None,
        image_paths: Optional[List[str]] = None,
        blocks: Optional[List[dict]] = None,
    ) -> dict:
        """向当前登录用户自己的单聊会话发送消息（自动获取 chat_id）。
        内部通过当前 sid 获取用户 ID，然后创建/获取与自己的 p2p 会话再发送。
        参数与 im_send 相同，但无需传 chat_id。
        """
        try:
            uid = self._get_current_user_id()
            if not uid:
                return _make_result(False, error="无法获取当前用户 ID")
            create_res = self.im_create_chat(
                chat_type="p2p",
                account_id_list=[uid],
            )
            if not create_res.get("success"):
                return _make_result(
                    False,
                    error=create_res.get("error") or "创建与自己的会话失败",
                )
            chat_id = (create_res.get("data") or {}).get("chat_id")
            if not chat_id:
                return _make_result(False, error="未获取到与自己的 chat_id")
            return self.im_send(
                chat_id=chat_id,
                promo_text=promo_text,
                text=text,
                file_id=file_id,
                mentions=mentions,
                image_paths=image_paths,
                blocks=blocks,
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    # ── 身份 ─────────────────────────────────────────────────────────────────

    def get_user(self, user_id: str) -> dict:
        """获取指定用户详细信息。GET /v7/users/{user_id}
        返回格式：
        {
            "success": True/False,
            "data": {
                "id": "用户ID",
                "user_name": "用户名",
                "email": "邮箱地址",
                "phone": "手机号"
            },
            "message": "获取用户信息成功"
        }
        """
        try:
            resp = self._req(
                "GET",
                "/v7/users/{user_id}",
                path_params={"user_id": str(user_id)},
            )
            if resp.get("code", -1) == 0:
                return _make_result(
                    True, data=resp.get("data") or {}, message="获取用户信息成功"
                )
            return _make_result(
                False, error=f"获取用户信息失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def search_user(
        self,
        keyword: str,
        page_size: int,
        status: Optional[List] = None,
        page_token: Optional[str] = None,
    ) -> dict:
        """搜索企业用户（按姓名/邮箱/手机/登录名）。GET /v7/users/search
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [
                    {
                        "id": "用户ID",
                        "user_name": "用户名",
                        "email": "邮箱地址",
                        "phone": "手机号"
                    }
                ],
                "count": 1,
                "next_page_token": ""
            },
            "message": "搜索到 1 个用户"
        }
        """
        try:
            if status is None:
                status = ["active"]
            _p: dict = {"keyword": keyword, "page_size": page_size, "status": status}
            if page_token is not None:
                _p["page_token"] = page_token
            resp = self._req("GET", "/v7/users/search", params=_p)
            if resp.get("code", -1) == 0:
                raw = resp.get("data") or {}
                raw_items = raw.get("items") or []
                items = [
                    {
                        "id": u.get("id", ""),
                        "user_name": u.get("user_name", ""),
                        "email": u.get("email", ""),
                        "phone": u.get("phone", ""),
                    }
                    for u in raw_items
                ]
                return _make_result(
                    True,
                    data={
                        "items": items,
                        "count": len(items),
                        "next_page_token": raw.get("next_page_token", ""),
                    },
                    message=f"搜索到 {len(items)} 个用户",
                )
            return _make_result(
                False, error=f"搜索用户失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    # ── 日程 ─────────────────────────────────────────────────────────────────

    def list_events_single_calendar(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
        calendar_id: str = "primary",
    ) -> dict:
        """查询日历日程列表。GET /v7/calendars/{calendar_id}/events
        start_time/end_time: RFC3339 格式，如 "2026-04-01T00:00:00+08:00"，区间需 ≤31 天。
        page_size: 默认 30，最大 100；数据未取完时响应会携带 next_page_token，需循环翻页。
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [
                    {
                        "id": "日程ID",
                        "summary": "标题",
                        "start_time": {"datetime": "2026-04-01T10:00:00+08:00"},
                        "end_time":   {"datetime": "2026-04-01T11:00:00+08:00"},
                        "organizer":  {"user_id": "...", "type": "user"},
                        "recurring_event_id": "父日程ID（重复日程子条目才有值）",
                        "recurrence": {...},
                        "status": "normal/cancelled",
                    }
                ],
                "count": 5,
                "next_page_token": "",
            },
            "message": "查询到 5 个日程"
        }

        """
        try:
            _p: dict = {}
            for k, v in [
                ("start_time", start_time),
                ("end_time", end_time),
                ("page_size", page_size),
                ("page_token", page_token),
            ]:
                if v is not None:
                    _p[k] = v
            resp = self._req(
                "GET",
                "/v7/calendars/{calendar_id}/events",
                path_params={"calendar_id": str(calendar_id)},
                params=_p,
            )
            if resp.get("code", -1) == 0:
                raw = resp.get("data") or {}
                items = raw.get("items") or []
                formatted = []
                for ev in items:
                    eid = ev.get("id", "")
                    summary = ev.get("summary") or "（无标题）"
                    st = (ev.get("start_time") or {}).get("datetime", "")
                    et = (ev.get("end_time") or {}).get("datetime", "")
                    st_fmt = _format_time_china(st) if st else ""
                    et_fmt = _format_time_china(et) if et else ""
                    status = ev.get("status", "normal")
                    time_range = (
                        f"{st_fmt} ~ {et_fmt}"
                        if st_fmt and et_fmt
                        else st_fmt or et_fmt
                    )
                    line = f"[{time_range}] {summary}（ID: {eid}，状态: {status}）"
                    if ev.get("recurring_event_id"):
                        line += f"（重复日程，父ID: {ev['recurring_event_id']}）"
                    formatted.append(line)
                return _make_result(
                    True,
                    data={
                        "events": formatted,
                        "count": len(formatted),
                        "next_page_token": raw.get("next_page_token", ""),
                    },
                    message=f"查询到 {len(formatted)} 个日程",
                )
            return _make_result(
                False, error=f"查询日程列表失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def get_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        """查询单个日程详情。GET /v7/calendars/{calendar_id}/events/{event_id}
        返回格式：
        {
            "success": True/False,
            "data": {
                "id": "日程ID",
                "summary": "标题",
                "description": "备注",
                "start_time": {"datetime": "2026-04-01T10:00:00+08:00"},
                "end_time":   {"datetime": "2026-04-01T11:00:00+08:00"},
                "organizer":  {"user_id": "...", "type": "user"},
                "recurrence": {"freq": "daily", "interval": 1, "exdate": [...]},
                "recurring_event_id": "（重复子日程才有值）",
                "online_meeting": {"url": "...", "join_code": "..."},
                "locations": [{"name": "会议室名称"}],
                "status": "normal/cancelled"
            },
            "message": "查询日程成功"
        }
        """
        try:
            resp = self._req(
                "GET",
                "/v7/calendars/{calendar_id}/events/{event_id}",
                path_params={
                    "calendar_id": str(calendar_id),
                    "event_id": str(event_id),
                },
                params={},
            )
            if resp.get("code", -1) == 0:
                ev = resp.get("data") or {}
                def _extract_time(raw) -> str:
                    if isinstance(raw, dict):
                        val = raw.get("datetime") or raw.get("date", "")
                    else:
                        val = raw or ""
                    return _format_time_china(val) if val else ""

                st = _extract_time(ev.get("start_time"))
                et = _extract_time(ev.get("end_time"))
                locations = [
                    loc.get("name", "")
                    for loc in (ev.get("locations") or [])
                    if loc.get("name")
                ]
                online_url = (ev.get("online_meeting") or {}).get("url", "")
                data = {
                    "id": ev.get("id", ""),
                    "summary": ev.get("summary") or "（无标题）",
                    "description": ev.get("description", ""),
                    "start_time": st,
                    "end_time": et,
                    "organizer_user_id": (ev.get("organizer") or {}).get("user_id", ""),
                    "status": ev.get("status", "normal"),
                    "locations": locations,
                    "online_meeting_url": online_url,
                    "recurrence": ev.get("recurrence") or {},
                    "recurring_event_id": ev.get("recurring_event_id", ""),
                }
                return _make_result(True, data=data, message="查询日程成功")
            return _make_result(
                False, error=f"查询日程失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def create_event(
        self, body: Optional[dict] = None, calendar_id: str = "primary"
    ) -> dict:
        """创建日程（仅新建，不处理参与者）。POST /v7/calendars/{calendar_id}/events/create
        body 支持字段：
            summary:       标题
            description:   备注
            start_time:    {"datetime": "2026-04-01T10:00:00+08:00"} 或全天 {"date": "2026-04-01"}
            end_time:      格式同上
            location:      地址
            recurrence:    重复规则
        返回格式：
        {
            "success": True/False,
            "data": {"id": "新日程ID", ...},
            "message": "创建日程成功"
        }
        """
        try:
            resp = self._req(
                "POST",
                "/v7/calendars/{calendar_id}/events/create",
                path_params={"calendar_id": str(calendar_id)},
                body=dict(body or {}),
            )
            if resp.get("code", -1) == 0:
                return _make_result(
                    True, data=resp.get("data") or {}, message="创建日程成功"
                )
            return _make_result(
                False, error=f"创建日程失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def delete_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        """删除日程。POST /v7/calendars/{calendar_id}/events/{event_id}/delete
        返回格式：
        {
            "success": True/False,
            "data": {},
            "message": "删除日程成功"
        }
        """
        try:
            resp = self._req(
                "POST",
                "/v7/calendars/{calendar_id}/events/{event_id}/delete",
                path_params={
                    "calendar_id": str(calendar_id),
                    "event_id": str(event_id),
                },
                body=None,
            )
            if resp.get("code", -1) == 0:
                return _make_result(True, data={}, message="删除日程成功")
            return _make_result(
                False, error=f"删除日程失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def list_event_attendees(
        self,
        event_id: str,
        page_size: int = 100,
        page_token: Optional[str] = None,
        calendar_id: str = "primary",
    ) -> dict:
        """获取日程参与者列表。GET /v7/calendars/{calendar_id}/events/{event_id}/attendees
        page_token: 翻页 token，首次不传。
        **注意**：若 next_page_token 非空，需循环传入 page_token 继续获取，否则会漏掉部分参与者。
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [
                    {
                        "id": "参与者ID",
                        "user_id": "用户ID",
                        "name": "用户名称",
                        "type": "user/group",
                        "response_status": "accepted/not_responded/declined/tentative"
                    }
                ],
                "count": 5,
                "next_page_token": ""
            },
            "message": "获取到 5 个参与者"
        }
        response_status 取值：accepted=已接受, declined=已拒绝, tentative=暂定, not_responded=未响应
        """
        _STATUS_ZH = {
            "accepted": "已接受",
            "declined": "已拒绝",
            "tentative": "暂定",
            "not_responded": "未响应",
        }
        try:
            _p: dict = {"page_size": page_size}
            if page_token:
                _p["page_token"] = page_token
            resp = self._req(
                "GET",
                "/v7/calendars/{calendar_id}/events/{event_id}/attendees",
                path_params={
                    "calendar_id": str(calendar_id),
                    "event_id": str(event_id),
                },
                params=_p,
            )
            if resp.get("code", -1) == 0:
                raw = resp.get("data") or {}
                items = raw.get("items") or []
                attendees = [
                    f"{a.get('name', '未知')}（{_STATUS_ZH.get(a.get('response_status', ''), a.get('response_status', '未知'))}）"
                    for a in items
                ]
                return _make_result(
                    True,
                    data={
                        "attendees": attendees,
                        "count": len(attendees),
                        "next_page_token": raw.get("next_page_token", ""),
                    },
                    message=f"获取到 {len(attendees)} 个参与者",
                )
            return _make_result(
                False, error=f"获取参与者列表失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def batch_create_event_attendee(
        self, event_id: str, attendees: List[dict], calendar_id: str = "primary"
    ) -> dict:
        """批量添加日程参与者。POST /v7/calendars/{calendar_id}/events/{event_id}/attendees/batch_create
        attendees: 参与者列表，每项 {"type": "user", "user_id": "uid_xxx"}，每次最多 200 人。
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [{"id": "...", "user_id": "...", "name": "...", "response_status": "..."}]
            },
            "message": "添加日程参与者成功"
        }
        """
        try:
            resp = self._req(
                "POST",
                "/v7/calendars/{calendar_id}/events/{event_id}/attendees/batch_create",
                path_params={
                    "calendar_id": str(calendar_id),
                    "event_id": str(event_id),
                },
                body={"attendees": list(attendees)},
            )
            if resp.get("code", -1) == 0:
                return _make_result(
                    True, data=resp.get("data") or {}, message="添加日程参与者成功"
                )
            return _make_result(
                False, error=f"添加日程参与者失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def batch_delete_event_attendee(
        self, event_id: str, attendee_ids: List[str], calendar_id: str = "primary"
    ) -> dict:
        """批量删除日程参与者。POST /v7/calendars/{calendar_id}/events/{event_id}/attendees/batch_delete
        attendee_ids: 参与者 id 列表，可从 list_event_attendees 获取，每次最多 50 个。
        返回格式：
        {
            "success": True/False,
            "data": {},
            "message": "删除日程参与者成功"
        }
        """
        try:
            resp = self._req(
                "POST",
                "/v7/calendars/{calendar_id}/events/{event_id}/attendees/batch_delete",
                path_params={
                    "calendar_id": str(calendar_id),
                    "event_id": str(event_id),
                },
                body={"attendee_ids": list(attendee_ids)},
            )
            if resp.get("code", -1) == 0:
                return _make_result(True, data={}, message="删除日程参与者成功")
            return _make_result(
                False, error=f"删除日程参与者失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def list_free_busy(
        self,
        start_time: str,
        end_time: str,
        user_ids: Optional[List] = None,
        room_ids: Optional[List] = None,
    ) -> dict:
        """查询用户/会议室忙闲（区间≤7天，user_ids/room_ids 至少传一个）。GET /v7/free_busy_list
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [
                    {
                        "user_id": "用户ID（若查用户忙闲）",
                        "room_id": "会议室ID（若查会议室忙闲）",
                        "busy_times": [
                            {"start": "2026-04-01T10:00:00+08:00", "end": "2026-04-01T11:00:00+08:00"}
                        ]
                    }
                ]
            },
            "message": "查询忙闲成功"
        }
        busy_times 为空列表表示该用户/会议室在指定时段内完全空闲。
        """
        try:
            _p: dict = {
                "start_time": start_time,
                "end_time": end_time,
                "calendar_type": "primary",
            }
            if user_ids is not None:
                _p["user_ids"] = user_ids
            if room_ids is not None:
                _p["room_ids"] = room_ids
            resp = self._req("GET", "/v7/free_busy_list", params=_p)
            if resp.get("code", -1) == 0:
                raw_items = ((resp.get("data") or {}).get("items")) or []
                items = [
                    {
                        "user_id": fb.get("user_id", ""),
                        "room_id": fb.get("room_id", ""),
                        "busy_times": [
                            {"start": bt.get("start", ""), "end": bt.get("end", "")}
                            for bt in (fb.get("busy_times") or [])
                        ],
                    }
                    for fb in raw_items
                ]
                return _make_result(True, data={"items": items}, message="查询忙闲成功")
            return _make_result(
                False, error=f"查询忙闲失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    # ── 聊天 ─────────────────────────────────────────────────────────────────

    def search_chats(
        self,
        page_size: int,
        keyword: str,
        page_token: Optional[str] = None,
    ) -> dict:
        """按关键字搜索单聊/群聊（按会话名称匹配，不搜消息内容）。GET /v7/chats/search
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [
                    {
                        "id": "会话ID",       # 注意是 id，不是 chat_id
                        "name": "会话名称",
                        "type": "p2p/group",
                        "ctime": "创建时间"
                    }
                ],
                "count": 2,
                "total": 10,
                "next_page_token": ""
            },
            "message": "搜索到 2 个会话"
        }
        """
        try:
            _p: dict = {"page_size": page_size, "keyword": keyword}
            if page_token is not None:
                _p["page_token"] = page_token
            resp = self._req("GET", "/v7/chats/search", params=_p)
            if resp.get("code", -1) == 0:
                raw = resp.get("data") or {}
                raw_items = raw.get("items") or []
                items = [
                    {
                        "id": item.get("chat", {}).get("id", ""),
                        "name": item.get("chat", {}).get("name", ""),
                    }
                    for item in raw_items
                ]
                return _make_result(
                    True,
                    data={
                        "items": items,
                        "count": len(items),
                        "next_page_token": raw.get("next_page_token", ""),
                    },
                    message=f"搜索到 {len(items)} 个会话",
                )
            return _make_result(
                False, error=f"搜索会话失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def search_messages(
        self,
        page_size: int,
        keyword: Optional[str] = None,
        chat_id_list: Optional[List] = None,
        sender_id_list: Optional[List] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        filter_chat_type_list: Optional[List] = None,
        with_chat: bool = True,
        page_token: Optional[str] = None,
    ) -> dict:
        """搜索消息内容（不是会话名称）。keyword/chat_id_list/时间范围 三选一必填。GET /v7/messages/search
        with_chat 默认 True，每项结果自动附带所在会话信息（item["chat"]）。
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [
                    {
                        "message": {
                            "id": "消息ID",
                            "type": "text/rich_text/file/...",
                            "content": {...},
                            "ctime": "发送时间",
                            "sender": {"id": "发送者ID", ...}
                        },
                        "chat": {                    # 仅传 with_chat=True 时存在
                            "id": "会话ID",
                            "name": "会话名称",
                            "type": "p2p/group"
                        }
                    }
                ],
                "count": 5,
                "next_page_token": ""
            },
            "message": "搜索到 5 条消息"
        }
        """
        try:
            _p: dict = {"page_size": page_size}
            for k, v in [
                ("keyword", keyword),
                ("chat_id_list", chat_id_list),
                ("sender_id_list", sender_id_list),
                ("filter_chat_type_list", filter_chat_type_list),
                ("with_chat", with_chat),
                ("page_token", page_token),
            ]:
                if v is not None:
                    _p[k] = v
            if start_time is not None:
                ts = _to_ts(start_time)
                if ts:
                    _p["start_time"] = ts
            if end_time is not None:
                ts = _to_ts(end_time)
                if ts:
                    _p["end_time"] = ts
            resp = self._req("GET", "/v7/messages/search", params=_p)
            if resp.get("code", -1) == 0:
                raw = resp.get("data") or {}
                items = raw.get("items") or []
                formatted = []
                for item in items:
                    msg = item.get("message") or {}
                    chat = item.get("chat") or {}
                    chat_name = chat.get("name", "")
                    line = _format_msg_text(msg)
                    if chat_name:
                        line = line.replace("]: ", f"（会话: {chat_name}）: ", 1)
                    formatted.append(line)
                return _make_result(
                    True,
                    data={
                        "messages": formatted,
                        "count": len(formatted),
                        "next_page_token": raw.get("next_page_token", ""),
                    },
                    message=f"搜索到 {len(formatted)} 条消息",
                )
            return _make_result(
                False, error=f"搜索消息失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    # ── 邮件 ─────────────────────────────────────────────────────────────────

    def get_primary_mailbox_id(self) -> str:
        """获取当前用户主邮箱 ID（带缓存，首次调用会请求 /v7/mailboxes）。"""
        if self._primary_mailbox_id:
            return self._primary_mailbox_id
        r = _wrap(self._session.get("/v7/mailboxes"))
        if r.get("code") != 0:
            raise RuntimeError(f"获取邮箱列表失败: {r.get('msg', '未知错误')}")
        items = (r.get("data") or {}).get("items", [])
        primary = next((mb for mb in items if mb.get("is_primary")), None)
        if primary is None and items:
            primary = items[0]
        if primary is None:
            raise RuntimeError("未找到可用邮箱")
        self._primary_mailbox_id = str(primary["id"])
        return self._primary_mailbox_id

    # 系统文件夹别名集合（get_mail_message 接口不支持别名，需解析为真实 ID）
    _FOLDER_ALIASES = frozenset({"inbox", "drafts", "sent", "junk", "trash"})

    def _resolve_folder_id(
        self, folder_id: str, mailbox_id: Optional[str] = None
    ) -> str:
        """将文件夹别名（inbox/sent/drafts/junk/trash）解析为真实 folder ID（带缓存）。
        get_mail_folder 接口支持别名，返回的 data.id 才是真实 ID。
        """
        if folder_id not in self._FOLDER_ALIASES:
            return folder_id
        if not mailbox_id:
            mailbox_id = self.get_primary_mailbox_id()
        cache_key = f"{mailbox_id}:{folder_id}"
        if cache_key in self._folder_id_cache:
            return self._folder_id_cache[cache_key]
        r = _wrap(self._session.get(f"/v7/mailboxes/{mailbox_id}/folders/{folder_id}"))
        if r.get("code") == 0:
            real_id = str((r.get("data") or {}).get("id") or folder_id)
            self._folder_id_cache[cache_key] = real_id
            return real_id
        return folder_id

    def upload_mail_attachment(
        self,
        file_path: str,
        mailbox_id: Optional[str] = None,
    ) -> dict:
        """上传本地文件为邮件附件。POST /v7/mailboxes/{mailbox_id}/attachments/create
        返回格式：
        {
            "success": True/False,
            "data": {"url": "附件URL"},
            "message": "附件上传成功"
        }
        """
        try:
            if not mailbox_id:
                mailbox_id = self.get_primary_mailbox_id()
            if not os.path.isfile(file_path):
                return _make_result(False, error=f"文件不存在: {file_path}")
            file_size = os.path.getsize(file_path)
            filename = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            # multipart/form-data：content 为文件二进制，size 为整数字段
            files: dict = {
                "content": (filename, file_bytes, "application/octet-stream"),
                "size": (None, str(file_size)),
            }
            path = _sub_path(
                "/v7/mailboxes/{mailbox_id}/attachments/create",
                mailbox_id=str(mailbox_id),
            )
            r = _wrap(self._session.post_form(path, files=files))
            if r.get("code") == 0:
                url = (r.get("data") or {}).get("url", "")
                return _make_result(True, data={"url": url}, message="附件上传成功")
            return _make_result(
                False, error=f"上传附件失败: {r.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=f"上传附件失败: {str(e)}")

    def list_mail_messages_in_folder(
        self,
        page_size: int,
        folder_id: str = "inbox",
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        page_token: Optional[str] = None,
        mailbox_id: Optional[str] = None,
    ) -> dict:
        """获取指定邮箱目录下的邮件列表（folder_id 默认 inbox，支持 sent/drafts/junk/trash）。
        GET /v7/mailboxes/{mailbox_id}/folders/{folder_id}/messages
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [
                    {
                        "id": "邮件ID",
                        "folder_id": "目录ID",
                        "subject": "邮件主题",
                        "from": {"name": "发件人", "email_address": "xxx@wps.cn"},
                        "is_read": True,
                        "ctime": "发送时间",
                        "has_attachments": False,
                        "body_preview": "正文预览"
                    }
                ],
                "count": 10,
                "next_page_token": ""
            },
            "message": "获取到 10 封邮件"
        }
        """
        try:
            if not mailbox_id:
                mailbox_id = self.get_primary_mailbox_id()
            _p: dict = {"page_size": page_size}
            if start_time is not None:
                ts = _to_ts(start_time)
                if ts:
                    _p["start_time"] = ts
            if end_time is not None:
                ts = _to_ts(end_time)
                if ts:
                    _p["end_time"] = ts
            if page_token is not None:
                _p["page_token"] = page_token
            resp = self._req(
                "GET",
                "/v7/mailboxes/{mailbox_id}/folders/{folder_id}/messages",
                path_params={
                    "mailbox_id": str(mailbox_id),
                    "folder_id": str(folder_id),
                },
                params=_p,
            )
            if resp.get("code", -1) == 0:
                raw = resp.get("data") or {}
                items = raw.get("items") or []
                mails = [
                    {"id": m.get("id", ""), "line": _format_mail_item(m)} for m in items
                ]
                return _make_result(
                    True,
                    data={
                        "mails": mails,
                        "count": len(mails),
                        "next_page_token": raw.get("next_page_token", ""),
                    },
                    message=f"获取到 {len(mails)} 封邮件",
                )
            return _make_result(
                False, error=f"获取目录邮件列表失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def get_mail_message(
        self,
        message_id: str,
        folder_id: str = "inbox",
        mailbox_id: Optional[str] = None,
    ) -> dict:
        """获取指定邮件完整内容。GET /v7/mailboxes/{mailbox_id}/folders/{folder_id}/messages/{message_id}
        mailbox_id 不传时自动使用主邮箱。
        message_id 对应列表中的 id 字段（不是 message_id 字段）。
        folder_id 默认 "inbox"；若邮件来自搜索/列表结果，建议传 msg["folder_id"] 确保准确。
        返回格式：
        {
            "success": True/False,
            "data": {
                "body_text": "纯文本正文",    # 推荐用此字段；body 字段是原始 HTML，直接 print 会显示空白
                "subject": "邮件主题",
                "from": {"name": "发件人", "email_address": "xxx@wps.cn"},
                "to_recipients": [{"name": "收件人", "email_address": "..."}],
                "ctime": "发送时间",
                "attachments": []
            },
            "message": "获取邮件成功"
        }
        """
        try:
            if not mailbox_id:
                mailbox_id = self.get_primary_mailbox_id()
            # get_mail_message 接口不接受 "inbox" 等别名，需解析为真实 folder ID
            real_folder_id = self._resolve_folder_id(folder_id, mailbox_id)
            resp = self._req(
                "GET",
                "/v7/mailboxes/{mailbox_id}/folders/{folder_id}/messages/{message_id}",
                path_params={
                    "mailbox_id": str(mailbox_id),
                    "folder_id": real_folder_id,
                    "message_id": str(message_id),
                },
            )
            if resp.get("code", -1) == 0:
                data = resp.get("data") or {}
                html_body = data.get("body", "") or ""
                if html_body:
                    try:
                        from bs4 import BeautifulSoup

                        data["body_text"] = BeautifulSoup(
                            html_body, "html.parser"
                        ).get_text(separator="\n", strip=True)
                    except ImportError:
                        import re

                        data["body_text"] = re.sub(r"<[^>]+>", "", html_body).strip()
                else:
                    data["body_text"] = ""
                return _make_result(True, data=data, message="获取邮件成功")
            return _make_result(
                False, error=f"获取邮件失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def search_mail_messages(
        self,
        keyword: str,
        type_: str,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
        mailbox_id: Optional[str] = None,
    ) -> dict:
        """搜索邮件（type_ 可选 sender/subject/body）。GET /v7/mailboxes/{mailbox_id}/messages
        返回格式：
        {
            "success": True/False,
            "data": {
                "items": [...],    # 字段同 list_mail_messages_in_folder
                "count": 5,
                "next_page_token": ""
            },
            "message": "搜索到 5 封邮件"
        }
        传给 get_mail_message 时用 msg["id"] 作为 message_id，msg["folder_id"] 作为 folder_id。
        """
        try:
            if not mailbox_id:
                mailbox_id = self.get_primary_mailbox_id()
            _p: dict = {"keyword": keyword, "type": type_}
            if start_time is not None:
                ts = _to_ts(start_time)
                if ts:
                    _p["start_time"] = ts
            if end_time is not None:
                ts = _to_ts(end_time)
                if ts:
                    _p["end_time"] = ts
            for k, v in [
                ("page_size", page_size),
                ("page_token", page_token),
            ]:
                if v is not None:
                    _p[k] = v
            resp = self._req(
                "GET",
                "/v7/mailboxes/{mailbox_id}/messages",
                path_params={"mailbox_id": str(mailbox_id)},
                params=_p,
            )
            if resp.get("code", -1) == 0:
                raw = resp.get("data") or {}
                items = raw.get("items") or []
                mails = [
                    {"id": m.get("id", ""), "line": _format_mail_item(m)} for m in items
                ]
                return _make_result(
                    True,
                    data={
                        "mails": mails,
                        "count": len(mails),
                        "next_page_token": raw.get("next_page_token", ""),
                    },
                    message=f"搜索到 {len(mails)} 封邮件",
                )
            return _make_result(
                False, error=f"搜索邮件失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def create_mail_draft(
        self, mailbox_id: Optional[str] = None, body: Optional[dict] = None
    ) -> dict:
        """
        创建一封新的草稿并将其存于用户草稿箱中。POST /v7/mailboxes/{mailbox_id}/messages/create
        mailbox_id: 邮箱ID（路径参数，必填）
        body 支持字段：
            subject:         邮件主题（str）
            to_recipients:   收件人列表（list[dict]），每项 {"name": "...", "email_address": "..."}
            cc_recipients:   抄送列表（list[dict]），格式同上
            bcc_recipients:  密送列表（list[dict]），格式同上
            body:            邮件正文，HTML 或纯文本字符串
            attachment_urls: 附件 URL 列表（list[str]）
        返回格式：
        {
            "success": True/False,
            "data": {"id": "草稿邮件ID"},
            "message": "创建草稿成功"
        }
        """
        try:
            if not mailbox_id:
                mailbox_id = self.get_primary_mailbox_id()
            _b: dict = dict(body or {})
            resp = self._req(
                "POST",
                "/v7/mailboxes/{mailbox_id}/messages/create",
                path_params={"mailbox_id": str(mailbox_id)},
                body=_b,
            )
            if resp.get("code", -1) == 0:
                return _make_result(
                    True, data=resp.get("data") or {}, message="创建草稿成功"
                )
            return _make_result(
                False, error=f"创建草稿失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=f"创建草稿失败: {str(e)}")

    def send_mail_message(
        self, message_id: str, mailbox_id: Optional[str] = None
    ) -> dict:
        """
        将草稿箱中的指定邮件投递出去。
        mailbox_id: 邮箱ID（路径参数，不传则自动使用主邮箱）
        message_id: 草稿邮件ID（路径参数，必填）
        返回格式：
        {
            "success": True/False,
            "data": {},
            "message": "发送邮件成功",
            "error": "错误信息（成功时为空）"
        }
        """
        try:
            if not mailbox_id:
                mailbox_id = self.get_primary_mailbox_id()
            resp = self._req(
                "POST",
                "/v7/mailboxes/{mailbox_id}/messages/{message_id}/send",
                path_params={
                    "mailbox_id": str(mailbox_id),
                    "message_id": str(message_id),
                },
                body={},
            )
            if resp.get("code", -1) == 0:
                return _make_result(
                    True, data=resp.get("data") or {}, message="发送邮件成功"
                )
            return _make_result(
                False, error=f"发送邮件失败: {resp.get('msg', '未知错误')}"
            )
        except Exception as e:
            return _make_result(False, error=f"发送邮件失败: {str(e)}")

    def send_mail(
        self,
        subject: str,
        to_recipients: List[dict],
        body: str,
        cc_recipients: Optional[List[dict]] = None,
        bcc_recipients: Optional[List[dict]] = None,
        attachment_files: Optional[List[str]] = None,
        attachment_urls: Optional[List[str]] = None,
        mailbox_id: Optional[str] = None,
    ) -> dict:
        """发送邮件（"传附件 → 创建草稿 → 发送"）。
        mailbox_id 不传则使用主邮箱。

        attachment_files: 本地文件路径列表。
            函数会自动调用 upload_mail_attachment 上传，上传失败立即返回错误。
        attachment_urls: 已有的可直接下载的 HTTP URL 列表。
            注意：WPS 云文档 link_url（https://365.kdocs.cn/l/xxx）是页面链接，
            不是下载链接，直接传入无法生成附件；本地文件请使用 attachment_files 参数。

        不可重复调用（非幂等）：
          - 若返回 success=True，邮件已发出，请勿重试
          - 若返回"草稿已创建但发送失败"，请仅调用
            send_mail_message(message_id=data["draft_id"]) 重试发送，
            切勿重新调用 send_mail()，否则会重复发送

        to_recipients / cc_recipients / bcc_recipients 每项格式：
            {"name": "张三", "email_address": "zhangsan@wps.cn"}

        返回格式：
        {
            "success": True/False,
            "data": {"message_id": "已发送邮件ID"},   # success=True 时存在
            "message": "邮件发送成功",
            "error": "错误描述",
            # 附件上传失败：error 说明哪个文件失败
            # 草稿已创建但发送失败：data={"draft_id": "xxx"}，可用于重试 send_mail_message
        }
        """
        try:
            if not mailbox_id:
                mailbox_id = self.get_primary_mailbox_id()

            # ── 上传本地附件 ──────────────────────────────────────────
            all_attachment_urls: List[str] = list(attachment_urls or [])
            for file_path in attachment_files or []:
                upload_result = self.upload_mail_attachment(
                    file_path=file_path, mailbox_id=mailbox_id
                )
                if not upload_result.get("success"):
                    return _make_result(
                        False,
                        error=f"附件上传失败（{file_path}）: {upload_result.get('error', '未知错误')}",
                    )
                url = (upload_result.get("data") or {}).get("url", "")
                if url:
                    all_attachment_urls.append(url)

            # ── 创建草稿 ─────────────────────────────────────────────
            draft_body: dict = {
                "subject": subject,
                "to_recipients": to_recipients,
                "body": body,
            }
            if cc_recipients:
                draft_body["cc_recipients"] = cc_recipients
            if bcc_recipients:
                draft_body["bcc_recipients"] = bcc_recipients
            if all_attachment_urls:
                draft_body["attachment_urls"] = all_attachment_urls

            draft_result = self.create_mail_draft(
                mailbox_id=mailbox_id, body=draft_body
            )
            if not draft_result.get("success"):
                return _make_result(
                    False,
                    error=f"草稿创建失败（可重试 send_mail）: {draft_result.get('error', '未知错误')}",
                )
            message_id = (draft_result.get("data") or {}).get("id")
            if not message_id:
                return _make_result(False, error="创建草稿成功但未返回邮件 ID")

            # ── 发送草稿 ─────────────────────────────────────────────
            send_result = self.send_mail_message(
                message_id=message_id, mailbox_id=mailbox_id
            )
            if send_result.get("success"):
                return _make_result(
                    True,
                    data={"message_id": message_id},
                    message="邮件发送成功",
                )
            # 草稿已创建，仅发送步骤失败 —— 返回 draft_id 供重试
            return _make_result(
                False,
                data={"draft_id": message_id},
                error=(
                    f"草稿已创建但发送失败（请勿重调 send_mail，"
                    f"改用 send_mail_message(message_id='{message_id}') 重试）: "
                    f"{send_result.get('error', '未知错误')}"
                ),
            )
        except Exception as e:
            return _make_result(False, error=f"发送邮件失败: {str(e)}")

    # ── AI 知识库  ─────────────────────────────────────────────────────

    def _list_aidocs_drive_ids(self) -> List[str]:
        """获取全部可访问知识库的 drive_id 列表（内部使用）。GET /wiki/api/v1/doclib/list"""
        r = _wrap(
            self._get_kdocs_session().get(
                "/wiki/api/v1/doclib/list",
                params={"page_size": 300, "filter_status": "success"},
            )
        )
        if r.get("code") != 0:
            msg = r.get("msg") or r.get("message") or "获取知识库列表失败"
            raise ValueError(f"{msg}（code={r.get('code')}）")
        d = r.get("data") or {}
        items = d.get("items", []) if isinstance(d, dict) else []
        return [item["drive_id"] for item in items if item.get("drive_id")]

    def recall_rank(
        self,
        query: str,
        topk: int = 5,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        _batch_size: int = 10,
    ) -> dict:
        """在全部可访问知识库中按问题召回相关片段。
        自动获取所有 drive_id，分批（每批 _batch_size 个）请求后聚合，按 ref_score 降序取 topk。
        start_time/end_time 接受 ISO 日期字符串（如 "2026-03-23T00:00:00+08:00"）。
        返回格式：
        {
            "success": True/False,
            "data": {
                "chunks": [
                    {
                        "file_name": "文件名",
                        "ref_score": 0.9123,
                        "content": "召回片段正文（最长 500 字符）",
                        "link_url": "https://365.kdocs.cn/l/xxx",
                        "file_id": "末段文件 ID"
                    }
                ],
                "count": 3
            },
            "message": "召回到 3 条片段（共扫描 N 个知识库）"
        }
        """
        try:
            drive_ids = self._list_aidocs_drive_ids()
            if not drive_ids:
                return _make_result(False, error="未找到可访问的知识库")

            # 构造公共参数
            extra: dict = {}
            if start_time is not None:
                ts = _to_ts(start_time)
                if ts is not None:
                    extra["start_time"] = ts
            if end_time is not None:
                ts = _to_ts(end_time)
                if ts is not None:
                    extra["end_time"] = ts

            # 分批请求，每批 _batch_size 个 drive_id，每批多取 topk 条保证精度
            all_chunks: List[dict] = []
            for i in range(0, len(drive_ids), _batch_size):
                batch = drive_ids[i : i + _batch_size]
                body: dict = {
                    "query": query,
                    "drives": [{"drive_id": d} for d in batch],
                    "topk": topk,
                    **extra,
                }
                r = _wrap(
                    self._session.post("/v7/docqa/instore/recall/rank", body=body)
                )
                if r.get("code") != 0:
                    msg = r.get("msg") or r.get("message") or "未知错误"
                    return _make_result(
                        False,
                        error=f"批次 {i // _batch_size + 1} 召回失败: {msg}（code={r.get('code')}）",
                    )
                batch_chunks = r.get("data") or []
                if isinstance(batch_chunks, list):
                    all_chunks.extend(batch_chunks)

            # 按 ref_score 降序聚合，取 topk
            all_chunks.sort(key=lambda c: c.get("ref_score", 0), reverse=True)
            chunks = all_chunks[:topk]
            for c in chunks:
                if "content" in c and len(c["content"]) > 500:
                    c["content"] = c["content"][:500] + "..."
                link = c.get("link_url", "")
                if link and "/" in link:
                    c["file_id"] = link.rstrip("/").rsplit("/", 1)[-1]
            return _make_result(
                True,
                data={"chunks": chunks, "count": len(chunks)},
                message=f"召回到 {len(chunks)} 条片段（共扫描 {len(drive_ids)} 个知识库）",
            )
        except Exception as e:
            return _make_result(False, error=str(e))


    def _find_meeting_by_event(self, event_id: str) -> dict:
        """通过日程 ID 找到对应的 meeting 对象。
        返回格式：
        {
            "success": True/False,
            "data": {
                "meeting": meeting_dict,
                "ev_start_ts": int,
                "ev_end_ts": int
            },
            "error": "错误信息"
        }
        策略：
        1. 直接用 _session.get 获取日程原始数据（绕过 _normalize_times，
           避免 start_time 被当作数值时间戳而破坏嵌套 dict 结构）
        2. 支持全天事件（date 字段）和具体时间事件（datetime 字段）
        3. 带时间窗口查询会议列表；若有多个同 calendar_event_id 的会议，
           优先返回 ended+有 transcripts 的那条
        4. 若日程没有具体时间，回退到最近 90 天窗口搜索
        """
        # 直接调用 _session.get，不经过 _wrap/_normalize_times，
        # 保留 start_time/end_time 的原始嵌套结构 {"datetime": "..."}
        raw_ev = self._session.get(f"/v7/calendars/primary/events/{event_id}")
        if raw_ev.get("code", -1) != 0:
            return _make_result(False, error=f"获取日程失败: {raw_ev.get('msg', '未知错误')}")
        ev = raw_ev.get("data") or {}
        join_code = (ev.get("online_meeting") or {}).get("join_code", "")
        meeting_url = (ev.get("online_meeting") or {}).get("url", "")
        if not join_code and meeting_url and "/meeting/" in meeting_url:
            join_code = meeting_url.rsplit("/meeting/", 1)[-1]

        # 兼容普通事件（datetime 字段）和全天事件（date 字段）
        ev_start_raw = ev.get("start_time") or {}
        ev_end_raw   = ev.get("end_time")   or {}
        ev_start = ev_start_raw.get("datetime") or ev_start_raw.get("date", "")
        ev_end   = ev_end_raw.get("datetime")   or ev_end_raw.get("date", "")
        start_ts = _iso_to_ts(ev_start)
        end_ts   = _iso_to_ts(ev_end)

        # 若日程没有具体时间，使用最近 90 天作为搜索窗口
        if not start_ts:
            import time
            now = int(time.time())
            start_ts = now - 90 * 86400
            end_ts = now + 86400

        params: dict = {
            "start_date_time": start_ts - 86400,
            "end_date_time": (end_ts or start_ts) + 86400,
        }

        r = _wrap(self._session.get("/v7/meetings", params=params))
        if r.get("code") != 0:
            return _make_result(False, error=f"获取会议列表失败: {r.get('msg', '')}")
        meetings = (r.get("data") or {}).get("items") or []

        def _pick_best(candidates: list) -> dict:
            """优先级：ended+有transcripts > ended+无transcripts > booking。"""
            ended_with = [m for m in candidates if m.get("type") == "ended" and m.get("transcripts")]
            if ended_with:
                return ended_with[0]
            ended = [m for m in candidates if m.get("type") == "ended"]
            return ended[0] if ended else candidates[0]

        matched = [m for m in meetings if m.get("calendar_event_id") == str(event_id)]
        if matched:
            return _make_result(
                True,
                data={"meeting": _pick_best(matched), "ev_start_ts": start_ts, "ev_end_ts": end_ts or start_ts},
            )

        if join_code:
            jc_matched = []
            for m in meetings:
                if m.get("join_code") == join_code:
                    jc_matched.append(m)
                    continue
                m_url = m.get("join_url", "")
                if m_url and "/meeting/" in m_url:
                    if m_url.rsplit("/meeting/", 1)[-1] == join_code:
                        jc_matched.append(m)
            if jc_matched:
                return _make_result(
                    True,
                    data={"meeting": _pick_best(jc_matched), "ev_start_ts": start_ts, "ev_end_ts": end_ts or start_ts},
                )

        return _make_result(
            False,
            error=f"未找到日程 {event_id} 对应的会议（已搜索 {len(meetings)} 条会议记录）",
        )

    def _list_meeting_summaries(self, event_id: str) -> dict:
        """（内部方法）获取指定日程对应会议的全部 AI 总结列表，供 get_meeting_summary 调用。"""
        try:
            find_result = self._find_meeting_by_event(event_id)
            if not find_result["success"]:
                return _make_result(False, error=find_result.get("error", "查找会议失败"))
            meeting = find_result["data"]["meeting"]
            real_meeting_id = meeting.get("id", "")
            raw_transcripts = meeting.get("transcripts") or []
            # 嵌入列表可能为空，显式调用 /transcripts 接口兜底
            if not raw_transcripts and real_meeting_id:
                tr = self._session.get(f"/v7/meetings/{real_meeting_id}/transcripts")
                raw_transcripts = (tr.get("data") or {}).get("items") or []
            items = [
                {k: v for k, v in t.items() if k not in ("state", "ctime", "mtime")}
                for t in raw_transcripts
            ]
            return _make_result(
                True,
                data={"items": items, "count": len(items), "_meeting_id": real_meeting_id},
                message=f"获取到 {len(items)} 条会议 AI 总结",
            )
        except Exception as e:
            return _make_result(False, error=str(e))

    def _fetch_transcript_content(self, meeting_id: str, transcript_id: str) -> dict:
        """通过 /content_json 接口获取转写正文，解析为 '发言人: 文本' 格式。
        返回格式：
        {
            "success": True/False,
            "data": {"content": "文本内容"},
            "error": "错误信息（仅 success=False 时存在）"
        }
        """
        r = self._session.get(f"/v7/meetings/{meeting_id}/transcripts/{transcript_id}/content_json")
        code = r.get("code")
        if str(code).startswith("403"):
            return _make_result(False, error=f"无权限获取转写内容（transcript_id={transcript_id}）")
        if code != 0:
            return _make_result(False, error=f"获取转写内容失败: {r.get('msg', '未知错误')}")
        paragraphs = (r.get("data") or {}).get("paragraphs") or []
        lines: List[str] = []
        for para in paragraphs:
            speaker = (para.get("speaker") or {}).get("name", "").strip()
            sentences = para.get("sentenses") or []
            text = "".join(s.get("text", "") for s in sentences).strip()
            if text:
                lines.append(f"{speaker}: {text}" if speaker else text)
        return _make_result(True, data={"content": "\n".join(lines)})

    def get_meeting_summary(self, event_id: str) -> dict:
        """获取指定日程对应会议的 AI 转写，将全部转写内容合并为一段文本返回。
        内容超过 1000 字符时，返回前 1000 字符并将完整内容保存为本地 Markdown 文件。
        event_id 为日程 ID（从 list_events_single_calendar / get_event 结果中获取）。
        返回格式：
        {
            "success": True/False,
            "data": {
                "title": "会议标题",
                "content": "发言人A: 内容...（最多 1000 字符）",
                "saved_path": "/path/to/file.md"   # 仅内容超 1000 字符时存在
            },
            "message": "..."
        }
        """
        try:
            find_result = self._find_meeting_by_event(event_id)
            if not find_result["success"]:
                return _make_result(False, error=find_result.get("error", "查找会议失败"))
            meeting = find_result["data"]["meeting"]
            real_meeting_id = meeting.get("id", "")

            raw_transcripts = meeting.get("transcripts") or []
            # 会议列表接口中嵌入的 transcripts 可能不完整（服务端有时返回空列表
            # 即使转写已生成），因此若嵌入列表为空则显式调用 /transcripts 接口兜底
            if not raw_transcripts and real_meeting_id:
                tr = self._session.get(f"/v7/meetings/{real_meeting_id}/transcripts")
                raw_transcripts = (tr.get("data") or {}).get("items") or []

            if not raw_transcripts:
                return _make_result(
                    True,
                    data={"title": "", "content": ""},
                    message="该会议暂无 AI 总结",
                )

            title = raw_transcripts[0].get("title", "")

            # 拉取所有转写正文并合并
            parts: List[str] = []
            for t in raw_transcripts:
                tid = t.get("id", "")
                fetch_result = self._fetch_transcript_content(real_meeting_id, tid)
                if not fetch_result["success"]:
                    return _make_result(False, error=fetch_result.get("error", f"无权限获取会议「{title}」的转写内容"))
                content = (fetch_result.get("data") or {}).get("content", "")
                if content:
                    parts.append(content)
            full_content = "\n\n".join(parts)

            if not full_content:
                return _make_result(
                    True,
                    data={"title": title, "content": ""},
                    message="该会议转写内容为空",
                )

            LIMIT = 1000
            if len(full_content) <= LIMIT:
                return _make_result(
                    True,
                    data={"title": title, "content": full_content},
                    message=f"获取会议「{title}」AI 总结成功",
                )

            # 超过限制：截断展示，完整内容写入本地文件
            truncated = full_content[:LIMIT]
            workspace = os.environ.get("WORKSPACE_DIR") or "/tmp"
            safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in title)
            save_path = os.path.join(workspace, f"meeting_summary_{event_id}_{safe_title}.md")
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(f"# {title}\n\n{full_content}")
            return _make_result(
                True,
                data={"title": title, "content": truncated, "saved_path": save_path},
                message=f"获取会议「{title}」AI 总结成功（内容较长，完整版已保存至 {save_path}）",
            )
        except Exception as e:
            return _make_result(False, error=str(e))


def _get() -> Wps365Client:
    return Wps365Client()


def im_recent_chats(*args, **kwargs):
    return _get().im_recent_chats(*args, **kwargs)


def im_get_history(*args, **kwargs):
    return _get().im_get_history(*args, **kwargs)


def im_get_chat_members(*args, **kwargs):
    return _get().im_get_chat_members(*args, **kwargs)


def im_recall(*args, **kwargs):
    return _get().im_recall(*args, **kwargs)


def im_create_chat(*args, **kwargs):
    return _get().im_create_chat(*args, **kwargs)


def im_send(*args, **kwargs):
    return _get().im_send(*args, **kwargs)


def im_send_me(*args, **kwargs):
    return _get().im_send_me(*args, **kwargs)


def get_user(*args, **kwargs):
    return _get().get_user(*args, **kwargs)


def search_user(*args, **kwargs):
    return _get().search_user(*args, **kwargs)


def list_events_single_calendar(*args, **kwargs):
    return _get().list_events_single_calendar(*args, **kwargs)


def get_event(*args, **kwargs):
    return _get().get_event(*args, **kwargs)


def create_event(*args, **kwargs):
    return _get().create_event(*args, **kwargs)


def delete_event(*args, **kwargs):
    return _get().delete_event(*args, **kwargs)


def list_event_attendees(*args, **kwargs):
    return _get().list_event_attendees(*args, **kwargs)


def batch_create_event_attendee(*args, **kwargs):
    return _get().batch_create_event_attendee(*args, **kwargs)


def batch_delete_event_attendee(*args, **kwargs):
    return _get().batch_delete_event_attendee(*args, **kwargs)


def list_free_busy(*args, **kwargs):
    return _get().list_free_busy(*args, **kwargs)


def search_chats(*args, **kwargs):
    return _get().search_chats(*args, **kwargs)


def search_messages(*args, **kwargs):
    return _get().search_messages(*args, **kwargs)


def get_primary_mailbox_id(*args, **kwargs):
    return _get().get_primary_mailbox_id(*args, **kwargs)


def upload_mail_attachment(*args, **kwargs):
    return _get().upload_mail_attachment(*args, **kwargs)


def list_mail_messages_in_folder(*args, **kwargs):
    return _get().list_mail_messages_in_folder(*args, **kwargs)


def get_mail_message(*args, **kwargs):
    return _get().get_mail_message(*args, **kwargs)


def search_mail_messages(*args, **kwargs):
    return _get().search_mail_messages(*args, **kwargs)


def create_mail_draft(*args, **kwargs):
    return _get().create_mail_draft(*args, **kwargs)


def send_mail_message(*args, **kwargs):
    return _get().send_mail_message(*args, **kwargs)


def send_mail(*args, **kwargs):
    return _get().send_mail(*args, **kwargs)


def recall_rank(*args, **kwargs):
    return _get().recall_rank(*args, **kwargs)


def get_meeting_summary(event_id: str) -> dict:
    return _get().get_meeting_summary(event_id)
