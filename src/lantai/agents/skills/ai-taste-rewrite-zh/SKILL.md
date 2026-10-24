---
name: ai-taste-rewrite
description: 检测或改写中英文草稿中的 AI 套话、空洞大词、机械排比、过度总结、假亲切和平台错位，同时保留作者事实、观点、个人语气与有意的不规则表达。用户要求“去 AI 味/改得像人写的/润色但别改风格/只检查 AI 痕迹”时使用；事实研究、从零写稿或只做错别字校对时不要触发。
kind: domain_workflow
domain: content_rewrite
version: 1.1.0
source: builtin
enabled: true
locked: false
permission_profile: workspace_write_with_approval
recommended_agents:
- latte
- cola
- coconut
inputs:
- draft
- brand_context
- platform_context
outputs:
- rewritten_draft
- ai_taste_notes
- fact_check_list
memory_writeback:
- rejected_phrasing
- accepted_phrasing
- platform_voice
- brand_boundary
tags:
- 改稿
- AI味
- 平台语感
---

# 人味改稿

目标不是“让所有句子更漂亮”，而是让内容更像这个作者在这个场合真的会说的话。

## 先选择模式

- **只检测**：标出问题、原因和优先级，不改正文。
- **轻改**：只处理 AI 痕迹、冗余和节奏，不重排论证。
- **深改**：允许重组段落与论证，但不改变核心事实和立场。
- **对照版**：给原句、改句和改动理由，适合重要稿件。

用户没有指定时，默认轻改。

## 工作流

1. 识别作者真正的判断、事实、受众、平台和不能改变的表达。
2. 标记高风险事实、引语、数字、专有名词和法律/品牌口径；这些内容不靠语言感觉改写。
3. 找出 AI 痕迹：
   - 没有信息增量的开场和总结；
   - “不仅……更……”等机械递进；
   - 三段式、整齐排比和相同句长连续出现；
   - 抽象名词过多，缺对象、动作和因果；
   - 假装亲切、过度鼓励、替用户下结论；
   - 为显得有文化而生硬引用或拼贴；
   - 中文翻译腔或英文 corporate filler。
4. 先删无任务句，再把判断和动作提前；只有确有信息时才补细节。
5. 保留作者惯用词、节奏、克制、幽默、停顿和有意的口语感。不要把所有句子标准化。
6. 复核是否改变事实、承诺、语气强度或责任归属。

## 输出

- 直接改稿时先给成稿；必要时附 3–7 条关键改动。
- 只检测时按“位置—问题—影响—建议”输出。
- 不能确认的事实单列为待核，不在正文中悄悄修补。
- 用户明确不喜欢的句式优先遵守；没有证据时不擅自扩展成长期偏好。

## 禁止

- 不套固定“人味”模板。
- 不故意加入错别字、虚假经历、伪造引语或情绪。
- 不用口语化掩盖论证空洞。
- 不把专业、克制误判成 AI 味。

## 停止条件

稿件任务清楚、信息密度提高、声音仍属于作者、事实与立场没有漂移、待核项已显式列出。
