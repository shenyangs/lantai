"""Run Lantai as an Agent Client Protocol server over stdio."""

from __future__ import annotations

import asyncio

import click
from acp import run_agent

from ..integrations.acp_server import LantaiAcpAgent, LantaiAcpBridge


async def _serve_acp(
    *,
    base_url: str,
    agent_id: str,
    task_timeout_seconds: float,
) -> None:
    bridge = LantaiAcpBridge(
        base_url=base_url,
        agent_id=agent_id,
        task_timeout_seconds=task_timeout_seconds,
    )
    try:
        await run_agent(LantaiAcpAgent(bridge))
    finally:
        await bridge.aclose()


@click.group("acp", invoke_without_command=True)
@click.option(
    "--base-url",
    default=None,
    help="Running Lantai API origin. Defaults to the CLI host and port.",
)
@click.option(
    "--agent",
    "agent_id",
    default="default",
    show_default=True,
    help="Lantai agent profile used for IDE sessions.",
)
@click.option(
    "--task-timeout",
    type=click.IntRange(60, 3600),
    default=900,
    show_default=True,
    help="Maximum seconds for one IDE turn.",
)
@click.pass_context
def acp_cmd(
    ctx: click.Context,
    base_url: str | None,
    agent_id: str,
    task_timeout: int,
) -> None:
    """Serve Lantai to ACP-compatible IDEs over standard input/output."""
    if ctx.invoked_subcommand is not None:
        return
    root_options = ctx.ensure_object(dict)
    resolved_base_url = base_url or (
        f"http://{root_options['host']}:{root_options['port']}"
    )
    asyncio.run(
        _serve_acp(
            base_url=resolved_base_url,
            agent_id=agent_id,
            task_timeout_seconds=float(task_timeout),
        ),
    )


@acp_cmd.command("pair")
@click.argument("code")
@click.option(
    "--base-url",
    default=None,
    help="Running Lantai API origin. Defaults to the CLI host and port.",
)
@click.pass_context
def pair_cmd(ctx: click.Context, code: str, base_url: str | None) -> None:
    """Pair this CLI with the packaged Lantai desktop API once."""
    parent = ctx.parent
    root = parent.parent if parent is not None else None
    root_options = root.ensure_object(dict) if root is not None else {}
    parent_base_url = parent.params.get("base_url") if parent is not None else None
    resolved_base_url = base_url or parent_base_url or (
        f"http://{root_options.get('host', '127.0.0.1')}:"
        f"{root_options.get('port', 8088)}"
    )

    async def pair_once() -> int:
        bridge = LantaiAcpBridge(base_url=resolved_base_url)
        try:
            return await bridge.pair(code)
        finally:
            await bridge.aclose()

    try:
        expires_at = asyncio.run(pair_once())
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Lantai ACP pairing saved (expires_at={expires_at}).")
