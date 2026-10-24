---
# id: lantai-memory-context (migrated to name)
name: lantai-memory-context
description: 管理澜台会话上下文、项目资料、长期记忆的提取、装配、解释、写回与遗忘边界。用户要求“记住/忘记这个”“为什么 Agent 知道这些”“整理上下文或迁移记忆”时使用；普通文档摘要、一次性检索或未经授权持久化敏感信息时不要触发。
kind: core_memory
domain: memory_runtime
version: 1.0.0
source: builtin
enabled: true
locked: true
permission_profile: memory_write_with_approval
recommended_agents:
- latte
- coconut
inputs:
- conversation
- project_result
- user_feedback
outputs:
- memory_candidates
- retrieved_memory
- compact_summary
memory_writeback:
- user_preference
- brand_boundary
- project_state
- failure_mode
tags:
- 澜台内置
- 记忆
- 上下文
---

# 澜台 记忆与上下文

适用场景：需要读取、搜索、更新、删除、重建用户记忆，或检索历史对话和压缩上下文时触发。
输出格式：记忆候选 + 注入摘要 + 审计记录

## 澜台 工具映射
- `memory_read` / `memory_update` / `memory_delete` / `memory_edit` / `memory_rebuild`
- `memory_store` / `memory_get` / `memory_forget` / `memory_list`
- `memory_search`：向量语义搜索记忆库。
- `chat_search`：跨对话关键词搜索历史消息。

## WorkflowGraph 节点
- extract_candidates
- dedupe
- approval_gate
- vector_index
- retrieve
- inject_context
- compact

## 权限规则
- 读取和搜索只读可自动。
- 更新、删除、重建记忆必须人工确认。
- 项目沉淀必须展示候选，不悄悄写入。

## 失败模式
- 记忆冲突时保留旧值，新增候选进入待确认。
- 长记忆不得全文堆入 prompt，只注入相关片段。
