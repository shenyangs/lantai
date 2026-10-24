# -*- coding: utf-8 -*-
"""Shared HTTP transport primitives for OpenAI-compatible clients.

Frozen macOS applications can spend several seconds loading the system and
certifi trust stores.  Lantai creates short-lived model facade objects for
each turn, but those facades do not need distinct trust stores.  Reuse the
immutable SSL context while keeping an independent HTTP connection pool per
SDK client, so credentials, headers and client shutdown remain isolated.
"""

from __future__ import annotations

import hashlib
import json
import ssl
from functools import lru_cache

import httpx
from openai import DefaultAsyncHttpxClient


OPENAI_TRANSPORT_CONNECT_TIMEOUT_SECONDS = 5.0
OPENAI_TRANSPORT_READ_TIMEOUT_SECONDS = 600.0
OPENAI_TRANSPORT_WRITE_TIMEOUT_SECONDS = 600.0
OPENAI_TRANSPORT_POOL_TIMEOUT_SECONDS = 600.0
# Runtime's RetryingModelClient owns retry lineage and budget accounting.  The
# SDK must not multiply those attempts invisibly underneath that owner.
OPENAI_SDK_MAX_RETRIES = 0


@lru_cache(maxsize=1)
def shared_openai_ssl_context() -> ssl.SSLContext:
    """Return the process-wide default trust context.

    ``httpx.create_ssl_context`` preserves its normal environment handling,
    including ``SSL_CERT_FILE`` and ``SSL_CERT_DIR``.  Environment-backed
    trust configuration is process startup configuration, so caching it for
    the backend lifetime is both deterministic and safe.
    """

    return httpx.create_ssl_context(verify=True, trust_env=True)


def create_openai_async_http_client() -> httpx.AsyncClient:
    """Create an OpenAI-default async client using the shared trust context."""

    return DefaultAsyncHttpxClient(
        verify=shared_openai_ssl_context(),
        timeout=httpx.Timeout(
            connect=OPENAI_TRANSPORT_CONNECT_TIMEOUT_SECONDS,
            read=OPENAI_TRANSPORT_READ_TIMEOUT_SECONDS,
            write=OPENAI_TRANSPORT_WRITE_TIMEOUT_SECONDS,
            pool=OPENAI_TRANSPORT_POOL_TIMEOUT_SECONDS,
        ),
    )


def openai_transport_policy_receipt() -> dict[str, float | int | str]:
    """Return the effective transport/retry policy without endpoint secrets."""

    return {
        "connect_timeout_seconds": OPENAI_TRANSPORT_CONNECT_TIMEOUT_SECONDS,
        "read_timeout_seconds": OPENAI_TRANSPORT_READ_TIMEOUT_SECONDS,
        "write_timeout_seconds": OPENAI_TRANSPORT_WRITE_TIMEOUT_SECONDS,
        "pool_timeout_seconds": OPENAI_TRANSPORT_POOL_TIMEOUT_SECONDS,
        "sdk_max_retries": OPENAI_SDK_MAX_RETRIES,
        "retry_owner": "lantai.runtime.model_wrappers.RetryingModelClient",
    }


def openai_endpoint_policy_receipt(
    *,
    provider_id: str,
    model_id: str,
    base_url: str,
    protocol: str,
) -> dict[str, object]:
    """Return a secret-free endpoint and transport policy receipt."""

    endpoint_identity = {
        "provider_id": provider_id,
        "model_id": model_id,
        "base_url": base_url.rstrip("/"),
        "protocol": protocol,
    }
    endpoint_fingerprint = hashlib.sha256(
        json.dumps(
            endpoint_identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "endpoint_fingerprint_sha256": endpoint_fingerprint,
        "provider_id": provider_id,
        "model_id": model_id,
        "protocol": protocol,
        "transport": openai_transport_policy_receipt(),
    }


__all__ = [
    "OPENAI_SDK_MAX_RETRIES",
    "create_openai_async_http_client",
    "openai_endpoint_policy_receipt",
    "openai_transport_policy_receipt",
    "shared_openai_ssl_context",
]
