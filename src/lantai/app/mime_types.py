# -*- coding: utf-8 -*-
"""Sandbox-safe MIME type initialization for the web console."""
from __future__ import annotations

import mimetypes


_BROWSER_MIME_TYPES = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".wasm": "application/wasm",
}


def init_browser_mime_types() -> None:
    """Initialize MIME types without reading host OS mime.types files.

    Python's ``mimetypes.init(files=[])`` still prepends ``knownfiles`` on
    Python 3.12, which makes sandboxed Mac App Store builds try to read paths
    such as ``/etc/apache2/mime.types`` during backend import. Clearing
    ``knownfiles`` first keeps the built-in table and avoids sandbox denials.
    """

    mimetypes.knownfiles = []
    mimetypes.init(files=[])
    for suffix, mime_type in _BROWSER_MIME_TYPES.items():
        mimetypes.add_type(mime_type, suffix)
