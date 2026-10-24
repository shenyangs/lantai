export const MARKDOWN_HANDOFF_STORAGE_KEY = "lantai:markdown-chat-handoff:v2";
export const MARKDOWN_HANDOFF_EVENT = "lantai:markdown-chat-handoff";

export type MarkdownHandoffMode = "ask" | "suggest" | "execute";

export interface MarkdownHandoffEnvelope {
  protocol_version: "2.0";
  mode: MarkdownHandoffMode;
  path: string;
  document_name: string;
  text: string;
  selection_only: boolean;
}

const MODE_INSTRUCTIONS: Record<MarkdownHandoffMode, string> = {
  ask: "请阅读下面的 Markdown 内容并回答我的问题。此轮为询问模式：只读，不修改文件，也不要把建议描述成已执行。",
  suggest:
    "请审阅下面的 Markdown 内容并给出可逐条确认的修改建议。此轮为建议模式：保留原文，不直接写入文件；每条建议说明目标、影响范围和建议文本。",
  execute:
    "请根据我接下来补充的要求修改这个 Markdown 文件。此轮为执行模式：先说明计划和影响范围，取得明确授权后再执行；写入时使用快照、校验、原子替换和失败回滚，并在完成后给出可核验的文件路径与变更摘要。",
};

export function isMarkdownHandoffEnvelope(
  value: unknown,
): value is MarkdownHandoffEnvelope {
  if (!value || typeof value !== "object") return false;
  const envelope = value as Partial<MarkdownHandoffEnvelope>;
  return (
    envelope.protocol_version === "2.0" &&
    ["ask", "suggest", "execute"].includes(String(envelope.mode)) &&
    typeof envelope.path === "string" &&
    typeof envelope.document_name === "string" &&
    typeof envelope.text === "string" &&
    envelope.text.trim().length > 0 &&
    typeof envelope.selection_only === "boolean"
  );
}

export function buildMarkdownHandoffPrompt(
  envelope: MarkdownHandoffEnvelope,
): string {
  const scope = envelope.selection_only ? "当前选区" : "完整文档快照";
  const safeName = envelope.document_name.trim() || "未命名 Markdown";
  return [
    MODE_INSTRUCTIONS[envelope.mode],
    "",
    `文档：${safeName}`,
    `范围：${scope}`,
    `本地路径：${envelope.path}`,
    "",
    "<markdown_snapshot>",
    envelope.text.trim(),
    "</markdown_snapshot>",
  ].join("\n");
}

export function storeMarkdownHandoff(envelope: MarkdownHandoffEnvelope): void {
  window.sessionStorage.setItem(
    MARKDOWN_HANDOFF_STORAGE_KEY,
    JSON.stringify(envelope),
  );
}

export function consumeMarkdownHandoff(): MarkdownHandoffEnvelope | null {
  const serialized = window.sessionStorage.getItem(
    MARKDOWN_HANDOFF_STORAGE_KEY,
  );
  if (!serialized) return null;
  window.sessionStorage.removeItem(MARKDOWN_HANDOFF_STORAGE_KEY);
  try {
    const parsed: unknown = JSON.parse(serialized);
    return isMarkdownHandoffEnvelope(parsed) ? parsed : null;
  } catch {
    return null;
  }
}
