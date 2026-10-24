#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real stdio MCP 2026-07-28 handshake/list/call smoke for a Lantai binary."""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client


async def run(executable: str, args: list[str]) -> dict[str, object]:
    transport = stdio_client(
        StdioServerParameters(command=executable, args=args),
    )
    async with Client(transport, mode="auto", read_timeout_seconds=30) as client:
        listed = await client.list_tools(cache_mode="refresh")
        names = [tool.name for tool in listed.tools]
        if "guanlan_capabilities" not in names:
            raise RuntimeError("guanlan_capabilities missing from MCP tool list")
        result = await client.call_tool(
            "guanlan_capabilities",
            {"format": "json"},
        )
        if result.is_error:
            raise RuntimeError("guanlan_capabilities returned an MCP error")
        invalid = await client.call_tool("guanlan_search", {})
        if not invalid.is_error:
            raise RuntimeError("Guanlan MCP input validation did not fail closed")
        return {
            "protocol_version": client.protocol_version,
            "tool_count": len(names),
            "call_ok": True,
            "input_validation_fail_closed": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable")
    parser.add_argument("args", nargs="*", default=["guanlan-mcp"])
    parsed = parser.parse_args()
    print(json.dumps(asyncio.run(run(parsed.executable, parsed.args)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
