import fs from "node:fs";
import { createHash } from "node:crypto";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const [baseUrlArg, outputDirArg, labelArg, profileArg, routeFilterArg] =
  process.argv.slice(2);
const baseUrl = baseUrlArg || "http://127.0.0.1:5173/";
const desktopToken = String(process.env.LANTAI_UI_REVIEW_DESKTOP_TOKEN || "");
const outputDir = path.resolve(
  outputDirArg || "../docs/assets/ui-review/2026-07-23",
);
const label = labelArg || "baseline";
const profile = profileArg || "core";
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDir, "../..");
const brandSourceFiles = [
  "console/vite.config.ts",
  "console/scripts/capture-ui-review-chrome.mjs",
  "console/src/components/ApprovalCard/ApprovalCard.module.less",
  "console/src/components/ApprovalCard/ApprovalCard.tsx",
  "console/src/pages/Chat/components/ChatArtifactPanel.module.less",
  "console/src/pages/Chat/components/ChatArtifactPanel.tsx",
  "console/src/pages/Chat/components/DailyBriefHomeShelf.tsx",
  "console/src/pages/Chat/components/Kdocs/Kdocs.module.less",
  "console/src/pages/Chat/components/Kdocs/KdocsWorkspacePanel.tsx",
  "console/src/pages/Chat/index.module.less",
  "console/src/pages/Chat/index.tsx",
  "console/src/pages/Agent/MCP/components/OfficialConnectorCatalog.tsx",
  "console/src/pages/Agent/MCP/index.module.less",
  "console/src/pages/Agent/MCP/index.tsx",
  "console/src/pages/Settings/Appearance/index.module.less",
  "console/src/pages/Settings/Appearance/index.tsx",
  "console/src/pages/Settings/InnovationLab/index.tsx",
  "console/src/styles/base/tokens.css",
  "console/src/theme/appearance.ts",
];

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function captureSourceFingerprint() {
  const files = brandSourceFiles.map((relativePath) => {
    const absolutePath = path.join(projectRoot, relativePath);
    return {
      path: relativePath,
      sha256: sha256(fs.readFileSync(absolutePath)),
    };
  });
  return {
    algorithm: "sha256",
    gitHead: execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: projectRoot,
      encoding: "utf8",
    }).trim(),
    digest: sha256(
      Buffer.from(
        files.map((entry) => `${entry.path}\0${entry.sha256}`).join("\n"),
      ),
    ),
    files,
  };
}
const imageGenerationFixturePath = path.resolve(
  scriptDir,
  "../../docs/assets/ui-review/fixtures/image-generation-result.png",
);
const imageGenerationFixtureDataUrl = fs.existsSync(imageGenerationFixturePath)
  ? `data:image/png;base64,${fs.readFileSync(imageGenerationFixturePath).toString("base64")}`
  : "";

const coreRoutes = [
  { id: "chat", path: "/chat", expectedText: "今天想推进" },
  { id: "settings", path: "/settings", expectedText: "关于澜台" },
  { id: "models", path: "/models", expectedText: "模型提供商" },
  { id: "skills", path: "/skills", expectedText: "当前显示" },
  { id: "task-center", path: "/task-center", expectedText: "运行控制台" },
];

const companionReleaseRoutes = [
  { id: "chat-companion", path: "/chat", expectedText: "今天想聊点什么" },
  { id: "works-companion", path: "/works", expectedText: "灵感有了形状" },
  {
    id: "settings-personalization",
    path: "/settings",
    expectedText: "关于澜台",
  },
];

const companionReleaseScenarios = companionReleaseRoutes.flatMap((route) => [
  {
    route,
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
    contrast: "no-preference",
    keyboardAudit: true,
  },
  {
    route,
    width: 1440,
    height: 900,
    theme: "dark",
    motion: "normal",
    contrast: "no-preference",
    keyboardAudit: true,
  },
  {
    route,
    width: 1280,
    height: 800,
    theme: "light",
    motion: "normal",
    contrast: "no-preference",
    keyboardAudit: true,
  },
  {
    route,
    width: 900,
    height: 720,
    theme: "light",
    motion: "reduce",
    contrast: "more",
    fontScale: 1.25,
    keyboardAudit: true,
  },
]);

const safetySmokeScenarios = [
  { id: "channels", path: "/channels", expectedText: "频道" },
  { id: "skills", path: "/skills", expectedText: "技能" },
  { id: "mcp", path: "/mcp", expectedText: "官方连接器" },
  {
    id: "task-center",
    path: "/task-center",
    expectedText: "运行控制台",
  },
  { id: "reminders", path: "/reminders", expectedText: "今日" },
  {
    id: "browser-workbench",
    path: "/browser-workbench",
    expectedText: "浏览器工作台",
  },
  {
    id: "environments",
    path: "/environments",
    expectedText: "环境变量",
  },
  { id: "works", path: "/works", expectedText: "作品" },
].map((route) => ({
  route,
  layoutAudits:
    route.id === "mcp"
      ? [
          {
            id: "mcp-scope-to-catalog",
            upperSelector: '[data-testid="mcp-scope-notice"]',
            lowerSelector: '[data-testid="mcp-official-catalog"]',
            minVerticalGap: 20,
            maxInlineEdgeDelta: 1,
          },
          {
            id: "mcp-catalog-to-advanced",
            upperSelector: '[data-testid="mcp-official-catalog"]',
            lowerSelector: '[data-testid="mcp-advanced-connections"]',
            minVerticalGap: 20,
            maxInlineEdgeDelta: 1,
          },
        ]
      : undefined,
  ignoredConsoleErrors:
    route.id === "mcp" ? ["ModelSelector: failed to load data"] : undefined,
  width: 1440,
  height: 900,
  theme: "light",
  motion: "reduce",
}));

const normalScenarios = [
  { width: 1440, height: 900, theme: "light", motion: "normal" },
  { width: 1440, height: 900, theme: "dark", motion: "normal" },
  { width: 1280, height: 800, theme: "light", motion: "normal" },
  { width: 1280, height: 800, theme: "dark", motion: "normal" },
];

const reducedScenarios = coreRoutes.map((route) => ({
  route,
  width: 1440,
  height: 900,
  theme: "light",
  motion: "reduce",
}));

const coreScenarios = [
  ...coreRoutes.flatMap((route) =>
    normalScenarios.map((scenario) => ({ route, ...scenario })),
  ),
  ...reducedScenarios,
].map((scenario) => ({
  ...scenario,
  // Core visual review is intentionally runnable without a live model backend.
  ignoredConsoleErrors: ["ModelSelector: failed to load data"],
}));

const memoryContrastScenarios = ["light", "dark"].map((theme) => ({
  route: {
    id: "memory-button-contrast",
    path: "/memory",
    expectedText: "澜台现在怎样理解你",
  },
  width: 1440,
  height: 900,
  theme,
  motion: "normal",
}));

