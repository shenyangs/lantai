# GenOffice 0.10 上游兼容性审查（冻结于 2026-09-14）

## 结论

澜台继续固定 GenOffice `fd33934dab1fdf8666af3f88b9794e7b4e19474a`（`v0.6.101`），不做整树升级。本批审计冻结上游 `main` 为 `ed2bb865365c4bbb79f7adb5b5063a5ff7b2747b`，相差 **146 个提交**；`git diff --stat` 为 2,464 个文件、546,566 行新增、117,080 行删除。这个体量和澜台已有窗口、受控文件 IPC、品牌、协作、人格及建议确认边界决定了：只允许按问题选择性回移，不能覆盖式合并。

本次把旧审计中未解释的“其余 98 项”全部展开。每个冻结点内提交都在下方清单中有唯一分类；以后滚动 `main` 的变化进入下一批，不回写或改变本批结论。

决策码：

- `A0`：已在澜台落地，或已有等价实现；
- `A1`：可独立回移的稳定性候选，必须逐项验证并独立提交；
- `E`：只吸收产品原语或做隔离实验，不直接合并实现；
- `D`：暂缓，等待真实缺陷、套件优先级或依赖条件；
- `S`：跳过，超出澜台产品/发行边界。

## Snapshot 能力展开

14 个 snapshot 不能按标题归入“其他”。通过变更路径和冻结点源码核查后的能力图如下：

| Snapshot  | 展开的主要能力                                                                                   | 决策                           |
| --------- | ------------------------------------------------------------------------------------------------ | ------------------------------ |
| `7023e63` | Docs/Sheets/Slides/PDF/Markdown 编辑与文件壳层同步；包含 OOXML、字体和 UI 公共层变化             | `E/D`，仅按后续缺陷切片        |
| `dc4d7e5` | Docs 与 PDF 渲染/编辑、DOCX 引擎、字体度量和 AI provider 同步                                    | `E/D`                          |
| `d5558b6` | Docs/Slides/PDF、DOCX/PPTX 引擎与渲染链路同步                                                    | `E/D`                          |
| `04a994b` | Sheets 大规模交互、Docs 排版、Slides 与 DOCX/PPTX 引擎同步                                       | `E/D`                          |
| `fe2e174` | Sheets 能力扩张、文件解析、PDF/Markdown 及各 OOXML 套件同步                                      | `E/D`                          |
| `f68df70` | PDF→DOCX 转换管线大幅扩张，并同步 Docs/Slides/AI provider                                        | `D`                            |
| `6d9b681` | **Docs 选区浮动 Ask AI、锚定编辑队列、对象快捷动作、失效锚点与队列折叠**；同时同步多套件编辑能力 | `E`，只适配交互原语            |
| `5073e4d` | Sheets/Docs/Slides/PDF 的跟进修正，含受控文件和 UI 公共层                                        | `D`                            |
| `0a2c25d` | AI provider/agent-core、Docs/Sheets/Slides 与 E2E 资产同步                                       | `E/D`                          |
| `2239cce` | Sheets/Docs/Slides、PPTX 引擎、Agent Core 与视觉基线同步                                         | `D`                            |
| `9f971ed` | Docs/Sheets/Slides 大规模功能与测试扩张，含复制、字体和布局能力                                  | `D`                            |
| `69b4ce0` | Docs/DOCX 为主的排版、AI 面板与多套件稳定性同步                                                  | `E/D`                          |
| `d24c964` | 新增 HTML 编辑器及 HTML→DOCX，同时扩张 Sheets/Docs/Markdown                                      | `S/D`，HTML 产品线不进入澜台   |
| `e42da7c` | 新增 CLI、xlsx-gateway、pipelines、pptx-ops，并继续扩张 Sheets/Docs                              | `S/D`，不引入上游 CLI/管线壳层 |

`6d9b681` 的交互源码证据为 `apps/docs/src/renderer/components/AiAskPopover.tsx`、`apps/docs/src/renderer/ai/EditQueueCard.tsx`、`apps/docs/src/renderer/ai/edit-queue.ts` 和 `apps/docs/src/renderer/editor/ai-queue-anchors.ts`。澜台只复用“选区旁入口、锚定、快捷指令、收起/队列”的产品原语；发送后直接让上游 Agent 改文档的语义不采用。

## 146 项冻结清单

