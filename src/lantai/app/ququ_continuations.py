# -*- coding: utf-8 -*-
"""Durable child records for user-requested Ququ continuations.

These records are a presentation sidecar owned by one persisted assistant
message.  They are intentionally not ordinary chat turns and never enter
agent memory.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from typing import Any, Literal
from uuid import uuid4

from .runner.conversation_identity import resolve_chat_session_state_id
from .runner.manager import ChatManager
from .runner.models import ChatSpec
from ..runtime.converters import legacy_memory_state_to_chat_messages


QUQU_CONTINUATIONS_META_KEY = "_ququ_continuations_v1"
_QUQU_LINE_RE = re.compile(
    r"^~>\s*(?:\[(lead|echo)\]\s*)?(.+)$", re.IGNORECASE
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_WIRE_SUFFIX_RE = re.compile(r":(?:message|reasoning)$")
_POST_REFLECTION_SOURCE_RE = re.compile(r"^post-reflection-ququ-([0-9a-f]{64})$")
QUQU_CLAIM_LEASE_SECONDS = 45.0


class QuquContinuationError(RuntimeError):
    """Expected request or state conflict with a user-safe status code."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = (
            {"message": detail, "code": code, "retryable": retryable}
            if code
            else detail
        )


@dataclass(frozen=True)
class QuquSource:
    """Server-verified owner and source identity for one visible Ququ."""

    agent_id: str
    workspace_scope_id: str
    chat_id: str
    session_id: str
    user_id: str
    channel: str
    source_message_id: str
    source_message_digest: str
    source_ququ_id: str
    ququ_ordinal: int
    placement: Literal["lead", "echo"]
    ququ_text: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(*parts: str) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_source_message_id(value: str) -> str:
    """Collapse display-only message/reasoning suffixes to the source id."""

    normalized = str(value or "").strip()
    while _WIRE_SUFFIX_RE.search(normalized):
        normalized = _WIRE_SUFFIX_RE.sub("", normalized)
    return normalized


def workspace_scope_id(agent_id: str, workspace_dir: str | Path) -> str:
    """Return a stable opaque workspace scope without exposing local paths."""

    resolved = str(Path(workspace_dir).expanduser().resolve())
    return f"workspace:{_stable_hash(agent_id, resolved)[:32]}"


def parse_ququ_lines(text: str) -> list[tuple[Literal["lead", "echo"], str]]:
    """Parse visible Ququ markers while ignoring fenced code examples."""

    result: list[tuple[Literal["lead", "echo"], str]] = []
    fence: str | None = None
    for line in str(text or "").splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        match = _QUQU_LINE_RE.match(line.strip())
        if not match:
            continue
        placement = str(match.group(1) or "echo").lower()
        ququ_text = str(match.group(2) or "").strip()
        if ququ_text:
            result.append(
                ("lead" if placement == "lead" else "echo", ququ_text)
            )
    return result


def _message_role(message: Any) -> str:
    role = getattr(message, "role", "")
    return str(getattr(role, "value", role) or "").lower()


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        block_type = getattr(block, "type", None)
        block_type = str(getattr(block_type, "value", block_type) or "")
        if block_type != "text":
            continue
        text = getattr(block, "text", "")
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def _message_identity_candidates(message: Any) -> set[str]:
    candidates = {normalize_source_message_id(getattr(message, "id", ""))}
    queue = [getattr(message, "metadata", None)]
    seen: set[int] = set()
    while queue:
        candidate = queue.pop(0)
        if not isinstance(candidate, dict) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        for key in (
            "source_message_id",
            "runtime_item_id",
            "original_id",
            "response_id",
        ):
            value = normalize_source_message_id(str(candidate.get(key) or ""))
            if value:
                candidates.add(value)
        queue.extend(
            value for value in candidate.values() if isinstance(value, dict)
        )
    candidates.discard("")
    return candidates


