---
name: wps365
description: "提供WPS协作（IM应用，也叫woa）、WPS邮箱的操作能力。涵盖聊天、日程、会议（包括纪要等）、邮件、身份、知识库等功能。WPS云文档操作请使用'kdocs'（WPS云文档）技能"
metadata:
  builtin_skill_version: "1.0"
---

# Wps365 SKILL

> **重要：** 所有 `scripts/` 路径均相对于本技能目录。
> 运行方式：`cd {this_skill_dir} && python scripts/wps365.py`
> 或在 Python 中先把 `scripts/` 加入 `sys.path` 后再 `import wps365`。

## 澜台原生路由与安全

- 本 Skill 只处理已连接的 WPS 365 账号能力；WPS 云文档正文读写优先交给 `kdocs`。
- 使用澜台环境与凭证存储，不从其他产品私有目录、Cookie 文件或聊天正文读取登录凭证。
- 查询、列举和读取属于只读；发送消息、创建/修改日程、邀请参会人、上传或覆盖文档属于外部写入，执行前确认准确目标。
- 不在输出、日志、Skill 目录或生成文件中回显 Token、Secret、Cookie 和完整个人联系方式。
- API 不可用时报告连接状态与缺失权限，不尝试安装其他平台客户端或绕过鉴权。

## 快速开始

```python
import sys
sys.path.insert(0, "scripts")
import wps365
```

> `start_time` / `end_time` 参数统一接受 ISO 字符串（如 `"2026-03-23T00:00:00+08:00"`）。

## 聊天

### wps365.im_recent_chats(page_size=50, filter_unread=False, filter_mention_me=False, page_token=None, start_time=None, end_time=None)

获取最近会话列表（含未读数）

| 参数 | 说明 |
|------|------|
| page_size | 返回数量 |
| filter_unread | 仅返回有未读的会话 |
| filter_mention_me | 仅返回 @我 的会话 |
| page_token | 翻页 token |
| start_time | 起始时间 |
| end_time | 结束时间 |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "chats": [
            {"id": "会话ID", "name": "会话名称", "type": "p2p/group", "unread_count": 0}
        ],
        "count": 3,
        "total_unread": 5,
        "next_page_token": ""
    },
    "message": "获取到 3 个会话，共 5 条未读"
}
```

**调用示例**

```python
result = wps365.im_recent_chats(filter_unread=True)
print(result)
```

### wps365.im_get_history(chat_id, page_size=20, page_token=None, start_time=None, end_time=None)

获取指定会话历史消息

| 参数 | 说明 |
|------|------|
| chat_id | 会话 ID |
| page_size | 返回数量，默认 20，最大 50 |
| page_token | 翻页 token |
| start_time | 起始时间 |
| end_time | 结束时间 |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "messages": [{"id": "消息ID", "text": "[2026-03-23 10:00:00] 张三: 你好"}],
        "count": 20,
        "next_page_token": ""
    },
    "message": "获取到 20 条消息"
}
```

**调用示例**

```python
result = wps365.im_get_history(chat_id="oc_xxx", page_size=20, start_time="2026-03-23T00:00:00+08:00")
print(result)
```

### wps365.im_get_chat_members(chat_id)

获取指定会话的全部成员列表

