import type {
  ChatRuntimeState,
  ChatTurnEventPreview,
  ChatTurnEventPreviewEvent,
} from "../../api/types/chat";

export type RuntimeStallLevel = "idle" | "healthy" | "stale" | "stalled";

export interface RuntimeStallState {
  level: RuntimeStallLevel;
  isRunning: boolean;
  waitingForFirstOutput: boolean;
  hasSubstantiveProgress: boolean;
  transportStatus: string;
  transportAlive: boolean;
  lastEvent: ChatTurnEventPreviewEvent | null;
  lastActivityAt: string | null;
  elapsedMs: number | null;
  elapsedLabel: string;
  stageLabel: string;
  summary: string;
}

export interface RuntimeStallThresholds {
  staleMs: number;
  stalledMs: number;
}

const DEFAULT_THRESHOLDS: RuntimeStallThresholds = {
  staleMs: 45_000,
  stalledMs: 300_000,
};

function eventTimeMs(event: ChatTurnEventPreviewEvent): number {
  const value = Date.parse(event.ts || "");
  return Number.isFinite(value) ? value : 0;
}

function isTransportKeepalive(event: ChatTurnEventPreviewEvent): boolean {
  const kind = String(event.kind || "").toLowerCase();
  const type = String(event.type || "").toLowerCase();
  const payload = event.payload || {};
  return (
    payload.structured_keepalive === true ||
    type === "runtime.progress" ||
    kind === "runtime.progress" ||
    kind.startsWith("heartbeat.") ||
    type.startsWith("heartbeat.")
  );
}

function isNonProgressDiagnostic(event: ChatTurnEventPreviewEvent): boolean {
  const kind = String(event.kind || "").toLowerCase();
  if (kind === "chat.model.routing_decision") return true;
  if (kind !== "chat.system.event") return false;
  const payload = event.payload || {};
  const detail = [
    event.label,
    event.detail,
    event.trace_title,
    event.trace_detail,
    payload.kind,
    payload.summary,
  ]
    .map((value) => String(value || ""))
    .join(" ")
    .toLowerCase();
  return /model usage|token usage|用量|routing|路由/.test(detail);
}

function isSubstantiveProgressEvent(event: ChatTurnEventPreviewEvent): boolean {
  const kind = String(event.kind || "").toLowerCase();
  const type = String(event.type || "").toLowerCase();
  return [kind, type].some(
    (value) =>
      value.startsWith("chat.message.") ||
      value.startsWith("chat.tool_call.") ||
      value.startsWith("message.") ||
      value.startsWith("tool.call.") ||
      value.startsWith("tool."),
  );
}

function isTurnStartedEvent(event: ChatTurnEventPreviewEvent): boolean {
  const kind = String(event.kind || "").toLowerCase();
  const type = String(event.type || "").toLowerCase();
  return (
    kind === "chat.turn.started" ||
    kind === "turn.started" ||
    type === "turn.started"
  );
}

function latestTransportRuntimeEvent(
  runtimeState?: ChatRuntimeState | null,
  preview?: ChatTurnEventPreview | null,
): ChatTurnEventPreviewEvent | null {
  const events = [
    ...(preview?.events || []),
    ...(runtimeState?.events_preview || []),
  ].filter(isTransportKeepalive);
  if (!events.length) return null;
  return events.reduce((latest, event) =>
    eventTimeMs(event) >= eventTimeMs(latest) ? event : latest,
  );
}

function latestSubstantiveRuntimeEvent(
  runtimeState?: ChatRuntimeState | null,
  preview?: ChatTurnEventPreview | null,
): ChatTurnEventPreviewEvent | null {
  const events = [
    ...(preview?.events || []),
    ...(runtimeState?.events_preview || []),
  ].filter(
    (event) => !isTransportKeepalive(event) && !isNonProgressDiagnostic(event),
  );
  if (!events.length) return null;
  return events.reduce((latest, event) => {
    const latestSeq = latest.seq ?? 0;
    const eventSeq = event.seq ?? 0;
    if (eventSeq !== latestSeq) return eventSeq > latestSeq ? event : latest;
    return eventTimeMs(event) >= eventTimeMs(latest) ? event : latest;
  });
}