def _delivered_post_reflection_source_text(
    *,
    chat: ChatSpec,
    messages: list[Any],
    agent_id: str,
    agent_workspace_dir: str | Path,
    state_session_id: str,
    source_message_id: str,
    source_message_digest: str,
) -> str | None:
    """Resolve a visible late Ququ through its durable, owned sidecar job."""

    match = _POST_REFLECTION_SOURCE_RE.fullmatch(source_message_id)
    if match is None:
        return None
    agent_home = Path(agent_workspace_dir).expanduser().resolve()
    meta = chat.meta if isinstance(chat.meta, dict) else {}
    raw_state_root = meta.get("state_workspace_dir") or meta.get(
        "topic_workspace_dir"
    )
    state_root = (
        Path(raw_state_root).expanduser().resolve()
        if isinstance(raw_state_root, str) and raw_state_root.strip()
        else agent_home
    )
    if state_root != agent_home and agent_home not in state_root.parents:
        return None
    safe_agent = re.sub(r"[^a-zA-Z0-9_.-]+", "-", agent_id)
    job_path = (
        state_root
        / ".lantai"
        / "persona"
        / "post-answer-reflections"
        / safe_agent
        / f"{match.group(1)}.json"
    )
    try:
        if job_path.is_symlink():
            return None
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(job, dict) or job.get("job_id") != match.group(1):
        return None
    if (
        job.get("status") != "completed"
        or job.get("message_persisted") is not True
        or job.get("event_persisted") is not True
    ):
        return None
    request = job.get("request")
    reflection = job.get("reflection")
    if not isinstance(request, dict) or not isinstance(reflection, dict):
        return None
    if (
        request.get("agent_id") != agent_id
        or request.get("chat_id") != chat.id
        or request.get("session_id") != state_session_id
        or request.get("workspace_dir") != str(state_root)
        or request.get("need_ququ") is not True
    ):
        return None
    answer_id = normalize_source_message_id(
        str(request.get("answer_message_id") or "")
    )
    answer_text = str(request.get("final_answer") or "")
    if not answer_id or not answer_text:
        return None
    if not any(
        _message_role(message) == "assistant"
        and answer_id in _message_identity_candidates(message)
        and _message_text(message) == answer_text
        for message in messages
    ):
        return None
    ququ_line = reflection.get("ququ_line")
    if not isinstance(ququ_line, str):
        return None
    if _stable_hash(ququ_line) != source_message_digest:
        return None
    return ququ_line


async def verify_ququ_source(
    *,
    manager: ChatManager,
    session: Any,
    agent_id: str,
    workspace_dir: str | Path,
    chat_id: str,
    session_id: str,
    user_id: str,
    channel: str,
    source_message_id: str,
    source_message_digest: str,
    ququ_ordinal: int,
    placement: str,
    ququ_text: str,
) -> QuquSource:
    """Resolve a client locator against authoritative persisted history."""

    chat = await manager.get_chat(chat_id)
    if chat is None:
        raise QuquContinuationError(404, "原回答已不存在，不能继续这句蛐蛐")
    if (
        chat.session_id != session_id
        or chat.user_id != user_id
        or chat.channel != channel
    ):
        raise QuquContinuationError(403, "这句蛐蛐不属于当前可访问的会话")

    state_session_id = await resolve_chat_session_state_id(manager, chat)
    state = await session.get_session_state_dict(
        state_session_id,
        chat.user_id,
        chat.channel,
        allow_not_exist=False,
    )
    memory = (
        state.get("agent", {}).get("memory", {})
        if isinstance(state, dict)
        else {}
    )
    messages = await legacy_memory_state_to_chat_messages(memory)
    requested_message_id = normalize_source_message_id(source_message_id)
    expected_message_digest = str(source_message_digest or "").strip().lower()
    source_message = next(
        (
            message
            for message in messages
            if _message_role(message) == "assistant"
            and requested_message_id in _message_identity_candidates(message)
            and _stable_hash(_message_text(message))
            == expected_message_digest
        ),
        None,
    )
    if _POST_REFLECTION_SOURCE_RE.fullmatch(requested_message_id):
        source_text = _delivered_post_reflection_source_text(
            chat=chat,
            messages=messages,
            agent_id=agent_id,
            agent_workspace_dir=workspace_dir,
            state_session_id=state_session_id,
            source_message_id=requested_message_id,
            source_message_digest=expected_message_digest,
        )
    else:
        source_text = (
            _message_text(source_message) if source_message is not None else None
        )
    if source_text is None:
        raise QuquContinuationError(409, "原回答版本已变化，请在当前回答上重新操作")

    ququ_blocks = parse_ququ_lines(source_text)
    if ququ_ordinal < 0 or ququ_ordinal >= len(ququ_blocks):
        raise QuquContinuationError(409, "原蛐蛐已不存在，请在当前回答上重新操作")
    stored_placement, stored_text = ququ_blocks[ququ_ordinal]
    expected_placement = "lead" if placement == "lead" else "echo"
    if (
        stored_placement != expected_placement
        or stored_text != ququ_text.strip()
    ):
        raise QuquContinuationError(409, "原蛐蛐版本已变化，请在当前回答上重新操作")

    opaque_scope = workspace_scope_id(agent_id, workspace_dir)
    message_digest = _stable_hash(source_text)
    source_ququ_id = "ququ:" + _stable_hash(
        opaque_scope,
        chat.id,
        requested_message_id,
        message_digest,
        str(ququ_ordinal),
        stored_placement,
        stored_text,
    )[:32]
    return QuquSource(
        agent_id=agent_id,
        workspace_scope_id=opaque_scope,
        chat_id=chat.id,
        session_id=chat.session_id,
        user_id=chat.user_id,
        channel=chat.channel,
        source_message_id=requested_message_id,
        source_message_digest=message_digest,
        source_ququ_id=source_ququ_id,
        ququ_ordinal=ququ_ordinal,
        placement=stored_placement,
        ququ_text=stored_text,
    )


