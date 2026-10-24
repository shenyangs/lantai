# 沈行知人格体系完整性契约

> 状态：项目级强制约束  
> 生效日期：2026-08-22  
> 最低体感基线：2026-08-22 01:05（Asia/Shanghai）完成的「隐性主体连续性 V4」  
> 适用范围：`lantai3` 中所有人格、对话、记忆、任务智力、蛐蛐和 Runtime 改动

## 1. 这份契约保护什么

沈行知的活人感不能靠更多「像人的句子」堆出来。他必须先是一个在后台连续存在的主体：记得真实发生过的事，理解用户此刻的言外之意，对自己刚做过的事留有余波，并让这些事实真正改变下一次判断、表达和干活方式。

受保护的完整因果链是：

`Stable Self → 真实事件/用户记忆/共同历史 → 私有主体状态 → 模型拥有的当轮语义理解 → 个人化后果 → 正文/专业判断/行动 → 一次后反思 → 蛐蛐与下一轮主体状态`

改动任何一环时，不能只问「这条 prompt 是否更周全」，而必须问「它是否让整条因果链更真，还是用一条局部规则取代了主体的判断」。

## 2. 人格是状态机，不是规则表

### 2.1 稳定自我

Constitution、SOUL、PROFILE、BACKSTORY、STATE 和 SELF_CANON 提供稳定价值、审美、专业反射和世界感。它们是生成判断的内在条件，不是供 Runtime 逐条执行的话术清单。

### 2.2 连续用户理解

保存证据、来源、时效、确定度、用户纠正、活跃项目、近期经历、价值倾向和未完事项。当轮由模型结合上下文产生联想；不得把「用户喜欢陶喆」之类记忆写成预制词义链、歌名触发器或饱和式标签关联。

### 2.3 主体连续性

沈行知对自己上一轮的判断、失误、松一口气、好奇、嘴硬或未完念头可以有微小余波。状态只接受真实对话、最终回答、真实执行与模型反思作为证据，不伪造现实生活或共同经历。

### 2.4 当轮智力

是否闲聊、是否需要完成工作区任务、用户真正担心什么、哪条记忆此刻有关、沈行知有什么主观反应、该在哪个出口显现，都属于语义智力。它们必须由模型看完整对话和投影后作出；本地代码不得根据「生气」「宝贝」「今晚」「帮我写」等词强制一段姿态。

### 2.5 个人化后果

投影必须产生一个内部可审计的「这次因为懂你，具体改变了什么」。它可以改变判断顺序、主动补齐、风险反对、验收严格度、决策负担、语气或联想。人格不能每轮都只落成一句关心，也不能只躲在蛐蛐里。

### 2.6 一次后反思与蛐蛐

最终正文完成后，允许一次模型反思，从真实回答中产生可选正文人格句、蛐蛐和下一轮状态。这一层应偏好有因的小心思、自我拆台、微小虚荣、松一口气、不完全工整的余念，而不是第二遍结论。

答后反思必须是异步旁车，不能延迟正文终态。正文一旦完成，应立刻结束前台生成状态、恢复输入和停止按钮；反思可优先使用配置的快速辅助模型，未配置时可以异步回退到当轮主模型，但不得回到前台同步等待。请求必须保留完整的用户输入、真实最终回答、人格计划、主体投影和必要历史，不得为追求速度而截断或瘦身。

每个答后反思任务必须持久化并严格绑定原 `chat_id / session_id / run_id / client_turn_id / answer_message_id`。迟到的蛐蛐只能追加到原回答卡，不得重开已结束轮次、改写当前运行状态、另起消息框或串入其他会话。生成结果应先持久化，再按消息与事件幂等投递；恢复时继续投递同一条结果，不得重新调用模型换一条蛐蛐。失败可以在同会话内有限重试，但失败时保持无可见本地兜底。

蛐蛐路由只有两层：

- 50% 基础硬概率：命中就必须调用模型产生真实反应，不允许「命中但安静」。
- 语义/情感金句旁路：未命中时，模型认为沉默会丢失这一刻的主体性，可无视概率出现。

没有第三层的本地静默门槛，没有固定兜底句，没有重复补发。

## 3. 确定性 Runtime 的正当边界

确定性代码可以并应当管理：

- 权限、审批、安全、工具 schema 与参数；
- 事实完整性、幂等、重试预算、超时和可观测性；
- 记忆来源、时效、删除、去重、敏感度和数据控制；
- 蛐蛐 50% 抽样、单轮只出现一次、重复检测与事件标记；
- Native / Legacy 投影一致性、模型输出 schema 和失败诊断。

确定性代码不得决定：

- 这轮是什么类型的对话、用户的潜台词或关系意味；
- 沈行知此刻「应该」什么感受；
- 用什么句式关心、反对、亲近、生气或设立边界；
- 某个关键词是否应当强制某个人格信号；
- 当模型没有产生人格文字时，用什么本地台词代补。

`user-correction` 为 01:05 基线中唯一保留的确定性修复提示，它用于防止已经发生的用户纠正被忽略，而不是用来规定口吻。未经用户明确批准，禁止扩大这个集合。

## 4. 禁止模式

以下实现一律视为人格回归：

