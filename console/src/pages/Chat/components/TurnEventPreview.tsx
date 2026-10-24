import {
  useChatAnywhereSessionsState,
  type ILantaiChatMessage,
  type ILantaiChatSession,
} from "@/vendor/lantaiChat";
import { Button, Drawer, Tooltip } from "antd";
import {
  AlertCircle,
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronRight,
  FilePenLine,
  FlaskConical,
  GitBranch,
  Loader2,
  MonitorPlay,
  PackageCheck,
  Rocket,
  ScanLine,
  Search,
  ShieldCheck,
  Square,
  Terminal,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";
import type {
  ChatRuntimeState,
  ChatTurnEventPreview,
  RuntimeFailureDiagnosis,
} from "../../../api/types/chat";
import { runtimeApi } from "../../../api/modules/runtime";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  buildRuntimeRepairPrompt,
  previewHarnessReportFromPayload,
  runtimeFailureDiagnosisFromPayload,
} from "../../../utils/runtimeFailureDiagnosis";
import sessionApi from "../sessionApi";
import { setTextareaValue } from "../utils";
import { extractUrls, isLocalPreviewUrl } from "../vibeCodingSignals";
import {
  buildIncompleteResponseContinuationPrompt,
  buildRuntimeContinuationPrompt,
  getRuntimeStallState,
  type RuntimeStallState,
} from "../runtimeStall";
import styles from "./TurnEventPreview.module.less";

interface ExtendedSession extends ILantaiChatSession {
  sessionId?: string;
  turnEventPreview?: ChatTurnEventPreview | null;
  runtimeState?: ChatRuntimeState | null;
}

interface TurnEventPreviewProps {
  variant?: "header" | "inline" | "presence" | "strip";
}

type PresenceTone = "active" | "quiet" | "warning" | "danger";

export interface PresencePresentation {
  stage: string;
  text: string;
  meta: string;
  tone: PresenceTone;
  authored: boolean;
}

interface AuthoredPresence {
  id: string;
  revision: number;
  runId: string;
  clientTurnId: string;
  toolCallIds: string[];
  text: string;
}

interface StableAuthoredPresence extends AuthoredPresence {
  runKey: string;
}

interface StableStructuralPresence {
  runKey: string;
  stage: string;
}

const RESPONSE_CARD_CODE = "LantaiRuntimeResponseCard";
const PUBLIC_PROGRESS_RESPONSE_STATUSES = new Set([
  "created",
  "in_progress",
  "running",
  "completed",
  "finished",
]);

