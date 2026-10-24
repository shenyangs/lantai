# -*- coding: utf-8 -*-
"""Local control plane for the optional Lantai cloud account."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..cloud_account import CloudAccountError, cloud_account_service

router = APIRouter(prefix="/cloud-account", tags=["cloud-account"])


def _raise_http(error: CloudAccountError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error)},
    ) from error


@router.get("/status")
async def cloud_account_status():
    try:
        return await cloud_account_service.status()
    except CloudAccountError as error:
        _raise_http(error)


@router.post("/watcha/start")
async def start_watcha_login(request: Request):
    desktop_redirect_uri = (
        str(request.base_url).rstrip("/")
        + "/api/cloud-account/watcha/callback"
    )
    try:
        return await cloud_account_service.start_watcha(
            desktop_redirect_uri=desktop_redirect_uri,
        )
    except CloudAccountError as error:
        _raise_http(error)


@router.post("/watcha/poll")
async def poll_watcha_login(payload: dict = Body(...)):
    attempt_id = str(payload.get("attempt_id") or "").strip()
    if not attempt_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "attempt_id_missing", "message": "登录请求参数不完整。"},
        )
    try:
        return await cloud_account_service.poll_watcha(attempt_id)
    except CloudAccountError as error:
        _raise_http(error)


@router.post("/watcha/cancel")
async def cancel_watcha_login(payload: dict = Body(...)):
    attempt_id = str(payload.get("attempt_id") or "").strip()
    if not attempt_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "attempt_id_missing", "message": "登录请求参数不完整。"},
        )
    return cloud_account_service.cancel_watcha(attempt_id)


@router.get("/watcha/callback", response_class=HTMLResponse)
async def complete_watcha_login(
    attempt: str = "",
    ticket: str = "",
    error: str = "",
):
    try:
        success = await cloud_account_service.complete_watcha(
            attempt_id=attempt.strip(),
            ticket=ticket.strip(),
            error=error.strip(),
        )
    except CloudAccountError:
        success = False
    title = "登录完成" if success else "登录未完成"
    description = (
        "观猹身份已连接，可以关闭此页面并返回澜台。"
        if success
        else "这次登录没有完成，请关闭此页面并返回澜台重试。"
    )
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta http-equiv='Content-Security-Policy' "
        "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'\">"
        f"<title>{title}</title><style>body{{margin:0;background:#f7f5ef;"
        "color:#20201e;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',"
        "sans-serif;display:grid;min-height:100vh;place-items:center}}main{max-width:32rem;"
        "margin:2rem;padding:2.5rem;border:1px solid #d8d4c8;background:#fff;"
        "border-radius:18px}h1{font-size:1.5rem;margin:0 0 1rem}p{line-height:1.7;"
        "margin:0;color:#5d5b55}</style></head><body><main>"
        f"<h1>{title}</h1><p>{description}</p></main></body></html>",
        status_code=200 if success else 400,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/logout")
async def logout_cloud_account():
    try:
        return await cloud_account_service.logout()
    except CloudAccountError as error:
        _raise_http(error)
