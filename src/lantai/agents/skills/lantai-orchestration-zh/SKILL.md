---
# id: lantai-orchestration (migrated to name)
name: lantai-orchestration
description: 将复杂目标拆成可交接的 Agent 子任务，选择角色、安排依赖与并行、控制上下文、汇总证据并处理失败。用户明确要求多 Agent 协作、委派、并行调查或工作流编排时使用；单角色可直接完成的简单任务、只需聊天或未授权创建外部任务时不要触发。
kind: orchestration
domain: workflow_runtime
version: 1.0.0
source: builtin
enabled: true
locked: true
permission_profile: internal_orchestration
recommended_agents:
- latte
- cola
- coconut
- oolong
inputs:
- user_goal
- selected_skill
- available_tools
outputs:
- task_tree
- agent_reports
- workflow_trace
memory_writeback:
- workflow_boundary
- agent_failure_mode
tags:
- 澜台内置
- 子Agent
- 任务树
---

# 澜台 Agent 编排

适用场景：需要创建子 Agent、调用已注册 Skill、写入任务树、读取任务状态或进入计划模式时触发。
输出格式：任务树 + 子 Agent 分工 + Trace

## 澜台 工具映射
- `agent`：创建并运行子 Agent 执行复杂任务。
- `skill_invoke`：调用已注册的 Skill 提示词模板。
- `todo_write` / `todo_read`：创建、更新、读取会话内任务列表。
- `enter_plan_mode` / `exit_plan_mode` / `ask_user`

## WorkflowGraph 节点
- match_skill
- plan
- spawn_agents
- todo_write
- collect_reports
- synthesize
- ask_if_blocked

## 权限规则
- 编排工具为内部锁定能力。
- 子 Agent 使用外部工具时继承当前权限 profile。
- 计划模式禁止写操作。

## 失败模式
- 子任务卡住时先产出阻塞原因和待确认问题。
- 子 Agent 失败不得吞掉，必须进入 Trace。
