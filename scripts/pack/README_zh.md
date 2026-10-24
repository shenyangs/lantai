# Lantai Desktop 打包脚本

一键打包：脚本会先运行 `scripts/wheel_build.sh` 构建 **wheel**
（包含 console 前端产物），再用 **临时 conda 环境** + **conda-pack**
（不依赖当前开发环境）。依赖以 `pyproject.toml` 为准。

- **Windows**: wheel → conda-pack → 解压 → NSIS 安装包 (`.exe`)
- **macOS**: wheel → conda-pack → 解压到 `.app` → 可选打 zip

## 系统要求

- **Windows**: Windows 10 或更高版本
- **macOS**: macOS 14 (Sonoma) 或更高版本，推荐 Apple Silicon (M1/M2/M3/M4)

## 前置

- **conda**（Miniconda/Anaconda）在 PATH
- **Node.js / npm**（用于构建 console 前端）
- （仅 Windows）**NSIS**：`makensis` 在 PATH
- **图标**：预生成的 `icon.ico` (Windows) 和 `icon.icns` (macOS) 已包含在 `scripts/pack/assets/` 中

## 一键打包

在**仓库根目录**执行：

正式产出发版包前，先跑 vibe-coding 聚焦检查：

```bash
python scripts/vibe_coding_preflight.py --full
```

它会在打包前覆盖运行事件连续性、预览诊断、修复提示、前端聚焦测试和 console 生产构建。

**macOS**
```bash
bash ./scripts/pack/build_macos.sh
# 产出: dist/Lantai.app

CREATE_ZIP=1 bash ./scripts/pack/build_macos.sh   # 同时生成 .zip
```

**Windows (PowerShell)**
```powershell
./scripts/pack/build_win.ps1
# 产出: dist/Lantai-Setup-<version>.exe
# 创建两个启动器：
#   - Lantai Desktop.vbs (静默启动，无终端窗口)
#   - Lantai Desktop (Debug).bat (显示终端，便于调试)
```

## 从终端启动并查看日志（macOS）

如果双击 .app 会闪退，可以在终端里运行以查看完整报错和日志：

```bash
# 在仓库根目录执行，强制只用打包环境（不用系统 conda / PYTHONPATH）。路径按需改。
APP_ENV="$(pwd)/dist/Lantai.app/Contents/Resources/env"
PYTHONNOUSERSITE=1 PYTHONPATH= PYTHONHOME="$APP_ENV" "$APP_ENV/bin/python" -m lantai desktop
```

`PYTHONNOUSERSITE=1` 可防止 Python 加载 `~/.local/lib/pythonX.Y/site-packages` 中的包，避免与打包环境冲突。所有标准输出和错误（包括 Python traceback）都会打在终端里；可加 `--log-level debug` 查看更详细日志。

若**双击** .app 没有任何窗口出现，启动器会把 stderr/stdout 写入 `~/.lantai/desktop.log`，可打开该文件查看报错。

首次打开时，macOS 可能弹出「请求访问桌面的文件」：请点**允许**，否则部分功能可能不可用或窗口会关闭。

## macOS 签名与公证

官网版 `lantai3` 的正式分发包应当满足：

- 使用 `Developer ID Application` 签名
- 使用 Apple `notarytool` 完成公证
- 在分发前完成 staple，让 `.app`、`.zip`、`.dmg` 带上票据

`build_and_deploy.sh` 现在会强制检查这条链路。它支持以下任一公证凭据来源：

- `LANTAI_NOTARY_KEYCHAIN_PROFILE` 或 `APPLE_KEYCHAIN_PROFILE`
- `APPLE_ID` + `APPLE_APP_SPECIFIC_PASSWORD` + `APPLE_TEAM_ID`
- `APPLE_API_KEY` + `APPLE_API_KEY_ID` + `APPLE_API_ISSUER`

