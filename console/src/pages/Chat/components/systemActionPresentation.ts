import type { ILantaiRuntimeMessage } from "@/vendor/lantaiChatTypes";

export type SystemActionStatus =
  | "running"
  | "completed"
  | "degraded"
  | "failed"
  | "empty"
  | "unavailable";

export interface SystemActionStep {
  id: string;
  title: string;
  detail?: string;
  status: "pending" | "running" | "completed" | "failed";
}

export interface SystemActionPresentation {
  kind: string;
  status: SystemActionStatus;
  title: string;
  detail?: string;
  summary?: string;
  messageCount?: number;
  beforeTokens?: number;
  afterTokens?: number;
  savedPercent?: number;
  technicalReason?: string;
  steps?: SystemActionStep[];
  showAvatar?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function nestedMetadata(
  message: ILantaiRuntimeMessage,
): Record<string, unknown>[] {
  const direct = isRecord(message.metadata) ? message.metadata : {};
  const nested = isRecord(direct.metadata) ? direct.metadata : {};
  return [direct, nested];
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : undefined;
}

function messageText(message: ILantaiRuntimeMessage): string {
  if (!Array.isArray(message.content)) return "";
  return message.content
    .map((item) => {
      if (!isRecord(item)) return "";
      return typeof item.text === "string" ? item.text : "";
    })
    .filter(Boolean)
    .join("\n");
}

function actionFromMetadata(
  message: ILantaiRuntimeMessage,
): SystemActionPresentation | null {
  for (const metadata of nestedMetadata(message)) {
    if (!isRecord(metadata.system_action)) continue;
    const action = metadata.system_action;
    const kind = stringValue(action.kind);
    const title = stringValue(action.title);
    if (!kind || !title) continue;
    const rawStatus = stringValue(action.status);
    const status: SystemActionStatus =
      rawStatus === "running" ||
      rawStatus === "degraded" ||
      rawStatus === "failed" ||
      rawStatus === "empty" ||
      rawStatus === "unavailable"
        ? rawStatus
        : "completed";
    const steps = Array.isArray(action.steps)
      ? action.steps.filter(isRecord).map((step, index) => {
          const rawStepStatus = stringValue(step.status);
          const stepStatus: SystemActionStep["status"] =
            rawStepStatus === "running" ||
            rawStepStatus === "completed" ||
            rawStepStatus === "failed"
              ? rawStepStatus
              : "pending";
          return {
            id: stringValue(step.id) || `step-${index}`,
            title: stringValue(step.title) || "处理中",
            detail: stringValue(step.detail),
            status: stepStatus,
          };
        })
      : undefined;
    return {
      kind,
      status,
      title,
      detail: stringValue(action.detail),
      summary: stringValue(action.summary),
      messageCount: numberValue(action.message_count),
      beforeTokens: numberValue(action.before_tokens),
      afterTokens: numberValue(action.after_tokens),
      savedPercent: numberValue(action.saved_percent),
      technicalReason: stringValue(action.technical_reason),
      steps,
      showAvatar: action.show_avatar === true,
    };
  }
  return null;
}

function parseLegacyToken(value?: string): number | undefined {
  if (!value) return undefined;
  const normalized = value.trim().toLowerCase();
  const parsed = Number.parseFloat(normalized);
  if (!Number.isFinite(parsed)) return undefined;
  return normalized.endsWith("k") ? Math.round(parsed * 1000) : parsed;
}

function legacyActionFromText(text: string): SystemActionPresentation | null {
  if (!text.trim()) return null;

  if (/Compact Complete!/i.test(text)) {
    const messageCount = Number(
      text.match(/Messages compacted:\s*(\d+)/i)?.[1] || "",
    );
    const tokenMatch = text.match(
      /Tokens:\s*([\d.]+k?)[^\n]*?->\s*([\d.]+k?)/i,
    );
    const summary = text
      .split(/\*\*Compressed Summary:\*\*/i)[1]
      ?.replace(/-\s*Summary task started in background[\s\S]*$/i, "")
      .trim();
    return {
      kind: "context_compaction",
      status: "completed",
      title: "对话已压缩",
      detail: "后续对话会继续使用这份上下文摘要。",
      summary,
      messageCount: Number.isFinite(messageCount) ? messageCount : undefined,
      beforeTokens: parseLegacyToken(tokenMatch?.[1]),
      afterTokens: parseLegacyToken(tokenMatch?.[2]),
    };
  }
  if (/Compact Failed!/i.test(text)) {
    return {
      kind: "context_compaction",
      status: "failed",
      title: "压缩未完成",
      detail: "可以重试；若上下文已超出上限，可新建或清空对话。",
    };
  }
  if (/No messages to compact/i.test(text)) {
    return {
      kind: "context_compaction",
      status: "empty",
      title: "无需压缩",
      detail: "当前对话上下文为空。",
    };
  }
  if (/New Conversation Started!/i.test(text)) {
    return {
      kind: "new_conversation",
      status: "completed",
      title: "已开始新对话",
      detail: "上一段对话正在后台归纳，任务计划已重置。",
    };
  }
  if (/History Cleared!/i.test(text)) {
    return {
      kind: "history_clear",
      status: "completed",
      title: "对话记录已清空",
      detail: "上下文摘要和任务计划也已重置。",
    };
  }
  if (/^\*\*Compressed Summary\*\*/i.test(text)) {
    return {
      kind: "compressed_summary",
      status: "completed",
      title: "当前上下文摘要",
      summary: text.replace(/^\*\*Compressed Summary\*\*\s*/i, "").trim(),
    };
  }
  if (/No Compressed Summary/i.test(text)) {
    return {
      kind: "compressed_summary",
      status: "empty",
      title: "暂无上下文摘要",
      detail: "可以先压缩对话，或等待自动压缩。",
    };
  }
  return null;
}

export function isSystemCommandMessage(
  message: ILantaiRuntimeMessage,
): boolean {
  return nestedMetadata(message).some(
    (metadata) =>
      typeof metadata.system_command === "string" ||
      isRecord(metadata.system_action),
  );
}

export function getSystemActionPresentation(
  message: ILantaiRuntimeMessage,
): SystemActionPresentation | null {
  return (
    actionFromMetadata(message) || legacyActionFromText(messageText(message))
  );
}
