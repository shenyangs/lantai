# -*- coding: utf-8 -*-
"""Browser pairing endpoints for the desktop loopback backend."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..browser_pairing import (
    BROWSER_SESSION_COOKIE,
    browser_pairing_manager,
    get_browser_session_token,
    has_valid_browser_session,
    is_loopback_request,
)

router = APIRouter(prefix="/browser-pairing", tags=["browser-pairing"])


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: int
    expires_in_seconds: int


class PairingCompleteBody(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class PairingStatusResponse(BaseModel):
    paired: bool


class PairingCompleteResponse(PairingStatusResponse):
    expires_at: int
    expires_in_seconds: int


@router.get("/status", response_model=PairingStatusResponse)
async def get_browser_pairing_status(request: Request) -> PairingStatusResponse:
    return PairingStatusResponse(paired=has_valid_browser_session(request))


@router.post("/code", response_model=PairingCodeResponse)
async def create_browser_pairing_code(
    request: Request,
) -> PairingCodeResponse:
    """Create a short-lived code.

    The desktop-token middleware protects this endpoint in desktop mode, so a
    normal browser cannot mint its own code.
    """

    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Loopback only")

    code = browser_pairing_manager.create_code()
    return PairingCodeResponse(
        code=code.code,
        expires_at=code.expires_at,
        expires_in_seconds=code.expires_in_seconds,
    )


@router.post("/complete", response_model=PairingCompleteResponse)
async def complete_browser_pairing(
    body: PairingCompleteBody,
    request: Request,
    response: Response,
) -> PairingCompleteResponse:
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Loopback only")

    session = browser_pairing_manager.consume_code(body.code)
    if session is None:
        raise HTTPException(status_code=400, detail="配对码无效或已过期")

    response.set_cookie(
        key=BROWSER_SESSION_COOKIE,
        value=session.token,
        max_age=session.expires_in_seconds,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )
    return PairingCompleteResponse(
        paired=True,
        expires_at=session.expires_at,
        expires_in_seconds=session.expires_in_seconds,
    )


@router.post("/logout", response_model=PairingStatusResponse)
async def logout_browser_pairing(
    request: Request,
    response: Response,
) -> PairingStatusResponse:
    browser_pairing_manager.revoke_session(get_browser_session_token(request))
    response.delete_cookie(
        key=BROWSER_SESSION_COOKIE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )
    return PairingStatusResponse(paired=False)
