# -*- coding: utf-8 -*-
"""Separate transport routing from durable conversation ownership."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .models import ChatSpec

if TYPE_CHECKING:
    from .manager import ChatManager

logger = logging.getLogger(__name__)

SESSION_STATE_ID_META_KEY = "session_state_id"
STATE_BINDING_VERSION_META_KEY = "state_binding_version"
STATE_MIGRATION_STATE_META_KEY = "state_migration_state"
LEGACY_SOURCE_REFERENCE_META_KEY = "legacy_source_reference"
LEGACY_HISTORY_NOTICE_META_KEY = "legacy_history_notice"
LEGACY_RECOVERY_PROVENANCE_META_KEY = "legacy_recovery_provenance"
STATE_BINDING_VERSION = 1
LEGACY_HISTORY_NOTICE = "检测到旧版混合历史，已隔离，原始内容仍可恢复"


def canonical_chat_session_state_id(chat: ChatSpec) -> str:
    """Return the durable state key owned only by ``chat``."""

    return f"{chat.channel}:chat:{chat.id}"


def _owned_explicit_session_state_id(chat: ChatSpec) -> str:
    """Return only a server-shaped key that is owned by ``chat``.

    Older releases accepted arbitrary chat metadata.  Treating an old,
    client-supplied value as authoritative would let one chat claim another
    chat's durable state after upgrade.
    """

    explicit = str(
        (chat.meta or {}).get(SESSION_STATE_ID_META_KEY) or ""
    ).strip()
    expected = canonical_chat_session_state_id(chat)
    if explicit == expected:
        return explicit
    if explicit:
        logger.warning(
            "Ignoring non-owned conversation state key: chat_id=%s "
            "state_session_id=%s expected=%s",
            chat.id,
            explicit,
            expected,
        )
    return ""


def new_chat_meta(
    meta: dict | None,
    *,
    chat_id: str,
    channel: str,
) -> dict:
    """Bind a newly-created chat to a server-owned durable state key."""

    owned = dict(meta or {})
    owned[SESSION_STATE_ID_META_KEY] = f"{channel}:chat:{chat_id}"
    owned[STATE_BINDING_VERSION_META_KEY] = STATE_BINDING_VERSION
    owned[STATE_MIGRATION_STATE_META_KEY] = "bound"
    return owned


def _ambiguous_legacy_meta(chat: ChatSpec) -> dict:
    """Describe an isolated legacy source without assigning its mixed bytes."""

    meta = new_chat_meta(
        chat.meta,
        chat_id=chat.id,
        channel=chat.channel,
    )
    meta.update(
        {
            STATE_MIGRATION_STATE_META_KEY: "needs_manual_recovery",
            LEGACY_SOURCE_REFERENCE_META_KEY: {
                "session_id": chat.session_id,
                "user_id": chat.user_id,
                "channel": chat.channel,
            },
            LEGACY_HISTORY_NOTICE_META_KEY: LEGACY_HISTORY_NOTICE,
        },
    )
    return meta


def bind_ambiguous_legacy_session_state_ids(
    chats: Sequence[ChatSpec],
) -> tuple[list[ChatSpec], bool]:
    """Persist the isolation decision for every ambiguous legacy owner.

    The pure resolver below must preserve the transport key for a sole legacy
    row so existing history remains readable after upgrade. Once two legacy
    rows claim the same transport tuple, however, their canonical isolation
    keys must become durable before either row is deleted or migrated. Without
    this binding, the survivor can later fall back to the formerly shared key.

    This function only changes ownership metadata. It deliberately preserves
    ``updated_at`` so a compatibility migration cannot reorder conversations.
    """

    bound = list(chats)
    legacy_groups: dict[tuple[str, str, str], list[int]] = {}
    for index, chat in enumerate(bound):
        if _owned_explicit_session_state_id(chat):
            continue
        key = (chat.session_id, chat.user_id, chat.channel)
        legacy_groups.setdefault(key, []).append(index)

    changed = False
    for indexes in legacy_groups.values():
        if len(indexes) <= 1:
            continue
        for index in indexes:
            chat = bound[index]
            canonical = canonical_chat_session_state_id(chat)
            bound[index] = chat.model_copy(
                update={
                    "meta": _ambiguous_legacy_meta(chat),
                },
            )
            changed = True
            logger.warning(
                "Persisting ambiguous legacy conversation owner: "
                "chat_id=%s transport_session_id=%s "
                "state_session_id=%s",
                chat.id,
                chat.session_id,
                canonical,
            )
    return bound, changed


def session_state_id_from_known_chats(
    chat: ChatSpec,
    chats: Sequence[ChatSpec],
) -> str:
    """Resolve state ownership without assigning ambiguous legacy data.

    Chats created before the ownership key was introduced keep their legacy
    state while they are the sole owner of a transport session. If two legacy
    chat rows claim the same transport tuple, neither is allowed to silently
    adopt the shared state file; each reads and writes its own canonical key.
    The old file remains on disk for explicit recovery.
    """

    explicit = _owned_explicit_session_state_id(chat)
    if explicit:
        return explicit

    matching_legacy = [
        candidate
        for candidate in chats
        if candidate.session_id == chat.session_id
        and candidate.user_id == chat.user_id
        and candidate.channel == chat.channel
        and not _owned_explicit_session_state_id(candidate)
    ]
    if len(matching_legacy) <= 1:
        return chat.session_id

    isolated = canonical_chat_session_state_id(chat)
    logger.warning(
        "Isolating ambiguous legacy conversation state: chat_id=%s "
        "transport_session_id=%s legacy_owners=%d state_session_id=%s",
        chat.id,
        chat.session_id,
        len(matching_legacy),
        isolated,
    )
    return isolated


async def resolve_chat_session_state_id(
    mgr: ChatManager,
    chat: ChatSpec,
) -> str:
    """Resolve and, when ambiguous, persist one chat's durable state key."""

    explicit = _owned_explicit_session_state_id(chat)
    if explicit:
        return explicit
    persisted = await mgr.ensure_chat_session_state_id(chat.id)
    if persisted is not None:
        return persisted
    chats = await mgr.list_chats(user_id=chat.user_id, channel=chat.channel)
    return session_state_id_from_known_chats(chat, chats)