def _record_matches_owner(record: dict[str, Any], source: QuquSource) -> bool:
    return all(
        str(record.get(key) or "") == expected
        for key, expected in (
            ("agent_id", source.agent_id),
            ("workspace_scope_id", source.workspace_scope_id),
            ("chat_id", source.chat_id),
            ("session_id", source.session_id),
            ("user_id", source.user_id),
            ("channel", source.channel),
            ("source_message_id", source.source_message_id),
            ("source_message_digest", source.source_message_digest),
            ("source_ququ_id", source.source_ququ_id),
        )
    )


def public_ququ_continuation(record: dict[str, Any]) -> dict[str, Any]:
    """Remove claim/failure internals from a history or mutation response."""

    allowed = {
        "continuation_id",
        "request_id",
        "agent_id",
        "workspace_scope_id",
        "chat_id",
        "session_id",
        "user_id",
        "channel",
        "source_message_id",
        "source_message_digest",
        "source_ququ_id",
        "source_continuation_id",
        "ququ_ordinal",
        "placement",
        "text",
        "status",
        "created_at",
        "updated_at",
    }
    return {key: value for key, value in record.items() if key in allowed}


def completed_ququ_continuations(
    meta: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Project only durable completed records for bulk history restore."""

    records = (meta or {}).get(QUQU_CONTINUATIONS_META_KEY, [])
    if not isinstance(records, list):
        return []
    return [
        public_ququ_continuation(record)
        for record in records
        if isinstance(record, dict)
        and record.get("status") == "completed"
        and str(record.get("text") or "").strip()
    ]


def without_ququ_continuation_storage(
    meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep private claim state out of the general-purpose history metadata."""

    public_meta = dict(meta or {})
    public_meta.pop(QUQU_CONTINUATIONS_META_KEY, None)
    return public_meta


def copy_ququ_continuations_for_branch(
    meta: dict[str, Any] | None,
    *,
    visible_messages: list[Any],
    branch_chat_id: str,
    branch_session_id: str,
    user_id: str,
    channel: str,
) -> dict[str, Any]:
    """Copy completed sidecars whose answer exists in an explicit branch.

    The copy receives new branch ownership and identity while retaining an
    explicit provenance link to the source continuation. In-flight and failed
    claims never cross a branch boundary.
    """

    branch_meta = dict(meta or {})
    visible_sources = [
        (
            _message_identity_candidates(message),
            _message_text(message),
        )
        for message in visible_messages
        if _message_role(message) == "assistant"
    ]
    now = _utc_now()
    copied: list[dict[str, Any]] = []
    for source_record in completed_ququ_continuations(meta):
        source_message_id = normalize_source_message_id(
            str(source_record.get("source_message_id") or "")
        )
        source_message = next(
            (
                text
                for identities, text in visible_sources
                if source_message_id in identities
                and _stable_hash(text)
                == str(source_record.get("source_message_digest") or "")
            ),
            None,
        )
        if source_message is None:
            continue
        try:
            ququ_ordinal = int(source_record.get("ququ_ordinal") or 0)
        except (TypeError, ValueError):
            continue
        ququ_blocks = parse_ququ_lines(source_message)
        if ququ_ordinal < 0 or ququ_ordinal >= len(ququ_blocks):
            continue
        placement, ququ_text = ququ_blocks[ququ_ordinal]
        if placement != str(source_record.get("placement") or "echo"):
            continue
        message_digest = _stable_hash(source_message)
        continuation_id = f"ququ-continuation:{uuid4()}"
        source_continuation_id = str(
            source_record.get("source_continuation_id")
            or source_record.get("continuation_id")
            or ""
        )
        source_ququ_id = (
            "ququ:"
            + _stable_hash(
                str(source_record.get("workspace_scope_id") or ""),
                branch_chat_id,
                source_message_id,
                message_digest,
                str(ququ_ordinal),
                placement,
                ququ_text,
            )[:32]
        )
        copied.append(
            {
                **source_record,
                "continuation_id": continuation_id,
                "request_id": f"branch-copy:{continuation_id}",
                "chat_id": branch_chat_id,
                "session_id": branch_session_id,
                "user_id": user_id,
                "channel": channel,
                "source_message_id": source_message_id,
                "source_message_digest": message_digest,
                "source_ququ_id": source_ququ_id,
                "source_continuation_id": source_continuation_id,
                "status": "completed",
                "created_at": now,
                "updated_at": now,
            }
        )
    if copied:
        branch_meta[QUQU_CONTINUATIONS_META_KEY] = copied
    else:
        branch_meta.pop(QUQU_CONTINUATIONS_META_KEY, None)
    return branch_meta


async def claim_ququ_continuation(
    *,
    manager: ChatManager,
    source: QuquSource,
    request_id: str,
    retry_failed: bool,
) -> tuple[dict[str, Any], bool]:
    """Atomically claim generation; return ``(record, owns_generation)``."""

    now = _utc_now()
    claim_expires_at = time.time() + QUQU_CLAIM_LEASE_SECONDS
    claim_token = secrets.token_urlsafe(24)

    def mutate(meta: dict[str, Any], chat: ChatSpec):
        if (
            chat.session_id != source.session_id
            or chat.user_id != source.user_id
            or chat.channel != source.channel
        ):
            raise QuquContinuationError(403, "会话权限已经变化，不能保存这句蛐蛐")
        raw_records = meta.get(QUQU_CONTINUATIONS_META_KEY, [])
        records = [
            dict(item) for item in raw_records if isinstance(item, dict)
        ]
        for record in records:
            if record.get(
                "request_id"
            ) == request_id and not _record_matches_owner(
                record,
                source,
            ):
                raise QuquContinuationError(409, "request_id 已用于另一条蛐蛐")
        existing_index = next(
            (
                index
                for index, record in enumerate(records)
                if record.get("source_ququ_id") == source.source_ququ_id
            ),
            None,
        )
        if existing_index is not None:
            existing = records[existing_index]
            status = str(existing.get("status") or "")
            if status == "completed":
                return None, (existing, False)
            if status == "generating":
                try:
                    claim_is_live = (
                        float(existing.get("claim_expires_at") or 0)
                        > time.time()
                    )
                except (TypeError, ValueError):
                    claim_is_live = False
                if claim_is_live:
                    return None, (existing, False)
                if not retry_failed:
                    existing.update(
                        {
                            "status": "failed",
                            "text": "",
                            "failure": "generation_interrupted_before_save",
                            "updated_at": now,
                        }
                    )
                    existing.pop("claim_token", None)
                    existing.pop("claim_expires_at", None)
                    records[existing_index] = existing
                    meta[QUQU_CONTINUATIONS_META_KEY] = records
                    return meta, (existing, False)
            if status == "failed" and not retry_failed:
                raise QuquContinuationError(
                    409,
                    "上次续念没有成功保存；请明确重试后再重新生成",
                    code="ququ_generation_failed",
                    retryable=True,
                )
            existing.update(
                {
                    "request_id": request_id,
                    "status": "generating",
                    "text": "",
                    "claim_token": claim_token,
                    "claim_expires_at": claim_expires_at,
                    "updated_at": now,
                }
            )
            existing.pop("failure", None)
            records[existing_index] = existing
            meta[QUQU_CONTINUATIONS_META_KEY] = records
            return meta, (existing, True)

        record = {
            "continuation_id": f"ququ-continuation:{uuid4()}",
            "request_id": request_id,
            "agent_id": source.agent_id,
            "workspace_scope_id": source.workspace_scope_id,
            "chat_id": source.chat_id,
            "session_id": source.session_id,
            "user_id": source.user_id,
            "channel": source.channel,
            "source_message_id": source.source_message_id,
            "source_message_digest": source.source_message_digest,
            "source_ququ_id": source.source_ququ_id,
            "ququ_ordinal": source.ququ_ordinal,
            "placement": source.placement,
            "text": "",
            "status": "generating",
            "created_at": now,
            "updated_at": now,
            "claim_token": claim_token,
            "claim_expires_at": claim_expires_at,
        }
        records.append(record)
        meta[QUQU_CONTINUATIONS_META_KEY] = records
        return meta, (record, True)

    chat, result = await manager.mutate_chat_meta(source.chat_id, mutate)
    if chat is None or result is None:
        raise QuquContinuationError(404, "原回答已不存在，不能继续这句蛐蛐")
    return result


async def renew_ququ_continuation_claim(
    *,
    manager: ChatManager,
    source: QuquSource,
    claimed_record: dict[str, Any],
) -> bool:
    """Extend a live generation claim without changing chat activity order."""

    claim_token = str(claimed_record.get("claim_token") or "")
    continuation_id = str(claimed_record.get("continuation_id") or "")

    def mutate(meta: dict[str, Any], chat: ChatSpec):
        if (
            chat.session_id != source.session_id
            or chat.user_id != source.user_id
            or chat.channel != source.channel
        ):
            return None, False
        raw_records = meta.get(QUQU_CONTINUATIONS_META_KEY, [])
        records = [
            dict(item) for item in raw_records if isinstance(item, dict)
        ]
        for index, record in enumerate(records):
            if (
                record.get("continuation_id") == continuation_id
                and record.get("claim_token") == claim_token
                and record.get("status") == "generating"
                and _record_matches_owner(record, source)
            ):
                record.update(
                    {
                        "claim_expires_at": (
                            time.time() + QUQU_CLAIM_LEASE_SECONDS
                        ),
                        "updated_at": _utc_now(),
                    }
                )
                records[index] = record
                meta[QUQU_CONTINUATIONS_META_KEY] = records
                return meta, True
        return None, False

    chat, renewed = await manager.mutate_chat_meta(source.chat_id, mutate)
    return chat is not None and bool(renewed)


async def wait_for_ququ_continuation(
    *,
    manager: ChatManager,
    source: QuquSource,
    timeout_seconds: float = 55.0,
) -> dict[str, Any]:
    """Wait for the atomic owner without starting another model call."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        chat = await manager.get_chat(source.chat_id)
        if chat is None:
            raise QuquContinuationError(404, "原回答已不存在，不能继续这句蛐蛐")
        raw_records = (chat.meta or {}).get(QUQU_CONTINUATIONS_META_KEY, [])
        records = raw_records if isinstance(raw_records, list) else []
        record = next(
            (
                item
                for item in records
                if isinstance(item, dict)
                and item.get("source_ququ_id") == source.source_ququ_id
            ),
            None,
        )
        if record is None:
            raise QuquContinuationError(
                409,
                "续念认领状态已失效，请明确重试",
                code="ququ_generation_failed",
                retryable=True,
            )
        status = str(record.get("status") or "")
        if status == "completed":
            return record
        if status == "failed":
            raise QuquContinuationError(
                409,
                "续念生成失败，请明确重试",
                code="ququ_generation_failed",
                retryable=True,
            )
        try:
            claim_is_live = (
                float(record.get("claim_expires_at") or 0) > time.time()
            )
        except (TypeError, ValueError):
            claim_is_live = False
        if status == "generating" and not claim_is_live:
            raise QuquContinuationError(
                409,
                "上次生成未成功保存；请明确重试后再重新生成",
                code="ququ_generation_failed",
                retryable=True,
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise QuquContinuationError(
                409,
                "续念仍在生成，请稍后重试同一请求",
                code="ququ_generation_pending",
            )
        await asyncio.sleep(0.05)


async def finalize_ququ_continuation(
    *,
    manager: ChatManager,
    source: QuquSource,
    claimed_record: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Durably finalize only the still-owned claim on the still-owned chat."""

    claim_token = str(claimed_record.get("claim_token") or "")
    continuation_id = str(claimed_record.get("continuation_id") or "")
    now = _utc_now()

    def mutate(meta: dict[str, Any], chat: ChatSpec):
        if (
            chat.session_id != source.session_id
            or chat.user_id != source.user_id
            or chat.channel != source.channel
        ):
            return None, None
        raw_records = meta.get(QUQU_CONTINUATIONS_META_KEY, [])
        records = [
            dict(item) for item in raw_records if isinstance(item, dict)
        ]
        target_index = next(
            (
                index
                for index, record in enumerate(records)
                if record.get("continuation_id") == continuation_id
            ),
            None,
        )
        if target_index is None:
            return None, None
        record = records[target_index]
        if (
            record.get("status") != "generating"
            or record.get("claim_token") != claim_token
            or not _record_matches_owner(record, source)
        ):
            return None, None
        record.update({"text": text, "status": "completed", "updated_at": now})
        record.pop("claim_token", None)
        record.pop("claim_expires_at", None)
        record.pop("failure", None)
        records[target_index] = record
        meta[QUQU_CONTINUATIONS_META_KEY] = records
        return meta, record

    chat, result = await manager.mutate_chat_meta(source.chat_id, mutate)
    if chat is None or result is None:
        raise QuquContinuationError(409, "原回答或权限已经变化，续念未保存")
    return result


async def fail_ququ_continuation(
    *,
    manager: ChatManager,
    source: QuquSource,
    claimed_record: dict[str, Any],
    failure: str,
) -> None:
    """Persist an observable failure without inventing recoverable text."""

    claim_token = str(claimed_record.get("claim_token") or "")
    continuation_id = str(claimed_record.get("continuation_id") or "")

    def mutate(meta: dict[str, Any], chat: ChatSpec):
        if (
            chat.session_id != source.session_id
            or chat.user_id != source.user_id
            or chat.channel != source.channel
        ):
            return None, False
        raw_records = meta.get(QUQU_CONTINUATIONS_META_KEY, [])
        records = [
            dict(item) for item in raw_records if isinstance(item, dict)
        ]
        for index, record in enumerate(records):
            if (
                record.get("continuation_id") == continuation_id
                and record.get("claim_token") == claim_token
                and record.get("status") == "generating"
            ):
                record.update(
                    {
                        "status": "failed",
                        "text": "",
                        "failure": failure[:200],
                        "updated_at": _utc_now(),
                    }
                )
                record.pop("claim_token", None)
                record.pop("claim_expires_at", None)
                records[index] = record
                meta[QUQU_CONTINUATIONS_META_KEY] = records
                return meta, True
        return None, False

    await manager.mutate_chat_meta(source.chat_id, mutate)
