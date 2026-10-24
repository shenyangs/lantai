# 工具边界

Runtime 负责编排、权限、工作区和交付；Presentation Skill 负责内容、叙事、设计与选工具；DeckIR 负责稳定身份、事实来源和版本检查点；PptxGenJS 负责新建 PPTX 的原生合成；python-pptx 负责检查与有限修改；OfficeCLI 负责查询、验证和有边界批量编辑；GenOffice 负责预览、人工编辑与保存。一次 mutation 只能有一个 writer。

最终 PPTX 的页面渲染与视觉审查统一由 `lantai.presentation.render`、`lantai.presentation.critic` 和质量门禁执行。Skill 不携带第二套缩略图或直接 JavaScript 写入器。

## 产品工具

| 工具 | 责任 | 禁止的隐式行为 |
| --- | --- | --- |
| `create_presentation` | 创建唯一持久化 PPT job，返回 `job_id` | 不把 `title` 冒充完整 brief，不为超时新建重复 job |
| `get_presentation_generation_status` | 只读返回阶段、预算、质量与发布就绪状态 | 不 copy、send 或 finalize |
| `retry_presentation_generation` | 对全部路由均允许自动恢复的失败原子递增重试次数，并从原 job/checkpoint 续跑 | 不新建 job、不重置预算、不把 pause/Stop 当失败重试、不隐式对外发布 |
| `finalize_presentation_generation` | 幂等发布已通过绑定证据的产物 | 不跳过质量或版本检查 |
| `resolve_pending_deliverable_choice` | 在原 Task 范围内原子化解析 continue / replace / cancel | 不用全局数字、正则或关键词推断意图 |

## 生命周期

Native 是唯一 Task owner；PPT job 仅保存阶段、checkpoint、成本和恢复信息，不是第二个任务执行器。断线、前台等待超时和用户 Stop 是三种状态：前两者不能自动中断持久化 job；Stop 必须在页级安全点终止后续合成和 critic，且不得发布。

失败重试只接受最新失败事件中完整、且全部允许自动恢复的持久化路由，并有明确次数上限；它恢复原 `job_id` 和已通过 checkpoint，不重置成本。共享资源、不可改数据、故事板或视觉方向改变时，必须正确传播 dirty 依赖。所有 utility 模型、图像和 critic 费用都计入当前 Runtime 预算；不得为 PPT 建立旁路账户或绕过授权。