function singleLineExcerpt(value: unknown, limit = 132): string {
  const text = String(value || "")
    .replace(/\[\u200B\]\(#lantai-stream-cursor\)/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  const characters = Array.from(text);
  return characters.length > limit
    ? `${characters.slice(0, limit).join("")}…`
    : text;
}

function publicProgressText(value: unknown): string {
  return singleLineExcerpt(value, Number.POSITIVE_INFINITY);
}

function outputText(output: Record<string, unknown>): string {
  if (typeof output.content === "string") {
    return publicProgressText(output.content);
  }
  if (!Array.isArray(output.content)) return "";
  return publicProgressText(
    output.content
      .map((block) => {
        if (!block || typeof block !== "object") return "";
        const record = block as Record<string, unknown>;
        return String(record.text || record.thinking || "");
      })
      .join(""),
  );
}

function objectField(value: unknown): Record<string, unknown> {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function stringArrayField(
  source: Record<string, unknown>,
  key: string,
): string[] {
  const value = source[key];
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const text = typeof item === "string" ? item.trim() : "";
    return text ? [text] : [];
  });
}

function isCanonicalPublicProgressMetadata(
  value: Record<string, unknown>,
): boolean {
  const revision = value.public_progress_revision;
  return Boolean(
    value.public_projection === "runtime_progress_v2" &&
    typeof value.public_progress_id === "string" &&
    value.public_progress_id.trim() &&
    value.source === "runtime_facts" &&
    value.private_reasoning_exposed === false &&
    value.text_origin === "assistant_public_text" &&
    typeof revision === "number" &&
    Number.isInteger(revision) &&
    revision > 0,
  );
}

function canonicalMetadataFromNestedPublicProgress(
  outputMetadata: Record<string, unknown>,
): Record<string, unknown> | null {
  const progress = objectField(outputMetadata.public_progress);
  const schemaVersion = progress.schema_version;
  const id =
    typeof progress.id === "string" ? singleLineExcerpt(progress.id, 160) : "";
  const revision = progress.revision;
  if (
    schemaVersion !== 2 ||
    !id ||
    progress.source !== "runtime_facts" ||
    progress.text_origin !== "assistant_public_text" ||
    typeof revision !== "number" ||
    !Number.isInteger(revision) ||
    revision <= 0
  ) {
    return null;
  }
  return {
    ...outputMetadata,
    public_projection: "runtime_progress_v2",
    public_progress_id: id,
    public_progress_revision: revision,
    source: "runtime_facts",
    private_reasoning_exposed: false,
    text_origin: "assistant_public_text",
    ...(Array.isArray(progress.tool_call_ids)
      ? { tool_call_ids: progress.tool_call_ids }
      : {}),
  };
}

function publicProgressProjection(
  output: Record<string, unknown>,
): { metadata: Record<string, unknown>; text: string } | null {
  const markedBlocks = Array.isArray(output.content)
    ? output.content.flatMap((block) => {
        const record = objectField(block);
        const metadata = objectField(record.metadata);
        if (!isCanonicalPublicProgressMetadata(metadata)) return [];
        const text = publicProgressText(record.text || record.thinking);
        return text ? [{ metadata, text }] : [];
      })
    : [];
  if (markedBlocks.length > 0) {
    const latestBlock = markedBlocks[markedBlocks.length - 1];
    const metadata = latestBlock.metadata;
    const progressId = String(metadata.public_progress_id || "");
    const revision = Number(metadata.public_progress_revision ?? 0);
    const matchingBlocks = markedBlocks.filter((block) => {
      const blockRevision = Number(
        block.metadata.public_progress_revision ?? 0,
      );
      return (
        String(block.metadata.public_progress_id || "") === progressId &&
        blockRevision === revision
      );
    });
    return {
      metadata,
      text: publicProgressText(
        matchingBlocks.map((block) => block.text).join(""),
      ),
    };
  }

  const outputMetadata = objectField(output.metadata);
  const metadata = isCanonicalPublicProgressMetadata(outputMetadata)
    ? outputMetadata
    : canonicalMetadataFromNestedPublicProgress(outputMetadata);
  if (metadata) {
    if (Array.isArray(output.content)) {
      const textualBlocks = output.content.filter((block) => {
        const record = objectField(block);
        return (
          typeof record.text === "string" || typeof record.thinking === "string"
        );
      });
      if (textualBlocks.length !== 1) return null;
    }
    const text = outputText(output);
    if (text) {
      return { metadata, text };
    }
  }
  return null;
}

function presenceIdentity(
  response: Record<string, unknown>,
  output: Record<string, unknown>,
  progressMetadata: Record<string, unknown>,
): { runId: string; clientTurnId: string } {
  const responseMetadata = objectField(response.metadata);
  const outputMetadata = objectField(output.metadata);
  const sources = [progressMetadata, outputMetadata, responseMetadata];
  const read = (...keys: string[]) => {
    for (const source of sources) {
      for (const key of keys) {
        const value = source[key];
        if (typeof value === "string" && value.trim()) return value.trim();
      }
    }
    return "";
  };
  return {
    runId: read(
      "run_id",
      "native_runtime_turn_id",
      "runtime_turn_id",
      "turn_id",
    ),
    clientTurnId: read("client_turn_id"),
  };
}

function messageClientTurnId(message: ILantaiChatMessage): string {
  const record = objectField(message);
  const metadata = objectField(record.metadata);
  const direct = singleLineExcerpt(metadata.client_turn_id, 160);
  if (direct) return direct;

  const cards = Array.isArray(record.cards) ? record.cards : [];
  for (const card of cards) {
    const data = objectField(objectField(card).data);
    const dataMetadata = objectField(data.metadata);
    const dataIdentity = singleLineExcerpt(
      data.client_turn_id || dataMetadata.client_turn_id,
      160,
    );
    if (dataIdentity) return dataIdentity;
    const inputs = Array.isArray(data.input) ? data.input : [];
    for (const input of inputs) {
      const inputMetadata = objectField(objectField(input).metadata);
      const inputIdentity = singleLineExcerpt(
        inputMetadata.client_turn_id,
        160,
      );
      if (inputIdentity) return inputIdentity;
    }
    const outputs = Array.isArray(data.output) ? data.output : [];
    for (const output of outputs) {
      const outputRecord = objectField(output);
      const outputMetadata = objectField(outputRecord.metadata);
      const outputIdentity = singleLineExcerpt(
        outputRecord.client_turn_id || outputMetadata.client_turn_id,
        160,
      );
      if (outputIdentity) return outputIdentity;
    }
  }
  // A message/card id is a rendering identity, not necessarily the causal
  // client-turn identity generated at submit time. Guessing here can filter
  // every legitimate runtime event out of the presence rail. When history
  // does not carry an explicit client_turn_id, let the preview/runtime event
  // provide the canonical identity instead.
  return "";
}

function normalizedTerminalAssistantStatus(status: unknown): string {
  const normalized = String(status || "")
    .trim()
    .toLowerCase();
  if (["completed", "finished"].includes(normalized)) return "completed";
  if (["stopped", "cancelled", "canceled"].includes(normalized)) {
    return "interrupted";
  }
  if (
    ["failed", "interrupted", "incomplete", "blocked", "needs_input"].includes(
      normalized,
    )
  ) {
    return normalized;
  }
  return "";
}

function latestUserTerminalAssistantStatus(
  messages?: ILantaiChatSession["messages"],
): string {
  const clientTurnId = latestUserClientTurnId(messages);
  if (!clientTurnId) return "";
  for (let index = (messages?.length || 0) - 1; index >= 0; index -= 1) {
    const message = messages![index];
    if (
      String(message.role || "").toLowerCase() !== "assistant" ||
      messageClientTurnId(message) !== clientTurnId
    ) {
      continue;
    }
    const messageStatus = normalizedTerminalAssistantStatus(message.msgStatus);
    if (messageStatus) return messageStatus;
    const cards = Array.isArray(message.cards)
      ? [...message.cards].reverse()
      : [];
    for (const card of cards) {
      if (card.code !== RESPONSE_CARD_CODE) continue;
      const response = objectField(card.data);
      const responseStatus = normalizedTerminalAssistantStatus(response.status);
      if (responseStatus) return responseStatus;
    }
  }
  return "";
}

function latestUserClientTurnId(
  messages?: ILantaiChatSession["messages"],
): string {
  for (let index = (messages?.length || 0) - 1; index >= 0; index -= 1) {
    const message = messages![index];
    if (!isAcceptedUserTurnMessage(message)) continue;
    return messageClientTurnId(message);
  }
  return "";
}

function runtimeQueueStatus(message: ILantaiChatMessage): string {
  for (const card of message.cards || []) {
    const data = objectField(card.data);
    const status = String(data.runtime_queue_status || "").toLowerCase();
    if (["sending", "steered", "queued", "failed"].includes(status)) {
      return status;
    }
  }
  return "";
}

function isAcceptedUserTurnMessage(message: ILantaiChatMessage): boolean {
  return (
    String(message.role || "").toLowerCase() === "user" &&
    !runtimeQueueStatus(message)
  );
}

function previousUserClientTurnId(
  messages?: ILantaiChatSession["messages"],
): string {
  let skippedLatestUser = false;
  for (let index = (messages?.length || 0) - 1; index >= 0; index -= 1) {
    const message = messages![index];
    if (!isAcceptedUserTurnMessage(message)) continue;
    if (!skippedLatestUser) {
      skippedLatestUser = true;
      continue;
    }
    const clientTurnId = messageClientTurnId(message);
    if (clientTurnId) return clientTurnId;
  }
  return "";
}

function latestUserMessageTimeMs(
  messages?: ILantaiChatSession["messages"],
): number {
  for (let index = (messages?.length || 0) - 1; index >= 0; index -= 1) {
    const message = messages![index];
    if (!isAcceptedUserTurnMessage(message)) continue;
    const record = objectField(message);
    const value =
      record.createdAt ?? record.created_at ?? record.timestamp ?? null;
    if (typeof value === "number") {
      const normalized = value < 10_000_000_000 ? value * 1000 : value;
      return Number.isFinite(normalized) ? normalized : 0;
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    return 0;
  }
  return 0;
}

function latestAuthoredPresence(
  messages?: ILantaiChatSession["messages"],
  currentRunId = "",
  currentClientTurnId = "",
  includePast = false,
): AuthoredPresence | null {
  const rows = messages || [];
  const latestUserIndex = rows.reduce(
    (latest, message, index) =>
      isAcceptedUserTurnMessage(message) ? index : latest,
    -1,
  );
  for (
    let messageIndex = rows.length - 1;
    messageIndex > latestUserIndex;
    messageIndex -= 1
  ) {
    const message = rows[messageIndex];
    if (String(message.role || "").toLowerCase() !== "assistant") continue;
    const cards = Array.isArray(message.cards)
      ? [...message.cards].reverse()
      : [];
    for (const card of cards) {
      if (card.code !== RESPONSE_CARD_CODE || !card.data) continue;
      const response = card.data as Record<string, unknown>;
      if (!PUBLIC_PROGRESS_RESPONSE_STATUSES.has(String(response.status || "")))
        continue;
      const outputs = Array.isArray(response.output)
        ? [...response.output].reverse()
        : [];
      for (const candidate of outputs) {
        if (!candidate || typeof candidate !== "object") continue;
        const output = candidate as Record<string, unknown>;
        const type = String(output.type || "").toLowerCase();
        // A later answer is a real phase boundary. Do not keep narrating
        // the preparation that preceded it while the answer is streaming.
        if (!includePast && type === "message" && outputText(output)) {
          const identity = presenceIdentity(
            response,
            output,
            objectField(output.metadata),
          );
          if (
            (!currentRunId || identity.runId === currentRunId) &&
            (!currentClientTurnId ||
              identity.clientTurnId === currentClientTurnId)
          )
            return null;
        }
        if (!["reasoning", "thinking"].includes(type)) continue;
        const projection = publicProgressProjection(output);
        if (!projection) continue;
        const identity = presenceIdentity(
          response,
          output,
          projection.metadata,
        );
        if (currentRunId && identity.runId !== currentRunId) continue;
        if (
          currentClientTurnId &&
          identity.clientTurnId !== currentClientTurnId
        ) {
          continue;
        }
        const { metadata, text } = projection;
        const rawRevision = Number(metadata.public_progress_revision ?? 0);
        return {
          id: String(metadata.public_progress_id || output.id || message.id),
          revision: Number.isFinite(rawRevision) ? rawRevision : 0,
          runId: identity.runId,
          clientTurnId: identity.clientTurnId,
          toolCallIds: stringArrayField(metadata, "tool_call_ids"),
          text,
        };
      }
    }
  }
  return null;
}

/** Full public statements remain inspectable even after their stage finishes. */
export function publicProgressHistory(
  messages: ILantaiChatSession["messages"] = [],
  runId = "",
  clientTurnId = "",
): AuthoredPresence[] {
  const latestUser = messages.reduce(
    (last, row, index) => (isAcceptedUserTurnMessage(row) ? index : last),
    -1,
  );
  const history = new Map<string, AuthoredPresence>();
  for (const message of messages.slice(latestUser + 1)) {
    for (const card of message.cards || []) {
      if (card.code !== RESPONSE_CARD_CODE) continue;
      const response = objectField(card.data);
      for (const output of Array.isArray(response.output)
        ? response.output
        : []) {
        const item = latestAuthoredPresence(
          [
            {
              ...message,
              cards: [{ ...card, data: { ...response, output: [output] } }],
            },
          ] as ILantaiChatSession["messages"],
          runId,
          clientTurnId,
        );
        if (!item) continue;
        const old = history.get(item.id);
        if (
          !old ||
          item.revision > old.revision ||
          (item.revision === old.revision && item.text.startsWith(old.text))
        ) {
          history.set(item.id, item);
        }
      }
    }
  }
  return [...history.values()];
}

/**
 * Reuse only an explicitly marked public-progress statement from the current
 * assistant turn. Ordinary answers, user text and private provider reasoning
 * are never treated as Shen Xingzhi's process voice.
 */
export function latestAuthoredPresenceText(
  messages?: ILantaiChatSession["messages"],
  currentRunId = "",
  currentClientTurnId = "",
): string {
  return (
    latestAuthoredPresence(messages, currentRunId, currentClientTurnId)?.text ||
    ""
  );
}

function previewEventRunId(
  event: ChatTurnEventPreview["events"][number],
): string {
  const payload = objectField(event.payload);
  return singleLineExcerpt(
    event.run_id || event.turn_id || payload.run_id || payload.turn_id,
    160,
  );
}

function previewEventClientTurnId(
  event: ChatTurnEventPreview["events"][number],
): string {
  const payload = objectField(event.payload);
  const message = objectField(payload.message);
  const messageMetadata = objectField(message.metadata);
  return singleLineExcerpt(
    event.client_turn_id ||
      payload.client_turn_id ||
      messageMetadata.client_turn_id,
    160,
  );
}

function previewEventRelatedRunIds(
  event: ChatTurnEventPreview["events"][number],
): Set<string> {
  const payload = objectField(event.payload);
  const subRun = objectField(payload.sub_run);
  return new Set(
    [
      previewEventRunId(event),
      singleLineExcerpt(payload.parent_run_id, 160),
      singleLineExcerpt(payload.source_run_id, 160),
      singleLineExcerpt(subRun.parent_run_id, 160),
    ].filter(Boolean),
  );
}

function isDirectTerminalEventForRun(
  event: ChatTurnEventPreview["events"][number],
  runId: string,
): boolean {
  if (!runId || previewEventRunId(event) !== runId) return false;
  const kind = String(event.kind || "").toLowerCase();
  const type = String(event.type || "").toLowerCase();
  return (
    [
      "turn.completed",
      "turn.failed",
      "turn.stalled",
      "turn.interrupted",
    ].includes(type) ||
    [
      "chat.turn.settled",
      "chat.turn.failed",
      "chat.turn.stalled",
      "chat.turn.interrupted",
      "chat.final_answer",
    ].includes(kind)
  );
}

function previewEventTimeMs(
  event: ChatTurnEventPreview["events"][number],
): number {
  const parsed = Date.parse(event.ts || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function latestDirectTurnStartTimeMs(
  events: ChatTurnEventPreview["events"],
  runId: string,
): number {
  return events.reduce((latest, event) => {
    const eventRunId = previewEventRunId(event);
    if (runId && eventRunId && eventRunId !== runId) return latest;
    const payload = objectField(event.payload);
    if (
      stringField(payload, "parent_run_id") ||
      stringField(payload, "source_run_id")
    ) {
      return latest;
    }
    const kind = String(event.kind || "").toLowerCase();
    const type = String(event.type || "").toLowerCase();
    if (kind !== "chat.turn.started" && type !== "turn.started") {
      return latest;
    }
    return Math.max(latest, previewEventTimeMs(event));
  }, 0);
}

function latestDirectClientTurnIdAfter(
  events: ChatTurnEventPreview["events"],
  runId: string,
  afterMs: number,
): string {
  let latestAtMs = 0;
  let latestClientTurnId = "";
  for (const event of events) {
    const eventAtMs = previewEventTimeMs(event);
    if (eventAtMs < afterMs || eventAtMs < latestAtMs) continue;
    const eventRunId = previewEventRunId(event);
    if (runId && eventRunId !== runId) continue;
    const payload = objectField(event.payload);
    if (
      stringField(payload, "parent_run_id") ||
      stringField(payload, "source_run_id")
    ) {
      continue;
    }
    const clientTurnId = previewEventClientTurnId(event);
    if (!clientTurnId) continue;
    latestAtMs = eventAtMs;
    latestClientTurnId = clientTurnId;
  }
  return latestClientTurnId;
}

function previewEventsForRun(
  events: ChatTurnEventPreview["events"],
  runId: string,
  clientTurnId = "",
  latestUserStartedAtMs = 0,
): ChatTurnEventPreview["events"] {
  if (!runId && !clientTurnId) return events;
  // Modern previews carry causal identity on every event. Older, already
  // run-scoped snapshots do not; preserve that all-unscoped collection for
  // compatibility, but once any scoped rows exist never mix unscoped/history
  // rows back into the current turn.
  const hasRunScopedEvents = Boolean(
    runId && events.some((event) => previewEventRelatedRunIds(event).size > 0),
  );
  const hasClientScopedEvents = Boolean(
    clientTurnId && events.some((event) => previewEventClientTurnId(event)),
  );
  let latestDirectClientTurnId = "";
  return events.filter((event) => {
    const eventRunId = previewEventRunId(event);
    const eventClientTurnId = previewEventClientTurnId(event);
    if (eventRunId === runId && eventClientTurnId) {
      latestDirectClientTurnId = eventClientTurnId;
    }
    const isDirectTerminal = isDirectTerminalEventForRun(event, runId);
    const eventTimeMs = previewEventTimeMs(event);
    if (
      runId &&
      hasRunScopedEvents &&
      !previewEventRelatedRunIds(event).has(runId)
    ) {
      return false;
    }
    if (
      isDirectTerminal &&
      clientTurnId &&
      latestDirectClientTurnId &&
      latestDirectClientTurnId !== clientTurnId
    ) {
      // Native terminal rows historically omitted client_turn_id. Infer the
      // logical turn only from the latest preceding event on this exact run;
      // queued user receipt time alone is insufficient because the old turn
      // may settle after the next user message has already been accepted.
      return false;
    }
    if (
      isDirectTerminal &&
      latestUserStartedAtMs > 0 &&
      eventTimeMs > 0 &&
      eventTimeMs < latestUserStartedAtMs
    ) {
      // A Native run may accept multiple queued client turns. Its terminal
      // event is run-scoped but historically had no client-turn id, so an old
      // logical turn must not settle a newer user message reusing that run.
      return false;
    }
    if (
      clientTurnId &&
      hasClientScopedEvents &&
      eventClientTurnId !== clientTurnId &&
      // Native terminal events carry the authoritative runtime turn id but no
      // client-turn id. Keep only a terminal row whose own run id exactly
      // matches the active run; a child run merely naming this run as parent
      // must never settle the parent.
      !isDirectTerminal
    ) {
      return false;
    }
    return true;
  });
}

function runningPresenceText(stageTitle: string): string {
  const stage = singleLineExcerpt(stageTitle || "处理当前任务", 72);
  return /^正在/.test(stage) ? stage : `正在${stage}`;
}

export function buildPresencePresentation(options: {
  status: string;
  stageTitle: string;
  stageLabel?: string;
  activeStageRunning?: boolean;
  authoredText?: string;
  structuredText?: string;
  stall: RuntimeStallState;
}): PresencePresentation | null {
  const { status, stageTitle, stall } = options;
  const authoredText = publicProgressText(options.authoredText);
  const structuredText = singleLineExcerpt(options.structuredText);
  const presenceText = authoredText || structuredText;

  if (status === "running") {
    // Preview timestamps measure ledger activity, not the primary text stream.
    // Silence cannot establish a failed task or a disconnected transport. A
    // transport heartbeat only proves the connection is alive; it must not be
    // promoted into evidence that the model or a tool made business progress.
    // Keep diagnostics in metadata and preserve authored progress verbatim.
    const transportInterrupted = ["reconnecting", "closed"].includes(
      stall.transportStatus,
    );
    const quiet = stall.level === "stale" || stall.level === "stalled";
    const waitingForFirstOutput = Boolean(
      stall.waitingForFirstOutput || options.stageLabel === "理解中",
    );
    const hasSubstantiveProgress = Boolean(stall.hasSubstantiveProgress);
    const activeStageRunning = options.activeStageRunning !== false;
    const stage = waitingForFirstOutput
      ? "等待回应"
      : !activeStageRunning && hasSubstantiveProgress
        ? "处理中"
        : options.stageLabel?.trim() || runningPresenceText(stageTitle);
    const healthyMeta = "";
    return {
      stage,
      text: presenceText,
      meta: transportInterrupted
        ? stall.transportStatus === "reconnecting"
          ? "正在恢复进度同步"
          : "进度同步暂时中断 · 查看详情"
        : quiet
          ? stall.transportAlive
            ? `连接正常${stall.elapsedLabel ? ` · ${stall.elapsedLabel} 没有新进展` : " · 暂无新进展"}`
            : stall.level === "stalled"
              ? "较久未收到进度更新 · 查看详情"
              : ""
          : healthyMeta,
      tone: transportInterrupted || quiet ? "quiet" : "active",
      authored: Boolean(authoredText),
    };
  }

  return (
    (
      {
        failed: {
          stage: "需要处理",
          text: "这次没能完成",
          meta: "查看原因和重试方式",
          tone: "warning",
          authored: false,
        },
        stalled: {
          stage: "需要处理",
          text: "暂时没有新进展",
          meta: "查看详情或续跑",
          tone: "warning",
          authored: false,
        },
        incomplete: {
          stage: "尚未完成",
          text: "回答还不完整，可以继续",
          meta: "继续回答",
          tone: "warning",
          authored: false,
        },
        needs_input: {
          stage: "等你补充",
          text: "需要你补充一些信息",
          meta: "等待你的回复",
          tone: "warning",
          authored: false,
        },
        blocked: {
          stage: "暂时受阻",
          text: "缺少必要条件，暂时无法继续",
          meta: "查看缺少的条件",
          tone: "warning",
          authored: false,
        },
        interrupted: {
          stage: "已经停下",
          text: "已停止",
          meta: "查看本轮执行",
          tone: "quiet",
          authored: false,
        },
        stopping: {
          stage: "正在停止",
          text: "正在停止，请稍等",
          meta: "等待停止确认",
          tone: "quiet",
          authored: false,
        },
        completed: {
          stage: "已完成",
          text: "",
          meta: "",
          tone: "quiet",
          authored: false,
        },
        finished: {
          stage: "已结束",
          text: "",
          meta: "",
          tone: "quiet",
          authored: false,
        },
      } satisfies Record<string, PresencePresentation>
    )[status] || null
  );
}

type TracePhase =
  | "request"
  | "model"
  | "dependency"
  | "test"
  | "lint"
  | "build"
  | "preview"
  | "deploy"
  | "search"
  | "command"
  | "file"
  | "approval"
  | "subtask"
  | "checkpoint"
  | "artifact"
  | "result"
  | "failure"
  | "tool";

export interface TraceStep {
  id: string;
  time: string;
  timestampMs: number | null;
  phase: TracePhase;
  title: string;
  status: string;
  detail: string;
  durationMs?: number | null;
  diagnosis?: RuntimeFailureDiagnosis | null;
  debug: Record<string, unknown>;
}

export type WorkPhaseId = "prepare" | "execute" | "verify" | "deliver";

export interface WorkPhaseSummary {
  id: WorkPhaseId;
  label: string;
  count: number;
  total: number;
  status:
    | "unknown"
    | "observed"
    | "pending"
    | "running"
    | "completed"
    | "failed";
}

const TRACE_PHASES = new Set<TracePhase>([
  "request",
  "model",
  "dependency",
  "test",
  "lint",
  "build",
  "preview",
  "deploy",
  "search",
  "command",
  "file",
  "approval",
  "subtask",
  "checkpoint",
  "artifact",
  "result",
  "failure",
  "tool",
]);

function formatEventTime(ts: string): string {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "--:--";
  return `${String(date.getHours()).padStart(2, "0")}:${String(
    date.getMinutes(),
  ).padStart(2, "0")}`;
}

function workspaceLabel(workspaceDir?: string | null): string {
  if (!workspaceDir) return "";
  const normalized = workspaceDir.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts[parts.length - 1] || workspaceDir;
}

function runtimeStatusLabel(status?: string | null): string {
  return (
    {
      running: "运行中",
      stopping: "正在停止",
      completed: "任务成功",
      finished: "回复结束",
      incomplete: "部分完成",
      needs_input: "等待补充",
      blocked: "受阻",
      interrupted: "已中断",
      failed: "失败",
      stalled: "已卡住",
      idle: "空闲",
    }[String(status || "").toLowerCase()] || "运行态"
  );
}

function traceStatusLabel(status?: string | null): string {
  return (
    {
      running: "进行中",
      queued: "等待中",
      completed: "已完成",
      finished: "已结束",
      incomplete: "部分完成",
      needs_input: "等待补充",
      blocked: "受阻",
      failed: "失败",
      stalled: "已卡住",
      interrupted: "已停止",
      recovered: "已恢复",
      info: "已记录",
      idle: "待开始",
    }[String(status || "").toLowerCase()] || "已记录"
  );
}

function traceStepKind(step: TraceStep): string {
  return String(step.debug.kind || "").toLowerCase();
}

function traceStepType(step: TraceStep): string {
  return String(step.debug.type || "").toLowerCase();
}

function traceStepTechnicalEvidence(step: TraceStep): string {
  const payload = objectField(step.debug.payload);
  return [
    traceStepKind(step),
    traceStepType(step),
    step.title,
    step.detail,
    stringField(step.debug, "tool_name"),
    stringField(payload, "tool_name"),
    stringField(payload, "name"),
    stringField(payload, "command"),
    stringField(payload, "path"),
    stringField(payload, "file_path"),
    stringField(payload, "trace_title"),
    stringField(payload, "trace_detail"),
  ].join(" ");
}

function isRunningTraceStatus(status?: string | null): boolean {
  return ["running", "queued"].includes(String(status || "").toLowerCase());
}

function isFailureTraceStatus(status?: string | null): boolean {
  return ["failed", "stalled", "interrupted"].includes(
    String(status || "").toLowerCase(),
  );
}

function workPhaseForTracePhase(phase: TracePhase): WorkPhaseId {
  if (["request", "model", "search", "dependency"].includes(phase)) {
    return "prepare";
  }
  if (["test", "lint", "build", "preview", "checkpoint"].includes(phase)) {
    return "verify";
  }
  if (["artifact", "deploy", "result"].includes(phase)) return "deliver";
  return "execute";
}

export function buildWorkPhaseSummary(steps: TraceStep[]): WorkPhaseSummary[] {
  const definitions: Array<{ id: WorkPhaseId; label: string }> = [
    { id: "prepare", label: "准备" },
    { id: "execute", label: "执行" },
    { id: "verify", label: "验证" },
    { id: "deliver", label: "交付" },
  ];
  return definitions.map(({ id, label }) => {
    const phaseSteps = steps.filter(
      (step) => workPhaseForTracePhase(step.phase) === id,
    );
    if (phaseSteps.length === 0) {
      return { id, label, count: 0, total: 0, status: "unknown" };
    }
    const completedCount = phaseSteps.filter((step) =>
      ["completed", "finished", "recovered"].includes(
        String(step.status || "").toLowerCase(),
      ),
    ).length;
    if (phaseSteps.some((step) => isFailureTraceStatus(step.status))) {
      return {
        id,
        label,
        count: completedCount,
        total: phaseSteps.length,
        status: "failed",
      };
    }
    if (
      phaseSteps.some(
        (step) => String(step.status || "").toLowerCase() === "running",
      )
    ) {
      return {
        id,
        label,
        count: completedCount,
        total: phaseSteps.length,
        status: "running",
      };
    }
    if (completedCount === phaseSteps.length) {
      return {
        id,
        label,
        count: completedCount,
        total: phaseSteps.length,
        status: "completed",
      };
    }
    if (
      phaseSteps.some((step) =>
        ["queued", "idle", "pending"].includes(
          String(step.status || "").toLowerCase(),
        ),
      )
    ) {
      return {
        id,
        label,
        count: completedCount,
        total: phaseSteps.length,
        status: "pending",
      };
    }
    return {
      id,
      label,
      count: completedCount,
      total: phaseSteps.length,
      status: "observed",
    };
  });
}

function traceStepBelongsToRun(step: TraceStep, currentRunId: string): boolean {
  if (!currentRunId) return true;
  const stepRunId = String(step.debug.run_id || "").trim();
  if (stepRunId) return stepRunId === currentRunId;
  const payload = objectField(step.debug.payload);
  const parentRunId = stringField(payload, "parent_run_id");
  if (parentRunId === currentRunId) return false;
  const sourceRunId = stringField(payload, "source_run_id");
  return !sourceRunId || sourceRunId === currentRunId;
}

function resolveOutcomeStatus(
  runtimeOutcomeStatus: string | null | undefined,
  steps: TraceStep[],
  currentRunId = "",
): string {
  const explicit = String(runtimeOutcomeStatus || "").toLowerCase();
  if (explicit && explicit !== "unknown") return explicit;
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (!traceStepBelongsToRun(steps[index], currentRunId)) continue;
    const rawPayload = steps[index].debug.payload;
    const payload =
      rawPayload && typeof rawPayload === "object" && !Array.isArray(rawPayload)
        ? (rawPayload as Record<string, unknown>)
        : {};
    const outcome = stringField(payload, "outcome_status").toLowerCase();
    if (outcome) return outcome;
  }
  return explicit || "unknown";
}

function completionPresentationStatus(outcomeStatus: string): string {
  return (
    {
      delivered: "completed",
      incomplete: "incomplete",
      needs_input: "needs_input",
      blocked: "blocked",
      unknown: "finished",
    }[outcomeStatus] || "finished"
  );
}

function resolveExecutionStatus(
  runtimeStatus: string | null | undefined,
  outcomeStatus: string,
  steps: TraceStep[],
  currentRunId = "",
): string {
  const rawExplicit = String(runtimeStatus || "").toLowerCase();
  const explicit =
    rawExplicit === "finished"
      ? "completed"
      : rawExplicit === "stopped"
        ? "interrupted"
        : rawExplicit;

  // A terminal turn event is a newer, causal fact than a lagging runtime
  // snapshot. In particular, reconnect races can leave `status=running` for
  // one render after `turn.failed` or `turn.completed` is already visible.
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index];
    const kind = traceStepKind(step);
    const type = traceStepType(step);
    if (!traceStepBelongsToRun(step, currentRunId)) continue;
    if (type === "turn.failed" || kind === "chat.turn.failed") return "failed";
    if (type === "turn.stalled" || kind === "chat.turn.stalled") {
      return "stalled";
    }
    if (type === "turn.interrupted" || kind === "chat.turn.interrupted") {
      return "interrupted";
    }
    if (
      type === "turn.completed" ||
      kind === "chat.turn.settled" ||
      kind === "chat.final_answer"
    ) {
      return completionPresentationStatus(outcomeStatus);
    }
  }

  if (
    explicit &&
    explicit !== "idle" &&
    explicit !== "completed" &&
    explicit !== "finished"
  ) {
    return explicit;
  }

  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (!traceStepBelongsToRun(steps[index], currentRunId)) continue;
    const status = String(steps[index].status || "").toLowerCase();
    if (status === "failed") return "failed";
    if (status === "stalled") return "stalled";
    if (status === "interrupted") return "interrupted";
  }

  const currentRunSteps = steps.filter((step) =>
    traceStepBelongsToRun(step, currentRunId),
  );
  if (currentRunSteps.some((step) => isRunningTraceStatus(step.status))) {
    return "running";
  }
  if (
    currentRunSteps.some((step) => traceStatusLabel(step.status) === "已完成")
  ) {
    return completionPresentationStatus(outcomeStatus);
  }
  if (explicit === "completed" || explicit === "finished") {
    return completionPresentationStatus(outcomeStatus);
  }
  return explicit || "idle";
}

