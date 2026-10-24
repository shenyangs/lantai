"""Optional, narrowly admitted Paper Search MCP using the existing control plane.

This builds configuration; it never installs dependencies, changes the user's
MCP store or starts a second research runtime. The external server stays in its
own interpreter because its FastMCP version differs from the desktop runtime.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from ..config.config import MCPClientConfig

PAPER_SEARCH_REPOSITORY = 'https://github.com/openags/paper-search-mcp'
PAPER_SEARCH_COMMIT = 'cab1b5cc276a0cd004a0044003926a6440088049'
PAPER_SEARCH_ALLOWED_TOOLS = ('search_crossref', 'get_crossref_paper_by_doi')


def build_paper_search_config(checkout: Path, python: Path, *, enabled=False) -> MCPClientConfig:
    """Validate the admitted installation and return a scoped stdio client config."""
    checkout, python = Path(checkout).resolve(), Path(python).absolute()
    if not (checkout / 'paper_search_mcp/server.py').is_file() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('Paper Search MCP requires a pinned installation and its isolated Python interpreter')

    def git(*args):
        try:
            result = subprocess.run(['git', '-C', str(checkout), *args], capture_output=True,
                                    text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError('Cannot verify the pinned Paper Search MCP installation') from exc
        if result.returncode:
            raise ValueError('Cannot verify the pinned Paper Search MCP installation')
        return result.stdout.strip()

    if git('rev-parse', 'HEAD') != PAPER_SEARCH_COMMIT:
        raise ValueError('Paper Search MCP installation is not the admitted pinned commit')
    if git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Paper Search MCP installation is modified; re-admission is required')
    return MCPClientConfig(name='论文元数据 · Paper Search', description='Crossref 只读检索与 DOI 查询；元数据不是全文证据。',
        enabled=enabled, transport='stdio', command=str(python),
        args=['-m', 'paper_search_mcp.server', '--transport', 'stdio'], cwd=str(checkout),
        env={'PAPER_SEARCH_MCP_SEARCH_CACHE_ENABLED': '0'}, allowed_tools=list(PAPER_SEARCH_ALLOWED_TOOLS))