| 参数 | 说明 |
|------|------|
| chat_id | 会话 ID |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "members": [{"id": "用户ID", "name": "张三"}],
        "count": 5
    },
    "message": "获取到 5 位成员"
}
```

**调用示例**

```python
result = wps365.im_get_chat_members(chat_id="xxx")
print(result)
```

### wps365.im_recall(chat_id, message_id)

撤回指定会话中的某条消息

| 参数 | 说明 |
|------|------|
| chat_id | 会话 ID |
| message_id | 消息 ID |

**返回值**

```python
{
    "success": True/False,
    "message": "消息撤回成功",
    "error": "错误信息（成功时不含此字段）"
}
```

**调用示例**

```python
result = wps365.im_recall(chat_id="xxx", message_id="yyy")
print(result)
```

### wps365.im_send_me(promo_text, text=None, file_id=None, image_paths=None, blocks=None)

向当前登录用户自己发送消息

| 参数 | 说明 |
|------|------|
| text | 文本内容 |
| file_id | 云文档链接 ID |
| image_paths | 本地图片路径列表 |
| blocks | 自由组合内容块列表，每项为 `{"type": "text"/"image", "content"/"path": ...}`，指定后忽略 `text`/`image_path`/`image_paths` |
| promo_text | 必填，用户自定义消息后缀/签名；传 `""` 时会自动附加默认后缀 |

**返回值**

```python
{
    "success": True/False,
    "message": "消息发送成功",
    "error": "错误信息（成功时不含此字段）"
}
```

**调用示例**

```python
result = wps365.im_send_me(promo_text="", text="给自己的备忘")
print(result)
```

### wps365.im_send(chat_id, promo_text, text=None, file_id=None, mentions=None, image_paths=None, blocks=None)

向指定会话发送消息，支持文本、单/多图片、云文档的自由组合

| 参数 | 说明 |
|------|------|
| chat_id | 会话 ID |
| text | 文本内容|
| file_id | 云文档链接 ID。即 `https://365.kdocs.cn/l/{file_id}` 中的那段，例如链接 `https://365.kdocs.cn/l/cuXBzEWuDnxL`，file_id = `"cuXBzEWuDnxL"` |
| image_paths | 本地图片路径列表，可与 `text` 组合发送 |
| blocks | 自由组合内容块列表（**推荐用于多图多文字交叉排列**），每项为 `{"type": "text", "content": "..."}` 或 `{"type": "image", "path": "..."}` |
| mentions | @ 列表，每项为用户 ID(纯数字) 或 `"all"`（@所有人），如 `["all", "17650706"]` |
| promo_text | 必填，用户自定义消息后缀/签名；传 `""` 时会自动附加默认后缀 |

**注意**：使用 `mentions` 参数时，`text` / `blocks` 中不要写入 `@名字`，@ 标记由系统自动追加，否则会重复显示且纯文本 @ 不会触发通知。
**注意**：使用 `promo_text` 参数时，若用户未指定消息后缀 / 签名，需读取用户记忆中「# --- 发送消息 ---」下的「用户发消息时常用后缀 / 签名(该字段仅供用户通过协作发送消息时使用)：」字段；若用户主动指定，则需将其更新记录到用户记忆中。

**返回值**

```python
{
    "success": True/False,
    "message": "消息发送成功",
    "error": "错误信息（成功时不含此字段）"
}
```

**调用示例**

```python
result = wps365.im_send(chat_id="oc_xxx", text="你好")
print(result)
```

### wps365.search_chats(page_size, keyword, page_token=None)

按会话名称关键字搜索单聊/群聊

| 参数 | 说明 |
|------|------|
| page_size | 返回数量（1-50） |
| keyword | 搜索关键字 |
| page_token | 翻页 token |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "items": [{"id": "会话ID", "name": "会话名称"}],
        "count": 2,
        "next_page_token": ""
    },
    "message": "搜索到 2 个会话"
}
```

**调用示例**

```python
result = wps365.search_chats(page_size=20, keyword="项目讨论")
print(result)
```

### wps365.search_messages(page_size, keyword=None, chat_id_list=None, start_time=None, end_time=None, filter_chat_type_list=None, page_token=None)

搜索消息内容（`keyword` / `chat_id_list` / 时间范围 三选一必填）

| 参数 | 说明 |
|------|------|
| page_size | 返回数量 |
| keyword | 消息内容关键字 |
| chat_id_list | 指定会话 ID 列表 |
| start_time | 起始时间 |
| end_time | 结束时间 |
| page_token | 翻页 token |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "items": [
            {
                "message": {"id": "消息ID", "type": "text/rich_text/...", "content": {...}, "ctime": "发送时间", "sender": {"id": "...", ...}},
                "chat": {"id": "会话ID", "name": "会话名称", "type": "p2p/group"}
            }
        ],
        "count": 5,
        "next_page_token": ""
    },
    "message": "搜索到 5 条消息"
}
```

