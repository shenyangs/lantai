# -*- coding: utf-8 -*-
"""Reviewed, provider-neutral migration of durable memories from another AI.

The pasted source text is parsed locally and is never stored.  Only a bounded
preview batch and the user's confirmed selections are persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .canonical_ledger import CanonicalMemoryLedger
from .policy import MemoryPolicyEngine, MemoryPolicyResult
from .structured_user_memory import (
    MemoryCandidate,
    StructuredUserMemoryStore,
    UserMemoryEntry,
    infer_memory_type,
    is_safe_memory_text,
    looks_like_assistant_meta_memory,
)


MAX_IMPORT_CHARS = 50_000
MAX_IMPORT_CANDIDATES = 60
IMPORT_BATCH_TTL_DAYS = 7

MEMORY_IMPORT_PROMPT_ZH = """请基于你在历史对话中对用户形成的长期了解，整理一份可迁移到其他 AI 助手的“个人使用画像”，用于让不同 AI 工具在称呼、背景理解和协作方式上保持一致。

筛选时严格遵守以下标准：
1. 只保留用户明确说过，或在多次对话中反复、稳定表现的信息。
2. 只保留预计在未来多次对话中仍然有用的信息，例如长期背景、持续项目、稳定偏好、常设规则和反复出现的协作习惯。
3. 每一项都必须能够脱离原对话独立理解。删除“这次”“当前”“刚才”等依赖临时上下文的表达。
4. 新旧信息冲突时，优先采用用户最近一次明确确认的版本；无法可靠判断时写「无」，不要自行推断。
5. 同一事实只保留一次，放入最合适的类别。不要换一种说法重复输出，也不要为了填满栏目而补充。

必须排除以下内容：
1. 一次性任务、短期待办、临时计划、已经结束的事项、本轮对话材料及某次回答的细节。
2. AI 助手自身的人设、职责、能力说明、系统提示词或通用回答原则。
3. 未经用户明确表达的性格判断、情绪推断、价值观推测或不确定结论。
4. 密码、验证码、API Key、Token、证件号码、支付信息、精确住址、账号密钥，以及医疗、政治、宗教、性取向等敏感信息。
5. 第三方的姓名、联系方式、隐私或可识别细节。家庭和人际关系只保留用户明确提及、对长期协作确有价值的关系层级。

严格按下面的字段和顺序输出。每个字段独占一行；同一字段有多个独立事实时，用中文分号「；」分隔。缺失的信息统一写「无」。

【基本信息】
称呼/别名：
语言偏好：
地区/时区：

【工作背景】
职位与职责：
所在公司/团队：
核心专业技能：

【个人背景】
家庭/人际关系：
兴趣爱好：
个人项目：

【协作偏好】
回复风格：
常设规则或指令：
不喜欢或需要纠正之处：

【长期记忆】
稳定行为模式或重要事项：