const artifactPanelScenarios = [
  {
    route: {
      id: "chat-agent-default",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  ...[
    { width: 1440, height: 900, theme: "light", motion: "normal" },
    { width: 1440, height: 900, theme: "dark", motion: "normal" },
    { width: 1280, height: 800, theme: "light", motion: "normal" },
  ].map((scenario) => ({
    route: {
      id: "chat-artifact-panel",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    action: {
      type: "click-selector",
      selector: '[data-testid="chat-artifact-panel-toggle"]',
      expectedText: "执行过程仍保留在聊天时间线中",
    },
    ...scenario,
  })),
  {
    route: {
      id: "chat-artifact-panel-resized",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    actions: [
      {
        type: "click-selector",
        selector: '[data-testid="chat-artifact-panel-toggle"]',
        expectedText: "执行过程仍保留在聊天时间线中",
      },
      {
        type: "drag-selector",
        selector: '[data-testid="chat-artifact-resizer"]',
        deltaX: -150,
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-artifact-panel-compressed",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    actions: [
      {
        type: "click-selector",
        selector: '[data-testid="chat-artifact-panel-toggle"]',
        expectedText: "执行过程仍保留在聊天时间线中",
      },
      {
        type: "drag-selector",
        selector: '[data-testid="chat-artifact-resizer"]',
        deltaX: 500,
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
];

const settingsAuditRoutes = [
  {
    id: "advanced-parameters",
    path: "/advanced-parameters",
    expectedText: "Agent 循环",
  },
  { id: "appearance", path: "/appearance", expectedText: "外观与显示" },
  { id: "about", path: "/about", expectedText: "关于澜台" },
  {
    id: "browser-login",
    path: "/browser-login",
    expectedText: "网页登录",
  },
  {
    id: "computer-permissions",
    path: "/computer-permissions",
    expectedText: "电脑权限",
  },
  { id: "debug", path: "/debug", expectedText: "调试" },
  {
    id: "environments",
    path: "/environments",
    expectedText: "环境变量",
  },
  {
    id: "office-documents",
    path: "/office-documents",
    expectedText: "Office 文档能力",
  },
  {
    id: "agent-stats",
    path: "/agent-stats",
    expectedText: "智能体统计",
  },
  {
    id: "token-usage",
    path: "/token-usage",
    expectedText: "Token 消耗",
  },
];

const settingsAuditScenarios = settingsAuditRoutes.flatMap((route) => [
  {
    route,
    layoutAudits:
      route.id === "appearance"
        ? [
            {
              id: "appearance-notice-to-sidebar",
              upperSelector: '[data-testid="appearance-effect-notice"]',
              lowerSelector: '[data-testid="appearance-sidebar-section"]',
              minVerticalGap: 16,
              maxInlineEdgeDelta: 1,
            },
          ]
        : undefined,
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route,
    action:
      route.id === "appearance"
        ? {
            type: "scroll-selector",
            selector: '[data-testid="conversation-display-section"]',
            expectedText: "沈行知的表达风格",
          }
        : undefined,
    width: 1440,
    height: 900,
    theme: "dark",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route,
    action:
      route.id === "appearance"
        ? {
            type: "scroll-selector",
            selector: '[data-testid="conversation-display-section"]',
            expectedText: "沈行知的表达风格",
          }
        : undefined,
    width: 1280,
    height: 800,
    theme: "light",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
]);
const settingsCoreIds = new Set([
  "advanced-parameters",
  "computer-permissions",
  "office-documents",
]);
const settingsCoreScenarios = settingsAuditScenarios.filter((scenario) =>
  settingsCoreIds.has(scenario.route.id),
);
const settingsPolishIds = new Set([
  "about",
  "agent-stats",
  "debug",
  "environments",
  "token-usage",
]);
const settingsPolishScenarios = settingsAuditScenarios.filter((scenario) =>
  settingsPolishIds.has(scenario.route.id),
);
const settingsDebugScenarios = settingsAuditScenarios.filter(
  (scenario) => scenario.route.id === "debug",
);

const stateScenarios = [
  {
    route: {
      id: "chat-long",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-long",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    width: 1440,
    height: 900,
    theme: "dark",
    motion: "normal",
  },
  {
    route: {
      id: "chat-long",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    width: 1280,
    height: 800,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-sessions",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "click-selector",
      selector: '[data-testid="chat-session-switcher"]',
      expectedText: "已归档",
    },
    finalExpectedText: "已归档",
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-sessions",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "click-selector",
      selector: '[data-testid="chat-session-switcher"]',
      expectedText: "已归档",
    },
    finalExpectedText: "已归档",
    width: 1440,
    height: 900,
    theme: "dark",
    motion: "normal",
  },
  {
    route: {
      id: "chat-session-actions",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [
      {
        type: "click-selector",
        selector: '[data-testid="chat-session-switcher"]',
        expectedText: "已归档",
      },
      {
        type: "click-selector",
        selector: '[data-session-action="true"]',
        expectedText: "复制会话链接",
      },
    ],
    finalExpectedText: "复制会话链接",
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-model-selector",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "click-text",
      selector: ".lantai-dropdown-trigger",
      text: "Kimi for Coding",
      expectedText: "切换后立即应用到当前智能体",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-commands",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "type-in-textarea",
      text: "/c",
      expectedText: "压缩对话上下文",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-attachment-drag",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "drag-file",
      expectedText: "松开添加到对话",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-history-drawer",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "click-selector",
      selector: '[data-testid="chat-history-button"]',
      expectedText: "全部聊天",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-history-drawer",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "click-selector",
      selector: '[data-testid="chat-history-button"]',
      expectedText: "全部聊天",
    },
    width: 1440,
    height: 900,
    theme: "dark",
    motion: "normal",
  },
  {
    route: {
      id: "chat-session-rename",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [
      {
        type: "click-selector",
        selector: '[data-testid="chat-history-button"]',
        expectedText: "全部聊天",
      },
      {
        type: "click-icon",
        icon: "SparkEditLine",
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-session-menu",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [
      {
        type: "click-selector",
        selector: '[data-testid="chat-history-button"]',
        expectedText: "全部聊天",
      },
      {
        type: "context-menu",
        selector: '[role="option"]',
        expectedText: "重命名",
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "skills-import-menu",
      path: "/skills",
      expectedText: "当前显示",
    },
    action: {
      type: "click-text",
      selector: "button",
      text: "导入与同步",
      expectedText: "从技能池载入",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "skills-create-drawer",
      path: "/skills",
      expectedText: "当前显示",
    },
    action: {
      type: "click-text",
      selector: "button",
      text: "创建技能",
      expectedText: "技能内容",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "models-provider-modal",
      path: "/models",
      expectedText: "模型提供商",
    },
    action: {
      type: "click-selector",
      selector: '[data-testid="provider-settings-ollama"]',
      expectedText: "配置 Ollama",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "permissions",
      path: "/permissions",
      expectedText: "托管免打扰",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "permissions",
      path: "/permissions",
      expectedText: "托管免打扰",
    },
    width: 1440,
    height: 900,
    theme: "dark",
    motion: "normal",
  },
  {
    route: {
      id: "security",
      path: "/security",
      expectedText: "工具防护",
    },
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
];

const securityUiScenarios = [
  ...[
    { width: 1440, height: 900, theme: "light" },
    { width: 1440, height: 900, theme: "dark" },
    { width: 1280, height: 800, theme: "light" },
  ].map((scenario) => ({
    route: {
      id: "chat-permission-menu",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "click-selector",
      selector: '[data-chat-permission-trigger="1"]',
      expectedText: "自定义权限",
    },
    finalExpectedText: "自定义权限",
    motion: "normal",
    ...scenario,
  })),
  ...[
    { width: 1440, height: 900, theme: "light" },
    { width: 1440, height: 900, theme: "dark" },
    { width: 1280, height: 800, theme: "light" },
  ].map((scenario) => ({
    route: {
      id: "chat-approval-card",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "mount-approval-preview",
      expectedText: "安全审批",
    },
    finalExpectedText: "安全审批",
    motion: "normal",
    ...scenario,
  })),
];

const imageGenerationScenarios = [
  ...[
    { width: 1440, height: 900, theme: "light" },
    { width: 1440, height: 900, theme: "dark" },
    { width: 1280, height: 800, theme: "light" },
  ].map((scenario) => ({
    route: {
      id: "models-image-generation",
      path: "/models",
      expectedText: "模型提供商",
    },
    action: {
      type: "scroll-selector",
      selector: '[data-testid="image-generation-settings"]',
      expectedText: "对话内生图",
    },
    motion: "normal",
    ...scenario,
  })),
  ...["running", "completed", "failed"].flatMap((variant) =>
    [
      { width: 1440, height: 900, theme: "light" },
      { width: 1440, height: 900, theme: "dark" },
    ].map((scenario) => ({
      route: {
        id: `chat-image-generation-${variant}`,
        path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
        expectedText: "总结：1到80的平方",
      },
      action: {
        type: "mount-image-generation-preview",
        variant,
        expectedText:
          variant === "running"
            ? "正在生成图片"
            : variant === "failed"
              ? "图片生成失败"
              : "已完成",
      },
      motion: "normal",
      ...scenario,
    })),
  ),
];

const structuredOutputScenarios = [
  ...[
    { width: 1440, height: 900, theme: "light" },
    { width: 1440, height: 900, theme: "dark" },
    { width: 1280, height: 800, theme: "light" },
  ].map((scenario) => ({
    route: {
      id: "models-utility-intelligence",
      path: "/models",
      expectedText: "模型提供商",
    },
    action: {
      type: "scroll-selector",
      selector: '[data-testid="utility-intelligence-settings"]',
      expectedText: "辅助智能模型",
    },
    motion: "normal",
    ...scenario,
  })),
  {
    route: {
      id: "models-utility-intelligence-dedicated",
      path: "/models",
      expectedText: "模型提供商",
    },
    actions: [
      {
        type: "scroll-selector",
        selector: '[data-testid="utility-intelligence-settings"]',
        expectedText: "辅助智能模型",
      },
      {
        type: "click-text",
        selector: '[class$="-segmented-item"]',
        text: "独立辅助模型",
        expectedText: "使用独立的提供商",
      },
    ],
    finalExpectedText: "使用独立的提供商",
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
  {
    route: {
      id: "chat-structured-document",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "mount-structured-document-preview",
      expectedText: "灵犀专业版最新新闻总结",
    },
    finalExpectedText: "灵犀专业版最新新闻总结",
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
];

const wikiConfirmScenarios = [
  { width: 1440, height: 900, theme: "light" },
  { width: 1440, height: 900, theme: "dark" },
].map((scenario) => ({
  route: {
    id: "chat-save-to-wiki-confirm",
    path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
    expectedText: "总结：1到80的平方",
  },
  action: {
    type: "click-selector",
    selector: '[aria-label="保存到知识库"]',
    expectedText: "保存到知识库？",
  },
  motion: "normal",
  ...scenario,
}));

const knowledgeWorkspaceScenarios = [
  { width: 1440, height: 900, theme: "light" },
  { width: 1440, height: 900, theme: "dark" },
  { width: 1280, height: 800, theme: "light" },
]
  .map((scenario) => ({
    route: {
      id: `knowledge-workspace-${scenario.width}-${scenario.theme}`,
      path: "/workspace",
      expectedText: "知识内容",
    },
    motion: "reduce",
    ...scenario,
  }))
  .concat([
    {
      route: {
        id: "knowledge-workspace-add-content",
        path: "/workspace",
        expectedText: "知识内容",
      },
      action: {
        type: "click-text",
        selector: "button",
        text: "添加资料",
        expectedText: "上传文件",
      },
      width: 1440,
      height: 900,
      theme: "light",
      motion: "reduce",
    },
    {
      route: {
        id: "knowledge-workspace-management",
        path: "/workspace",
        expectedText: "知识内容",
      },
      action: {
        type: "click-text",
        selector: "button",
        text: "管理",
        expectedText: "用途与范围",
      },
      width: 1440,
      height: 900,
      theme: "light",
      motion: "reduce",
    },
  ]);

const tokenPopoverScenarios = [
  { width: 1440, height: 900, theme: "light" },
  { width: 1440, height: 900, theme: "dark" },
  { width: 1280, height: 800, theme: "light" },
].map((scenario) => ({
  route: {
    id: "chat-token-popover",
    path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
    expectedText: "总结：1到80的平方",
  },
  action: {
    type: "click-selector",
    selector: '[aria-label="查看上下文占用与保护选项"]',
    expectedText: "本轮上下文",
  },
  motion: "normal",
  ...scenario,
}));

const messageActionScenarios = [
  ...[
    { width: 1440, height: 900, theme: "light" },
    { width: 1440, height: 900, theme: "dark" },
  ].map((scenario) => ({
    route: {
      id: "chat-message-actions-hidden",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    motion: "normal",
    ...scenario,
  })),
  ...[
    { width: 1440, height: 900, theme: "light" },
    { width: 1440, height: 900, theme: "dark" },
  ].map((scenario) => ({
    route: {
      id: "chat-message-actions-hover",
      path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
      expectedText: "总结：1到80的平方",
    },
    action: {
      type: "hover-selector",
      selector: ".lantai-user-message-stack",
      index: -1,
    },
    motion: "normal",
    ...scenario,
  })),
];

const branchConfirmScenarios = [
  { width: 1440, height: 900, theme: "light" },
  { width: 1440, height: 900, theme: "dark" },
].map((scenario) => ({
  route: {
    id: "chat-branch-confirm",
    path: "/chat/1b3ac79f-62e8-43d4-aed9-4baaacc77718",
    expectedText: "总结：1到80的平方",
  },
  action: {
    type: "click-selector",
    selector: '[aria-label="从此处创建分支"]',
    expectedText: "从此处创建分支？",
  },
  motion: "normal",
  ...scenario,
}));

const attachmentPasteScenarios = [
  { width: 1440, height: 900, theme: "light" },
  { width: 1440, height: 900, theme: "dark" },
].map((scenario) => ({
  route: {
    id: "chat-attachment-paste",
    path: "/chat",
    expectedText: "今天想推进",
  },
  action: {
    type: "paste-files",
    expectedText: "设计说明.md",
  },
  finalExpectedText: "设计说明.md",
  ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  motion: "normal",
  ...scenario,
}));

const chatProcessScenarios = [
  ...[
    { width: 1440, height: 900, theme: "light", variant: "pending" },
    { width: 1440, height: 900, theme: "dark", variant: "pending" },
    { width: 1440, height: 900, theme: "light", variant: "running" },
    { width: 1440, height: 900, theme: "dark", variant: "running" },
    { width: 1440, height: 900, theme: "light", variant: "result-order" },
    { width: 1440, height: 900, theme: "light", variant: "long-delivery" },
    { width: 1440, height: 900, theme: "light", variant: "recovery-states" },
  ].map((scenario) => ({
    route: {
      id: `chat-process-${scenario.variant}`,
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type:
        scenario.variant === "recovery-states"
          ? "mount-response-status-preview"
          : "mount-chat-process-preview",
      variant: scenario.variant,
      expectedText:
        scenario.variant === "pending"
          ? "正在等待首个更新"
          : scenario.variant === "result-order"
            ? "这是用户要看的完整结果"
            : scenario.variant === "long-delivery"
              ? "灵犀专业版 · 公开动态"
              : scenario.variant === "recovery-states"
                ? "已生成的内容已保留"
                : "正在编辑文件",
    },
    finalExpectedText:
      scenario.variant === "pending"
        ? "正在等待首个更新"
        : scenario.variant === "result-order"
          ? "这是用户要看的完整结果"
          : scenario.variant === "long-delivery"
            ? "灵犀专业版 · 公开动态"
            : scenario.variant === "recovery-states"
              ? "已生成的内容已保留"
              : "正在编辑文件",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
    motion: "normal",
    ...scenario,
  })),
  {
    route: {
      id: "chat-process-running",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "mount-chat-process-preview",
      variant: "running",
      expectedText: "正在编辑文件",
    },
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "reduce",
  },
];

const systemActionScenarios = [
  ...[
    { width: 1440, height: 900, theme: "light" },
    { width: 1440, height: 900, theme: "dark" },
    { width: 1280, height: 800, theme: "light" },
  ].map((scenario) => ({
    route: {
      id: "chat-system-action-compact",
      path: "/chat",
      expectedText: "今天想推进",
    },
    action: {
      type: "mount-system-action-preview",
      expectedText: "对话已压缩",
    },
    finalExpectedText: "对话已压缩",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
    motion: "normal",
    ...scenario,
  })),
  {
    route: {
      id: "chat-system-action-summary",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [
      {
        type: "mount-system-action-preview",
        expectedText: "对话已压缩",
      },
      {
        type: "click-text",
        selector: ".lantai-system-action-toggle",
        text: "查看摘要",
        expectedText: "保留用户正在推进的任务",
      },
    ],
    finalExpectedText: "保留用户正在推进的任务",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "normal",
  },
];

const interactionScenarioIds = new Set([
  "chat-commands",
  "chat-history-drawer",
  "chat-session-rename",
  "chat-session-menu",
  "models-provider-modal",
]);
const interactionScenarios = stateScenarios.filter((scenario) =>
  interactionScenarioIds.has(scenario.route.id),
);
const commandScenarios = stateScenarios.filter(
  (scenario) => scenario.route.id === "chat-commands",
);
const chatSessionScenarios = stateScenarios.filter(
  (scenario) =>
    scenario.route.id === "chat-sessions" ||
    scenario.route.id === "chat-session-actions",
);
const foundationScenarios = coreScenarios.filter((scenario) =>
  ["chat", "settings"].includes(scenario.route.id),
);

const guidanceQueueScenarios = ["light", "dark"].map((theme) => ({
  route: {
    id: "chat-guidance-queue",
    path: "/chat",
    expectedText: "今天想推进",
  },
  action: {
    type: "mount-guidance-queue-preview",
    expectedText: "调整方向",
  },
  finalExpectedText: "已接收 · 等待安全边界",
  width: 1440,
  height: 900,
  theme,
  motion: "normal",
  ignoredConsoleErrors: ["ModelSelector: failed to load data"],
}));

const brandOsScenarios = [
  {
    route: {
      id: "brand-composer-light",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [{ type: "focus-selector", selector: ".lantai-sender textarea" }],
    styleAudits: [
      { id: "composer", selector: ".lantai-sender" },
      { id: "composer-focus", selector: ".lantai-sender textarea" },
      {
        id: "submit",
        selector:
          ".lantai-sender button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn)",
      },
    ],
    controlStateAudits: [
      {
        id: "submit",
        selector:
          ".lantai-sender button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn)",
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    palette: "mist",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-composer-dark",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [{ type: "focus-selector", selector: ".lantai-sender textarea" }],
    styleAudits: [
      { id: "composer", selector: ".lantai-sender" },
      { id: "composer-focus", selector: ".lantai-sender textarea" },
      {
        id: "submit",
        selector:
          ".lantai-sender button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn)",
      },
    ],
    controlStateAudits: [
      {
        id: "submit",
        selector:
          ".lantai-sender button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn)",
      },
    ],
    width: 1440,
    height: 900,
    theme: "dark",
    palette: "mist",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-tools-light",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [
      {
        type: "click-selector",
        selector: '[data-chat-capability-trigger="1"]',
        expectedText: "连接器",
      },
      {
        type: "click-selector",
        selector:
          '[data-testid="capability-menu-popover"] [role="menu"] > button[role="menuitem"]:last-child',
        expectedText: "金山文档",
      },
    ],
    finalExpectedText: "金山文档",
    styleAudits: [
      { id: "tool-trigger", selector: '[data-chat-capability-trigger="1"]' },
      { id: "tool-menu", selector: '[data-testid="capability-menu-popover"]' },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    palette: "blue",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-approval-light",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [{ type: "mount-approval-preview", expectedText: "安全审批" }],
    finalExpectedText: "安全审批",
    styleAudits: [
      { id: "approval-card", selector: "#approval-review-host article" },
      {
        id: "allow-once",
        selector: '#approval-review-host button[class*="approveButton"]',
      },
      {
        id: "cancel-task",
        selector: '#approval-review-host button[aria-label="取消整个任务"]',
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    palette: "mist",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-approval-narrow-dark",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [{ type: "mount-approval-preview", expectedText: "安全审批" }],
    finalExpectedText: "安全审批",
    styleAudits: [
      { id: "approval-card", selector: "#approval-review-host article" },
      {
        id: "cancel-task",
        selector: '#approval-review-host button[aria-label="取消整个任务"]',
      },
    ],
    width: 900,
    height: 720,
    theme: "dark",
    palette: "mist",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-approval-loading-light",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [
      {
        type: "mount-approval-preview",
        pendingApproval: true,
        expectedText: "安全审批",
      },
      {
        type: "click-selector",
        selector: '#approval-review-host button[class*="approveButton"]',
      },
    ],
    finalExpectedText: "安全审批",
    styleAudits: [
      { id: "approval-card", selector: "#approval-review-host article" },
      {
        id: "allow-once-loading",
        selector: '#approval-review-host button[class*="approveButton"]',
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    palette: "mist",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-artifact-split-light",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [{ type: "mount-artifact-preview", expectedText: "项目周报" }],
    finalExpectedText: "项目周报",
    styleAudits: [
      { id: "artifact-panel", selector: '[data-testid="chat-artifact-panel"]' },
      { id: "artifact-close", selector: '[aria-label="收起产物面板"]' },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    palette: "warm",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-artifact-overlay-dark",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [{ type: "mount-artifact-preview", expectedText: "项目周报" }],
    finalExpectedText: "项目周报",
    styleAudits: [
      { id: "artifact-panel", selector: '[data-testid="chat-artifact-panel"]' },
      { id: "artifact-close", selector: '[aria-label="收起产物面板"]' },
    ],
    width: 900,
    height: 720,
    theme: "dark",
    palette: "violet",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-kdocs-split-light",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [
      { type: "mount-kdocs-preview", expectedText: "品牌系统实施计划" },
    ],
    finalExpectedText: "品牌系统实施计划",
    styleAudits: [
      { id: "kdocs-panel", selector: '[aria-label="金山文档"]' },
      { id: "kdocs-close", selector: '[aria-label="关闭金山文档"]' },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    palette: "lantai",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-kdocs-overlay-dark",
      path: "/chat",
      expectedText: "今天想推进",
    },
    actions: [
      { type: "mount-kdocs-preview", expectedText: "品牌系统实施计划" },
    ],
    finalExpectedText: "品牌系统实施计划",
    styleAudits: [
      { id: "kdocs-panel", selector: '[aria-label="金山文档"]' },
      { id: "kdocs-close", selector: '[aria-label="关闭金山文档"]' },
    ],
    width: 900,
    height: 720,
    theme: "dark",
    palette: "blue",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "brand-appearance-light",
      path: "/appearance",
      expectedText: "主题色",
    },
    actions: [
      {
        type: "scroll-selector",
        selector: '[class*="paletteGrid"]',
        expectedText: "石墨",
      },
    ],
    styleAudits: [
      { id: "palette-grid", selector: '[class*="paletteGrid"]' },
      {
        id: "mist-option",
        selector: '[class*="paletteGrid"] button[aria-pressed="true"]',
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    palette: "mist",
    motion: "normal",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
];

const dailyBriefScenarios = [
  {
    route: {
      id: "daily-brief-home-entry",
      path: "/chat",
      expectedText: "澜台简报",
    },
    styleAudits: [
      {
        id: "daily-brief-home-card",
        selector: '[aria-label^="打开澜台简报"]',
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "reduce",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "daily-brief-workbench",
      path: "/daily-brief",
      expectedText: "从你真正关心的变化开始",
    },
    styleAudits: [
      {
        id: "daily-brief-workbench",
        selector: '[aria-label="澜台简报工作台"]',
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "reduce",
  },
  {
    route: {
      id: "daily-brief-card-open",
      path: "/chat",
      expectedText: "澜台简报",
    },
    actions: [
      {
        type: "click-selector",
        selector: '[aria-label^="打开澜台简报"]',
        expectedText: "从你真正关心的变化开始",
      },
    ],
    finalExpectedText: "从你真正关心的变化开始",
    styleAudits: [
      {
        id: "daily-brief-workbench",
        selector: '[aria-label="澜台简报工作台"]',
      },
    ],
    width: 1440,
    height: 900,
    theme: "light",
    motion: "reduce",
    ignoredConsoleErrors: ["ModelSelector: failed to load data"],
  },
  {
    route: {
      id: "daily-brief-settings",
      path: "/daily-brief",
      expectedText: "开始设置",
    },
    actions: [
      {
        type: "click-text",
        selector: "button",
        text: "开始设置",
        expectedText: "简报设置",
      },
    ],
    finalExpectedText: "简报设置",
    width: 1440,
    height: 900,
    theme: "light",
    motion: "reduce",
  },
  {
    route: {
      id: "daily-brief-workbench-narrow",
      path: "/daily-brief",
      expectedText: "从你真正关心的变化开始",
    },
    width: 900,
    height: 720,
    theme: "light",
    motion: "reduce",
  },
];

const profileScenarios =
  profile === "brand-os"
    ? brandOsScenarios
    : profile === "daily-brief"
      ? dailyBriefScenarios
      : profile === "companion-release"
        ? companionReleaseScenarios
        : profile === "memory-contrast"
          ? memoryContrastScenarios
          : profile === "safety-smoke"
            ? safetySmokeScenarios
            : profile === "ui-foundation"
              ? foundationScenarios
              : profile === "guidance-queue"
                ? guidanceQueueScenarios
                : profile === "states"
                  ? stateScenarios
                  : profile === "agent" || profile === "workbench"
                    ? artifactPanelScenarios
                    : profile === "settings"
                      ? settingsAuditScenarios
                      : profile === "settings-core"
                        ? settingsCoreScenarios
                        : profile === "settings-polish"
                          ? settingsPolishScenarios
                          : profile === "settings-debug"
                            ? settingsDebugScenarios
                            : profile === "security-ui"
                              ? securityUiScenarios
                              : profile === "image-generation"
                                ? imageGenerationScenarios
                                : profile === "structured-output"
                                  ? structuredOutputScenarios
                                  : profile === "structured-document"
                                    ? structuredOutputScenarios.filter(
                                        (scenario) =>
                                          scenario.route.id ===
                                          "chat-structured-document",
                                      )
                                    : profile === "knowledge"
                                      ? knowledgeWorkspaceScenarios
                                      : profile === "wiki-confirm"
                                        ? wikiConfirmScenarios
                                        : profile === "token-popover"
                                          ? tokenPopoverScenarios
                                          : profile === "message-actions"
                                            ? messageActionScenarios
                                            : profile === "branch-confirm"
                                              ? branchConfirmScenarios
                                              : profile === "attachments"
                                                ? attachmentPasteScenarios
                                                : profile === "chat-process"
                                                  ? chatProcessScenarios
                                                  : profile === "system-actions"
                                                    ? systemActionScenarios
                                                    : profile === "commands"
                                                      ? commandScenarios
                                                      : profile ===
                                                          "chat-sessions"
                                                        ? chatSessionScenarios
                                                        : profile ===
                                                            "interactions"
                                                          ? interactionScenarios
                                                          : profile === "all"
                                                            ? [
                                                                ...coreScenarios,
                                                              ...stateScenarios,
                                                            ]
                                                            : coreScenarios;
const routeFilter = new Set(
  String(routeFilterArg || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);
const scenarios = routeFilter.size
  ? profileScenarios.filter((scenario) => routeFilter.has(scenario.route.id))
  : profileScenarios;
const routes = Array.from(
  new Map(
    scenarios.map((scenario) => [scenario.route.id, scenario.route]),
  ).values(),
);

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function candidateChromePaths() {
  const candidates = [
    process.env.CHROME_PATH,
    process.env.GOOGLE_CHROME_BIN,
  ].filter(Boolean);

  if (process.platform === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      path.join(
        os.homedir(),
        "Applications",
        "Google Chrome.app",
        "Contents",
        "MacOS",
        "Google Chrome",
      ),
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
    );
  }

  if (process.platform === "linux") {
    candidates.push(
      "/usr/bin/google-chrome",
      "/usr/bin/google-chrome-stable",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
    );
  }

  if (process.platform === "win32") {
    candidates.push(
      path.join(
        process.env.PROGRAMFILES || "C:\\Program Files",
        "Google",
        "Chrome",
        "Application",
        "chrome.exe",
      ),
      path.join(
        process.env["PROGRAMFILES(X86)"] || "C:\\Program Files (x86)",
        "Google",
        "Chrome",
        "Application",
        "chrome.exe",
      ),
      path.join(
        process.env.LOCALAPPDATA || "",
        "Google",
        "Chrome",
        "Application",
        "chrome.exe",
      ),
    );
  }

  return candidates;
}

function resolveChromePath() {
  for (const candidate of candidateChromePaths()) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  throw new Error(
    "Could not resolve Chrome binary. Set CHROME_PATH or install Google Chrome.",
  );
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => {
        if (address && typeof address === "object") resolve(address.port);
        else reject(new Error("Could not allocate a Chrome debugging port."));
      });
    });
  });
}

function chromeArgs(port, profileDir) {
  return [
    "--headless=new",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-gpu",
    "--disable-sync",
    "--hide-scrollbars",
    "--no-default-browser-check",
    "--no-first-run",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    "about:blank",
  ];
}

async function waitForJson(url) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch {
      // Chrome is still starting.
    }
    await delay(100);
  }
  throw new Error(`Timed out waiting for Chrome DevTools at ${url}`);
}

function createCdpClient(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  let nextId = 1;
  const pending = new Map();
  const events = new Map();

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) {
        reject(
          new Error(message.error.message || JSON.stringify(message.error)),
        );
      } else {
        resolve(message.result || {});
      }
      return;
    }

    if (message.method && events.has(message.method)) {
      for (const listener of events.get(message.method)) {
        listener(message.params || {});
      }
    }
  });

  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });

  function send(method, params = {}) {
    const id = nextId;
    nextId += 1;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }

  function on(method, listener) {
    const listeners = events.get(method) || new Set();
    listeners.add(listener);
    events.set(method, listeners);
    return () => listeners.delete(listener);
  }

  return {
    opened,
    send,
    on,
    close: () => socket.close(),
  };
}

async function openTarget(port) {
  const response = await fetch(
    `http://127.0.0.1:${port}/json/new?about:blank`,
    {
      method: "PUT",
    },
  );
  if (!response.ok) {
    throw new Error(`Could not create Chrome target: ${response.status}`);
  }
  const target = await response.json();
  if (!target.webSocketDebuggerUrl) {
    throw new Error("Chrome target did not provide a WebSocket URL.");
  }
  return target;
}

function waitForLoadEvent(client) {
  return new Promise((resolve) => {
    const off = client.on("Page.loadEventFired", () => {
      off();
      resolve();
    });
  });
}

async function waitForApplication(client, expectedText) {
  const compatibleExpectedTexts =
    expectedText === "今天想推进"
      ? ["今天想推进", "今天想聊点什么"]
      : [expectedText];
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const bodyText = document.body.innerText || "";
        const page = document.querySelector(".page-container");
        const pageRect = page?.getBoundingClientRect();
        const pageStyle = page ? getComputedStyle(page) : null;
        const loading =
          bodyText.includes("加载中...") ||
          bodyText.includes("加载中…") ||
          bodyText.includes("Loading...") ||
          /(?:正在)?加载.{0,24}(?:中|…|[.]{3})/.test(bodyText);
        return Boolean(
          page &&
          pageRect &&
          pageRect.width > 600 &&
          pageRect.height > 400 &&
          pageStyle &&
          pageStyle.display !== "none" &&
          pageStyle.visibility !== "hidden" &&
          Number(pageStyle.opacity || 1) >= 0.99 &&
          bodyText.trim().length >= 40 &&
          bodyText.includes("聊天") &&
          bodyText.includes("设置") &&
          ${JSON.stringify(compatibleExpectedTexts)}.some((text) => bodyText.includes(text)) &&
          !loading
        );
      })()`,
      returnByValue: true,
    });
    if (result.result?.value === true) {
      await client.send("Runtime.evaluate", {
        expression:
          "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
        awaitPromise: true,
      });
      await delay(700);
      return true;
    }
    await delay(100);
  }
  return false;
}

async function waitForImages(client) {
  await client.send("Runtime.evaluate", {
    expression: `Promise.race([
      Promise.all(
        Array.from(document.images)
          .filter((image) => !image.complete)
          .map(
            (image) =>
              new Promise((resolve) => {
                image.addEventListener("load", resolve, { once: true });
                image.addEventListener("error", resolve, { once: true });
              })
          )
      ),
      new Promise((resolve) => setTimeout(resolve, 2500))
    ])`,
    awaitPromise: true,
    returnByValue: true,
  });
}

async function waitForText(client, expectedText) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const result = await client.send("Runtime.evaluate", {
      expression: `document.body.innerText.includes(${JSON.stringify(
        expectedText,
      )})`,
      returnByValue: true,
    });
    if (result.result?.value === true) {
      await delay(350);
      return true;
    }
    await delay(100);
  }
  return false;
}

async function performAction(client, action) {
  if (!action) return true;

  if (action.type === "click-text") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const candidates = Array.from(
          document.querySelectorAll(${JSON.stringify(action.selector)})
        ).filter((element) =>
          element.textContent?.replace(/\\s+/g, " ").trim().includes(
            ${JSON.stringify(action.text)}
          )
        );
        const target = candidates[${action.index || 0}];
        if (!(target instanceof HTMLElement)) return false;
        target.click();
        return true;
      })()`,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "click-selector") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const target = document.querySelector(${JSON.stringify(
          action.selector,
        )});
        if (!(target instanceof HTMLElement)) return false;
        target.click();
        return true;
      })()`,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "scroll-selector") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const target = document.querySelector(${JSON.stringify(
          action.selector,
        )});
        if (!(target instanceof HTMLElement)) return false;
        target.scrollIntoView({ block: "center", inline: "nearest" });
        return true;
      })()`,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "focus-selector") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const target = document.querySelector(${JSON.stringify(
          action.selector,
        )});
        if (!(target instanceof HTMLElement)) return false;
        target.focus();
        return document.activeElement === target;
      })()`,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "click-icon") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const icon = document.querySelector(
          '[data-icon=${JSON.stringify(action.icon)}]'
        );
        const target = icon?.closest("button");
        if (!(target instanceof HTMLElement)) return false;
        target.click();
        return true;
      })()`,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "hover-selector") {
    const position = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const candidates = Array.from(
          document.querySelectorAll(${JSON.stringify(action.selector)})
        );
        const target = candidates.at(${Number(action.index ?? 0)});
        if (!(target instanceof HTMLElement)) return null;
        const rect = target.getBoundingClientRect();
        return {
          x: rect.left + rect.width / 2,
          y: rect.top + Math.min(rect.height / 2, 32)
        };
      })()`,
      returnByValue: true,
    });
    const point = position.result?.value;
    if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) {
      return false;
    }
    await client.send("Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: point.x,
      y: point.y,
    });
  }

  if (action.type === "context-menu") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const target = document.querySelector(${JSON.stringify(
          action.selector,
        )});
        if (!(target instanceof HTMLElement)) return false;
        const rect = target.getBoundingClientRect();
        target.dispatchEvent(
          new MouseEvent("contextmenu", {
            bubbles: true,
            cancelable: true,
            clientX: rect.left + Math.min(120, rect.width / 2),
            clientY: rect.top + Math.min(40, rect.height / 2)
          })
        );
        return true;
      })()`,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "type-in-textarea") {
    const focused = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const target = document.querySelector("textarea");
        if (!(target instanceof HTMLTextAreaElement)) return false;
        target.focus();
        return true;
      })()`,
      returnByValue: true,
    });
    if (focused.result?.value !== true) return false;
    for (const character of action.text) {
      const upper = character.toUpperCase();
      const code = character === "/" ? "Slash" : `Key${upper}`;
      const keyCode = character === "/" ? 191 : upper.charCodeAt(0);
      await client.send("Input.dispatchKeyEvent", {
        type: "keyDown",
        key: character,
        code,
        text: character,
        unmodifiedText: character,
        windowsVirtualKeyCode: keyCode,
        nativeVirtualKeyCode: keyCode,
      });
      await client.send("Input.dispatchKeyEvent", {
        type: "keyUp",
        key: character,
        code,
        windowsVirtualKeyCode: keyCode,
        nativeVirtualKeyCode: keyCode,
      });
    }
  }

  if (action.type === "drag-file") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const target = document.querySelector('[data-empty-conversation="true"]');
        if (!(target instanceof HTMLElement)) return false;
        const transfer = new DataTransfer();
        transfer.items.add(
          new File(["UI review"], "ui-review.txt", { type: "text/plain" })
        );
        target.dispatchEvent(
          new DragEvent("dragenter", {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer
          })
        );
        return true;
      })()`,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "paste-files") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const target = document.querySelector("textarea");
        if (!(target instanceof HTMLTextAreaElement)) return false;
        const pngBytes = Uint8Array.from(
          atob(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAD0lEQVR4nGP4z8DAwMAAAAYAAQHLpfQAAAAASUVORK5CYII="
          ),
          (character) => character.charCodeAt(0)
        );
        const transfer = new DataTransfer();
        [
          new File([pngBytes], "界面参考.png", { type: "image/png" }),
          new File(["# 设计说明\\n\\n保留聊天主线。"], "设计说明.md", {
            type: "text/markdown"
          }),
          new File(["%PDF-1.4\\n% UI review"], "需求.pdf", {
            type: "application/pdf"
          }),
          new File(["office review"], "交付清单.docx", {
            type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          }),
          new File(["PK UI review"], "素材包.zip", {
            type: "application/zip"
          })
        ].forEach((file) => transfer.items.add(file));
        target.dispatchEvent(
          new ClipboardEvent("paste", {
            bubbles: true,
            cancelable: true,
            clipboardData: transfer
          })
        );
        for (let attempt = 0; attempt < 120; attempt += 1) {
          const text = document.body.innerText || "";
          if (
            text.includes("界面参考.png") &&
            text.includes("设计说明.md") &&
            text.includes("需求.pdf") &&
            text.includes("交付清单.docx") &&
            text.includes("素材包.zip") &&
            !text.includes("上传中")
          ) {
            return true;
          }
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
        return false;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "drag-selector") {
    const position = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const target = document.querySelector(${JSON.stringify(
          action.selector,
        )});
        if (!(target instanceof HTMLElement)) return null;
        const rect = target.getBoundingClientRect();
        return {
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2
        };
      })()`,
      returnByValue: true,
    });
    const start = position.result?.value;
    if (!start || !Number.isFinite(start.x) || !Number.isFinite(start.y)) {
      return false;
    }
    const endX = start.x + (action.deltaX || 0);
    const endY = start.y + (action.deltaY || 0);
    await client.send("Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: start.x,
      y: start.y,
    });
    await client.send("Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: start.x,
      y: start.y,
      button: "left",
      buttons: 1,
      clickCount: 1,
    });
    for (let step = 1; step <= 6; step += 1) {
      await client.send("Input.dispatchMouseEvent", {
        type: "mouseMoved",
        x: start.x + ((endX - start.x) * step) / 6,
        y: start.y + ((endY - start.y) * step) / 6,
        button: "left",
        buttons: 1,
      });
    }
    await client.send("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: endX,
      y: endY,
      button: "left",
      buttons: 0,
      clickCount: 1,
    });
  }

  if (action.type === "mount-guidance-queue-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const chatStage = document.querySelector('[data-testid="chat-stage"]');
        const composer = document.querySelector(".lantai-chat-anywhere-input");
        if (!(composer instanceof HTMLElement)) return false;
        if (chatStage instanceof HTMLElement) {
          chatStage.dataset.emptyConversation = "false";
        }
        const welcome = document.querySelector(
          ".lantai-chat-anywhere-message-list-welcome"
        );
        if (welcome instanceof HTMLElement) welcome.style.display = "none";
        const promptChips = document.querySelector('[class*="emptyPromptChips"]');
        if (promptChips instanceof HTMLElement) promptChips.style.display = "none";
        document.getElementById("guidance-queue-review-fixture")?.remove();
        const previewComposer = composer.cloneNode(true);
        if (!(previewComposer instanceof HTMLElement)) return false;
        previewComposer.id = "guidance-queue-review-fixture";
        previewComposer
          .querySelectorAll(".lantai-sender-editing-notice")
          .forEach((notice) => notice.remove());
        composer.style.visibility = "hidden";
        const queue = document.createElement("section");
        queue.className = "lantai-guidance-queue";
        queue.setAttribute("aria-label", "待发送引导");
        queue.innerHTML = \`
          <article class="lantai-guidance-card" data-guidance-status="admitted">
            <div class="lantai-guidance-card-copy">
              <svg class="lantai-guidance-mark" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 10-5 5 5 5"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>
              <span class="lantai-guidance-content">优先保留用户已经确认的方案，不要重写无关页面</span>
              <span class="lantai-guidance-status">已接收 · 等待安全边界</span>
            </div>
            <div class="lantai-guidance-actions" role="group" aria-label="引导操作">
              <button type="button" class="lantai-guidance-steer">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 10-5 5 5 5"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>
                调整方向
              </button>
              <button type="button" class="lantai-guidance-icon-action is-danger" aria-label="删除引导"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/></svg></button>
              <button type="button" class="lantai-guidance-icon-action" aria-label="更多引导操作"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></svg></button>
            </div>
          </article>\`;
        const previewSender = previewComposer.querySelector(".lantai-sender");
        if (!(previewSender instanceof HTMLElement)) return false;
        previewSender.before(queue);
        const actions = previewComposer.querySelector(".lantai-sender-actions");
        if (!(actions instanceof HTMLElement)) return false;
        actions.innerHTML = \`
          <button type="button" class="lantai-sender-actions-btn lantai-sender-stop-btn" aria-label="停止当前任务" title="停止当前任务">
            <span class="ant-btn-icon">
              <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" stroke="none" aria-hidden="true"><rect width="12" height="12" x="6" y="6" rx="1"/></svg>
            </span>
          </button>\`;
        composer.parentElement?.appendChild(previewComposer);
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        );
        return true;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "mount-chat-process-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const ReactModule = await import("/@id/react");
        const ReactDOMModule = await import("/@id/react-dom/client");
        const React = ReactModule.default ?? ReactModule;
        const ReactDOM = ReactDOMModule.default ?? ReactDOMModule;
        const antd = await import("/@id/antd");
        const responseModule = await import(
          "/src/pages/Chat/components/QuquResponseCard.tsx"
        );
        const toolModule = await import(
          "/src/pages/Chat/components/ToolCallCard.tsx"
        );
        const ChatAnywhereOptionsProvider =
          responseModule.ChatAnywhereOptionsProvider;
        const QuquResponseCard = responseModule.default;
        const ToolCallCard = toolModule.default;
        const variant = ${JSON.stringify(action.variant)};
        document.documentElement.dataset.displayThinking = "1";
        document.getElementById("chat-process-review-host")?.remove();
        document.getElementById("chat-process-review-style")?.remove();
        const reviewStyle = document.createElement("style");
        reviewStyle.id = "chat-process-review-style";
        reviewStyle.textContent = [
          "#chat-process-review-host{width:min(760px,calc(100% - 48px));margin:18px 0 26px;align-self:flex-start}",
        ].join("");
        document.head.appendChild(reviewStyle);
        const messageList = document.querySelector(
          ".lantai-chat-anywhere-message-list"
        );
        let bubbleList = document.querySelector(".lantai-bubble-list");
        const chatArea = document.querySelector('[class*="chatMessagesArea"]');
        const chatStage = document.querySelector('[data-testid="chat-stage"]');
        if (
          !(messageList instanceof HTMLElement) ||
          !(chatArea instanceof HTMLElement) ||
          !ChatAnywhereOptionsProvider ||
          !QuquResponseCard ||
          !ToolCallCard
        ) {
          return false;
        }
        if (chatStage instanceof HTMLElement) {
          chatStage.dataset.emptyConversation = "false";
        }
        const welcome = document.querySelector(
          ".lantai-chat-anywhere-message-list-welcome"
        );
        if (welcome instanceof HTMLElement) welcome.style.display = "none";
        if (!(bubbleList instanceof HTMLElement)) {
          bubbleList = document.createElement("div");
          bubbleList.className = "lantai-bubble-list";
          messageList.appendChild(bubbleList);
        }
        const promptChips = document.querySelector('[class*="emptyPromptChips"]');
        if (promptChips instanceof HTMLElement) promptChips.style.display = "none";
        const shell = document.createElement("div");
        shell.id = "chat-process-review-host";
        shell.className =
          "lantai-bubble-list-item lantai-bubble-assistant lantai-ui-review-tool-preview";
        if (variant === "long-delivery") shell.style.marginTop = "58px";
        bubbleList.appendChild(shell);
        const now = Math.floor(Date.now() / 1000);
        const data =
          variant === "pending"
            ? {
                id: "ui-review-pending",
                status: "in_progress",
                created_at: now - 14,
                output: [
                  {
                    id: "ui-review-heartbeat",
                    type: "heartbeat",
                    status: "in_progress",
                    content: []
                  }
                ]
              }
            : variant === "result-order"
              ? {
                  id: "ui-review-result-order",
                  status: "completed",
                  created_at: now - 22,
                  output: [
                    {
                      id: "ui-review-progress-answer",
                      type: "message",
                      status: "completed",
                      content: [{ type: "text", text: "我先核对关键信息。" }]
                    },
                    {
                      id: "ui-review-final-answer",
                      type: "message",
                      status: "completed",
                      content: [
                        {
                          type: "text",
                          text: "## 这是用户要看的完整结果\\n\\n最终结论会稳定落在工具过程之后，用户不需要再向上翻找。"
                        }
                      ]
                    },
                    {
                      id: "ui-review-tool-after-answer",
                      type: "plugin_call",
                      status: "in_progress",
                      content: [
                        {
                          type: "function_call",
                          data: {
                            call_id: "ui-review-search-call",
                            name: "guanlan_read",
                            arguments: JSON.stringify({
                              url: "https://example.com/source"
                            })
                          }
                        }
                      ]
                    },
                    {
                      id: "ui-review-tool-output",
                      type: "plugin_call_output",
                      status: "completed",
                      content: [
                        {
                          type: "function_call_output",
                          data: {
                            call_id: "ui-review-search-call",
                            name: "guanlan_read",
                            output: "已核对来源，关键信息一致。"
                          }
                        }
                      ]
                    }
                  ]
                }
              : variant === "long-delivery"
                ? {
                    id: "ui-review-long-delivery",
                    status: "completed",
                    created_at: now - 18,
                    output: [
                      {
                        id: "ui-review-long-answer",
                        type: "message",
                        status: "completed",
                        content: [
                          {
                            type: "text",
                            text: "# 灵犀专业版 · 公开动态\\n\\n先把信息确认到位。目前能够稳定确认的，是产品已经开始强调办公场景的深度集成与持续任务能力。\\n\\n**核心判断**\\n\\n它的价值并不只是“告诉用户怎么做”，而是在文档、表格和演示文稿中持续完成任务。竞争力将更多取决于真实编辑深度，而不是入口数量。\\n\\n**目前可观察到的能力**\\n\\n1. 在办公套件内理解当前文件和选区。\\n2. 连续执行多步任务，并在过程中保留上下文。\\n3. 将结果直接写回作品，而不只是生成一段建议。"
                          }
                        ]
                      }
                  ]
                }
              : variant === "recovery-states"
                ? {
                    id: "ui-review-recovery-states",
                    status: "failed",
                    created_at: now - 8,
                    output: [
                      {
                        id: "ui-review-user-stopped",
                        type: "error",
                        status: "completed",
                        message: "已停止生成",
                        code: "user_stopped",
                        user_stopped: true
                      },
                      {
                        id: "ui-review-technical-failure",
                        type: "error",
                        status: "completed",
                        message:
                          "HTTP 502: upstream_exception { trace_id: ui-review-1234567890 }"
                      }
                    ]
                  }
              : {
                id: "ui-review-running",
                status: "in_progress",
                created_at: now - 18,
                output: [
                  {
                    id: "ui-review-reasoning",
                    type: "reasoning",
                    status: "in_progress",
                    content: [
                      {
                        type: "text",
                        text: "先确认现有文件结构和页面入口。\\n正在核对消息流中的状态变化。\\n接着检查 Thinking 是否完整保存。\\n确认动态小窗不会推动主聊天滚动。\\n再验证用户手动滚动后暂停自动跟随。\\n最后检查点击展开和任务完成后的折叠状态。"
                      }
                    ]
                  },
                  {
                    id: "ui-review-write",
                    type: "plugin_call",
                    status: "in_progress",
                    content: [
                      {
                        data: {
                          call_id: "ui-review-write-call",
                          name: "write_file",
                          arguments: { path: "index.html" }
                        }
                      }
                    ]
                  }
                ]
              };
        ReactDOM.createRoot(shell).render(
          React.createElement(
            antd.ConfigProvider,
            { prefixCls: "lantai" },
            React.createElement(
              antd.App,
              null,
              React.createElement(
                ChatAnywhereOptionsProvider,
                {
                  options: {
                  customToolRenderConfig: {
                    guanlan_read: ToolCallCard,
                    write_file: ToolCallCard
                  },
                  welcome: {
                    avatar: "./lantai.png",
                    nick: "沈行知"
                  },
                  actions: { list: [] }
                  ,
                  recovery: {
                    continueGeneration: () => {},
                    retryGeneration: () => {}
                  }
                  }
                },
                React.createElement(QuquResponseCard, {
                  data,
                  isLast: true,
                  showThinking: true,
                  showToolCalls: true
                })
              )
            )
          )
        );
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        );
        await new Promise((resolve) => setTimeout(resolve, 180));
        if (variant === "long-delivery") {
          messageList.scrollTop = 0;
        } else {
          shell.scrollIntoView({ block: "center", inline: "nearest" });
        }
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        );
        if (variant === "result-order") {
          const tool = shell.querySelector('[data-chat-tool-call="1"]');
          const fallbackTool = shell.querySelector(".lantai-tool-card");
          const answer = shell.querySelector('[data-lantai-assistant-answer="1"]:last-of-type') ??
            Array.from(shell.querySelectorAll('[data-lantai-assistant-answer="1"]')).at(-1);
          const composer = document.querySelector(".lantai-chat-anywhere-input");
          const avatar = shell.querySelector('[data-lantai-response-avatar="1"]');
          const viewport = messageList.getBoundingClientRect();
          if (
            !(tool instanceof HTMLElement) ||
            fallbackTool instanceof HTMLElement ||
            !(answer instanceof HTMLElement)
          ) {
            return false;
          }
          const answerRect = answer.getBoundingClientRect();
          const composerRect = composer instanceof HTMLElement
            ? composer.getBoundingClientRect()
            : null;
          const avatarRect = avatar instanceof HTMLElement
            ? avatar.getBoundingClientRect()
            : null;
          const toolPrecedesAnswer = Boolean(
            tool.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING
          );
          const answerClearsComposer = !composerRect ||
            answerRect.bottom <= composerRect.top - 12;
          const answerInsideViewport =
            answerRect.top >= viewport.top && answerRect.bottom <= viewport.bottom;
          const avatarInsideViewport = !avatarRect ||
            (avatarRect.top >= viewport.top && avatarRect.bottom <= viewport.bottom);
          if (
            !toolPrecedesAnswer ||
            !answerClearsComposer ||
            !answerInsideViewport ||
            !avatarInsideViewport
          ) {
            return false;
          }
        } else if (variant === "long-delivery") {
          const answer = shell.querySelector('[data-lantai-assistant-answer="1"]');
          const composer = document.querySelector(".lantai-chat-anywhere-input");
          const avatar = shell.querySelector('[data-lantai-response-avatar="1"]');
          if (
            !(answer instanceof HTMLElement) ||
            !(composer instanceof HTMLElement) ||
            !(avatar instanceof HTMLElement)
          ) {
            return false;
          }
          const answerRect = answer.getBoundingClientRect();
          const composerRect = composer.getBoundingClientRect();
          const avatarRect = avatar.getBoundingClientRect();
          const viewport = messageList.getBoundingClientRect();
          if (
            avatarRect.top < viewport.top ||
            avatarRect.bottom > viewport.bottom ||
            answerRect.top < avatarRect.bottom ||
            answerRect.bottom > composerRect.top - 12
          ) {
            throw new Error(
              "Long delivery geometry failed: " +
                JSON.stringify({
                  viewport: { top: viewport.top, bottom: viewport.bottom },
                  avatar: { top: avatarRect.top, bottom: avatarRect.bottom },
                  answer: { top: answerRect.top, bottom: answerRect.bottom },
                  composer: { top: composerRect.top }
                })
            );
          }
        }
        return true;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(
        `Chat process preview failed: ${
          result.exceptionDetails.exception?.description ??
          result.exceptionDetails.text ??
          "unknown browser exception"
        }`,
      );
    }
    if (result.result?.value !== true) return false;
  }

  if (action.type === "mount-response-status-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => {
        document.getElementById("chat-response-status-review-host")?.remove();
        const messageList = document.querySelector(
          ".lantai-chat-anywhere-message-list"
        );
        let bubbleList = document.querySelector(".lantai-bubble-list");
        const chatStage = document.querySelector('[data-testid="chat-stage"]');
        if (!(messageList instanceof HTMLElement)) return false;
        if (chatStage instanceof HTMLElement) {
          chatStage.dataset.emptyConversation = "false";
        }
        const welcome = document.querySelector(
          ".lantai-chat-anywhere-message-list-welcome"
        );
        if (welcome instanceof HTMLElement) welcome.style.display = "none";
        if (!(bubbleList instanceof HTMLElement)) {
          bubbleList = document.createElement("div");
          bubbleList.className = "lantai-bubble-list";
          messageList.appendChild(bubbleList);
        }
        const promptChips = document.querySelector('[class*="emptyPromptChips"]');
        if (promptChips instanceof HTMLElement) promptChips.style.display = "none";
        const shell = document.createElement("div");
        shell.id = "chat-response-status-review-host";
        shell.className =
          "lantai-bubble-list-item lantai-bubble-assistant lantai-ui-review-tool-preview";
        shell.style.cssText = [
          "width:min(760px,calc(100% - 48px))",
          "margin:18px 0 26px",
          "align-self:flex-start"
        ].join(";");
        shell.innerHTML = \`
          <div data-lantai-response-avatar="1" style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <img src="./lantai.png" alt="" style="width:32px;height:32px;border-radius:999px;object-fit:cover">
            <span>沈行知</span>
          </div>
          <section class="lantai-response-status" data-tone="stopped" role="status">
            <span class="lantai-response-status-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="12" cy="12" r="9"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg></span>
            <div class="lantai-response-status-body"><div class="lantai-response-status-copy"><strong>已停止生成</strong><span>已生成的内容已保留</span></div></div>
            <div class="lantai-response-status-actions"><button type="button">▶ 继续生成</button><button type="button">↻ 重新生成</button></div>
          </section>
        \`;
        bubbleList.appendChild(shell);
        shell.scrollIntoView({ block: "center", inline: "nearest" });
        return true;
      })()`,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "mount-system-action-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const ReactModule = await import("/@id/react");
        const ReactDOMModule = await import("/@id/react-dom/client");
        const React = ReactModule.default ?? ReactModule;
        const ReactDOM = ReactDOMModule.default ?? ReactDOMModule;
        const antd = await import("/@id/antd");
        const responseModule = await import(
          "/src/pages/Chat/components/QuquResponseCard.tsx"
        );
        const QuquResponseCard = responseModule.default;
        document.getElementById("system-action-review-host")?.remove();
        const chatStage = document.querySelector('[data-testid="chat-stage"]');
        const bubbleList = document.querySelector(".lantai-bubble-list");
        const messageList = document.querySelector(
          ".lantai-chat-anywhere-message-list"
        );
        const chatArea = document.querySelector('[class*="chatMessagesArea"]');
        const hostParent =
          bubbleList instanceof HTMLElement
            ? bubbleList
            : messageList instanceof HTMLElement
              ? messageList
              : chatArea instanceof HTMLElement
                ? chatArea
              : null;
        if (!hostParent || !QuquResponseCard) return false;
        if (chatStage instanceof HTMLElement) {
          chatStage.dataset.emptyConversation = "false";
        }
        const welcome = document.querySelector(
          ".lantai-chat-anywhere-message-list-welcome"
        );
        if (welcome instanceof HTMLElement) welcome.style.display = "none";
        const promptChips = document.querySelector('[class*="emptyPromptChips"]');
        if (promptChips instanceof HTMLElement) promptChips.style.display = "none";
        const shell = document.createElement("div");
        shell.id = "system-action-review-host";
        shell.className =
          "lantai-bubble-list-item lantai-bubble-assistant lantai-ui-review-tool-preview";
        shell.style.cssText = [
          "width: min(760px, calc(100% - 48px))",
          bubbleList instanceof HTMLElement
            ? "margin: 18px 0 26px"
            : "margin: 54px auto 26px",
          "align-self: flex-start",
        ].join(";");
        hostParent.appendChild(shell);
        const data = {
          id: "ui-review-system-action",
          status: "completed",
          output: [
            {
              id: "ui-review-system-action-message",
              type: "message",
              status: "completed",
              metadata: {
                source: "legacy",
                metadata: {
                  system_command: "compact",
                  system_action: {
                    kind: "context_compaction",
                    status: "completed",
                    title: "对话已压缩",
                    detail: "后续对话会继续使用这份上下文摘要。",
                    message_count: 29,
                    before_tokens: 262100,
                    after_tokens: 18340,
                    saved_percent: 93,
                    summary: [
                      "# 当前目标",
                      "",
                      "保留用户正在推进的任务，并从压缩后的上下文继续。",
                      "",
                      "# 已知问题",
                      "",
                      "（无）",
                      "",
                      "# 下一步行动",
                      "",
                      "- 继续处理当前请求",
                      "- 仅在需要时展开技术细节"
                    ].join("\\n")
                  }
                }
              },
              content: [
                {
                  type: "text",
                  text: "对话已压缩：29 条消息，262.1k → 18.3k Token。"
                }
              ]
            }
          ]
        };
        ReactDOM.createRoot(shell).render(
          React.createElement(
            antd.ConfigProvider,
            { prefixCls: "lantai" },
            React.createElement(
              antd.App,
              null,
              React.createElement(QuquResponseCard, {
                data,
                isLast: true,
                showThinking: true,
                showToolCalls: true
              })
            )
          )
        );
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        );
        shell.scrollIntoView({ block: "center", inline: "nearest" });
        return true;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "mount-approval-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const ReactModule = await import("/@id/react");
        const ReactDOMModule = await import("/@id/react-dom/client");
        const React = ReactModule.default ?? ReactModule;
        const ReactDOM = ReactDOMModule.default ?? ReactDOMModule;
        const antd = await import("/@id/antd");
        const { ApprovalCard } = await import(
          "/src/components/ApprovalCard/ApprovalCard.tsx"
        );
        const pendingApproval = ${JSON.stringify(
          Boolean(action.pendingApproval),
        )};
        document.getElementById("approval-review-host")?.remove();
        const host = document.createElement("div");
        host.id = "approval-review-host";
        host.style.cssText = [
          "position: fixed",
          "right: clamp(16px, 2vw, 28px)",
          "bottom: 72px",
          "width: min(472px, calc(100vw - 32px))",
          "z-index: 1060"
        ].join(";");
        document.body.appendChild(host);
        const props = {
          requestId: "ui-review",
          toolName: "execute_shell_command",
          severity: "medium",
          findingsCount: 1,
          findingsSummary: "该权限模式下 Bash 副作用命令需要确认。",
          toolParams: {
            command: "npm install && npm run build",
            cwd: "/project/lantai"
          },
          createdAt: Date.now() / 1000,
          timeoutSeconds: 284,
          policyDecision: {
            risk_level: "medium",
            permission_mode: "risk",
            command_category: "shell.side_effect",
            command_head: "npm",
            decision_source: "builtin_policy",
            effective_rule_id: "shell.side_effect"
          },
          approvalOptions: [
            "allow_once",
            "allow_session",
            "allow_workspace",
            "deny",
            "view_rules"
          ],
          rememberable: true,
          keyboardActive: true,
          onApprove: pendingApproval
            ? () => new Promise(() => {})
            : async () => {},
          onDeny: async () => {},
          onCancel: () => {},
          onViewRules: () => {}
        };
        ReactDOM.createRoot(host).render(
          React.createElement(
            antd.ConfigProvider,
            { prefixCls: "lantai" },
            React.createElement(
              antd.App,
              null,
              React.createElement(ApprovalCard, props)
            )
          )
        );
        return true;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "mount-artifact-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const ReactModule = await import("/@id/react");
        const ReactDOMModule = await import("/@id/react-dom/client");
        const React = ReactModule.default ?? ReactModule;
        const ReactDOM = ReactDOMModule.default ?? ReactDOMModule;
        const antd = await import("/@id/antd");
        const { default: ChatArtifactPanel } = await import(
          "/src/pages/Chat/components/ChatArtifactPanel.tsx"
        );
        const { worksApi } = await import("/src/api/modules/works.ts");
        const { chatApi } = await import("/src/api/modules/chat.ts");
        const work = {
          id: "work-report-v1",
          artifact_id: "asset-report",
          title: "项目周报",
          kind: "document",
          path: "/managed/work-report-v1/项目周报.docx",
          source_path: "/project/lantai/项目周报.docx",
          mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          size: 28672,
          source_chat_id: "chat-brand-os",
          source_run_id: "run-brand-os",
          source_digest: "sha256:report-v1",
          artifact_role: "primary",
          completion_bound: true,
          delivery_status: "verified",
          lineage_root_id: "work-report-v1",
          version: 1,
          availability_status: "available",
          integrity_status: "intact",
          created_at: "2026-09-21T08:00:00.000Z",
          updated_at: "2026-09-21T08:05:00.000Z"
        };
        worksApi.list = async () => [work];
        worksApi.textPreview = async () => ({
          work_id: work.id,
          source_digest: work.source_digest,
          kind: "document",
          method: "fixture",
          content_type: "text/markdown",
          content: [
            "# 项目周报",
            "",
            "本周完成 Brand OS 第一批触点的规范对齐与交互验证。",
            "",
            "## 已完成",
            "",
            "- 输入区与工具菜单",
            "- 授权状态闭环",
            "- 成果面板与金山文档响应式边界"
          ].join("\\n"),
          truncated: false,
          message: ""
        });
        chatApi.getChat = async () => ({
          chat_id: "chat-brand-os",
          messages: [],
          meta: {},
          runtime_state: {
            run_id: "run-brand-os",
            status: "completed",
            outcome_status: "delivered",
            delivery_status: "verified"
          }
        });
        document.getElementById("artifact-review-host")?.remove();
        const host = document.createElement("div");
        host.id = "artifact-review-host";
        host.style.cssText = [
          "position: fixed",
          "inset: 0",
          "z-index: 1070",
          "height: 100vh",
          "background: var(--lt-workspace-bg)"
        ].join(";");
        document.body.appendChild(host);
        const stage = React.createElement(
          "main",
          {
            style: {
              minWidth: 0,
              flex: "1 1 auto",
              padding: "clamp(28px, 5vw, 72px)",
              display: "flex",
              flexDirection: "column",
              gap: "28px",
              color: "var(--lt-text)",
              background: "var(--lt-workspace-bg)"
            }
          },
          React.createElement(
            "header",
            { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            React.createElement("span", { style: { fontSize: "13px", color: "var(--lt-text-muted)" } }, "任务 · 项目周报"),
            React.createElement("span", { style: { fontSize: "12px", color: "var(--lt-accent)" } }, "已完成")
          ),
          React.createElement(
            "section",
            {
              style: {
                width: "min(680px, 100%)",
                margin: "auto",
                padding: "28px 30px",
                border: "1px solid var(--lt-border-subtle)",
                borderRadius: "20px",
                background: "var(--lt-surface)",
                boxShadow: "var(--lt-shadow-sm)"
              }
            },
            React.createElement("strong", { style: { display: "block", marginBottom: "10px", fontSize: "18px" } }, "周报已经整理完成"),
            React.createElement("p", { style: { margin: 0, color: "var(--lt-text-muted)", lineHeight: 1.7 } }, "我保留了原始材料，并把可交付文件、版本和打开动作收进右侧成果面板。")
          )
        );
        const shell = React.createElement(
          "div",
          {
            style: {
              position: "relative",
              width: "100%",
              height: "100%",
              minWidth: 0,
              display: "flex",
              overflow: "hidden",
              background: "var(--lt-workspace-bg)"
            }
          },
          stage,
          React.createElement(ChatArtifactPanel, {
            open: true,
            chatId: "chat-brand-os",
            onClose: () => {}
          })
        );
        ReactDOM.createRoot(host).render(
          React.createElement(
            antd.ConfigProvider,
            { prefixCls: "lantai" },
            React.createElement(antd.App, { style: { height: "100%" } }, shell)
          )
        );
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        );
        await new Promise((resolve) => setTimeout(resolve, 180));
        return true;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "mount-kdocs-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const ReactModule = await import("/@id/react");
        const ReactDOMModule = await import("/@id/react-dom/client");
        const React = ReactModule.default ?? ReactModule;
        const ReactDOM = ReactDOMModule.default ?? ReactDOMModule;
        const antd = await import("/@id/antd");
        const { default: KdocsWorkspacePanel } = await import(
          "/src/pages/Chat/components/Kdocs/KdocsWorkspacePanel.tsx"
        );
        const documentItem = {
          key: "drive-a:file-a",
          drive_id: "drive-a",
          file_id: "file-a",
          parent_id: "0",
          name: "品牌系统实施计划.docx",
          type: "docx",
          suffix: "docx",
          is_folder: false,
          size: 28672,
          version: "8",
          content_hash: "sha256:kdocs-v8",
          link_id: "",
          original_url: "https://www.kdocs.cn/l/brand-system",
          location: "项目资料 / 品牌系统",
          owner: null,
          modifier: null,
          shared: false,
          permissions: { can_read: true, can_edit: true }
        };
        const reference = {
          schema_version: 1,
          key: "drive-a:file-a:file",
          kind: "file",
          drive_id: "drive-a",
          file_id: "file-a",
          name_snapshot: documentItem.name,
          path_snapshot: documentItem.location,
          suffix: documentItem.suffix,
          original_url: documentItem.original_url,
          read_status: "read",
          read_snapshot: {
            version: "8",
            content_hash: "sha256:kdocs-v8",
            read_at: "2026-09-21T08:00:00.000Z"
          },
          content_snapshot: {
            text: [
              "# 品牌系统实施计划",
              "",
              "工作界面保持安静，品牌绿只标记关键状态与动作。",
              "",
              "## 本批范围",
              "",
              "- 输入区与工具菜单",
              "- 授权卡与成果面板",
              "- 金山文档侧栏与 Daily Brief 入口"
            ].join("\\n"),
            format: "markdown",
            truncated: false
          }
        };
        document.getElementById("kdocs-review-host")?.remove();
        const host = document.createElement("div");
        host.id = "kdocs-review-host";
        host.style.cssText = [
          "position: fixed",
          "inset: 0",
          "z-index: 1070",
          "height: 100vh",
          "background: var(--lt-workspace-bg)"
        ].join(";");
        document.body.appendChild(host);
        const stage = React.createElement(
          "main",
          {
            style: {
              minWidth: 0,
              flex: "1 1 auto",
              padding: "clamp(28px, 5vw, 72px)",
              display: "flex",
              flexDirection: "column",
              gap: "28px",
              color: "var(--lt-text)",
              background: "var(--lt-workspace-bg)"
            }
          },
          React.createElement(
            "header",
            { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            React.createElement("span", { style: { fontSize: "13px", color: "var(--lt-text-muted)" } }, "对话 · 品牌系统"),
            React.createElement("span", { style: { fontSize: "12px", color: "var(--lt-accent)" } }, "已引用 1 份文档")
          ),
          React.createElement(
            "section",
            {
              style: {
                width: "min(680px, 100%)",
                margin: "auto",
                padding: "28px 30px",
                border: "1px solid var(--lt-border-subtle)",
                borderRadius: "20px",
                background: "var(--lt-surface)"
              }
            },
            React.createElement("strong", { style: { display: "block", marginBottom: "10px", fontSize: "18px" } }, "引用金山文档继续工作"),
            React.createElement("p", { style: { margin: 0, color: "var(--lt-text-muted)", lineHeight: 1.7 } }, "文档正文、版本信息和引用动作保留在右侧工作面板，聊天区继续承载任务上下文。")
          )
        );
        const shell = React.createElement(
          "div",
          {
            style: {
              position: "relative",
              width: "100%",
              height: "100%",
              minWidth: 0,
              display: "flex",
              overflow: "hidden",
              background: "var(--lt-workspace-bg)"
            }
          },
          stage,
          React.createElement(KdocsWorkspacePanel, {
            open: true,
            references: [reference],
            requestedDocument: documentItem,
            requestedReference: reference,
            onRequestedDocumentHandled: () => {},
            onClose: () => {},
            onAddDocument: () => {},
            onAddExcerpt: () => {},
            onRemoveDocument: () => {},
            onMarkDocumentUpdated: () => {}
          })
        );
        ReactDOM.createRoot(host).render(
          React.createElement(
            antd.ConfigProvider,
            { prefixCls: "lantai" },
            React.createElement(antd.App, { style: { height: "100%" } }, shell)
          )
        );
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        );
        await new Promise((resolve) => setTimeout(resolve, 180));
        return true;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "mount-image-generation-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const ReactModule = await import("/@id/react");
        const ReactDOMModule = await import("/@id/react-dom/client");
        const React = ReactModule.default ?? ReactModule;
        const ReactDOM = ReactDOMModule.default ?? ReactDOMModule;
        const antd = await import("/@id/antd");
        const cardModule = await import(
          "/src/pages/Chat/components/ImageGenerationToolCard.tsx"
        );
        const ImageGenerationToolCard =
          cardModule.default ?? cardModule.ImageGenerationToolCard;
        const variant = ${JSON.stringify(action.variant)};
        const fixtureUrl = ${JSON.stringify(imageGenerationFixtureDataUrl)};
        if (variant === "completed" && !fixtureUrl) return false;
        document.getElementById("image-generation-review-host")?.remove();
        const bubbleList = document.querySelector(".lantai-bubble-list");
        if (!(bubbleList instanceof HTMLElement)) return false;
        const shell = document.createElement("div");
        shell.id = "image-generation-review-host";
        shell.className =
          "lantai-bubble-list-item lantai-bubble-assistant lantai-ui-review-tool-preview";
        shell.style.cssText = [
          "width: min(100%, 660px)",
          "margin: 18px 0 26px",
          "align-self: flex-start"
        ].join(";");
        bubbleList.appendChild(shell);
        const prompt =
          "中国古诗《静夜思》意境插画：月光、窗棂与远山，安静克制。";
        const call = {
          data: {
            name: "generate_image",
            arguments: { prompt, size: "1024x1024", quality: "high" }
          }
        };
        let data;
        if (variant === "running") {
          data = { status: "running", content: [call] };
        } else if (variant === "failed") {
          data = {
            status: "failed",
            content: [
              call,
              {
                data: {
                  name: "generate_image",
                  error: "首选模型暂时不可用，已完成降级尝试。请稍后重试。"
                }
              }
            ]
          };
        } else {
          data = {
            status: "completed",
            content: [
              call,
              {
                data: {
                  name: "generate_image",
                  output: {
                    content: [
                      {
                        type: "image",
                        image_url: fixtureUrl,
                        mime_type: "image/png"
                      },
                      {
                        type: "text",
                        text: "已按专用生图模型优先级生成 1 张图片。"
                      }
                    ]
                  }
                }
              }
            ]
          };
        }
        ReactDOM.createRoot(shell).render(
          React.createElement(
            antd.ConfigProvider,
            { prefixCls: "lantai" },
            React.createElement(
              antd.App,
              null,
              React.createElement(ImageGenerationToolCard, { data })
            )
          )
        );
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        );
        shell.scrollIntoView({ block: "start", inline: "nearest" });
        const scrollRoot = shell.closest(".lantai-chat-scroll");
        if (scrollRoot instanceof HTMLElement) {
          scrollRoot.scrollTop = Math.max(0, scrollRoot.scrollTop - 72);
        }
        return true;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  if (action.type === "mount-structured-document-preview") {
    const result = await client.send("Runtime.evaluate", {
      expression: `(async () => {
        const ReactModule = await import("/@id/react");
        const ReactDOMModule = await import("/@id/react-dom/client");
        const React = ReactModule.default ?? ReactModule;
        const ReactDOM = ReactDOMModule.default ?? ReactDOMModule;
        const antd = await import("/@id/antd");
        const contentModule = await import(
          "/src/pages/Chat/components/StructuredContent.tsx"
        );
        const markdownModule = await import("/src/vendor/lantaiChat.ts");
        const DocumentOutput = contentModule.DocumentOutput;
        const Markdown = markdownModule.Markdown;
        if (!DocumentOutput || !Markdown) return false;
        const content = [
          "# 灵犀专业版最新新闻总结",
          "",
          "金山办公在 AI 生产力大会上发布灵犀专业版，定位为面向个人的专业 AI 办公助理。",
          "",
          "## 产品定位",
          "",
          "- 独立产品，不是 WPS 里的一个 AI 按钮。",
          "- 支持网页端和移动端，并与办公文档流程衔接。",
          "- 以项目为单位管理对话、资料、技能和任务。",
          "",
          "## 核心卖点",
          "",
          "1. 懂上下文：减少重复解释背景，并持续更新个人知识。",
          "2. 能交付：调用文档、数据、浏览器与代码能力推进任务。",
          "3. 专业 Office 操作：交付可继续编辑的原生文件。"
        ].join("\\n");
        document.getElementById("structured-document-review-host")?.remove();
        const bubbleList =
          document.querySelector(".lantai-bubble-list") ??
          document.querySelector('[class*="chatMessagesArea"]');
        if (!(bubbleList instanceof HTMLElement)) return false;
        if (!bubbleList.classList.contains("lantai-bubble-list")) {
          Array.from(bubbleList.children).forEach((child) => {
            if (child instanceof HTMLElement) child.style.display = "none";
          });
          bubbleList.style.display = "flex";
          bubbleList.style.flexDirection = "column";
          bubbleList.style.justifyContent = "flex-start";
          bubbleList.style.padding = "88px 48px 120px";
          bubbleList.style.overflowY = "auto";
        }
        const shell = document.createElement("div");
        shell.id = "structured-document-review-host";
        shell.className =
          "lantai-bubble-list-item lantai-bubble-assistant lantai-ui-review-tool-preview";
        shell.style.cssText = [
          "width: min(100%, 760px)",
          "margin: 0 auto 26px",
          "align-self: flex-start"
        ].join(";");
        bubbleList.appendChild(shell);
        ReactDOM.createRoot(shell).render(
          React.createElement(
            antd.ConfigProvider,
            { prefixCls: "lantai" },
            React.createElement(
              antd.App,
              null,
              React.createElement(
                DocumentOutput,
                { content },
                React.createElement(Markdown, {
                  content,
                  cursor: false
                })
              )
            )
          )
        );
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))
        );
        shell.scrollIntoView({ block: "start", inline: "nearest" });
        const scrollRoot = shell.closest(".lantai-chat-scroll");
        if (scrollRoot instanceof HTMLElement) {
          scrollRoot.scrollTop = Math.max(0, scrollRoot.scrollTop - 72);
        }
        return true;
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.result?.value !== true) return false;
  }

  return action.expectedText
    ? waitForText(client, action.expectedText)
    : (await delay(500), true);
}

async function performActions(client, actions) {
  for (const action of actions) {
    const settled = await performAction(client, action);
    if (!settled) return false;
  }
  return true;
}

function scenarioName(scenario) {
  const { route, theme, motion, width, height, fontScale = 1 } = scenario;
  const motionSuffix = motion === "reduce" ? "-reduced" : "";
  const fontSuffix =
    fontScale > 1 ? `-large-text-${Math.round(fontScale * 100)}` : "";
  return `${label}-${route.id}-${theme}-${width}x${height}${motionSuffix}${fontSuffix}`;
}

async function readControlStyle(client, selector) {
  const result = await client.send("Runtime.evaluate", {
    expression: `(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      if (!(element instanceof HTMLElement)) return null;
      const style = getComputedStyle(element);
      return {
        color: style.color,
        backgroundColor: style.backgroundColor,
        borderColor: style.borderColor,
        outline: style.outline,
        boxShadow: style.boxShadow,
        transform: style.transform,
        opacity: style.opacity,
        cursor: style.cursor,
        className: String(element.className || ""),
        disabled: "disabled" in element ? Boolean(element.disabled) : false,
        ariaBusy: element.getAttribute("aria-busy") || "",
        ariaDisabled: element.getAttribute("aria-disabled") || ""
      };
    })()`,
    returnByValue: true,
  });
  return result.result?.value ?? null;
}

async function setControlDisabled(client, selector, disabled) {
  const result = await client.send("Runtime.evaluate", {
    expression: `(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      if (!(element instanceof HTMLElement) || !("disabled" in element)) {
        return false;
      }
      element.disabled = ${disabled ? "true" : "false"};
      return true;
    })()`,
    returnByValue: true,
  });
  return result.result?.value === true;
}

async function captureControlStateAudit(client, descriptor) {
  const documentNode = await client.send("DOM.getDocument", { depth: 0 });
  const query = await client.send("DOM.querySelector", {
    nodeId: documentNode.root.nodeId,
    selector: descriptor.selector,
  });
  if (!query.nodeId) {
    return { ...descriptor, exists: false, states: {} };
  }

  const initial = await readControlStyle(client, descriptor.selector);
  const originalDisabled = Boolean(initial?.disabled);
  const states = {};
  try {
    await setControlDisabled(client, descriptor.selector, false);
    await delay(180);
    states.normal = await readControlStyle(client, descriptor.selector);

    const pseudoStates = [
      ["hover", ["hover"]],
      ["pressed", ["active"]],
      ["focus", ["focus", "focus-visible"]],
    ];
    for (const [stateName, forcedPseudoClasses] of pseudoStates) {
      await client.send("CSS.forcePseudoState", {
        nodeId: query.nodeId,
        forcedPseudoClasses,
      });
      await delay(180);
      states[stateName] = await readControlStyle(client, descriptor.selector);
    }

    await client.send("CSS.forcePseudoState", {
      nodeId: query.nodeId,
      forcedPseudoClasses: [],
    });
    await setControlDisabled(client, descriptor.selector, true);
    await delay(180);
    states.disabled = await readControlStyle(client, descriptor.selector);
  } finally {
    await client.send("CSS.forcePseudoState", {
      nodeId: query.nodeId,
      forcedPseudoClasses: [],
    });
    await setControlDisabled(client, descriptor.selector, originalDisabled);
    await delay(180);
  }

  return {
    ...descriptor,
    exists: true,
    originalDisabled,
    states,
  };
}

async function captureScenario(port, scenario) {
  const target = await openTarget(port);
  const client = createCdpClient(target.webSocketDebuggerUrl);
  const consoleErrors = [];
  const networkRequests = [];
  await client.opened;

  try {
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    await client.send("DOM.enable");
    await client.send("CSS.enable");
    await client.send("Network.enable");
    client.on("Network.requestWillBeSent", ({ request, type }) => {
      if (!request?.url) return;
      networkRequests.push({
        method: String(request.method || "GET").toUpperCase(),
        url: request.url,
        resourceType: type || "",
      });
    });
    if (desktopToken) {
      await client.send("Network.setExtraHTTPHeaders", {
        headers: { "X-Lantai-Desktop-Token": desktopToken },
      });
    }
    client.on("Runtime.consoleAPICalled", ({ type, args = [] }) => {
      if (type !== "error") return;
      consoleErrors.push(
        args.map((arg) => arg.value || arg.description || "").join(" "),
      );
    });
    await client.send("Emulation.setDeviceMetricsOverride", {
      width: scenario.width,
      height: scenario.height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await client.send("Emulation.setEmulatedMedia", {
      features: [
        {
          name: "prefers-color-scheme",
          value: scenario.theme,
        },
        {
          name: "prefers-reduced-motion",
          value: scenario.motion === "reduce" ? "reduce" : "no-preference",
        },
        {
          name: "prefers-contrast",
          value: scenario.contrast === "more" ? "more" : "no-preference",
        },
      ],
    });
    await client.send("Page.addScriptToEvaluateOnNewDocument", {
      source: `
        const __lantaiCaptureTheme = ${JSON.stringify(scenario.theme)};
        const __lantaiCapturePalette = ${JSON.stringify(
          scenario.palette || "mist",
        )};
        localStorage.setItem("lantai-theme", __lantaiCaptureTheme);
        localStorage.setItem("lantai-density", "comfortable");
        localStorage.setItem("lantai-appearance-palette", __lantaiCapturePalette);
        localStorage.setItem("language", "zh");
        localStorage.setItem("lantai-sidebar-collapsed", "0");
        localStorage.removeItem("lantai:chat-artifact-panel-width");
        const __lantaiCaptureFetch = window.fetch.bind(window);
        window.fetch = (input, init) => {
          const url = typeof input === "string" ? input : input?.url || "";
          const method = String(init?.method || input?.method || "GET").toUpperCase();
          if (method === "GET" && url.includes("/api/auth/status")) {
            return Promise.resolve(new Response(JSON.stringify({
              enabled: false,
              has_users: false
            }), {
              status: 200,
              headers: { "Content-Type": "application/json" }
            }));
          }
          if (method === "GET" && url.includes("/api/settings/appearance")) {
            return Promise.resolve(new Response(JSON.stringify({
              theme_mode: __lantaiCaptureTheme,
              density_mode: "comfortable",
              appearance_palette: __lantaiCapturePalette
            }), {
              status: 200,
              headers: { "Content-Type": "application/json" }
            }));
          }
          if (method === "GET" && url.includes("/api/mcp/catalog")) {
            return Promise.resolve(new Response("[]", {
              status: 200,
              headers: { "Content-Type": "application/json" }
            }));
          }
          if (
            method === "GET" &&
            (url.endsWith("/api/mcp") || url.includes("/api/mcp?"))
          ) {
            return Promise.resolve(new Response("[]", {
              status: 200,
              headers: { "Content-Type": "application/json" }
            }));
          }
          if (method === "GET" && url.includes("/api/skills/integrations/kdocs/status")) {
            return Promise.resolve(new Response(JSON.stringify({
              installed: true,
              authenticated: false,
              cli_version: "preview",
              token_url: ""
            }), {
              status: 200,
              headers: { "Content-Type": "application/json" }
            }));
          }
          if (method === "GET" && url.includes("/api/skills/integrations/wps-note/status")) {
            return Promise.resolve(new Response(JSON.stringify({
              installed: true,
              desktop_installed: true,
              connected: false,
              cli_version: "preview",
              tool_count: 0,
              install_method: "wps_note_desktop",
              repository_url: "",
              download_url: "",
              next_step: "enable_in_wps_note",
              launch_requested: false
            }), {
              status: 200,
              headers: { "Content-Type": "application/json" }
            }));
          }
          return __lantaiCaptureFetch(input, init);
        };
      `,
    });

    const loaded = waitForLoadEvent(client);
    const pageUrl = `${baseUrl.replace(/#.*$/, "").replace(/\/$/, "")}/#${scenario.route.path}`;
    await client.send("Page.navigate", { url: pageUrl });
    await loaded;
    if ((scenario.fontScale || 1) !== 1) {
      const fontScale = Number(scenario.fontScale);
      if (!Number.isFinite(fontScale) || fontScale < 1 || fontScale > 2) {
        throw new Error(
          `Invalid fontScale for ${scenario.route.id}: ${scenario.fontScale}`,
        );
      }
      await client.send("Runtime.evaluate", {
        expression: `(() => {
          document.documentElement.style.zoom = ${JSON.stringify(String(fontScale))};
          return document.documentElement.style.zoom;
        })()`,
        returnByValue: true,
      });
      await client.send("Runtime.evaluate", {
        expression:
          "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
        awaitPromise: true,
      });
    }
    const settled = await waitForApplication(
      client,
      scenario.route.expectedText,
    );
    const actionSettled = await performActions(
      client,
      scenario.actions || (scenario.action ? [scenario.action] : []),
    );
    await waitForImages(client);
    const finalSettled = await waitForApplication(
      client,
      scenario.finalExpectedText || scenario.route.expectedText,
    );

    const pageState = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const page = document.querySelector(".page-container");
        const rect = page?.getBoundingClientRect();
        const interactive = Array.from(document.querySelectorAll(
          'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )).filter((element) => {
          const style = getComputedStyle(element);
          const target = element.getBoundingClientRect();
          return style.display !== "none" && style.visibility !== "hidden" && target.width > 0 && target.height > 0;
        });
        const unlabeledInteractive = interactive.filter((element) => {
          const text = (
            element.getAttribute("aria-label") ||
            element.getAttribute("title") ||
            element.labels?.[0]?.textContent ||
            element.textContent ||
            ""
          ).trim();
          return !text;
        });
        const horizontalOverflow = Math.max(
          document.documentElement.scrollWidth,
          document.body.scrollWidth
        ) - innerWidth;
        const styleAudits = ${JSON.stringify(
          scenario.styleAudits || [],
        )}.map(({ id, selector }) => {
          const element = document.querySelector(selector);
          if (!(element instanceof HTMLElement)) {
            return { id, selector, exists: false };
          }
          const style = getComputedStyle(element);
          const target = element.getBoundingClientRect();
          return {
            id,
            selector,
            exists: true,
            color: style.color,
            backgroundColor: style.backgroundColor,
            borderColor: style.borderColor,
            outline: style.outline,
            boxShadow: style.boxShadow,
            rect: {
              x: Math.round(target.x),
              y: Math.round(target.y),
              width: Math.round(target.width),
              height: Math.round(target.height)
            },
            layout: element.dataset.layout || "",
            shellLayout: element.dataset.shellLayout || "",
            role: element.getAttribute("role") || "",
            ariaModal: element.getAttribute("aria-modal") || "",
            focused: document.activeElement === element,
            className: String(element.className || ""),
            disabled: "disabled" in element ? Boolean(element.disabled) : false,
            ariaBusy: element.getAttribute("aria-busy") || "",
            ariaDisabled: element.getAttribute("aria-disabled") || ""
          };
        });
        const layoutAudits = ${JSON.stringify(
          scenario.layoutAudits || [],
        )}.map(({
          id,
          upperSelector,
          lowerSelector,
          minVerticalGap = 0,
          maxInlineEdgeDelta,
        }) => {
          const upper = document.querySelector(upperSelector);
          const lower = document.querySelector(lowerSelector);
          if (!(upper instanceof HTMLElement) || !(lower instanceof HTMLElement)) {
            return {
              id,
              upperSelector,
              lowerSelector,
              exists: false,
              passed: false,
            };
          }
          const upperRect = upper.getBoundingClientRect();
          const lowerRect = lower.getBoundingClientRect();
          const verticalGap = Math.round((lowerRect.top - upperRect.bottom) * 100) / 100;
          const inlineStartDelta = Math.round(Math.abs(lowerRect.left - upperRect.left) * 100) / 100;
          const inlineEndDelta = Math.round(Math.abs(lowerRect.right - upperRect.right) * 100) / 100;
          const inlineEdgesPass =
            maxInlineEdgeDelta == null ||
            (inlineStartDelta <= maxInlineEdgeDelta && inlineEndDelta <= maxInlineEdgeDelta);
          return {
            id,
            upperSelector,
            lowerSelector,
            exists: true,
            passed: verticalGap >= minVerticalGap && inlineEdgesPass,
            verticalGap,
            minVerticalGap,
            inlineStartDelta,
            inlineEndDelta,
            maxInlineEdgeDelta: maxInlineEdgeDelta ?? null,
          };
        });
        return {
          title: document.title,
          route: location.hash,
          theme: document.documentElement.dataset.theme,
          width: innerWidth,
          height: innerHeight,
          bodyTextLength: (document.body.innerText || "").length,
          bodyText: (document.body.innerText || "").slice(0, 300),
          loadingText: (document.body.innerText || "").match(/(?:正在)?加载.{0,24}(?:中|…|[.]{3})/)?.[0] || "",
          contrast: matchMedia("(prefers-contrast: more)").matches ? "more" : "no-preference",
          reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
          fontScale: Number.parseFloat(document.documentElement.style.zoom || "1") || 1,
          interactiveCount: interactive.length,
          unlabeledInteractiveCount: unlabeledInteractive.length,
          unlabeledInteractive: unlabeledInteractive.map((element) => ({
            tag: element.tagName,
            className: String(element.className || "").slice(0, 160),
            role: element.getAttribute("role") || "",
            type: element.getAttribute("type") || "",
            html: element.outerHTML.slice(0, 300),
          })),
          styleAudits,
          layoutAudits,
          horizontalOverflow,
          shellVisible: Boolean(
            page &&
            rect &&
            rect.width > 600 &&
            rect.height > 400
          )
        };
      })()`,
      returnByValue: true,
    });
    const capturedState = pageState.result?.value;
    const controlStateAudits = [];
    for (const descriptor of scenario.controlStateAudits || []) {
      controlStateAudits.push(
        await captureControlStateAudit(client, descriptor),
      );
    }
    let keyboardAudit = { passed: true, focusedCount: 0, labels: [] };
    if (scenario.keyboardAudit) {
      const labels = [];
      for (let index = 0; index < 12; index += 1) {
        await client.send("Input.dispatchKeyEvent", {
          type: "keyDown",
          key: "Tab",
          code: "Tab",
          windowsVirtualKeyCode: 9,
          nativeVirtualKeyCode: 9,
        });
        await client.send("Input.dispatchKeyEvent", {
          type: "keyUp",
          key: "Tab",
          code: "Tab",
          windowsVirtualKeyCode: 9,
          nativeVirtualKeyCode: 9,
        });
        const focused = await client.send("Runtime.evaluate", {
          expression: `(() => {
            const active = document.activeElement;
            if (!active || active === document.body) return "";
            return (active.getAttribute("aria-label") || active.getAttribute("title") || active.textContent || active.tagName).trim().slice(0, 120);
          })()`,
          returnByValue: true,
        });
        if (focused.result?.value) labels.push(focused.result.value);
      }
      keyboardAudit = {
        passed: new Set(labels).size >= 3,
        focusedCount: labels.length,
        labels,
      };
    }
    const ignoredConsoleErrors = scenario.ignoredConsoleErrors || [];
    const capturedErrors = consoleErrors
      .filter(Boolean)
      .filter(
        (error) =>
          !ignoredConsoleErrors.some((ignored) => error.includes(ignored)),
      );

    if (
      !settled ||
      !actionSettled ||
      !finalSettled ||
      !capturedState?.shellVisible ||
      capturedState?.styleAudits?.some((audit) => !audit.exists) ||
      capturedState?.layoutAudits?.some(
        (audit) => !audit.exists || !audit.passed,
      ) ||
      controlStateAudits.some(
        (audit) =>
          !audit.exists ||
          ["normal", "hover", "pressed", "focus", "disabled"].some(
            (state) => !audit.states?.[state],
          ),
      ) ||
      capturedState.bodyTextLength < 40 ||
      capturedState.horizontalOverflow > 1 ||
      capturedState.unlabeledInteractiveCount > 0 ||
      !keyboardAudit.passed ||
      capturedErrors.length > 0
    ) {
      throw new Error(
        `Scenario ${scenario.route.id} was not capture-ready: ${JSON.stringify({
          settled,
          actionSettled,
          finalSettled,
          pageState: capturedState,
          controlStateAudits,
          keyboardAudit,
          consoleErrors: capturedErrors,
        })}`,
      );
    }

    const screenshot = await client.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false,
    });
    const filename = `${scenarioName(scenario)}.png`;
    const filePath = path.join(outputDir, filename);
    fs.writeFileSync(filePath, Buffer.from(screenshot.data, "base64"));

    const apiRequests = networkRequests
      .map((request) => {
        try {
          const url = new URL(request.url);
          return {
            method: request.method,
            path: `${url.pathname}${url.search}`,
            resourceType: request.resourceType,
          };
        } catch {
          return null;
        }
      })
      .filter((request) => request?.path.startsWith("/api/"));
    const apiRequestCounts = apiRequests.reduce((counts, request) => {
      const key = `${request.method} ${request.path}`;
      counts[key] = (counts[key] || 0) + 1;
      return counts;
    }, {});
    const dailyBriefApiRequests = apiRequests.filter((request) =>
      request.path.startsWith("/api/daily-brief/"),
    );
    const dailyBriefApiRequestCounts = dailyBriefApiRequests.reduce(
      (counts, request) => {
        const key = `${request.method} ${request.path}`;
        counts[key] = (counts[key] || 0) + 1;
        return counts;
      },
      {},
    );

    return {
      ...scenario,
      route: scenario.route,
      file: filename,
      pageState: capturedState,
      controlStateAudits,
      keyboardAudit,
      settled: settled && finalSettled,
      actionSettled,
      finalSettled,
      consoleErrors: capturedErrors,
      apiRequests,
      apiRequestCounts,
      apiMutationCount: apiRequests.filter(
        (request) => !["GET", "HEAD", "OPTIONS"].includes(request.method),
      ).length,
      dailyBriefApiRequests,
      dailyBriefApiRequestCounts,
      dailyBriefMutationCount: dailyBriefApiRequests.filter(
        (request) => !["GET", "HEAD", "OPTIONS"].includes(request.method),
      ).length,
    };
  } finally {
    client.close();
    if (target.id) {
      await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`).catch(
        () => undefined,
      );
    }
  }
}

