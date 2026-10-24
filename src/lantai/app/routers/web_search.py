# -*- coding: utf-8 -*-
"""Configuration API for the optional backend of the unified Web tools."""

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict

from ..web_search import (
    DeepSeekWebClient,
    DeepSeekWebError,
    WebSearchStatus,
    WebSearchStore,
    get_web_search_store,
)

router = APIRouter(prefix="/web-search", tags=["web-search"])


class WebSearchUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = ""
    enabled: bool = False


def get_store() -> WebSearchStore:
    return get_web_search_store()


async def validate_api_key(api_key: str) -> bool:
    return await DeepSeekWebClient(api_key=api_key).validate()


@router.get("/config", response_model=WebSearchStatus)
async def get_config() -> WebSearchStatus:
    try:
        return get_store().status()
    except (ValueError, OSError) as exc:
        raise HTTPException(
            409, "Web search configuration could not be read."
        ) from exc


@router.put("/config", response_model=WebSearchStatus)
async def put_config(body: WebSearchUpdate = Body(...)) -> WebSearchStatus:
    store = get_store()
    supplied = str(body.api_key or "").strip()
    try:
        nonempty_verification = False
        if body.enabled or supplied:
            api_key = supplied or store.load_api_key()
            if not api_key:
                raise ValueError(
                    "Enter a separate DeepSeek Web API key first."
                )
            nonempty_verification = await validate_api_key(api_key)
        # Blank input keeps the encrypted key. Disabling never needs a paid
        # validation call, a working key, or an agent/tool-registry reload.
        status = store.save(api_key=supplied, enabled=body.enabled)
        if nonempty_verification:
            store.record_request("search")
            status = store.status()
        return status
    except DeepSeekWebError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            500, "Web search configuration could not be saved."
        ) from exc


# Old console clients may still save the former settings route during an
# upgrade. It writes the same store; no standalone tool or old transport lives
# behind this deprecated alias. Historical tool cards retain their own label.
legacy_router = APIRouter(prefix="/deepseek-quick-search", tags=["web-search"])
legacy_router.add_api_route(
    "/config",
    get_config,
    methods=["GET"],
    response_model=WebSearchStatus,
    deprecated=True,
)
legacy_router.add_api_route(
    "/config",
    put_config,
    methods=["PUT"],
    response_model=WebSearchStatus,
    deprecated=True,
)
