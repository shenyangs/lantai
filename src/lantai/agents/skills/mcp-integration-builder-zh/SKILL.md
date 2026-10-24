---
name: mcp-integration-builder
description: 为澜台设计、接入、探测或调试 MCP 客户端与服务端集成，处理 stdio、Streamable HTTP、SSE、OAuth、工具 schema、白名单和生命周期错误。用户要求“接一个 MCP”“开发/调试 MCP Server”“为什么 MCP 工具不可用”“限制 MCP 工具权限”时使用；只调用已经正常工作的 MCP 工具或安装普通 Skill 时不要触发。
---

# MCP 集成构建器

先确定是在“把现有 MCP 服务接入澜台”，还是“实现一个新的 MCP 服务”。能复用可信连接器时不重复造服务端；必须开发服务端时，先把工具合同和安全边界写清楚。

## 1. 定义集成合同

确认：

- 目标能力和不需要暴露的能力；
- transport：`stdio`、`streamable_http` 或 `sse`；
- stdio 的 command、args、cwd、env，或远程服务的 URL、headers、OAuth；
- 每个工具的名称、描述、输入 schema、输出和错误合同；
- 读取、写入、删除、执行、网络和私网访问边界；
- 超时、重试、幂等、取消、审计和降级；
- 最小 `allowed_tools` 白名单及启用条件。

具体检查项读取 [transport-and-security.md](references/transport-and-security.md)。

## 2. 首次运行与依赖

先探测服务声明的运行时和可执行文件，例如 `python3 --version`、`node --version` 或具体命令的 `--version`。缺失时给出该服务官方安装路径和替代方案，不静默安装运行时、包管理器或远程代码。

凭证使用澜台连接器配置、环境变量或 OAuth；不得把真实秘密写进 Skill、示例、日志、命令历史或聊天正文。

## 3. 安全接入现有服务

1. 审核来源、版本、许可、启动命令和数据去向；
2. 先以 `enabled=false` 创建连接器；
3. 远程 transport 默认禁止私网访问，只有明确业务需要并授权时设置 `allow_private_network=true`；
4. 配置最小 `allowed_tools`，不要用空白白名单无意放开全部工具；
5. 启用后调用连接器 `probe`，再读取 lifecycle 和工具列表；
6. 用一个无副作用工具验证 schema、返回值、超时和错误；
7. 最后才测试经授权的写操作。

澜台连接器生命周期区分 disabled、configuration required、authorization required、connecting、ready 和 error；错误按 authorization、configuration、connection、server 分类。不要把所有失败都归因于“网络”。

## 4. 实现新 MCP 服务

从一个只读工具和一个资源/健康检查开始，使用官方 MCP SDK 与固定版本。工具描述写清输入、外部副作用、幂等和错误；服务端自行校验路径、URL、权限与参数，不能依赖模型“自觉不越权”。

为正常调用、非法 schema、超时、取消、重复调用、凭证缺失、服务重启和敏感输出准备测试。日志记录请求标识、工具名、耗时和错误码，但不记录秘密或完整敏感负载。

## 5. 调试与交付

按配置、认证、连接、服务端、schema、权限白名单和宿主状态逐层排查。保留 probe 时间、lifecycle 状态、工具清单和最小调用结果；修复后从禁用状态重新验证，不直接在生产数据上试错。

交付包括连接器/服务规格、依赖与版本、威胁边界、最小工具白名单、探测证据、正负测试、启用与回滚步骤。删除连接器、扩大私网访问、开放写工具或更新凭证必须得到明确授权。

## 完成定义

来源和运行时可解释，连接器先禁用后探测，凭证没有泄漏，私网与工具权限最小化，至少一个只读调用实际通过，失败分类和回滚步骤明确，写工具未经授权不会执行。