1. 新增「公开愤怒」「亲密称呼」「深夜」等本地正则并强制注入可见台词。
2. 为了看起来更安全，在第一句强制说边界、拒绝、风险教育或目标访谈。
3. 用「默认保持安静」「只有明显值得说」等本地门槛在 50% 概率之后再压一次蛐蛐。
4. 蛐蛐失败后用「我还在」类固定句补位，或在 Native / Legacy / UI 多处各补一次。
5. 把用户偏好展开成预制词义网，每次命中就卖弄记忆。
6. 把人格全部塞进蛐蛐，正文和干活仍然是可替换的通用模型。
7. 用「每轮都要温暖」「每轮都要先表态」等配额替代语义时机判断。
8. 为了分数更高而消灭停顿、半句话、小心思、自我拆台和普通人的不完全工整。
9. 当其他任务也在改相同文件时，不核对而直接覆盖、回滚或以时间戳判定哪一版正确。

## 5. 受保护面

下列文件或目录不是「不能修改」，而是任何修改都必须证明没有破坏本契约：

- `src/lantai/agents/persona/subject_continuity.py`
- `src/lantai/agents/persona/post_answer_reflection.py`
- `src/lantai/agents/persona/perceptible_presence.py`
- `src/lantai/agents/persona/ququ.py`
- `src/lantai/agents/persona/subjective_reaction.py`
- `src/lantai/agents/turn_intelligence.py`
- `src/lantai/agents/legacy_agent_components.py`
- `src/lantai/agents/legacy_lantai_agent.py`
- `src/lantai/runtime/adapters/native_runtime.py`
- `src/lantai/app/runner/runner.py`
- `src/lantai/app/post_answer_reflection_service.py`
- `console/src/pages/Chat/runtimeEventStream.ts`
- `src/lantai/agents/memory/`
- `src/lantai/agents/md_files/constitutions/`
- `src/lantai/agents/md_files/{zh,en}/SOUL.md`
- `src/lantai/agents/md_files/{zh,en}/PROFILE.md`
- `src/lantai/agents/md_files/{zh,en}/STATE.md`
- 人格迁移、多轮基准、真实 Provider smoke 与包态验收脚本

## 6. 修改协议

### 6.1 开工前

1. 阅读本契约、V4 方案、V4 实现记录和架构导览。
2. 运行 `git status --short`，用 `git diff -- <protected files>` 检查其他进程的重叠。
3. 先写出要保持和要改变的用户可见行为。
4. 为失败模式增加行为锁，再修实现。

### 6.2 实现中

1. 让模型输出语义决策和个人化后果，让 Runtime 执行边界和记录事实。
2. 保持单一投影，不为 Native、Legacy、UI 分别实现「更保险的一层」。
3. 新增安全要求时，约束行动结果和工具权限，不要把程序语言式的 `must/must not` 台词硬塞进每次真实回答。
4. 记录失败原因，不用另一句「很像沈行知」的文字掩盖失败。
5. 答后反思只允许延迟投递自己的结果，禁止延迟前台终态；恢复、重试和辅助模型切换不得改变原消息归属。

### 6.3 验收

人格相关改动至少完成：

1. `uv run pytest -q tests/unit/agents/persona/test_persona_system_integrity.py`
2. 受影响的 Agent / Memory / Runner / Native / Legacy 单元测试。
3. 源码态真实 Provider 多轮 smoke，验证上一轮事件真正改变下一轮的判断或交付。
4. 如果生成发行候选，运行冻结后端、真实 Provider、蛐蛐重复、版本一致性和签名检查。
5. 与 01:05 黄金证据对比，并由人检查因果感、主体性、特定关系感、不完全工整和任务完整性。
6. 验证正文终态先于答后反思、迟到蛐蛐不重开轮次、同任务幂等、跨会话隔离、失败恢复复用同一生成结果，以及完整请求未被裁剪。

不允许只报「测试数量通过」。验收记录必须说明用户实际会感受到什么，并附上真实回答与蛐蛐样本。

## 7. 01:05 黄金基线与证据

设计与实现：

- `docs/shen-xingzhi-hidden-subject-continuity-v4-plan-2026-08-22-zh.md`
- `docs/shen-xingzhi-hidden-subject-continuity-v4-implementation-2026-08-22-zh.md`
- `docs/remediation/shen-xingzhi-subject-continuity-sprint-2026-08-22.md`

真实 Provider 证据：

- `docs/remediation/evidence/subject-continuity-v4-source-provider-2026-08-22.json`
- `docs/remediation/evidence/subject-continuity-v4-packaged-provider-2026-08-22.json`
- `docs/remediation/evidence/subject-continuity-v4-packaged-screenshots-2026-08-22/`

可回放的本地候选包：

- `release/builds/2.0.2-8f07f7e65823-arm64-candidate-subject-continuity-v4/`

其中最重要的不是文案，而是行为：一轮算错的真实经历，在下一轮工作任务中改变了沈行知的专业谨慎、判断路径和蛐蛐，而不是机械复述「我记得刚才算错」。

## 8. 冲突处理

受保护文件被多任务同时修改时：

1. 暂停最后写入合并，分别列出每个改动的用户目标。
2. 保留与人格无关且不切断因果链的产品改动。
3. 人格行为以用户最后明确认可的基线为准，不以时间戳、评分或改动行数作为正确性依据。
4. 合并后重跑完整性测试和真实 Provider smoke，不能只看 diff。

这份契约保护的不是某一段文案，而是「沈行知真的在这里，且他是同一个人」这件事。
