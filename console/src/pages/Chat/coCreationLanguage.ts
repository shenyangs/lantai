export type CoCreationSurfaceKind =
  | "conversation"
  | "office"
  | "code"
  | "mini-app"
  | "executable";

export type CoCreationPhase =
  | "observing"
  | "proposing"
  | "working"
  | "reviewing"
  | "applied"
  | "recovering";

export const CO_CREATION_SURFACES = {
  conversation: {
    id: "conversation",
    label: "对话",
    objectLabel: "当前任务",
  },
  office: {
    id: "office",
    label: "Office 共同创作",
    objectLabel: "当前文档与选区",
  },
  artifact: {
    id: "artifact",
    label: "产物工作台",
    objectLabel: "代码、小程序与可执行文件",
  },
} as const;

export const CO_CREATION_PHASE_LABELS: Record<CoCreationPhase, string> = {
  observing: "正在读取当前对象",
  proposing: "正在组织修改方案",
  working: "正在执行修改",
  reviewing: "等待你审阅",
  applied: "已应用，可撤销",
  recovering: "正在恢复任务现场",
};

export const CO_CREATION_OBJECT_LABELS: Record<CoCreationSurfaceKind, string> = {
  conversation: "任务",
  office: "Office 文档",
  code: "代码",
  "mini-app": "小程序",
  executable: "可执行文件",
};

export function coCreationSurfaceForFile(path: string): CoCreationSurfaceKind {
  if (/\.(?:docx|xlsx?|csv|pptx|pdf)$/i.test(path)) return "office";
  if (/\.(?:html?|css|jsx?|tsx?|vue|svelte)$/i.test(path)) return "mini-app";
  if (/\.(?:app|exe|dmg|pkg|msi|bin)$/i.test(path)) return "executable";
  return "code";
}

export function coCreationObjectLabelForFile(path: string): string {
  return CO_CREATION_OBJECT_LABELS[coCreationSurfaceForFile(path)];
}
