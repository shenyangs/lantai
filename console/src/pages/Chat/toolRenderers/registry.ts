import { buildVibeCodingSignal } from "../vibeCodingSignals";

export type ToolStatus =
  | "running"
  | "done"
  | "failed"
  | "pending"
  | "unknown";

export interface ToolRenderContext {
  toolName: string;
  args: unknown;
  status: ToolStatus;
  argsText?: string;
  resultText?: string;
}

export interface ToolRenderer {
  getDisplayName?: (context: ToolRenderContext) => string;
  getSummary?: (context: ToolRenderContext) => string;
}

const SUMMARY_LIMIT = 90;

function tryParseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed) return value;
  if (!/^[{["]/.test(trimmed)) return value;

  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

export function truncateSummary(value: string, limit = SUMMARY_LIMIT): string {
  const text = normalizeWhitespace(value);
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}...`;
}

export function readPathName(path?: unknown): string {
  if (typeof path !== "string") return "";
  const normalized = path.replace(/\\/g, "/");
  return normalized.split("/").filter(Boolean).pop() || path;
}

function asPlainRecord(value: unknown): Record<string, unknown> | null {
  const parsed = tryParseJson(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return null;
  }
  return parsed as Record<string, unknown>;
}

function getStringField(
  record: Record<string, unknown>,
  keys: string[],
): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
  }
  return "";
}

function invokesShellCommand(text: string, commandPattern: string): boolean {
  const commandStart = String.raw`(?:^|(?:&&|\|\||[;|\n])\s*)`;
  const wrappers = String.raw`(?:(?:command|builtin|sudo|env)\s+)*`;
  const executablePath = String.raw`(?:[\w@%+=:,./-]+/)?`;
  return new RegExp(
    `${commandStart}${wrappers}${executablePath}(?:${commandPattern})(?=\\s|$)`,
    "i",
  ).test(text);
}

export function commandPhaseLabel(command: string): string {
  const text = command.toLowerCase();
  if (
    /\b(npm|pnpm|yarn|bun)\s+(install|add|i)\b|\b(pip|uv|poetry|bundle)\s+(install|add|sync)\b/.test(
      text,
    )
  ) {
    return "安装依赖";
  }
  if (
    /\b(deploy|release|vercel|wrangler|firebase deploy|cloudbase|tcb)\b/.test(
      text,
    )
  ) {
    return "发布部署";
  }
  if (
    /\b(pytest|vitest|jest|playwright|cypress)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?(test|vitest|jest)\b|\b(go|cargo|swift)\s+test\b|\bxcodebuild\b.*\btest\b/.test(
      text,
    )
  ) {
    return "运行测试";
  }
  if (
    /\b(eslint|ruff|mypy|pyright|prettier|tsc)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?(lint|typecheck|type-check|format:check)\b/.test(
      text,
    )
  ) {
    return "代码检查";
  }
  if (
    /\b(npm|pnpm|yarn|bun)\s+(run\s+)?build\b|\b(vite|next|nuxt|astro)\s+build\b|\b(go|cargo|swift)\s+build\b|\bxcodebuild\b|\bmvn\s+package\b/.test(
      text,
    )
  ) {
    return "构建项目";
  }
  if (
    /\b(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|preview|start|serve)\b|\b(vite|next|nuxt|astro)\s+(dev|preview)\b|\bpython\s+-m\s+http\.server\b|\blocalhost:\d+\b/.test(
      text,
    )
  ) {
    return "启动预览";
  }
  if (invokesShellCommand(text, String.raw`git\s+(?:status|diff)`)) {
    return "检查代码变更";
  }
  if (invokesShellCommand(text, String.raw`rg\s+--files`)) {
    return "查看文件";
  }
  if (invokesShellCommand(text, "rg|grep|ag|ack")) {
    return "搜索内容";
  }
  if (
    invokesShellCommand(text, "cat|head|tail|less|more") ||
    invokesShellCommand(text, String.raw`sed\s+-[^\s]*n`)
  ) {
    return "读取文件";
  }
  if (invokesShellCommand(text, "ls|tree|find|fd")) {
    return "查看文件";
  }
  return "终端操作";
}

const shellRenderer: ToolRenderer = {
  getDisplayName({ args }) {
    const record = asPlainRecord(args);
    if (!record) return "终端操作";
    const command = getStringField(record, ["command", "cmd", "script"]);
    return command ? commandPhaseLabel(command) : "终端操作";
  },
  getSummary({ toolName, args, argsText, resultText }) {
    const record = asPlainRecord(args);
    if (!record) return "";
    const command = getStringField(record, ["command", "cmd", "script"]);
    const signal = buildVibeCodingSignal({
      toolName,
      args,
      argsText,
      resultText,
    });
    if (signal?.kind === "preview") {
      return `启动预览 · ${signal.label}`;
    }
    if (signal?.kind === "deployment") {
      return `发布部署 · ${signal.label}`;
    }
    return command
      ? `${commandPhaseLabel(command)} · ${truncateSummary(command, 120)}`
      : "";
  },
};

const fileRenderer: ToolRenderer = {
  getSummary({ toolName, args }) {
    const record = asPlainRecord(args);
    if (!record) return "";
    const lowerToolName = toolName.toLowerCase();
    const path = getStringField(record, ["file_path", "path", "filename"]);
    const action =
      lowerToolName.includes("write") || lowerToolName.includes("append")
        ? "写入文件"
        : lowerToolName.includes("edit") || lowerToolName.includes("patch")
          ? "编辑文件"
          : "读取文件";
    return path ? `${action} · ${readPathName(path)}` : action;
  },
};

const searchRenderer: ToolRenderer = {
  getSummary({ args }) {
    const record = asPlainRecord(args);
    if (!record) return "";
    const query = getStringField(record, ["query", "pattern", "glob"]);
    return query ? `检索代码 · ${truncateSummary(query, 80)}` : "检索代码";
  },
};

const todoRenderer: ToolRenderer = {
  getSummary({ args }) {
    const record = asPlainRecord(args);
    const todos = Array.isArray(record?.todos) ? record?.todos : [];
    if (!todos.length) return "更新任务清单";
    const done = todos.filter((item) => {
      return (
        !!item &&
        typeof item === "object" &&
        (item as Record<string, unknown>).status === "completed"
      );
    }).length;
    return `更新任务清单 · ${done}/${todos.length} 完成`;
  },
};

const webRenderer: ToolRenderer = {
  getSummary({ args }) {
    const record = asPlainRecord(args);
    if (!record) return "";
    const url = getStringField(record, ["url"]);
    if (url) return `读取网页 · ${truncateSummary(url, 90)}`;
    const query = getStringField(record, ["query"]);
    return query ? `搜索网页 · ${truncateSummary(query, 90)}` : "访问网页";
  },
};

const RENDERERS: Record<string, ToolRenderer> = {
  execute_shell_command: shellRenderer,
  shell: shellRenderer,
  bash: shellRenderer,
  Bash: shellRenderer,
  read_file: fileRenderer,
  write_file: fileRenderer,
  edit_file: fileRenderer,
  append_file: fileRenderer,
  read: fileRenderer,
  write: fileRenderer,
  edit: fileRenderer,
  multiedit: fileRenderer,
  Read: fileRenderer,
  Write: fileRenderer,
  Edit: fileRenderer,
  MultiEdit: fileRenderer,
  grep: searchRenderer,
  glob: searchRenderer,
  Grep: searchRenderer,
  Glob: searchRenderer,
  search: searchRenderer,
  TodoWrite: todoRenderer,
  todowrite: todoRenderer,
  todo_write: todoRenderer,
  web_fetch: webRenderer,
  web_search: webRenderer,
  WebFetch: webRenderer,
  WebSearch: webRenderer,
};

function stripMcpPrefix(name: string): string {
  if (!name.startsWith("mcp__")) return name;
  const parts = name.split("__");
  return parts.slice(2).join("__") || name;
}

export function getToolRenderer(toolName: string): ToolRenderer | null {
  if (RENDERERS[toolName]) return RENDERERS[toolName];
  const stripped = stripMcpPrefix(toolName);
  if (stripped !== toolName && RENDERERS[stripped]) return RENDERERS[stripped];
  const lower = stripped.toLowerCase();
  return RENDERERS[lower] ?? null;
}

export function buildRegisteredToolSummary(context: ToolRenderContext): string {
  const record = asPlainRecord(context.args);
  const renderer =
    getToolRenderer(context.toolName) ||
    (record && "command" in record ? shellRenderer : null);
  const rendered = renderer?.getSummary?.(context) || "";
  if (rendered) return rendered;
  return buildVibeCodingSignal(context)?.summary || "";
}

export function buildRegisteredToolDisplayName(
  context: ToolRenderContext,
): string {
  const record = asPlainRecord(context.args);
  const renderer =
    getToolRenderer(context.toolName) ||
    (record && "command" in record ? shellRenderer : null);
  return renderer?.getDisplayName?.(context) || "";
}