function isResponseTraceStep(step: TraceStep): boolean {
  const kind = traceStepKind(step);
  const type = traceStepType(step);
  return (
    kind.startsWith("chat.message.") ||
    kind === "chat.final_answer" ||
    type.startsWith("message.")
  );
}

function isReasoningResponseTraceStep(step: TraceStep): boolean {
  const payload = objectField(step.debug.payload);
  const message = objectField(payload.message);
  const explicitType = stringField(
    payload,
    "content_type",
    "stream_message_type",
    "message_type",
  ).toLowerCase();
  if (["thinking", "reasoning"].includes(explicitType)) return true;
  if (
    ["thinking", "reasoning"].includes(
      stringField(message, "type").toLowerCase(),
    )
  ) {
    return true;
  }
  const content = Array.isArray(message.content) ? message.content : [];
  return content.some((block) =>
    ["thinking", "reasoning"].includes(
      stringField(objectField(block), "type").toLowerCase(),
    ),
  );
}

function isTurnCompletionTraceStep(step: TraceStep): boolean {
  const kind = traceStepKind(step);
  const type = traceStepType(step);
  return (
    kind === "chat.turn.settled" ||
    type === "turn.completed" ||
    type === "turn.interrupted"
  );
}

function isTurnStartTraceStep(step: TraceStep): boolean {
  const kind = traceStepKind(step);
  const type = traceStepType(step);
  return kind === "chat.turn.started" || type === "turn.started";
}

function isQuietDiagnosticStep(step: TraceStep): boolean {
  if (step.diagnosis || isFailureTraceStatus(step.status)) return false;
  const kind = traceStepKind(step);
  const detail = `${step.title} ${step.detail}`.toLowerCase();
  return (
    kind === "chat.model.routing_decision" ||
    (kind === "chat.system.event" &&
      /model usage|token usage|用量|routing|路由/.test(detail))
  );
}

export function buildExecutionStages(
  rawSteps: TraceStep[],
  executionStatus: string,
): TraceStep[] {
  const stages: TraceStep[] = [];
  let reasoningStageIndex = -1;
  let responseStageIndex = -1;

  for (const step of rawSteps) {
    if (isResponseTraceStep(step)) {
      const completed =
        ["completed", "finished"].includes(
          String(step.status || "").toLowerCase(),
        ) ||
        traceStepKind(step) === "chat.message.completed" ||
        traceStepKind(step) === "chat.final_answer";
      if (isReasoningResponseTraceStep(step)) {
        const nextReasoningStage: TraceStep = {
          ...step,
          id:
            reasoningStageIndex >= 0 ? stages[reasoningStageIndex].id : step.id,
          phase: "model",
          title: "梳理任务思路",
          status: completed ? "completed" : "running",
          detail: completed ? "思路已梳理" : "正在理解并梳理任务",
        };
        if (reasoningStageIndex >= 0) {
          stages[reasoningStageIndex] = nextReasoningStage;
        } else {
          reasoningStageIndex = stages.length;
          stages.push(nextReasoningStage);
        }
        continue;
      }
      if (reasoningStageIndex >= 0) {
        stages[reasoningStageIndex] = {
          ...stages[reasoningStageIndex],
          status: "completed",
          detail: "思路已梳理",
        };
      }
      const nextResponseStage: TraceStep = {
        ...step,
        id: responseStageIndex >= 0 ? stages[responseStageIndex].id : step.id,
        phase: "result",
        title: "生成回复",
        status: completed ? "completed" : "running",
        detail: completed ? "回复已生成" : "正在组织并生成答复",
      };
      if (responseStageIndex >= 0) {
        stages[responseStageIndex] = nextResponseStage;
      } else {
        responseStageIndex = stages.length;
        stages.push(nextResponseStage);
      }
      continue;
    }

    if (isTurnCompletionTraceStep(step)) {
      if (responseStageIndex >= 0) {
        stages[responseStageIndex] = {
          ...stages[responseStageIndex],
          status:
            String(step.status || "").toLowerCase() === "interrupted"
              ? "interrupted"
              : "completed",
          detail:
            String(step.status || "").toLowerCase() === "interrupted"
              ? "生成已停止"
              : "回复已生成",
          durationMs: step.durationMs ?? stages[responseStageIndex].durationMs,
        };
      } else if (stages.length === 0) {
        stages.push({
          ...step,
          phase: "result",
          title: "完成本轮",
          detail: step.detail || "本轮执行已结束",
        });
      }
      continue;
    }

    if (isQuietDiagnosticStep(step)) continue;

    if (isTurnStartTraceStep(step)) {
      stages.push({
        ...step,
        title: "收到消息",
        detail: step.detail.replace(/^文本：\s*/, "") || "消息已送达模型",
      });
      continue;
    }

    stages.push(step);
  }

  if (stages.length === 0 && rawSteps.length > 0) {
    const fallback = rawSteps[rawSteps.length - 1];
    stages.push({
      ...fallback,
      phase: executionStatus === "running" ? "request" : "result",
      title: executionStatus === "running" ? "等待模型响应" : "完成本轮",
      status: executionStatus === "running" ? "running" : executionStatus,
      detail:
        executionStatus === "running"
          ? "请求已送达，正在等待首个回复或工具动作"
          : "本轮执行信息已记录",
    });
  }

  if (stages.length === 0 && executionStatus === "running") {
    stages.push({
      id: "running-awaiting-first-event",
      time: "",
      timestampMs: null,
      phase: "request",
      title: "等待模型响应",
      status: "running",
      detail: "请求已经开始，正在等待首个回复或工具动作",
      diagnosis: null,
      debug: { synthesized_from_running_status: true },
    });
  }

  if (
    stages.length === 0 &&
    ["completed", "finished"].includes(executionStatus)
  ) {
    stages.push({
      id: "terminal-transcript-reconciliation",
      time: "",
      timestampMs: null,
      phase: "result",
      title: "完成本轮",
      status: "completed",
      detail: "回复已生成，执行详情仍在同步",
      diagnosis: null,
      debug: { synthesized_from_terminal_status: true },
    });
  }

  const terminal = [
    "completed",
    "finished",
    "incomplete",
    "needs_input",
    "blocked",
    "failed",
    "stalled",
    "interrupted",
  ].includes(executionStatus);
  const lastRunningIndex = stages.reduce(
    (latest, step, index) =>
      isRunningTraceStatus(step.status) ? index : latest,
    -1,
  );

  const settledStages = stages.map((step, index) => {
    if (!isRunningTraceStatus(step.status)) return step;
    const hasLaterVisibleStage = index < stages.length - 1;
    if (terminal || hasLaterVisibleStage || index !== lastRunningIndex) {
      return {
        ...step,
        status:
          isFailureTraceStatus(executionStatus) && index === lastRunningIndex
            ? executionStatus
            : "completed",
      };
    }
    return { ...step, status: "running" };
  });

  if (executionStatus !== "completed") return settledStages;

  return settledStages.map((step) => {
    if (!isFailureTraceStatus(step.status)) return step;
    return {
      ...step,
      status: "recovered",
      detail: "这一步遇到的问题已由后续步骤处理，原始记录仍保留在技术详情中。",
      diagnosis: null,
      debug: {
        ...step.debug,
        recovered_failure: {
          status: step.status,
          detail: step.detail,
          diagnosis: step.diagnosis,
        },
      },
    };
  });
}

