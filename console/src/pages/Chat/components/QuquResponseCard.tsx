import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Avatar, Flex } from "antd";
import { Bubble, DefaultCards, Markdown } from "@/vendor/lantaiChat";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronDown,
  Cloud,
  Copy,
  Loader2,
} from "lucide-react";
import LantaiRuntimeResponseBuilder from "@/vendor/lantaiChatResponseBuilder";
import Actions from "@/vendor/lantaiChatResponseActions";
import ErrorMessage from "@/vendor/lantaiChatResponseError";
import Reasoning from "@/vendor/lantaiChatResponseReasoning";
import Tool from "@/vendor/lantaiChatResponseTool";
import { useChatAnywhereOptions } from "@/vendor/lantaiChatOptions";
import { useChatAnywhereSessionsState } from "@/vendor/lantaiChat";
export { ChatAnywhereOptionsProvider } from "@/vendor/lantaiChat";
import {
  LantaiRuntimeContentType,
  LantaiRuntimeMessageType,
  LantaiRuntimeRunStatus,
  type ILantaiRuntimeMessage,
  type ILantaiRuntimeResponse,
  type ITextContent,
} from "@/vendor/lantaiChatTypes";
import type { ComponentProps } from "@ant-design/x-markdown";
import { useTranslation } from "react-i18next";
import type {
  ChatTurnEventPreviewEvent,
  UtilityArtifactClassificationResponse,
  UtilityClassifiedArtifact,
} from "@/api/types";
import { chatApi } from "@/api";
import { ApiRequestError } from "@/api/request";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { copyText } from "../utils";
import { parseQuquBlocks, stripQuquFromArtifactContent } from "../ququ";
import {
  upsertQuquContinuation,
  useQuquContinuation,
  useQuquSourceMessageDigest,
} from "../ququContinuationStore";
import { extractLatestThemeCssCandidate } from "../themeCss";
import {
  getDomNodeText,
  normalizeSegmentText,
  shouldOfferSegmentCopy,
  shouldRenderDenseSegment,
} from "../copyableSegment";
import ThemeCssApplyCard from "./ThemeCssApplyCard";
import {
  DocumentOutput,
  ResearchAdvisoryCard,
  RuntimeFileCard,
  StandaloneTextArtifact,
  isDocumentLikeMarkdown,
  structuredMarkdownComponents,
} from "./StructuredContent";
import {
  splitStandaloneTextArtifacts,
  type StructuredOutputSegment,
} from "./StructuredTextArtifacts";
import {
  classifyUtilityArtifactsCached,
  getPersistedUtilityArtifactClassification,
  shouldClassifyUtilityArtifacts,
} from "./utilityArtifactClassification";
import { SystemActionNotice } from "./SystemActionNotice";
import CollaborationPhaseRail from "./CollaborationPhaseRail";
import {
  getSystemActionPresentation,
  isSystemCommandMessage,
} from "./systemActionPresentation";
import { latestCollaborationPreparation } from "../collaborationEventStore";
import WorkMethodProjectionCard from "./WorkMethodProjectionCard";
import { isScheduledTaskToolName } from "./scheduledTaskDraft";
import { isCustomizationDraftToolName } from "./customizationDraft";
import { isSubAgentToolName } from "./SubAgentToolCard";

const { Audios, Images, Videos } = DefaultCards;
const ARTIFACT_CLASSIFICATION_IDLE_DELAY_MS = 1200;

const TOOL_MESSAGE_TYPES = new Set<string>([
  LantaiRuntimeMessageType.PLUGIN_CALL,
  LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT,
  LantaiRuntimeMessageType.MCP_CALL,
  LantaiRuntimeMessageType.MCP_CALL_OUTPUT,
  LantaiRuntimeMessageType.MCP_APPROVAL_REQUEST,
]);

type PendingStage = "received" | "starting" | "waiting" | "long";
type InlineProcessStatus = "running" | "completed" | "incomplete" | "attention";

interface InlineProcessSummary {
  status: InlineProcessStatus;
  title: string;
  detail: string;
}

type InlineToolGroupStatus = "running" | "completed" | "attention";