全程以「用户」指代，不使用“我”“你”“他/她”等第一或第二人称代词。描述应具体、克制、可验证，避免评价性措辞。只输出纯文本，不添加前言、总结、解释、代码块、Markdown 标题或其他字段。"""

_TYPE_ALIASES: dict[str, str] = {
    "identity": "identity",
    "profile": "identity",
    "basic": "identity",
    "基本信息": "identity",
    "个人信息": "identity",
    "用户档案": "identity",
    "身份": "identity",
    "身份记忆": "identity",
    "称呼": "identity",
    "别名": "identity",
    "称呼/别名": "identity",
    "地区": "identity",
    "时区": "identity",
    "地区/时区": "identity",
    "个人背景": "identity",
    "家庭": "identity",
    "人际关系": "identity",
    "家庭/人际关系": "identity",
    "兴趣爱好": "identity",
    "个人项目": "identity",
    "工作背景": "identity",
    "workflow": "workflow",
    "work": "workflow",
    "tools": "workflow",
    "工作流": "workflow",
    "工作方式": "workflow",
    "工具": "workflow",
    "技能": "workflow",
    "职位与职责": "identity",
    "所在公司/团队": "identity",
    "核心专业技能": "identity",
    "稳定行为模式": "workflow",
    "稳定行为模式或重要事项": "workflow",
    "style": "style",
    "preference": "style",
    "preferences": "style",
    "沟通风格": "style",
    "表达偏好": "style",
    "语言偏好": "style",
    "偏好": "style",
    "协作偏好": "style",
    "回复风格": "style",
    "不喜欢或需要纠正之处": "style",
    "instruction": "instruction",
    "instructions": "instruction",
    "rule": "instruction",
    "rules": "instruction",
    "协作规则": "instruction",
    "长期规则": "instruction",
    "用户指令": "instruction",
    "禁忌": "instruction",
    "常设规则或指令": "instruction",
    "长期记忆": "identity",
}
_TYPE_LABELS = {
    "identity": "身份记忆",
    "workflow": "工作方式",
    "style": "表达偏好",
    "instruction": "用户指令",
}
_SKIP_VALUE_RE = re.compile(r"^(?:无|暂无|没有|未知|none|n/?a|null|-+)$", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•·]|(?:\d+|[一二三四五六七八九十]+)[.)、])\s*")
_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:【|\[)?(?P<title>[^】\]\n：:]{1,30})(?:】|\])?\s*(?:[:：])?\s*$"
)
_ONE_OFF_RE = re.compile(
    r"(?:这次|本次|本轮|当前对话|当前任务|今天|明天|稍后|下一步|待办|todo|"
    r"请分析|请写|请生成|请总结|请说明|截图|本次需求)",
    re.I,
)
_DURABLE_RE = re.compile(
    r"(?:长期|以后|今后|每次|始终|一直|通常|常用|默认|习惯|偏好|喜欢|"
    r"不喜欢|希望|倾向|工作方式|工作背景|职业|职位|负责|擅长|"
    r"规则|不要|必须)",
    re.I,
)
_QUESTION_RE = re.compile(r"[?？]\s*$|^(?:请|帮我|如何|为什么|哪些|什么)")
_THIRD_PARTY_RE = re.compile(r"(?:父亲|母亲|父母|孩子|妻子|丈夫|同事|客户|朋友)")
_SECRET_RE = re.compile(
    r"(?:api[_ -]?key|password|密码|token|secret|身份证|护照|银行卡|"
    r"出生(?:日期|时间|地点|地|年月|时辰)|生辰|八字|住址|病史|诊断|"
    r"性取向|宗教|政治立场)",
    re.I,
)


class MemoryImportError(ValueError):
    """User-correctable import error."""


class MemoryImportNotFoundError(MemoryImportError):
    """The requested preview batch no longer exists."""


class MemoryImportConflictError(MemoryImportError):
    """The batch cannot be safely committed or undone in its current state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", value.casefold(), flags=re.UNICODE)


def _safe_provider(value: str) -> str:
    clean = re.sub(r"\s+", " ", value or "").strip()[:40]
    return clean or "其他 AI"


def _batch_root(working_dir: str | Path) -> Path:
    return Path(working_dir) / "memory-imports"


def _batch_path(working_dir: str | Path, batch_id: str) -> Path:
    if not re.fullmatch(r"import_[a-f0-9]{32}", batch_id or ""):
        raise MemoryImportNotFoundError("迁移批次不存在或已经过期")
    return _batch_root(working_dir) / f"{batch_id}.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _load_batch(working_dir: str | Path, batch_id: str) -> dict[str, Any]:
    path = _batch_path(working_dir, batch_id)
    if not path.exists():
        raise MemoryImportNotFoundError("迁移批次不存在或已经过期")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MemoryImportNotFoundError("迁移批次无法读取") from exc
    if not isinstance(payload, dict):
        raise MemoryImportNotFoundError("迁移批次格式无效")
    return payload


