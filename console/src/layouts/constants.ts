// ── URLs ──────────────────────────────────────────────────────────────────

export const PYPI_URL = "https://pypi.org/pypi/lantai/json";

export const GITHUB_URL =
  "https://github.com/shenyangs/lantai3-releases" as const;

// ── Timing ────────────────────────────────────────────────────────────────

export const ONE_HOUR_MS = 60 * 60 * 1000;

// ── Navigation ────────────────────────────────────────────────────────────

export const DEFAULT_OPEN_KEYS = [
  "chat-group",
  "control-group",
  "agent-group",
  "settings-group",
];

export const KEY_TO_PATH: Record<string, string> = {
  chat: "/chat",
  works: "/works",
  "daily-brief": "/daily-brief",
  reminders: "/today",
  channels: "/channels",
  sessions: "/sessions",
  "cron-jobs": "/cron-jobs",
  heartbeat: "/heartbeat",
  skills: "/skills",
  "skill-pool": "/skill-pool",
  tools: "/tools",
  capabilities: "/capabilities",
  mcp: "/mcp",
  workspace: "/workspace",
  memory: "/memory",
  agents: "/agents",
  models: "/models",
  settings: "/settings",
  environments: "/environments",
  "agent-config": "/agent-config",
  "advanced-parameters": "/advanced-parameters",
  permissions: "/permissions",
  security: "/security",
  "token-usage": "/token-usage",
  "agent-stats": "/agent-stats",
  debug: "/debug",
  backups: "/backups",
};

export const KEY_TO_LABEL: Record<string, string> = {
  chat: "nav.chat",
  works: "nav.works",
  "daily-brief": "nav.dailyBrief",
  reminders: "nav.reminders",
  channels: "nav.channels",
  sessions: "nav.sessions",
  "cron-jobs": "nav.cronJobs",
  heartbeat: "nav.heartbeat",
  skills: "nav.skills",
  "skill-pool": "nav.skillPool",
  tools: "nav.tools",
  capabilities: "nav.capabilities",
  mcp: "nav.mcp",
  "agent-config": "nav.agentConfig",
  workspace: "nav.workspace",
  memory: "nav.memory",
  models: "nav.models",
  settings: "nav.settings",
  environments: "nav.environments",
  "advanced-parameters": "nav.advancedParameters",
  permissions: "nav.permissions",
  security: "nav.security",
  "token-usage": "nav.tokenUsage",
  "agent-stats": "nav.agentStats",
  agents: "nav.agents",
  debug: "nav.debug",
  backups: "nav.backups",
};

// ── URL helpers ───────────────────────────────────────────────────────────

export const getWebsiteLang = (lang: string): string =>
  lang.startsWith("zh")
    ? "zh"
    : lang.startsWith("ja")
      ? "ja"
      : lang.startsWith("ko")
        ? "ko"
        : "en";

export const getDocsUrl = (lang: string): string =>
  `${GITHUB_URL}/blob/main/README_${getWebsiteLang(lang)}.md`;

export const getFaqUrl = (_lang: string): string => `${GITHUB_URL}/issues`;

export const getReleaseNotesUrl = (_lang: string): string =>
  `${GITHUB_URL}/releases`;

// ── Version helpers ────────────────────────────────────────────────────────

// Filter out pre-release versions; post-releases are treated as stable.
// PEP 440 pre-release suffixes: aN / bN / rcN (or cN) / devN.
export const isStableVersion = (v: string): boolean =>
  !/(\d)(a|alpha|b|beta|rc|c|dev)\d*/i.test(v);

