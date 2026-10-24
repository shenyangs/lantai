"""Prompt projection for chats explicitly bound to one knowledge source."""

from __future__ import annotations

from typing import Any


def build_knowledge_source_prompt(
    request_context: dict[str, Any] | None,
) -> str:
    context = request_context or {}
    raw_references = context.get("knowledge_sources")
    references = (
        [item for item in raw_references if isinstance(item, dict)]
        if isinstance(raw_references, list)
        else []
    )
    if not references and isinstance(context.get("knowledge_source"), dict):
        references = [context["knowledge_source"]]
    normalized = [
        {
            "filename": str(item.get("filename") or "").strip(),
            "path": str(item.get("path") or "").strip(),
        }
        for item in references[:20]
    ]
    normalized = [item for item in normalized if item["filename"] and item["path"]]
    if not normalized:
        return ""
    evidence = str(context.get("knowledge_source_context") or "").strip()
    exact_scope = str(context.get("knowledge_scope_mode") or "exact") == "exact"
    parts = [
        "# 当前对话已选择资料",
        *[
            f"- 资料：{item['filename']}（{item['path']}）"
            for item in normalized
        ],
        (
            "用户要求仅依据这些资料回答。若资料中找不到，直接说明，不得用未选择的知识库内容或常识补全事实。"
            if exact_scope
            else "优先依据这些资料回答；如需补充其他信息，必须明确区分资料证据与补充内容。"
        ),
        "对表格类资料，回答金额、名称或数量时必须保留工作表名和行号/单元格范围；若同名记录存在多个值，展示差异而不要猜测。",
        "回答中的资料事实必须就近附上 Markdown 引用链接，链接只能复制证据中系统提供的 Citation URL，格式为 `[资料名](Citation URL)`；不得编造链接或位置。",
    ]
    if evidence:
        parts.extend(
            [
                "\n以下是根据当前用户问题从该资料中召回的证据：",
                evidence,
            ]
        )
    else:
        parts.append("在回答具体事实前，先检索所选资料，必要时读取原文核对。")
    return "\n".join(parts)