def _cleanup_expired_batches(working_dir: str | Path) -> None:
    root = _batch_root(working_dir)
    if not root.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=IMPORT_BATCH_TTL_DAYS)
    for path in root.glob("import_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(str(payload.get("created_at") or ""))
        except (OSError, ValueError, TypeError):
            continue
        if payload.get("status") == "preview" and created < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def _memory_type(value: str, content: str = "") -> str:
    clean = re.sub(r"[\s_\-/]+", "", value or "").casefold()
    for label, memory_type in _TYPE_ALIASES.items():
        if re.sub(r"[\s_\-/]+", "", label).casefold() == clean:
            return memory_type
    return infer_memory_type(content)


def _json_payload(text: str) -> Any | None:
    snippets = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    snippets.append(text)
    decoder = json.JSONDecoder()
    for snippet in snippets:
        for index, char in enumerate(snippet):
            if char not in "[{":
                continue
            try:
                payload, _ = decoder.raw_decode(snippet[index:])
            except ValueError:
                continue
            if isinstance(payload, (dict, list)):
                return payload
    return None


def _from_json(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, dict):
        values = (
            payload.get("memories")
            or payload.get("memory")
            or payload.get("items")
            or payload.get("data")
            or []
        )
    else:
        values = payload
    if not isinstance(values, list):
        return []

    parsed: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, str):
            content = item
            item_type = ""
            evidence = ""
        elif isinstance(item, dict):
            content = str(
                item.get("content")
                or item.get("text")
                or item.get("value")
                or item.get("description")
                or ""
            )
            item_type = str(item.get("type") or item.get("category") or "")
            evidence = str(item.get("evidence") or item.get("reason") or "")
        else:
            continue
        parsed.append(
            {
                "content": content,
                "type": _memory_type(item_type, content),
                "evidence": evidence[:240],
                **({key: str(item[key]) for key in ("subject", "scope", "scope_id") if key in item} if isinstance(item, dict) else {}),
            }
        )
    return parsed


def _from_markdown(text: str) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    current_type = ""
    current_subject = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            heading_type = _memory_type(heading.group("title"))
            if heading.group("title").strip() in _TYPE_ALIASES:
                current_type = heading_type
                current_subject = "project" if heading.group("title").strip() == "个人项目" else "user"
                continue

        content = _BULLET_RE.sub("", line).strip()
        if not content or _SKIP_VALUE_RE.match(content):
            continue
        subject = current_subject
        label_match = re.match(r"^(?P<label>[^：:]{1,24})[：:]\s*(?P<value>.+)$", content)
        if label_match:
            label = label_match.group("label").strip()
            value = label_match.group("value").strip()
            if _SKIP_VALUE_RE.match(value):
                continue
            item_type = _memory_type(label, content)
            # These are declared import fields, not semantic keyword guesses.
            if label not in _TYPE_ALIASES:
                subject = "unknown"
            elif label == "个人项目":
                subject = "project"
            elif item_type == "identity":
                content = f"用户的{label}：{value}"
            elif item_type == "workflow":
                content = f"用户长期的{label}：{value}"
            elif item_type == "style":
                content = f"用户的长期偏好：{value}"
            else:
                content = f"用户的长期协作规则：{value}"
        else:
            item_type = current_type or infer_memory_type(content)
            subject = current_subject or "unknown"

        parsed.append(
            {
                "content": content,
                "type": item_type,
                "evidence": "",
                "subject": subject,
                **({"scope": "project"} if subject == "project" else {}),
            }
        )
    return parsed


def _parsed_items(text: str) -> list[dict[str, str]]:
    payload = _json_payload(text)
    items = _from_json(payload) if payload is not None else []
    if not items:
        items = _from_markdown(text)

    result: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for item in items:
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if not content or _SKIP_VALUE_RE.match(content):
            continue
        normalized = _normalize(content)
        identity = (normalized, *(str(item.get(key) or "") for key in ("type", "subject", "scope", "scope_id")))
        if not normalized or identity in seen:
            continue
        seen.add(identity)
        result.append(item)
        if len(result) >= MAX_IMPORT_CANDIDATES:
            break
    return result


