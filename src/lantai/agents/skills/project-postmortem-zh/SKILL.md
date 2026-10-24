---
# id: project-postmortem (migrated to name)
name: project-postmortem
description: 在项目、发布或重大任务结束后复盘目标、时间线、决策、偏差、事故、有效做法和可复用资产，并形成经确认的知识沉淀。用户要求“项目复盘/事后总结/沉淀经验/发布回顾”时使用；进行中的状态汇报、甩锅式追责或未经授权写入长期记忆时不要触发。
kind: project_memory
domain: knowledge_base
version: 1.0.0
source: builtin
enabled: true
locked: false
permission_profile: memory_write_with_approval
recommended_agents:
- latte
- cola
- coconut
inputs:
- final_outputs
- user_feedback
- trace_events
outputs:
- reusable_skill
- memory_candidates
- failure_modes
memory_writeback:
- accepted_phrasing
- rejected_phrasing
- workflow_boundary
- user_preference
tags:
- 复盘
- Skill沉淀
- 知识库
---

# 项目复盘沉淀

适用场景：项目结束后，需要把有效流程、用户修改、失败模式和可复用模板沉淀成底座时触发。
输出格式：Project Memory Skill + 记忆候选 + 知识库条目

## 子 Agent 分工
- 拿铁提炼流程骨架。
- 可乐记录表达层面被采纳和被打回的句式。
- 椰汁整理事实边界和待核习惯。

## WorkflowGraph 节点
- collect_outputs
- mine_trace
- draft_skill
- extract_memory_candidates
- approval_gate
- save_skill

## 人工确认点
- 写入长期记忆和团队共享前必须确认。

## 失败模式
- 没有成功样本时只保存复盘，不生成标准 Skill。