function stringifyRuntimeContent(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function runtimeInputText(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value
    .flatMap((message) => {
      if (!message || typeof message !== "object") return [];
      const content = (message as Record<string, unknown>).content;
      if (!Array.isArray(content)) return [];
      return content.flatMap((item) => {
        if (!item || typeof item !== "object") return [];
        const record = item as Record<string, unknown>;
        return record.type === LantaiRuntimeContentType.TEXT &&
          typeof record.text === "string"
          ? [record.text]
          : [];
      });
    })
    .filter((text) => text.trim())
    .join("\n\n")
    .trim();
}

function recordText(record: Record<string, unknown>): string {
  const value = record.text || record.thinking || record.refusal;
  return typeof value === "string" ? value : stringifyRuntimeContent(value);
}

function completedTextContent(text: string): ITextContent {
  return {
    type: LantaiRuntimeContentType.TEXT,
    text,
    status: LantaiRuntimeRunStatus.Completed,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function timestampMs(value: unknown): number | null {
  if (typeof value === "string") {
    const parsedNumber = Number(value);
    if (Number.isFinite(parsedNumber)) return timestampMs(parsedNumber);
    const parsedDate = Date.parse(value);
    return Number.isFinite(parsedDate) ? parsedDate : null;
  }
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return null;
  }
  return value < 1_000_000_000_000 ? value * 1000 : value;
}

function responseStartedAt(data: ILantaiRuntimeResponse): number | null {
  return (
    timestampMs(data.created_at) ||
    timestampMs(data.metadata?.created_at) ||
    timestampMs(data.metadata?.started_at)
  );
}

function pendingStage(elapsedSeconds: number): PendingStage {
  if (elapsedSeconds < 30) return "waiting";
  return "long";
}

function toolName(message: ILantaiRuntimeMessage): string {
  const records = [
    message,
    ...(Array.isArray(message.content) ? message.content.filter(isRecord) : []),
  ];
  for (const record of records) {
    const nested = isRecord(record.data) ? record.data : {};
    const value =
      nested.name ||
      nested.tool_name ||
      nested.toolName ||
      record.name ||
      record.tool_name ||
      record.toolName;
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function toolActivityLabel(name: string): string {
  const normalized = name.toLowerCase();
  if (/(write|edit|patch|replace|create|save)(_file)?/.test(normalized)) {
    return "编辑文件";
  }
  if (/(web|browser|fetch|crawl|navigate)/.test(normalized)) {
    return "搜索与检查";
  }
  if (/(read|list|search|grep|glob|find|inspect)/.test(normalized)) {
    return "读取与检索";
  }
  if (/(bash|shell|exec|command|terminal|run)/.test(normalized)) {
    return "运行命令";
  }
  if (/(test|verify|lint|build|check)/.test(normalized)) {
    return "验证结果";
  }
  return "使用工具";
}

function toolRequiresStandalonePresentation(
  message: ILantaiRuntimeMessage,
): boolean {
  const name = toolName(message);
  return (
    name === "generate_image" ||
    name === "generate_video" ||
    isSubAgentToolName(name) ||
    isScheduledTaskToolName(name) ||
    isCustomizationDraftToolName(name)
  );
}

function isGroupableToolMessage(message: ILantaiRuntimeMessage): boolean {
  return (
    TOOL_MESSAGE_TYPES.has(message.type) &&
    message.type !== LantaiRuntimeMessageType.MCP_APPROVAL_REQUEST &&
    !toolRequiresStandalonePresentation(message)
  );
}

function messageFailed(message: ILantaiRuntimeMessage): boolean {
  return /(fail|error|cancel|reject|interrupt|stall)/.test(
    String(message.status || "").toLowerCase(),
  );
}

function messageRunning(message: ILantaiRuntimeMessage): boolean {
  return /(progress|running|queued|created|pending)/.test(
    String(message.status || "").toLowerCase(),
  );
}

function messageHasDeliverableContent(message: ILantaiRuntimeMessage): boolean {
  if (message.type !== LantaiRuntimeMessageType.MESSAGE) return false;
  const content = Array.isArray(message.content) ? message.content : [];
  return content.some((item) => {
    if (typeof item === "string") return item.trim().length > 0;
    if (!isRecord(item)) return false;
    const type = String(item.type || "").toLowerCase();
    if (type === LantaiRuntimeContentType.TEXT) {
      return String(item.text || "").trim().length > 0;
    }
    return [
      LantaiRuntimeContentType.IMAGE,
      LantaiRuntimeContentType.VIDEO,
      LantaiRuntimeContentType.AUDIO,
      LantaiRuntimeContentType.FILE,
    ].some((contentType) => String(contentType).toLowerCase() === type);
  });
}

function buildInlineProcessSummary(
  messages: ILantaiRuntimeMessage[],
  generating: boolean,
): InlineProcessSummary | null {
  const reasoningCount = messages.filter(
    (message) => message.type === LantaiRuntimeMessageType.REASONING,
  ).length;
  const tools = messages.filter((message) =>
    TOOL_MESSAGE_TYPES.has(message.type),
  );
  // Reasoning has its own complete, expandable stream. This summary exists
  // only to keep raw tool traffic out of the conversation transcript; it must
  // never replace or paraphrase the model's reasoning.
  if (tools.length === 0) return null;

  const failedCount = tools.filter(messageFailed).length;
  const runningTool = [...tools].reverse().find(messageRunning);
  const hasDeliverableContent = messages.some(messageHasDeliverableContent);
  const status: InlineProcessStatus = generating
    ? "running"
    : failedCount > 0
      ? "attention"
      : hasDeliverableContent
        ? "completed"
        : "incomplete";
  const title =
    status === "completed"
      ? "回复已生成"
      : status === "attention"
        ? hasDeliverableContent
          ? "回复已生成，仍有步骤未完成"
          : "有步骤未完成"
        : status === "incomplete"
          ? "工具执行已结束"
          : runningTool
            ? `正在${toolActivityLabel(toolName(runningTool))}`
            : "正在继续处理";
  const details = [];
  if (tools.length > 0) details.push(`${tools.length} 次工具调用`);
  if (failedCount > 0) details.push(`${failedCount} 个步骤未完成`);
  if (reasoningCount > 0) details.push(`${reasoningCount} 段思考`);
  if (status === "incomplete") details.push("尚未看到完整回复");
  return { status, title, detail: details.join(" · ") };
}

function inlineToolGroupStatus(
  messages: ILantaiRuntimeMessage[],
  generating: boolean,
  settledByAnswer: boolean,
): InlineToolGroupStatus {
  if (messages.some(messageRunning)) return "running";

  // A failed attempt is not a failed turn. While the Runtime is still active,
  // the model may be reasoning, retrying with corrected arguments, or choosing
  // a policy-safe path. Keep that recoverable history in the expandable audit
  // surface without presenting a terminal warning in the conversation. Do not
  // let that turn-wide recovery state overwrite already completed tool groups.
  if (generating && messages.some(messageFailed)) return "running";
  if (messages.some(messageFailed)) return "attention";
  if (settledByAnswer) return "completed";
  return "completed";
}

function compactToolActivities(messages: ILantaiRuntimeMessage[]): string {
  const activities = Array.from(
    new Set(messages.map((message) => toolActivityLabel(toolName(message)))),
  );
  if (!activities.length) return "处理任务";
  const visible = activities.slice(0, 3).join("、");
  return activities.length > 3
    ? `${visible} 等 ${activities.length} 类操作`
    : visible;
}

function InlineToolGroup({
  messages,
  generating,
  settledByAnswer,
}: {
  messages: ILantaiRuntimeMessage[];
  generating: boolean;
  settledByAnswer: boolean;
}) {
  const status = inlineToolGroupStatus(messages, generating, settledByAnswer);
  const [expanded, setExpanded] = useState(status === "running");

  useEffect(() => {
    setExpanded(status === "running");
  }, [status]);

  const failedCount = messages.filter(messageFailed).length;
  const title =
    status === "running"
      ? failedCount > 0
        ? "正在尝试其他方式"
        : "正在执行工具"
      : status === "attention"
        ? failedCount > 1
          ? "部分步骤未完成"
          : "有一步未完成"
        : "工具执行已完成";
  const detail = `${messages.length} 次调用 · ${compactToolActivities(messages)}${
    status === "attention" ? " · 详情已保留" : ""
  }`;
  const StatusIcon =
    status === "running"
      ? Loader2
      : status === "attention"
        ? AlertCircle
        : CheckCircle2;

  return (
    <section
      className="lantai-inline-tool-group"
      data-lantai-tool-group="1"
      data-status={status}
      aria-busy={status === "running" ? "true" : undefined}
    >
      <button
        type="button"
        className="lantai-inline-tool-group-summary"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-label={`${expanded ? "收起" : "展开"}执行过程，${title}，${detail}`}
      >
        <span className="lantai-inline-tool-group-icon" aria-hidden="true">
          <StatusIcon
            size={14}
            className={status === "running" ? "lantai-tool-group-spin" : ""}
          />
        </span>
        <span className="lantai-inline-tool-group-copy">
          <strong>{title}</strong>
          <span>{detail}</span>
        </span>
        <ChevronDown
          size={15}
          className="lantai-inline-tool-group-chevron"
          data-open={expanded ? "1" : "0"}
          aria-hidden="true"
        />
      </button>
      {expanded ? (
        <div className="lantai-inline-tool-group-details">
          {messages.map((message) => (
            <Tool key={message.id} data={message} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function formatElapsed(elapsedSeconds: number): string {
  if (elapsedSeconds < 1) return "刚刚";
  if (elapsedSeconds < 60) return `${elapsedSeconds} 秒`;
  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = elapsedSeconds % 60;
  return seconds > 0 ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
}

function PendingAssistantStatus({
  data,
  progressInComposer = false,
  identity,
}: {
  data: ILantaiRuntimeResponse;
  progressInComposer?: boolean;
  identity?: React.ReactNode;
}) {
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  const currentSession = sessions.find(
    (session) => session.id === currentSessionId,
  ) as
    | {
        generating?: boolean;
        __primaryStreamActive?: boolean;
        status?: string | null;
        runtimeState?: {
          status?: string | null;
          run_id?: string | null;
          events_preview?: ChatTurnEventPreviewEvent[];
        } | null;
      }
    | undefined;
  const collaboration =
    String(currentSession?.runtimeState?.status || "").toLowerCase() ===
    "running"
      ? latestCollaborationPreparation(
          currentSession?.runtimeState?.events_preview || [],
        )
      : undefined;
  const fallbackStartedAtRef = useRef(Date.now());
  const startedAt = responseStartedAt(data) || fallbackStartedAtRef.current;
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  const runtimeStatus = String(
    currentSession?.runtimeState?.status || "",
  ).toLowerCase();
  const runtimeRunId = String(
    currentSession?.runtimeState?.run_id || "",
  ).trim();
  const responseRunId = String(data.metadata?.run_id || "").trim();
  const optimisticPending = data.metadata?.optimistic_pending === true;
  const sessionGenerating = Boolean(
    currentSession?.generating === true ||
    String(currentSession?.status || "").toLowerCase() === "running",
  );
  const liveOptimisticPending = Boolean(
    optimisticPending && (!currentSession || sessionGenerating),
  );
  const elapsedSeconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  const stage = pendingStage(elapsedSeconds);
  const terminalRuntime = [
    "completed",
    "failed",
    "interrupted",
    "stalled",
    "cancelled",
    "finished",
  ].includes(runtimeStatus);
  const runIdsMismatch = Boolean(
    runtimeRunId && responseRunId && runtimeRunId !== responseRunId,
  );
  const verifiedCurrentRun = Boolean(
    runtimeStatus === "running" &&
    runtimeRunId &&
    responseRunId &&
    runtimeRunId === responseRunId,
  );
  // A server receipt acknowledges this local placeholder before transcript
  // output supplies its turn id. Keep the identity through that gap, but only
  // while the same local stream and backend Run are still active.
  const verifiedPendingReceipt = Boolean(
    sessionGenerating &&
    currentSession?.__primaryStreamActive === true &&
    runtimeStatus === "running" &&
    runtimeRunId &&
    data.metadata?.pending_receipt_run_id === runtimeRunId &&
    !runIdsMismatch,
  );
  const hasHeartbeatEvidence = Boolean(
    data.output?.some(
      (message) => message.type === LantaiRuntimeMessageType.HEARTBEAT,
    ),
  );
  const hasPendingEvidence = Boolean(
    liveOptimisticPending ||
    verifiedPendingReceipt ||
    responseRunId ||
    verifiedCurrentRun ||
    hasHeartbeatEvidence ||
    collaboration,
  );

  // An empty in-progress response can be restored from stale history or be
  // created transiently while session state is reconciling. It must not claim
  // that a task is starting until the current turn has a concrete receipt.
  if (!hasPendingEvidence) return null;

  // A terminal runtime without a distinct newer response ID usually proves
  // that this empty card is stale. The exception is the local optimistic card
  // for a newly submitted turn while the session itself is still generating:
  // it may briefly retain the previous run's terminal snapshot until the
  // backend assigns the new run_id. Once generation ends, the local receipt
  // is no longer evidence and must disappear.
  if (
    terminalRuntime &&
    ((!runIdsMismatch && !liveOptimisticPending) || stage === "long")
  ) {
    return null;
  }
  // Session navigation can restore an old in-progress card next to a newer
  // backend run. Never project the newer run's liveness onto the old card.
  if (runtimeStatus === "running" && runIdsMismatch) {
    return null;
  }
  // After 30 seconds, only a matching backend run (or a concrete hosted
  // collaboration preparation event) may assert that work is still active.
  if (
    stage === "long" &&
    !verifiedCurrentRun &&
    !verifiedPendingReceipt &&
    !collaboration
  ) {
    return null;
  }
  const copy =
    stage === "long"
      ? {
          title: "暂时没有新进展",
          detail:
            verifiedCurrentRun && hasHeartbeatEvidence
              ? "连接正常，还没收到新进展；可以查看过程或停止任务"
              : "还没收到新进展，请查看过程确认状态",
        }
      : {
          title: "等待回应",
          detail:
            verifiedCurrentRun && hasHeartbeatEvidence
              ? "连接正常，正在等待回应"
              : liveOptimisticPending
                ? "消息已发出"
                : "还没收到回应",
        };

  // Keep the assistant visible as soon as the real send receipt exists, even
  // before the provider's first byte. The composer still owns progress: the
  // identity is not a second status and must not pretend the model is thinking.
  // The liveness guards above also keep stale/restored cards from showing it.
  if (progressInComposer) return identity || null;

  if (collaboration) {
    return (
      <div
        role="status"
        aria-live="polite"
        aria-label={collaboration.title}
        className="lantai-collaboration-pending"
        data-lantai-assistant-pending="1"
        data-lantai-collaboration-pending="1"
      >
        <span className="lantai-collaboration-pending-row">
          <Bubble.Spin />
          <span className="lantai-pending-assistant-copy">
            <span className="lantai-pending-assistant-title">
              {collaboration.title}
            </span>
            <span className="lantai-pending-assistant-detail">
              {collaboration.detail}
            </span>
          </span>
          <span className="lantai-pending-assistant-elapsed">
            {formatElapsed(elapsedSeconds)}
          </span>
        </span>
        <CollaborationPhaseRail phase={collaboration.phase} compact />
      </div>
    );
  }

  return (
    <Flex
      align="center"
      gap={10}
      role="status"
      aria-live="polite"
      aria-label={copy.title}
      className="lantai-pending-assistant"
      data-lantai-assistant-pending="1"
      data-stage={stage}
    >
      <Bubble.Spin />
      <span className="lantai-pending-assistant-copy">
        <span className="lantai-pending-assistant-title">{copy.title}</span>
        <span className="lantai-pending-assistant-detail">{copy.detail}</span>
      </span>
      <span className="lantai-pending-assistant-elapsed">
        {formatElapsed(elapsedSeconds)}
      </span>
    </Flex>
  );
}

function InlineProcessStatus({ summary }: { summary: InlineProcessSummary }) {
  return (
    <div
      className="lantai-inline-process-summary"
      data-lantai-inline-process="1"
      data-status={summary.status}
      role="status"
      aria-live={summary.status === "running" ? "polite" : "off"}
    >
      <span className="lantai-inline-process-indicator" aria-hidden="true" />
      <span className="lantai-inline-process-title">{summary.title}</span>
      <span className="lantai-inline-process-detail">{summary.detail}</span>
    </div>
  );
}

function normalizeRenderableContent(content: unknown): unknown[] {
  if (typeof content === "string") return [completedTextContent(content)];
  if (!Array.isArray(content)) {
    return content == null
      ? []
      : [completedTextContent(stringifyRuntimeContent(content))];
  }

  return content.map((item) => {
    if (!item || typeof item !== "object") {
      return completedTextContent(stringifyRuntimeContent(item));
    }
    const record = item as Record<string, unknown>;
    const rawType =
      typeof record.type === "string" && record.type.trim()
        ? record.type.trim()
        : LantaiRuntimeContentType.TEXT;
    if (rawType === "thinking" || rawType === "reasoning") {
      return completedTextContent(recordText(record));
    }
    if (rawType === LantaiRuntimeContentType.TEXT) {
      return {
        ...record,
        type: LantaiRuntimeContentType.TEXT,
        text: recordText(record),
        status:
          typeof record.status === "string"
            ? record.status
            : LantaiRuntimeRunStatus.Completed,
      };
    }
    return {
      ...record,
      type: rawType,
      status:
        typeof record.status === "string"
          ? record.status
          : LantaiRuntimeRunStatus.Completed,
    };
  });
}

function normalizeRenderableMessage(
  message: ILantaiRuntimeMessage,
): ILantaiRuntimeMessage {
  const type =
    typeof message.type === "string" && message.type.trim()
      ? message.type.trim()
      : LantaiRuntimeMessageType.MESSAGE;
  if (type === LantaiRuntimeMessageType.MESSAGE) {
    return {
      ...message,
      type,
      content: normalizeRenderableContent(message.content),
    };
  }
  return { ...message, type };
}

function mergeAdjacentReasoningMessages(
  messages: ILantaiRuntimeMessage[],
): ILantaiRuntimeMessage[] {
  const merged: ILantaiRuntimeMessage[] = [];
  for (const message of messages) {
    if (message.type !== LantaiRuntimeMessageType.REASONING) {
      merged.push(message);
      continue;
    }
    const previous = merged[merged.length - 1];
    if (previous?.type !== LantaiRuntimeMessageType.REASONING) {
      merged.push(message);
      continue;
    }
    const sourceIdentity = reasoningSourceIdentity(message);
    if (
      !sourceIdentity ||
      sourceIdentity !== reasoningSourceIdentity(previous)
    ) {
      merged.push(message);
      continue;
    }
    previous.id = `${previous.id || "reasoning"}+${message.id || "reasoning"}`;
    previous.status =
      previous.status === LantaiRuntimeRunStatus.Completed &&
      message.status === LantaiRuntimeRunStatus.Completed
        ? LantaiRuntimeRunStatus.Completed
        : String(message.status || previous.status || "in_progress");
    previous.content = [
      ...(Array.isArray(previous.content) ? previous.content : []),
      ...(Array.isArray(message.content) ? message.content : []),
    ] as ILantaiRuntimeMessage["content"];
  }
  return merged;
}

function reasoningSourceIdentity(message: ILantaiRuntimeMessage): string {
  const metadata = isRecord(message.metadata) ? message.metadata : {};
  const nested = isRecord(metadata.metadata) ? metadata.metadata : {};
  for (const container of [metadata, nested]) {
    for (const key of ["source_message_id", "original_id", "runtime_item_id"]) {
      const value = container[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  return typeof message.id === "string" ? message.id.trim() : "";
}

function CopyableParagraph(props: ComponentProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [copied, setCopied] = React.useState(false);
  const text = useMemo(
    () => normalizeSegmentText(getDomNodeText(props.domNode)),
    [props.domNode],
  );
  const canCopy = props.streamStatus === "done" && shouldOfferSegmentCopy(text);
  const dense = shouldRenderDenseSegment(text);

  const handleCopy = useCallback(async () => {
    if (!text) return;
    try {
      await copyText(text);
      setCopied(true);
      message.success(t("common.copied"));
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      message.error(t("common.copyFailed"));
    }
  }, [message, t, text]);

  return (
    <p
      className={`lantai-copyable-segment ${props.className || ""}`}
      data-copyable={canCopy ? "true" : "false"}
      data-dense={dense ? "true" : "false"}
    >
      {props.children}
      {canCopy ? (
        <button
          type="button"
          className="lantai-copyable-segment-button"
          aria-label={t("common.copy")}
          title={t("common.copy")}
          onClick={handleCopy}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
      ) : null}
    </p>
  );
}

function QuquAside({
  text,
  placement,
  responseId,
  sourceMessageId,
  sourceMessageDigest,
  ququOrdinal,
  owner,
  userText,
  answerText,
}: {
  text: string;
  placement: "lead" | "echo";
  responseId: string;
  sourceMessageId: string;
  sourceMessageDigest: string;
  ququOrdinal: number;
  owner?: {
    chatId: string;
    sessionId: string;
    userId: string;
    channel: string;
  };
  userText: string;
  answerText: string;
}) {
  const savedContinuation = useQuquContinuation({
    chat_id: owner?.chatId || "",
    source_message_id: sourceMessageId,
    source_message_digest: sourceMessageDigest,
    ququ_ordinal: ququOrdinal,
    placement,
  });
  const [continuing, setContinuing] = useState(false);
  const [continuationError, setContinuationError] = useState("");
  const requestIdRef = useRef("");
  const explicitRetryRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const continueThought = useCallback(async () => {
    if (continuing || savedContinuation) return;
    if (
      !owner?.chatId ||
      !owner.sessionId ||
      !sourceMessageId ||
      !sourceMessageDigest
    ) {
      setContinuationError("原回答还没有稳定下来，请稍后再试");
      return;
    }
    if (!requestIdRef.current) {
      requestIdRef.current =
        globalThis.crypto?.randomUUID?.() ||
        `ququ-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    setContinuing(true);
    setContinuationError("");
    try {
      const result = await chatApi.continueQuqu({
        request_id: requestIdRef.current,
        chat_id: owner.chatId,
        session_id: owner.sessionId,
        user_id: owner.userId,
        channel: owner.channel,
        source_message_id: sourceMessageId,
        source_message_digest: sourceMessageDigest,
        ququ_ordinal: ququOrdinal,
        placement,
        response_id: responseId,
        ququ_text: text,
        user_text: userText,
        answer_text: answerText,
        retry_failed: explicitRetryRef.current,
      });
      upsertQuquContinuation(result);
    } catch (error) {
      const sourceConflict =
        error instanceof ApiRequestError &&
        error.httpStatus === 409 &&
        error.code !== "ququ_generation_failed";
      explicitRetryRef.current = !sourceConflict;
      if (mountedRef.current) {
        setContinuationError(
          sourceConflict
            ? error.message
            : "这句一时没接上，再试一次",
        );
      }
    } finally {
      if (mountedRef.current) setContinuing(false);
    }
  }, [
    answerText,
    continuing,
    owner,
    placement,
    ququOrdinal,
    responseId,
    savedContinuation,
    sourceMessageDigest,
    sourceMessageId,
    text,
    userText,
  ]);

  const continuation = savedContinuation?.text || "";

  return (
    <div
      className="ququ inner_monologue"
      data-block="ququ"
      data-ququ-placement={placement}
    >
      <Cloud
        aria-hidden="true"
        className="ququ-icon"
        focusable="false"
        size={14}
        strokeWidth={1.8}
      />
      <span className="ququ-body">
        <span className="ququ-text">{text}</span>
        {continuation ? (
          <span className="ququ-continuation" data-ququ-continuation="1">
            {continuation}
          </span>
        ) : continuationError ? (
          <span className="ququ-continuation-error" role="status">
            {continuationError}
          </span>
        ) : null}
      </span>
      <span className="ququ-actions" role="group" aria-label="蛐蛐操作">
        {!continuation ? (
          <button
            type="button"
            className="ququ-action-continue"
            disabled={continuing || !sourceMessageDigest}
            aria-busy={continuing ? "true" : undefined}
            onClick={() => void continueThought()}
          >
            {continuing ? "在想…" : "再说一点"}
          </button>
        ) : null}
      </span>
    </div>
  );
}

function QuquMarkdown({
  content,
  cursor,
  classificationResult,
  allowDocumentOutput = true,
}: {
  content: string;
  cursor: boolean;
  classificationResult: UtilityArtifactClassificationResponse | null;
  allowDocumentOutput?: boolean;
}) {
  const blocks = useMemo(() => parseQuquBlocks(content), [content]);
  const documentContent = useMemo(
    () => stripQuquFromArtifactContent(content),
    [content],
  );
  const classifiedArtifacts: UtilityClassifiedArtifact[] = useMemo(
    () =>
      classificationResult?.status === "classified"
        ? classificationResult.artifacts
        : [],
    [classificationResult],
  );
  const themeCssCandidate = useMemo(
    () => (cursor ? null : extractLatestThemeCssCandidate(content)),
    [content, cursor],
  );
  const lastMarkdownIndex = useMemo(() => {
    for (let index = blocks.length - 1; index >= 0; index -= 1) {
      if (blocks[index].type === "markdown") return index;
    }
    return -1;
  }, [blocks]);
  const structuredSegments = useMemo(
    () =>
      blocks.map((block) =>
        block.type === "markdown"
          ? splitStandaloneTextArtifacts(
              block.text,
              classifiedArtifacts.flatMap((artifact) => {
                const start = block.text.indexOf(artifact.source_text);
                if (
                  start < 0 ||
                  block.text.indexOf(artifact.source_text, start + 1) !== -1
                ) {
                  return [];
                }
                return [
                  {
                    ...artifact,
                    start,
                    end: start + artifact.source_text.length,
                  },
                ];
              }),
            )
          : ([] as StructuredOutputSegment[]),
      ),
    [blocks, classifiedArtifacts],
  );
  const hasTextArtifacts = useMemo(
    () =>
      structuredSegments.some((segments) =>
        segments.some((segment) => segment.type === "artifact"),
      ),
    [structuredSegments],
  );
  const markdownComponents = useMemo(
    () => ({
      p: CopyableParagraph,
      ...structuredMarkdownComponents,
    }),
    [],
  );

  const renderMarkdownBlock = (index: number) => {
    const block = blocks[index];
    if (block.type !== "markdown") return null;
    return (
      <React.Fragment key={`markdown-${index}`}>
        {structuredSegments[index].map((segment, segmentIndex) => {
          const segmentIsLast =
            index === lastMarkdownIndex &&
            segmentIndex === structuredSegments[index].length - 1;
          const segmentIsStreaming = cursor && segmentIsLast;

          if (segment.type === "markdown") {
            return (
              <Markdown
                key={`markdown-${index}-${segmentIndex}`}
                content={segment.content}
                cursor={segmentIsStreaming}
                components={markdownComponents}
              />
            );
          }

          return (
            <StandaloneTextArtifact
              key={`artifact-${index}-${segmentIndex}-${segment.title}`}
              artifact={segment}
              complete={!segmentIsStreaming}
            >
              <Markdown
                content={segment.body}
                cursor={segmentIsStreaming}
                components={structuredMarkdownComponents}
              />
            </StandaloneTextArtifact>
          );
        })}
      </React.Fragment>
    );
  };
  const rendered = (
    <>
      {blocks.map((block, index) =>
        block.type === "markdown" ? renderMarkdownBlock(index) : null,
      )}
      {themeCssCandidate ? (
        <ThemeCssApplyCard candidate={themeCssCandidate} />
      ) : null}
    </>
  );
  const isDocumentOutput =
    allowDocumentOutput &&
    !cursor &&
    !hasTextArtifacts &&
    isDocumentLikeMarkdown(documentContent);

  if (isDocumentOutput) {
    const documentBody = blocks.flatMap((block, index) =>
      block.type === "markdown" ? [renderMarkdownBlock(index)] : [],
    );

    return (
      <>
        <DocumentOutput content={documentContent}>
          {documentBody}
        </DocumentOutput>
        {themeCssCandidate ? (
          <ThemeCssApplyCard candidate={themeCssCandidate} />
        ) : null}
      </>
    );
  }

  return rendered;
}

function useUtilityOutputPresentation({
  text,
  classificationKey,
  enabled,
  cursor,
}: {
  text: string;
  classificationKey: string;
  enabled: boolean;
  cursor: boolean;
}): UtilityArtifactClassificationResponse | null {
  const identity = `${classificationKey}\u0000${text}`;
  const persisted = useMemo(
    () =>
      cursor
        ? null
        : getPersistedUtilityArtifactClassification(classificationKey, text),
    [classificationKey, cursor, text],
  );
  const [state, setState] = useState<{
    identity: string;
    result: UtilityArtifactClassificationResponse | null;
  }>(() => ({ identity, result: persisted }));
  const currentResult = state.identity === identity ? state.result : persisted;

  useEffect(() => {
    const restored = cursor
      ? null
      : getPersistedUtilityArtifactClassification(classificationKey, text);
    if (restored) {
      setState({ identity, result: restored });
      return;
    }
    if (!enabled || cursor || !shouldClassifyUtilityArtifacts(text)) {
      setState({ identity, result: null });
      return;
    }

    let disposed = false;
    const controller = new AbortController();
    setState({ identity, result: null });
    const timer = window.setTimeout(() => {
      void classifyUtilityArtifactsCached(
        text,
        classificationKey,
        controller.signal,
      )
        .then((result) => {
          if (disposed) return;
          setState({
            identity,
            result: result.status === "classified" ? result : null,
          });
        })
        .catch(() => {
          // Best-effort presentation enhancement: plain Markdown stays visible.
        });
    }, ARTIFACT_CLASSIFICATION_IDLE_DELAY_MS);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [classificationKey, cursor, enabled, identity, text]);

  return currentResult;
}

const QuquMessage = React.memo(function QuquMessage({
  data,
  classifyArtifacts,
  responseInProgress,
  responseId,
  continuationOwner,
  turnUserText,
}: {
  data: ILantaiRuntimeMessage;
  classifyArtifacts: boolean;
  responseInProgress: boolean;
  responseId: string;
  continuationOwner?: {
    chatId: string;
    sessionId: string;
    userId: string;
    channel: string;
  };
  turnUserText: string;
}) {
  const replaceMediaURL = useChatAnywhereOptions((v) => v.api?.replaceMediaURL);
  const formatMediaURL = useCallback(
    (url?: string) => {
      if (!url) return url;
      return replaceMediaURL?.(url) || url;
    },
    [replaceMediaURL],
  );
  const systemAction = getSystemActionPresentation(data);
  const systemCommand = isSystemCommandMessage(data);
  const sourceMessageText = useMemo(
    () =>
      (data.content || [])
        .flatMap((item) =>
          item.type === LantaiRuntimeContentType.TEXT
            ? [String((item as ITextContent).text || "")]
            : [],
        )
        .filter(Boolean)
        .join("\n"),
    [data.content],
  );
  const sourceMessageDigest = useQuquSourceMessageDigest(sourceMessageText);
  const classificationText = useMemo(
    () =>
      (data.content || [])
        .flatMap((item) =>
          item.type === LantaiRuntimeContentType.TEXT
            ? [stripQuquFromArtifactContent((item as ITextContent).text)]
            : [],
        )
        .filter((text) => text.trim())
        .join("\n\n"),
    [data.content],
  );
  const classificationCursor = Boolean(
    (data.content || []).some(
      (item) =>
        item.type === LantaiRuntimeContentType.TEXT &&
        item.status === LantaiRuntimeRunStatus.InProgress,
    ),
  );
  const classificationResult = useUtilityOutputPresentation({
    text: classificationText,
    classificationKey: data.id ? String(data.id) : "",
    enabled: classifyArtifacts && !systemCommand && !systemAction,
    cursor: classificationCursor,
  });

  if (!data.content?.length) return null;
  if (systemAction) return <SystemActionNotice action={systemAction} />;
  const ququBlocks = data.content.flatMap((item, contentIndex) => {
    if (item.type !== LantaiRuntimeContentType.TEXT) return [];
    return parseQuquBlocks((item as ITextContent).text).flatMap(
      (block, blockIndex) =>
        block.type === "ququ"
          ? [
              {
                ...block,
                key: `${contentIndex}:${blockIndex}`,
              },
            ]
          : [],
    );
  });
  const sourceMessageId = String(data.id || "");
  const stableSourceDigest =
    responseInProgress ||
    classificationCursor ||
    data.status === LantaiRuntimeRunStatus.InProgress
      ? ""
      : sourceMessageDigest;

  return (
    <>
      {ququBlocks
        .filter((block) => block.placement === "lead")
        .map((block) => (
          <QuquAside
            key={`lead:${block.key}`}
            text={block.text}
            placement={block.placement}
            responseId={responseId}
            sourceMessageId={sourceMessageId}
            sourceMessageDigest={stableSourceDigest}
            ququOrdinal={ququBlocks.indexOf(block)}
            owner={continuationOwner}
            userText={turnUserText}
            answerText={classificationText}
          />
        ))}
      {data.content.map((item, index) => {
        switch (item.type) {
          case LantaiRuntimeContentType.TEXT: {
            const artifactContent = stripQuquFromArtifactContent(
              (item as ITextContent).text,
            );
            if (!artifactContent) return null;
            return (
              <QuquMarkdown
                key={index}
                content={artifactContent}
                cursor={item.status === LantaiRuntimeRunStatus.InProgress}
                classificationResult={classificationResult}
                allowDocumentOutput={!systemCommand}
              />
            );
          }
          case LantaiRuntimeContentType.REFUSAL:
            return <Markdown raw key={index} content={item.refusal} />;
          case LantaiRuntimeContentType.IMAGE:
            return (
              <Images
                key={index}
                data={[{ url: formatMediaURL(item.image_url) }]}
              />
            );
          case LantaiRuntimeContentType.VIDEO:
            return (
              <Videos
                key={index}
                data={[
                  {
                    src: formatMediaURL(item.video_url) || "",
                    poster: formatMediaURL(item.video_poster),
                  },
                ]}
              />
            );
          case LantaiRuntimeContentType.FILE:
            return (
              <RuntimeFileCard
                key={index}
                url={formatMediaURL(item.file_url)}
                sourceUrl={item.file_source_url || item.file_url}
                name={item.file_name || item.fileName || item.file_id}
                size={item.file_size}
              />
            );
          case LantaiRuntimeContentType.AUDIO:
            return (
              <Audios
                key={index}
                data={[
                  { src: formatMediaURL(item.audio_url || item.data) || "" },
                ]}
              />
            );
          default:
            return <div key={index}>{JSON.stringify(item)}</div>;
        }
      })}
      {!systemCommand && classificationResult?.advisory ? (
        <ResearchAdvisoryCard advisory={classificationResult.advisory} />
      ) : null}
      {ququBlocks
        .filter((block) => block.placement === "echo")
        .map((block) => (
          <QuquAside
            key={`echo:${block.key}`}
            text={block.text}
            placement={block.placement}
            responseId={responseId}
            sourceMessageId={sourceMessageId}
            sourceMessageDigest={stableSourceDigest}
            ququOrdinal={ququBlocks.indexOf(block)}
            owner={continuationOwner}
            userText={turnUserText}
            answerText={classificationText}
          />
        ))}
    </>
  );
});

export default function QuquResponseCard(props: {
  data: ILantaiRuntimeResponse;
  isLast?: boolean;
  showThinking?: boolean;
  showToolCalls?: boolean;
  progressInComposer?: boolean;
}) {
  const avatar = useChatAnywhereOptions((v) => v.welcome?.avatar);
  const nick = useChatAnywhereOptions((v) => v.welcome?.nick);
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  const continuationOwner = useMemo(() => {
    const session = sessions.find((item) => item.id === currentSessionId) as
      | {
          id?: string;
          realId?: string;
          sessionId?: string;
          userId?: string;
          channel?: string;
        }
      | undefined;
    const chatId = String(session?.realId || session?.id || "").trim();
    const sessionId = String(session?.sessionId || "").trim();
    if (!chatId || !sessionId) return undefined;
    return {
      chatId,
      sessionId,
      userId: String(session?.userId || "default"),
      channel: String(session?.channel || "console"),
    };
  }, [currentSessionId, sessions]);
  const lastVisibleMessagesRef = useRef<{
    identity: string;
    messages: ILantaiRuntimeMessage[];
  }>({ identity: "", messages: [] });
  const processMessages = useMemo(() => {
    return mergeAdjacentReasoningMessages(
      LantaiRuntimeResponseBuilder.mergeToolMessages(props.data.output).map(
        normalizeRenderableMessage,
      ),
    ).filter((item) => item.type !== LantaiRuntimeMessageType.HEARTBEAT);
  }, [props.data.output]);
  const messages = useMemo(
    () =>
      processMessages.filter((item) => {
        // Reasoning and meaningful tool activity stay in their source order.
        // Consecutive tool calls are compacted into an expandable group below;
        // the run drawer remains the second-level audit surface.
        if (item.type === LantaiRuntimeMessageType.REASONING) {
          return props.showThinking !== false;
        }
        if (TOOL_MESSAGE_TYPES.has(item.type)) {
          return (
            item.type === LantaiRuntimeMessageType.MCP_APPROVAL_REQUEST ||
            props.showToolCalls !== false
          );
        }
        return true;
      }),
    [processMessages, props.showThinking, props.showToolCalls],
  );
  const responseIdentity = String(
    props.data.id ||
      props.data.metadata?.run_id ||
      props.data.metadata?.response_id ||
      "",
  );
  const turnUserText = useMemo(
    () => runtimeInputText(props.data.input),
    [props.data.input],
  );
  const cachedMessages = lastVisibleMessagesRef.current;
  if (responseIdentity && cachedMessages.identity !== responseIdentity) {
    lastVisibleMessagesRef.current = {
      identity: responseIdentity,
      messages: [],
    };
  }
  if (responseIdentity && messages.length > 0) {
    lastVisibleMessagesRef.current = { identity: responseIdentity, messages };
  }
  const visibleMessages =
    messages.length > 0
      ? messages
      : responseIdentity &&
          LantaiRuntimeResponseBuilder.maybeGenerating(props.data) &&
          lastVisibleMessagesRef.current.identity === responseIdentity
        ? lastVisibleMessagesRef.current.messages
        : messages;
  const generating = LantaiRuntimeResponseBuilder.maybeGenerating(props.data);
  let terminalAnswerIndex = -1;
  if (!generating) {
    for (let index = visibleMessages.length - 1; index >= 0; index -= 1) {
      if (visibleMessages[index].type === LantaiRuntimeMessageType.MESSAGE) {
        terminalAnswerIndex = index;
        break;
      }
    }
  }
  const renderedMessages = visibleMessages;
  const hasOutputError = renderedMessages.some(
    (item) => item.type === LantaiRuntimeMessageType.ERROR,
  );
  const hasDeliveredFile = renderedMessages.some(
    (item) =>
      item.type === LantaiRuntimeMessageType.MESSAGE &&
      Array.isArray(item.content) &&
      item.content.some(
        (content) => content?.type === LantaiRuntimeContentType.FILE,
      ),
  );
  const completionGateWarning = String(
    props.data.metadata?.completion_gate_warning || "",
  ).trim();
  const responseErrorCode = String(
    props.data.error?.code ||
      props.data.error?.error_code ||
      props.data.error?.reason ||
      "",
  )
    .trim()
    .toLowerCase();
  const deliveredGateWarning = Boolean(
    completionGateWarning ||
    (hasDeliveredFile && responseErrorCode === "completion_gate_failed"),
  );
  const workMethodId = String(props.data.metadata?.work_method_id || "");
  const showWorkMethodProjection = [
    "deep_research",
    "sustained_execution",
  ].includes(workMethodId);
  const workMethodProjection = showWorkMethodProjection ? (
    <WorkMethodProjectionCard
      methodId={workMethodId}
      events={props.data.metadata?.runtime_projection_events}
      generating={generating}
    />
  ) : null;
  const inlineProcessSummary = buildInlineProcessSummary(
    processMessages,
    generating,
  );
  const showInlineProcessSummary = Boolean(
    inlineProcessSummary &&
    (inlineProcessSummary.status === "attention" ||
      (terminalAnswerIndex < 0 &&
        ((props.isLast === true &&
          (generating ||
            inlineProcessSummary.status !== "completed" ||
            props.showToolCalls === false)) ||
          (!generating && inlineProcessSummary.status === "incomplete")))),
  );
  let lastAnswerIndex = -1;
  for (let index = renderedMessages.length - 1; index >= 0; index -= 1) {
    if (renderedMessages[index].type === LantaiRuntimeMessageType.MESSAGE) {
      lastAnswerIndex = index;
      break;
    }
  }
  const assistantIdentity = avatar ? (
    <Flex
      align="center"
      gap={8}
      style={{ marginBottom: 8 }}
      data-lantai-response-avatar="1"
    >
      <Avatar src={avatar} />
      {nick && <span>{nick as string}</span>}
    </Flex>
  ) : null;

  if (!renderedMessages?.length && generating) {
    if (props.isLast === false) {
      return null;
    }
    return (
      <>
        {workMethodProjection}
        {inlineProcessSummary ? (
          <InlineProcessStatus summary={inlineProcessSummary} />
        ) : (
          <PendingAssistantStatus
            data={props.data}
            progressInComposer={props.progressInComposer}
            identity={assistantIdentity}
          />
        )}
      </>
    );
  }

  if (
    !renderedMessages?.length &&
    !generating &&
    props.isLast !== false &&
    String(props.data.status || "").toLowerCase() === "completed"
  ) {
    return (
      <>
        {workMethodProjection}
        <InlineProcessStatus
          summary={{
            status: "incomplete",
            title: "未收到最终回复",
            detail: "可先核实已有结果，再决定是否重新执行",
          }}
        />
      </>
    );
  }

  const standaloneSystemAction =
    renderedMessages.length === 1 &&
    renderedMessages[0].type === LantaiRuntimeMessageType.MESSAGE
      ? getSystemActionPresentation(renderedMessages[0])
      : null;
  if (standaloneSystemAction) {
    return (
      <>
        {standaloneSystemAction.showAvatar ? assistantIdentity : null}
        <SystemActionNotice action={standaloneSystemAction} />
      </>
    );
  }

  return (
    <>
      {assistantIdentity}
      {workMethodProjection}
      {showInlineProcessSummary && inlineProcessSummary ? (
        <InlineProcessStatus summary={inlineProcessSummary} />
      ) : null}
      {renderedMessages.map((item, index) => {
        const previousItem = renderedMessages[index - 1];
        if (isGroupableToolMessage(item)) {
          if (previousItem && isGroupableToolMessage(previousItem)) {
            return null;
          }
          const groupedTools = [];
          for (
            let toolIndex = index;
            toolIndex < renderedMessages.length;
            toolIndex += 1
          ) {
            const candidate = renderedMessages[toolIndex];
            if (!isGroupableToolMessage(candidate)) {
              break;
            }
            groupedTools.push(candidate);
          }
          return (
            <InlineToolGroup
              key={`tool-group:${groupedTools[0]?.id || index}`}
              messages={groupedTools}
              generating={generating}
              settledByAnswer={terminalAnswerIndex >= 0}
            />
          );
        }
        switch (item.type) {
          case LantaiRuntimeMessageType.MESSAGE:
            return (
              <div
                key={item.id}
                className="lantai-assistant-answer"
                data-lantai-assistant-answer="1"
              >
                <QuquMessage
                  data={item}
                  responseInProgress={
                    props.data.status === LantaiRuntimeRunStatus.InProgress
                  }
                  responseId={responseIdentity}
                  continuationOwner={continuationOwner}
                  turnUserText={turnUserText}
                  classifyArtifacts={
                    props.isLast === true &&
                    props.data.status === LantaiRuntimeRunStatus.Completed &&
                    index === lastAnswerIndex
                  }
                />
              </div>
            );
          case LantaiRuntimeMessageType.PLUGIN_CALL:
          case LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT:
          case LantaiRuntimeMessageType.MCP_CALL:
          case LantaiRuntimeMessageType.MCP_CALL_OUTPUT:
            return <Tool key={item.id} data={item} />;
          case LantaiRuntimeMessageType.MCP_APPROVAL_REQUEST:
            return <Tool key={item.id} data={item} isApproval />;
          case LantaiRuntimeMessageType.REASONING:
            return <Reasoning key={item.id} data={item} />;
          case LantaiRuntimeMessageType.ERROR:
            return <ErrorMessage key={item.id} data={item} />;
          default:
            console.warn(`[WIP] Unknown message type: ${item.type}`);
            return null;
        }
      })}
      {deliveredGateWarning && !hasOutputError ? (
        <ErrorMessage
          data={{
            code: "delivered_with_validation_warning",
            partial_success: true,
            message:
              completionGateWarning ||
              "文件已生成并保留，但质量校验尚未全部通过。",
          }}
        />
      ) : props.data.error && !hasOutputError ? (
        <ErrorMessage data={props.data.error} />
      ) : null}
      <Actions {...props} />
    </>
  );
}