**调用示例**

```python
result = wps365.search_messages(page_size=20, keyword="项目进度")
print(result)
```

### wps365.im_create_chat(account_id_list, name=None)

创建会话

| 参数 | 说明 |
|------|------|
| account_id_list | 成员用户 ID 列表，已默认包含当前用户 |
| chat_type | 会话类型（默认 group）；`p2p` 仅用于创建和某人的单聊对话，`group` 仅用于创建多人群聊 |
| name | 群聊名称 |

**返回值**

```python
{
    "success": True/False,
    "data": {"chat_id": "oc_xxx", "name": "项目讨论群", "type": "group"},
    "message": "会话创建成功"
}
```

**调用示例**

```python
result = wps365.im_create_chat(
    account_id_list=["111", "222", "333"],
    name="项目讨论群",
)
print(result)
```

## 身份


### wps365.get_user(user_id)

获取指定用户详细信息

| 参数 | 说明 |
|------|------|
| user_id | 用户 ID |

**返回值**

```python
{
    "success": True/False,
    "data": {"id": "用户ID", "user_name": "张三", "email": "zhangsan@wps.cn", "phone": "138xxxx"},
    "message": "获取用户信息成功"
}
```

**调用示例**

```python
result = wps365.get_user(user_id="123456")
print(result)
```

### wps365.search_user(keyword, page_size, status=None, page_token=None)

搜索企业用户（按姓名/邮箱/手机/登录名）

| 参数 | 说明 |
|------|------|
| keyword | 搜索关键字 |
| page_size | 返回数量 |
| status | 用户状态过滤 |
| page_token | 翻页 token |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "items": [{"id": "用户ID", "user_name": "张三", "email": "zhangsan@wps.cn", "phone": "138xxxx"}],
        "count": 1,
        "next_page_token": ""
    },
    "message": "搜索到 1 个用户"
}
```

**调用示例**

```python
result = wps365.search_user(keyword="张三", page_size=20)
print(result)
```

## 日程

> `event_id` 从列表结果中获取。

### wps365.list_events_single_calendar(start_time=None, end_time=None, page_size=None, page_token=None)

查询日历日程列表（区间≤31天）。

| 参数 | 说明 |
|------|------|
| start_time | 开始时间 |
| end_time | 结束时间 |
| page_size | 每页数量 |
| page_token | 翻页 token |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "events": ["[2026-03-01 10:00 ~ 11:00] 团队周会（ID: 125744232，状态: normal）"],
        "count": 5,
        "next_page_token": ""
    },
    "message": "查询到 5 个日程"
}
```

```python
result = wps365.list_events_single_calendar(
    start_time="2026-03-01T00:00:00+08:00",
    end_time="2026-03-31T23:59:59+08:00"
)
print(result)
```

### wps365.get_event(event_id)

查询单个日程详情。

| 参数 | 说明 |
|------|------|
| event_id | 日程 ID（纯数字，不带日期后缀） |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "id": "日程ID",
        "summary": "标题",
        "description": "备注",
        "start_time": "2026-04-01 10:00 (北京时间)",
        "end_time": "2026-04-01 11:00 (北京时间)",
        "organizer_user_id": "...",
        "status": "normal/cancelled",
        "locations": ["会议室名称"],
        "online_meeting_url": "https://...",
        "recurrence": {},
        "recurring_event_id": ""
    },
    "message": "查询日程成功"
}
```

```python
result = wps365.get_event(event_id="125744232")
print(result)
```

### wps365.create_event(body=None)

创建日程（仅新建，不处理参与者，邀请参与者须在创建后单独调用 `batch_create_event_attendee`）。

**返回值**

```python
{
    "success": True/False,
    "data": {"id": "新日程ID", ...},
    "message": "创建日程成功"
}
```

```python
result = wps365.create_event(body={
    "summary": "团队周会",
    "start_time": {"dateTime": "2026-03-15T10:00:00+08:00"},
    "end_time":   {"dateTime": "2026-03-15T11:00:00+08:00"},
})
print(result)
```


### wps365.delete_event(event_id)

删除日程。


**返回值**

```python
{"success": True/False, "data": {}, "message": "删除日程成功"}
```

```python
result = wps365.delete_event(event_id="125744232")
print(result)
```

### wps365.list_event_attendees(event_id, page_size=100, page_token=None)

获取日程参与者列表。

| 参数 | 说明 |
|------|------|
| event_id | 日程 ID |
| page_size | 每页数量，默认 100 |
| page_token | 翻页 token |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "attendees": ["张三（已接受）", "李四（未响应）"],
        "count": 2,
        "next_page_token": ""
    },
    "message": "获取到 2 个参与者"
}
```

