---
name: multi_agent_collaboration
description: 标准协作模式：当任务涉及代码、调试、前端/设计、架构、研究、风险、策略、文案、复核或多模块拆分时，优先咨询合适的子 agent；2–4 个互不依赖的只读咨询任务优先使用 collaborate_with_agents 真并发批次，单专家或有依赖任务保持串行。
metadata:
  builtin_skill_version: "2.0"
  lantai:
    emoji: "🤝"
---

# 多智能体协作

## 什么时候用

当任务需要其他 agent 的专业能力、上下文、workspace 内容、第二意见或复核时，使用本 skill。用户明确要求某个 agent 参与时，也必须使用。

当前默认是**标准协作模式**：遇到非平凡的代码、调试、前端/设计、架构、构建/打包、研究、风险、策略、文案、评审、排查或多模块任务时，优先做一次快速专业咨询，再整合结论。

小问答、小改动、纯执行且你已经很有把握的任务，可以直接完成，不必为了显得复杂而调用 agent。

## 调用工具

优先使用内建工具，而不是 shell 命令：

```text
list_agents()

collaborate_with_agents(tasks=[
  {
    "to_agent": "<target_agent_id>",
    "text": "<仅供协作者执行的完整任务>",
    "label": "<可安全公开的一句话任务摘要>"
  },
  {
    "to_agent": "<another_agent_id>",
    "text": "<仅供协作者执行的完整任务>",
    "label": "<可安全公开的一句话任务摘要>"
  }
])

chat_with_agent(
  to_agent="<target_agent_id>",
  text="[Agent <your_agent_id> requesting] ...",
)

submit_to_agent(
  to_agent="<target_agent_id>",
  text="[Agent <your_agent_id> requesting] ...",
)

check_agent_task(task_id="<task_id>")
```

`collaborate_with_agents(...)` 用于 2–4 个互不依赖的只读咨询任务。一次调用即启动完整批次，工具会等待所有成员进入终态并返回可综合结果。`label` 会直接展示给用户，必须简短、准确且不含路径、密钥、内部 ID 或私密提示词。

用户明确要求“圆桌”“会议厅”或“会议室”且任务是判断/讨论时，传入 `protocol="roundtable"`。该协议只增加一次交叉审阅，复用各成员会话，最多两轮；成稿、写文件或其他产物任务保持默认 `parallel_consultation`。

`chat_with_agent(...)` 用于单个专家或需要马上等回复的前台咨询。
`submit_to_agent(...)` 用于较长、较重、可以后台完成的任务；之后用 `check_agent_task(...)` 查结果。

## 内置团队

调用前先用 `list_agents()` 确认实际可用 ID，不要猜。

- 美式 `Lantai_Americano_Agent`：策略、定位、主线、取舍
- 椰汁 `Lantai_Coconut_Agent`：事实核查、信源、证据、时间线
- 乌龙 `Lantai_Oolong_Agent`：公关、合规、危机、风险口径
- 冰茶 `Lantai_IcedTea_Agent`：热点舆论、社会情绪、议题升温
- 雪碧 `Lantai_Sprite_Agent`：媒体、KOL、发布节奏、采访
- 奶茶 `Lantai_MilkTea_Agent`：社媒运营、排期、正文、互动
- 苏打 `Lantai_Soda_Agent`：投放、预算、A/B 测试、指标
- 抹茶 `Lantai_Matcha_Agent`：长文、观点稿、演讲稿、采访稿
- 可乐 `Lantai_Cola_Agent`：标题、slogan、短文案、去模板化
- 拿铁 `Lantai_Latte_Agent`：文档工程、统稿、格式、交付
- 摩卡 `Lantai_Mocha_Agent`：视觉设计、前端、页面、可视化
- 橙汁 `Lantai_Orange_Agent`：视频脚本、分镜、KV、素材清单
- 龙井 `Lantai_Longjing_Agent`：论证链、反方预判、答辩 Q&A
- 白水 `Lantai_Water_Agent`：代码、脚本、自动化、agent/skill 工程

## 决策规则

1. 用户明确要求调用其他 agent 时，先 `list_agents()`，再调用实际存在且启用的 agent。
2. 当任务可拆为 2–4 个互不依赖的研究、写作、复核、设计建议或风险判断任务时，优先一次调用 `collaborate_with_agents(...)`；不要连续调用多个 `chat_with_agent(...)` 模拟并发。
3. 只有一个合适专家、任务存在先后依赖、后一步需要前一步结果时，使用 `chat_with_agent(...)` 或 `submit_to_agent(...)` 串行处理。
4. 多个任务会写入同一工作区、修改同一资源、产生外部副作用或需要逐步审批时，不得批量并发；由主 agent 串行执行实际写入。
5. 批量协作只用于咨询型工作。成员可以读取、搜索和分析，但不得修改共享工作区或外部状态。
6. 发出批次后必须等待批次终态，不得把“正在等待”“稍后汇总”等过程话术当作最终答案。
7. 单个成员失败不阻断其他成员。等待所有成员进入终态，明确标注失败项，并用其余有效结果完成部分交付。
8. 不要回调刚刚给你发消息的 agent，也不要让协作者递归组队，避免循环。
9. 子 agent 的回复是材料，不是最终答案；你必须整合、判断、裁剪后直接回复用户，不能倾倒其思考过程。
10. 圆桌只在用户明确要求时开启；不得超过第二轮，不得将一般协作自动升级为圆桌。

## 消息写法

给子 agent 的消息要清楚说明任务、材料、限制和输出格式。推荐以身份前缀开头：

```text
[Agent default requesting]
请从你的专业角度处理下面任务。只输出对沈行知整合有用的结论、风险和建议。

任务：
...

输出格式：
1. 结论
2. 依据
3. 风险
4. 建议
```

批量任务的 `text` 可以包含完整上下文，但 `label` 只写用户可见的安全摘要，例如“核查事实与来源”“评估合规风险”。不要把文件路径、会话 ID、任务 ID、密钥、个人数据或完整子任务提示词放进 `label`。

如果需要续聊，使用上一次 `chat_with_agent(...)` 返回的 `[SESSION: ...]` 作为 `session_id`。
