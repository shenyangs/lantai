# -*- coding: utf-8 -*-
"""
Agent tools schema: type definitions for agent tool responses.
"""
from typing import Literal, Optional
from typing_extensions import TypedDict, Required


class Base64Source(TypedDict):
    """Base64-encoded media source."""

    type: Required[Literal["base64"]]
    media_type: Required[str]
    data: Required[str]


class URLSource(TypedDict):
    """URL media source."""

    type: Required[Literal["url"]]
    url: Required[str]


class FileBlock(TypedDict, total=False):
    """File block for sending files to users."""

    type: Required[Literal["file"]]
    """The type of the block"""

    source: Required[Base64Source | URLSource]
    """The source of the file"""

    filename: Optional[str]
    """The filename of the file"""