def _fact_identity(content: str, memory_type: str, subject: str, scope: str, scope_id: str) -> tuple[str, ...]:
    return (_normalize(content), memory_type, subject, scope, scope_id)


def _classify_candidate(
    candidate: MemoryCandidate,
    *,
    entries: list[UserMemoryEntry],
    existing_fact_contents: set[tuple[str, ...]],
    policy: MemoryPolicyEngine,
    agent_id: str = "",
) -> dict[str, Any]:
    content = candidate.content
    policy_result = policy.evaluate(
        content,
        memory_type=candidate.type,
        explicit_user_request=False,
    )
    base = {
        "id": f"candidate_{uuid.uuid4().hex}",
        "type": candidate.type,
        "type_label": _TYPE_LABELS[candidate.type],
        "content": content,
        "confidence": candidate.confidence,
        "evidence": candidate.evidence,
        "scope": candidate.scope if candidate.scope != "global" or candidate.scope_id else policy_result.scope,
        "scope_id": candidate.scope_id,
        "subject": candidate.subject,
        "sensitivity": policy_result.sensitivity,
        "selected_by_default": False,
        "selectable": False,
    }

    if _SECRET_RE.search(content) or policy_result.sensitivity == "sensitive":
        return {
            **base,
            "content": "包含敏感信息的候选（内容已隐藏）",
            "evidence": "",
            "bucket": "rejected",
            "reason": "涉及敏感个人信息或凭据，不通过迁移写入",
        }
    if _THIRD_PARTY_RE.search(content) or policy_result.subject == "third_party":
        return {
            **base,
            "content": "涉及第三方信息的候选（内容已隐藏）",
            "evidence": "",
            "bucket": "rejected",
            "reason": "涉及第三方信息，不适合作为你的长期记忆",
        }
    if _QUESTION_RE.search(content) or _ONE_OFF_RE.search(content):
        return {
            **base,
            "bucket": "rejected",
            "reason": "更像一次性任务或当前对话内容",
        }
    if policy_result.subject == "assistant" or looks_like_assistant_meta_memory(content):
        return {
            **base,
            "bucket": "rejected",
            "reason": "这是助手人设或通用回答原则，不是用户记忆",
        }

    if candidate.subject == "project":
        base["scope"] = "project"
    if base["scope"] == "project" and not base["scope_id"]:
        base["scope_id"] = f"workspace:{agent_id}"
    if candidate.subject == "unknown":
        return {**base, "bucket": "review", "selectable": True,
                "reason": "内容归属尚不明确，保留原文供核对，不作为你的个人画像"}

    resolved = replace(candidate, scope=str(base["scope"]), scope_id=str(base["scope_id"]))
    duplicate = StructuredUserMemoryStore._find_duplicate(entries, resolved)
    if duplicate is not None or _fact_identity(content, candidate.type, candidate.subject, resolved.scope, resolved.scope_id) in existing_fact_contents:
        return {
            **base,
            "bucket": "duplicate",
            "reason": "已有相同或高度相似的记忆",
            "related_entry_id": duplicate.id if duplicate is not None else "",
        }

    conflict = StructuredUserMemoryStore._find_possible_conflict(entries, resolved)
    if conflict is not None:
        return {
            **base,
            "bucket": "review",
            "reason": "可能与现有记忆冲突，需要你逐条判断",
            "selectable": True,
            "related_entry_id": conflict.id,
            "related_content": conflict.content,
        }

    durable = bool(_DURABLE_RE.search(content))
    recommended = (
        policy_result.decision in {"suggest", "confirm"}
        and policy_result.utility_score >= 0.7
        and base["scope"] == "global"
    )
    if candidate.type in {"style", "instruction"} and durable and base["scope"] == "global":
        recommended = True
    if candidate.type == "identity" and durable and base["scope"] == "global":
        recommended = True

    if recommended:
        return {
            **base,
            "bucket": "recommended",
            "reason": "信息明确、稳定，且未来对话有复用价值",
            "selectable": True,
            "selected_by_default": True,
        }
    if durable or candidate.type in {"identity", "workflow", "style", "instruction"}:
        return {
            **base,
            "bucket": "review",
            "reason": (
                "可能只适用于某个项目，请确认是否值得长期保留"
                if base["scope"] == "project"
                else "长期价值或稳定性不足，需要你确认"
            ),
            "selectable": True,
        }
    return {
        **base,
        "bucket": "rejected",
        "reason": policy_result.reason or "没有足够证据表明未来会复用",
    }


