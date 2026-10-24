---
# id: lantai-evolution-feedback (migrated to name)
name: lantai-evolution-feedback
description: 把用户反馈、失败样例、使用数据和系统能力地图转成可审阅的澜台改进提案、评测与受控变更。用户要求“让澜台从反馈中改进/形成能力优化方案/验证自进化建议”时使用；单个产品 bug 修复、无证据的自主改写或未经批准直接应用系统变更时不要触发。
kind: evolution
domain: system_evolution
version: 1.0.0
source: builtin
enabled: true
locked: false
permission_profile: propose_only_apply_with_approval
recommended_agents:
- latte
- oolong
inputs:
- system_map
- user_feedback
- page_spec
outputs:
- evolution_proposal
- feedback_record
- widget_plan
memory_writeback:
- user_feedback_pattern
- ui_preference
tags:
- 澜台内置
- 自进化
- 反馈
---

# 澜台 自进化与反馈

适用场景：需要读取系统地图、提出自进化建议、管理自定义页面/Widget，或向作者/画廊反馈时触发。
输出格式：改进方案 + 风险说明 + 人工确认点

## 澜台 工具映射
- `read_system_map`
- `custom_page` / `personalize_ui`
- `list_widgets` / `create_widget` / `modify_widget` / `delete_widget`
- `bind_slot` / `widget_storage`
- `propose_evolution`
- `submit_idea` / `gallery_comment` / `gallery_get` / `gallery_submit` / `gallery_upload_image`

## WorkflowGraph 节点
- inspect_system
- propose
- approval_gate
- sandbox_apply
- collect_feedback

## 权限规则
- 系统地图只读。
- 自进化默认只能提出建议。
- 改页面、改 Widget、发帖、上传图片都必须确认。

## 失败模式
- 不清楚系统结构时不改代码，只写方案。
- 自进化不能绕过权限和审计。