// Compare two PEP 440 version strings. Returns >0 if a>b, <0 if a<b, 0 if equal.
// .postN releases sort after their base version (e.g. 1.0.0.post1 > 1.0.0).
// Pre-release versions (aN, bN, rcN) sort before their base version.
export const compareVersions = (a: string, b: string): number => {
  const normalise = (v: string): number[] => {
    // Handle .postN suffix
    const postMatch = v.match(/\.post(\d+)$/i);
    const postNum = postMatch ? Number(postMatch[1]) : 0;
    const baseVersion = v.replace(/\.post\d+$/i, "");

    // Handle pre-release suffix (e.g., 1.0.1b1 -> base=1.0.1, preType=b, preNum=1)
    const preMatch = baseVersion.match(/^(.+?)(a|alpha|b|beta|rc|c)(\d*)$/i);
    let coreVersion = baseVersion;
    let preType = 0; // 0 = stable, -3 = alpha, -2 = beta, -1 = rc
    let preNum = 0;
    if (preMatch) {
      coreVersion = preMatch[1];
      const preLabel = preMatch[2].toLowerCase();
      preType =
        preLabel === "a" || preLabel === "alpha"
          ? -3
          : preLabel === "b" || preLabel === "beta"
            ? -2
            : -1; // rc or c
      preNum = preMatch[3] ? Number(preMatch[3]) : 0;
    }

    const parts = coreVersion.split(/[.-]/).map((seg) => Number(seg) || 0);
    // Append: preType (0 for stable, negative for pre-release), preNum, postNum
    return [...parts, preType, preNum, postNum];
  };

  const aN = normalise(a);
  const bN = normalise(b);
  const len = Math.max(aN.length, bN.length);
  for (let i = 0; i < len; i++) {
    const diff = (aN[i] ?? 0) - (bN[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
};

// ── Update markdown ───────────────────────────────────────────────────────
export const UPDATE_MD: Record<string, string> = {
  zh: `### 澜台如何更新

要更新 澜台 到最新版本，可根据你的安装方式选择对应方法：

1. 如果你使用的是一键安装脚本，直接重新运行安装命令即可自动升级。

2. 如果你是通过 pip 安装，在终端中执行以下命令升级：

\`\`\`
lantai update
\`\`\`

3. 如果你是从源码安装，进入项目目录并拉取最新代码后重新安装：

\`\`\`
cd lantai
git pull origin main
cd console && npm ci && npm run build
cd .. && mkdir -p src/lantai/console
cp -R console/dist/. src/lantai/console/
pip install -e .
\`\`\`

4. 如果你使用的是 Docker，拉取最新镜像并重启容器：

\`\`\`
docker pull ghcr.io/shenyangs/lantai:latest
docker run -p 127.0.0.1:8088:8088 -v lantai-data:/app/working ghcr.io/shenyangs/lantai:latest
\`\`\`

升级后重启服务 lantai app。`,

  en: `### How to update Lantai

To update Lantai, use the method matching your installation type:

1. If installed via one-line script, re-run the installer to upgrade.

2. If installed via pip, run:

\`\`\`
lantai update
\`\`\`

3. If installed from source, pull the latest code and reinstall:

\`\`\`
cd lantai
git pull origin main
cd console && npm ci && npm run build
cd .. && mkdir -p src/lantai/console
cp -R console/dist/. src/lantai/console/
pip install -e .
\`\`\`

4. If using Docker, pull the latest image and restart the container:

\`\`\`
docker pull ghcr.io/shenyangs/lantai:latest
docker run -p 127.0.0.1:8088:8088 -v lantai-data:/app/working ghcr.io/shenyangs/lantai:latest
\`\`\`

After upgrading, restart the service with \`lantai app\`.`,

  ja: `### Lantai の更新方法

インストール方法に合わせて更新してください。

1. インストールスクリプトを使った場合は、同じインストーラーを再実行します。

2. pip でインストールした場合:

\`\`\`
lantai update
\`\`\`

3. ソースからインストールした場合:

\`\`\`
cd lantai
git pull origin main
cd console && npm ci && npm run build
cd .. && mkdir -p src/lantai/console
cp -R console/dist/. src/lantai/console/
pip install -e .
\`\`\`

4. Docker の場合:

\`\`\`
docker pull ghcr.io/shenyangs/lantai:latest
docker run -p 127.0.0.1:8088:8088 -v lantai-data:/app/working ghcr.io/shenyangs/lantai:latest
\`\`\`

更新後は \`lantai app\` を再起動してください。`,

  ko: `### Lantai 업데이트 방법

설치 방식에 맞춰 업데이트하세요.

1. 설치 스크립트로 설치했다면 같은 설치 명령을 다시 실행합니다.

2. pip 로 설치했다면:

\`\`\`
lantai update
\`\`\`

3. 소스에서 설치했다면:

\`\`\`
cd lantai
git pull origin main
cd console && npm ci && npm run build
cd .. && mkdir -p src/lantai/console
cp -R console/dist/. src/lantai/console/
pip install -e .
\`\`\`

4. Docker 를 사용한다면:

\`\`\`
docker pull ghcr.io/shenyangs/lantai:latest
docker run -p 127.0.0.1:8088:8088 -v lantai-data:/app/working ghcr.io/shenyangs/lantai:latest
\`\`\`

업데이트 후 \`lantai app\` 서비스를 다시 시작하세요.`,
};
