# GitHub CLI 能力与权限矩阵

## 首次运行

1. 用 `gh --version` 探测，不假设已安装。
2. 用 `gh auth status` 判断认证状态，不读取或打印认证文件。
3. 缺失时优先让用户按 [GitHub CLI 官方安装说明](https://cli.github.com/) 安装：macOS 可在已有 Homebrew 时运行 `brew install gh`，Windows 可运行 `winget install --id GitHub.cli`，Linux 使用官方发行版对应的软件源。
4. 不静默安装包管理器、运行时或 GitHub CLI；可回退到已配置连接器或浏览器。

## 常用操作

| 目的 | 示例 | 默认权限 |
|---|---|---|
| 查看 Issue | `gh issue view NUMBER --comments` | 只读 |
| 查看 PR | `gh pr view NUMBER --comments` | 只读 |
| 查看检查 | `gh pr checks NUMBER` | 只读 |
| 查看失败日志 | `gh run view RUN_ID --log-failed` | 只读 |
| 查看 diff | `gh pr diff NUMBER` | 只读 |
| 创建 PR | `gh pr create` | 需授权 |
| 回复评论 | `gh pr comment NUMBER --body ...` | 需授权 |
| 重跑工作流 | `gh run rerun RUN_ID` | 需授权 |
| 合并 PR | `gh pr merge NUMBER` | 单独授权 |

优先传显式仓库 `--repo OWNER/REPO` 和显式编号，避免对错误仓库操作。任何正文先在本地构造并检查是否包含凭证、个人信息和内部路径。
