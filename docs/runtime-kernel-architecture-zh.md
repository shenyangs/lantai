# 澜台 Runtime Kernel 架构

日期：2026-06-01

## 目标

Runtime Kernel 是澜台自己的 Agent 执行内核，负责把一次用户输入转成可观察、可回放、可治理的 turn。它不依赖特定第三方 Agent 框架，核心边界由澜台自己的消息、事件、工具、模型和状态协议组成。

## 分层

```text
Console / Channels / TaskCenter / API Routers
        |
RuntimeRunner / AgentService
        |
RuntimeAdapter
        |
NativeLantaiRuntime
  - turn loop
  - RuntimeEvent stream
  - tool call/result loop
  - approval/policy
  - memory/context write-back
        |
LantaiModelClient / LantaiToolRegistry / State Stores
```

应用层只关心 `RuntimeRunner` 和 `RuntimeEvent`，不直接调用具体框架对象。运行时实现可以是 native，也可以是保守兼容路径；二者必须投影到同一组 runtime contract。

## 核心协议

- `RuntimeMessage`：统一表达 system/user/assistant/tool 消息，content 使用结构化 block。
- `RuntimeEvent`：统一表达 turn、message、tool、approval、memory、error 等运行中事件。
- `LantaiToolRegistry`：统一注册内置工具、skills、MCP、memory tools 和插件工具。
- `LantaiModelClient`：统一模型请求、正文流、thinking 流、tool call、usage 和 retry。
- `RuntimeAdapter`：执行一个 turn，并把实现细节压缩成 RuntimeEvent 流。

## Native Runtime

`NativeLantaiRuntime` 实现最小 ReAct turn loop：

1. 加载 session、历史消息、上下文和可用工具。
2. 通过 `LantaiModelClient.stream()` 逐字输出正文和 thinking。
3. 解析 tool call，交给 `LantaiToolRegistry` 和 policy middleware。
4. 写入 tool result，继续模型循环或完成 turn。
5. 将 message、tool、approval、memory 更新写回 session ledger。

Native Runtime 是目标路径；兼容路径只用于回滚、对齐和高风险边缘能力的缓冲。

## 单发行版原则

- `lantai3` 是唯一开发、验证、构建和发布源。
- 所有 native/canary 切换、回滚和运行时验收只以官网/Developer ID 版为准。
- 已归档的 App Store/TestFlight 目录不得再作为同步目标、兼容门槛或发布阻塞项。

## 回滚与观测

- `runtime.engine` 保留 fallback 配置，出现 native 异常时可切回兼容路径。
- 双运行时对比只允许 fixture/replay 或无副作用场景，避免真实 shell、file、browser 工具被重复执行。
- 关键 smoke 覆盖普通聊天、thinking 展开、正文流式、tool call/result、approval、cancel/interrupt、后端启动和 console build。