def get_memory_import_prompt() -> dict[str, Any]:
    return {
        "prompt": MEMORY_IMPORT_PROMPT_ZH,
        "max_characters": MAX_IMPORT_CHARS,
        "max_candidates": MAX_IMPORT_CANDIDATES,
        "privacy_note": "粘贴内容只在本机解析，原文不会保存。",
    }


def preview_memory_import(
    working_dir: str | Path,
    *,
    text: str,
    provider: str = "其他 AI",
    agent_id: str = "",
) -> dict[str, Any]:
    clean = (text or "").strip()
    if len(clean) < 4:
        raise MemoryImportError("请先粘贴其他 AI 整理出的记忆")
    if len(clean) > MAX_IMPORT_CHARS:
        raise MemoryImportError(f"粘贴内容不能超过 {MAX_IMPORT_CHARS} 个字符")

    _cleanup_expired_batches(working_dir)
    parsed = _parsed_items(clean)
    if not parsed:
        raise MemoryImportError(
            "没有识别到可审核的记忆，请检查输出格式后重试"
        )

    store = StructuredUserMemoryStore(working_dir)
    entries = store.load_entries()
    ledger = CanonicalMemoryLedger(working_dir)
    existing_fact_contents = {
        _fact_identity(fact.content, fact.memory_type, fact.subject, fact.scope, fact.scope_id)
        for fact in ledger.list_facts(
            statuses={"observed", "pending", "confirmed", "conflict"}
        )
    }
    policy = MemoryPolicyEngine()
    candidates: list[dict[str, Any]] = []
    for item in parsed:
        raw_content = str(item.get("content") or "")
        if not is_safe_memory_text(raw_content):
            candidates.append(
                {
                    "id": f"candidate_{uuid.uuid4().hex}",
                    "type": _memory_type(str(item.get("type") or ""), raw_content),
                    "type_label": _TYPE_LABELS[
                        _memory_type(str(item.get("type") or ""), raw_content)
                    ],
                    "content": "无法安全解析的候选（内容已隐藏）",
                    "bucket": "rejected",
                    "reason": "内容为空、过长或包含不安全指令",
                    "selectable": False,
                    "selected_by_default": False,
                    "confidence": 0.0,
                    "evidence": "",
                    "scope": "turn",
                    "sensitivity": "normal",
                }
            )
            continue
        candidate = MemoryCandidate.from_dict(
            {
                "type": item.get("type"),
                "content": raw_content,
                "confidence": 0.84,
                "reason": "从其他 AI 迁移候选",
                "evidence": item.get("evidence"),
                "subject": item.get("subject") or policy.evaluate(raw_content, memory_type=str(item.get("type") or "identity")).subject,
                "scope": item.get("scope"),
                "scope_id": item.get("scope_id"),
            }
        )
        if candidate is None:
            candidates.append(
                {
                    "id": f"candidate_{uuid.uuid4().hex}",
                    "type": _memory_type(str(item.get("type") or ""), raw_content),
                    "type_label": _TYPE_LABELS[
                        _memory_type(str(item.get("type") or ""), raw_content)
                    ],
                    "content": raw_content[:600],
                    "bucket": "rejected",
                    "reason": "这是助手人设或不适合形成长期记忆",
                    "selectable": False,
                    "selected_by_default": False,
                    "confidence": 0.0,
                    "evidence": "",
                    "scope": "turn",
                    "sensitivity": "normal",
                }
            )
            continue
        candidates.append(
            _classify_candidate(
                candidate,
                entries=entries,
                existing_fact_contents=existing_fact_contents,
                policy=policy,
                agent_id=agent_id,
            )
        )

    stats = {
        bucket: sum(1 for item in candidates if item["bucket"] == bucket)
        for bucket in ("recommended", "review", "duplicate", "rejected")
    }
    batch_id = f"import_{uuid.uuid4().hex}"
    batch = {
        "version": 1,
        "id": batch_id,
        "agent_id": agent_id,
        "provider": _safe_provider(provider),
        "status": "preview",
        "created_at": _now(),
        "updated_at": _now(),
        "raw_sha256": hashlib.sha256(clean.encode("utf-8")).hexdigest(),
        "raw_character_count": len(clean),
        "raw_stored": False,
        "candidates": candidates,
        "stats": stats,
    }
    _write_json_atomic(_batch_path(working_dir, batch_id), batch)
    return {
        "batch_id": batch_id,
        "provider": batch["provider"],
        "created_at": batch["created_at"],
        "candidates": candidates,
        "stats": stats,
        "raw_stored": False,
    }