```python
result = wps365.list_event_attendees(event_id="125744232")
print(result)
```

### wps365.batch_create_event_attendee(event_id, attendees)

向已有日程批量添加参与者。

| 参数 | 说明 |
|------|------|
| attendees | 参与者列表，每项 `{"type": "user", "user_id": "uid_xxx"}` |

**返回值**

```python
{
    "success": True/False,
    "data": {"items": [{"id": "...", "user_id": "...", "name": "...", "response_status": "not_responded"}]},
    "message": "添加日程参与者成功"
}
```

```python
result = wps365.batch_create_event_attendee(
    event_id="125744232",
    attendees=[{"type": "user", "user_id": "uid_aaa"}]
)
print(result)
```

### wps365.batch_delete_event_attendee(event_id, attendee_ids)

批量删除日程参与者。

| 参数 | 说明 |
|------|------|
| attendee_ids | 参与者 `id` 列表 |

**返回值**

```python
{"success": True/False, "data": {}, "message": "删除日程参与者成功"}
```

```python
result = wps365.batch_delete_event_attendee(
    event_id="125744232",
    attendee_ids=["att_aaa"]
)
print(result)
```

### wps365.list_free_busy(start_time, end_time, user_ids)

查询用户忙闲

| 参数 | 说明 |
|------|------|
| start_time | 开始时间 |
| end_time | 结束时间 |
| user_ids | 要查询的用户 ID 列表 |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "items": [
            {
                "user_id": "用户ID",
                "busy_times": [{"start": "2026-03-15T10:00:00+08:00", "end": "2026-03-15T11:00:00+08:00"}]
            }
        ]
    },
    "message": "查询忙闲成功"
}
```

```python
result = wps365.list_free_busy(
    start_time="2026-03-15T09:00:00+08:00",
    end_time="2026-03-15T18:00:00+08:00",
    user_ids=["uid_aaa", "uid_bbb"]
)
print(result)
```


## 邮件

### wps365.list_mail_messages_in_folder(page_size, start_time=None, end_time=None, page_token=None)

获取指定邮箱目录的邮件列表

| 参数 | 说明 |
|------|------|
| page_size | 返回数量 |
| start_time | 起始时间 |
| end_time | 结束时间 |
| page_token | 翻页 token |

**返回值**

```python
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
```

**调用示例**

```python
result = wps365.list_mail_messages_in_folder(page_size=20)
print(result)
```


### wps365.get_mail_message(message_id)

获取指定邮件完整内容

| 参数 | 说明 |
|------|------|
| message_id | 邮件 ID（对应邮件列表结果中的 `["id"]`） |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "body_text": "纯文本正文（推荐用此字段，body 字段为原始 HTML）",
        "subject": "邮件主题",
        "from": {"name": "发件人", "email_address": "xxx@wps.cn"},
        "to_recipients": [{"name": "收件人", "email_address": "..."}],
        "ctime": "发送时间",
        "attachments": []
    },
    "message": "获取邮件成功"
}
```

**调用示例**

```python
result = wps365.get_mail_message(message_id="msg_xxx")
print(result)
```


