# 澜台 Mac 应用操作架构

状态：已实现受控基线
适用发行版：官网 Developer ID 版
更新日期：2026-09-21

## 用户从哪里使用

主聊天输入区不常驻 Mac 应用操作按钮。用户直接用自然语言说明任务，由当前主模型结合完整上下文判断是否需要 Mac Use；模型先检查应用操作能力后，对应工具卡才按需提供“选择要操作的应用”。用户点击并选择一个真实 App 窗口后，目标会以可移除的上下文条显示，并按对话保存。前端不以关键词、正则或本地分类器猜测意图，写操作仍必须经过 Runtime 权限链。

工具卡区分：正在执行、已读取、已核验、待核验、未执行。`dispatch_status=sent` 不会被展示成任务成功；`effect_status=ambiguous` 明确告诉用户需要先核对。

## 运行边界

```text
Chat 自然语言任务
  -> 当前主模型理解任务并检查 Mac Use 能力
  -> 对应工具卡按需请求目标选择
  -> mac_inspect / mac_observe / mac_act / mac_verify / mac_session
  -> Runtime 权限、审批和 Effect Journal
  -> Python MacUseService
  -> 鉴权回环 HTTP bridge
  -> Electron macUseController
  -> 随包签名 Lantai Mac Use Helper.app
  -> 用户选定的 App/窗口
```

它复用 `RuntimeRun` / `RuntimeEvent` / `RuntimeEffectJournal` 和原工具审批，不建第二套任务库或桌面规划模型。选中窗口只是目标事实，不是权限事实。界面里读到的文字只是数据，不能改变系统指令或扩大任务范围。

## 原生组件

`console/electron/native/mac-use-helper/main.swift` 编译为 macOS 14+ 的 arm64/x86_64 通用 App helper，用固定 JSON v1 协议接受单个动作，不开公网端口，不接受 shell/eval/任意 URL。Electron 直接创建 helper 子进程，请求只经 stdin 传入，响应只经 stdout 返回；helper 核对父进程必须是同一成品 App 内的澜台 Electron，开发态则必须是仓库固定 Electron 路径。第三方进程直接调 helper 只能读取无效果的 `status`。

当前原生通道：

- 窗口列表和 App/bundle/PID/进程启动时间/窗口 ID/几何信息；
- Electron `desktopCapturer` 按已选窗口获取 PNG，helper 作开发态后备捕获；
- AX 控件树、精确元素路径、值读取、设值与 `AXPress`；
- 绑定窗口的前台定位、归一化点击、Unicode 文本和少量按键白名单；
- 原子 `flock` 共享输入互斥，合成事件尾迹与用户正在操作的区分；
- 会话暂停、恢复、停止；取消标记同时绑定 controller 生命期随机代次和会话撤销代次，恢复不会复活旧 helper，重启也不会误用旧取消文件。

输入类动作不提供 `force`。`type_text` 必须绑定观察到的、已启用且非敏感的 `AXTextArea` 或 `AXTextField`；按钮、链接和其他控件不接受全局文本事件。用户活动检测覆盖键盘、修饰键、鼠标移动/拖拽/左右与其他按键、滚轮/触控板滚动；长文本发送也在分块之间重验，检测到用户接管即停止。终端和 IDE 中的 Enter/Return，以及文本中的 CR/LF/Unicode 行分隔符，都在聚焦或发送输入前被 helper 拒绝。密码、Touch ID、钥匙串、TCC 授权和支付窗口不出现在选择器中，也不能签发目标收据。

## 目标、观察与防重

写操作必须携带最近 120 秒内的 `observation_id`。收据绑定 run、bundle ID、PID、进程启动时间、window ID、窗口几何和撤销代次。暂停或停止会增加代次；恢复后必须重新观察。当 App 不提供 `AXWindowNumber` 时，helper 只接受 AX 几何与已选 CGWindow 唯一匹配的窗口，同名或未命名文字不作为回退身份。元素写入不信任模型转述的 role/title，Electron 从原观察收据注入 role、subrole、title、identifier 和 bounds 指纹，helper 逐项重验后才写入。动作前 helper 还会重验 App/窗口、几何、遮挡、用户活动和取消状态。

`action_id` 在 Electron 进程内去重，运行层的 `effect_id` 进入原有 Effect Journal。GUI 写入默认 `not_idempotent + probe_required`。已发送后连接中断返回 `dispatch_status=unknown` / `effect_status=ambiguous` / `retry_safe=false`，只能先 `mac_verify` 或 `mac_observe`。

结果状态分三个维度：

| 字段 | 含义 |
|---|---|
| `policy_status` | 是否获准 |
| `dispatch_status` | 动作是否可确定地发出 |
| `effect_status` | 应用结果是否被独立读回验证 |

像素或 DOM 变化只能作为 `observed_change`，不能自动推导 `verified`。整个任务是否完成继续由 LoopContract 验收。

## 权限与数据

“电脑权限”设置页同时读取主 App 和 Mac Use helper 的真实状态。用户只在需要时点击请求：辅助功能由 helper 触发 macOS 标准提示；窗口读取由 Electron 触发屏幕录制权限评估。“系统已授权”和“当前任务允许操作这个窗口”是两个独立条件。

默认只截已选窗口。中间截图写入应用的 `mac-use/evidence` 受控目录，模型不能指定路径；bridge 启动及每次观察前都清理超过 24 小时且符合精确命名格式的普通 PNG，不跟随符号链接。每张证据记录字节数和 SHA-256。界面截图会按当前 Provider 和任务路径进入模型上下文；本机工具不代表云端 Provider 看不到内容。

## 当前支持边界

已实现的通用机制可用于开发和实验；单个第三方 App 只有在对应版本/动作/环境通过真机矩阵后才能标记“已验证”。当前 TextEdit 临时文档的控件观察、指纹设值和读回已通过。系统不自动重启第三方 Electron App 开启 CDP，不扫描未知端口，锁屏状态为已锁定或无法确定时都拒绝写入，也不把通用坐标操作宣传为任意 App 保证成功。

如果文件、OfficeCLI、正式 AppleScript/CLI 或浏览器工具已能更可靠地完成，应继续走原有路径。Mac Use 主要补足只存在于实际 App 窗口和控件中的状态。

## 验证

```bash
cd console
npm run build:mac-use-helper
node --test electron/macUseController.test.cjs electron/packageAllowlist.test.cjs
npm run smoke:mac-use
npm run smoke:mac-use-lock
npm run smoke:mac-use-textedit
npm exec -- vitest run src/pages/Chat/ChatPage.test.tsx src/pages/Chat/components/MacUsePicker.test.tsx src/pages/Chat/components/MacUseToolCard.test.tsx

cd ..
.venv/bin/python -m pytest tests/unit/app/test_mac_use.py tests/unit/routers/test_router_mac_use.py tests/unit/security/test_tool_policy.py -q
python3 scripts/check_foundation_experience_gate.py
```

正式发布还必须执行 `build_and_deploy.sh` 的 helper 通用架构、版本、签名、成品包 smoke、SBOM/二进制来源和全部基础体验门禁。

## 来源

分层控制、后台观察和操作后取证思路参考 [huashu-mac-use](https://github.com/alchaincyf/huashu-mac-use) 的 `b51b3701507a72ce07d3609059da3d4b92fa14e5` 版本。澜台的协议、helper、权限、Runtime 和 UI 为独立实现，不带入上游 skill/CLI/坐标档案或运行时依赖。来源和 MIT 许可信息见 `../THIRD_PARTY_NOTICES.md`。