export function latestRuntimePreviewEvent(
  runtimeState?: ChatRuntimeState | null,
  preview?: ChatTurnEventPreview | null,
): ChatTurnEventPreviewEvent | null {
  const events = [
    ...(preview?.events || []),
    ...(runtimeState?.events_preview || []),
  ];
  if (!events.length) return null;
  return events.reduce((latest, event) => {
    const latestSeq = latest.seq ?? 0;
    const eventSeq = event.seq ?? 0;
    if (eventSeq !== latestSeq) return eventSeq > latestSeq ? event : latest;
    return eventTimeMs(event) >= eventTimeMs(latest) ? event : latest;
  });
}

function formatElapsed(ms: number | null): string {
  if (ms === null || ms < 0) return "";
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const minutesRest = minutes % 60;
  return minutesRest ? `${hours}h ${minutesRest}m` : `${hours}h`;
}

function stringField(
  source: Record<string, unknown>,
  ...keys: string[]
): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

export function runtimeStageLabel(
  event?: ChatTurnEventPreviewEvent | null,
): string {
  if (!event) return "未知阶段";
  const payload = event.payload || {};
  return (
    stringField(payload, "trace_title") ||
    event.trace_title ||
    event.label ||
    event.kind ||
    event.type ||
    "运行事件"
  );
}

export function getRuntimeStallState(
  runtimeState?: ChatRuntimeState | null,
  preview?: ChatTurnEventPreview | null,
  nowMs = Date.now(),
  thresholds: RuntimeStallThresholds = DEFAULT_THRESHOLDS,
): RuntimeStallState {
  const runtimeStatus = String(runtimeState?.status || "").toLowerCase();
  const transportStatus = String(
    runtimeState?.transport_status || "",
  ).toLowerCase();
  const isRunning = runtimeStatus === "running";
  const isBackendStalled = runtimeStatus === "stalled";
  const latestTransportEvent = latestTransportRuntimeEvent(
    runtimeState,
    preview,
  );
  const lastEvent = latestSubstantiveRuntimeEvent(runtimeState, preview);
  const hasSubstantiveProgress = [
    ...(preview?.events || []),
    ...(runtimeState?.events_preview || []),
  ].some(isSubstantiveProgressEvent);
  const waitingForFirstOutput = Boolean(
    isRunning &&
    !hasSubstantiveProgress &&
    (!lastEvent || isTurnStartedEvent(lastEvent)),
  );
  const lastEventAt = lastEvent ? eventTimeMs(lastEvent) : 0;
  const startedAtMs = runtimeState?.started_at
    ? Date.parse(runtimeState.started_at)
    : 0;
  const updatedAtMs = runtimeState?.updated_at
    ? Date.parse(runtimeState.updated_at)
    : 0;
  const lastActivityMs = lastEventAt || startedAtMs || updatedAtMs || 0;
  const elapsedMs = lastActivityMs ? Math.max(0, nowMs - lastActivityMs) : null;
  const elapsedLabel = formatElapsed(elapsedMs);
  const stageLabel = runtimeStageLabel(lastEvent);
  const explicitTransportAlive = ["connecting", "connected"].includes(
    transportStatus,
  );
  const explicitTransportInterrupted = ["reconnecting", "closed"].includes(
    transportStatus,
  );
  const transportStateIsRecent = Boolean(
    updatedAtMs && nowMs - updatedAtMs < thresholds.staleMs,
  );

  if (!isRunning) {
    if (isBackendStalled) {
      return {
        level: "stalled",
        isRunning,
        waitingForFirstOutput,
        hasSubstantiveProgress,
        transportStatus,
        transportAlive: false,
        lastEvent,
        lastActivityAt: lastActivityMs
          ? new Date(lastActivityMs).toISOString()
          : null,
        elapsedMs,
        elapsedLabel,
        stageLabel,
        summary: runtimeState?.summary || "已标记卡住",
      };
    }
    return {
      level: "idle",
      isRunning,
      waitingForFirstOutput,
      hasSubstantiveProgress,
      transportStatus,
      transportAlive: false,
      lastEvent,
      lastActivityAt: lastActivityMs
        ? new Date(lastActivityMs).toISOString()
        : null,
      elapsedMs,
      elapsedLabel,
      stageLabel,
      summary: "当前没有运行中的任务",
    };
  }

  let level: RuntimeStallLevel = "healthy";
  if (elapsedMs === null) {
    level = "stale";
  } else if (elapsedMs >= thresholds.stalledMs) {
    level = "stalled";
  } else if (elapsedMs >= thresholds.staleMs) {
    level = "stale";
  }

  const transportAlive = Boolean(
    !explicitTransportInterrupted &&
    ((explicitTransportAlive && transportStateIsRecent) ||
      (latestTransportEvent &&
        nowMs - eventTimeMs(latestTransportEvent) < thresholds.staleMs)),
  );
  const summary =
    level === "stalled"
      ? explicitTransportInterrupted
        ? `连接已中断 · ${elapsedLabel || "无事件"}`
        : transportAlive
          ? `连接正常 · ${elapsedLabel || "无事件"} 无业务进展`
          : `长时间没有新进展 · ${elapsedLabel || "无事件"}`
      : level === "stale"
        ? transportAlive
          ? waitingForFirstOutput
            ? `连接正常 · ${elapsedLabel || "尚未"} 未收到模型输出`
            : `连接正常 · ${elapsedLabel || "尚未"} 无业务进展`
          : explicitTransportInterrupted
            ? `正在重新连接 · ${elapsedLabel || "无事件"}`
            : `仍在处理 · ${elapsedLabel || "无事件"}`
        : transportAlive
          ? elapsedLabel
            ? `连接正常 · 最近业务进展 ${elapsedLabel}`
            : "连接正常 · 等待业务进展"
          : elapsedLabel
            ? `处理中 · ${elapsedLabel}`
            : "等待运行事件";

  return {
    level,
    isRunning,
    waitingForFirstOutput,
    hasSubstantiveProgress,
    transportStatus,
    transportAlive,
    lastEvent,
    lastActivityAt: lastActivityMs
      ? new Date(lastActivityMs).toISOString()
      : null,
    elapsedMs,
    elapsedLabel,
    stageLabel,
    summary,
  };
}

