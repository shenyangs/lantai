"""Bridge ACP sessions to Lantai's authoritative local Agent API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ...agents.tools.agent_management import (
    create_async_agent_api_client,
    extract_agent_text_content,
)
from ...constant import SECRET_DIR
from ...security.secret_store import decrypt, encrypt
from ...utils.async_settlement import await_settled


ACP_SESSION_COOKIE = "lantai_browser_session"
ACP_SESSION_STORE_VERSION = 1
DEFAULT_ACP_SESSION_STORE = SECRET_DIR / "acp-browser-sessions.json"
logger = logging.getLogger(__name__)


class AcpBridgeError(RuntimeError):
    """A user-visible failure while bridging an ACP request to Lantai."""


class AcpSessionStore:
    """Persist desktop pairing sessions encrypted with Lantai's secret key."""

    def __init__(
        self,
        path: Path = DEFAULT_ACP_SESSION_STORE,
        *,
        encrypt_value: Callable[[str], str] = encrypt,
        decrypt_value: Callable[[str], str] = decrypt,
    ) -> None:
        self.path = path
        self._encrypt_value = encrypt_value
        self._decrypt_value = decrypt_value

    def _read_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"version": ACP_SESSION_STORE_VERSION, "sessions": {}}
        if not isinstance(payload, dict):
            return {"version": ACP_SESSION_STORE_VERSION, "sessions": {}}
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        return {"version": ACP_SESSION_STORE_VERSION, "sessions": sessions}

    def load(self, api_origin: str) -> str | None:
        record = self._read_payload()["sessions"].get(api_origin)
        if not isinstance(record, dict):
            return None
        try:
            expires_at = int(record.get("expires_at") or 0)
        except (TypeError, ValueError):
            return None
        if expires_at <= int(time.time()):
            return None
        ciphertext = record.get("session")
        if not isinstance(ciphertext, str) or not ciphertext:
            return None
        try:
            token = self._decrypt_value(ciphertext)
        except Exception:
            return None
        return token or None

    def save(self, api_origin: str, token: str, expires_at: int) -> None:
        payload = self._read_payload()
        payload["sessions"][api_origin] = {
            "session": self._encrypt_value(token),
            "expires_at": int(expires_at),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.tmp",
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


@dataclass(frozen=True)
class AcpWorkspaceGrant:
    """The existing Lantai workspace authority bound to an ACP session."""

    grant_id: str
    project_root: Path
    access_mode: str
    trust_state: str


@dataclass(frozen=True)
class AcpPromptResult:
    """Final text and stop state for one bridged prompt."""

    text: str
    stop_reason: str


@dataclass
class _AcpActiveTurn:
    """ACP transport identity; the backend remains the execution owner."""

    submission_key: str
    task_id: str | None = None
    cancel_requested: bool = False
    submission_in_flight: bool = True


class LantaiAcpBridge:
    """Use Lantai's background task API without creating a second runtime."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        agent_id: str = "default",
        task_timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 0.2,
        client: httpx.AsyncClient | None = None,
        pairing_code: str | None = None,
        session_store: AcpSessionStore | None = None,
    ) -> None:
        self.agent_id = agent_id.strip() or "default"
        self.task_timeout_seconds = min(
            3600.0,
            max(60.0, float(task_timeout_seconds)),
        )
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self._client = client or create_async_agent_api_client(base_url)
        self._owns_client = client is None
        self._pairing_code = (pairing_code or "").strip()
        self._api_origin = self._resolve_api_origin()
        self._session_store = (
            session_store
            if session_store is not None
            else (AcpSessionStore() if client is None else None)
        )
        if self._session_store is not None:
            cached_session = self._session_store.load(self._api_origin)
            if cached_session:
                self._client.cookies.set(
                    ACP_SESSION_COOKIE,
                    cached_session,
                )
        self._active_tasks: dict[str, _AcpActiveTurn] = {}
        self._active_tasks_lock = asyncio.Lock()
        self._prompt_tasks: dict[str, asyncio.Task[Any]] = {}
        self._settlement_tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    def _resolve_api_origin(self) -> str:
        parsed = urlsplit(str(self._client.base_url))
        if not parsed.scheme or not parsed.hostname:
            raise AcpBridgeError("Lantai ACP requires an absolute API URL.")
        host = parsed.hostname
        authority = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            authority = f"{authority}:{parsed.port}"
        return f"{parsed.scheme.lower()}://{authority.lower()}"

    def _is_loopback_origin(self) -> bool:
        parsed = urlsplit(self._api_origin)
        return (parsed.hostname or "").lower() in {
            "127.0.0.1",
            "::1",
            "localhost",
        }

    async def aclose(self) -> None:
        # SDK EOF cancels request handlers before their POST receipts may
        # arrive. The bridge still owns the resulting backend settlement.
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned_work())
        try:
            await await_settled(self._close_task)
        except BaseException:
            if self._close_task.done() and not self._close_task.cancelled():
                if self._close_task.exception() is not None:
                    self._close_task = None
            raise

    async def _close_owned_work(self) -> None:
        prompts = tuple(self._prompt_tasks.values())
        for task in prompts:
            if not task.done():
                task.cancel()
        if prompts:
            await asyncio.gather(*prompts, return_exceptions=True)
        while self._settlement_tasks:
            await asyncio.gather(*tuple(self._settlement_tasks), return_exceptions=True)
        # A prior polling or receipt failure can leave a retained turn after
        # its request handler has ended. Resolve that same identity as well.
        results = await asyncio.gather(
            *(self.cancel(session_id) for session_id in tuple(self._active_tasks)),
            return_exceptions=True,
        )
        if self._active_tasks:
            error = next((item for item in results if isinstance(item, BaseException)), None)
            raise AcpBridgeError(
                "ACP shutdown could not confirm backend settlement; retained "
                "turn identities and the HTTP client remain available for cancellation."
            ) from error
        if self._owns_client:
            await self._client.aclose()

    def _track_settlement(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._settlement_tasks.add(task)
        task.add_done_callback(self._settlement_tasks.discard)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        allow_pairing_retry: bool = True,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                headers={"X-Agent-Id": self.agent_id},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            detail: object = None
            code = ""
            try:
                payload = exc.response.json()
                if isinstance(payload, dict):
                    detail = payload.get("detail")
                    code = str(payload.get("code") or "")
                else:
                    detail = payload
            except ValueError:
                detail = None
            if (
                allow_pairing_retry
                and exc.response.status_code == 403
                and code == "desktop_token_required"
            ):
                if self._pairing_code:
                    await self.pair(self._pairing_code)
                    self._pairing_code = ""
                    return await self._request_json(
                        method,
                        path,
                        json_body=json_body,
                        allow_pairing_retry=False,
                    )
                raise AcpBridgeError(
                    "The packaged Lantai desktop API requires local pairing. "
                    "In Lantai, open Settings > Browser Connection and generate a "
                    "code, then run `lantai acp pair <code>` once before "
                    "starting the IDE agent.",
                ) from exc
            message = str(detail or exc.response.text or exc)
            raise AcpBridgeError(
                f"Lantai API rejected {method} {path}: {message}",
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AcpBridgeError(
                "Cannot reach the running Lantai service. Open Lantai first and "
                f"verify its local API, then retry. Details: {exc}",
            ) from exc

    async def pair(self, code: str) -> int:
        """Exchange a one-time desktop code and cache its encrypted session."""
        normalized_code = code.strip()
        if not normalized_code:
            raise AcpBridgeError("A non-empty Lantai pairing code is required.")
        if not self._is_loopback_origin():
            raise AcpBridgeError(
                "ACP pairing codes may only be sent to a loopback Lantai API.",
            )
        try:
            self._client.cookies.delete(ACP_SESSION_COOKIE)
            response = await self._client.post(
                "/browser-pairing/complete",
                json={"code": normalized_code},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail") or "")
            except ValueError:
                pass
            raise AcpBridgeError(
                "Lantai rejected the pairing code: "
                f"{detail or exc.response.text or exc}",
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AcpBridgeError(
                f"Cannot complete Lantai ACP pairing: {exc}",
            ) from exc

        if not isinstance(payload, dict) or payload.get("paired") is not True:
            raise AcpBridgeError("Lantai returned an invalid pairing response.")
        session_token = response.cookies.get(ACP_SESSION_COOKIE)
        if not session_token:
            raise AcpBridgeError("Lantai did not return a pairing session.")
        try:
            expires_at = int(payload.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise AcpBridgeError(
                "Lantai returned an invalid pairing expiry.",
            ) from exc
        if expires_at <= int(time.time()):
            raise AcpBridgeError("Lantai returned an expired pairing session.")
        if self._session_store is not None:
            try:
                self._session_store.save(
                    self._api_origin,
                    session_token,
                    expires_at,
                )
            except Exception as exc:
                raise AcpBridgeError(
                    "Lantai paired successfully but could not securely cache "
                    f"the local session: {exc}",
                ) from exc
        return expires_at

    async def require_workspace_grant(self, cwd: str) -> AcpWorkspaceGrant:
        """Resolve an IDE cwd only through an existing explicit Lantai grant."""
        try:
            requested_root = Path(cwd).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AcpBridgeError(f"ACP workspace does not exist: {cwd}") from exc
        if not requested_root.is_dir():
            raise AcpBridgeError(f"ACP workspace is not a directory: {cwd}")

        payload = await self._request_json("GET", "/workspace-grants")
        if not isinstance(payload, list):
            raise AcpBridgeError("Lantai returned an invalid workspace grant list.")
        for item in payload:
            if not isinstance(item, dict) or item.get("status") != "active":
                continue
            raw_root = item.get("project_root")
            if not isinstance(raw_root, str):
                continue
            try:
                granted_root = Path(raw_root).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if granted_root != requested_root:
                continue
            grant_id = str(item.get("id") or "").strip()
            if not grant_id:
                continue
            return AcpWorkspaceGrant(
                grant_id=grant_id,
                project_root=granted_root,
                access_mode=str(item.get("access_mode") or "read_write"),
                trust_state=str(item.get("trust_state") or "restricted"),
            )

        raise AcpBridgeError(
            "This IDE workspace has not been authorized in Lantai: "
            f"{requested_root}. Select this folder once in Lantai, then create "
            "the IDE agent session again.",
        )

    async def _reserve_session(self, session_id: str) -> _AcpActiveTurn:
        turn = _AcpActiveTurn(submission_key=f"acp-{uuid4().hex}")
        async with self._active_tasks_lock:
            if self._closing:
                raise AcpBridgeError("The ACP bridge is closing.")
            if session_id in self._active_tasks:
                raise AcpBridgeError(
                    "This ACP session already has a running Lantai turn.",
                )
            self._active_tasks[session_id] = turn
            task = asyncio.current_task()
            if task is not None:
                self._prompt_tasks[session_id] = task
        return turn

    async def _bind_task(
        self,
        session_id: str,
        turn: _AcpActiveTurn,
        task_id: str,
    ) -> bool:
        async with self._active_tasks_lock:
            if self._active_tasks.get(session_id) is not turn:
                raise AcpBridgeError(
                    "The ACP session lost its pending Lantai turn reservation.",
                )
            if turn.task_id is not None and turn.task_id != task_id:
                raise AcpBridgeError("The ACP task identity changed unexpectedly.")
            turn.task_id = task_id
            turn.submission_in_flight = False
            return turn.cancel_requested

    async def _unbind_task(self, session_id: str, turn: _AcpActiveTurn) -> None:
        async with self._active_tasks_lock:
            if self._active_tasks.get(session_id) is turn:
                self._active_tasks.pop(session_id, None)

    async def _lookup_submission(self, turn: _AcpActiveTurn) -> str | None:
        payload = await self._request_json(
            "GET",
            f"/agent/process/task/submission/{turn.submission_key}",
        )
        if not isinstance(payload, dict):
            raise AcpBridgeError("Lantai returned an invalid submission lookup.")
        if payload.get("status") == "not_found":
            return None
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise AcpBridgeError("Lantai returned an invalid submission lookup.")
        return task_id.strip()

    async def _submission_task_id(
        self,
        session_id: str,
        turn: _AcpActiveTurn,
        submission: asyncio.Task[Any],
    ) -> str:
        try:
            submitted = await submission
        except AcpBridgeError as exc:
            rejection = exc.__cause__
            if (
                isinstance(rejection, httpx.HTTPStatusError)
                and rejection.response.status_code in {400, 401, 403, 404, 422}
            ):
                # These responses reject admission before a task is created.
                await self._unbind_task(session_id, turn)
                raise
            # The POST may have been accepted before its response was lost.
            # A read by the same key is safe; replaying the POST is not.
            try:
                task_id = await self._lookup_submission(turn)
            except AcpBridgeError as lookup_error:
                raise AcpBridgeError(
                    "Lantai submission outcome is unknown; keep this ACP "
                    "session reserved and retry cancellation after reconnecting.",
                ) from lookup_error
            if task_id is None:
                raise AcpBridgeError(
                    "Lantai submission outcome is unknown; no second task "
                    "was submitted.",
                ) from exc
            return task_id

        task_id = (
            submitted.get("task_id")
            if isinstance(submitted, dict)
            else None
        )
        if isinstance(task_id, str) and task_id.strip():
            return task_id.strip()
        # A successful but malformed receipt is checked against the backend
        # identity map before releasing its reservation.
        found = await self._lookup_submission(turn)
        if found is not None:
            return found
        await self._unbind_task(session_id, turn)
        raise AcpBridgeError("Lantai did not return a task identity.")

    async def _cancel_bound_task(
        self,
        session_id: str,
        turn: _AcpActiveTurn,
    ) -> bool:
        task_id = turn.task_id
        if task_id is None:
            raise AcpBridgeError("The ACP task has no backend identity yet.")
        payload = await self._request_json(
            "POST",
            f"/agent/process/task/{task_id}/cancel",
        )
        if not isinstance(payload, dict):
            raise AcpBridgeError("Lantai returned an invalid cancellation status.")
        status = str(payload.get("status") or "").strip()
        if status not in {"cancelled", "finished", "failed"}:
            raise AcpBridgeError(
                f"Lantai could not confirm task cancellation: {status or 'empty'}",
            )
        await self._unbind_task(session_id, turn)
        return status == "cancelled"

    async def _settle_cancelled_submission(
        self,
        session_id: str,
        turn: _AcpActiveTurn,
        submission: asyncio.Task[Any],
    ) -> None:
        try:
            task_id = await self._submission_task_id(session_id, turn, submission)
            await self._bind_task(session_id, turn, task_id)
            await self._cancel_bound_task(session_id, turn)
        except Exception:
            logger.exception(
                "ACP cancelled prompt could not confirm backend settlement "
                "for submission %s",
                turn.submission_key,
            )
        finally:
            turn.submission_in_flight = False

    async def _settle_cancelled_bound_task(
        self,
        session_id: str,
        turn: _AcpActiveTurn,
    ) -> None:
        try:
            await self._cancel_bound_task(session_id, turn)
        except Exception:
            logger.exception(
                "ACP cancelled prompt could not confirm backend task %s",
                turn.task_id,
            )

    async def run_prompt(
        self,
        *,
        session_id: str,
        text: str,
        workspace: AcpWorkspaceGrant,
    ) -> AcpPromptResult:
        """Run one prompt through Lantai and retain its exact cancellation ID."""
        turn = await self._reserve_session(session_id)
        request = {
            "session_id": session_id,
            "user_id": f"acp:{session_id}",
            "channel": "acp",
            "channel_meta": {
                "workspace_grant_id": workspace.grant_id,
                "coding_mode": True,
                "coding_context": (
                    "ACP IDE session rooted at " f"{workspace.project_root}"
                ),
            },
            "metadata": {
                "presentation_context": "acp",
                "agent_task_timeout_seconds": self.task_timeout_seconds,
                "background_task_idempotency_key": turn.submission_key,
            },
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            ],
        }
        submission: asyncio.Task[Any] | None = None
        terminal = False
        detached_settlement = False

        try:
            submission = asyncio.create_task(
                self._request_json(
                    "POST",
                    "/agent/process/task",
                    json_body=request,
                ),
            )
            try:
                await asyncio.shield(submission)
            except AcpBridgeError:
                # Resolve an ambiguous POST from the existing submission key.
                pass
            task_id = await self._submission_task_id(session_id, turn, submission)
            if await self._bind_task(session_id, turn, task_id):
                if await self._cancel_bound_task(session_id, turn):
                    terminal = True
                    return AcpPromptResult(text="", stop_reason="cancelled")
            while True:
                status_payload = await self._request_json(
                    "GET",
                    f"/agent/process/task/{task_id}",
                )
                if not isinstance(status_payload, dict):
                    raise AcpBridgeError("Lantai returned an invalid task status.")
                status = str(status_payload.get("status") or "").strip()
                if status in {"submitted", "running"}:
                    await asyncio.sleep(self.poll_interval_seconds)
                    continue
                if status == "finished":
                    terminal = True
                    result = status_payload.get("result")
                    text_output = (
                        extract_agent_text_content(result)
                        if isinstance(result, dict)
                        else ""
                    )
                    if not text_output:
                        raise AcpBridgeError(
                            "Lantai finished the turn without a visible assistant reply.",
                        )
                    return AcpPromptResult(
                        text=text_output,
                        stop_reason="end_turn",
                    )
                if status == "cancelled":
                    terminal = True
                    return AcpPromptResult(text="", stop_reason="cancelled")
                if status == "failed":
                    terminal = True
                    error = status_payload.get("error")
                    message = (
                        error.get("message")
                        if isinstance(error, dict)
                        else error
                    )
                    raise AcpBridgeError(
                        f"Lantai failed this ACP turn: {message or 'unknown error'}",
                    )
                raise AcpBridgeError(
                    f"Lantai returned an unknown task status: {status or 'empty'}",
                )
        except asyncio.CancelledError:
            turn.cancel_requested = True
            if turn.task_id is not None:
                self._track_settlement(
                    self._settle_cancelled_bound_task(session_id, turn),
                )
            elif submission is not None:
                detached_settlement = True
                self._track_settlement(
                    self._settle_cancelled_submission(
                        session_id,
                        turn,
                        submission,
                    ),
                )
            raise
        finally:
            if not detached_settlement:
                turn.submission_in_flight = False
            if terminal:
                await self._unbind_task(session_id, turn)
            if self._prompt_tasks.get(session_id) is asyncio.current_task():
                self._prompt_tasks.pop(session_id, None)

    async def cancel(self, session_id: str) -> bool:
        """Cancel the authoritative task currently bound to an ACP session."""
        async with self._active_tasks_lock:
            turn = self._active_tasks.get(session_id)
            if turn is None:
                return False
            turn.cancel_requested = True
            if turn.task_id is None and turn.submission_in_flight:
                return True
            task_id = turn.task_id
        if task_id is None:
            task_id = await self._lookup_submission(turn)
            if task_id is None:
                raise AcpBridgeError(
                    "Lantai has not confirmed this submission; the ACP "
                    "session remains reserved for later cancellation.",
                )
            await self._bind_task(session_id, turn, task_id)
        return await self._cancel_bound_task(session_id, turn)
