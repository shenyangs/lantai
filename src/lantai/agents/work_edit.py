# -*- coding: utf-8 -*-
"""System-prompt support for conversations created from saved Works."""

from __future__ import annotations

from typing import Any


def trusted_private_editor_binding_from_context(
    request_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Copy the complete server-attested project binding into turn context.

    Console routing rebuilds this binding from the stored chat and project.
    The private marker and every field are required; a work-method selection
    alone does not authorize project tools or unbounded record projection.
    """

    context = request_context or {}
    if (
        context.get("_trusted_private_editor_binding") is not True
        or context.get("work_method_id") != "private_editor"
        or context.get("private_editor_project_status") != "active"
    ):
        return {}
    string_keys = (
        "private_editor_project_id",
        "private_editor_original_work_id",
        "private_editor_current_work_id",
        "private_editor_current_digest",
        "private_editor_project_status",
    )
    result: dict[str, Any] = {
        "_trusted_private_editor_binding": True,
        "work_method_id": "private_editor",
    }
    for key in string_keys:
        value = context.get(key)
        if not isinstance(value, str) or not value.strip():
            return {}
        result[key] = value.strip()
    for key in (
        "private_editor_record_revision",
        "private_editor_revocation_epoch",
    ):
        value = context.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return {}
        result[key] = value
    return result


def build_work_edit_prompt(request_context: dict[str, Any] | None) -> str:
    context = str((request_context or {}).get("work_edit_context") or "").strip()
    if not context:
        return ""
    if trusted_private_editor_binding_from_context(request_context):
        # The server-generated context contains the current read-only Works
        # location and complete author records. A generic editable-branch
        # instruction would contradict adoption, which clears that old copy.
        return f"【作品修改会话】\n{context}"
    if context.startswith("【当前会话成果修改目标】"):
        return (
            "【成果精确修改】\n"
            "这是用户在当前对话里确认提交的成果修改目标；继续沿用当前对话上下文。\n"
            f"{context[:8000]}\n"
            "- 修改前检查目标文件及相关资源，保留成果现有能力。\n"
            "- 只在已绑定的可编辑副本内操作，不得写回或删除收藏的来源版本。\n"
            "- 优先处理用户选中的页、段落或区域，同时检查修改对相邻内容的影响。\n"
            "- 完成后运行与文件类型相匹配的最小验证，并按既有版本链保存新版本。"
        )
    return (
        "【作品修改会话】\n"
        "这是用户从「作品」页面新建的独立修改会话。无需回读原会话。\n"
        f"{context[:8000]}\n"
        "- 在修改前先检查主文件及相关资源，保留作品现有能力。\n"
        "- 当前目录是独立修改分支；不得写回或删除收藏的来源版本。\n"
        "- 根据用户当前指令修改；若指令不足以判断目标，再简短询问。\n"
        "- 完成后运行与文件类型相匹配的最小验证，并说明修改内容与来源版本。"
    )