export function buildRuntimeContinuationPrompt(options: {
  stall: RuntimeStallState;
  runId?: string | null;
  workspaceName?: string;
  workspaceDir?: string | null;
}): string {
  const { stall, runId, workspaceName, workspaceDir } = options;
  const event = stall.lastEvent;
  const payload = event?.payload || {};
  const detail =
    event?.trace_detail ||
    stringField(payload, "trace_detail", "detail", "summary") ||
    event?.detail ||
    "";
  const lines = [
    "上一轮任务似乎中途停住了。请从当前状态继续，不要重复已经完成的步骤。",
    "",
    "请先做三件事：",
    "1. 检查当前工作区和运行状态，确认哪些步骤已经完成。",
    "2. 从最后停住的阶段继续执行；如果发现上一步其实失败了，先修复失败原因。",
    "3. 根据用户原始意图继续：普通问答直接回答；只有确实需要文件或制品时才继续制作。",
    "",
    "已知上下文：",
    runId ? `- run_id: ${runId}` : "",
    workspaceName ? `- 工作区: ${workspaceName}` : "",
    workspaceDir && workspaceDir !== workspaceName
      ? `- 工作区路径: ${workspaceDir}`
      : "",
    `- 停住阶段: ${stall.stageLabel}`,
    stall.elapsedLabel ? `- 距离最后更新: ${stall.elapsedLabel}` : "",
    event?.status ? `- 最后事件状态: ${event.status}` : "",
    event?.kind ? `- 最后事件类型: ${event.kind}` : "",
    detail ? `- 最后事件详情: ${detail}` : "",
  ].filter(Boolean);
  return lines.join("\n");
}

export function buildIncompleteResponseContinuationPrompt(options: {
  runId?: string | null;
  workspaceName?: string;
  workspaceDir?: string | null;
}): string {
  const { runId, workspaceName, workspaceDir } = options;
  return [
    "上一轮运行已结束，但可能没有形成完整回答。请先根据原始消息判断用户真正想要什么。",
    "",
    "处理原则：",
    "1. 先复用上一轮已有信息，不要重复已经完成的步骤。",
    "2. 如果用户只是在聊天或提问，直接给出自然回答，不要制造文件或制品。",
    "3. 只有原始请求明确需要执行或产物时，才补齐尚未完成的部分。",
    "",
    "已知上下文：",
    runId ? `- run_id: ${runId}` : "",
    workspaceName ? `- 工作区: ${workspaceName}` : "",
    workspaceDir && workspaceDir !== workspaceName
      ? `- 工作区路径: ${workspaceDir}`
      : "",
  ]
    .filter(Boolean)
    .join("\n");
}