### wps365.search_mail_messages(keyword, type_, start_time=None, end_time=None, page_size=None, page_token=None)

搜索邮件

| 参数 | 说明 |
|------|------|
| keyword | 搜索关键字 |
| type_ | 搜索范围：`sender`（发件人）/ `subject`（主题）/ `body`（正文） |
| start_time | 起始时间 |
| end_time | 结束时间 |
| page_size | 返回数量，默认 10，最大 50 |
| page_token | 翻页 token |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "items": [{"id": "邮件ID", "folder_id": "...", "subject": "主题", "from": {...}, "is_read": True, "ctime": "...", "has_attachments": False, "body_preview": "..."}],
        "count": 5,
        "next_page_token": ""
    },
    "message": "搜索到 5 封邮件"
}
```

**调用示例**

```python
result = wps365.search_mail_messages(keyword="绩效考核", type_="subject")
print(result)
```

### wps365.send_mail(subject, to_recipients, body, cc_recipients=None, bcc_recipients=None, attachment_urls=None)

发送邮件

> **不可重复调用**：若返回"草稿已创建但发送失败"，请重试发送，切勿重新调用 `send_mail()`。

| 参数 | 说明 |
|------|------|
| subject | 邮件主题 |
| to_recipients | 收件人列表，每项 `{"name": "...", "email_address": "..."}` |
| body | 邮件正文（HTML 或纯文本） |
| cc_recipients | 抄送列表，格式同 `to_recipients` |
| bcc_recipients | 密送列表，格式同 `to_recipients` |
| attachment_files | 本地文件路径列表，函数自动上传后作为附件（推荐） |
| attachment_urls | 已有附件 URL 列表（须为可直接下载的 HTTP URL） |

**返回值**

```python
{
    "success": True/False,
    "data": {"message_id": "已发送邮件ID"},
    "message": "邮件发送成功",
    "error": "错误描述（失败时含此字段）"
}
```

**调用示例**

```python
result = wps365.send_mail(
    subject="邮件主题",
    to_recipients=[{"name": "张三", "email_address": "zhangsan@wps.cn"}],
    body="正文内容",
    attachment_files=["/tmp/report.pdf"]
)
print(result)
```

## AI 知识库


### wps365.recall_rank(query, topk=1, start_time=None, end_time=None)

在全部可访问知识库中按问题召回相关文档片段。

| 参数 | 说明 |
|------|------|
| query | 查询关键字 |
| topk | 召回片段数量上限，默认 5，最大 100 |
| start_time | 文档修改起始时间 |
| end_time | 文档修改结束时间 |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "chunks": [
            {
                "file_name": "文件名",
                "ref_score": 0.9123,
                "content": "召回片段正文（最长 500 字符）",
                "link_url": "https://365.kdocs.cn/l/xxx",
                "file_id": "末段文件ID"
            }
        ],
        "count": 3
    },
    "message": "召回到 3 条片段（共扫描 N 个知识库）"
}
```

**调用示例**

```python
result = wps365.recall_rank(
    "产品发布流程",
    topk=5,
    start_time="2025-01-01T00:00:00+08:00",
    end_time="2026-04-01T00:00:00+08:00",
)
print(result)
```


## 会议纪要

### wps365.get_meeting_summary(event_id)

获取指定日程对应会议的 AI 总结详情 

| 参数 | 说明 |
|------|------|
| event_id | 日程 ID |

**返回值**

```python
{
    "success": True/False,
    "data": {
        "title": "会议标题",
        "content": "发言人A: 内容...",    # 最多 1000 字符
        "saved_path": "/path/to/file.md"  # 仅内容超 1000 字符时存在
    },
    "message": "获取会议「xxx」AI 总结成功"
}
```

**调用示例**

```python
result = wps365.get_meeting_summary(event_id="127181701")
print(result)
```

## 错误处理

| 错误 | 原因 | 解决方案 |
|------|------|----------|
| 未知错误 | API 返回错误 | 检查参数和权限 |