function executionHeadline(status: string): string {
  return (
    {
      running: "正在响应",
      stopping: "停止请求已发送",
      completed: "任务成功，成果已交付",
      finished: "回复已结束",
      incomplete: "任务部分完成",
      needs_input: "等待你的补充",
      blocked: "本轮执行受阻",
      failed: "本轮未完成",
      stalled: "执行可能卡住",
      interrupted: "本轮已停止",
      idle: "执行记录",
    }[status] || "执行记录"
  );
}

const FRIENDLY_PHASE_TITLES: Record<TracePhase, string> = {
  request: "理解你的要求",
  model: "梳理任务思路",
  dependency: "准备环境",
  test: "验证结果",
  lint: "检查格式",
  build: "构建产物",
  preview: "查看预览",
  deploy: "发布结果",
  search: "查找资料",
  command: "执行命令",
  file: "处理文件",
  approval: "等待你的确认",
  subtask: "处理子任务",
  checkpoint: "保存进度",
  artifact: "生成结果",
  result: "整理回答",
  failure: "处理遇到的问题",
  tool: "执行工具",
};

function isTechnicalTraceText(value: string): boolean {
  const text = String(value || "").trim();
  if (!text) return false;
  return (
    /(^|\s)(cd|cat|ls|find|grep|rg|sed|awk|python\d*|node|npm|pnpm|yarn|git|curl|mkdir|cp|mv|unzip|pytest|vitest)(\s|$)/i.test(
      text,
    ) ||
    /(^|[\s"'`])(?:\.?\.?\/|[A-Za-z]:\\|\/Users\/|\/tmp\/|\/var\/)/.test(
      text,
    ) ||
    /\b(?:read_file|write_file|execute_shell_command|tool_call|trace_detail|workspace_dir)\b/i.test(
      text,
    ) ||
    /(?:&&|\|\||\$\s|```|\{\s*"|\bimport\s+[A-Za-z_])/i.test(text) ||
    /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/i.test(text)
  );
}

function friendlyStageTitle(step: TraceStep): string {
  const title = singleLineExcerpt(step.title, 64);
  if (
    step.phase === "file" &&
    /\bsend_file_to_user\b/i.test(traceStepTechnicalEvidence(step))
  ) {
    return "交付文件";
  }
  if (
    step.phase === "file" &&
    /^(?:读取文件|更新文件|写入文件|修改文件)$/.test(title)
  ) {
    return title;
  }
  const alwaysProject = [
    "dependency",
    "test",
    "lint",
    "build",
    "preview",
    "deploy",
    "search",
    "command",
    "file",
    "tool",
  ].includes(step.phase);
  if (title && !isTechnicalTraceText(title) && !alwaysProject) {
    return title;
  }
  if (step.phase === "file") {
    const evidence = traceStepTechnicalEvidence(step);
    if (
      /\b(write|save|create|edit)\b|写入|保存|创建|生成|编辑/i.test(evidence)
    ) {
      return "更新文件";
    }
  }
  return FRIENDLY_PHASE_TITLES[step.phase];
}

function friendlyStageDetail(step: TraceStep): string {
  const status = String(step.status || "").toLowerCase();
  const isRunning = isRunningTraceStatus(status);
  const isFailed = isFailureTraceStatus(status);
  const detail = singleLineExcerpt(step.detail, 120);
  const technicalEvidence = traceStepTechnicalEvidence(step);

  if (step.debug.recovered_failure) {
    return /send_file_to_user/i.test(technicalEvidence)
      ? "这次文件发送没有成功；本轮后来已恢复，请以聊天中的实际附件为准。"
      : "这一步曾遇到问题，后续步骤已经恢复；原始记录仍保留在技术详情中。";
  }
  if (isFailed) return "这一步没有顺利完成，原因和原始输出已经保留。";
  if (step.phase === "file" && /send_file_to_user/i.test(technicalEvidence)) {
    if (isRunning) return "正在把文件发送到当前聊天。";
    return "文件已发送到当前聊天，可以直接打开或下载。";
  }
  if (detail && !isTechnicalTraceText(detail)) return detail;
  if (isRunning) return "正在完成这一步，新的进展会继续显示在这里。";

  return (
    {
      request: "要求已经收到，接下来会按目标逐步完成。",
      model: "处理思路已经整理清楚。",
      dependency: "需要的运行条件已经准备好。",
      test: "结果已经通过这一轮检查。",
      lint: "内容和格式检查已经完成。",
      build: "可交付内容已经整理完成。",
      preview: "预览已经准备好，可以检查实际效果。",
      deploy: "结果已经发布。",
      search: "相关资料已经找到并纳入处理。",
      command: "所需处理已经完成，结果已经记录。",
      file: "相关材料已经读取或更新。",
      approval: "确认结果已经记录。",
      subtask: "并行任务已经返回结果。",
      checkpoint: "当前进度已经安全保留。",
      artifact: "新的交付内容已经生成。",
      result: "回复已经整理完成。",
      failure: "问题已经记录，可以查看原因并继续处理。",
      tool: "所需能力已经调用完成。",
    } satisfies Record<TracePhase, string>
  )[step.phase];
}

function presenceStageLabel(step?: TraceStep): string {
  if (!step) return "处理中";
  if (step.phase === "request") return "理解中";
  if (step.phase === "model") return "思考中";
  if (step.phase === "result") return "收尾中";
  if (step.phase === "approval") return "等你确认";
  const title = friendlyStageTitle(step);
  return /^(?:正在|等待|需要|继续)/.test(title)
    ? title
    : runningPresenceText(title);
}

function presenceStructuredDetail(step?: TraceStep): string {
  if (!step || step.phase !== "subtask") return "";
  const payload = objectField(step.debug.payload);
  const traceTitle = stringField(payload, "trace_title");
  if (!["正在组建协作室", "正在修正协作配置"].includes(traceTitle)) {
    return "";
  }
  const detail = singleLineExcerpt(stringField(payload, "trace_detail"), 120);
  if (!detail.startsWith("沈行知正在") || isTechnicalTraceText(detail)) {
    return "";
  }
  const title = singleLineExcerpt(step.title, 120);
  return detail === title ? "" : detail;
}

function traceStepToolCallId(step?: TraceStep): string {
  if (!step) return "";
  const payload = objectField(step.debug.payload);
  const direct = stringField(
    payload,
    "id",
    "tool_call_id",
    "call_id",
    "effect_id",
  );
  if (direct) return direct;
  const startedEvent = objectField(step.debug.started_event);
  const startedPayload = objectField(startedEvent.payload);
  return stringField(
    startedPayload,
    "id",
    "tool_call_id",
    "call_id",
    "effect_id",
  );
}

function presenceLiveAnnouncement(
  agentName: string,
  presentation: PresencePresentation,
): string {
  const primary = presentation.text || presentation.stage;
  const details = [
    presentation.text ? presentation.stage : "",
    presentation.meta,
  ].filter(Boolean);
  return `${agentName}：${primary}${details.length ? `；${details.join("；")}` : ""}`;
}

function workbenchAgentName(session?: ExtendedSession): string {
  const candidate =
    session?.agent_name || session?.agentName || session?.assistant_name;
  return singleLineExcerpt(candidate, 24) || "沈行知";
}

function isCompletedOutputStage(step: TraceStep): boolean {
  if (step.debug.recovered_failure) return false;
  return [
    "completed",
    "finished",
    "recovered",
    "succeeded",
    "success",
    "verified",
  ].includes(String(step.status || "").toLowerCase());
}

function isDeliveredOutputStage(step: TraceStep): boolean {
  if (!isCompletedOutputStage(step)) return false;
  const payload = objectField(step.debug.payload);
  const toolName = stringField(payload, "tool_name", "name").toLowerCase();
  if (toolName === "send_file_to_user") return true;
  return (
    traceStepKind(step) === "chat.artifact.created" ||
    traceStepType(step) === "artifact.created"
  );
}

export interface WorkbenchOverview {
  label: string;
  headline: string;
  detail: string;
  nextStep: string;
  tone: "active" | "success" | "warning" | "danger" | "quiet";
  results: Array<{ title: string; detail: string }>;
}

function isGenericWorkbenchSummary(value: string) {
  return /^(?:本轮|这轮)(?:已)?(?:完成|结束|运行结束|开始|处理中)$|^(?:回复|回答)(?:已经)?(?:生成|完成)$/.test(
    value.trim(),
  );
}

export function buildWorkbenchOverview({
  status,
  outcomeStatus,
  deliveryStatus,
  summary,
  stages,
}: {
  status: string;
  outcomeStatus: string;
  deliveryStatus?: string | null;
  summary?: string | null;
  stages: TraceStep[];
}): WorkbenchOverview {
  const normalizedStatus = String(status || "").toLowerCase();
  const normalizedOutcome = String(outcomeStatus || "").toLowerCase();
  const normalizedDelivery = String(deliveryStatus || "").toLowerCase();
  const results = stages
    .filter(isDeliveredOutputStage)
    .map((step) => ({
      title: friendlyStageTitle(step),
      detail: friendlyStageDetail(step),
    }))
    .filter(
      (item, index, values) =>
        values.findIndex(
          (candidate) =>
            candidate.title === item.title && candidate.detail === item.detail,
        ) === index,
    )
    .slice(0, 4);
  const delivered =
    normalizedOutcome === "delivered" || normalizedDelivery === "verified";
  const genericFallback = (() => {
    if (normalizedStatus === "running") return "新的进展会继续显示在下方。";
    if (normalizedStatus === "needs_input")
      return "需要你的决定或补充后才能继续。";
    if (["failed", "blocked", "stalled"].includes(normalizedStatus)) {
      return "问题和恢复入口已经保留在下方。";
    }
    if (normalizedStatus === "interrupted") return "已有结果仍然保留。";
    if (normalizedStatus === "incomplete")
      return "已有内容已保留，可以继续补全。";
    if (results[0]) return results[0].detail;
    return delivered
      ? "结果已显示在当前聊天中。"
      : "运行已经结束，结果仍需以聊天中的实际内容为准。";
  })();
  const cleanSummary = singleLineExcerpt(summary, 180);
  const detail =
    cleanSummary && !isGenericWorkbenchSummary(cleanSummary)
      ? cleanSummary
      : genericFallback;

  if (normalizedStatus === "running") {
    return {
      label: "正在处理",
      headline: "正在完成这轮工作",
      detail,
      nextStep: "可以离开此页，工作会在后台继续。",
      tone: "active",
      results,
    };
  }
  if (normalizedStatus === "needs_input") {
    return {
      label: "需要你处理",
      headline: "还需要你的决定或补充",
      detail,
      nextStep: "查看下方说明，补充信息后继续。",
      tone: "warning",
      results,
    };
  }
  if (["failed", "blocked", "stalled"].includes(normalizedStatus)) {
    return {
      label: "需要处理",
      headline: "这轮还没有完成",
      detail,
      nextStep: "查看问题说明，再选择继续处理或重新尝试。",
      tone: "danger",
      results,
    };
  }
  if (normalizedStatus === "incomplete") {
    return {
      label: "结果不完整",
      headline: "回答还需要补全",
      detail,
      nextStep: "选择“继续回答”，已有内容不会丢失。",
      tone: "warning",
      results,
    };
  }
  if (normalizedStatus === "interrupted") {
    return {
      label: "已停止",
      headline: "这轮已经停止",
      detail,
      nextStep: "如需继续，可重新尝试；已有结果仍会保留。",
      tone: "quiet",
      results,
    };
  }
  if (delivered) {
    return {
      label: "本轮结果",
      headline: "任务成功，成果已交付",
      detail,
      nextStep: results.length
        ? "在当前聊天中打开或下载交付内容。"
        : "结果已显示在当前聊天回复中。",
      tone: "success",
      results,
    };
  }
  return {
    label: "本轮状态",
    headline: "回复已结束",
    detail,
    nextStep: "请以当前聊天中的实际回复和文件为准。",
    tone: "quiet",
    results,
  };
}

function executionElapsedLabel(
  runtimeState: ChatRuntimeState | null,
  steps: TraceStep[],
  nowMs: number,
  running: boolean,
  currentRunId = "",
): string {
  const startedAt = Date.parse(runtimeState?.started_at || "") || 0;
  const currentRunSteps = steps.filter((step) =>
    traceStepBelongsToRun(step, currentRunId),
  );
  const eventTimestamps = currentRunSteps
    .map((step) => step.timestampMs)
    .filter((value): value is number => typeof value === "number");
  const fallbackStart = startedAt || eventTimestamps[0] || 0;
  let terminalAt = 0;
  for (let index = currentRunSteps.length - 1; index >= 0; index -= 1) {
    const step = currentRunSteps[index];
    const kind = traceStepKind(step);
    const type = traceStepType(step);
    if (
      [
        "turn.completed",
        "turn.failed",
        "turn.stalled",
        "turn.interrupted",
      ].includes(type) ||
      [
        "chat.turn.settled",
        "chat.turn.failed",
        "chat.turn.stalled",
        "chat.turn.interrupted",
        "chat.final_answer",
      ].includes(kind)
    ) {
      terminalAt = step.timestampMs || 0;
      if (terminalAt) break;
    }
  }
  // `updated_at` is also the history/projection hydration time, so it can move
  // hours after a completed task. A durable terminal event is the causal end;
  // use mutable snapshot metadata only for legacy rows without event times.
  const completedAt =
    terminalAt ||
    eventTimestamps[eventTimestamps.length - 1] ||
    Date.parse(runtimeState?.updated_at || "") ||
    0;
  if (!fallbackStart) return "";
  const end = running ? nowMs : completedAt;
  if (!end || end < fallbackStart) return "";
  const elapsed = end - fallbackStart;
  if (elapsed > 86_400_000) return "";
  return formatDuration(elapsed);
}

function buildRetryableText(response: {
  text?: string | null;
  reason?: string;
}): string {
  return typeof response.text === "string" ? response.text.trim() : "";
}

function tracePhaseFromEvent(
  event: ChatTurnEventPreview["events"][number],
): TracePhase {
  const payload = event.payload || {};
  const explicit = String(event.phase || payload.phase || "").toLowerCase();
  const toolName = stringField(payload, "tool_name", "name").toLowerCase();
  const combined = [
    explicit,
    event.kind,
    event.type || "",
    event.label,
    event.detail,
    String(payload.tool_name || ""),
    String(payload.command || ""),
  ]
    .join(" ")
    .toLowerCase();

  if (
    ["read_file", "write_file", "edit_file", "send_file_to_user"].includes(
      toolName,
    )
  ) {
    return "file";
  }

  if (TRACE_PHASES.has(explicit as TracePhase) && explicit !== "command") {
    return explicit as TracePhase;
  }
  if (
    /\b(npm|pnpm|yarn|bun)\s+(install|add|i)\b|\b(pip|uv|poetry|bundle)\s+(install|add|sync)\b/.test(
      combined,
    )
  ) {
    return "dependency";
  }
  if (
    /\b(deploy|release|vercel|wrangler|firebase deploy|cloudbase|tcb)\b/.test(
      combined,
    )
  ) {
    return "deploy";
  }
  if (
    /\b(pytest|vitest|jest|playwright|cypress)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?(test|vitest|jest)\b|\b(go|cargo|swift)\s+test\b|\bxcodebuild\b.*\btest\b/.test(
      combined,
    )
  ) {
    return "test";
  }
  if (
    /\b(eslint|ruff|mypy|pyright|prettier|tsc)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?(lint|typecheck|type-check|format:check)\b/.test(
      combined,
    )
  ) {
    return "lint";
  }
  if (
    /\b(npm|pnpm|yarn|bun)\s+(run\s+)?build\b|\b(vite|next|nuxt|astro)\s+build\b|\b(go|cargo|swift)\s+build\b|\bxcodebuild\b|\bmvn\s+package\b/.test(
      combined,
    )
  ) {
    return "build";
  }
  if (
    /\b(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|preview|start|serve)\b|\b(vite|next|nuxt|astro)\s+(dev|preview)\b|\bpython\s+-m\s+http\.server\b|\blocalhost:\d+\b/.test(
      combined,
    )
  ) {
    return "preview";
  }
  if (explicit === "command") return "command";
  if (/failed|error|失败|异常/.test(combined)) return "failure";
  if (/rg|grep|search|find|glob|搜索|检索/.test(combined)) return "search";
  if (
    explicit === "command" ||
    /shell|exec|command|bash|zsh|命令/.test(combined)
  ) {
    return "command";
  }
  if (
    explicit === "file" ||
    /file|patch|edit|write|文件|编辑|写入|修改/.test(combined)
  ) {
    return "file";
  }
  if (explicit === "approval" || /approval|审批|确认/.test(combined)) {
    return "approval";
  }
  if (
    explicit === "subtask" ||
    /subagent|subtask|agent|子任务/.test(combined)
  ) {
    return "subtask";
  }
  if (
    explicit === "checkpoint" ||
    /checkpoint|snapshot|fork|replay|restore|rollback|检查点|快照|分叉|回放|恢复|回滚/.test(
      combined,
    )
  ) {
    return "checkpoint";
  }
  if (explicit === "artifact" || /artifact|产物/.test(combined))
    return "artifact";
  if (explicit === "result" || /final|settled|结果|回复|结束/.test(combined)) {
    return "result";
  }
  if (
    explicit === "request" ||
    event.kind === "chat.turn.started" ||
    event.type === "turn.started"
  ) {
    return "request";
  }
  if (explicit === "model" || event.kind === "chat.model.routing_decision") {
    return "model";
  }
  return "tool";
}

function phaseLabel(phase: TracePhase): string {
  return (
    {
      request: "请求",
      model: "模型",
      dependency: "依赖",
      test: "测试",
      lint: "检查",
      build: "构建",
      preview: "预览",
      deploy: "部署",
      search: "搜索",
      command: "命令",
      file: "文件",
      approval: "审批",
      subtask: "子任务",
      checkpoint: "检查点",
      artifact: "产物",
      result: "结果",
      failure: "失败",
      tool: "工具",
    } satisfies Record<TracePhase, string>
  )[phase];
}

function phaseIcon(phase: TracePhase, status: string): LucideIcon {
  if (/failed|error|interrupted|stalled|失败|中断|卡住/i.test(status)) {
    return AlertCircle;
  }
  if (/running|运行/i.test(status)) return Loader2;
  if (phase === "dependency") return PackageCheck;
  if (phase === "test") return FlaskConical;
  if (phase === "lint") return ScanLine;
  if (phase === "build") return Wrench;
  if (phase === "preview") return MonitorPlay;
  if (phase === "deploy") return Rocket;
  if (phase === "search") return Search;
  if (phase === "command") return Terminal;
  if (phase === "file") return FilePenLine;
  if (phase === "approval") return ShieldCheck;
  if (phase === "subtask") return Bot;
  if (phase === "checkpoint") return GitBranch;
  if (phase === "tool") return Wrench;
  return CheckCircle2;
}

function formatDuration(ms?: number | null): string {
  if (!ms || ms < 0) return "";
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
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

function readableToolStageTitle(toolName: string): string {
  const normalized = toolName.trim().toLowerCase();
  return (
    (
      {
        equip_lantai_tools: "准备所需工具",
        read_skill: "读取技能说明",
        read_file: "读取文件",
        write_file: "写入文件",
        edit_file: "修改文件",
        run_skill_script: "执行脚本",
        send_file_to_user: "交付文件",
        view_image: "查看图片",
        glob_search: "查找文件",
        execute_shell_command: "执行处理步骤",
      } as Record<string, string>
    )[normalized] || ""
  );
}

function subRunPayload(payload: Record<string, unknown>) {
  return payload.sub_run && typeof payload.sub_run === "object"
    ? (payload.sub_run as Record<string, unknown>)
    : {};
}

function buildTraceStep(
  event: ChatTurnEventPreview["events"][number],
): TraceStep {
  const payload = event.payload || {};
  const phase = tracePhaseFromEvent(event);
  let title = String(
    event.trace_title ||
      payload.trace_title ||
      event.label ||
      phaseLabel(phase),
  );
  let detail = String(
    event.trace_detail || payload.trace_detail || event.detail || "",
  );
  const toolName = stringField(payload, "tool_name", "name");
  const readableToolTitle = readableToolStageTitle(toolName);
  if (
    readableToolTitle &&
    /^(?:工具调用|工具结果|tool(?: call| result)?|tool)$/i.test(title.trim())
  ) {
    title = readableToolTitle;
  }
  if (phase === "subtask") {
    const subRun = subRunPayload(payload);
    const agentName =
      stringField(payload, "agent_name") || stringField(subRun, "agent_name");
    const agentRole =
      stringField(payload, "agent_role") || stringField(subRun, "agent_role");
    const reason =
      stringField(payload, "delegation_reason") ||
      stringField(subRun, "delegation_reason");
    const result =
      stringField(payload, "failure_reason") ||
      stringField(subRun, "failure_reason") ||
      stringField(payload, "result_summary") ||
      stringField(subRun, "result_summary");
    if (agentName) title = `子任务：${agentName}`;
    detail = [agentRole, reason, result, detail]
      .filter((item, index, array) => item && array.indexOf(item) === index)
      .join(" · ");
  } else if (phase === "approval") {
    const sourceAgent = stringField(payload, "source_agent_name");
    if (sourceAgent) detail = `${detail} · 来源：${sourceAgent}`;
  }
  return {
    id: event.id,
    time: formatEventTime(event.ts),
    timestampMs: Number.isNaN(Date.parse(event.ts))
      ? null
      : Date.parse(event.ts),
    phase,
    title,
    status: event.status,
    detail,
    durationMs:
      typeof event.duration_ms === "number"
        ? event.duration_ms
        : typeof payload.duration_ms === "number"
          ? payload.duration_ms
          : null,
    diagnosis: runtimeFailureDiagnosisFromPayload(payload),
    debug: {
      id: event.id,
      seq: event.seq,
      run_id: previewEventRunId(event),
      client_turn_id: previewEventClientTurnId(event),
      kind: event.kind,
      type: event.type,
      label: event.label,
      detail: event.detail,
      trace_title: event.trace_title,
      trace_detail: event.trace_detail,
      payload,
    },
  };
}

function mergeToolLifecycleSteps(steps: TraceStep[]): TraceStep[] {
  const merged: TraceStep[] = [];
  const toolIndex = new Map<string, number>();
  for (const step of steps) {
    const kind = String(step.debug.kind || step.debug.type || "");
    const payload =
      step.debug.payload && typeof step.debug.payload === "object"
        ? (step.debug.payload as Record<string, unknown>)
        : {};
    const isToolEvent =
      kind.includes("tool_call") ||
      kind.startsWith("tool.") ||
      kind.includes(".tool.");
    const toolId = isToolEvent
      ? stringField(payload, "id", "tool_call_id", "call_id", "effect_id")
      : "";
    if (!toolId) {
      merged.push(step);
      continue;
    }
    const existingIndex = toolIndex.get(toolId);
    if (existingIndex === undefined) {
      toolIndex.set(toolId, merged.length);
      merged.push(step);
      continue;
    }
    const started = merged[existingIndex];
    merged[existingIndex] = {
      ...started,
      title: step.title || started.title,
      status: step.status,
      detail: step.detail || started.detail,
      durationMs: step.durationMs ?? started.durationMs,
      diagnosis: step.diagnosis ?? started.diagnosis,
      debug: {
        ...step.debug,
        started_event: started.debug,
      },
    };
  }
  return merged;
}

export function buildProcessTrace(preview: ChatTurnEventPreview | null) {
  const events = (preview?.events || []).filter((event) => {
    const presentation = event.payload?.presentation;
    if (!presentation || typeof presentation !== "object") return true;
    const record = presentation as Record<string, unknown>;
    return (
      String(record.visibility || "") !== "hidden" &&
      String(record.surface || "") !== "debug"
    );
  });
  if (!events.length) {
    return {
      summary: preview?.summary_line || "",
      chips: [] as string[],
      steps: [] as TraceStep[],
      rawSteps: [] as TraceStep[],
    };
  }
  const rawSteps = events.map(buildTraceStep);
  const steps = mergeToolLifecycleSteps(rawSteps);
  const phaseCounts = steps.reduce(
    (acc, step) => {
      acc[step.phase] = (acc[step.phase] || 0) + 1;
      return acc;
    },
    {} as Partial<Record<TracePhase, number>>,
  );

  const chips = [
    phaseCounts.dependency ? `依赖 ${phaseCounts.dependency}` : "",
    phaseCounts.test ? `测试 ${phaseCounts.test}` : "",
    phaseCounts.lint ? `检查 ${phaseCounts.lint}` : "",
    phaseCounts.build ? `构建 ${phaseCounts.build}` : "",
    phaseCounts.preview ? `预览 ${phaseCounts.preview}` : "",
    phaseCounts.deploy ? `部署 ${phaseCounts.deploy}` : "",
    phaseCounts.search ? `搜索 ${phaseCounts.search}` : "",
    phaseCounts.command ? `命令 ${phaseCounts.command}` : "",
    phaseCounts.file ? `文件 ${phaseCounts.file}` : "",
    phaseCounts.approval ? `审批 ${phaseCounts.approval}` : "",
    phaseCounts.subtask ? `子任务 ${phaseCounts.subtask}` : "",
    phaseCounts.checkpoint ? `检查点 ${phaseCounts.checkpoint}` : "",
    phaseCounts.failure ? `失败 ${phaseCounts.failure}` : "",
  ].filter(Boolean);
  if (!chips.length) {
    const toolCount = phaseCounts.tool || 0;
    if (toolCount) chips.push(`工具 ${toolCount}`);
  }
  if (!chips.length && phaseCounts.request) {
    chips.push(`用户请求 ${phaseCounts.request}`);
  }
  return {
    summary: chips.length ? chips.join(" / ") : preview?.summary_line || "",
    chips,
    steps,
    rawSteps,
  };
}

export default function TurnEventPreview({
  variant = "header",
}: TurnEventPreviewProps) {
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  const [expanded, setExpanded] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [runningAction, setRunningAction] = useState<
    "stop" | "retry" | "continue" | null
  >(null);
  const [inspectingStepId, setInspectingStepId] = useState<string | null>(null);
  const [keepAwakeActive, setKeepAwakeActive] = useState<boolean | null>(null);
  const [stableAuthoredPresence, setStableAuthoredPresence] =
    useState<StableAuthoredPresence | null>(null);
  const [stableStructuralPresence, setStableStructuralPresence] =
    useState<StableStructuralPresence | null>(null);
  const presenceStatusId = useId();
  const { message } = useAppMessage();

  const currentSession = useMemo(
    () =>
      sessions.find((session) => session.id === currentSessionId) as
        | ExtendedSession
        | undefined,
    [currentSessionId, sessions],
  );

  const runtimeState = currentSession?.runtimeState ?? null;
  const currentClientTurnId = useMemo(
    () => latestUserClientTurnId(currentSession?.messages),
    [currentSession?.messages],
  );
  const terminalAssistantStatus = useMemo(
    () => latestUserTerminalAssistantStatus(currentSession?.messages),
    [currentSession?.messages],
  );
  const priorClientTurnId = useMemo(
    () => previousUserClientTurnId(currentSession?.messages),
    [currentSession?.messages],
  );
  const latestUserStartedAtMs = useMemo(
    () => latestUserMessageTimeMs(currentSession?.messages),
    [currentSession?.messages],
  );
  const sourcePreviewEvents = useMemo(
    () =>
      runtimeState?.events_preview?.length
        ? runtimeState.events_preview
        : currentSession?.turnEventPreview?.events || [],
    [currentSession?.turnEventPreview?.events, runtimeState?.events_preview],
  );
  const localSendPending = Boolean(
    currentSession?.generating &&
    currentSession.messages?.slice(-1).some(
      (row) =>
        row.role === "assistant" &&
        row.cards?.some((card) => {
          if (card.code !== RESPONSE_CARD_CODE) return false;
          const data = objectField(card.data);
          const metadata = objectField(data.metadata);
          // Old live/cache projections can still carry the local marker.
          // Backend identity or output takes precedence over that receipt.
          return (
            metadata.optimistic_pending === true &&
            ["created", "in_progress", "running"].includes(
              String(data.status || ""),
            ) &&
            (!Array.isArray(data.output) || data.output.length === 0) &&
            !String(metadata.run_id || metadata.attempt_id || "").trim()
          );
        }),
    ),
  );
  const provisionalUnscopedUserTurn = useMemo(() => {
    if (localSendPending) return true;
    if (terminalAssistantStatus) return false;
    if (latestUserStartedAtMs <= 0) return false;
    const runId = runtimeState?.run_id?.trim() || "";
    const latestTurnStartAtMs = latestDirectTurnStartTimeMs(
      sourcePreviewEvents,
      runId,
    );
    const postUserClientTurnId = latestDirectClientTurnIdAfter(
      sourcePreviewEvents,
      runId,
      latestUserStartedAtMs,
    );
    if (currentClientTurnId) {
      const runtimeClientTurnId = String(
        runtimeState?.client_turn_id || "",
      ).trim();
      if (runtimeClientTurnId === currentClientTurnId) return false;
      if (postUserClientTurnId === currentClientTurnId) return false;
      if (latestTurnStartAtMs >= latestUserStartedAtMs) return false;
      return true;
    }
    if (latestTurnStartAtMs >= latestUserStartedAtMs) return false;
    if (
      postUserClientTurnId &&
      (!priorClientTurnId || postUserClientTurnId !== priorClientTurnId)
    ) {
      return false;
    }
    if (latestTurnStartAtMs > 0) {
      return latestUserStartedAtMs > latestTurnStartAtMs;
    }
    const runtimeStartedAtMs = Date.parse(runtimeState?.started_at || "") || 0;
    if (runtimeStartedAtMs > 0) {
      return latestUserStartedAtMs > runtimeStartedAtMs;
    }
    const latestEventAtMs = sourcePreviewEvents.reduce(
      (latest, event) => Math.max(latest, previewEventTimeMs(event)),
      0,
    );
    return latestEventAtMs > 0 && latestUserStartedAtMs > latestEventAtMs;
  }, [
    localSendPending,
    currentClientTurnId,
    latestUserStartedAtMs,
    priorClientTurnId,
    runtimeState?.run_id,
    runtimeState?.client_turn_id,
    runtimeState?.started_at,
    sourcePreviewEvents,
    terminalAssistantStatus,
  ]);

  const preview = useMemo(() => {
    const currentRunId = runtimeState?.run_id?.trim() || "";
    const runtimeEvents = provisionalUnscopedUserTurn
      ? []
      : previewEventsForRun(
          runtimeState?.events_preview ?? [],
          currentRunId,
          currentClientTurnId,
          latestUserStartedAtMs,
        );
    if (
      runtimeState &&
      (provisionalUnscopedUserTurn ||
        runtimeEvents.length > 0 ||
        !["", "idle"].includes(String(runtimeState.status || "").toLowerCase()))
    ) {
      return {
        workspace_dir: runtimeState.workspace_dir,
        summary_line:
          (provisionalUnscopedUserTurn
            ? "正在理解你的要求"
            : runtimeState.summary?.trim()) ||
          runtimeStatusLabel(
            provisionalUnscopedUserTurn ? "running" : runtimeState.status,
          ) ||
          "本轮运行事件",
        events: runtimeEvents,
      } satisfies ChatTurnEventPreview;
    }
    const current = sessions.find(
      (session) => session.id === currentSessionId,
    ) as ExtendedSession | undefined;
    const legacyPreview = current?.turnEventPreview ?? null;
    if (legacyPreview?.events?.length) {
      return {
        ...legacyPreview,
        summary_line: provisionalUnscopedUserTurn
          ? "正在理解你的要求"
          : legacyPreview.summary_line?.trim() || "本轮运行事件",
        events: provisionalUnscopedUserTurn
          ? []
          : previewEventsForRun(
              legacyPreview.events,
              currentRunId,
              currentClientTurnId,
              latestUserStartedAtMs,
            ),
      } satisfies ChatTurnEventPreview;
    }
    if (provisionalUnscopedUserTurn) {
      return {
        workspace_dir: runtimeState?.workspace_dir,
        summary_line: "正在理解你的要求",
        events: [],
      } satisfies ChatTurnEventPreview;
    }
    return legacyPreview;
  }, [
    currentClientTurnId,
    currentSessionId,
    latestUserStartedAtMs,
    provisionalUnscopedUserTurn,
    runtimeState,
    sessions,
  ]);

  const processTrace = useMemo(() => buildProcessTrace(preview), [preview]);
  const outcomeStatus = useMemo(
    () =>
      resolveOutcomeStatus(
        provisionalUnscopedUserTurn ? "unknown" : runtimeState?.outcome_status,
        processTrace.steps,
        runtimeState?.run_id?.trim() || "",
      ),
    [
      processTrace.steps,
      provisionalUnscopedUserTurn,
      runtimeState?.outcome_status,
      runtimeState?.run_id,
    ],
  );
  const executionStatus = useMemo(() => {
    const runtimeRunId = runtimeState?.run_id?.trim() || "";
    const runtimeClientTurnId = String(
      runtimeState?.client_turn_id || "",
    ).trim();
    const runtimeOwnsCurrentTurn = Boolean(
      runtimeRunId &&
      (!currentClientTurnId ||
        !runtimeClientTurnId ||
        runtimeClientTurnId === currentClientTurnId),
    );
    const runtimeExecutionStatus = resolveExecutionStatus(
      provisionalUnscopedUserTurn ? "running" : runtimeState?.status,
      outcomeStatus,
      processTrace.steps,
      runtimeRunId,
    );
    return runtimeOwnsCurrentTurn
      ? runtimeExecutionStatus
      : terminalAssistantStatus || runtimeExecutionStatus;
  }, [
    currentClientTurnId,
    outcomeStatus,
    processTrace.steps,
    provisionalUnscopedUserTurn,
    runtimeState?.client_turn_id,
    runtimeState?.run_id,
    runtimeState?.status,
    terminalAssistantStatus,
  ]);
  const executionStages = useMemo(
    () => buildExecutionStages(processTrace.steps, executionStatus),
    [executionStatus, processTrace.steps],
  );
  const workPhaseSummary = useMemo(
    () => buildWorkPhaseSummary(executionStages),
    [executionStages],
  );
  const presenceRunId = useMemo(() => {
    const explicitRunId = runtimeState?.run_id?.trim();
    if (explicitRunId) return explicitRunId;
    for (
      let index = (preview?.events?.length || 0) - 1;
      index >= 0;
      index -= 1
    ) {
      const eventRunId = previewEventRunId(preview!.events[index]);
      if (eventRunId) return eventRunId;
    }
    return "";
  }, [preview, runtimeState?.run_id]);
  const presenceClientTurnId = useMemo(() => {
    if (currentClientTurnId) return currentClientTurnId;
    if (provisionalUnscopedUserTurn) return "";
    for (
      let index = (preview?.events?.length || 0) - 1;
      index >= 0;
      index -= 1
    ) {
      const eventClientTurnId = previewEventClientTurnId(
        preview!.events[index],
      );
      if (eventClientTurnId) return eventClientTurnId;
    }
    return "";
  }, [currentClientTurnId, preview, provisionalUnscopedUserTurn]);
  const presenceRunKey = provisionalUnscopedUserTurn
    ? `${presenceRunId || currentSessionId || "legacy-run"}::pending-user:${latestUserStartedAtMs}`
    : [presenceRunId, presenceClientTurnId].filter(Boolean).join("::") ||
      runtimeState?.started_at?.trim() ||
      currentSessionId ||
      "legacy-run";
  const progressHistory = useMemo(
    () =>
      publicProgressHistory(
        currentSession?.messages,
        presenceRunId,
        presenceClientTurnId,
      ),
    [currentSession?.messages, presenceRunId, presenceClientTurnId],
  );
  const authoredPresenceCandidate = useMemo(
    () =>
      latestAuthoredPresence(
        currentSession?.messages,
        presenceRunId,
        presenceClientTurnId,
      ),
    [currentSession?.messages, presenceClientTurnId, presenceRunId],
  );

  const authoredPhaseEnded =
    !authoredPresenceCandidate &&
    Boolean(
      latestAuthoredPresence(
        currentSession?.messages,
        presenceRunId,
        presenceClientTurnId,
        true,
      ),
    );
  useEffect(() => {
    setStableAuthoredPresence((previous) => {
      if (executionStatus !== "running" || authoredPhaseEnded) return null;
      if (!authoredPresenceCandidate) {
        return previous?.runKey === presenceRunKey ? previous : null;
      }
      if (
        previous?.runKey === presenceRunKey &&
        previous.id === authoredPresenceCandidate.id &&
        previous.revision > authoredPresenceCandidate.revision
      )
        return previous;
      if (
        previous?.runKey === presenceRunKey &&
        previous.id === authoredPresenceCandidate.id &&
        previous.revision === authoredPresenceCandidate.revision
      ) {
        if (authoredPresenceCandidate.text === previous.text) return previous;
        // A streamed public-progress sentence commonly grows while retaining
        // one id/revision. Advance only for a cumulative extension; keep the
        // longer stable copy when a transient render exposes a shorter prefix.
        if (authoredPresenceCandidate.text.startsWith(previous.text)) {
          return {
            ...authoredPresenceCandidate,
            runKey: presenceRunKey,
          };
        }
        return previous;
      }
      return {
        ...authoredPresenceCandidate,
        runKey: presenceRunKey,
      };
    });
  }, [
    authoredPresenceCandidate,
    authoredPhaseEnded,
    executionStatus,
    presenceRunKey,
  ]);
  const latestDiagnosis = useMemo(() => {
    const diagnoses = executionStages
      .map((step) => step.diagnosis)
      .filter(Boolean);
    return diagnoses[diagnoses.length - 1] ?? null;
  }, [executionStages]);
  const scopedRuntimeState = useMemo(() => {
    if (!runtimeState) return null;
    const userStartedAt = latestUserStartedAtMs;
    const runtimeStartedAt = Date.parse(runtimeState.started_at || "") || 0;
    const runtimeUpdatedAt = Date.parse(runtimeState.updated_at || "") || 0;
    const scopedStartedAt = Math.max(runtimeStartedAt, userStartedAt);
    const scopedUpdatedAt = Math.max(runtimeUpdatedAt, userStartedAt);
    return {
      ...runtimeState,
      status: provisionalUnscopedUserTurn ? "running" : runtimeState.status,
      outcome_status: provisionalUnscopedUserTurn
        ? "unknown"
        : runtimeState.outcome_status,
      delivery_status: provisionalUnscopedUserTurn
        ? "pending"
        : runtimeState.delivery_status,
      started_at: scopedStartedAt
        ? new Date(scopedStartedAt).toISOString()
        : runtimeState.started_at,
      updated_at: scopedUpdatedAt
        ? new Date(scopedUpdatedAt).toISOString()
        : runtimeState.updated_at,
      // `preview` is already filtered to the active run/client turn. Feeding
      // the original event collection back into the stall helper would let a
      // previous queued turn leak its stage and timestamp into this one.
      events_preview: preview?.events || [],
    } satisfies ChatRuntimeState;
  }, [
    latestUserStartedAtMs,
    preview?.events,
    provisionalUnscopedUserTurn,
    runtimeState,
  ]);
  const stallState = useMemo(
    () => getRuntimeStallState(scopedRuntimeState, null, nowMs),
    [nowMs, scopedRuntimeState],
  );

  useEffect(() => {
    setNowMs(Date.now());
    if (!["running", "stopping"].includes(executionStatus)) return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [executionStatus, presenceRunKey]);

  const agentName = workbenchAgentName(currentSession);
  const runningStage = executionStages.find((stage) =>
    isRunningTraceStatus(stage.status),
  );
  const activeStage =
    runningStage || executionStages[executionStages.length - 1];
  const activeStageDisplayTitle =
    executionStatus === "running"
      ? activeStage
        ? friendlyStageTitle(activeStage)
        : undefined
      : activeStage?.title;
  const authoredPresenceForRender =
    stableAuthoredPresence?.runKey === presenceRunKey
      ? stableAuthoredPresence
      : authoredPresenceCandidate;
  const runningStepToolCallId = traceStepToolCallId(runningStage);
  const authoredPresenceIsCurrent = Boolean(
    !authoredPhaseEnded &&
    authoredPresenceForRender &&
    (authoredPresenceForRender.toolCallIds.length === 0
      ? executionStatus === "running"
      : Boolean(
          runningStage &&
          authoredPresenceForRender.toolCallIds.includes(runningStepToolCallId),
        )),
  );
  const candidatePresencePresentation = localSendPending
    ? {
        stage: "正在发送",
        text: "正在发送",
        meta: "",
        tone: "quiet" as const,
        authored: false,
      }
    : buildPresencePresentation({
        status: executionStatus,
        stageTitle:
          activeStageDisplayTitle || preview?.summary_line || "处理当前任务",
        stageLabel: presenceStageLabel(runningStage),
        activeStageRunning: Boolean(runningStage),
        authoredText: authoredPresenceIsCurrent
          ? authoredPresenceForRender?.text
          : "",
        structuredText: presenceStructuredDetail(activeStage),
        stall: stallState,
      });

  useEffect(() => {
    setStableStructuralPresence((previous) => {
      if (executionStatus !== "running") return null;
      if (
        !candidatePresencePresentation ||
        candidatePresencePresentation.stage === "等待回应" ||
        !["active", "quiet"].includes(candidatePresencePresentation.tone)
      ) {
        return previous?.runKey === presenceRunKey ? previous : null;
      }
      if (
        previous?.runKey === presenceRunKey &&
        previous.stage === candidatePresencePresentation.stage
      ) {
        return previous;
      }
      return {
        runKey: presenceRunKey,
        stage: candidatePresencePresentation.stage,
      };
    });
  }, [candidatePresencePresentation, executionStatus, presenceRunKey]);

  const presencePresentation =
    candidatePresencePresentation &&
    executionStatus === "running" &&
    candidatePresencePresentation.stage === "等待回应" &&
    stableStructuralPresence?.runKey === presenceRunKey
      ? {
          ...candidatePresencePresentation,
          stage: stableStructuralPresence.stage,
        }
      : candidatePresencePresentation;

  useEffect(() => {
    if (
      executionStatus !== "running" ||
      !window.electronAPI?.getSystemBehavior
    ) {
      setKeepAwakeActive(null);
      return;
    }
    let cancelled = false;
    let timer: number | null = null;
    const refresh = async () => {
      try {
        const response = await window.electronAPI?.getSystemBehavior?.();
        if (!cancelled) {
          setKeepAwakeActive(Boolean(response?.preferences?.keep_awake_active));
        }
      } catch {
        if (!cancelled) setKeepAwakeActive(null);
      }
      if (!cancelled) {
        timer = window.setTimeout(() => void refresh(), 15_000);
      }
    };
    void refresh();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [executionStatus]);

  if (!preview || !Array.isArray(preview.events)) {
    return null;
  }

  const runId = runtimeState?.run_id?.trim() || "";
  const runtimeStatus = executionStatus;
  const backendMarkedStalled = runtimeStatus === "stalled";
  const incompleteResponse = runtimeStatus === "incomplete";
  const workspaceName = workspaceLabel(preview.workspace_dir);
  const executionIsRunning = ["running", "stopping"].includes(runtimeStatus);
  const executionElapsed = executionElapsedLabel(
    scopedRuntimeState,
    processTrace.steps,
    nowMs,
    executionIsRunning,
    runId,
  );
  const canStop = Boolean(
    !provisionalUnscopedUserTurn && runtimeState?.can_stop && runId,
  );
  const canRetry = Boolean(
    runId &&
    ["completed", "finished", "blocked", "failed", "interrupted"].includes(
      runtimeStatus,
    ),
  );
  const canContinue =
    Boolean(runId) &&
    (incompleteResponse ||
      backendMarkedStalled ||
      (stallState.isRunning && stallState.level === "stalled"));

  const applyOptimisticRuntimeState = (
    next: Partial<ChatRuntimeState>,
    nextSummaryLine?: string,
  ) => {
    if (!currentSessionId || !runtimeState) return;
    const now = new Date().toISOString();
    const nextRuntimeState: ChatRuntimeState = {
      ...runtimeState,
      ...next,
      updated_at: now,
      summary: next.summary ?? runtimeState.summary,
      events_preview:
        next.events_preview ?? runtimeState.events_preview ?? preview.events,
    };
    void sessionApi.updateSession({
      id: currentSessionId,
      generating: ["running", "stopping"].includes(nextRuntimeState.status),
      runtimeState: nextRuntimeState,
      turnEventPreview: nextSummaryLine
        ? {
            workspace_dir: preview.workspace_dir,
            summary_line: nextSummaryLine,
            events: nextRuntimeState.events_preview ?? [],
          }
        : undefined,
    } as any);
  };

  const handleStop = async () => {
    if (!runId || !currentSessionId || runningAction) return;
    setRunningAction("stop");
    try {
      const response = await runtimeApi.interruptRun(runId);
      if (!response.stopped) {
        message.warning("当前没有可停止的运行。");
        return;
      }
      const stopEvent = {
        id: `runtime-stop-${Date.now()}`,
        ts: new Date().toISOString(),
        kind: "chat.turn.stop_requested",
        status: "info",
        label: "请求停止",
        detail: "已请求停止当前运行",
      };
      applyOptimisticRuntimeState(
        {
          status: "stopping",
          can_stop: false,
          can_reconnect: true,
          summary: "本轮已请求停止",
          events_preview: [...preview.events, stopEvent],
        },
        "本轮已请求停止",
      );
      message.success("正在停止，请稍等。");
    } catch (error) {
      console.error("Failed to stop runtime run:", error);
      message.error("停止当前运行失败。");
    } finally {
      setRunningAction(null);
    }
  };

  const handleRetry = async () => {
    if (!runId || !currentSessionId || runningAction) return;
    setRunningAction("retry");
    try {
      const response = await runtimeApi.retryRun(runId);
      const retryText = buildRetryableText(response);
      if (!retryText) {
        message.warning("这一轮暂时没有可回填的重试文本。");
        return;
      }
      const textarea = document.querySelector(
        '[class*="sender"] textarea',
      ) as HTMLTextAreaElement | null;
      if (!textarea) {
        message.warning("没有找到当前输入框，请回到聊天输入区后再试。");
        return;
      }
      setTextareaValue(textarea, retryText);
      textarea.focus();
      sessionApi.saveDraftText(currentSessionId, retryText);
      message.success("已把重试文本放回输入框，确认后重新发送。");
    } catch (error) {
      console.error("Failed to prepare retry text:", error);
      message.error("获取重试文本失败。");
    } finally {
      setRunningAction(null);
    }
  };

  const handleContinueFromStall = async () => {
    if (!runId || !currentSessionId || runningAction) return;
    setRunningAction("continue");
    try {
      const latestSeq =
        stallState.lastEvent?.seq ??
        Math.max(0, ...preview.events.map((event) => event.seq ?? 0));
      const response = await runtimeApi.continueRun(runId, {
        chat_id: currentSessionId,
        session_id: currentSession?.sessionId ?? currentSessionId,
        after_seq: latestSeq,
        stage_label: incompleteResponse ? "继续回答" : stallState.stageLabel,
        workspace_dir: preview.workspace_dir,
      });
      if (response.reconnected) {
        applyOptimisticRuntimeState(
          {
            status: "running",
            can_reconnect: true,
            can_stop: runtimeState?.can_stop ?? true,
            summary: "已重新接上运行",
          },
          "已重新接上运行",
        );
        message.success("后端任务仍在运行，已重新接上运行态。");
        return;
      }

      if (response.reason === "ambiguous_effect_requires_resolution") {
        message.warning(
          "工具调用的执行状态尚未确认，请先确认该副作用的实际结果后再续跑。",
        );
        return;
      }

      const textarea = document.querySelector(
        '[class*="sender"] textarea',
      ) as HTMLTextAreaElement | null;
      if (!textarea) {
        message.warning("没有找到当前输入框，请回到聊天输入区后再试。");
        return;
      }
      const continuationText =
        buildRetryableText(response) ||
        (incompleteResponse
          ? buildIncompleteResponseContinuationPrompt({
              runId,
              workspaceName,
              workspaceDir: preview.workspace_dir,
            })
          : buildRuntimeContinuationPrompt({
              stall: stallState,
              runId,
              workspaceName,
              workspaceDir: preview.workspace_dir,
            }));
      setTextareaValue(textarea, continuationText);
      textarea.focus();
      sessionApi.saveDraftText(currentSessionId, continuationText);
      message.success(
        incompleteResponse
          ? "已根据原始请求生成继续提示，确认后发送。"
          : "已根据后端运行状态生成续跑提示，确认后发送。",
      );
    } catch (error) {
      console.error("Failed to prepare continuation:", error);
      message.error("准备续跑失败。");
    } finally {
      setRunningAction(null);
    }
  };

  const handleCopyDebug = async (step: TraceStep) => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(step.debug, null, 2));
      message.success("已复制调试信息");
    } catch {
      message.error("复制调试信息失败。");
    }
  };

  const handleRepairFromDiagnosis = (
    diagnosis: RuntimeFailureDiagnosis,
    step?: TraceStep,
  ) => {
    const textarea = document.querySelector(
      '[class*="sender"] textarea',
    ) as HTMLTextAreaElement | null;
    if (!textarea) {
      message.warning("没有找到当前输入框，请回到聊天输入区后再试。");
      return;
    }

    const diagnosisStep =
      step ||
      [...processTrace.steps]
        .reverse()
        .find((item) => item.diagnosis === diagnosis);
    const stepPayload =
      diagnosisStep?.debug.payload &&
      typeof diagnosisStep.debug.payload === "object"
        ? (diagnosisStep.debug.payload as Record<string, unknown>)
        : null;
    const repairText = buildRuntimeRepairPrompt({
      diagnosis,
      runId,
      stageLabel: diagnosisStep?.title || stallState.stageLabel,
      eventDetail: diagnosisStep?.detail,
      workspaceDir: preview.workspace_dir,
      previewHarness: previewHarnessReportFromPayload(stepPayload),
    });
    setTextareaValue(textarea, repairText);
    textarea.focus();
    if (currentSessionId) {
      sessionApi.saveDraftText(currentSessionId, repairText);
    }
    message.success("已生成修复提示，确认后发送。");
  };

  const previewUrlFromStep = (step: TraceStep): string => {
    const payload =
      step.debug.payload && typeof step.debug.payload === "object"
        ? (step.debug.payload as Record<string, unknown>)
        : {};
    return (
      extractUrls(
        step.detail,
        payload.preview_url,
        payload.command,
        payload.trace_detail,
      ).find(isLocalPreviewUrl) || ""
    );
  };

  const handleInspectPreview = async (step: TraceStep) => {
    const url = previewUrlFromStep(step);
    if (!url) {
      message.warning("这个预览事件里没有可诊断的本地 URL。");
      return;
    }
    setInspectingStepId(step.id);
    try {
      const report = await runtimeApi.inspectPreview({
        url,
        run_id: runId || undefined,
        chat_id: currentSessionId,
        session_id: currentSession?.sessionId ?? currentSessionId,
        workspace_dir: preview.workspace_dir,
        stage_label: step.title,
        record_event: Boolean(currentSessionId),
      });
      if (report.ok) {
        message.success("预览诊断通过。");
      } else {
        message.warning(report.summary || "预览诊断发现问题。");
      }
    } catch (error) {
      console.error("Failed to inspect preview:", error);
      message.error("预览诊断失败。");
    } finally {
      setInspectingStepId(null);
    }
  };

  const hasRuntimeActions = canStop || canRetry || canContinue;
  const completedStageCount = executionStages.filter((step) =>
    ["已完成", "已结束", "已恢复"].includes(traceStatusLabel(step.status)),
  ).length;
  const diagnosedIssueCount = executionStages.filter(
    (step) => step.diagnosis || isFailureTraceStatus(step.status),
  ).length;
  const artifactCount = executionStages.filter(isDeliveredOutputStage).length;
  const workbenchStatus =
    runtimeStatus === "running" && stallState.level === "stalled"
      ? "stalled"
      : runtimeStatus;
  const workbenchOverview = buildWorkbenchOverview({
    status: workbenchStatus,
    outcomeStatus,
    deliveryStatus: scopedRuntimeState?.delivery_status,
    summary: scopedRuntimeState?.summary || preview.summary_line,
    stages: executionStages,
  });
  const statusLabel = runtimeStatusLabel(runtimeStatus);
  // Move normal completion to the reply. Keep attention/recovery states at
  // the composer, where their explanation and next action remain visible.
  const executionIsSettled = ["completed", "finished"].includes(runtimeStatus);
  const inlineStage = presencePresentation?.stage || statusLabel;
  const inlineDetail = presencePresentation?.text || "查看本轮执行记录";

  if (
    variant === "presence" &&
    (!presencePresentation || (executionIsSettled && !expanded))
  )
    return null;
  if (variant === "inline" && !executionIsSettled) return null;

  return (
    <div
      className={styles.wrapper}
      data-chat-turn-preview="1"
      data-variant={variant}
    >
      {variant === "presence" && executionIsSettled ? null : variant ===
        "inline" ? (
        <>
          <button
            type="button"
            className={styles.inlineProgressButton}
            data-testid="inline-turn-progress"
            data-tone={presencePresentation?.tone}
            data-open={expanded ? "1" : "0"}
            onClick={() => setExpanded(true)}
            aria-label={`查看${agentName}的本轮工作过程`}
            aria-describedby={presenceStatusId}
            aria-expanded={expanded}
            aria-busy={executionIsRunning}
          >
            <span className={styles.inlineFlow} aria-hidden="true">
              <i />
            </span>
            <span className={styles.inlineProgressCopy}>
              <strong>{inlineStage}</strong>
            </span>
            {executionElapsed ? (
              <span className={styles.inlineProgressMeta}>
                {`用时 ${executionElapsed}`}
              </span>
            ) : null}
            <span className={styles.inlineProgressAction}>查看过程</span>
            <ChevronRight
              className={styles.inlineProgressChevron}
              size={15}
              aria-hidden="true"
            />
          </button>
          <span
            id={presenceStatusId}
            className={styles.presenceLiveStatus}
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            {`${agentName}：${inlineStage}；${inlineDetail}`}
          </span>
        </>
      ) : variant === "presence" && presencePresentation ? (
        <>
          <button
            type="button"
            className={styles.presenceButton}
            data-tone={presencePresentation.tone}
            data-authored={presencePresentation.authored ? "1" : "0"}
            data-running={executionIsRunning ? "1" : "0"}
            data-open={expanded ? "1" : "0"}
            onClick={() => setExpanded(true)}
            aria-label={`打开${agentName}的本轮工作过程`}
            aria-describedby={presenceStatusId}
            aria-expanded={expanded}
            aria-busy={executionIsRunning}
          >
            <span className={styles.presenceIndicator} aria-hidden="true">
              <span className={styles.presenceSignal} />
            </span>
            <span className={styles.presenceCopy}>
              <span className={styles.presencePrimary}>
                <span className={styles.presenceMessage}>
                  {Array.from(presencePresentation.text).length > 72
                    ? presencePresentation.stage
                    : presencePresentation.text || presencePresentation.stage}
                </span>
              </span>
              {presencePresentation.meta ? (
                <small className={styles.presenceMeta}>
                  {presencePresentation.meta}
                </small>
              ) : null}
            </span>
            <span className={styles.presenceProcess} aria-hidden="true">
              查看过程
            </span>
            <ChevronRight
              className={styles.presenceChevron}
              size={14}
              aria-hidden="true"
            />
          </button>
          <span
            id={presenceStatusId}
            className={styles.presenceLiveStatus}
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            {presenceLiveAnnouncement(agentName, presencePresentation)}
          </span>
        </>
      ) : (
        <Tooltip title={`本轮执行：${statusLabel}`}>
          <button
            type="button"
            className={
              variant === "strip"
                ? styles.runtimeStripButton
                : styles.traceToggleButton
            }
            data-running={
              executionIsRunning ||
              stallState.isRunning ||
              backendMarkedStalled ||
              canStop
                ? "1"
                : "0"
            }
            data-open={expanded ? "1" : "0"}
            onClick={() => setExpanded(true)}
            aria-label={`打开本轮执行，${statusLabel}，${executionStages.length} 个阶段`}
            aria-expanded={expanded}
          >
            {variant === "strip" ? (
              <>
                <span
                  className={`${styles.statusDot} ${
                    styles[
                      `status${
                        runtimeStatus.charAt(0).toUpperCase() +
                        runtimeStatus.slice(1)
                      }` as keyof typeof styles
                    ] || styles.statusIdle
                  }`}
                  aria-hidden="true"
                />
                <span className={styles.runtimeStripStatus}>{statusLabel}</span>
                <strong className={styles.runtimeStripStage}>
                  {activeStageDisplayTitle || preview.summary_line}
                </strong>
                <span className={styles.runtimeStripMeta}>
                  {executionElapsed ? `${executionElapsed} · ` : ""}
                  {completedStageCount}/{executionStages.length || 1} 阶段
                  {keepAwakeActive ? " · 保持唤醒" : ""}
                </span>
                <GitBranch size={14} aria-hidden="true" />
              </>
            ) : (
              <svg
                className={styles.statusOrb}
                data-status={runtimeStatus}
                data-spinning={executionIsRunning ? "1" : "0"}
                data-testid="execution-status-orb"
                viewBox="0 0 20 20"
                aria-hidden="true"
              >
                <g className={styles.statusOrbDots}>
                  <circle cx="10" cy="2.8" r="1" />
                  <circle cx="15.1" cy="4.9" r="1" />
                  <circle cx="17.2" cy="10" r="1" />
                  <circle cx="15.1" cy="15.1" r="1" />
                  <circle cx="10" cy="17.2" r="1" />
                  <circle cx="4.9" cy="15.1" r="1" />
                  <circle cx="2.8" cy="10" r="1" />
                  <circle cx="4.9" cy="4.9" r="1" />
                </g>
              </svg>
            )}
          </button>
        </Tooltip>
      )}
      {variant === "strip" && canStop ? (
        <Tooltip title="停止当前运行">
          <button
            type="button"
            className={styles.runtimeStripStop}
            aria-label="停止当前运行"
            disabled={Boolean(runningAction)}
            onClick={() => void handleStop()}
          >
            <Square size={12} fill="currentColor" aria-hidden="true" />
          </button>
        </Tooltip>
      ) : null}

      <Drawer
        open={expanded}
        onClose={() => setExpanded(false)}
        placement="right"
        width={420}
        mask
        maskClosable
        keyboard
        closable={false}
        aria-label={`${agentName}的本轮工作`}
        destroyOnHidden
        title={null}
        rootClassName={styles.traceDrawerRoot}
        className={styles.traceDrawer}
        styles={{
          body: { padding: 0 },
          header: { display: "none" },
          mask: { background: "transparent" },
        }}
      >
        <div className={styles.drawerShell}>
          <div className={styles.drawerHeader}>
            <div className={styles.workbenchIdentity}>
              <span className={styles.workbenchAvatar} aria-hidden="true">
                <img src="./lantai.png" alt="" />
                <span data-status={runtimeStatus} />
              </span>
              <div className={styles.workbenchIdentityCopy}>
                <div className={styles.drawerTitleRow}>
                  <strong>{agentName} · 本轮工作</strong>
                  <span
                    className={styles.workbenchStatus}
                    data-status={runtimeStatus}
                  >
                    {statusLabel}
                  </span>
                </div>
                <span className={styles.workbenchNow}>
                  {presencePresentation?.text ||
                    singleLineExcerpt(
                      runtimeState?.summary || preview.summary_line,
                      84,
                    ) ||
                    executionHeadline(runtimeStatus)}
                </span>
              </div>
            </div>
            <div className={styles.drawerHeaderActions}>
              <Tooltip title="关闭工作台">
                <Button
                  type="text"
                  size="small"
                  className={styles.drawerCloseButton}
                  aria-label="关闭工作台"
                  icon={<X size={15} aria-hidden="true" />}
                  onClick={() => setExpanded(false)}
                />
              </Tooltip>
            </div>
          </div>
          <div className={styles.drawerBody}>
            {executionElapsed ? (
              <p className={styles.workbenchElapsed}>
                {executionIsRunning ? "已运行" : "用时"} {executionElapsed}
              </p>
            ) : null}
            {progressHistory.length > 0 ? (
              <section
                className={styles.publicProgressHistory}
                aria-label="公开进展"
              >
                <h3>进展记录</h3>
                {progressHistory.map((item) => (
                  <p key={item.id}>{item.text}</p>
                ))}
              </section>
            ) : null}
            <section
              className={styles.workbenchOverview}
              data-tone={workbenchOverview.tone}
              aria-live="polite"
            >
              <span className={styles.workbenchOverviewLabel}>
                {workbenchOverview.label}
              </span>
              <strong>{workbenchOverview.headline}</strong>
              <p>{workbenchOverview.detail}</p>
              {workbenchOverview.results.length ? (
                <div
                  className={styles.workbenchResults}
                  aria-label="本轮交付内容"
                >
                  {workbenchOverview.results.map((result, index) => (
                    <div key={`${result.title}-${index}`}>
                      <CheckCircle2 size={14} aria-hidden="true" />
                      <span>
                        <strong>{result.title}</strong>
                        <small>{result.detail}</small>
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
              <div className={styles.workbenchNextStep}>
                <span>下一步</span>
                <strong>{workbenchOverview.nextStep}</strong>
              </div>
              <span className={styles.workbenchFacts}>
                <span>{completedStageCount} 项已完成</span>
                {artifactCount > 0 ? <span>{artifactCount} 项交付</span> : null}
                {diagnosedIssueCount > 0 ? (
                  <span>{diagnosedIssueCount} 个问题待处理</span>
                ) : null}
              </span>
            </section>
            {executionIsRunning ||
            !["completed", "finished"].includes(runtimeStatus) ? (
              <section
                className={styles.workbenchProgress}
                data-status={runtimeStatus}
                aria-busy={executionIsRunning}
              >
                <span className={styles.workbenchProgressCopy}>
                  <strong>
                    {executionIsRunning && stallState.level === "stale"
                      ? "暂无新进展"
                      : executionHeadline(runtimeStatus)}
                  </strong>
                </span>
              </section>
            ) : null}
            {hasRuntimeActions ? (
              <div className={styles.workbenchActions}>
                {canContinue ? (
                  <Button
                    size="small"
                    aria-label={incompleteResponse ? "继续回答" : "继续处理"}
                    onClick={() => void handleContinueFromStall()}
                    loading={runningAction === "continue"}
                  >
                    {incompleteResponse ? "继续回答" : "继续处理"}
                  </Button>
                ) : null}
                {canStop ? (
                  <Button
                    size="small"
                    aria-label="停止"
                    onClick={handleStop}
                    loading={runningAction === "stop"}
                  >
                    停止
                  </Button>
                ) : null}
                {canRetry ? (
                  <Button
                    size="small"
                    aria-label="准备重试，回填输入框后由你发送"
                    onClick={handleRetry}
                    loading={runningAction === "retry"}
                  >
                    准备重试
                  </Button>
                ) : null}
                <small className={styles.actionSemantics}>
                  {canContinue
                    ? "继续会先检查原运行：仍在执行则重新连接，已结束则回填续跑提示，不会自动发送。"
                    : canRetry
                      ? "准备重试只会把重试内容放回输入框，检查后由你发送。"
                      : canStop
                        ? "停止会立即请求中断当前运行，已生成内容仍会保留。"
                        : ""}
                </small>
              </div>
            ) : null}
            {executionIsRunning &&
            stallState.level === "stale" &&
            !canContinue ? (
              <div className={styles.processingNotice} aria-live="polite">
                <Loader2
                  size={14}
                  className={styles.processingNoticeSpin}
                  aria-hidden="true"
                />
                <span>
                  <strong>暂时没有新进展</strong>
                  <small>
                    还没收到下一步更新。你可以继续等待，或停止任务。
                  </small>
                </span>
              </div>
            ) : null}
            {canContinue ? (
              <div
                className={`${styles.stallNotice} ${
                  backendMarkedStalled || stallState.level === "stalled"
                    ? styles.stallNoticeStrong
                    : ""
                }`}
                role="alert"
              >
                <AlertTriangle size={14} aria-hidden="true" />
                <span>
                  {incompleteResponse ? (
                    <>
                      本轮已结束，但回答可能不完整。可点击“继续回答”，系统会先依据原始请求判断该做什么。
                    </>
                  ) : (
                    <>
                      {backendMarkedStalled || stallState.level === "stalled"
                        ? "可能卡住"
                        : "暂无更新"}
                      ： 最后停在「{stallState.stageLabel}」，距今{" "}
                      {stallState.elapsedLabel || "一段时间"}
                      。可点击“继续处理”恢复这一轮。
                    </>
                  )}
                </span>
              </div>
            ) : null}
            {latestDiagnosis ? (
              <div className={styles.diagnosisNotice}>
                <AlertCircle size={14} aria-hidden="true" />
                <span>
                  <strong>{latestDiagnosis.summary}</strong>
                  <em>{latestDiagnosis.next_action}</em>
                  {latestDiagnosis.evidence ? (
                    <small>证据：{latestDiagnosis.evidence}</small>
                  ) : null}
                </span>
                <Button
                  size="small"
                  type="text"
                  className={styles.diagnosisNoticeAction}
                  onClick={() => handleRepairFromDiagnosis(latestDiagnosis)}
                >
                  生成修复提示
                </Button>
              </div>
            ) : null}
            <section
              className={styles.phaseOverview}
              aria-label="本轮工作阶段证据"
            >
              {workPhaseSummary.map((phase) => (
                <div
                  key={phase.id}
                  className={styles.phaseOverviewItem}
                  data-status={phase.status}
                >
                  <span aria-hidden="true" />
                  <strong>{phase.label}</strong>
                  <small>
                    {phase.status === "unknown"
                      ? "无记录"
                      : phase.status === "running"
                        ? `${phase.count}/${phase.total} 已完成`
                        : phase.status === "failed"
                          ? `${phase.count}/${phase.total} 已完成 · 有问题`
                          : phase.status === "completed"
                            ? `${phase.count} 条证据`
                            : phase.status === "pending"
                              ? `${phase.count}/${phase.total} 已完成`
                              : `${phase.total} 条记录`}
                  </small>
                </div>
              ))}
              {workPhaseSummary.some((phase) => phase.status === "unknown") ? (
                <p>未记录的阶段不会被视为已经通过。</p>
              ) : null}
            </section>
            <div className={styles.sectionHeading}>
              <strong>本轮已知过程</strong>
              <span>
                {completedStageCount}/{executionStages.length} 已完成
              </span>
            </div>
            <div className={styles.timeline} data-testid="execution-stages">
              {executionStages.map((step) => {
                const Icon = phaseIcon(step.phase, step.status);
                const duration = formatDuration(step.durationMs);
                const stepIsRunning = isRunningTraceStatus(step.status);
                return (
                  <div
                    className={styles.item}
                    key={step.id}
                    data-phase={step.phase}
                    data-step-status={step.status}
                    aria-current={stepIsRunning ? "step" : undefined}
                  >
                    <span className={styles.itemMarker}>
                      <Icon
                        className={`${styles.itemIcon} ${
                          stepIsRunning ? styles.itemIconSpin : ""
                        }`}
                        size={14}
                        aria-hidden="true"
                      />
                    </span>
                    <div className={styles.itemBody}>
                      <div className={styles.itemHeading}>
                        <span className={styles.itemTitle}>
                          {friendlyStageTitle(step)}
                        </span>
                        <span className={styles.itemMeta}>
                          <span className={styles.itemStatus}>
                            {traceStatusLabel(step.status)}
                          </span>
                          {duration ? (
                            <span className={styles.itemDuration}>
                              {duration}
                            </span>
                          ) : null}
                          <span className={styles.itemTime}>{step.time}</span>
                        </span>
                      </div>
                      <div className={styles.itemDetail}>
                        {friendlyStageDetail(step)}
                      </div>
                      {step.phase === "preview" && previewUrlFromStep(step) ? (
                        <button
                          type="button"
                          className={styles.previewInspectAction}
                          onClick={() => void handleInspectPreview(step)}
                          disabled={inspectingStepId === step.id}
                        >
                          {inspectingStepId === step.id ? "诊断中" : "诊断预览"}
                        </button>
                      ) : null}
                      {step.diagnosis ? (
                        <div className={styles.itemDiagnosis}>
                          <strong>{step.diagnosis.summary}</strong>
                          <span>{step.diagnosis.next_action}</span>
                          {step.diagnosis.evidence ? (
                            <em>证据：{step.diagnosis.evidence}</em>
                          ) : null}
                          <button
                            type="button"
                            className={styles.itemDiagnosisAction}
                            onClick={() =>
                              handleRepairFromDiagnosis(step.diagnosis!, step)
                            }
                          >
                            生成修复提示
                          </button>
                        </div>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
            <details className={styles.diagnosticPanel}>
              <summary>
                <span>
                  <strong>技术详情</strong>
                  <small>命令、路径与原始运行记录</small>
                </span>
                <em>{processTrace.rawSteps.length} 条原始事件</em>
              </summary>
              {workspaceName ? (
                <div className={styles.workspaceLine}>
                  <span>工作区</span>
                  <strong>{workspaceName}</strong>
                </div>
              ) : null}
              <div className={styles.diagnosticList}>
                {processTrace.rawSteps.map((step) => (
                  <details className={styles.diagnosticEvent} key={step.id}>
                    <summary>
                      <span>{step.time}</span>
                      <strong>{step.title}</strong>
                      <em>{traceStatusLabel(step.status)}</em>
                    </summary>
                    <button
                      type="button"
                      onClick={() => void handleCopyDebug(step)}
                    >
                      复制事件
                    </button>
                    <pre>{JSON.stringify(step.debug, null, 2)}</pre>
                  </details>
                ))}
              </div>
            </details>
          </div>
        </div>
      </Drawer>
    </div>
  );
}
