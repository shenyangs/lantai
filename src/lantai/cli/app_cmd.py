# -*- coding: utf-8 -*-
from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Callable
from typing import Any

import click
import uvicorn

from ..constant import LOG_LEVEL_ENV
from ..config.utils import write_last_api
from ..utils.logging import setup_logger, SuppressPathAccessLogFilter


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _auth_enabled_from_env() -> bool:
    return os.environ.get("LANTAI_AUTH_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _auth_bootstrap_ready() -> bool:
    from ..app.auth import AuthStorageError, has_registered_users

    try:
        registered = has_registered_users()
    except AuthStorageError as exc:
        raise click.ClickException(
            "Refusing a non-loopback bind because auth.json is unavailable. "
            "Preserve the original file and inspect its backup before "
            "restoring a known-good copy."
        ) from exc
    if registered:
        return True
    username = os.environ.get("LANTAI_AUTH_USERNAME", "").strip()
    password = os.environ.get("LANTAI_AUTH_PASSWORD", "").strip()
    return bool(username and password)


def _run_app(
    host: str,
    port: int,
    reload: bool,
    workers: int | None,  # pylint: disable=unused-argument
    log_level: str,
    hide_access_paths: tuple[str, ...],
) -> None:
    """Run 澜台 FastAPI app."""
    if not _is_loopback_host(host) and not _auth_enabled_from_env():
        raise click.ClickException(
            "Refusing a non-loopback bind while authentication is disabled. "
            "Set LANTAI_AUTH_ENABLED=true and configure an account before "
            "exposing 澜台 beyond this machine.",
        )
    if not _is_loopback_host(host) and not _auth_bootstrap_ready():
        raise click.ClickException(
            "Refusing a non-loopback bind without a registered account. "
            "Register locally first, or set both LANTAI_AUTH_USERNAME and "
            "LANTAI_AUTH_PASSWORD for first-run bootstrap.",
        )

    # Handle deprecated --workers parameter
    if workers is not None:
        click.echo(
            "⚠️  WARNING: --workers option is deprecated and will be removed "
            "in a future version.",
            err=True,
        )
        click.echo(
            "   澜台 always uses 1 worker for stability. "
            "Your specified value will be ignored.",
            err=True,
        )
        click.echo(err=True)

    # Persist last used host/port for other terminals
    if host == "0.0.0.0":
        write_last_api("127.0.0.1", port)
    else:
        write_last_api(host, port)
    os.environ[LOG_LEVEL_ENV] = log_level

    # Signal reload mode to browser_control.py for Windows
    # compatibility: use sync Playwright + ThreadPool only when reload=True
    if reload:
        os.environ["LANTAI_RELOAD_MODE"] = "1"
    else:
        os.environ.pop("LANTAI_RELOAD_MODE", None)

    setup_logger(log_level)
    if log_level in ("debug", "trace"):
        from .main import log_init_timings

        log_init_timings()

    paths = [p for p in hide_access_paths if p]
    if paths:
        logging.getLogger("uvicorn.access").addFilter(
            SuppressPathAccessLogFilter(paths),
        )

    from lantai.app._app import app as fastapi_app

    uvicorn.run(
        fastapi_app,
        host=host,
        port=port,
        reload=reload,
        workers=1,
        log_level=log_level,
    )


def _app_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Apply the shared server options to both public command names."""
    func = click.option(
        "--workers",
        type=int,
        default=None,
        help="[DEPRECATED] Number of worker processes. "
        "This option is deprecated and will be removed in a future version. "
        "澜台 always uses 1 worker.",
    )(func)
    func = click.option(
        "--hide-access-paths",
        multiple=True,
        default=("/console/push-messages",),
        show_default=True,
        help="Path substrings to hide from uvicorn access log (repeatable).",
    )(func)
    func = click.option(
        "--log-level",
        default="info",
        type=click.Choice(
            ["critical", "error", "warning", "info", "debug", "trace"],
            case_sensitive=False,
        ),
        show_default=True,
        help="Log level",
    )(func)
    func = click.option(
        "--reload",
        is_flag=True,
        help="Enable auto-reload (dev only)",
    )(func)
    func = click.option(
        "--port",
        default=8088,
        type=int,
        show_default=True,
        help="Bind port",
    )(func)
    return click.option(
        "--host",
        default="127.0.0.1",
        show_default=True,
        help="Bind host",
    )(func)


@click.command("app")
@_app_options
def app_cmd(
    host: str,
    port: int,
    reload: bool,
    workers: int | None,
    log_level: str,
    hide_access_paths: tuple[str, ...],
) -> None:
    """Run the 澜台 FastAPI app (legacy command name)."""
    _run_app(host, port, reload, workers, log_level, hide_access_paths)


@click.command("run")
@_app_options
def run_cmd(
    host: str,
    port: int,
    reload: bool,
    workers: int | None,
    log_level: str,
    hide_access_paths: tuple[str, ...],
) -> None:
    """Start Console, agents, channels and scheduled tasks in one process."""
    _run_app(host, port, reload, workers, log_level, hide_access_paths)