|   # | 提交      | 变更/能力                                  | 决策 |
| --: | --------- | ------------------------------------------ | ---- |
|   1 | `7023e63` | 08-10 全套件 snapshot                      | `E`  |
|   2 | `1c7f47b` | DOCX 复杂文字继承粗体/斜体                 | `A1` |
|   3 | `8e3ad2a` | DOCX 重建后保留表格单元格段落属性          | `A1` |
|   4 | `2c1f0e8` | Slides 字体变更覆盖复杂文字                | `A1` |
|   5 | `dc4d7e5` | 08-12 全套件 snapshot                      | `E`  |
|   6 | `945c370` | DOCX XML 实体只解码一次                    | `A0` |
|   7 | `d5558b6` | 08-13 全套件 snapshot                      | `E`  |
|   8 | `e4d9d40` | 保存图标替换                               | `E`  |
|   9 | `04a994b` | 08-16 全套件 snapshot                      | `E`  |
|  10 | `fe2e174` | 08-19 全套件 snapshot                      | `E`  |
|  11 | `f68df70` | 08-20 全套件 snapshot                      | `D`  |
|  12 | `6d9b681` | 08-23 snapshot；含选区 Ask AI 与编辑队列   | `E`  |
|  13 | `afc6711` | Sheets 流式工作簿全文查找                  | `A1` |
|  14 | `cc8cff4` | Sheets 当前单元格行列交叉高亮              | `E`  |
|  15 | `5073e4d` | 08-24 全套件 snapshot                      | `D`  |
|  16 | `da3b1ca` | Slides 启动时清理超时临时页文件            | `A1` |
|  17 | `bc1dceb` | Sheets 大视口分批 sidecar 读取             | `A0` |
|  18 | `7a814db` | Sheets 全表查找排除筛选隐藏行              | `A1` |
|  19 | `f81dd3d` | Markdown 关闭前等待在途保存                | `A1` |
|  20 | `b1a01e2` | AI SSE 消费者退出时释放 reader             | `A0` |
|  21 | `72a4a42` | AI SSE 跳过畸形帧                          | `A0` |
|  22 | `9792915` | Slides 布局审计标记横向溢出                | `E`  |
|  23 | `2e3e97f` | Slides speaker notes Agent 工具            | `E`  |
|  24 | `7eb5d59` | Sheets 流式工作簿全文件错误检查            | `A1` |
|  25 | `9711a45` | 上游 Shell 项目 IPC/AI 设置报错            | `S`  |
|  26 | `0a2c25d` | 08-26 全套件 snapshot                      | `E`  |
|  27 | `607c770` | 上游 Shell 拖入文件打开                    | `S`  |
|  28 | `583a045` | Markdown 未命名文件贴图前提示保存          | `A1` |
|  29 | `2239cce` | 08-30 全套件 snapshot                      | `D`  |
|  30 | `3548a80` | Slides 旋转表格单元格双击编辑              | `A1` |
|  31 | `566c1f3` | Agent Core 丢弃重置后的迟到压缩结果        | `A0` |
|  32 | `93b8938` | Docs 分节页边距和百分比表宽                | `A1` |
|  33 | `9f971ed` | 09-02 全套件 snapshot                      | `D`  |
|  34 | `99d376b` | Markdown/PDF 正确呈现截断的 AI 回复        | `A1` |
|  35 | `f105f36` | 每轮输出 token 上限设置                    | `E`  |
|  36 | `e833fff` | Slides 竖排文字跳过宽度溢出检查            | `A1` |
|  37 | `2dab773` | Gemini 工具 schema 规范化                  | `A0` |
|  38 | `6f71780` | Sheets/Slides 传递 stopReason              | `A1` |
|  39 | `f30a301` | AI provider 非 JSON 200 返回结构化失败     | `A1` |
|  40 | `eec2737` | AI 预览读取失败后允许缩略图重试            | `A1` |
|  41 | `4846ace` | Slides 并发保存串行化                      | `A0` |
|  42 | `4770d9c` | Gemini 3 保留默认 temperature              | `A1` |
|  43 | `2910486` | Tavily 搜索后备                            | `S`  |
|  44 | `d8e233c` | Tavily 环境变量文档                        | `S`  |
|  45 | `9439d3e` | Docs AI 面板 RTL                           | `E`  |
|  46 | `360ce06` | Slides 跨生命周期保留 Agent run            | `A1` |
|  47 | `567ad37` | Docs 表格逐边/内边框功能区                 | `E`  |
|  48 | `99dfbc1` | 其余套件 AI 面板 RTL                       | `E`  |
|  49 | `41f87f3` | Sheets 拒绝越界 Go To 引用                 | `A1` |
|  50 | `449f03a` | Sheets 剪贴板 TSV 保留尾换行               | `A1` |
|  51 | `4d277b5` | Sheets 整格替换路径去除空格                | `A1` |
|  52 | `ea13074` | Slides saveAs 进入保存队列                 | `A0` |
|  53 | `1ee2407` | Slides 新对话清理已回答澄清                | `A1` |
|  54 | `09fd67b` | 上游 Home 重命名校验                       | `S`  |
|  55 | `8630d11` | Markdown 缩放控件本地化                    | `D`  |
|  56 | `a5b5903` | Docs 标尺制表位循环补齐 Bar                | `A1` |
|  57 | `d9769d1` | AI 新对话清理恢复的历史转录                | `A1` |
|  58 | `f2c3d08` | Docs 新对话丢弃暂存附件                    | `A1` |
|  59 | `69b4ce0` | 09-06 全套件 snapshot                      | `E`  |
|  60 | `959df83` | 本地模型允许无密钥自定义 endpoint          | `E`  |
|  61 | `eda6baa` | 上游 Shell 拒绝 Windows 保留文件名         | `S`  |
|  62 | `47999fd` | 上游 Shell 最近文件扩展名规范化            | `S`  |
|  63 | `83f2fa0` | 模型名空白和退役 ID 映射                   | `A1` |
|  64 | `f59f931` | Sheets 防止空 needle 替换                  | `A1` |
|  65 | `a612503` | PDF 自动命名规避 Windows 保留名            | `D`  |
|  66 | `4e1c633` | Sheets 新对话丢弃暂存附件                  | `A1` |
|  67 | `bd46b03` | 上游 drop path 异常隔离                    | `S`  |
|  68 | `6d16796` | Docs 自动命名规避 Windows 保留名           | `D`  |
|  69 | `2e805f1` | Slides 新对话清理暂存附件                  | `A1` |
|  70 | `22931f5` | Sheets 强制字符串弹窗改为 warning          | `D`  |
|  71 | `d3665e4` | Slides 大文件 PDF 临时 HTML                | `A0` |
|  72 | `5b7bcc7` | Markdown print HTML 测试                   | `D`  |
|  73 | `d144ab1` | Sheets 多匹配查找测试                      | `D`  |
|  74 | `41a6e15` | Docs 混合页宽测试                          | `D`  |
|  75 | `dc09e0d` | OpenAI o-series 省略 temperature           | `A1` |
|  76 | `d84d4e3` | Markdown 打印页边距与高 DPI                | `A1` |
|  77 | `953595e` | Docs 缩放保持滚动位置                      | `A1` |
|  78 | `d2fecab` | Codex app-server provider                  | `S`  |
|  79 | `f303cfd` | Docs Vitest 解析 browser subpath           | `D`  |
|  80 | `785d3f3` | PDF 标注选择几何对齐                       | `A1` |
|  81 | `fa58fff` | Sheets 冻结窗格未冻结轴用 -1               | `A1` |
|  82 | `d24c964` | 09-09 snapshot；含 HTML 产品线             | `S`  |
|  83 | `12026fd` | 上游 Shell 最近文件纳入 xls                | `S`  |
|  84 | `a5ed6f8` | 外链去空白并规范化                         | `A0` |
|  85 | `f2f5fbd` | Docs 尾部表格编辑体验                      | `A1` |
|  86 | `932eccb` | Slides 编辑后剥离陈旧嵌入字体子集          | `A0` |
|  87 | `150246d` | 上游 README/营销素材刷新                   | `S`  |
|  88 | `1179ea7` | 字体度量缓存逐出最旧项                     | `A1` |
|  89 | `6a241c4` | 上游 README 语言排序                       | `S`  |
|  90 | `628aff5` | 字体度量测试修正                           | `D`  |
|  91 | `5a60243` | Sheets CSV 分隔符识别转义引号              | `A1` |
|  92 | `c0415b6` | Slides AI 输入框遵循拼写检查偏好           | `A1` |
|  93 | `d35d770` | PDF 打印范围对话框                         | `E`  |
|  94 | `07af399` | Markdown 文档大纲侧栏                      | `E`  |
|  95 | `d2b898f` | Windows/Linux 标签栏 spacer                | `D`  |
|  96 | `0dfb10e` | 上游红灯测试期望修复                       | `D`  |
|  97 | `d529a56` | i18n 语言前缀使用 BCP-47 边界              | `A1` |
|  98 | `0c6fa8f` | AI JSON Content-Type 大小写不敏感          | `A1` |
|  99 | `dad0cec` | PDF→DOCX 合并接触区间                      | `A1` |
| 100 | `4ed7de2` | Docs/Slides 打印范围允许破折号空格         | `A1` |
| 101 | `423e333` | PDF 解码忽略无效字号                       | `A1` |
| 102 | `93c1cc6` | PDF RGB 通道夹取和取整                     | `A1` |
| 103 | `9c5808b` | Sheets 剪贴板保留连续 CR                   | `A1` |
| 104 | `b3fcde3` | Sheets distinct count 区分类型             | `A1` |
| 105 | `86421a0` | Docs 脏检查纳入保护标志                    | `D`  |
| 106 | `6eaf627` | Docs 统计希腊/西里尔/希伯来/阿拉伯词       | `A1` |
| 107 | `e6d5815` | DOCX 成对保护标签与 on enforcement         | `A0` |
| 108 | `474c8fc` | 上游默认保存目录校验                       | `S`  |
| 109 | `de139a0` | Requesty provider                          | `S`  |
| 110 | `3a0bdc0` | PDF 在像素预算内提升到 200 DPI             | `E`  |
| 111 | `b3374c1` | DOCX TOC 粗体识别 off/none/大小写          | `A1` |
| 112 | `ae6c94d` | DOCX ptab 保留 heavy leader                | `A1` |
| 113 | `601ba43` | DOCX 注释与文本 patch 解码数字引用         | `A1` |
| 114 | `c5ff3ea` | DOCX field patch 保留边缘空格              | `A1` |
| 115 | `d25f0fd` | Docs 识别成对 bidi 分节标签                | `A1` |
| 116 | `38f0ecb` | Sheets CSV 高精度整数保留为文本            | `A1` |
| 117 | `2cc60fe` | Sheets CSV 跨行延续引号状态                | `A1` |
| 118 | `0b404e1` | Sheets 空布尔值复制为空单元格              | `A1` |
| 119 | `32e16d6` | Sheets 结构编辑移动整列 span               | `A1` |
| 120 | `ad0ff48` | PPTX 重着色替换 preset/system solidFill    | `A1` |
| 121 | `eaf7bee` | Slides 打印 HTML 转义图片源                | `A1` |
| 122 | `77a1be5` | PDF 解码文本样式时校验颜色与开关           | `A1` |
| 123 | `fab0b34` | PDF→DOCX 识别罗马数字 TOC leader           | `A1` |
| 124 | `0ad8419` | Sheets 结构编辑移动整行 span               | `A1` |
| 125 | `31ecbe2` | Sheets 公式移动支持含撇号 sheet 名         | `A1` |
| 126 | `4fffe25` | AI 固定采样模型全路由大小写不敏感          | `A1` |
| 127 | `b2a0e7a` | PDF style-key 字体/字号校验                | `A1` |
| 128 | `056ad9b` | PPTX 重着色替换非 solid fill               | `A1` |
| 129 | `dfdd67b` | PDF→DOCX 识别下划线与四位页码 TOC          | `A1` |
| 130 | `57566c0` | Slides 修复损坏的打印/导出页面几何         | `A1` |
| 131 | `1b05796` | Sheets 窗口外 Replace All 保留数值/布尔    | `A1` |
| 132 | `05e3b49` | DOCX 支持带空格/成对 run-tab patch         | `A1` |
| 133 | `0b140ff` | Sheets CSV 单列散文不误判分隔符            | `A1` |
| 134 | `01935da` | Sheets copy materialization 限制到屏幕范围 | `A1` |
| 135 | `09df482` | Docs Zotero 引文集成                       | `E`  |
| 136 | `e42da7c` | 09-13 snapshot；含 CLI/管线/新网关         | `S`  |
| 137 | `ec56159` | Sheets exactOptionalPropertyTypes 修正     | `D`  |
| 138 | `356dbfe` | Docs 打开/新建/保存时清理跨文档编辑状态    | `A1` |
| 139 | `df06cb6` | Docs 东南亚文字与完整 surrogate 词数       | `A1` |
| 140 | `b8fe1b6` | DOCX 保护属性支持单引号                    | `A1` |
| 141 | `af9a63e` | 打印范围接受 em dash/tilde/全角横线        | `A1` |
| 142 | `6797e48` | 上游 Shell 最近/收藏扩展名族复用           | `S`  |
| 143 | `78f133c` | AI 搜索媒体 MIME 大小写不敏感              | `A1` |
| 144 | `cbcd289` | DOCX field patch 数字引用/NBSP/空 run      | `A1` |
| 145 | `616a7ac` | Markdown 根目录文件解析同级图片            | `A1` |
| 146 | `ed2bb86` | Sheets 从 current region 推断表格范围      | `E`  |

