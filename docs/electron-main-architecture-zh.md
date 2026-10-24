# Electron 主进程架构说明

适用目录：`lantai3`
发布目标：官网 / GitHub Release / Developer ID

本文记录 Electron 主进程的拆分边界。后续 AI 编程助手修改 `console/electron/main.cjs` 前，应先阅读本文和 `docs/ai-architecture-guide-zh.md`。

---

## 当前模块

```text
console/electron/main.cjs
  桌面入口编排层：
  - 初始化各控制器
  - 启动 Python 后端
  - 安装 IPC handlers
  - 创建窗口和托盘
  - 处理 app activate / before-quit / window-all-closed 生命周期

console/electron/appWindowController.cjs
  窗口控制器：
  - 创建 BrowserWindow
  - 注入 preload 与 --lantai-api-base-url 参数
  - 处理 did-fail-load / render-process-gone / console-message 日志
  - 开发模式加载 Vite，正式包加载本地后端 URL
  - 读写 window-state.json
  - Electron 进程每次冷启动时，仅第一个真实应用页携带一次性意图并创建 New Chat；启动状态页不消费该意图
  - 同一进程内从设置返回、切换页面或 macOS 关闭后再开窗口时，恢复该智能体最后会话与草稿；应用退出时允许完整退出

console/electron/trayController.cjs
  托盘控制器：
  - 解析托盘图标路径
  - 创建 18px 托盘图标
  - 注册“显示澜台”和“退出”菜单
  - 点击托盘时恢复或聚焦窗口

console/electron/desktopIpcController.cjs
  通用 IPC 控制器：
  - lantai:select-directory
  - lantai:open-path
  - lantai:computer-permissions-status
  - lantai:computer-permissions-request
  - lantai:app-version（只读本地版本号）

console/electron/macUseController.cjs
  Mac 应用操作桥：
  - 在回环地址提供带短时 HMAC capability 的固定协议
  - 绑定窗口观察收据、去重 action_id 与会话撤销代次
  - 用 desktopCapturer 只捕获选定窗口并记录证据哈希
  - 直接创建包内 Lantai Mac Use Helper.app 子进程，用 stdin/stdout 交换单次 JSON，helper 重验 Electron 父进程
  - 停止时先撤销队列，再关闭桥接

console/electron/desktopSecurityHeaders.cjs
  桌面安全 header 控制器：
  - 对当前本地后端 API 注入 X-Lantai-Desktop-Token
  - 安装逻辑幂等，避免重复注册 webRequest hook

console/electron/backendLifecycleController.cjs
  后端生命周期控制器：
  - 选择端口、生成桌面 token、启动 Python 子进程
  - 绑定 stdout / stderr / exit / close / error
  - 执行健康检查和自动重启 timer
  - 就绪探测超时时保留仍存活的冷启动进程，继续等待并在就绪后自动 reload；仅在进程退出或启动失败后重启
  - 退出时清理后端子进程和重启 timer
  - 通过注入回调处理窗口 reload 与桌面 API header

console/electron/backendProcess.cjs
  纯后端启动工具：
  - Python 后端启动 executable / args / env / cwd
  - 后端 base URL 与健康检查 URL
  - 桌面 API 鉴权 header
  - 临时端口选择、端口地址解析、重启策略判断和 spawn option
  - 后端启动 / 退出日志消息格式

console/electron/runtimePaths.cjs
  纯路径 facade：
  - 项目根目录推导
  - 托盘图标路径
  - window-state 路径
  - 兼容 re-export 后端启动计划，避免旧 import 断裂

console/electron/windowState.cjs
  纯窗口状态工具：
  - 窗口尺寸与坐标边界校验
  - 多显示器可见性判断
  - window-state.json 读写
  - 无效、离屏或缺失状态时回退默认窗口尺寸

```

---

## 依赖方向

`main.cjs` 是唯一入口，负责把 Electron 对象和回调显式注入控制器。控制器之间不互相偷偷读取全局状态。

```text
main.cjs
  -> appWindowController
  -> trayController
  -> desktopIpcController
  -> macUseController
  -> desktopSecurityHeaders
  -> backendLifecycleController

appWindowController -> runtimePaths / windowState
trayController -> runtimePaths
backendLifecycleController -> backendProcess
macUseController -> Lantai Mac Use Helper.app -> AX / CoreGraphics
```

---

## 保持不变的行为

这次拆分只搬结构，不改变：

- IPC channel 名称和返回结构。
- 窗口关闭、隐藏、恢复、尺寸保存行为。
- 后端启动、健康检查和自动重启流程。
- 托盘菜单文字和点击行为。

---

## 修改守则

- 改 `appWindowController.cjs` 后，必须确认正式包窗口加载的是 `backendLifecycle.getApiBaseUrl()`，开发模式加载 Vite。
- 改 `desktopSecurityHeaders.cjs` 后，必须确认 token 只注入当前后端 API，不注入其它 localhost 请求。
- 改 `desktopIpcController.cjs` 时，应保留仍受支持的 IPC channel 名称和返回结构。
- 改 `macUseController.cjs` 或 Mac Use helper 后，必须验证回环鉴权、目标/观察绑定、输入互斥、取消、通用二进制架构、成品包路径和实际 helper 权限归因。
- 涉及 `app.quit()`、后端子进程、窗口关闭时，必须跑桌面 smoke 或明确说明没有跑。
- 自动更新控制器、更新 IPC、更新 preload API 和更新依赖已移除；新增版本只能保留本地版本号只读展示，不得恢复后台检查、下载或安装。
