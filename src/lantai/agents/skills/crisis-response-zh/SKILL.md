---
# id: crisis-response (migrated to name)
name: crisis-response
description: 在舆情、事故、产品故障、数据泄露或公共争议发生后，组织事实核查、利益相关方判断、回应口径、记者问答和升级节奏。用户要求“危机回应/声明/舆情处置/事故沟通”时使用；普通品牌文案、虚构危机或未经确认直接对外发布时不要触发。
kind: domain_workflow
domain: public_relations
version: 1.0.0
source: builtin
enabled: true
locked: false
permission_profile: external_publish_with_approval
recommended_agents:
- oolong
- latte
- coconut
- sprite
inputs:
- incident_facts
- stakeholder_questions
- confirmed_boundary
outputs:
- risk_level
- holding_statement
- qa
- escalation_list
memory_writeback:
- risk_boundary
- rejected_phrasing
- media_followup
tags:
- 公关
- 危机
- 声明
---

# 危机回应

适用场景：出现负面、舆情、投诉、媒体问询、责任归因或需要先出 holding statement 时触发。
输出格式：风险分级 + 暂态声明 + Q&A + 升级确认清单

## 子 Agent 分工
- 乌龙先定风险等级和不能说的话。
- 椰汁核事实、时间线和证据状态。
- 拿铁写声明结构。
- 雪碧把 Q&A 改成记者会追问的顺序。

## WorkflowGraph 节点
- classify_risk
- fact_boundary
- draft_statement
- qa
- approval_gate
- update_memory

## 人工确认点
- 所有对外回应必须人工确认。

## 失败模式
- 事实不明时只给占位回应，不归因。