如果没有 Developer ID 证书或没有公证凭据，脚本会直接失败，而不是继续产出一个在新 Mac 上容易白屏或拉不起后端的坏包。只有在明确需要“本机自测包”时，才可设置 `LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1`。

## 官网正式发布

官网版使用公开仓库 [`shenyangs/lantai3-releases`](https://github.com/shenyangs/lantai3-releases) 发布安装包。`build_and_deploy.sh` 会生成 `.app`、`.dmg` 和 `.zip`；只有在明确要求发布时，才会上传公开 GitHub Release。桌面应用本身不检查、下载或安装新版本。

日常开发构建和本机 smoke **不要**带发布参数。正式发版必须满足以下条件：源码工作区干净、`v<version>` 签名 Tag 指向当前提交、Developer ID 签名、App 与 DMG 公证均为 `Accepted`、Gatekeeper 与发布证据门禁均通过。

首次在这台 Mac 上发布，登录官方 GitHub CLI：

```bash
/Users/example/.local/bin/gh auth login --hostname github.com --git-protocol https --web
```

正式发布命令：

```bash
./build_and_deploy.sh --no-launch --publish-github-release
```

脚本会在上传前后运行版本链路审计，并上传 `.dmg`、`.zip`、SBOM、供应链清单和产物清单。Release Tag 固定为 `v<version>`，已存在的 Tag 会直接失败，避免覆盖已发布制品。若本机 `gh` 命令被其他 Node 工具占用，可显式指定：

```bash
LANTAI_GITHUB_CLI=/Users/example/.local/bin/gh ./build_and_deploy.sh --no-launch --publish-github-release
```

## macOS：内部自测包被 Gatekeeper 拦截时怎么打开

刻意跳过公证的内部自测包，仍然可能看到「Apple 无法验证“Lantai”是否包含可能危害 Mac 安全或泄漏隐私的恶意软件」。可按以下方式打开：

- **右键打开（推荐）**
  在 Lantai 应用上 **右键（或 Control + 点击）** → 选 **「打开」** → 在弹窗里再点一次 **「打开」**。即表示你确认运行该应用，Gatekeeper 会放行，之后双击即可正常打开。

- **在系统设置里放行**
  若仍被拦截，进入 **系统设置 → 隐私与安全性**，往下找到类似「已阻止使用 Lantai，因为无法验证开发者」的提示，点 **「仍要打开」** 或 **「允许」** 即可。

- **用终端去掉隔离属性（不推荐普通用户）**
  在终端执行：`xattr -cr /Applications/Lantai.app`（或解压后 .app 的实际路径）。会去掉「从互联网下载」的隔离标记，一般就不再弹恶意软件提示，但不如「右键 → 打开」安全、可控。

## CI

`.github/workflows/desktop-release.yml`：

- **触发**: Release 发布 或 手动 workflow_dispatch
- **Preflight**: 在 macOS/Windows 打包前运行 `python scripts/vibe_coding_preflight.py --full`
- **Windows**: 构建 console → 临时 conda 环境 + conda-pack → NSIS → 上传 artifact
- **macOS**: 构建 console → 临时 conda 环境 + conda-pack → .app → zip → 上传 artifact
- **Release**: 将桌面端产物发布到 `shenyangs/lantai3-releases`

## 脚本说明

| 文件 | 说明 |
|------|------|
| `build_common.py` | 创建临时 conda 环境，从 wheel 安装 `lantai[full]`，conda-pack 产出归档 |
| `build_macos.sh` | 一键：构建 wheel → build_common → 解压到 Lantai.app；可选打 zip |
| `build_win.ps1` | 一键：构建 wheel → build_common → 解压 → 创建 VBS/BAT 启动器 → makensis 安装包 |
| `desktop.nsi` | NSIS 脚本：打包 `dist/win-unpacked`，添加图标，创建快捷方式 |
| `assets/icon.ico` | 预生成的 Windows 图标（安装包和快捷方式使用） |
| `assets/icon.icns` | 预生成的 macOS 图标（应用包使用） |
