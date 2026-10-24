---
# id: lantai-core-tools (migrated to name)
name: lantai-core-tools
description: 使用澜台工作区文件、Shell、搜索和基础系统工具完成本地读写、命令执行与产物交付，并遵守路径、权限和破坏性操作边界。任务需要操作本地文件或运行命令且没有更专门技能时使用；纯知识问答、外部 SaaS 操作或未经确认的删除覆盖不要触发。
kind: core_tool
domain: system_runtime
version: 1.0.1
source: builtin
enabled: true
locked: true
permission_profile: workspace_read_auto_write_approval
recommended_agents:
- latte
inputs:
- workspace_path
- user_request
outputs:
- command_result
- file_patch
- search_hits
memory_writeback:
- tool_boundary
- failure_mode
tags:
- 澜台内置
- Shell
- 文件
- 搜索
---

# 澜台 Core Tools

适用场景：需要执行命令、读取文件、编辑文件、在工作区搜索路径或文本时触发。
输出格式：工具结果 + 审计日志 + 可回滚变更说明

## 澜台 工具映射
- `bash`：在 Shell 中执行命令，运行脚本、检查文件、安装依赖。
- `read_file`：读取文件内容，支持偏移和行数限制。
- `list_dir`：列出目录下的文件和子目录。
- `write_file`：创建或覆盖文件内容。
- `edit_file`：精确替换文件中的指定文本片段。
- `grep`：使用 ripgrep 在文件中搜索文本模式。
- `glob`：使用 glob 模式匹配文件路径。

## WorkflowGraph 节点
- classify
- permission_check
- execute_tool
- capture_trace
- summarize_result
- writeback_if_needed

## 权限规则
- 读操作可在工作区内自动执行。
- 写文件、覆盖文件、安装依赖、删除、发布、强推必须进入确认链。
- Shell 命令必须先走危险模式分类，再执行。

## 失败模式
- 路径不在工作区时停止并说明边界。
- 工具输出过长时进入上下文裁剪，不把原始大输出塞回对话。

## 自恢复规则
- Shell 失败后先读取返回中的 `[lantai:recovery]` 和结构化恢复信息，改变执行路径后继续原任务，不重复原命令。
- 用户明确要求安装或接入外部 CLI 时，不以“必须先进入澜台界面”为由停止。优先使用厂商安装器或标准包管理器做用户级/项目级安装，验证 `command -v`、版本和一个无副作用命令后继续原任务；除非用户明确要求，不使用 `sudo`、不覆盖系统运行时。澜台的权限确认发生在有副作用的工具边界，不等于禁止 Agent 完成已获授权的安装任务。
- Office 文件优先使用 `read_file` 或 OfficeCLI；不得为了只读文档而要求用户手工安装 Python 库。
- Python 出现 PEP 668、pip 缺失或模块缺失时，禁止破坏系统 Python；改用工作区虚拟环境及其显式 Python 路径。权限允许时由 Agent 自行恢复，不把命令复制粘贴工作转交给非技术用户。