async function stopProcess(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill("SIGTERM");
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill("SIGKILL");
      }
      resolve();
    }, 1500);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

async function run() {
  fs.mkdirSync(outputDir, { recursive: true });
  const response = await fetch(baseUrl, {
    headers: desktopToken
      ? { "X-Lantai-Desktop-Token": desktopToken }
      : undefined,
  });
  if (!response.ok) {
    throw new Error(
      `Application server returned ${response.status}: ${baseUrl}`,
    );
  }

  const chromePath = resolveChromePath();
  const debugPort = await getFreePort();
  const profileDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-ui-review-chrome-"),
  );
  const chrome = spawn(chromePath, chromeArgs(debugPort, profileDir), {
    stdio: ["ignore", "ignore", "ignore"],
  });

  try {
    await waitForJson(`http://127.0.0.1:${debugPort}/json/version`);
    const captures = [];
    for (const scenario of scenarios) {
      const result = await captureScenario(debugPort, scenario);
      captures.push(result);
      process.stdout.write(`Captured ${path.basename(result.file)}\n`);
    }

    const manifestPath = path.join(outputDir, `${label}-manifest.json`);
    fs.writeFileSync(
      manifestPath,
      JSON.stringify(
        {
          capturedAt: new Date().toISOString(),
          source: baseUrl,
          desktopAuth: desktopToken ? "injected" : "none",
          renderer: "chrome-headless-cdp",
          label,
          profile,
          sourceFingerprint: captureSourceFingerprint(),
          routes,
          scenarios: captures,
        },
        null,
        2,
      ),
    );
    process.stdout.write(`UI review captures written to ${outputDir}\n`);
  } finally {
    await stopProcess(chrome);
    fs.rmSync(profileDir, { recursive: true, force: true });
  }
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