def commit_memory_import(
    working_dir: str | Path,
    *,
    batch_id: str,
    selected_ids: list[str],
    agent_id: str = "",
) -> dict[str, Any]:
    batch = _load_batch(working_dir, batch_id)
    if batch.get("status") != "preview":
        raise MemoryImportConflictError("这个迁移批次已经处理过")
    if agent_id and batch.get("agent_id") and batch.get("agent_id") != agent_id:
        raise MemoryImportNotFoundError("迁移批次不属于当前智能体")

    selected = list(dict.fromkeys(str(item) for item in selected_ids if item))
    if not selected:
        raise MemoryImportError("请至少选择一条值得迁移的记忆")
    if len(selected) > MAX_IMPORT_CANDIDATES:
        raise MemoryImportError("一次最多迁移 60 条记忆")

    candidates_by_id = {
        str(item.get("id") or ""): item
        for item in batch.get("candidates", [])
        if isinstance(item, dict)
    }
    invalid = [
        item_id
        for item_id in selected
        if item_id not in candidates_by_id
        or not candidates_by_id[item_id].get("selectable")
    ]
    if invalid:
        raise MemoryImportError("选择中包含不可迁移或不存在的记忆")

    store = StructuredUserMemoryStore(working_dir)
    ledger = CanonicalMemoryLedger(working_dir)
    before_entries = {entry.id: entry.to_dict() for entry in store.load_entries()}
    before_fact_ids = {fact.id for fact in ledger.list_facts()}
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    source_title = f"来自 {batch.get('provider') or '其他 AI'} 的记忆"

    for item_id in selected:
        item = candidates_by_id[item_id]
        candidate = MemoryCandidate.from_dict(item)
        if candidate is None:
            skipped.append({"id": item_id, "reason": "候选内容已无法通过安全检查"})
            continue

        current_entries = store.load_entries()
        duplicate = StructuredUserMemoryStore._find_duplicate(
            current_entries,
            candidate,
        )
        active_fact_norms = {
            _fact_identity(fact.content, fact.memory_type, fact.subject, fact.scope, fact.scope_id)
            for fact in ledger.list_facts(
                statuses={"observed", "pending", "confirmed", "conflict"}
            )
        }
        if duplicate is not None or _fact_identity(candidate.content, candidate.type, candidate.subject, candidate.scope, candidate.scope_id) in active_fact_norms:
            skipped.append({"id": item_id, "reason": "预览后已存在相同记忆"})
            continue

        decision = store.add_entry(
            content=candidate.content,
            memory_type=candidate.type,
            source="agent",
            confidence=max(candidate.confidence, 0.9),
            reason="用户审核后从其他 AI 迁移",
            evidence=f"import-batch:{batch_id}",
            subject=candidate.subject,
            scope=candidate.scope,
            scope_id=(
                candidate.scope_id
                or (f"workspace:{agent_id}" if candidate.scope == "project" else "")
            ),
        )
        entry_payload = decision.get("entry") if isinstance(decision, dict) else None
        if not isinstance(entry_payload, dict):
            skipped.append({"id": item_id, "reason": "没有生成可写入的长期记忆"})
            continue

        entry_id = str(entry_payload.get("id") or "")
        event = ledger.record_event(
            candidate.content,
            source_message_id=f"memory-import:{batch_id}:{item_id}",
            session_id=f"memory-import:{batch_id}",
            source_role="reviewed_import",
        )
        evaluated = ledger.policy.evaluate(
            candidate.content,
            memory_type=candidate.type,
            explicit_user_request=True,
        )
        reviewed_policy = MemoryPolicyResult(
            decision="confirm",
            scope=candidate.scope,  # type: ignore[arg-type]
            subject=candidate.subject,
            utility_score=max(0.98, evaluated.utility_score),
            reason="用户已在迁移预览中逐条确认",
            sensitivity=evaluated.sensitivity,
        )
        resolved_scope_id = candidate.scope_id or (
            f"workspace:{agent_id}" if candidate.scope == "project" else ""
        )
        fact = (
            ledger.capture_fact(
                candidate.content,
                memory_type=candidate.type,
                event=event,
                explicit_user_request=True,
                confidence=max(candidate.confidence, 0.95),
                policy_result=reviewed_policy,
                scope_id=resolved_scope_id,
            )
            if event is not None
            else None
        )
        if fact is None:
            store.delete_entry(entry_id)
            skipped.append({"id": item_id, "reason": "未通过长期记忆安全策略"})
            continue
        ledger.link_legacy_entry(fact.id, entry_id)
        annotated = store.annotate_import_source(
            entry_id,
            source_title=source_title,
            batch_id=batch_id,
            canonical_fact_id=fact.id,
        )
        if annotated is None:
            raise MemoryImportConflictError("迁移写入未完成，请重试")
        annotated = store.mark_confirmed(
            entry_id,
            canonical_fact_id=fact.id,
            scope=fact.scope,
            scope_id=fact.scope_id,
        )
        if annotated is None:
            raise MemoryImportConflictError("迁移作用域写入未完成，请重试")
        imported.append(
            {
                "candidate_id": item_id,
                "entry_id": entry_id,
                "fact_id": fact.id,
                "content": annotated.content,
                "type": annotated.type,
            }
        )

    if not imported:
        raise MemoryImportConflictError(
            "所选内容在预览后已存在或不再适合迁移，没有写入新记忆"
        )

    after_entries = {entry.id: entry.to_dict() for entry in store.load_entries()}
    created_entry_ids = sorted(set(after_entries) - set(before_entries))
    modified_entry_ids = sorted(
        entry_id
        for entry_id in set(after_entries) & set(before_entries)
        if after_entries[entry_id] != before_entries[entry_id]
    )
    created_fact_ids = sorted(
        {fact.id for fact in ledger.list_facts()} - before_fact_ids
    )
    affected_ids = [*created_entry_ids, *modified_entry_ids]
    stored_imported = [
        {
            "candidate_id": item["candidate_id"],
            "entry_id": item["entry_id"],
            "fact_id": item["fact_id"],
            "type": item["type"],
        }
        for item in imported
    ]

    batch.update(
        {
            "status": "committed",
            "updated_at": _now(),
            "committed_at": _now(),
            "selected_ids": selected,
            "candidate_outcomes": [
                {
                    "id": item_id,
                    "bucket": candidates_by_id[item_id].get("bucket"),
                    "selected": item_id in selected,
                }
                for item_id in candidates_by_id
            ],
            "candidates": [],
            "imported": stored_imported,
            "skipped": skipped,
            "rollback": {
                "created_entry_ids": created_entry_ids,
                "created_fact_ids": created_fact_ids,
                "restore_entries": [
                    before_entries[entry_id] for entry_id in modified_entry_ids
                ],
                "expected_entries": [
                    after_entries[entry_id] for entry_id in affected_ids
                ],
            },
        }
    )
    _write_json_atomic(_batch_path(working_dir, batch_id), batch)
    return {
        "batch_id": batch_id,
        "provider": batch["provider"],
        "status": "committed",
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "imported": imported,
        "skipped": skipped,
        "committed_at": batch["committed_at"],
        "can_undo": True,
    }


