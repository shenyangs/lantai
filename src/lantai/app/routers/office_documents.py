# -*- coding: utf-8 -*-
"""Office document capability APIs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from ...utils.async_settlement import run_sync_settled
from ..office_documents import (
    OfficeCliConfig,
    compare_office_documents,
    get_officecli_status,
    preview_office_document,
    update_officecli_config,
)

router = APIRouter(prefix="/office-documents", tags=["office-documents"])


class OfficeCliConfigUpdate(BaseModel):
    """Office document capability preference patch."""

    enabled: bool | None = Field(
        default=None,
        description="Whether Office document capability is enabled",
    )
    officecli_path: str | None = Field(
        default=None,
        description="Optional custom OfficeCLI executable path",
    )


class OfficeDocumentPreviewRequest(BaseModel):
    """Preview a local Office document."""

    file_path: str = Field(..., description="Absolute path to an Office file")
    mode: Literal["text", "outline", "annotated", "stats", "issues", "html"] = (
        "text"
    )


class OfficeDocumentCompareRequest(BaseModel):
    """Compare an original Office document with a modified version."""

    before_file_path: str = Field(..., description="Original Office file path")
    after_file_path: str = Field(..., description="Modified Office file path")


@router.get("/status", response_model=dict, summary="Get OfficeCLI status")
async def get_status() -> dict:
    """Return detected OfficeCLI state and integration guidance."""

    return await run_sync_settled(lambda: get_officecli_status().to_dict())


@router.put("/config", response_model=dict, summary="Update OfficeCLI config")
async def put_config(body: OfficeCliConfigUpdate = Body(...)) -> dict:
    """Persist Office document capability preferences."""

    def update_and_get() -> dict:
        update_officecli_config(
            enabled=body.enabled,
            officecli_path=body.officecli_path,
        )
        return get_officecli_status().to_dict()

    return await run_sync_settled(update_and_get)


@router.post("/preview", response_model=dict, summary="Preview Office file")
async def post_preview(body: OfficeDocumentPreviewRequest = Body(...)) -> dict:
    """Render/extract a local Office document for workspace preview."""

    try:
        return await run_sync_settled(
            lambda: preview_office_document(
                body.file_path,
                mode=body.mode,
            ).to_dict()
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/compare", response_model=dict, summary="Compare Office files")
async def post_compare(body: OfficeDocumentCompareRequest = Body(...)) -> dict:
    """Compare two local Office documents for Agent modification review."""

    try:
        return await run_sync_settled(
            lambda: compare_office_documents(
                body.before_file_path,
                body.after_file_path,
            ).to_dict()
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/templates", response_model=dict, summary="Office task templates")
async def get_templates() -> dict:
    """Return product-level Office workflow templates for Task Center."""

    return {
        "templates": [
            {
                "id": "weekly-report-docx",
                "title": "生成周报 Word",
                "description": "汇总本周聊天、任务和 Wiki 资料，生成可继续编辑的周报。",
                "prompt": (
                    "请基于当前工作区资料生成一份本周工作周报。"
                    "输出 Word 文档，包含：本周进展、关键风险、下周计划、"
                    "需要我确认的问题。优先使用 Office 文档能力/OfficeCLI，"
                    "生成后给出文件路径和可修改建议。"
                ),
            },
            {
                "id": "sales-table-xlsx",
                "title": "生成销售 Excel",
                "description": "把零散销售数据整理成表格、公式和可检查的汇总页。",
                "prompt": (
                    "请把我提供的销售数据整理成 Excel 工作簿。"
                    "需要原始数据页、汇总页、核心指标、基础图表和异常说明；"
                    "请用 Office 文档能力/OfficeCLI 创建或修改 xlsx，并说明计算逻辑。"
                ),
            },
            {
                "id": "briefing-deck-pptx",
                "title": "生成汇报 PPT",
                "description": "从资料和结论生成结构清晰、可预览和可迭代的 PPT。",
                "prompt": (
                    "请基于当前资料生成一份汇报 PPT。"
                    "先给出 8-12 页大纲，再用 Office 文档能力/OfficeCLI 创建 pptx；"
                    "生成后预览检查标题溢出、层级和版式一致性，并给我修改点。"
                ),
            },
        ]
    }