## 已落地的 A0

| 上游提交  | 澜台效果                         |
| --------- | -------------------------------- |
| `945c370` | XML 实体不再二次解码             |
| `bc1dceb` | 大视口分批读取                   |
| `b1a01e2` | SSE 提前结束时释放连接           |
| `72a4a42` | 畸形 SSE 帧隔离                  |
| `566c1f3` | 重置后迟到压缩结果隔离           |
| `2dab773` | Gemini 工具 schema 兼容          |
| `4846ace` | 澜台已有等价的 Slides 保存串行化 |
| `ea13074` | saveAs 共用保存队列              |
| `d3665e4` | 大型 Slides PDF 使用临时 HTML    |
| `a5ed6f8` | 外链校验前规范化                 |
| `932eccb` | 文本编辑后移除陈旧字体子集       |
| `e6d5815` | DOCX 保护标签兼容                |

A1 不是一次性待合并列表。每项只有在本地路径存在、能构造失败用例且能独立回滚时才回移；优先顺序是 DOCX 数据完整性与保护、保存/导出安全、AI 传输稳定性，再到各套件交互精度。

## 架构冲突矩阵

| 上游假设                                              | 澜台边界                                                      | 处理                                          |
| ----------------------------------------------------- | ------------------------------------------------------------- | --------------------------------------------- |
| 上游 Shell 直接拥有窗口、最近文件、重命名和 drop path | 澜台使用自己的窗口模型与受控文件 IPC                          | `S`；仅在澜台边界内重写等价行为               |
| 上游 AI 面板/provider/搜索可直接扩张                  | 沈行知是 Agent 主体，模型只是运行资源；提供商和权限由澜台管理 | 稳定性可 `A1`，新服务一律 `S/E`               |
| 选区请求可让 Agent 直接改正文                         | 澜台要求建议先行、明确接受、冲突校验和撤销                    | 只 `E` 交互原语，不采用直接写入               |
| 上游锚定队列随 AI 面板一起存在                        | 侧栏在澜台降为复杂工作台                                      | DOCX 先做界面内单条随改；多处队列后续独立切片 |
| HTML、CLI、pipeline 和 xlsx gateway 属于同一产品      | 澜台当前产品边界不含这些上游壳层                              | `S` 或按真实套件问题 `D`                      |
| 上游品牌、账户、更新和发布体系可直接沿用              | 澜台只有 Developer ID/GitHub Release 主线                     | `S`                                           |