def undo_memory_import(
    working_dir: str | Path,
    *,
    batch_id: str,
    agent_id: str = "",
) -> dict[str, Any]:
    batch = _load_batch(working_dir, batch_id)
    if batch.get("status") != "committed":
        raise MemoryImportConflictError("这个迁移批次不能撤销")
    if agent_id and batch.get("agent_id") and batch.get("agent_id") != agent_id:
        raise MemoryImportNotFoundError("迁移批次不属于当前智能体")

    rollback = batch.get("rollback")
    if not isinstance(rollback, dict):
        raise MemoryImportConflictError("这个迁移批次缺少撤销信息")
    store = StructuredUserMemoryStore(working_dir)
    try:
        result = store.rollback_import_changes(
            created_entry_ids=[
                str(item) for item in rollback.get("created_entry_ids", []) if item
            ],
            restore_entries=[
                item
                for item in rollback.get("restore_entries", [])
                if isinstance(item, dict)
            ],
            expected_entries=[
                item
                for item in rollback.get("expected_entries", [])
                if isinstance(item, dict)
            ],
        )
    except ValueError as exc:
        raise MemoryImportConflictError(str(exc)) from exc

    ledger = CanonicalMemoryLedger(working_dir)
    forgotten = 0
    for fact_id in rollback.get("created_fact_ids", []):
        if ledger.forget(str(fact_id)):
            forgotten += 1

    batch.update(
        {
            "status": "undone",
            "updated_at": _now(),
            "undone_at": _now(),
            "rollback": {},
            "undo_result": {**result, "forgotten_facts": forgotten},
        }
    )
    _write_json_atomic(_batch_path(working_dir, batch_id), batch)
    return {
        "batch_id": batch_id,
        "status": "undone",
        "removed_count": result["removed"],
        "restored_count": result["restored"],
        "forgotten_fact_count": forgotten,
        "undone_at": batch["undone_at"],
    }


def get_recent_memory_import(
    working_dir: str | Path,
    *,
    agent_id: str = "",
) -> dict[str, Any]:
    _cleanup_expired_batches(working_dir)
    root = _batch_root(working_dir)
    if not root.exists():
        return {"batch": None}
    batches: list[dict[str, Any]] = []
    for path in root.glob("import_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if agent_id and payload.get("agent_id") and payload.get("agent_id") != agent_id:
            continue
        if payload.get("status") not in {"committed", "undone"}:
            continue
        batches.append(payload)
    if not batches:
        return {"batch": None}
    latest = max(batches, key=lambda item: str(item.get("updated_at") or ""))
    imported = latest.get("imported") if isinstance(latest.get("imported"), list) else []
    return {
        "batch": {
            "batch_id": latest.get("id"),
            "provider": latest.get("provider"),
            "status": latest.get("status"),
            "imported_count": len(imported),
            "committed_at": latest.get("committed_at", ""),
            "undone_at": latest.get("undone_at", ""),
            "can_undo": latest.get("status") == "committed",
        }
    }
