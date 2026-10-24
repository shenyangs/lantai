---
# id: pr-launch (migrated to name)
name: pr-launch
description: 基于已核事实完成发布声明、新闻稿、媒体叙事、记者 Q&A、舆情风险分级和危机预案。用户准备新品/项目/合作发布、对外口径、媒体回应或公关风险审查时使用；普通营销文案、虚假造势或未核实传闻不要触发。
kind: domain_workflow
domain: public_relations
version: 1.0.0
source: builtin
enabled: true
locked: false
permission_profile: external_publish_with_approval
recommended_agents:
- latte
- coconut
- oolong
- sprite
inputs:
- facts
- stakeholder_map
- media_questions
outputs:
- statement
- qa
- media_briefing
- risk_boundary
memory_writeback:
- risk_boundary
- accepted_phrasing
- rejected_phrasing
- media_followup
tags:
- 公关
- 发布
- Q&A
---

# PR Launch

适用场景：新品、项目、活动或合作发布前，需要把事实、声明、Q&A 和媒体沟通统一收口。
输出格式：Markdown

## 子 Agent 分工
- 拿铁负责声明结构、导语和标题。
- 椰汁核事实、时间、数字、引用和来源状态。
- 乌龙盯高风险边界、确认链和公关口径。
- 雪碧改写成记者真的会追问的 Q&A 顺序。

## WorkflowGraph 节点
- classify
- load_context
- retrieve_sources
- load_memory
- plan
- subagents
- synthesize
- ai_taste_review
- edit_options
- approval_gate
- artifact
- memory_writeback

## AI味检查规则
- 声明稿不要像发布会大屏口号。
- Q&A 不写正确废话，优先给记者会追问的版本。

## 证据与决策规则
- 建立“主张—证据—负责人—可公开状态”台账；不能公开的信息不写进对外稿。
- 把事实、公司立场、预测和第三方观点分开。
- 至少预演支持者、普通用户、记者、竞品、监管和受影响群体的追问。
- 舆情判断使用公开、可追溯样本，不把少量高声量账号等同于公众意见。
- 不组织水军、虚假身份、骚扰、隐蔽操纵或伪造媒体背书。

需要完整风险分级、声明和 Q&A 结构时读取 [pr-evidence-workflow.md](references/pr-evidence-workflow.md)。

## 人工确认点
- 对外声明、数字、未公开信息、竞品比较和责任归因必须人工确认。

## 复盘回写规则
- 保留被打回的句子、最终通过的边界和媒体追问点。

## 失败模式
- 资料不足时只给待核声明框架，不补事实。
- 风险判断不清时先升高到人工确认，不自动放行。