## 验证与回滚边界

- 每个 `A1` 先增加能在澜台固定点复现的失败测试，再做最小 patch；不能因同一上游 snapshot 而捆绑多个套件。
- DOCX/DOCX Engine 变更必须覆盖 XML round-trip、保护文档、选区冲突和保存重开。
- Slides/PPTX、Sheets、PDF、Markdown 分别运行自己的类型检查、测试和生产构建，不以另一个套件通过代替。
- AI provider/agent-core 只允许稳定性修复，不新增 provider、网络服务、模型默认值或长期记忆写入。
- 任何回移都保留独立提交；出现数据完整性、受控文件、人格或发行边界回归时，可以单项回滚。
- 本审计本身不生成、签名、发布或覆盖最终安装包。

## 本批状态

Phase 0 冻结点与 146 项分类完成。Phase 1 的实现只采用 `6d9b681` 中经适配的界面内原语，并继续复用澜台既有的选区快照、建议生成、冲突检测和确认写回链路；没有合入上游整树、Shell、账户、provider 或默认 AI 侧栏。

Phase 1 当前把范围收敛为“选中—建议—接受为沈行知修订—撤销/重开”的可靠闭环：浮层打开后选区改变或折叠会立即失效，App 发起请求前还会再次校验当前选区；首次生成或二次调整失败都会保留原指令与当前候选稿，并可在范围仍有效时重试；接受使用 DOCX 修订桥而非无痕直接替换；自动化 round-trip 已验证保存重开仍保留“沈行知”修订作者，拒绝修订可恢复原文。第二动作准确命名为“生成并转到清单”，它会立即生成，不冒充 Phase 2 的后台排队能力。真实模型和发行 App 视觉验收仍未完成，因此尚不宣称 Phase 1 已正式退出。
