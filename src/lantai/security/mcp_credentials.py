# -*- coding: utf-8 -*-
"""Transparent at-rest protection for MCP connector credentials.

The public configuration model stays ergonomic and contains plaintext while
it is in memory.  This module is the persistence seam: every non-empty MCP
header/environment value and every OAuth secret is encrypted before JSON is
written, then decrypted immediately after JSON is read.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .secret_store import decrypt, encrypt, is_encrypted

_OAUTH_SECRET_FIELDS = ("client_secret", "access_token", "refresh_token")


def protect_agent_mcp_credentials(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return an encrypted copy and whether plaintext credentials changed."""

    protected = deepcopy(data)
    changed = False
    mcp = protected.get("mcp")
    clients = mcp.get("clients") if isinstance(mcp, dict) else None
    if not isinstance(clients, dict):
        return protected, changed

    for client in clients.values():
        if not isinstance(client, dict):
            continue
        for mapping_name in ("headers", "env"):
            mapping = client.get(mapping_name)
            if not isinstance(mapping, dict):
                continue
            for key, value in list(mapping.items()):
                if isinstance(value, str) and value and not is_encrypted(value):
                    mapping[key] = encrypt(value)
                    changed = True

        oauth = client.get("oauth")
        if not isinstance(oauth, dict):
            continue
        for field in _OAUTH_SECRET_FIELDS:
            value = oauth.get(field)
            if isinstance(value, str) and value and not is_encrypted(value):
                oauth[field] = encrypt(value)
                changed = True

    return protected, changed


def reveal_agent_mcp_credentials(data: dict[str, Any]) -> dict[str, Any]:
    """Return a decrypted copy, failing closed on corrupt ciphertext."""

    revealed = deepcopy(data)
    mcp = revealed.get("mcp")
    clients = mcp.get("clients") if isinstance(mcp, dict) else None
    if not isinstance(clients, dict):
        return revealed

    for client in clients.values():
        if not isinstance(client, dict):
            continue
        for mapping_name in ("headers", "env"):
            mapping = client.get(mapping_name)
            if not isinstance(mapping, dict):
                continue
            for key, value in list(mapping.items()):
                if isinstance(value, str) and value:
                    mapping[key] = decrypt(value)

        oauth = client.get("oauth")
        if not isinstance(oauth, dict):
            continue
        for field in _OAUTH_SECRET_FIELDS:
            value = oauth.get(field)
            if isinstance(value, str) and value:
                oauth[field] = decrypt(value)

    return revealed

