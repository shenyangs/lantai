# 澜台 · Lantai

一个面向开发者的桌面 AI 助手源码项目：把模型接入、会话与任务运行、本地文件、浏览器操作和办公文档编辑放进同一套应用。

**项目目前暂停主动开发。** 这是供研究、复用和社区接续的源码快照，不是已完成全部验收的稳定产品。已知限制见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。不承诺维护周期、问题响应时间或后续版本。

Lantai is an experimental desktop AI assistant built with Electron, React and Python/FastAPI. This source snapshot is shared for learning, reuse and community forks. Active development is paused; it is **not a fully validated stable release**.

## 可以从这里获得什么

- **一套完整应用的工程参考**：桌面窗口、Python 后端、模型 Provider、流式消息、工具执行和任务状态如何连接。
- **可拆用的实现**：会话恢复、工作区权限、浏览器工作台、办公编辑器集成、研究引用与文档生成。
- **回归问题与边界的记录**：契约和测试覆盖了一些真实工程问题；这些记录也能帮助后来者理解哪些尝试有效、哪些仍未解决。

这里的价值是可阅读、可修改的实现和经验。实际效果取决于模型、权限、操作系统与任务复杂度；功能存在不代表任意任务都能可靠完成。

## 源码运行

建议首先在独立测试目录使用。需要 Python 3.10–3.13、Node.js 22.12+、npm 10+；macOS 原生辅助组件还需要 Xcode Command Line Tools。模型服务需自行配置，可能产生服务商费用。网络访问用于安装依赖和调用你配置的服务。

```bash
git clone https://github.com/shenyangs/lantai.git
cd lantai
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

# 使用隔离的开发数据目录，避免读取或覆盖已安装桌面版的数据。
export LANTAI_WORKING_DIR="$PWD/.local-data/working"
export LANTAI_SECRET_DIR="$PWD/.local-data/working.secret"

cd console
npm ci
npm run build
cd ..
python scripts/check_console_static_fresh.py --sync
lantai init
lantai run
```

随后在本机打开 `http://127.0.0.1:8088`，按界面配置模型。保持上述环境变量在启动进程中生效。密钥、会话和个人文件不属于源码，不能提交到 Git。

前端开发可在 `console` 执行 `npm run dev -- --host 127.0.0.1`，配合运行中的后端。桌面开发入口为 `npm run electron:dev`（同目录），需要相应原生组件与系统权限。Windows 的 shell 激活方式和原生能力不同；本次发布没有重新验证各平台完整安装流程。

`build_and_deploy.sh` 和 `scripts/pack` 保留了原发行工程供参考，其中的签名、公证、证据门禁与部署依赖原维护环境；它们不是社区源码运行的必需步骤。本仓库不附带签名身份、私有发行证据或新的安装包。

## 目录与阅读入口

| 路径 | 内容 |
| --- | --- |
| `src/lantai` | Python 后端、运行时、模型、工具和领域能力 |
| `console` | React 界面与 Electron 桌面层 |
| `console/vendor/lantai-docx-editor` | 办公编辑器集成及上游许可 |
| `tests` | 单元测试、集成测试与回归样本 |
| `contracts` | 基础体验和行为契约 |
| `scripts` | 开发检查与原发行辅助工具 |
| `docs` | 架构与行为设计文档 |

从 [架构指南](docs/ai-architecture-guide-zh.md)、[运行时内核](docs/runtime-kernel-architecture-zh.md) 或 [Electron 主进程](docs/electron-main-architecture-zh.md) 开始。设计文档描述目标和演进背景，不能替代当前版本的验证结果。

## 验证与参与

```bash
python scripts/check_foundation_experience_gate.py
python -m pytest tests/unit/utils/test_redaction.py tests/unit/backup/test_sanitized_export.py
NODE_PATH="$PWD/console/node_modules" node --test scripts/pdf-visibility-lifecycle.test.cjs
```

更大范围的测试需要对应依赖，有些集成检查需要真实外部服务或私有发行材料；不要默认运行全部脚本。公开前的实际检查记录见 [SOURCE_RELEASE.md](SOURCE_RELEASE.md)，参与方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全说明见 [SECURITY.md](SECURITY.md)。欢迎自行 fork，但当前不承诺合并或修复。

## 许可证

澜台自身代码沿用 [Apache-2.0](LICENSE)。第三方代码、模型、字体、引用数据和工具按各自许可证提供；请同时阅读 [NOTICE](NOTICE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，保留对应声明。本仓库不包含旧私有仓库的 Git 历史、运行数据和内部验收资料。
