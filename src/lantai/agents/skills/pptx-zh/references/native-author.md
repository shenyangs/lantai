# 页级原生作者

`visual_direction.palette` 按画布、正文主色、可选强调色排序。明确传入时，自主设计上下文消费作者视觉概念与这些角色色；不能把内置合成风格误当外部参考稿覆盖方向。模板 HARD 品牌约束与用户明确颜色优先于作者提案。其余视觉字段保留为作者意图，但字体、图像和装饰是否真正兑现仍须检查最终页。

`create_presentation` 一次接收整套 slide/object schema。新作品调用时须传入完整 `brief`、`source_paths`、`required_facts`、`immutable_data`、`visual_direction`，以及成对的 `narrative_arc` 与 `storyboard`；`title` 不是 brief。`storyboard` 与最终 slides 一一对应，页级 `source_indexes` 从 0 起引用 `source_paths`，每份输入材料至少要有一页去向；这只验证映射，不证明材料已完整抽取。页级必留/不可改事实只能从整套集合中选取。历史调用可省略故事板以保持兼容。

页级作者的输入是经过编辑的故事板和统一视觉方向。页对象优先使用原生 text、shape、image、table 和 chart，并保留语义角色、来源和不可改数据。`deck-plan.json` 将逐页主张绑定到原生页 ID，供逐页 critic 使用；整套叙事弧也进入审查上下文。这个接口仍一次提交全套页面，不等同于已完成页级生成与恢复。

当且仅当 freeform 页的每个对象都提供明确 frame 时，设置 `composition_authority: "author"`。这表示对象坐标和层级是作者意图，下游只做测量、约束、序列化和验证，不得再摊平或重排。缺少完整 frame 时用 `design_system`。

不得为让代码识别作者页而添加无语义 Shape。表格、图表、纯文字页和图片页都可以拥有作者权威。如果实际测量发现越界或不可读，返回作者局部修页，不在后续阶段偷换布局。
