# -*- coding: utf-8 -*-
"""Guarded BrowserSkill CLI adapter for Lantai Browser Workbench.

BrowserSkill owns Chromium/extension transport and its isolated Agent Window.
Lantai remains the authority for task scope, policy, confirmation, file grants,
effect evidence, and replay.  Commands are executed without a shell so page or
model text can never become executable command syntax.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
from typing import Any

from ...constant import WORKING_DIR
from ...config.context import get_current_workspace_dir
from .models import BrowserAction
from .policy import canonical_action_name, infer_action_access


BROWSER_SKILL_REPOSITORY = "https://github.com/Tencent/BrowserSkill"
BROWSER_SKILL_UPSTREAM_COMMIT = "47ac947f188bc554b66c09f5873b8045d6be57b2"
_MAX_CLI_OUTPUT_BYTES = 2 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 40.0


class BrowserSkillClient:
    """Translate broker-approved actions into BrowserSkill ``bsk`` commands."""

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.cli_path = self._resolve_cli_path(cli_path)
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 300.0))
        self._sessions: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def installed(self) -> bool:
        return self.cli_path is not None

    async def status(self) -> dict[str, Any]:
        if self.cli_path is None:
            self.cli_path = self._resolve_cli_path(None)
        if self.cli_path is None:
            return {
                "ok": True,
                "installed": False,
                "connected": False,
                "ready": False,
                "status": "not_installed",
                "runtime": "browser_skill",
                "message": "尚未安装 BrowserSkill CLI。",
                "repository_url": BROWSER_SKILL_REPOSITORY,
                "upstream_commit": BROWSER_SKILL_UPSTREAM_COMMIT,
                "browsers": [],
                "sessions": [],
            }
        payload = await self._run_json(["status"], timeout_seconds=8.0)
        if not payload.get("ok"):
            return {
                **payload,
                "installed": True,
                "connected": False,
                "ready": False,
                "status": "unavailable",
                "runtime": "browser_skill",
                "message": str(
                    payload.get("error") or "BrowserSkill 暂时不可用。"
                ),
                "repository_url": BROWSER_SKILL_REPOSITORY,
                "upstream_commit": BROWSER_SKILL_UPSTREAM_COMMIT,
            }
        browsers = payload.get("browsers")
        sessions = payload.get("sessions")
        connected = isinstance(browsers, list) and bool(browsers)
        return {
            **payload,
            "ok": True,
            "installed": True,
            "connected": connected,
            "ready": connected and len(browsers) == 1,
            "status": "available" if connected else "extension_disconnected",
            "runtime": "browser_skill",
            "message": self._status_message(
                connected=connected,
                browser_count=len(browsers) if isinstance(browsers, list) else 0,
            ),
            "browsers": browsers if isinstance(browsers, list) else [],
            "sessions": sessions if isinstance(sessions, list) else [],
            "repository_url": BROWSER_SKILL_REPOSITORY,
            "upstream_commit": BROWSER_SKILL_UPSTREAM_COMMIT,
        }

    async def is_connected(self) -> bool:
        return bool((await self.status()).get("connected"))

    @staticmethod
    def _status_message(*, connected: bool, browser_count: int) -> str:
        if browser_count > 1:
            return "检测到多个 BrowserSkill 浏览器，请先只保留一个连接。"
        if connected:
            return "BrowserSkill 已连接，可复用 Chrome/Edge 登录状态。"
        return "CLI 已安装，等待 Chrome/Edge 扩展连接。"

    async def execute(self, action: BrowserAction) -> dict[str, Any]:
        if self.cli_path is None:
            return self._error(
                "BrowserSkill CLI is not installed.",
                "browser_skill_not_installed",
            )
        profile_id = action.profile_id or "default"
        lock = self._locks.setdefault(profile_id, asyncio.Lock())
        async with lock:
            canonical = canonical_action_name(action.action)
            if canonical in {"status", "state"}:
                return await self.status()
            if canonical in {"stop", "close_task_group"}:
                return await self.stop(profile_id)

            session_id = await self._ensure_session(action)
            if not session_id:
                return self._error(
                    "BrowserSkill could not start an Agent Window session.",
                    "browser_skill_session_unavailable",
                )
            command = await self._command_for_action(action, session_id)
            if isinstance(command, dict):
                return command

            payload = await self._run_json(
                command,
                timeout_seconds=self._action_timeout(action),
                effect_unknown_on_timeout=(infer_action_access(canonical) != "read"),
            )
            payload.setdefault("runtime", "browser_skill")
            payload.setdefault("session_id", session_id)
            payload.setdefault("profile_id", profile_id)
            if "tab_id" in payload:
                payload.setdefault("page_id", str(payload["tab_id"]))
            if payload.get("ok") and canonical in {"click", "press_key"}:
                payload = await self._attach_tab_state(payload, session_id)
            if canonical in {"snapshot", "snapshot_ax", "snapshot_dom"}:
                text = payload.get("text")
                if isinstance(text, str):
                    payload.setdefault("snapshot", text)
            return payload

    async def stop(self, profile_id: str = "default") -> dict[str, Any]:
        session_id = self._sessions.pop(profile_id or "default", None)
        if not session_id:
            return {
                "ok": True,
                "runtime": "browser_skill",
                "message": "BrowserSkill session is already stopped.",
                "stopped": [],
            }
        payload = await self._run_json(
            ["session", "stop", session_id],
            timeout_seconds=65.0,
        )
        payload.setdefault("runtime", "browser_skill")
        payload.setdefault("session_id", session_id)
        return payload

    async def stop_all(self) -> dict[str, Any]:
        """Stop every session created by this Lantai process."""
        profile_ids = list(self._sessions)
        results = [await self.stop(profile_id) for profile_id in profile_ids]
        return {
            "ok": all(bool(result.get("ok")) for result in results),
            "runtime": "browser_skill",
            "profiles": profile_ids,
            "results": results,
        }

    async def _ensure_session(self, action: BrowserAction) -> str:
        profile_id = action.profile_id or "default"
        current = self._sessions.get(profile_id)
        if current:
            return current

        status = await self.status()
        if not status.get("connected"):
            return ""
        browsers = status.get("browsers") or []
        requested_browser = str(
            action.metadata.get("browser_skill_browser_id") or ""
        ).strip()
        if not requested_browser and len(browsers) == 1:
            requested_browser = str(browsers[0].get("instance_id") or "").strip()
        if not requested_browser and len(browsers) > 1:
            return ""

        command = ["session", "start", "--no-focus"]
        if requested_browser:
            command.extend(["--browser", requested_browser])
        payload = await self._run_json(command, timeout_seconds=25.0)
        session_id = str(payload.get("session_id") or "").strip()
        if payload.get("ok") and session_id:
            self._sessions[profile_id] = session_id
            return session_id
        return ""

    async def _command_for_action(
        self,
        action: BrowserAction,
        session_id: str,
    ) -> list[str] | dict[str, Any]:
        canonical = canonical_action_name(action.action)
        tab_id = self._tab_id(action.page_id)
        tab_args = ["--tab-id", str(tab_id)] if tab_id is not None else []

        if canonical in {"open", "navigate"}:
            if not action.url:
                return self._error("Browser URL is required.", "browser_url_required")
            return ["navigate", action.url, "--session", session_id, *tab_args]
        if canonical == "new_tab":
            command = ["tab", "create", "--session", session_id]
            if action.url:
                command.extend(["--url", action.url])
            return command
        if canonical in {"snapshot", "snapshot_ax", "snapshot_dom"}:
            command = ["observe", "--session", session_id, *tab_args]
            max_tokens = int(action.metadata.get("max_tokens") or 12_000)
            command.extend(["--max-tokens", str(max(500, min(max_tokens, 50_000)))])
            if action.metadata.get("probe_hover"):
                command.append("--probe-hover")
            return command
        if canonical in {"screenshot", "take_screenshot"}:
            output_path = self._resolve_output_path(action.path or "page.png")
            command = [
                "screenshot",
                "--session",
                session_id,
                "--out",
                str(output_path),
                *tab_args,
            ]
            if action.ref:
                command.extend(["--ref", action.ref])
            return command
        if canonical in {"click", "hover"}:
            target = self._target(action)
            if not target:
                return self._error(
                    f"Browser {canonical} requires a ref or selector.",
                    "browser_target_required",
                )
            return [canonical, target, "--session", session_id, *tab_args]
        if canonical == "type":
            target = self._target(action)
            if not target:
                return self._error(
                    "Browser fill requires a ref or selector.",
                    "browser_target_required",
                )
            return [
                "fill",
                target,
                "--value",
                action.text,
                "--session",
                session_id,
                *tab_args,
            ]
        if canonical == "select":
            target = self._target(action)
            value = str(action.metadata.get("value") or action.text or "")
            if not value:
                raw_values = action.metadata.get("values_json")
                try:
                    parsed_values = json.loads(str(raw_values or "[]"))
                except json.JSONDecodeError:
                    parsed_values = []
                if isinstance(parsed_values, list) and parsed_values:
                    value = str(parsed_values[0])
            if not target or not value:
                return self._error(
                    "Browser select requires a target and option value.",
                    "browser_select_params_required",
                )
            return [
                "select",
                target,
                "--value",
                value,
                "--session",
                session_id,
                *tab_args,
            ]
        if canonical == "press_key":
            key = str(action.metadata.get("key") or action.text or "").strip()
            if not key:
                return self._error("Browser key is required.", "browser_key_required")
            return ["press", key, "--session", session_id, *tab_args]
        if canonical == "evaluate":
            if not action.code:
                return self._error(
                    "Browser evaluate code is required.",
                    "browser_evaluate_code_required",
                )
            return [
                "evaluate",
                action.code,
                "--session",
                session_id,
                *tab_args,
            ]
        if canonical in {"tabs", "tab_list"}:
            tab_action = str(action.metadata.get("tab_action") or "list").lower()
            if tab_action == "new":
                return ["tab", "create", "--session", session_id]
            if tab_action in {"close", "select"}:
                index = action.metadata.get("index")
                if not isinstance(index, int) or index < 0:
                    return self._error(
                        "BrowserSkill tab close/select requires a non-negative index.",
                        "browser_skill_tab_index_required",
                    )
                listed = await self._run_json(
                    ["tab", "list", "--session", session_id, "--scope", "agent"],
                    timeout_seconds=8.0,
                )
                tabs = listed.get("tabs")
                if not isinstance(tabs, list) or index >= len(tabs):
                    return self._error(
                        "BrowserSkill tab index is out of range.",
                        "browser_skill_tab_index_out_of_range",
                    )
                selected_id = self._tab_id(str(tabs[index].get("tab_id") or ""))
                if selected_id is None:
                    return self._error(
                        "BrowserSkill returned a tab without a numeric id.",
                        "browser_skill_tab_id_required",
                    )
                return ["tab", tab_action, str(selected_id), "--session", session_id]
            scope = str(action.metadata.get("scope") or "agent").lower()
            if scope not in {"agent", "user", "all"}:
                scope = "agent"
            return ["tab", "list", "--session", session_id, "--scope", scope]
        if canonical in {
            "close",
            "select_tab",
            "close_tab",
            "tab_borrow",
            "tab_return",
        }:
            if tab_id is None:
                tab_id = self._tab_id(str(action.metadata.get("tab_id") or ""))
            if tab_id is None:
                return self._error(
                    "A numeric BrowserSkill tab id is required.",
                    "browser_skill_tab_id_required",
                )
            subcommand = {
                "close": "close",
                "select_tab": "select",
                "close_tab": "close",
                "tab_borrow": "borrow",
                "tab_return": "return",
            }[canonical]
            return ["tab", subcommand, str(tab_id), "--session", session_id]
        if canonical == "navigate_back":
            return ["navigate-back", "--session", session_id, *tab_args]
        if canonical == "navigate_forward":
            return ["navigate-forward", "--session", session_id, *tab_args]
        if canonical == "reload":
            return ["reload", "--session", session_id, *tab_args]
        if canonical == "wait_for":
            wait_seconds = float(action.metadata.get("wait_time") or 0)
            if wait_seconds <= 0:
                return self._error(
                    "BrowserSkill wait requires wait_time.",
                    "browser_wait_time_required",
                )
            return ["wait-ms", f"{int(min(wait_seconds, 300) * 1000)}ms"]
        if canonical == "console_list":
            return ["console", "--session", session_id, *tab_args]
        if canonical == "network_list":
            return ["network", "--session", session_id, *tab_args]
        if canonical == "request_help":
            prompt = str(action.text or action.reason or "请完成当前页面中的人工步骤。")
            command = [
                "request-help",
                "--session",
                session_id,
                "--prompt",
                prompt,
                *tab_args,
            ]
            if action.ref or action.selector:
                command.extend(["--target", self._target(action)])
            timeout = int(action.metadata.get("timeout_seconds") or 300)
            command.extend(["--timeout", f"{max(10, min(timeout, 300))}s"])
            return command
        return self._error(
            f"BrowserSkill does not support action '{canonical}'.",
            "browser_skill_action_unsupported",
        )

    async def _attach_tab_state(
        self,
        payload: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        """Attach the post-action URL so Broker policy can inspect redirects."""
        tab_id = self._tab_id(str(payload.get("tab_id") or ""))
        if tab_id is None:
            return payload
        listed = await self._run_json(
            ["tab", "list", "--session", session_id, "--scope", "agent"],
            timeout_seconds=8.0,
        )
        tabs = listed.get("tabs")
        if not isinstance(tabs, list):
            return payload
        for tab in tabs:
            if self._tab_id(str(tab.get("tab_id") or "")) != tab_id:
                continue
            url = str(tab.get("url") or "").strip()
            if url:
                payload.setdefault("url", url)
                payload.setdefault("final_url", url)
            title = str(tab.get("title") or "").strip()
            if title:
                payload.setdefault("title", title)
            break
        return payload

    async def _run_json(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        effect_unknown_on_timeout: bool = False,
    ) -> dict[str, Any]:
        if self.cli_path is None:
            return self._error(
                "BrowserSkill CLI is not installed.",
                "browser_skill_not_installed",
            )
        command = [str(self.cli_path), "--json", *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1.0, timeout_seconds),
            )
        except asyncio.TimeoutError:
            await self._terminate_process(process)
            payload = self._error(
                "BrowserSkill command timed out.",
                "browser_skill_timeout",
            )
            if effect_unknown_on_timeout:
                payload["effect_state"] = "unknown"
            return payload
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        except OSError as exc:
            return self._error(
                f"BrowserSkill CLI could not start: {exc}",
                "browser_skill_cli_start_failed",
            )

        raw_stdout = stdout[:_MAX_CLI_OUTPUT_BYTES].decode("utf-8", "replace").strip()
        raw_stderr = stderr[:_MAX_CLI_OUTPUT_BYTES].decode("utf-8", "replace").strip()
        payload = self._parse_json(raw_stdout) or self._parse_json(raw_stderr)
        if process.returncode != 0:
            if not isinstance(payload, dict):
                payload = {}
            code = str(payload.get("code") or "browser_skill_cli_error")
            message = str(payload.get("message") or raw_stderr or raw_stdout or code)
            return {
                **payload,
                "ok": False,
                "error": message,
                "error_code": code,
                "exit_code": process.returncode,
                "runtime": "browser_skill",
            }
        if not isinstance(payload, dict):
            return self._error(
                "BrowserSkill returned invalid JSON.",
                "browser_skill_invalid_json",
            )
        payload.setdefault("ok", True)
        payload.setdefault("runtime", "browser_skill")
        return payload

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        """Give bsk time to forward cancellation, then force local cleanup."""
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        await process.communicate()

    def _action_timeout(self, action: BrowserAction) -> float:
        raw = action.metadata.get(
            "browser_task_action_timeout_seconds",
            action.metadata.get("timeout_seconds"),
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = self.timeout_seconds
        return max(1.0, min(value, 300.0))

    @staticmethod
    def _parse_json(value: str) -> dict[str, Any] | None:
        if not value:
            return None
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _target(action: BrowserAction) -> str:
        return str(action.ref or action.selector or "").strip()

    @staticmethod
    def _tab_id(value: str) -> int | None:
        raw = str(value or "").strip()
        if raw in {"", "default"}:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    def _resolve_output_path(value: str) -> Path:
        workspace = get_current_workspace_dir() or WORKING_DIR
        root = (Path(workspace) / "browser").resolve(strict=False)
        candidate = Path(value).expanduser()
        resolved = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (root / candidate).resolve(strict=False)
        )
        if resolved != root and root not in resolved.parents:
            raise ValueError("browser output path must stay inside workspace/browser")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    @staticmethod
    def _error(message: str, code: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": message,
            "error_code": code,
            "runtime": "browser_skill",
        }

    @staticmethod
    def _resolve_cli_path(value: str | Path | None) -> Path | None:
        configured = str(value or os.getenv("LANTAI_BROWSER_SKILL_CLI") or "").strip()
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured).expanduser())
        discovered = shutil.which("bsk")
        if discovered:
            candidates.append(Path(discovered))
        candidates.extend(
            [
                Path.home() / ".local" / "bin" / "bsk",
                Path("/opt/homebrew/bin/bsk"),
                Path("/usr/local/bin/bsk"),
            ]
        )
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_file() and os.access(resolved, os.X_OK):
                return resolved
        return None


_default_browser_skill_client: BrowserSkillClient | None = None


def get_browser_skill_client() -> BrowserSkillClient:
    """Return the process-wide client so atomic tool calls share sessions."""
    global _default_browser_skill_client
    if _default_browser_skill_client is None:
        _default_browser_skill_client = BrowserSkillClient()
    return _default_browser_skill_client


__all__ = [
    "BROWSER_SKILL_REPOSITORY",
    "BROWSER_SKILL_UPSTREAM_COMMIT",
    "BrowserSkillClient",
    "get_browser_skill_client",
]
