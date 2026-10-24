---
# id: lantai-automation-channel (migrated to name)
name: lantai-automation-channel
description: 设计和管理澜台定时任务、周期监控、提醒、消息渠道与完成通知，明确触发时间、幂等、失败重试和外部发送确认。用户要求“定时执行/每天检查/监控并通知/接入消息渠道”时使用；一次性立即任务、普通计划或未经授权向外部收件人发送时不要触发。
kind: automation
domain: channel_runtime
version: 1.0.0
source: builtin
enabled: true
locked: false
permission_profile: external_action_with_approval
recommended_agents:
- oolong
- latte
inputs:
- task_spec
- channel_context
- notification_target
outputs:
- scheduled_task
- channel_reply_draft
- system_notification
memory_writeback:
- followup_pattern
- channel_boundary
tags:
- 澜台内置
- 定时任务
- 渠道
- 通知
---

# 澜台 自动化与渠道

适用场景：需要创建定时任务、向外部渠道回复或主动发送系统通知时触发。
输出格式：任务配置 + 待确认外发内容 + 通知记录

## 澜台 工具映射
- `schedule_task`：创建、修改、暂停、恢复、取消定时执行任务。
- `send_to_channel`：向微信、Telegram 等外部渠道用户发消息。
- `send_system_message`：向用户发送应用内 Toast 和系统推送。
- `calendar_query` / `calendar_add` / `calendar_modify` / `calendar_delete`

## WorkflowGraph 节点
- parse_schedule
- permission_check
- draft_message
- approval_gate
- execute
- record

## 权限规则
- 查询日历可授权后读取。
- 添加、修改、删除日历事件必须确认。
- 任何外部渠道发送必须确认。
- 定时任务创建和修改必须确认。

## 失败模式
- 没有渠道上下文时只生成草稿，不发送。
- 定时规则不明确时先询问用户。
