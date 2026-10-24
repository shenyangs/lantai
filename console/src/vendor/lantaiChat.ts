import React, {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import { flushSync } from "react-dom";
import { Button, Dropdown, Image, Input, Modal, Spin, Tooltip } from "antd";
import {
  ArrowUp,
  Check,
  ChevronDown,
  CircleStop,
  Copy,
  CornerDownLeft,
  File as FileIcon,
  GitBranch,
  LoaderCircle,
  MoreHorizontal,
  Pencil,
  RotateCcw,
  Square,
  Trash2,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  LantaiRuntimeContentType,
  LantaiRuntimeMessageType,
  LantaiRuntimeRunStatus,
  type ILantaiRuntimeMessage,
  type ILantaiRuntimeResponse,
} from "./lantaiChatTypes";
import {
  attachmentFingerprint,
  clipboardDataHasAttachmentHint,
  filesFromClipboardData,
  filesFromDesktopClipboardAttachments,
  formatAttachmentSize,
  inferAttachmentMimeType,
  prepareAttachmentFile,
} from "./lantaiChatAttachments";
import {
  ATTACHMENT_ROLE_OPTIONS,
  buildAttachmentRoleContext,
  DEFAULT_ATTACHMENT_ROLE,
  getAttachmentRoleLabel,
  isLantaiAttachmentRole,
  parseAttachmentRoleContext,
  shouldConfirmAttachmentRoles,
  stripAttachmentRoleContext,
  type LantaiAttachmentRole,
} from "./lantaiAttachmentRoles";
import { createStreamPresentationScheduler } from "./lantaiChatStreamPresentation";
import { createTranscriptReconciler } from "./lantaiTranscriptReconciler";
export {
  userMessageClientTurnKey,
  userMessageInputSignature,
} from "./lantaiTranscriptReconciler";
import {
  ConversationNavigator,
  scrollConversationToMessage,
} from "./lantaiConversationNavigator";
import {
  consumePendingChatMessageNavigation,
  peekPendingChatMessageNavigation,
  subscribeChatMessageNavigation,
  type PendingChatMessageNavigation,
} from "./lantaiChatMessageNavigation";
import { useAgentStore } from "../stores/agentStore";
import {
  MARKDOWN_HANDOFF_EVENT,
  buildMarkdownHandoffPrompt,
  consumeMarkdownHandoff,
} from "../components/MarkdownHandoffService/markdownHandoff";

const REQUEST_CARD_CODE = "LantaiRuntimeRequestCard";
const RESPONSE_CARD_CODE = "LantaiRuntimeResponseCard";
const ATTACHMENT_ONLY_SUBMIT_MARKER = "\u200B";
const MIN_THINKING_PRESENTATION_MS = 500;
const MIN_COMMITTED_THINKING_HOLD_MS = 180;
const MAX_THINKING_COMMIT_WAIT_MS = 900;
const FIRST_TURN_WELCOME_EXIT_MS = 320;
const STREAM_CURSOR_HREF = "#lantai-stream-cursor";
const STREAM_CURSOR_MARKDOWN = `[\u200B](${STREAM_CURSOR_HREF})`;
const LIVE_THINKING_SELECTOR = [
  '[data-chat-thinking="1"][data-status="created"]',
  '[data-chat-thinking="1"][data-status="in_progress"]',
  '[data-chat-thinking="1"][data-status="running"]',
].join(",");
const COMMITTED_THINKING_SELECTOR = '[data-chat-thinking="1"]';

function thinkingElementSignature(element: Element): string {
  return `${element.getAttribute("data-status") || ""}\u0000${element.textContent || ""}`;
}

export interface ILantaiChatMessage {
  id: string;
  role?: string;
  cards?: Array<{ code?: string; data?: any }>;
  msgStatus?: string;
  createdAt?: string | number | null;
  [key: string]: unknown;
}

type ChatTimestamp = string | number | null | undefined;

interface ChatTimestampFormatOptions {
  expanded?: boolean;
  locale?: string;
  timeZone?: string;
}

function normalizeChatTimestamp(
  value: ChatTimestamp,
): { date: Date; preserveWallClock: boolean } | null {
  if (value == null || value === "") return null;
  if (typeof value === "string") {
    const legacy = value.match(
      /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$/,
    );
    if (legacy) {
      const date = new Date(
        Date.UTC(
          Number(legacy[1]),
          Number(legacy[2]) - 1,
          Number(legacy[3]),
          Number(legacy[4]),
          Number(legacy[5]),
          Number(legacy[6]),
          Number((legacy[7] || "0").padEnd(3, "0")),
        ),
      );
      return { date, preserveWallClock: true };
    }
  }
  const numeric =
    typeof value === "number"
      ? value < 10_000_000_000
        ? value * 1000
        : value
      : value;
  const date = new Date(numeric);
  return Number.isNaN(date.getTime())
    ? null
    : { date, preserveWallClock: false };
}

export function formatChatTimestamp(
  value: ChatTimestamp,
  options: ChatTimestampFormatOptions = {},
): string {
  const normalized = normalizeChatTimestamp(value);
  if (!normalized) return "";
  const { date, preserveWallClock } = normalized;
  const locale = options.locale || "zh-CN";
  const timeZone = preserveWallClock ? "UTC" : options.timeZone;
  const time = new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
  }).format(date);
  if (!options.expanded) return time;

  const monthDayParts = new Intl.DateTimeFormat(locale, {
    month: "numeric",
    day: "numeric",
    timeZone,
  }).formatToParts(date);
  const month =
    monthDayParts.find((part) => part.type === "month")?.value || "";
  const day = monthDayParts.find((part) => part.type === "day")?.value || "";
  const monthDay = locale.toLowerCase().startsWith("zh")
    ? `${month}月${day}日`
    : new Intl.DateTimeFormat(locale, {
        month: "numeric",
        day: "numeric",
        timeZone,
      }).format(date);
  const weekday = new Intl.DateTimeFormat(locale, {
    weekday: "long",
    timeZone,
  }).format(date);
  return `${monthDay} ${weekday} ${time}`;
}

export function ConversationTimeAnchor({
  value,
  locale = "zh-CN",
  timeZone,
}: {
  value: ChatTimestamp;
  locale?: string;
  timeZone?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const label = formatChatTimestamp(value, {
    expanded,
    locale,
    timeZone,
  });
  if (!label) return null;
  return React.createElement(
    "button",
    {
      type: "button",
      className: "lantai-conversation-time-anchor",
      "aria-expanded": expanded,
      "aria-label": label,
      title: expanded ? "点击收起日期" : "点击查看完整日期",
      onClick: () => setExpanded((current) => !current),
    },
    label,
  );
}

function messageTimestamp(message: ILantaiChatMessage): ChatTimestamp {
  if (message.createdAt != null) return message.createdAt;
  if (message.created_at != null) {
    return message.created_at as ChatTimestamp;
  }
  const responseCard = message.cards?.find(
    (card) => card.code === RESPONSE_CARD_CODE,
  );
  return responseCard?.data?.created_at as ChatTimestamp;
}

export interface ILantaiChatSession {
  id: string;
  name?: string;
  messages?: ILantaiChatMessage[];
  __primaryStreamActive?: boolean;
  __primaryStreamStartedAt?: number;
  __primaryStreamSettledAt?: number;
  [key: string]: unknown;
}

export interface ILantaiChatDraftAttachment {
  id: string;
  name: string;
  type: string;
  size: number;
  url: string;
  status: "ready";
  fingerprint?: string;
  role?: LantaiAttachmentRole;
  roleConfirmed?: boolean;
  canonicalPath?: string;
  storedName?: string;
  digest?: string;
  immutableVersion?: number;
  sourceId?: string;
  sourceChatId?: string;
  uploadReceipt?: string;
}

export interface ILantaiChatSessionAPI {
  /** Mark the renderer's authoritative active conversation before hydration. */
  activateSession?: (sessionId?: string | null) => void;
  getSessionList?: () => Promise<ILantaiChatSession[]>;
  getSession?: (sessionId: string) => Promise<ILantaiChatSession>;
  createSession?: (
    session: Partial<ILantaiChatSession>,
  ) => Promise<ILantaiChatSession[]>;
  updateSession?: (
    session: Partial<ILantaiChatSession>,
  ) => Promise<ILantaiChatSession[]>;
  branchSession?: (
    sessionId: string,
    userMessageId: string,
  ) => Promise<ILantaiChatSession[]>;
  recoverLegacyHistory?: (sessionId: string) => Promise<ILantaiChatSession[]>;
  removeSession?: (
    session: Partial<ILantaiChatSession>,
  ) => Promise<ILantaiChatSession[]>;
  loadDraftText?: (sessionId: string) => string;
  saveDraftText?: (sessionId: string, text: string) => void;
  clearDraftText?: (sessionId: string) => void;
  loadDraftAttachments?: (sessionId: string) => ILantaiChatDraftAttachment[];
  saveDraftAttachments?: (
    sessionId: string,
    attachments: ILantaiChatDraftAttachment[],
  ) => void;
  clearDraftAttachments?: (sessionId: string) => void;
}

export interface ILantaiRuntimeWebUIRef {
  messages: {
    getMessages: () => ILantaiChatMessage[];
    removeAllMessages: (ownerSessionIds: string | string[]) => boolean;
    upsertTransientAssistantResponse: (
      ownerSessionIds: string | string[],
      messageId: string,
      data: ILantaiRuntimeResponse,
    ) => boolean;
  };
  navigation: {
    scrollToMessage: (messageId: string) => boolean;
  };
  branch: {
    createFromLatestUserMessage: () => boolean;
  };
}

type Selector<T, R> = (value: T) => R;

export type ChatOptions = Record<string, any> & {
  api?: Record<string, any>;
  cards?: Record<string, ComponentType<any>>;
  session?: {
    api?: ILantaiChatSessionAPI;
    multiple?: boolean;
    hideBuiltInSessionList?: boolean;
    initialSessionId?: string;
    initialSessionStrategy?: "first" | "new";
  };
};

type MessagesAction =
  | { type: "replace"; messages: ILantaiChatMessage[] }
  | {
      type: "update";
      update: (messages: ILantaiChatMessage[]) => ILantaiChatMessage[];
    };

type CommandSuggestion = {
  label?: ReactNode;
  value: string;
  command?: string;
  description?: string;
};

type SelectedCommand = {
  value: string;
  command: string;
  description?: string;
};

function normalizeCommandSuggestion(
  suggestion: CommandSuggestion,
): SelectedCommand | null {
  const value = suggestion.value?.trim();
  if (!value) return null;
  const explicitCommand = suggestion.command?.trim();
  return {
    value,
    command: explicitCommand?.startsWith("/")
      ? explicitCommand
      : `/${explicitCommand || value}`,
    description: suggestion.description?.trim() || undefined,
  };
}

function commandDraftBody(draft: string, selected: SelectedCommand): string {
  if (draft === selected.command) return "";
  const separator = draft.slice(
    selected.command.length,
    selected.command.length + 1,
  );
  if (
    draft.startsWith(selected.command) &&
    (separator === " " || separator === "\n" || separator === "\t")
  ) {
    return draft.slice(selected.command.length + 1);
  }
  return draft;
}

function composeCommandDraft(selected: SelectedCommand, body: string): string {
  return body ? `${selected.command} ${body}` : selected.command;
}

function selectedCommandFromDraft(
  draft: string,
  suggestions: CommandSuggestion[],
): SelectedCommand | null {
  for (const suggestion of suggestions) {
    const selected = normalizeCommandSuggestion(suggestion);
    if (!selected) continue;
    if (
      draft === selected.command ||
      draft.startsWith(`${selected.command} `) ||
      draft.startsWith(`${selected.command}\n`) ||
      draft.startsWith(`${selected.command}\t`)
    ) {
      return selected;
    }
  }
  return null;
}

function messagesReducer(
  state: ILantaiChatMessage[],
  action: MessagesAction,
): ILantaiChatMessage[] {
  if (action.type === "replace") return action.messages;
  return action.update(state);
}

export type ILantaiRuntimeWebUIOptions = ChatOptions;

function requestBizParamsSnapshot(
  api: Record<string, any> | undefined,
): Record<string, unknown> {
  const value = api?.getRequestBizParams?.();
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function requestBizParamsForTurn(
  api: Record<string, any> | undefined,
  meta: Record<string, unknown>,
): Record<string, unknown> {
  const snapshot = requestBizParamsSnapshot(api);
  const snapshotMeta =
    snapshot.meta &&
    typeof snapshot.meta === "object" &&
    !Array.isArray(snapshot.meta)
      ? (snapshot.meta as Record<string, unknown>)
      : {};
  return {
    ...snapshot,
    meta: {
      ...snapshotMeta,
      ...meta,
    },
  };
}

type SessionsState = {
  sessions: ILantaiChatSession[];
  currentSessionId?: string;
  setCurrentSessionId: (sessionId?: string | null) => void;
  setSessions: (
    sessions:
      | ILantaiChatSession[]
      | ((sessions: ILantaiChatSession[]) => ILantaiChatSession[]),
  ) => void;
};

type SessionsActions = {
  createSession: (
    seed?: Partial<ILantaiChatSession>,
  ) => Promise<ILantaiChatSession | null>;
};

type InputState = {
  loading: boolean;
  setLoading: (loading: boolean | string) => void;
  getLoading: () => boolean;
  /** Append a visible draft only when the originating conversation still owns the input. */
  appendDraft?: (text: string, ownerSessionId: string) => boolean;
};

export const ChatAnywhereSessionsContext = createContext<
  SessionsState & SessionsActions
>({
  sessions: [],
  currentSessionId: undefined,
  setCurrentSessionId: () => {},
  setSessions: () => {},
  createSession: async () => null,
});

export const ChatAnywhereInputContext = createContext<InputState>({
  loading: false,
  setLoading: () => {},
  getLoading: () => false,
});

const ChatAnywhereOptionsContext = createContext<ChatOptions>({});

export function ChatAnywhereOptionsProvider({
  options,
  children,
}: {
  options: ChatOptions;
  children: ReactNode;
}) {
  return React.createElement(
    ChatAnywhereOptionsContext.Provider,
    { value: options },
    children,
  );
}

function useSelectedContext<T, R>(value: T, selector?: Selector<T, R>): T | R {
  return selector ? selector(value) : value;
}

export function useChatAnywhereOptions<R = ChatOptions>(
  selector?: Selector<ChatOptions, R>,
): ChatOptions | R {
  const value = useContext(ChatAnywhereOptionsContext);
  return useSelectedContext(value, selector);
}

export function useChatAnywhereInput<R = InputState>(
  selector?: Selector<InputState, R>,
): InputState | R {
  const value = useContext(ChatAnywhereInputContext);
  return useSelectedContext(value, selector);
}

export function useChatAnywhereSessionsState<R = SessionsState>(
  selector?: Selector<SessionsState, R>,
): SessionsState | R {
  const value = useContext(ChatAnywhereSessionsContext);
  const state = useMemo<SessionsState>(
    () => ({
      sessions: value.sessions,
      currentSessionId: value.currentSessionId,
      setCurrentSessionId: value.setCurrentSessionId,
      setSessions: value.setSessions,
    }),
    [
      value.currentSessionId,
      value.sessions,
      value.setCurrentSessionId,
      value.setSessions,
    ],
  );
  return useSelectedContext(state, selector);
}

export function useChatAnywhereSessions<R = SessionsActions>(
  selector?: Selector<SessionsActions, R>,
): SessionsActions | R {
  const value = useContext(ChatAnywhereSessionsContext);
  const actions = useMemo<SessionsActions>(
    () => ({
      createSession: value.createSession,
    }),
    [value.createSession],
  );
  return useSelectedContext(actions, selector);
}

function generateId(prefix = "lantai"): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function normalizeText(value: string): string {
  return value.split(ATTACHMENT_ONLY_SUBMIT_MARKER).join("").trim();
}

function sentAttachmentKind(
  record: Record<string, unknown>,
  filename: string,
): string {
  const extension = filename.match(/\.([a-z0-9]{1,10})$/i)?.[1];
  if (extension) return extension.toUpperCase();
  const mediaType = String(record.media_type || record.mediaType || "");
  const subtype = mediaType.split("/")[1]?.split(/[;+]/)[0];
  if (subtype) return subtype.toUpperCase();
  if (record.type === LantaiRuntimeContentType.AUDIO) return "音频";
  if (record.type === LantaiRuntimeContentType.VIDEO) return "视频";
  return "文件";
}

function sentAttachmentMeta(
  record: Record<string, unknown>,
  filename: string,
): string {
  const kind = sentAttachmentKind(record, filename);
  const size = Number(record.file_size || record.fileSize || record.size || 0);
  const formattedSize =
    Number.isFinite(size) && size > 0 ? formatAttachmentSize(size) : "";
  const role = isLantaiAttachmentRole(record.attachment_role)
    ? record.attachment_role
    : DEFAULT_ATTACHMENT_ROLE;
  return [getAttachmentRoleLabel(role), kind, formattedSize]
    .filter(Boolean)
    .join(" · ");
}

function textContent(text: string, status = LantaiRuntimeRunStatus.Created) {
  return {
    type: LantaiRuntimeContentType.TEXT,
    text,
    status,
  };
}

export type AttachmentItem = {
  id: string;
  name: string;
  type: string;
  size: number;
  url: string;
  status: "uploading" | "ready" | "error";
  progress?: number;
  error?: string;
  fingerprint?: string;
  role?: LantaiAttachmentRole;
  roleConfirmed?: boolean;
  previewUrl?: string;
  sourceFile?: File;
  canonicalPath?: string;
  storedName?: string;
  digest?: string;
  immutableVersion?: number;
  sourceId?: string;
  sourceChatId?: string;
  uploadReceipt?: string;
};

const PENDING_ATTACHMENT_SESSION_KEY = "__lantai_pending_session__";

type SubmissionDraft = {
  text: string;
  attachments: AttachmentItem[];
  userMessage?: ILantaiChatMessage;
  branchFromMessageId?: string;
  replacesCommandId?: string;
  replacesMessageId?: string;
  controlIntent?: ControlInputIntent;
  expectedRunId?: string;
  expectedAttemptId?: string;
};

type ControlInputIntent = "supplement" | "redirect" | "queue_next";

type ControlInputReplacementTarget = {
  sessionId: string;
  messageId: string;
  commandId: string;
};

type RuntimeQueueStatus =
  | "sending"
  | "admitted"
  | "claimed"
  | "bound"
  | "applied"
  | "steered"
  | "queued"
  | "uncertain"
  | "cancelled"
  | "superseded"
  | "failed";

const RUNTIME_QUEUE_STATUS_KEY = "runtime_queue_status";
const RUNTIME_CONTROL_COMMAND_ID_KEY = "runtime_control_command_id";
const RUNTIME_REPLACES_COMMAND_ID_KEY = "runtime_replaces_command_id";
const RUNTIME_REPLACES_MESSAGE_ID_KEY = "runtime_replaces_message_id";
const RUNTIME_CONTROL_INTENT_KEY = "runtime_control_intent";
const RUNTIME_WAITING_REASON_KEY = "runtime_control_waiting_reason";
const RUNTIME_EXPECTED_RUN_ID_KEY = "runtime_expected_run_id";
const RUNTIME_EXPECTED_ATTEMPT_ID_KEY = "runtime_expected_attempt_id";
const RUNTIME_CONTROL_ERROR_CODE_KEY = "runtime_control_error_code";
const RUNTIME_CONTROL_ERROR_MESSAGE_KEY = "runtime_control_error_message";

function runtimeControlIntent(
  message: ILantaiChatMessage,
): ControlInputIntent | null {
  const value = String(
    message.cards?.[0]?.data?.[RUNTIME_CONTROL_INTENT_KEY] || "",
  );
  return ["supplement", "redirect", "queue_next"].includes(value)
    ? (value as ControlInputIntent)
    : null;
}

function runtimeControlWaitingReason(message: ILantaiChatMessage): string {
  return String(
    message.cards?.[0]?.data?.[RUNTIME_WAITING_REASON_KEY] || "",
  ).trim();
}

function runtimeControlErrorCode(message: ILantaiChatMessage): string {
  return String(
    message.cards?.[0]?.data?.[RUNTIME_CONTROL_ERROR_CODE_KEY] || "",
  ).trim();
}

function runtimeQueueStatus(
  message: ILantaiChatMessage,
): RuntimeQueueStatus | null {
  const value = message.cards?.[0]?.data?.[RUNTIME_QUEUE_STATUS_KEY];
  return [
    "sending",
    "admitted",
    "claimed",
    "bound",
    "applied",
    "steered",
    "queued",
    "uncertain",
    "cancelled",
    "superseded",
    "failed",
  ].includes(String(value))
    ? (value as RuntimeQueueStatus)
    : null;
}

function withRuntimeQueueStatus(
  message: ILantaiChatMessage,
  status: RuntimeQueueStatus,
): ILantaiChatMessage {
  return {
    ...message,
    cards: (message.cards || []).map((card, index) =>
      index === 0
        ? {
            ...card,
            data: {
              ...(card.data || {}),
              [RUNTIME_QUEUE_STATUS_KEY]: status,
            },
          }
        : card,
    ),
  };
}

function runtimeControlCommandId(message: ILantaiChatMessage): string {
  return String(
    message.cards?.[0]?.data?.[RUNTIME_CONTROL_COMMAND_ID_KEY] || message.id,
  ).trim();
}

function withRuntimeControlMetadata(
  message: ILantaiChatMessage,
  fields: {
    commandId?: string;
    replacesCommandId?: string;
    replacesMessageId?: string;
    controlIntent?: ControlInputIntent;
    waitingReason?: string | null;
    expectedRunId?: string;
    expectedAttemptId?: string;
    errorCode?: string | null;
    errorMessage?: string | null;
  },
): ILantaiChatMessage {
  return {
    ...message,
    cards: (message.cards || []).map((card, index) => {
      if (index !== 0) return card;
      const data = {
        ...(card.data || {}),
        ...(fields.commandId
          ? { [RUNTIME_CONTROL_COMMAND_ID_KEY]: fields.commandId }
          : {}),
        ...(fields.replacesCommandId
          ? { [RUNTIME_REPLACES_COMMAND_ID_KEY]: fields.replacesCommandId }
          : {}),
        ...(fields.replacesMessageId
          ? { [RUNTIME_REPLACES_MESSAGE_ID_KEY]: fields.replacesMessageId }
          : {}),
        ...(fields.controlIntent
          ? { [RUNTIME_CONTROL_INTENT_KEY]: fields.controlIntent }
          : {}),
        ...(typeof fields.waitingReason === "string" && fields.waitingReason
          ? { [RUNTIME_WAITING_REASON_KEY]: fields.waitingReason }
          : {}),
        ...(fields.expectedRunId
          ? { [RUNTIME_EXPECTED_RUN_ID_KEY]: fields.expectedRunId }
          : {}),
        ...(fields.expectedAttemptId
          ? { [RUNTIME_EXPECTED_ATTEMPT_ID_KEY]: fields.expectedAttemptId }
          : {}),
        ...(typeof fields.errorCode === "string" && fields.errorCode
          ? { [RUNTIME_CONTROL_ERROR_CODE_KEY]: fields.errorCode }
          : {}),
        ...(typeof fields.errorMessage === "string" && fields.errorMessage
          ? { [RUNTIME_CONTROL_ERROR_MESSAGE_KEY]: fields.errorMessage }
          : {}),
      };
      if (fields.waitingReason === null) {
        delete data[RUNTIME_WAITING_REASON_KEY];
      }
      if (fields.errorCode === null) {
        delete data[RUNTIME_CONTROL_ERROR_CODE_KEY];
      }
      if (fields.errorMessage === null) {
        delete data[RUNTIME_CONTROL_ERROR_MESSAGE_KEY];
      }
      return { ...card, data };
    }),
  };
}

function withoutRuntimeQueueStatus(
  message: ILantaiChatMessage,
): ILantaiChatMessage {
  return {
    ...message,
    cards: (message.cards || []).map((card, index) => {
      if (index !== 0 || !card.data) return card;
      const data = { ...card.data };
      delete data[RUNTIME_QUEUE_STATUS_KEY];
      return { ...card, data };
    }),
  };
}

type TurnControlReceipt = {
  receiptStatus: "accepted" | "steered" | "queued" | "admitted";
  executionStatus?: string;
  commandId?: string;
  waitingReason?: string;
};

const TURN_RECEIPT_MAX_EVENT_CHARS = 256 * 1024;
const TURN_RECEIPT_TIMEOUT_MS = 10_000;

class TurnReceiptProtocolError extends Error {}
class TurnReceiptTimeoutError extends Error {}

class TurnControlHttpError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "TurnControlHttpError";
    this.status = status;
    this.code = code;
  }
}

function errorStringField(value: unknown, field: "code" | "message"): string {
  if (!value || typeof value !== "object") return "";
  const candidate = (value as Record<string, unknown>)[field];
  return typeof candidate === "string" ? candidate.trim() : "";
}

async function buildTurnControlHttpError(
  response: Response,
): Promise<TurnControlHttpError> {
  let code = `http_${response.status}`;
  let message = response.statusText || `HTTP ${response.status}`;
  try {
    const body = (await response.text()).trim();
    if (body) {
      const payload = JSON.parse(body) as Record<string, unknown>;
      const detail = payload.detail;
      const error = payload.error;
      code =
        errorStringField(error, "code") ||
        errorStringField(detail, "code") ||
        (typeof payload.code === "string" ? payload.code.trim() : "") ||
        code;
      message =
        errorStringField(error, "message") ||
        errorStringField(detail, "message") ||
        (typeof detail === "string" ? detail.trim() : "") ||
        (typeof payload.message === "string" ? payload.message.trim() : "") ||
        message;
    }
  } catch {
    // Keep the HTTP boundary itself actionable even when the response body is
    // not JSON or is truncated.
  }
  return new TurnControlHttpError(response.status, code, message);
}

function runtimeQueueStatusFromExecution(
  value: unknown,
): RuntimeQueueStatus | null {
  const status = String(value || "")
    .trim()
    .toLowerCase();
  if (status === "pending" || status === "waiting_boundary") {
    return "admitted";
  }
  if (status === "claimed" || status === "waiting_approval") {
    return "claimed";
  }
  if (status === "bound_to_request") return "bound";
  if (status === "applied") return "applied";
  if (status === "cancelled") return "cancelled";
  if (status === "superseded") return "superseded";
  if (status === "not_applied") return "failed";
  return null;
}

function parseTurnReceiptEvent(event: string): TurnControlReceipt | null {
  const encoded = event
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.replace(/^data:\s?/, ""))
    .join("\n")
    .trim();
  if (!encoded || encoded === "[DONE]") return null;

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(encoded) as Record<string, unknown>;
  } catch {
    // Other SSE frames can share this response. Only an explicit receipt
    // proves admission.
    return null;
  }

  const nestedReceipt =
    payload.receipt && typeof payload.receipt === "object"
      ? (payload.receipt as Record<string, unknown>)
      : null;
  const isReceiptFrame =
    payload.object === "turn_receipt" || nestedReceipt !== null;
  if (!isReceiptFrame) return null;

  const receipt = nestedReceipt || payload;
  const rawSchemaVersion = receipt.schema_version;
  const schemaVersion =
    rawSchemaVersion === undefined || rawSchemaVersion === null
      ? 1
      : Number(rawSchemaVersion);
  if (
    !Number.isInteger(schemaVersion) ||
    schemaVersion < 1 ||
    schemaVersion > 2
  ) {
    throw new TurnReceiptProtocolError(
      `Unsupported TurnReceipt schema version ${String(rawSchemaVersion)}`,
    );
  }

  const admissionStatus = String(receipt.admission_status || "").toLowerCase();
  if (schemaVersion === 2) {
    if (admissionStatus !== "admitted") {
      throw new TurnReceiptProtocolError(
        "TurnReceipt v2 did not confirm admission",
      );
    }
    return {
      receiptStatus: "admitted",
      executionStatus: String(
        receipt.execution_status || payload.status || "pending",
      ).toLowerCase(),
      commandId: String(receipt.command_id || "").trim() || undefined,
      waitingReason:
        String(receipt.next_waiting_reason || "").trim() || undefined,
    };
  }

  const status = String(receipt.status || payload.status || "").toLowerCase();
  if (["accepted", "steered", "queued"].includes(status)) {
    return {
      receiptStatus: status as "accepted" | "steered" | "queued",
    };
  }
  return null;
}

async function readTurnReceiptStatus(
  response: Response,
  timeoutMs = TURN_RECEIPT_TIMEOUT_MS,
): Promise<TurnControlReceipt> {
  if (!response.ok) {
    throw await buildTurnControlHttpError(response);
  }
  if (!response.body) throw new Error("Missing TurnReceipt stream");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  const boundedTimeoutMs =
    Number.isFinite(timeoutMs) && timeoutMs > 0
      ? timeoutMs
      : TURN_RECEIPT_TIMEOUT_MS;
  const deadline = Date.now() + boundedTimeoutMs;
  try {
    while (true) {
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) {
        throw new TurnReceiptTimeoutError("Timed out waiting for TurnReceipt");
      }
      let timeoutId: ReturnType<typeof setTimeout> | undefined;
      const { value, done } = await Promise.race([
        reader.read(),
        new Promise<never>((_resolve, reject) => {
          timeoutId = setTimeout(() => {
            reject(
              new TurnReceiptTimeoutError("Timed out waiting for TurnReceipt"),
            );
          }, remainingMs);
        }),
      ]).finally(() => {
        if (timeoutId !== undefined) clearTimeout(timeoutId);
      });
      buffered += decoder.decode(value, { stream: !done });
      let delimiter = buffered.match(/\r?\n\r?\n/);
      while (delimiter?.index !== undefined) {
        const event = buffered.slice(0, delimiter.index);
        buffered = buffered.slice(delimiter.index + delimiter[0].length);
        const receipt = parseTurnReceiptEvent(event);
        if (receipt) return receipt;
        delimiter = buffered.match(/\r?\n\r?\n/);
      }
      if (buffered.length > TURN_RECEIPT_MAX_EVENT_CHARS) {
        throw new TurnReceiptProtocolError(
          "TurnReceipt SSE frame is too large",
        );
      }
      if (done) {
        const receipt = parseTurnReceiptEvent(buffered);
        if (receipt) return receipt;
        break;
      }
    }
  } finally {
    void reader.cancel().catch(() => undefined);
  }
  throw new Error("Missing TurnReceipt");
}

type SessionRunState = {
  loading: boolean;
  submitting: boolean;
  stopping: boolean;
  controller: AbortController | null;
  steerControllers: Set<AbortController>;
  queue: SubmissionDraft[];
  bindingGuidanceMessageIds: Set<string>;
  awaitingVisibleAssistantOutput: boolean;
  confirmedStopTurnKeys: Set<string>;
};

type SteerActiveRunResult =
  | "admitted"
  | "steered"
  | "queued"
  | "failed"
  | "inactive";

function isQueuedGuidanceMessage(message: ILantaiChatMessage): boolean {
  const status = runtimeQueueStatus(message);
  return (
    message.role === "user" &&
    ["queued", "sending", "uncertain", "failed"].includes(String(status))
  );
}

function isGuidanceStripMessage(message: ILantaiChatMessage): boolean {
  const status = runtimeQueueStatus(message);
  return (
    message.role === "user" &&
    [
      "queued",
      "sending",
      "uncertain",
      "failed",
      "admitted",
      "claimed",
    ].includes(String(status))
  );
}

function queuedDraftsFromMessages(
  messages: ILantaiChatMessage[],
): SubmissionDraft[] {
  return messages.filter(isQueuedGuidanceMessage).map((message) => ({
    ...draftFromUserMessage(message),
    userMessage: message,
  }));
}

function isAutoStartableQueuedDraft(draft: SubmissionDraft): boolean {
  return (
    !draft.controlIntent &&
    (!draft.userMessage || runtimeQueueStatus(draft.userMessage) === "queued")
  );
}

function takeNextAutoStartableQueuedDraft(
  queue: SubmissionDraft[],
): SubmissionDraft | undefined {
  const index = queue.findIndex(isAutoStartableQueuedDraft);
  if (index < 0) return undefined;
  return queue.splice(index, 1)[0];
}

function hasCompleteRuntimeControlTarget(target: {
  expectedRunId?: string;
  expectedAttemptId?: string;
}): boolean {
  return Boolean(target.expectedRunId && target.expectedAttemptId);
}

export function attachmentToContent(
  attachment: AttachmentItem,
): Record<string, unknown> {
  const role = attachment.role || DEFAULT_ATTACHMENT_ROLE;
  const roleFields = {
    attachment_role: role,
    attachment_role_label: getAttachmentRoleLabel(role),
    file_source_url: attachment.canonicalPath || undefined,
    stored_name: attachment.storedName || undefined,
    attachment_digest: attachment.digest || undefined,
    attachment_version: attachment.immutableVersion || undefined,
    attachment_source_id: attachment.sourceId || undefined,
    attachment_source_chat_id: attachment.sourceChatId || undefined,
    attachment_upload_receipt: attachment.uploadReceipt || undefined,
  };
  if (attachment.type.startsWith("image/")) {
    return {
      type: LantaiRuntimeContentType.IMAGE,
      image_url: attachment.url,
      file_name: attachment.name,
      filename: attachment.name,
      file_size: attachment.size,
      media_type: attachment.type,
      status: LantaiRuntimeRunStatus.Created,
      ...roleFields,
    };
  }
  if (attachment.type.startsWith("video/")) {
    return {
      type: LantaiRuntimeContentType.VIDEO,
      video_url: attachment.url,
      file_name: attachment.name,
      filename: attachment.name,
      file_size: attachment.size,
      media_type: attachment.type,
      status: LantaiRuntimeRunStatus.Created,
      ...roleFields,
    };
  }
  if (attachment.type.startsWith("audio/")) {
    return {
      type: LantaiRuntimeContentType.AUDIO,
      data: attachment.url,
      file_name: attachment.name,
      filename: attachment.name,
      file_size: attachment.size,
      media_type: attachment.type,
      status: LantaiRuntimeRunStatus.Created,
      ...roleFields,
    };
  }
  return {
    type: LantaiRuntimeContentType.FILE,
    file_url: attachment.url,
    file_name: attachment.name,
    filename: attachment.name,
    file_size: attachment.size,
    media_type: attachment.type,
    status: LantaiRuntimeRunStatus.Created,
    ...roleFields,
  };
}

export function localAttachmentFilePath(value: unknown): string {
  const source = String(value || "").trim();
  if (!source) return "";
  if (/^(?:\/|[A-Za-z]:[\\/])/.test(source)) return source;
  if (source.startsWith("file://")) {
    try {
      return decodeURIComponent(new URL(source).pathname);
    } catch {
      return decodeURIComponent(source.slice("file://".length));
    }
  }
  try {
    const url = new URL(source, window.location.origin);
    const localPreview =
      !url.hostname ||
      url.hostname === "localhost" ||
      url.hostname === "127.0.0.1" ||
      url.hostname === "[::1]";
    const marker = "/files/preview/";
    const markerIndex = url.pathname.indexOf(marker);
    if (!localPreview || markerIndex < 0) return "";
    const encodedPath = url.pathname.slice(markerIndex + marker.length);
    return `/${decodeURIComponent(encodedPath).replace(/^\/+/, "")}`;
  } catch {
    return "";
  }
}

export function browserAttachmentUrl(value: unknown): string {
  const source = String(value || "").trim();
  if (!source || typeof window === "undefined") return "";
  try {
    const url = new URL(source, window.location.origin);
    return ["http:", "https:", "blob:"].includes(url.protocol)
      ? url.toString()
      : "";
  } catch {
    return "";
  }
}

function buildUserMessage(
  text: string,
  attachments: AttachmentItem[],
): ILantaiChatMessage {
  const id = generateId("user");
  const content: Record<string, unknown>[] = [];
  if (text) content.push(textContent(text));
  content.push(...attachments.map(attachmentToContent));
  const roleContext = buildAttachmentRoleContext(attachments);
  if (roleContext) content.push(textContent(roleContext));
  if (content.length === 0) content.push(textContent(""));
  return {
    id,
    role: "user",
    createdAt: Date.now(),
    metadata: {
      client_turn_id: id,
      client_turn_id_origin: "optimistic_submission",
    },
    cards: [
      {
        code: REQUEST_CARD_CODE,
        data: {
          client_turn_id: id,
          client_turn_id_origin: "optimistic_submission",
          metadata: {
            client_turn_id: id,
            client_turn_id_origin: "optimistic_submission",
          },
          input: [
            {
              role: "user",
              type: "message",
              content,
              metadata: {
                client_turn_id: id,
                client_turn_id_origin: "optimistic_submission",
              },
            },
          ],
        },
      },
    ],
  };
}

function draftFromUserMessage(message: ILantaiChatMessage): SubmissionDraft {
  const cardData = message.cards?.[0]?.data || {};
  const input = message.cards?.[0]?.data?.input;
  const first = Array.isArray(input)
    ? (input[0] as Record<string, unknown> | undefined)
    : undefined;
  const content = Array.isArray(first?.content)
    ? (first.content as Record<string, unknown>[])
    : [];
  const rawText = content
    .filter((item) => item.type === LantaiRuntimeContentType.TEXT)
    .map((item) => String(item.text || ""))
    .filter(Boolean)
    .join("\n");
  const roleAssignments = parseAttachmentRoleContext(rawText);
  const text = stripAttachmentRoleContext(rawText);
  const attachments = content
    .filter((item) => item.type !== LantaiRuntimeContentType.TEXT)
    .map((item, index) => {
      const type = String(item.type || LantaiRuntimeContentType.FILE);
      const mime =
        type === LantaiRuntimeContentType.IMAGE
          ? "image/*"
          : type === LantaiRuntimeContentType.VIDEO
            ? "video/*"
            : type === LantaiRuntimeContentType.AUDIO
              ? "audio/*"
              : "application/octet-stream";
      const name = String(
        item.file_name || item.fileName || item.filename || "附件",
      );
      const role = isLantaiAttachmentRole(item.attachment_role)
        ? item.attachment_role
        : roleAssignments.get(name) || DEFAULT_ATTACHMENT_ROLE;
      return {
        id: String(item.id || `${message.id}-attachment-${index}`),
        name,
        type: String(item.media_type || mime),
        size: Number(item.file_size || item.size || 0),
        url: String(
          item.image_url ||
            item.video_url ||
            item.file_url ||
            item.data ||
            item.url ||
            "",
        ),
        canonicalPath: String(item.file_source_url || ""),
        storedName: String(item.stored_name || ""),
        digest: String(item.attachment_digest || ""),
        immutableVersion: Number(item.attachment_version || 0) || undefined,
        sourceId: String(item.attachment_source_id || ""),
        sourceChatId: String(item.attachment_source_chat_id || ""),
        uploadReceipt: String(item.attachment_upload_receipt || ""),
        status: "ready" as const,
        role,
        roleConfirmed: true,
      };
    });
  return {
    text,
    attachments,
    controlIntent: runtimeControlIntent(message) || undefined,
    expectedRunId:
      String(cardData[RUNTIME_EXPECTED_RUN_ID_KEY] || "").trim() || undefined,
    expectedAttemptId:
      String(cardData[RUNTIME_EXPECTED_ATTEMPT_ID_KEY] || "").trim() ||
      undefined,
    replacesCommandId:
      String(cardData[RUNTIME_REPLACES_COMMAND_ID_KEY] || "").trim() ||
      undefined,
    replacesMessageId:
      String(cardData[RUNTIME_REPLACES_MESSAGE_ID_KEY] || "").trim() ||
      undefined,
  };
}

function confirmAttachmentRoleReview(
  attachments: AttachmentItem[],
): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: boolean) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    Modal.confirm({
      title: "确认附件用途",
      width: 460,
      centered: true,
      okText: "确认并发送",
      cancelText: "返回调整",
      maskClosable: true,
      keyboard: true,
      content: React.createElement(
        "div",
        { className: "lantai-attachment-role-confirm" },
        React.createElement(
          "p",
          null,
          "澜台会按这些用途处理文件，避免把原稿误当成普通参考资料。",
        ),
        React.createElement(
          "ul",
          null,
          attachments.map((attachment) =>
            React.createElement(
              "li",
              { key: attachment.id },
              React.createElement("strong", null, attachment.name),
              React.createElement(
                "span",
                null,
                getAttachmentRoleLabel(attachment.role),
              ),
            ),
          ),
        ),
      ),
      onOk: () => finish(true),
      onCancel: () => finish(false),
      afterClose: () => finish(false),
    });
  });
}

async function copyPlainText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function getResponseCardData(
  message: ILantaiChatMessage,
): ILantaiRuntimeResponse | null {
  const card = (message.cards || []).find(
    (item) => item.code === RESPONSE_CARD_CODE,
  );
  return (card?.data as ILantaiRuntimeResponse | undefined) || null;
}

function normalizeStreamMessageType(type: unknown): string {
  const value = typeof type === "string" ? type : "";
  if (value === "reasoning") return LantaiRuntimeMessageType.REASONING;
  if (value === "error") return LantaiRuntimeMessageType.ERROR;
  if (
    value === LantaiRuntimeMessageType.PLUGIN_CALL ||
    value === LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT ||
    value === LantaiRuntimeMessageType.MCP_CALL ||
    value === LantaiRuntimeMessageType.MCP_CALL_OUTPUT ||
    value === LantaiRuntimeMessageType.MCP_APPROVAL_REQUEST ||
    value === LantaiRuntimeMessageType.HEARTBEAT
  ) {
    return value;
  }
  return LantaiRuntimeMessageType.MESSAGE;
}

function contentTypeForMessage(messageType: string): string {
  return messageType === LantaiRuntimeMessageType.REASONING
    ? LantaiRuntimeContentType.TEXT
    : LantaiRuntimeContentType.TEXT;
}

function emptyResponseData(): ILantaiRuntimeResponse {
  const now = Math.floor(Date.now() / 1000);
  return {
    id: `response_${generateId("stream")}`,
    object: "response",
    status: "in_progress",
    output: [],
    created_at: now,
    sequence_number: 0,
    error: null,
    completed_at: null,
    usage: null,
  };
}

function buildAssistantMessage(data = emptyResponseData()): ILantaiChatMessage {
  return {
    id: generateId("assistant"),
    role: "assistant",
    createdAt: data.created_at,
    cards: [{ code: RESPONSE_CARD_CODE, data }],
    msgStatus: data.status === "completed" ? "finished" : "loading",
  };
}

function buildOptimisticAssistantMessage(): ILantaiChatMessage {
  const data = emptyResponseData();
  data.metadata = {
    ...(data.metadata || {}),
    // This marker exists only in the local optimistic transcript. Persisted
    // history never recreates it, so a completed runtime from the previous
    // turn cannot hide the new receipt while the backend assigns a run_id.
    optimistic_pending: true,
  };
  return buildAssistantMessage(data);
}

function cloneResponseData(
  data: ILantaiRuntimeResponse,
): ILantaiRuntimeResponse {
  return {
    ...data,
    output: Array.isArray(data.output)
      ? data.output.map((item) => ({
          ...item,
          content: Array.isArray(item.content)
            ? item.content.map((block) =>
                block && typeof block === "object" ? { ...block } : block,
              )
            : item.content,
          metadata: item.metadata ? { ...item.metadata } : item.metadata,
        }))
      : data.output,
    metadata: data.metadata ? { ...data.metadata } : data.metadata,
    error:
      data.error && typeof data.error === "object"
        ? { ...data.error }
        : data.error,
  };
}

function activeAssistantMessageIndex(messages: ILantaiChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "assistant") continue;
    const data = getResponseCardData(message);
    if (
      data &&
      (data.status === "in_progress" ||
        data.status === "running" ||
        data.status === "created")
    ) {
      return index;
    }
  }
  return -1;
}

function activeAssistantMessageIndexWithoutTurnIdentity(
  messages: ILantaiChatMessage[],
): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "assistant") continue;
    const data = getResponseCardData(message);
    if (data && isActiveRuntimeStatus(data.status) && !responseTurnKey(data)) {
      return index;
    }
  }
  return -1;
}

function assistantMessageIndexForTurn(
  messages: ILantaiChatMessage[],
  turnKey: string,
): number {
  if (!turnKey) return -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "assistant") continue;
    const data = getResponseCardData(message);
    if (data && responseTurnKey(data) === turnKey) return index;
  }
  return -1;
}

const RUNTIME_TURN_IDENTITY_KEYS = [
  "client_turn_id",
  "native_runtime_turn_id",
  "turn_id",
  "run_id",
  "attempt_id",
] as const;

function runtimeTurnKeyFromMetadata(metadata: unknown): string {
  if (!metadata || typeof metadata !== "object") return "";
  const record = metadata as Record<string, unknown>;
  for (const key of RUNTIME_TURN_IDENTITY_KEYS) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function runtimeTurnKeysFromMetadata(metadata: unknown): Set<string> {
  const keys = new Set<string>();
  if (!metadata || typeof metadata !== "object") return keys;
  const record = metadata as Record<string, unknown>;
  for (const key of RUNTIME_TURN_IDENTITY_KEYS) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) keys.add(value.trim());
  }
  return keys;
}

function streamPayloadTurnKey(payload: Record<string, unknown>): string {
  return runtimeTurnKeyFromMetadata(payload.metadata);
}

function runtimeOutputTurnKey(output: unknown): string {
  if (!output || typeof output !== "object") return "";
  return runtimeTurnKeyFromMetadata(
    (output as Record<string, unknown>).metadata,
  );
}

function attachStreamTurnIdentity(
  data: ILantaiRuntimeResponse,
  payload: Record<string, unknown>,
): void {
  const metadata = payload.metadata;
  if (!metadata || typeof metadata !== "object") return;
  const incoming = metadata as Record<string, unknown>;
  data.metadata = {
    ...(data.metadata || {}),
    ...incoming,
  };
}

function upsertAssistantMessage(
  messages: ILantaiChatMessage[],
  turnKey = "",
): {
  messages: ILantaiChatMessage[];
  data: ILantaiRuntimeResponse;
} {
  // One native provider stream may be queued behind another. Its earliest
  // control envelope must bind to that run's own response card, never simply
  // append to whichever assistant message happens to be active last.
  const exactTurnIndex = assistantMessageIndexForTurn(messages, turnKey);
  const activeIndex =
    exactTurnIndex >= 0
      ? exactTurnIndex
      : turnKey
        ? activeAssistantMessageIndexWithoutTurnIdentity(messages)
        : activeAssistantMessageIndex(messages);
  if (activeIndex >= 0) {
    const activeMessage = messages[activeIndex];
    const data = getResponseCardData(activeMessage)!;
    const nextData = cloneResponseData(data);
    const nextMessage = {
      ...activeMessage,
      cards: (activeMessage.cards || []).map((card) =>
        card.code === RESPONSE_CARD_CODE ? { ...card, data: nextData } : card,
      ),
    };
    const nextMessages = [...messages];
    nextMessages[activeIndex] = nextMessage;
    return {
      messages: nextMessages,
      data: nextData,
    };
  }
  const next = [...messages, buildAssistantMessage()];
  return {
    messages: next,
    data: getResponseCardData(next[next.length - 1]) || emptyResponseData(),
  };
}

function findOrCreateOutput(
  data: ILantaiRuntimeResponse,
  payload: Record<string, unknown>,
): ILantaiRuntimeMessage {
  if (!Array.isArray(data.output)) data.output = [];
  const id = String(payload.id || payload.msg_id || generateId("message"));
  let output = data.output.find((item) => item.id === id);
  if (!output) {
    const messageType = normalizeStreamMessageType(payload.type);
    output = {
      id,
      object: "message",
      status: String(payload.status || "in_progress"),
      type: messageType,
      role: String(payload.role || "assistant"),
      content: [
        {
          object: "content",
          status: String(payload.status || "in_progress"),
          type: contentTypeForMessage(messageType),
          index: 0,
          text: "",
        },
      ],
      metadata: {},
    };
    data.output.push(output);
  }
  return output;
}

function applyMessagePayload(
  data: ILantaiRuntimeResponse,
  payload: Record<string, unknown>,
): void {
  const output = findOrCreateOutput(data, payload);
  output.status = String(payload.status || output.status || "in_progress");
  output.type = normalizeStreamMessageType(payload.type || output.type);
  output.role = String(payload.role || output.role || "assistant");
  if (payload.metadata && typeof payload.metadata === "object") {
    output.metadata = {
      ...(output.metadata || {}),
      ...(payload.metadata as Record<string, unknown>),
    };
  }
  if (Array.isArray(payload.content)) {
    output.content = payload.content as Array<Record<string, unknown>>;
  }
  if (!Array.isArray(output.content)) {
    output.content = [
      {
        object: "content",
        status: output.status,
        type: contentTypeForMessage(String(output.type)),
        index: 0,
        text: "",
      },
    ];
  }
}

function applyContentPayload(
  data: ILantaiRuntimeResponse,
  payload: Record<string, unknown>,
): void {
  const output = findOrCreateOutput(data, {
    id: payload.msg_id || payload.id,
    status: payload.status,
    // Content frames describe their block as `text`, while the preceding
    // message frame owns whether that text is reasoning or an answer.
    type: payload.message_type || payload.type,
  });
  const content = Array.isArray(output.content) ? output.content : [];
  const first =
    content[0] ||
    ({
      object: "content",
      status: "in_progress",
      type: contentTypeForMessage(String(output.type)),
      index: 0,
      text: "",
    } as Record<string, unknown>);
  const delta = payload.delta === true;
  const text = String(payload.text || payload.thinking || "");
  first.text = delta ? `${String(first.text || "")}${text}` : text;
  first.status = String(payload.status || "in_progress");
  output.content = [first, ...content.slice(1)];
  output.status = String(payload.status || "in_progress");
}

function streamPayloadMessageId(payload: Record<string, unknown>): string {
  return String(payload.msg_id || payload.id || "").trim();
}

function streamPayloadText(payload: Record<string, unknown>): string {
  const direct = payload.text || payload.thinking;
  if (typeof direct === "string" && direct) return direct;
  const content = Array.isArray(payload.content) ? payload.content : [];
  return content
    .map((item) => {
      if (!item || typeof item !== "object") return "";
      const block = item as Record<string, unknown>;
      return String(block.text || block.thinking || "");
    })
    .join("");
}

function visibleStreamPayloadType(payload: Record<string, unknown>): string {
  return normalizeStreamMessageType(payload.message_type || payload.type);
}

function payloadHasVisibleAssistantFeedback(
  payload: Record<string, unknown>,
): boolean {
  const status = String(payload.status || "").toLowerCase();
  if (status === "failed" || payload.error) return true;
  if (streamPayloadText(payload).trim()) return true;
  const output = Array.isArray(payload.output) ? payload.output : [];
  return output.some(
    (item) =>
      item &&
      typeof item === "object" &&
      runtimeMessageText(item as Record<string, unknown>).trim().length > 0,
  );
}

function recordChatStreamProbe(frame: Record<string, unknown>): void {
  if (typeof window === "undefined") return;
  const probe = (
    window as Window & {
      __lantaiChatStreamProbe?: {
        startedAt?: number;
        frames?: Array<Record<string, unknown>>;
      };
    }
  ).__lantaiChatStreamProbe;
  if (!probe || !Array.isArray(probe.frames)) return;
  const startedAt =
    typeof probe.startedAt === "number" ? probe.startedAt : performance.now();
  probe.frames.push({
    at_ms: Math.max(0, Math.round(performance.now() - startedAt)),
    ...frame,
  });
  if (probe.frames.length > 120) probe.frames.shift();
}

function waitForThinkingPresentation(
  delayMs: number,
  signal?: AbortSignal,
): Promise<void> {
  if (delayMs <= 0 || signal?.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, delayMs);
    signal?.addEventListener("abort", finish, { once: true });
  });
}

function thinkingPresentationSnapshot(): Map<Element, string> {
  if (typeof document === "undefined") return new Map();
  return new Map(
    Array.from(document.querySelectorAll(COMMITTED_THINKING_SELECTOR)).map(
      (element) => [element, thinkingElementSignature(element)],
    ),
  );
}

function waitForCommittedThinkingFrame(
  previousThinking: ReadonlyMap<Element, string>,
  signal?: AbortSignal,
): Promise<void> {
  if (
    signal?.aborted ||
    typeof document === "undefined" ||
    typeof MutationObserver === "undefined"
  ) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    let settled = false;
    let observer: MutationObserver | null = null;
    let holdTimer: ReturnType<typeof setTimeout> | null = null;
    let fallbackTimer: ReturnType<typeof setTimeout> | null = null;

    const finish = () => {
      if (settled) return;
      settled = true;
      observer?.disconnect();
      if (holdTimer) clearTimeout(holdTimer);
      if (fallbackTimer) clearTimeout(fallbackTimer);
      signal?.removeEventListener("abort", finish);
      resolve();
    };
    const commitIfVisible = () => {
      if (settled) return;
      const currentThinking = Array.from(
        document.querySelectorAll(COMMITTED_THINKING_SELECTOR),
      );
      const currentTurnCommitted = currentThinking.some((element) => {
        const previous = previousThinking.get(element);
        const current = thinkingElementSignature(element);
        return previous === undefined || previous !== current;
      });
      if (!currentTurnCommitted) return;
      observer?.disconnect();
      observer = null;
      if (!holdTimer) {
        // Hold the answer after React commits this turn's Thinking node so
        // the intermediate state remains observable in Electron's renderer.
        holdTimer = setTimeout(finish, MIN_COMMITTED_THINKING_HOLD_MS);
      }
    };

    observer = new MutationObserver(commitIfVisible);
    observer.observe(document.documentElement, {
      attributes: true,
      characterData: true,
      childList: true,
      subtree: true,
    });
    fallbackTimer = setTimeout(finish, MAX_THINKING_COMMIT_WAIT_MS);
    signal?.addEventListener("abort", finish, { once: true });
    commitIfVisible();
  });
}

function waitForPendingAssistantPaint(): Promise<void> {
  if (
    typeof window === "undefined" ||
    typeof window.requestAnimationFrame !== "function"
  ) {
    return new Promise((resolve) => setTimeout(resolve, 0));
  }
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      window.clearTimeout(fallback);
      resolve();
    };
    const fallback = window.setTimeout(finish, 80);
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(finish);
    });
  });
}

const DELIVERED_ARTIFACT_TYPES = new Set(["file", "image", "audio", "video"]);

function containsDeliveredArtifact(value: unknown, depth = 0): boolean {
  if (depth > 5) return false;
  if (Array.isArray(value)) {
    return value.some((item) => containsDeliveredArtifact(item, depth + 1));
  }
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  const type = String(record.type || "").toLowerCase();
  if (DELIVERED_ARTIFACT_TYPES.has(type)) {
    const source = record.source;
    if (
      record.file_url ||
      record.image_url ||
      record.audio_url ||
      record.video_url ||
      record.url ||
      record.data ||
      (source && typeof source === "object")
    ) {
      return true;
    }
  }
  return [record.content, record.output, record.result].some((item) =>
    containsDeliveredArtifact(item, depth + 1),
  );
}

function failedPayloadMessage(payload: Record<string, unknown>): string {
  const error = payload.error;
  if (typeof error === "string" && error.trim()) return error.trim();
  if (error && typeof error === "object") {
    const errorRecord = error as Record<string, unknown>;
    const nested = errorRecord.message || errorRecord.detail;
    if (typeof nested === "string" && nested.trim()) return nested.trim();
  }
  const message = payload.message || payload.detail;
  return typeof message === "string" && message.trim()
    ? message.trim()
    : "请求失败";
}

function normalizeFailedResponse(
  payload: Record<string, unknown>,
  partialSuccess = false,
) {
  const failure = failedPayloadMessage(payload);
  const text = partialSuccess
    ? `部分完成：已生成的产物已保留。后续步骤失败：${failure}`
    : failure;
  return {
    id: String(payload.id || generateId("error")),
    object: "message",
    status: "completed",
    type: LantaiRuntimeMessageType.ERROR,
    role: "assistant",
    metadata: partialSuccess ? { partial_success: true } : undefined,
    content: [
      {
        type: LantaiRuntimeContentType.TEXT,
        text,
        status: "completed",
      },
    ],
  };
}

function runtimeMessageText(message: Record<string, unknown>): string {
  const content = Array.isArray(message.content) ? message.content : [];
  return content
    .map((item) =>
      item && typeof item === "object"
        ? String((item as Record<string, unknown>).text || "")
        : "",
    )
    .join("");
}

function isTerminalRuntimeStatus(status: unknown): boolean {
  return status === "completed" || status === "failed";
}

function responseOutcomeStatus(payload: Record<string, unknown>): string {
  const metadata =
    payload.metadata && typeof payload.metadata === "object"
      ? (payload.metadata as Record<string, unknown>)
      : {};
  return String(metadata.outcome_status || payload.outcome_status || "")
    .trim()
    .toLowerCase();
}

function hasIncompleteResponseOutcome(
  payload: Record<string, unknown>,
): boolean {
  return ["incomplete", "needs_input", "blocked"].includes(
    responseOutcomeStatus(payload),
  );
}

function isActiveRuntimeStatus(status: unknown): boolean {
  const normalized = String(status || "")
    .trim()
    .toLowerCase();
  return (
    normalized === "in_progress" ||
    normalized === "running" ||
    normalized === "created" ||
    normalized === "stopping"
  );
}

function isSettledRuntimeStatus(status: unknown): boolean {
  return [
    "completed",
    "finished",
    "failed",
    "interrupted",
    "cancelled",
    "canceled",
    "stalled",
  ].includes(
    String(status || "")
      .trim()
      .toLowerCase(),
  );
}

function isStoppedRuntimeStatus(status: unknown): boolean {
  return ["interrupted", "cancelled", "canceled"].includes(
    String(status || "")
      .trim()
      .toLowerCase(),
  );
}

function sessionRuntimeStatus(session?: ILantaiChatSession): unknown {
  const runtimeState = session?.runtimeState;
  return runtimeState && typeof runtimeState === "object"
    ? (runtimeState as Record<string, unknown>).status
    : undefined;
}

function runtimeControlTarget(session?: ILantaiChatSession): {
  expectedRunId?: string;
  expectedAttemptId?: string;
} {
  const runtimeState = session?.runtimeState;
  if (!runtimeState || typeof runtimeState !== "object") return {};
  const state = runtimeState as Record<string, unknown>;
  const runId = typeof state.run_id === "string" ? state.run_id.trim() : "";
  const attemptId =
    typeof state.attempt_id === "string" ? state.attempt_id.trim() : "";
  return {
    ...(runId ? { expectedRunId: runId } : {}),
    ...(attemptId ? { expectedAttemptId: attemptId } : {}),
  };
}

function sessionHasDurableRuntimeActivity(
  session?: ILantaiChatSession,
): boolean {
  return isActiveRuntimeStatus(sessionRuntimeStatus(session));
}

function sessionHasActiveRun(session?: ILantaiChatSession): boolean {
  return Boolean(
    session?.__primaryStreamActive === true ||
    isActiveRuntimeStatus(session?.status) ||
    sessionHasDurableRuntimeActivity(session),
  );
}

function runtimeStateTurnKeys(session?: ILantaiChatSession): Set<string> {
  const runtimeState = session?.runtimeState;
  return runtimeState && typeof runtimeState === "object"
    ? runtimeTurnKeysFromMetadata(runtimeState)
    : new Set<string>();
}

function preserveLocallySettledRuntimeState(
  previous: ILantaiChatSession | undefined,
  incoming: ILantaiChatSession,
  snapshotRequestedAt?: number,
): ILantaiChatSession {
  const snapshotPredatesActivePrimaryStream = Boolean(
    previous?.__primaryStreamActive === true &&
    typeof snapshotRequestedAt === "number" &&
    typeof previous.__primaryStreamStartedAt === "number" &&
    previous.__primaryStreamStartedAt >= snapshotRequestedAt,
  );
  const incomingIsAuthoritativeTerminal = Boolean(
    incoming.__primaryStreamActive === false ||
    isSettledRuntimeStatus(sessionRuntimeStatus(incoming)),
  );
  if (
    previous?.__primaryStreamActive === true &&
    !incomingIsAuthoritativeTerminal &&
    (sessionHasActiveRun(incoming) || snapshotPredatesActivePrimaryStream)
  ) {
    const previousTarget = runtimeControlTarget(previous);
    const incomingTarget = runtimeControlTarget(incoming);
    const incomingIdentityDoesNotConflict = Boolean(
      (!incomingTarget.expectedRunId ||
        incomingTarget.expectedRunId === previousTarget.expectedRunId) &&
      (!incomingTarget.expectedAttemptId ||
        incomingTarget.expectedAttemptId === previousTarget.expectedAttemptId),
    );
    if (
      hasCompleteRuntimeControlTarget(previousTarget) &&
      !hasCompleteRuntimeControlTarget(incomingTarget) &&
      incomingIdentityDoesNotConflict
    ) {
      // The initial SSE frame is the first authoritative owner of the active
      // Run/Attempt. A route hydration requested before that frame can resolve
      // later with a still-running snapshot that predates the identity. Keep
      // the local identity while accepting the snapshot's other runtime data.
      return {
        ...incoming,
        runtimeState: {
          ...(incoming.runtimeState && typeof incoming.runtimeState === "object"
            ? (incoming.runtimeState as Record<string, unknown>)
            : {}),
          run_id: previousTarget.expectedRunId,
          attempt_id: previousTarget.expectedAttemptId,
        },
      };
    }
  }

  const snapshotPredatesLocalSettlement = Boolean(
    previous &&
    typeof snapshotRequestedAt === "number" &&
    typeof previous.__primaryStreamSettledAt === "number" &&
    previous.__primaryStreamSettledAt >= snapshotRequestedAt,
  );
  if (
    !previous ||
    previous.__primaryStreamActive !== false ||
    (!isSettledRuntimeStatus(sessionRuntimeStatus(previous)) &&
      !snapshotPredatesLocalSettlement) ||
    !sessionHasActiveRun(incoming)
  ) {
    return incoming;
  }

  // Cancel acknowledgement is authoritative for the current turn. A list or
  // hydration request already in flight can still return its older `running`
  // snapshot after the acknowledgement. Runtime identity fields are opaque
  // transport aliases and can rotate while one logical turn is settling. A
  // different attempt id is therefore not ordering evidence. Stay settled
  // until a local submission explicitly marks this session active again.
  const runtimeState =
    snapshotPredatesLocalSettlement &&
    !isSettledRuntimeStatus(sessionRuntimeStatus(previous))
      ? {
          ...(previous.runtimeState && typeof previous.runtimeState === "object"
            ? (previous.runtimeState as Record<string, unknown>)
            : {}),
          status: "interrupted",
          can_reconnect: false,
          can_stop: false,
        }
      : previous.runtimeState;
  return {
    ...incoming,
    generating: false,
    status: "idle",
    runtimeState,
    __primaryStreamActive: false,
    __primaryStreamSettledAt: previous.__primaryStreamSettledAt || Date.now(),
  };
}

function hasNativeRuntimeStreamMarker(data: ILantaiRuntimeResponse): boolean {
  if (data.metadata?.native_runtime_stream === true) return true;
  const output = Array.isArray(data.output) ? data.output : [];
  return output.some((item) => item?.metadata?.native_runtime_stream === true);
}

function responseTurnKey(data: ILantaiRuntimeResponse): string {
  const metadata = data.metadata || {};
  const output = Array.isArray(data.output) ? data.output : [];
  // Identity precedence applies across the whole response, not merely inside
  // the response envelope. Persisted history can carry one shared run_id on
  // the envelope while each queued output owns a distinct client_turn_id. If
  // we return that shared run_id before inspecting output metadata, separate
  // queued turns reconcile into the wrong assistant card.
  for (const key of RUNTIME_TURN_IDENTITY_KEYS) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    for (const item of output) {
      const itemMetadata = item?.metadata || {};
      const value = itemMetadata[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  return "";
}

function responseClientTurnKey(data: ILantaiRuntimeResponse): string {
  const metadata = data.metadata || {};
  const responseValue = metadata.client_turn_id;
  if (typeof responseValue === "string" && responseValue.trim()) {
    return responseValue.trim();
  }
  const output = Array.isArray(data.output) ? data.output : [];
  for (const item of output) {
    const value = item?.metadata?.client_turn_id;
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function responseTransportRunKeys(data: ILantaiRuntimeResponse): Set<string> {
  const keys = new Set<string>();
  const collect = (metadata: Record<string, unknown> | undefined) => {
    for (const key of ["run_id", "turn_id"] as const) {
      const value = metadata?.[key];
      if (typeof value === "string" && value.trim()) keys.add(value.trim());
    }
  };
  collect(data.metadata);
  for (const output of Array.isArray(data.output) ? data.output : []) {
    collect(output?.metadata);
  }
  return keys;
}

function responsesShareTransportRun(
  previous: ILantaiRuntimeResponse,
  incoming: ILantaiRuntimeResponse,
): boolean {
  const previousKeys = responseTransportRunKeys(previous);
  return Array.from(responseTransportRunKeys(incoming)).some((key) =>
    previousKeys.has(key),
  );
}

function runtimeOutputText(output: ILantaiRuntimeMessage): string {
  return runtimeMessageText(output as Record<string, unknown>).trim();
}

function runtimeOutputSourceMessageId(output: ILantaiRuntimeMessage): string {
  const metadata = output.metadata || {};
  for (const key of [
    "source_message_id",
    "native_runtime_logical_source_id",
    "native_runtime_source_id",
  ] as const) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function canonicalRuntimeOutputSourceMessageId(
  output: ILantaiRuntimeMessage,
): string {
  let sourceId = runtimeOutputSourceMessageId(output);
  const usesOutputIdFallback =
    !sourceId && Boolean(String(output.id || "").trim());
  if (usesOutputIdFallback) sourceId = String(output.id || "").trim();
  if (!sourceId) return "";
  // The direct Legacy converter carries the Runtime event/item identity,
  // whereas the ledger mirror carries native_runtime_stream. Both namespace
  // narrative source ids; persisted provider ids must remain untouched.
  const isDirectRuntimeEvent =
    ["message.delta", "message.completed"].includes(
      String(output.metadata?.runtime_event_type || ""),
    ) && output.metadata?.runtime_item_id === sourceId;
  if (
    output.metadata?.native_runtime_stream !== true &&
    !isDirectRuntimeEvent &&
    !usesOutputIdFallback
  ) {
    return sourceId;
  }
  const turnKey = runtimeOutputTurnKey(output);
  if (turnKey && sourceId.startsWith(`${turnKey}:`)) {
    sourceId = sourceId.slice(turnKey.length + 1);
  }
  const suffix =
    narrativeOutputKind(output) === "reasoning" ? ":reasoning" : ":message";
  return sourceId.endsWith(suffix)
    ? sourceId.slice(0, -suffix.length)
    : sourceId;
}

function runtimeOutputSourceKey(output: ILantaiRuntimeMessage): string {
  // The low-latency runtime stream namespaces narrative items as
  // ``<provider-id>:message``/``:reasoning`` while durable history stores the
  // provider id itself. They are aliases for one logical output, not two
  // user-visible answers. Normalize only the explicitly marked live side so
  // unrelated persisted ids that happen to end with a type word stay apart.
  const sourceId = canonicalRuntimeOutputSourceMessageId(output);
  const kind = narrativeOutputKind(output);
  const turnKey = runtimeOutputTurnKey(output);
  return sourceId && kind ? `${kind}\u001e${turnKey}\u001e${sourceId}` : "";
}

function runtimeOutputSourceKeys(output: ILantaiRuntimeMessage): string[] {
  const kind = narrativeOutputKind(output);
  const turnKey = runtimeOutputTurnKey(output);
  const rawSourceId =
    runtimeOutputSourceMessageId(output) || String(output.id || "").trim();
  const exactKey =
    rawSourceId && kind ? `${kind}\u001e${turnKey}\u001e${rawSourceId}` : "";
  const canonicalKey = runtimeOutputSourceKey(output);
  const outputType = String(output.type || "").toLowerCase();
  const toolPhase =
    outputType === "plugin_call" || outputType === "function_call"
      ? "started"
      : outputType === "plugin_call_output" ||
          outputType === "function_call_output"
        ? "completed"
        : "";
  const toolCallId = (Array.isArray(output.content) ? output.content : [])
    .map((item) => {
      if (!item || typeof item !== "object") return "";
      const data = (item as Record<string, unknown>).data;
      if (!data || typeof data !== "object") return "";
      const record = data as Record<string, unknown>;
      return String(record.call_id || record.id || "").trim();
    })
    .find(Boolean);
  // Direct SSE and the durable Runtime ledger can assign different row ids to
  // the same tool phase.  The provider call id is the protocol identity; keep
  // started/completed separate and scope it to the logical user turn so a
  // later call cannot collapse into an earlier card after history hydration.
  const toolKey =
    toolPhase && toolCallId
      ? `tool\u001e${toolPhase}\u001e${turnKey}\u001e${toolCallId}`
      : "";
  return Array.from(new Set([exactKey, canonicalKey, toolKey].filter(Boolean)));
}

function comparableNarrativeText(text: string): string {
  return text.replace(/^\s*~>\s*\[echo\]\s*/i, "").trim();
}

function isExplicitNarrativeEcho(text: string): boolean {
  return /^\s*~>\s*\[echo\]/i.test(text);
}

function dedupeRuntimeContent(
  output: ILantaiRuntimeMessage,
): ILantaiRuntimeMessage["content"] {
  if (!Array.isArray(output.content)) return output.content;
  const seenText = new Set<string>();
  return output.content.flatMap((item) => {
    if (!item || typeof item !== "object") return [item];
    const clone = { ...item };
    const record = clone as Record<string, unknown>;
    if (typeof record.text !== "string" || !record.text.trim()) return [clone];
    const key = `${String(record.type || "text")}\u001e${record.text.trim()}`;
    if (seenText.has(key)) return [];
    seenText.add(key);
    return [clone];
  }) as ILantaiRuntimeMessage["content"];
}

function hasEquivalentStreamedAnswer(
  previous: ILantaiRuntimeResponse,
  incoming: ILantaiRuntimeResponse,
): boolean {
  // The native stream and the persisted transcript can legitimately use
  // different response/run ids. Only relax that identity boundary when one
  // side is known to be the live renderer stream and both sides contain an
  // exact, non-empty final answer. This keeps separate queued turns apart.
  if (
    !hasNativeRuntimeStreamMarker(previous) &&
    !hasNativeRuntimeStreamMarker(incoming)
  ) {
    return false;
  }
  const previousSources = new Set(
    (previous.output || []).flatMap(runtimeOutputSourceKeys),
  );
  return (incoming.output || []).some((output) => {
    return runtimeOutputSourceKeys(output).some((sourceKey) =>
      previousSources.has(sourceKey),
    );
  });
}

function isReasoningOutput(output: ILantaiRuntimeMessage): boolean {
  const type = String(output.type || "").toLowerCase();
  if (type === "reasoning" || type === "thinking") return true;
  const content = Array.isArray(output.content) ? output.content : [];
  return content.some((item) => {
    if (!item || typeof item !== "object") return false;
    const contentType = String(
      (item as Record<string, unknown>).type || "",
    ).toLowerCase();
    return contentType === "reasoning" || contentType === "thinking";
  });
}

function narrativeOutputKind(
  output: ILantaiRuntimeMessage,
): "reasoning" | "answer" | "" {
  const type = String(output.type || "").toLowerCase();
  if (type === "reasoning" || type === "thinking") return "reasoning";
  if (type === "message") return "answer";
  return "";
}

function hasOnlyReasoningOutput(data: ILantaiRuntimeResponse): boolean {
  const output = Array.isArray(data.output) ? data.output : [];
  const visibleOutput = output.filter((item) => runtimeOutputText(item));
  return visibleOutput.length > 0 && visibleOutput.every(isReasoningOutput);
}

function hasAnswerOutput(data: ILantaiRuntimeResponse): boolean {
  const output = Array.isArray(data.output) ? data.output : [];
  return output.some(
    (item) => runtimeOutputText(item) && !isReasoningOutput(item),
  );
}

function cloneRuntimeOutput(
  output: ILantaiRuntimeMessage,
): ILantaiRuntimeMessage {
  return {
    ...output,
    content: dedupeRuntimeContent(output),
    metadata: output.metadata ? { ...output.metadata } : output.metadata,
  };
}

const RUNTIME_OUTPUT_ALIAS_IDS_KEY = "runtime_output_alias_ids";

function runtimeOutputAliasIds(output: ILantaiRuntimeMessage): string[] {
  const value = output.metadata?.[RUNTIME_OUTPUT_ALIAS_IDS_KEY];
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item || "").trim()).filter(Boolean);
}

function runtimeOutputIdentityIds(output: ILantaiRuntimeMessage): string[] {
  return Array.from(
    new Set(
      [String(output.id || "").trim(), ...runtimeOutputAliasIds(output)].filter(
        Boolean,
      ),
    ),
  );
}

function mergeRuntimeOutput(
  previous: ILantaiRuntimeMessage,
  incoming: ILantaiRuntimeMessage,
): ILantaiRuntimeMessage {
  const previousText = runtimeMessageText(previous as Record<string, unknown>);
  const incomingText = runtimeMessageText(incoming as Record<string, unknown>);
  const incomingRegressesTerminal =
    isTerminalRuntimeStatus(previous.status) &&
    isActiveRuntimeStatus(incoming.status);
  const previousComparableText = comparableNarrativeText(previousText);
  const incomingComparableText = comparableNarrativeText(incomingText);
  const isEquivalentEcho =
    previousComparableText.length > 0 &&
    previousComparableText === incomingComparableText;
  const previousIsEcho = /^\s*~>\s*\[echo\]/i.test(previousText);
  const incomingIsEcho = /^\s*~>\s*\[echo\]/i.test(incomingText);
  const previousIsExactSnapshotReplay =
    incomingText.length > 0 &&
    previousText.length > incomingText.length &&
    previousText.length % incomingText.length === 0 &&
    previousText ===
      incomingText.repeat(previousText.length / incomingText.length);
  const incomingIsStrictTerminalPrefix =
    isTerminalRuntimeStatus(incoming.status) &&
    incomingText.length < previousText.length &&
    previousText.startsWith(incomingText) &&
    !previousIsExactSnapshotReplay;
  const preferIncomingContent =
    !incomingRegressesTerminal &&
    !incomingIsStrictTerminalPrefix &&
    (!isEquivalentEcho || previousIsEcho || !incomingIsEcho) &&
    (incomingText.length >= previousText.length ||
      isTerminalRuntimeStatus(incoming.status));
  const preferred = preferIncomingContent ? incoming : previous;
  const fallback = preferIncomingContent ? previous : incoming;
  const status =
    incoming.status === "failed" || previous.status === "failed"
      ? "failed"
      : isTerminalRuntimeStatus(incoming.status)
        ? incoming.status
        : isTerminalRuntimeStatus(previous.status)
          ? previous.status
          : isActiveRuntimeStatus(incoming.status) ||
              isActiveRuntimeStatus(previous.status)
            ? "in_progress"
            : incoming.status || previous.status;
  const aliasIds = Array.from(
    new Set([
      ...runtimeOutputIdentityIds(previous),
      ...runtimeOutputIdentityIds(incoming),
    ]),
  );
  return {
    ...fallback,
    ...preferred,
    status,
    content: dedupeRuntimeContent(preferred),
    metadata: {
      ...(previous.metadata || {}),
      ...(incoming.metadata || {}),
      ...(aliasIds.length > 1
        ? { [RUNTIME_OUTPUT_ALIAS_IDS_KEY]: aliasIds }
        : {}),
    },
  };
}

export function mergeRuntimeOutputs(
  previousOutput?: ILantaiRuntimeMessage[],
  incomingOutput?: ILantaiRuntimeMessage[],
): ILantaiRuntimeMessage[] {
  const merged: ILantaiRuntimeMessage[] = [];
  const indexById = new Map<string, number>();
  const indexBySource = new Map<string, number>();
  const indexOutputIdentities = (
    output: ILantaiRuntimeMessage,
    index: number,
  ) => {
    for (const identity of runtimeOutputIdentityIds(output)) {
      indexById.set(identity, index);
    }
    for (const sourceKey of runtimeOutputSourceKeys(output)) {
      indexBySource.set(sourceKey, index);
    }
  };
  const append = (output: ILantaiRuntimeMessage, incoming: boolean) => {
    if (!output || typeof output !== "object") return;
    const outputText = runtimeOutputText(output);
    const outputKind = narrativeOutputKind(output);
    const outputComparableText = comparableNarrativeText(outputText);
    if (outputKind && outputComparableText) {
      const outputIsEcho = isExplicitNarrativeEcho(outputText);
      const equivalentIndex = merged.findIndex((candidate) => {
        if (narrativeOutputKind(candidate) !== outputKind) return false;
        const candidateText = runtimeOutputText(candidate);
        return (
          isExplicitNarrativeEcho(candidateText) !== outputIsEcho &&
          comparableNarrativeText(candidateText) === outputComparableText
        );
      });
      if (equivalentIndex >= 0) {
        // ``~>[echo]`` is the explicit Ququ/persona side-channel marker. It is
        // valuable only when it adds a distinct line. If it repeats the main
        // answer verbatim, suppress that side-channel row; this does not use
        // text equality to infer generic message identity.
        if (outputIsEcho) return;
        const previousEcho = merged[equivalentIndex];
        merged[equivalentIndex] = cloneRuntimeOutput(output);
        if (previousEcho?.id) indexById.delete(String(previousEcho.id));
        for (const previousSourceKey of previousEcho
          ? runtimeOutputSourceKeys(previousEcho)
          : []) {
          indexBySource.delete(previousSourceKey);
        }
        indexOutputIdentities(output, equivalentIndex);
        return;
      }
    }
    const id = String(output.id || "");
    if (id && indexById.has(id)) {
      const index = indexById.get(id)!;
      merged[index] = mergeRuntimeOutput(merged[index], output);
      // A direct content delta can create the row before message metadata is
      // available.  When a later frame enriches that same output id with its
      // source_message_id, index the newly learned logical identity as well.
      // Otherwise the durable Native mirror can arrive next with a different
      // provider id and be appended as a second visible answer.
      indexOutputIdentities(merged[index], index);
      return;
    }
    const matchingSourceKey = runtimeOutputSourceKeys(output).find(
      (sourceKey) => indexBySource.has(sourceKey),
    );
    if (matchingSourceKey) {
      const index = indexBySource.get(matchingSourceKey)!;
      merged[index] = mergeRuntimeOutput(merged[index], output);
      indexOutputIdentities(merged[index], index);
      return;
    }
    const narrativeKind = outputKind;
    if (incoming && narrativeKind) {
      const emptyCandidates = merged
        .map((candidate, index) => ({ candidate, index }))
        .filter(
          ({ candidate }) =>
            narrativeOutputKind(candidate) === narrativeKind &&
            !runtimeOutputText(candidate) &&
            !runtimeOutputSourceMessageId(candidate),
        );
      // An empty anonymous loading shell has no user content to lose and may
      // safely be claimed by the first identified stream output. Once a row
      // contains visible text, ids/source_message_id are mandatory for merge.
      if (emptyCandidates.length === 1) {
        const index = emptyCandidates[0].index;
        merged[index] = mergeRuntimeOutput(merged[index], output);
        indexOutputIdentities(merged[index], index);
        return;
      }
    }
    const clone = cloneRuntimeOutput(output);
    const index = merged.length;
    merged.push(clone);
    indexOutputIdentities(clone, index);
  };
  for (const output of Array.isArray(previousOutput) ? previousOutput : []) {
    append(output, false);
  }
  for (const output of Array.isArray(incomingOutput) ? incomingOutput : []) {
    append(output, true);
  }
  return merged;
}

/**
 * A visible completed answer is an append-only timeline anchor. A terminal
 * history snapshot may own ordering only after it contains every such anchor;
 * otherwise letting the snapshot lead would move or erase narration that the
 * user already saw between tool phases.
 */
function incomingPreservesCompletedNarrationTimeline(
  previousOutput?: ILantaiRuntimeMessage[],
  incomingOutput?: ILantaiRuntimeMessage[],
): boolean {
  const previousCompletedNarration = (
    Array.isArray(previousOutput) ? previousOutput : []
  ).filter(
    (output) =>
      narrativeOutputKind(output) === "answer" &&
      output.status === "completed" &&
      Boolean(runtimeOutputText(output).trim()),
  );
  if (previousCompletedNarration.length === 0) return true;

  const incoming = Array.isArray(incomingOutput) ? incomingOutput : [];
  const claimedIncomingIndices = new Set<number>();
  return previousCompletedNarration.every((previous) => {
    const previousIds = new Set(runtimeOutputIdentityIds(previous));
    const previousSources = new Set(runtimeOutputSourceKeys(previous));
    const previousText = comparableNarrativeText(runtimeOutputText(previous));
    const matchIndex = incoming.findIndex((candidate, index) => {
      if (claimedIncomingIndices.has(index)) return false;
      if (narrativeOutputKind(candidate) !== "answer") return false;
      if (
        runtimeOutputIdentityIds(candidate).some((id) => previousIds.has(id))
      ) {
        return true;
      }
      if (
        runtimeOutputSourceKeys(candidate).some((source) =>
          previousSources.has(source),
        )
      ) {
        return true;
      }
      return Boolean(
        previousText &&
        comparableNarrativeText(runtimeOutputText(candidate)) === previousText,
      );
    });
    if (matchIndex < 0) return false;
    claimedIncomingIndices.add(matchIndex);
    return true;
  });
}

function mergeResponseData(
  previous: ILantaiRuntimeResponse,
  incoming: ILantaiRuntimeResponse,
  orderOwner: "previous" | "incoming" = "previous",
): ILantaiRuntimeResponse {
  // Incremental stream snapshots are appended to the live order. A durable
  // session snapshot is different: its output array is the canonical turn
  // ledger, so it must own row positions while the live copy may still enrich
  // content/status. Without this distinction, a race that first rendered
  // narrative rows and only later received tools became permanently sticky.
  const effectiveOrderOwner =
    orderOwner === "incoming" &&
    !incomingPreservesCompletedNarrationTimeline(
      previous.output,
      incoming.output,
    )
      ? "previous"
      : orderOwner;
  const output =
    effectiveOrderOwner === "incoming"
      ? mergeRuntimeOutputs(incoming.output, previous.output)
      : mergeRuntimeOutputs(previous.output, incoming.output);
  const hasFailedOutput = output.some((item) => item.status === "failed");
  const hasRunningOutput = output.some((item) =>
    isActiveRuntimeStatus(item.status),
  );
  const status =
    previous.status === "failed" ||
    incoming.status === "failed" ||
    hasFailedOutput
      ? "failed"
      : hasRunningOutput
        ? "in_progress"
        : output.length > 0
          ? "completed"
          : incoming.status || previous.status;
  return {
    ...previous,
    ...incoming,
    id: previous.id || incoming.id,
    status,
    output,
    metadata: {
      ...(previous.metadata || {}),
      ...(incoming.metadata || {}),
    },
    error: incoming.error ?? previous.error ?? null,
    completed_at:
      status === "completed"
        ? incoming.completed_at ||
          previous.completed_at ||
          Math.floor(Date.now() / 1000)
        : incoming.completed_at || previous.completed_at || null,
  };
}

function mergeAssistantResponseMessages(
  previous: ILantaiChatMessage,
  incoming: ILantaiChatMessage,
  orderOwner: "previous" | "incoming" = "previous",
): ILantaiChatMessage {
  const previousData = getResponseCardData(previous)!;
  const incomingData = getResponseCardData(incoming)!;
  const mergedData = mergeResponseData(previousData, incomingData, orderOwner);
  const previousCards = previous.cards || [];
  const incomingCards = incoming.cards || [];
  const otherCards = [
    ...previousCards.filter((card) => card.code !== RESPONSE_CARD_CODE),
    ...incomingCards.filter((card) => card.code !== RESPONSE_CARD_CODE),
  ];
  return {
    ...previous,
    ...incoming,
    id: previous.id || incoming.id,
    role: "assistant",
    cards: [
      ...otherCards,
      {
        code: RESPONSE_CARD_CODE,
        data: mergedData,
      },
    ],
    msgStatus:
      mergedData.status === "completed" || mergedData.status === "failed"
        ? "finished"
        : "loading",
  };
}

function shouldMergeAssistantResponses(
  previous: ILantaiChatMessage,
  incoming: ILantaiChatMessage,
): boolean {
  if (previous.role !== "assistant" || incoming.role !== "assistant") {
    return false;
  }
  const previousData = getResponseCardData(previous);
  const incomingData = getResponseCardData(incoming);
  if (!previousData || !incomingData) return false;
  const previousTurnKey = responseTurnKey(previousData);
  const incomingTurnKey = responseTurnKey(incomingData);
  // A renderer stream can close its reasoning output before the final answer
  // is committed. Those two adjacent cards are still one assistant turn even
  // when the transport assigned different run ids to the two phases. An
  // explicit client turn identity is stronger than that structural fallback:
  // reasoning for one queued turn must never absorb the next turn's answer.
  if (hasOnlyReasoningOutput(previousData) && hasAnswerOutput(incomingData)) {
    const previousClientTurnKey = responseClientTurnKey(previousData);
    const incomingClientTurnKey = responseClientTurnKey(incomingData);
    if (
      previousClientTurnKey &&
      incomingClientTurnKey &&
      previousClientTurnKey !== incomingClientTurnKey
    ) {
      return false;
    }
    return true;
  }
  const previousClientTurnKey = responseClientTurnKey(previousData);
  const incomingClientTurnKey = responseClientTurnKey(incomingData);
  if (
    previousClientTurnKey &&
    incomingClientTurnKey &&
    previousClientTurnKey !== incomingClientTurnKey
  ) {
    return false;
  }
  // Legacy/provider-specific tool rows can lose client_turn_id while still
  // retaining the Console run identity. Keep an adjacent fragment inside the
  // same visible assistant turn unless both sides explicitly identify
  // different logical client turns (handled above). This is the renderer's
  // final defence after the Runtime and Console protocol layers.
  if (responsesShareTransportRun(previousData, incomingData)) return true;
  // A run identity is a hard response boundary. The previous rule only kept
  // two identified runs apart, so an identified queued run could still merge
  // into an older untagged loading card and make two turns look like one.
  if (previousTurnKey || incomingTurnKey) {
    return (
      previousTurnKey === incomingTurnKey ||
      hasEquivalentStreamedAnswer(previousData, incomingData)
    );
  }
  return (
    hasNativeRuntimeStreamMarker(previousData) ||
    hasNativeRuntimeStreamMarker(incomingData) ||
    isActiveRuntimeStatus(previousData.status) ||
    isActiveRuntimeStatus(incomingData.status)
  );
}

function mergeAdjacentAssistantResponseCards(
  messages: ILantaiChatMessage[],
): ILantaiChatMessage[] {
  let touched = false;
  const merged: ILantaiChatMessage[] = [];
  for (const message of messages) {
    const previous = merged[merged.length - 1];
    if (previous && shouldMergeAssistantResponses(previous, message)) {
      merged[merged.length - 1] = mergeAssistantResponseMessages(
        previous,
        message,
      );
      touched = true;
      continue;
    }
    merged.push(message);
  }
  return touched ? merged : messages;
}

const transcriptReconciler = createTranscriptReconciler({
  responseData: getResponseCardData,
  responseTurnKey,
  responseClientTurnKey,
  hasEquivalentStreamedAnswer,
  shouldMergeAssistantResponses,
  mergeAssistantResponses: mergeAssistantResponseMessages,
  normalizeAdjacentAssistantResponses: mergeAdjacentAssistantResponseCards,
  isActiveStatus: isActiveRuntimeStatus,
  isTerminalStatus: isTerminalRuntimeStatus,
});

function normalizeSessionMessages(
  session: ILantaiChatSession,
): ILantaiChatSession {
  if (!Array.isArray(session.messages)) return session;
  const messages = mergeAdjacentAssistantResponseCards(session.messages);
  return messages === session.messages ? session : { ...session, messages };
}

function normalizeSessionsMessages(
  sessions: ILantaiChatSession[],
): ILantaiChatSession[] {
  let touched = false;
  const next = sessions.map((session) => {
    const normalized = normalizeSessionMessages(session);
    if (normalized !== session) touched = true;
    return normalized;
  });
  return touched ? next : sessions;
}

function normalizedSessionIdentity(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function sessionAuthoritativeIds(session: ILantaiChatSession): Set<string> {
  return new Set(
    [session.id, session.realId].map(normalizedSessionIdentity).filter(Boolean),
  );
}

function sessionsShareAuthoritativeIdentity(
  left: ILantaiChatSession,
  right: ILantaiChatSession,
): boolean {
  const leftIds = sessionAuthoritativeIds(left);
  return Array.from(sessionAuthoritativeIds(right)).some((id) =>
    leftIds.has(id),
  );
}

function uniqueSessionAliasOwner(
  sessions: ILantaiChatSession[],
  alias: string,
): ILantaiChatSession | undefined {
  if (!alias) return undefined;
  const matches = sessions.filter(
    (session) => normalizedSessionIdentity(session.sessionId) === alias,
  );
  return matches.length === 1 ? matches[0] : undefined;
}

function findSameSessionForStreamPreservation(
  activeSessions: ILantaiChatSession[],
  previous: ILantaiChatSession[],
  incoming: ILantaiChatSession[],
  target: ILantaiChatSession,
): ILantaiChatSession | undefined {
  const authoritativeMatch = activeSessions.find((candidate) =>
    sessionsShareAuthoritativeIdentity(candidate, target),
  );
  if (authoritativeMatch) return authoritativeMatch;

  // A local optimistic id can be replaced by a backend id after the first
  // response. The channel session id is a valid migration bridge only when it
  // uniquely identifies one record on both sides. Workspace-scoped aliases
  // may be shared by several chats and must never transfer message ownership.
  const alias = normalizedSessionIdentity(target.sessionId);
  const previousOwner = uniqueSessionAliasOwner(previous, alias);
  const incomingOwner = uniqueSessionAliasOwner(incoming, alias);
  if (!previousOwner || incomingOwner !== target) return undefined;
  return activeSessions.includes(previousOwner) ? previousOwner : undefined;
}

function findSessionByStableIdentity(
  sessions: ILantaiChatSession[],
  sessionId: string | undefined,
): ILantaiChatSession | undefined {
  const normalized = normalizedSessionIdentity(sessionId);
  if (!normalized) return undefined;
  const exact = sessions.find(
    (session) => normalizedSessionIdentity(session.id) === normalized,
  );
  if (exact) return exact;

  const authoritative = sessions.filter(
    (session) => normalizedSessionIdentity(session.realId) === normalized,
  );
  if (authoritative.length === 1) return authoritative[0];

  const aliases = sessions.filter(
    (session) => normalizedSessionIdentity(session.sessionId) === normalized,
  );
  return aliases.length === 1 ? aliases[0] : undefined;
}

function preservePrimaryStreamOwnership(
  previous: ILantaiChatSession[],
  incoming: ILantaiChatSession[],
): ILantaiChatSession[] {
  const activeSessions = previous.filter(
    (session) => session.__primaryStreamActive === true,
  );
  let touched = false;
  const next = incoming.map((session) => {
    const activeSession = findSameSessionForStreamPreservation(
      activeSessions,
      previous,
      incoming,
      session,
    );
    if (!activeSession) return session;

    const messages = chooseActiveSessionMessages(
      activeSession.messages || [],
      session.messages || [],
    );
    const alreadyPreserved =
      session.__primaryStreamActive === true &&
      session.generating === true &&
      session.status === "running" &&
      messages === session.messages;
    if (alreadyPreserved) return session;

    touched = true;
    return {
      ...session,
      messages,
      __preserveMessages: true,
      __primaryStreamActive: true,
      generating: true,
      status: "running",
    };
  });
  return touched ? next : incoming;
}

function preserveLocallyStoppedRuntimeStates(
  previous: ILantaiChatSession[],
  incoming: ILantaiChatSession[],
): ILantaiChatSession[] {
  let touched = false;
  const next = incoming.map((session) => {
    const previousSession = findSameSessionForStreamPreservation(
      previous,
      previous,
      incoming,
      session,
    );
    if (!isStoppedRuntimeStatus(sessionRuntimeStatus(previousSession))) {
      return session;
    }
    const preserved = preserveLocallySettledRuntimeState(
      previousSession,
      session,
    );
    if (preserved !== session) touched = true;
    return preserved;
  });
  return touched ? next : incoming;
}

function appendFailedResponseOutput(
  data: ILantaiRuntimeResponse,
  payload: Record<string, unknown>,
): void {
  if (!Array.isArray(data.output)) data.output = [];
  const failed = normalizeFailedResponse(
    payload,
    containsDeliveredArtifact(data.output),
  );
  const failedText = runtimeMessageText(failed);
  const hasSameError = data.output.some(
    (item) =>
      item.type === LantaiRuntimeMessageType.ERROR &&
      runtimeMessageText(item as Record<string, unknown>) === failedText,
  );
  if (!hasSameError) data.output.push(failed);
}

function isDedupedClientTurnResponse(
  payload: Record<string, unknown>,
): boolean {
  const metadata = payload.metadata as Record<string, unknown> | undefined;
  return (
    payload.object === "response" &&
    payload.status === "completed" &&
    metadata?.deduped_client_turn === true &&
    Array.isArray(payload.output) &&
    payload.output.length === 0
  );
}

function finalizeStreamedOutputs(
  data: ILantaiRuntimeResponse,
  status: "completed" | "failed",
): void {
  if (!Array.isArray(data.output)) data.output = [];
  for (const item of data.output) {
    if (!item || typeof item !== "object") continue;
    if (item.status !== "completed" && item.status !== "failed") {
      item.status = status;
    }
    if (!Array.isArray(item.content)) continue;
    item.content = item.content.map((block) => {
      if (!block || typeof block !== "object") return block;
      const next = { ...block };
      if (next.status !== "completed" && next.status !== "failed") {
        next.status = status;
      }
      return next;
    });
  }
}

function isEmptyLiveStreamEnvelope(payload: Record<string, unknown>): boolean {
  const object = String(payload.object || "");
  const status = String(payload.status || "").toLowerCase();
  if (status === "completed" || status === "failed") return false;
  if (object === "response") {
    return (
      (!Array.isArray(payload.output) || payload.output.length === 0) &&
      !payload.error
    );
  }
  if (object !== "message") return false;
  const content = Array.isArray(payload.content) ? payload.content : [];
  return content.length === 0;
}

function splitCumulativeResponseByLogicalTurn(
  payload: Record<string, unknown>,
): Array<{ turnKey: string; payload: Record<string, unknown> }> {
  if (payload.object !== "response" || !Array.isArray(payload.output)) {
    return [];
  }
  const identifiedTurnKeys = new Set(
    payload.output.map(runtimeOutputTurnKey).filter(Boolean),
  );
  if (identifiedTurnKeys.size <= 1) return [];

  const fallbackTurnKey = streamPayloadTurnKey(payload);
  const groups = new Map<string, unknown[]>();
  for (const output of payload.output) {
    const turnKey = runtimeOutputTurnKey(output) || fallbackTurnKey;
    if (!turnKey) return [];
    const group = groups.get(turnKey) || [];
    group.push(output);
    groups.set(turnKey, group);
  }
  if (groups.size <= 1) return [];

  const responseMetadata =
    payload.metadata && typeof payload.metadata === "object"
      ? (payload.metadata as Record<string, unknown>)
      : {};
  return [...groups.entries()].map(([turnKey, output]) => {
    const firstMetadata =
      output[0] && typeof output[0] === "object"
        ? ((output[0] as Record<string, unknown>).metadata as
            | Record<string, unknown>
            | undefined)
        : undefined;
    return {
      turnKey,
      payload: {
        ...payload,
        output,
        metadata: {
          ...responseMetadata,
          ...(firstMetadata || {}),
          // The grouped output owns the logical response card. Preserve the
          // transport run aliases, but never let the terminal envelope's last
          // client id steal an earlier queued answer.
          client_turn_id: turnKey,
        },
      },
    };
  });
}

function applyStreamPayload(
  current: ILantaiChatMessage[],
  payload: Record<string, unknown>,
): ILantaiChatMessage[] {
  if (!payload || typeof payload !== "object") return current;
  if (isDedupedClientTurnResponse(payload)) {
    const activeIndex = activeAssistantMessageIndex(current);
    const activeMessage = activeIndex >= 0 ? current[activeIndex] : undefined;
    const activeResponse = activeMessage
      ? getResponseCardData(activeMessage)
      : null;
    if (
      activeMessage?.role === "assistant" &&
      activeResponse &&
      ["created", "in_progress", "running"].includes(
        String(activeResponse.status || ""),
      ) &&
      (!Array.isArray(activeResponse.output) ||
        activeResponse.output.length === 0)
    ) {
      return current.filter((_, index) => index !== activeIndex);
    }
    return current;
  }
  const cumulativeResponseGroups =
    splitCumulativeResponseByLogicalTurn(payload);
  if (cumulativeResponseGroups.length > 1) {
    return cumulativeResponseGroups.reduce(
      (messages, group) => applyStreamPayload(messages, group.payload),
      current,
    );
  }
  const turnKey = streamPayloadTurnKey(payload);
  // Keep the initial running envelope. It binds the direct-stream placeholder
  // to a concrete run immediately, which gives the renderer a visible loading
  // card before a slow provider produces its first token.
  const { messages, data } = upsertAssistantMessage([...current], turnKey);
  attachStreamTurnIdentity(data, payload);
  const object = String(payload.object || "");
  // The optimistic marker describes local delivery only. Once the backend
  // owns this response, including an empty first-output envelope, it must not
  // keep masking reasoning, tool activity, or authored public progress.
  if (STREAM_PAYLOAD_OBJECTS.has(object) && data.metadata) {
    delete data.metadata.optimistic_pending;
  }
  if (isEmptyLiveStreamEnvelope(payload)) {
    // ``message.started`` is intentionally invisible, but it is not
    // identity-free.  Persist its output id and metadata before the first
    // content delta arrives.  If the delta creates an anonymous output, the
    // durable Runtime mirror cannot recognise it as the same logical answer
    // and a timing race briefly renders both copies.
    if (object === "message") applyMessagePayload(data, payload);
    return messages;
  }

  if (object === "response") {
    const nextData = data;
    // Response snapshots commonly contain only the newest tool call. Replacing
    // the card output here used to erase reasoning that had already streamed
    // and been visible to the user. Merge every snapshot by output identity so
    // thinking, tool calls, and the eventual answer remain one durable turn.
    Object.assign(
      nextData,
      mergeResponseData(nextData, payload as unknown as ILantaiRuntimeResponse),
    );
    if (!Array.isArray(nextData.output)) nextData.output = [];
    if (payload.status === "failed") {
      appendFailedResponseOutput(nextData, payload);
      finalizeStreamedOutputs(nextData, "failed");
    }
    if (payload.status === "completed" && nextData.output.length === 0) {
      nextData.status = "failed";
      nextData.type = "response.failed";
      nextData.error = {
        message: "后端完成了请求但没有返回可显示内容",
      };
      appendFailedResponseOutput(nextData, nextData);
    } else if (payload.status === "completed") {
      finalizeStreamedOutputs(nextData, "completed");
      if (hasIncompleteResponseOutcome(payload)) {
        // The transport is settled, but the task is not fully delivered. Keep
        // completed artifacts intact while preventing the response footer from
        // contradicting the run/outcome facts with a green "completed" label.
        nextData.status = "failed";
        nextData.type = "response.failed";
      }
    }
  } else if (object === "message") {
    applyMessagePayload(data, payload);
  } else if (object === "content") {
    applyContentPayload(data, payload);
  }

  // Two live owners can report the same logical answer before the turn-level
  // response settles: the low-latency provider stream and the durable runtime
  // mirror. Provider ids are transport details, so exact visible duplicates
  // must collapse in the same render commit instead of flashing twice and
  // relying on a later history hydration to repair the card.
  data.output = mergeRuntimeOutputs([], data.output);

  const anyRunning = (data.output || []).some((item) =>
    isActiveRuntimeStatus(item.status),
  );
  data.status =
    data.status === "failed"
      ? "failed"
      : anyRunning
        ? "in_progress"
        : data.status === "completed"
          ? "completed"
          : "in_progress";

  for (const message of messages) {
    const cardData = getResponseCardData(message);
    if (cardData === data) {
      message.msgStatus =
        data.status === "completed" || data.status === "failed"
          ? "finished"
          : "loading";
    }
  }
  return messages;
}

function parseSsePayloads(buffer: string): {
  payloads: string[];
  rest: string;
} {
  const chunks = buffer.split(/\n\n|\r\n\r\n/);
  const rest = chunks.pop() || "";
  const payloads = chunks
    .map((chunk) =>
      chunk
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n")
        .trim(),
    )
    .filter(Boolean);
  return { payloads, rest };
}

const STREAM_PAYLOAD_OBJECTS = new Set(["response", "message", "content"]);
const STREAM_CONTROL_PAYLOAD_OBJECTS = new Set([
  "turn_receipt",
  "runtime_progress",
]);

function initialRuntimeControlIdentity(
  payload: Record<string, unknown>,
): { runId: string; attemptId: string } | null {
  if (
    String(payload.object || "") !== "response" ||
    String(payload.status || "").toLowerCase() !== "in_progress"
  ) {
    return null;
  }
  const metadata =
    payload.metadata && typeof payload.metadata === "object"
      ? (payload.metadata as Record<string, unknown>)
      : null;
  if (!metadata || metadata.initial_status !== true) return null;
  const receipt =
    metadata.turn_receipt && typeof metadata.turn_receipt === "object"
      ? (metadata.turn_receipt as Record<string, unknown>)
      : null;
  const runId = String(metadata.run_id || receipt?.run_id || "").trim();
  const attemptId = String(receipt?.attempt_id || "").trim();
  if (!runId || !attemptId) return null;
  const receiptRunId = String(receipt?.run_id || "").trim();
  if (receiptRunId && receiptRunId !== runId) return null;
  return { runId, attemptId };
}

export function applyRuntimeControlIdentityFrame(
  sessions: ILantaiChatSession[],
  targetSessionId: string,
  payload: Record<string, unknown>,
): ILantaiChatSession[] {
  const identity = initialRuntimeControlIdentity(payload);
  if (!identity) return sessions;
  let changed = false;
  const next = sessions.map((session) => {
    if (
      session.id !== targetSessionId &&
      String(session.sessionId || "") !== targetSessionId
    ) {
      return session;
    }
    const previousRuntimeState =
      session.runtimeState && typeof session.runtimeState === "object"
        ? (session.runtimeState as Record<string, unknown>)
        : {};
    if (
      previousRuntimeState.run_id === identity.runId &&
      previousRuntimeState.attempt_id === identity.attemptId
    ) {
      return session;
    }
    changed = true;
    return {
      ...session,
      runtimeState: {
        ...previousRuntimeState,
        run_id: identity.runId,
        attempt_id: identity.attemptId,
        ...(session.generating === true &&
        previousRuntimeState.run_id !== identity.runId
          ? { status: "running" }
          : {}),
      },
    };
  });
  return changed ? next : sessions;
}

export function applyRuntimeProgressFrame(
  sessions: ILantaiChatSession[],
  targetSessionId: string,
  payload: Record<string, unknown>,
  receivedAt = new Date().toISOString(),
): ILantaiChatSession[] {
  if (String(payload.object || "") !== "runtime_progress") return sessions;
  const metadata =
    payload.metadata && typeof payload.metadata === "object"
      ? (payload.metadata as Record<string, unknown>)
      : {};
  const heartbeatAt =
    typeof metadata.last_update_at === "string" && metadata.last_update_at
      ? metadata.last_update_at
      : receivedAt;
  let changed = false;
  const next = sessions.map((session) => {
    if (
      session.id !== targetSessionId &&
      String(session.sessionId || "") !== targetSessionId
    ) {
      return session;
    }
    const previousRuntimeState =
      session.runtimeState && typeof session.runtimeState === "object"
        ? (session.runtimeState as Record<string, unknown>)
        : {};
    const previousRuntimeStatus = String(
      previousRuntimeState.status || "",
    ).toLowerCase();
    if (
      [
        "completed",
        "failed",
        "interrupted",
        "stalled",
        "cancelled",
        "finished",
      ].includes(previousRuntimeStatus)
    ) {
      return session;
    }
    changed = true;
    return {
      ...session,
      generating: true,
      status: "running",
      runtimeState: {
        ...previousRuntimeState,
        status: "running",
        updated_at: heartbeatAt,
        can_reconnect: true,
        can_stop: true,
        transport_status: "connected",
        transport_error: null,
      },
    };
  });
  return changed ? next : sessions;
}

function safeStreamProtocolToken(value: unknown): string {
  if (typeof value !== "string") return "";
  const token = value.trim();
  return /^[a-zA-Z0-9_.:-]{1,80}$/.test(token) ? token : "<invalid>";
}

function streamProtocolDiagnostic(raw: string): Record<string, unknown> {
  try {
    const payload = JSON.parse(raw) as unknown;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return { raw_kind: "non_object_json", raw_length: raw.length };
    }
    const record = payload as Record<string, unknown>;
    const keys = Object.keys(record)
      .filter((key) => /^[a-zA-Z0-9_.:-]{1,80}$/.test(key))
      .sort()
      .slice(0, 24)
      .join(",");
    return {
      raw_kind: "json_object",
      raw_length: raw.length,
      object: safeStreamProtocolToken(record.object),
      type: safeStreamProtocolToken(record.type),
      status: safeStreamProtocolToken(record.status),
      keys,
    };
  } catch {
    return { raw_kind: "invalid_json", raw_length: raw.length };
  }
}

function parseRawStreamPayload(
  raw: string,
  parser?: (text: string) => unknown,
): Record<string, unknown> {
  const payload = parser ? parser(raw) : JSON.parse(raw);
  if (!payload || typeof payload !== "object") {
    throw new Error("响应事件格式无效");
  }
  const record = payload as Record<string, unknown>;
  const object = String(record.object || "");
  if (
    !STREAM_PAYLOAD_OBJECTS.has(object) &&
    !STREAM_CONTROL_PAYLOAD_OBJECTS.has(object)
  ) {
    throw new Error("响应事件格式无效");
  }
  return record;
}

function sessionIdFromSession(session?: ILantaiChatSession | null): string {
  if (!session) return "";
  return String(session.sessionId || session.id || "");
}

export function chooseActiveSessionMessages(
  current: ILantaiChatMessage[],
  incoming?: ILantaiChatMessage[],
): ILantaiChatMessage[] {
  return transcriptReconciler.choose(current, incoming);
}

export function reconcileActiveSessionProjection(
  current: ILantaiChatMessage[],
  incoming: ILantaiChatMessage[] | undefined,
  visibleMessagesBelongToActiveSession: boolean,
): ILantaiChatMessage[] {
  return transcriptReconciler.reconcileProjection(
    current,
    incoming,
    visibleMessagesBelongToActiveSession,
  );
}

export function visibleSessionOwnerMatches(
  visibleSessionIds: ReadonlySet<string>,
  canonicalActiveSessionId: unknown,
): boolean {
  const normalized =
    typeof canonicalActiveSessionId === "string"
      ? canonicalActiveSessionId.trim()
      : "";
  return Boolean(normalized && visibleSessionIds.has(normalized));
}

function UserCard({
  message,
  sourceChatId,
  disabled,
  onEdit,
  onBranch,
  onReplaceControlInput,
  onCancelControlInput,
}: {
  message: ILantaiChatMessage;
  sourceChatId?: string;
  disabled: boolean;
  onEdit: (message: ILantaiChatMessage) => void;
  onBranch: (message: ILantaiChatMessage) => void;
  onReplaceControlInput: (message: ILantaiChatMessage) => void;
  onCancelControlInput: (message: ILantaiChatMessage) => void;
}) {
  const [copied, setCopied] = useState(false);
  const data = message.cards?.[0]?.data || {};
  const input = Array.isArray(data.input) ? data.input : [];
  const first = input[0] as Record<string, unknown> | undefined;
  const content = Array.isArray(first?.content) ? first.content : [];
  const draft = draftFromUserMessage(message);
  const copyText =
    draft.text ||
    draft.attachments.map((attachment) => attachment.name).join("\n");
  const textItems = content
    .map((item, index) => ({
      index,
      text:
        (item as Record<string, unknown>).type === LantaiRuntimeContentType.TEXT
          ? stripAttachmentRoleContext(
              String((item as Record<string, unknown>).text || ""),
            )
          : "",
    }))
    .filter((item) => Boolean(item.text));
  const attachmentItems = content
    .map((item, index) => ({
      index,
      record: item as Record<string, unknown>,
    }))
    .filter((item) => item.record.type !== LantaiRuntimeContentType.TEXT);
  const queuedStatus = runtimeQueueStatus(message);
  const controlIntent = runtimeControlIntent(message);
  const waitingReason = runtimeControlWaitingReason(message);
  const canChangePendingControlInput =
    queuedStatus === "admitted" || queuedStatus === "claimed";
  const admittedLabel =
    controlIntent === "queue_next" || waitingReason === "awaiting_next_turn"
      ? "已接收 · 等待你启动下一任务"
      : controlIntent === "supplement"
        ? "已接收 · 等待当前任务安全边界"
        : controlIntent === "redirect"
          ? "已接收 · 等待安全让步"
          : "已接收 · 等待安全边界";
  const queuedLabel = queuedStatus
    ? {
        sending: "正在提交引导…",
        admitted: admittedLabel,
        claimed: "已接收 · 正在交给运行中任务",
        bound: "已绑定模型请求 · 等待模型响应",
        applied: "已应用到模型请求",
        steered:
          controlIntent === "supplement"
            ? "已补充当前任务"
            : controlIntent === "queue_next"
              ? "已排队下一任务"
              : "已调整当前方向",
        queued: "排队中 · 当前任务结束后发送",
        uncertain: "接收状态待确认 · 可安全重试",
        cancelled: "未应用 · 已取消",
        superseded: "未应用 · 已被后续改向取代",
        failed: "调整未完成 · 仍保留在队列",
      }[queuedStatus]
    : "";

  const copyMessage = async () => {
    if (!copyText) return;
    await copyPlainText(copyText);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  return React.createElement(
    "div",
    { className: "lantai-bubble lantai-bubble-end lantai-bubble-user" },
    React.createElement(
      "div",
      { className: "lantai-user-message-stack" },
      attachmentItems.length
        ? React.createElement(
            "div",
            {
              className: "lantai-user-attachment-stack",
              "aria-label": "用户附件",
            },
            attachmentItems.map(({ record, index }) => {
              const attachmentName = String(
                record.file_name ||
                  record.fileName ||
                  record.filename ||
                  (record.type === LantaiRuntimeContentType.IMAGE
                    ? "图片"
                    : "附件"),
              );
              const imageUrl = String(
                record.image_url ||
                  record.imageUrl ||
                  record.file_url ||
                  record.url ||
                  "",
              );
              if (record.type === LantaiRuntimeContentType.IMAGE && imageUrl) {
                return React.createElement(
                  "div",
                  {
                    key: index,
                    className: "lantai-user-image-attachment",
                  },
                  React.createElement(Image, {
                    src: imageUrl,
                    alt: attachmentName,
                    className: "lantai-user-image-preview",
                    preview: {
                      mask: "查看大图",
                    },
                  }),
                );
              }
              const fileSource = String(
                record.file_source_url || record.file_url || record.url || "",
              );
              const localFilePath = localAttachmentFilePath(fileSource);
              const localOfficePath =
                /\.(docx|xlsx|xls|csv|pptx|pdf|md|markdown)$/i.test(
                  attachmentName,
                )
                  ? localFilePath
                  : "";
              const localHtmlPath = /\.html?$/i.test(attachmentName)
                ? localFilePath
                : "";
              const remoteFileUrl = localFilePath
                ? ""
                : browserAttachmentUrl(fileSource);
              const attachmentProps = localOfficePath
                ? {
                    type: "button",
                    "aria-label": `在澜台中编辑：${attachmentName}`,
                    "data-office-editable": "true",
                    ...(/\.docx$/i.test(attachmentName)
                      ? { "data-docx-editable": "true" }
                      : {}),
                    onClick: () =>
                      void (
                        window.electronAPI?.openOfficeEditor ||
                        window.electronAPI?.openDocxEditor
                      )?.(localOfficePath, {
                        source_chat_id: sourceChatId || "",
                        agent_id: useAgentStore.getState().selectedAgent,
                      }),
                  }
                : localHtmlPath
                  ? {
                      type: "button",
                      "aria-label": `打开附件：${attachmentName}`,
                      onClick: () =>
                        void window.electronAPI?.openHtmlWork?.(
                          localHtmlPath,
                          attachmentName,
                        ),
                    }
                  : localFilePath
                    ? {
                        type: "button",
                        "aria-label": `打开附件：${attachmentName}`,
                        onClick: () =>
                          void window.electronAPI?.openPath?.(localFilePath),
                      }
                    : remoteFileUrl
                      ? {
                          type: "button",
                          "aria-label": `打开附件：${attachmentName}`,
                          onClick: () =>
                            void window.open(
                              remoteFileUrl,
                              "_blank",
                              "noopener,noreferrer",
                            ),
                        }
                      : {
                          role: "group",
                          "aria-label": `附件：${attachmentName}`,
                        };
              return React.createElement(
                localFilePath || remoteFileUrl ? "button" : "div",
                {
                  key: index,
                  className: "lantai-user-file-attachment",
                  "data-attachment-kind": "file",
                  ...attachmentProps,
                },
                React.createElement(
                  "span",
                  {
                    className: "lantai-user-file-attachment-icon",
                    "aria-hidden": true,
                  },
                  React.createElement(FileIcon, {
                    size: 18,
                    strokeWidth: 1.8,
                  }),
                ),
                React.createElement(
                  "span",
                  { className: "lantai-user-file-attachment-details" },
                  React.createElement(
                    "strong",
                    { className: "lantai-user-file-attachment-name" },
                    attachmentName,
                  ),
                  React.createElement(
                    "span",
                    { className: "lantai-user-file-attachment-meta" },
                    sentAttachmentMeta(record, attachmentName),
                  ),
                ),
              );
            }),
          )
        : null,
      textItems.length
        ? React.createElement(
            "div",
            { className: "lantai-bubble-content-filled" },
            textItems.map((item) =>
              React.createElement(
                "div",
                {
                  key: item.index,
                  className: "lantai-user-message-text",
                  style: { whiteSpace: "pre-wrap" },
                },
                item.text,
              ),
            ),
          )
        : null,
      queuedLabel
        ? React.createElement(
            "span",
            {
              className: "lantai-user-message-queue-status",
              "data-status": queuedStatus,
              role: "status",
            },
            queuedLabel,
          )
        : null,
      React.createElement(
        "div",
        {
          className: "lantai-user-message-actions",
          role: "group",
          "aria-label": "用户消息操作",
        },
        React.createElement(
          Tooltip,
          { title: copied ? "已复制" : "复制" },
          React.createElement(
            "button",
            {
              type: "button",
              className: "lantai-user-message-action",
              "aria-label": copied ? "已复制用户消息" : "复制用户消息",
              onClick: () => void copyMessage(),
            },
            React.createElement(copied ? Check : Copy, {
              size: 15,
              "aria-hidden": true,
            }),
          ),
        ),
        React.createElement(
          Tooltip,
          { title: disabled ? "请先停止当前生成" : "编辑" },
          React.createElement(
            "button",
            {
              type: "button",
              className: "lantai-user-message-action",
              "aria-label": "编辑用户消息",
              disabled,
              onClick: () => onEdit(message),
            },
            React.createElement(Pencil, {
              size: 15,
              "aria-hidden": true,
            }),
          ),
        ),
        React.createElement(
          Tooltip,
          { title: disabled ? "请先停止当前生成" : "重新发送" },
          React.createElement(
            "button",
            {
              type: "button",
              className: "lantai-user-message-action",
              "aria-label": "重试用户消息",
              disabled,
              onClick: () => onBranch(message),
            },
            React.createElement(RotateCcw, {
              size: 15,
              "aria-hidden": true,
            }),
          ),
        ),
        canChangePendingControlInput
          ? React.createElement(
              Tooltip,
              { title: "修改未应用引导" },
              React.createElement(
                "button",
                {
                  type: "button",
                  className: "lantai-user-message-action",
                  "aria-label": `修改未应用引导：${copyText || "附件引导"}`,
                  onClick: () => onReplaceControlInput(message),
                },
                React.createElement(Pencil, {
                  size: 15,
                  "aria-hidden": true,
                }),
              ),
            )
          : null,
        canChangePendingControlInput
          ? React.createElement(
              Tooltip,
              { title: "取消未应用引导" },
              React.createElement(
                "button",
                {
                  type: "button",
                  className: "lantai-user-message-action",
                  "aria-label": `取消未应用引导：${copyText || "附件引导"}`,
                  onClick: () => onCancelControlInput(message),
                },
                React.createElement(X, {
                  size: 15,
                  "aria-hidden": true,
                }),
              ),
            )
          : null,
      ),
    ),
  );
}

function runtimeControlFailureCopy(message: ILantaiChatMessage): string {
  switch (runtimeControlErrorCode(message)) {
    case "control_attachment_not_trusted":
      return "附件未被上传边界接受 · 请编辑并重新上传";
    case "control_attachment_grant_revoked":
      return "附件授权已失效 · 请重新授权或上传";
    case "authentication_required":
    case "http_401":
      return "登录已失效 · 请重新登录后再发送";
    case "desktop_token_required":
      return "桌面连接授权已失效 · 请重新连接后再发送";
    case "http_403":
      return "当前授权不允许发送 · 请重新授权后再发送";
    case "target_ended":
    case "run_not_active":
      return "当前任务已结束 · 内容已保留，未自动启动新任务";
    case "control_queue_full":
      return "引导队列已满 · 请先取消一条未应用引导";
    case "stale_attempt":
    case "target_mismatch":
      return "运行目标已变化 · 请检查后重新发送";
    case "invalid_control_input":
    case "empty_control_input":
      return "输入无法接收 · 请编辑后重试";
    default:
      return "未插入 · 仍排队";
  }
}

function QueuedGuidanceCard({
  message,
  position,
  total,
  onSubmitIntent,
  onEdit,
  onDelete,
}: {
  message: ILantaiChatMessage;
  position: number;
  total: number;
  onSubmitIntent: (
    message: ILantaiChatMessage,
    intent: ControlInputIntent,
  ) => void;
  onEdit: (message: ILantaiChatMessage) => void;
  onDelete: (message: ILantaiChatMessage) => void;
}) {
  const draft = draftFromUserMessage(message);
  const status = runtimeQueueStatus(message) || "queued";
  const label =
    draft.text ||
    (draft.attachments.length === 1
      ? draft.attachments[0]?.name || "附件引导"
      : `${draft.attachments.length} 个附件`);
  const statusCopy =
    runtimeControlWaitingReason(message) === "awaiting_run_identity"
      ? "等待 Run 身份绑定"
      : status === "sending"
        ? "正在提交…"
        : status === "uncertain"
          ? "接收待确认 · 可安全重试"
          : status === "failed"
            ? runtimeControlFailureCopy(message)
            : status === "admitted"
              ? runtimeControlIntent(message) === "queue_next"
                ? "已接收 · 等待下一任务"
                : runtimeControlIntent(message) === "supplement"
                  ? "已接收 · 等待当前任务安全边界"
                  : runtimeControlIntent(message) === "redirect"
                    ? "已接收 · 等待安全让步"
                    : "已接收 · 等待安全边界"
              : status === "claimed"
                ? "已接收 · 正在交给当前任务"
                : "等待当前任务";
  const busy = status === "sending";
  const currentIntent = runtimeControlIntent(message);
  const uncertainIntent =
    status === "uncertain" ? runtimeControlIntent(message) : null;
  const intentDisabled = (intent: ControlInputIntent) =>
    busy ||
    (status === "uncertain" && uncertainIntent !== intent) ||
    (["admitted", "claimed"].includes(status) && currentIntent === intent);
  const editingDisabled = busy || status === "uncertain";
  const menuItems = [
    ...(status === "uncertain" && uncertainIntent === "supplement"
      ? [
          {
            key: "supplement",
            label: "重试补充当前任务",
            disabled: intentDisabled("supplement"),
          },
        ]
      : []),
    {
      key: "queue_next",
      label:
        status === "uncertain" && uncertainIntent === "queue_next"
          ? "重试排队下一任务"
          : "排队下一任务",
      disabled: intentDisabled("queue_next"),
    },
    {
      key: "edit",
      label: "编辑",
      disabled: editingDisabled,
    },
  ];

  return React.createElement(
    "article",
    {
      className: "lantai-guidance-card",
      "data-guidance-status": status,
      "data-lantai-guidance-id": message.id,
      "aria-label": `引导 ${position + 1}：${label}`,
    },
    React.createElement(
      "div",
      { className: "lantai-guidance-card-copy" },
      React.createElement(CornerDownLeft, {
        size: 14,
        strokeWidth: 1.75,
        className: "lantai-guidance-mark",
        "aria-hidden": true,
      }),
      total > 1
        ? React.createElement(
            "span",
            { className: "lantai-guidance-position" },
            `${position + 1}/${total}`,
          )
        : null,
      React.createElement(
        "span",
        { className: "lantai-guidance-content", title: label },
        label,
      ),
      React.createElement(
        "span",
        {
          className: "lantai-guidance-status",
          role: "status",
          "aria-live": "polite",
        },
        busy
          ? React.createElement(LoaderCircle, {
              size: 12,
              className: "lantai-guidance-spinner",
              "aria-hidden": true,
            })
          : null,
        statusCopy,
      ),
    ),
    React.createElement(
      "div",
      {
        className: "lantai-guidance-actions",
        role: "group",
        "aria-label": `引导操作：${label}`,
      },
      React.createElement(
        "button",
        {
          type: "button",
          className: "lantai-guidance-steer lantai-guidance-intent",
          "aria-label": `调整方向：${label}`,
          disabled: intentDisabled("redirect"),
          onClick: () => onSubmitIntent(message, "redirect"),
        },
        React.createElement(CornerDownLeft, {
          size: 14,
          "aria-hidden": true,
        }),
        ["admitted", "claimed"].includes(status) && currentIntent === "redirect"
          ? "已调整方向"
          : status === "failed" ||
              (status === "uncertain" && uncertainIntent === "redirect")
            ? "重试调整方向"
            : "调整方向",
      ),
      React.createElement(
        Tooltip,
        {
          title: "删除未应用引导",
        },
        React.createElement(
          "button",
          {
            type: "button",
            className: "lantai-guidance-icon-action is-danger",
            "aria-label": `删除引导：${label}`,
            disabled: editingDisabled,
            onClick: () => onDelete(message),
          },
          React.createElement(Trash2, { size: 14, "aria-hidden": true }),
        ),
      ),
      React.createElement(
        Dropdown,
        {
          trigger: ["click"],
          placement: "bottomRight",
          menu: {
            items: menuItems,
            onClick: ({ key }) => {
              if (key === "edit") {
                onEdit(message);
                return;
              }
              onSubmitIntent(message, key as ControlInputIntent);
            },
          },
        },
        React.createElement(
          Tooltip,
          {
            title: "更多",
          },
          React.createElement(
            "button",
            {
              type: "button",
              className: "lantai-guidance-icon-action",
              "aria-label": `更多引导操作：${label}`,
              disabled: menuItems.every((item) => item.disabled),
            },
            React.createElement(MoreHorizontal, {
              size: 15,
              "aria-hidden": true,
            }),
          ),
        ),
      ),
    ),
  );
}

function renderCard(
  message: ILantaiChatMessage,
  options: ChatOptions,
  isLast: boolean,
  userActions: {
    disabled: boolean;
    onEdit: (message: ILantaiChatMessage) => void;
    onBranch: (message: ILantaiChatMessage) => void;
    onReplaceControlInput: (message: ILantaiChatMessage) => void;
    onCancelControlInput: (message: ILantaiChatMessage) => void;
  },
  sourceChatId?: string,
) {
  const cards = message.cards || [];
  if (cards.length === 0) return null;
  return cards.map((card, index) => {
    const key = `${message.id}:${card.code || index}`;
    if (card.code === REQUEST_CARD_CODE) {
      return React.createElement(UserCard, {
        key,
        message,
        sourceChatId,
        ...userActions,
      });
    }
    const Renderer = card.code ? options.cards?.[card.code] : undefined;
    if (Renderer) {
      return React.createElement(Renderer, {
        key,
        data: card.data,
        isLast,
      });
    }
    return React.createElement(
      "pre",
      { key, style: { whiteSpace: "pre-wrap" } },
      JSON.stringify(card.data || {}, null, 2),
    );
  });
}

function Markdown({
  content,
  cursor,
  components,
}: {
  content?: string;
  cursor?: boolean;
  raw?: boolean;
  components?: Record<string, ComponentType<any>>;
}) {
  const patchedComponents = useMemo(() => {
    const result: Record<string, ComponentType<any>> = {};
    for (const [key, Component] of Object.entries(components || {})) {
      result[key] = (props: Record<string, unknown>) =>
        React.createElement(Component, {
          ...props,
          domNode: props.node,
          streamStatus: cursor ? "streaming" : "done",
        });
    }
    const AnchorComponent = result.a;
    result.a = (props: Record<string, unknown>) => {
      if (props.href === STREAM_CURSOR_HREF) {
        return React.createElement("span", {
          className: "lantai-stream-cursor",
          "aria-hidden": "true",
        });
      }
      if (AnchorComponent) {
        return React.createElement(AnchorComponent, props);
      }
      const { node: _node, ...anchorProps } = props;
      return React.createElement("a", anchorProps);
    };
    return result;
  }, [components, cursor]);

  return React.createElement(
    "div",
    {
      className: "lantai-markdown",
      "data-streaming": cursor ? "true" : "false",
    },
    React.createElement(
      ReactMarkdown,
      { remarkPlugins: [remarkGfm], components: patchedComponents },
      `${content || ""}${cursor ? STREAM_CURSOR_MARKDOWN : ""}`,
    ),
  );
}

function MediaImages({ data }: { data: Array<Record<string, unknown>> }) {
  return React.createElement(
    "div",
    { className: "lantai-media-grid" },
    data.map((item, index) =>
      React.createElement(Image, {
        key: index,
        src: String(item.url || item.src || ""),
        alt: String(item.name || ""),
        style: { maxWidth: 280, borderRadius: 8 },
      }),
    ),
  );
}

function MediaVideos({ data }: { data: Array<Record<string, unknown>> }) {
  return React.createElement(
    "div",
    { className: "lantai-media-grid" },
    data.map((item, index) =>
      React.createElement("video", {
        key: index,
        src: String(item.src || item.url || ""),
        poster: item.poster ? String(item.poster) : undefined,
        controls: true,
        style: { maxWidth: 320, borderRadius: 8 },
      }),
    ),
  );
}

function MediaAudios({ data }: { data: Array<Record<string, unknown>> }) {
  return React.createElement(
    "div",
    { className: "lantai-media-list" },
    data.map((item, index) =>
      React.createElement("audio", {
        key: index,
        src: String(item.src || item.url || ""),
        controls: true,
      }),
    ),
  );
}

function MediaFiles({ data }: { data: Array<Record<string, unknown>> }) {
  return React.createElement(
    "div",
    { className: "lantai-media-list" },
    data.map((item, index) =>
      React.createElement(
        "a",
        {
          key: index,
          href: String(item.url || ""),
          target: "_blank",
          rel: "noreferrer",
        },
        String(item.name || item.url || "file"),
      ),
    ),
  );
}

function BubbleComponent({ children }: { children?: ReactNode }) {
  return React.createElement("div", { className: "lantai-bubble" }, children);
}

BubbleComponent.Spin = function BubbleSpin() {
  return React.createElement(Spin, { size: "small" });
};

export const Bubble = BubbleComponent;
export const DefaultCards = {
  Images: MediaImages,
  Videos: MediaVideos,
  Audios: MediaAudios,
  Files: MediaFiles,
};
export { Markdown };

const CHAT_AUTO_FOLLOW_MIN_DISTANCE_PX = 320;
const CHAT_AUTO_FOLLOW_VIEWPORT_RATIO = 0.75;
const CHAT_AUTO_FOLLOW_MAX_DISTANCE_PX = 640;
const CHAT_AUTO_FOLLOW_RESUME_DISTANCE_PX = 24;
const CHAT_SCROLL_PRESENTATION_PAUSE_MS = 120;
const CHAT_SUBMITTED_MESSAGE_READING_POSITION_RATIO = 0.58;

function isMessageListNearEnd(list: HTMLDivElement): boolean {
  return (
    list.scrollHeight - list.clientHeight - list.scrollTop <=
    CHAT_AUTO_FOLLOW_RESUME_DISTANCE_PX
  );
}

export function scrollMessageListToEnd(
  list: HTMLDivElement,
  options: { force?: boolean } = {},
): boolean {
  const autoFollowDistance = Math.min(
    CHAT_AUTO_FOLLOW_MAX_DISTANCE_PX,
    Math.max(
      CHAT_AUTO_FOLLOW_MIN_DISTANCE_PX,
      list.clientHeight * CHAT_AUTO_FOLLOW_VIEWPORT_RATIO,
    ),
  );
  const distanceFromBottom =
    list.scrollHeight - list.clientHeight - list.scrollTop;
  if (!options.force && distanceFromBottom > autoFollowDistance) return false;

  if (typeof list.scrollTo === "function") {
    list.scrollTo({ top: list.scrollHeight, behavior: "auto" });
  } else {
    list.scrollTop = list.scrollHeight;
  }
  return true;
}

export function chainChatBoundaryWheel(
  target: EventTarget | null,
  deltaY: number,
  messageList: HTMLDivElement,
): boolean {
  if (!(target instanceof Element) || deltaY === 0) return false;
  const processSurface = target.closest<HTMLElement>(
    '[data-chat-scroll-chain="1"]',
  );
  if (!processSurface) return false;

  const remainingBelow =
    processSurface.scrollHeight -
    processSurface.clientHeight -
    processSurface.scrollTop;
  const reachedBoundary =
    (deltaY < 0 && processSurface.scrollTop <= 1) ||
    (deltaY > 0 && remainingBelow <= 1);
  if (!reachedBoundary) return false;

  const previousScrollTop = messageList.scrollTop;
  const maxScrollTop = Math.max(
    0,
    messageList.scrollHeight - messageList.clientHeight,
  );
  messageList.scrollTop = Math.min(
    maxScrollTop,
    Math.max(0, previousScrollTop + deltaY),
  );
  return messageList.scrollTop !== previousScrollTop;
}

export const LantaiRuntimeWebUI = forwardRef<
  ILantaiRuntimeWebUIRef,
  { options?: ILantaiRuntimeWebUIOptions }
>(function LantaiRuntimeWebUI({ options = {} }, ref) {
  const sessionApi = options.session?.api;
  const initialSessionIdRef = useRef(
    options.session?.initialSessionId?.trim() || undefined,
  );
  const initialSessionId = initialSessionIdRef.current;
  const initialSessionStrategy =
    options.session?.initialSessionStrategy === "new" ? "new" : "first";
  const [sessions, setSessionsState] = useState<ILantaiChatSession[]>([]);
  const [currentSessionId, setCurrentSessionIdState] = useState<
    string | undefined
  >(initialSessionId);
  const [messages, dispatchMessages] = useReducer(messagesReducer, []);
  const [inputValue, setInputValue] = useState("");
  const [selectedCommand, setSelectedCommand] =
    useState<SelectedCommand | null>(null);
  const [activeSuggestionIndex, setActiveSuggestionIndex] = useState(0);
  const [dismissedSuggestionInput, setDismissedSuggestionInput] = useState<
    string | null
  >(null);
  const [attachments, setAttachments] = useState<AttachmentItem[]>([]);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [replacingControlInput, setReplacingControlInput] =
    useState<ControlInputReplacementTarget | null>(null);
  const [pendingBranchMessage, setPendingBranchMessage] =
    useState<ILantaiChatMessage | null>(null);
  const [branchingMessageId, setBranchingMessageId] = useState<string | null>(
    null,
  );
  const [legacyRecoveryBusy, setLegacyRecoveryBusy] = useState(false);
  const [legacyRecoveryError, setLegacyRecoveryError] = useState<string | null>(
    null,
  );
  const [loading, setLoadingState] = useState(false);
  const [controlInputHydrationRevision, setControlInputHydrationRevision] =
    useState(0);
  const [welcomeExitActive, setWelcomeExitActive] = useState(false);
  const [transientNotice, setTransientNotice] = useState<string | null>(null);
  const messagesRef = useRef(messages);
  const sessionsRef = useRef(sessions);
  const currentSessionIdRef = useRef(currentSessionId);
  const inputValueRef = useRef(inputValue);
  const inputOwnerSessionIdRef = useRef(currentSessionId);
  const loadingRef = useRef(loading);
  const welcomeExitTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const transientNoticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const sessionInitializationRef = useRef<Promise<void> | null>(null);
  const createSessionInFlightRef = useRef<
    Map<string, Promise<ILantaiChatSession | null>>
  >(new Map());
  const senderCommandSuggestions = useMemo(
    () =>
      (Array.isArray(options.sender?.suggestions)
        ? options.sender.suggestions
        : []) as CommandSuggestion[],
    [options.sender?.suggestions],
  );
  const senderCommandSuggestionsRef = useRef(senderCommandSuggestions);
  senderCommandSuggestionsRef.current = senderCommandSuggestions;
  const sessionRunStatesRef = useRef<Map<string, SessionRunState>>(new Map());
  const sessionRunStateAliasesRef = useRef<Map<string, string>>(new Map());
  const pendingInitialSubmissionRef = useRef(false);
  const submitTaskRef = useRef<
    (draft?: SubmissionDraft, requestedSessionId?: string) => Promise<void>
  >(async () => undefined);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const composerContainerRef = useRef<HTMLDivElement | null>(null);
  const autoFollowEnabledRef = useRef(true);
  const programmaticReadingScrollTargetRef = useRef<number | null>(null);
  const userScrollInteractionUntilRef = useRef(0);
  const scrollFrameRef = useRef<number | null>(null);
  const pendingScrollForceRef = useRef(false);
  const localMessagesSessionIdsRef = useRef<Set<string>>(new Set());
  const visibleMessagesSessionIdsRef = useRef<Set<string>>(new Set());
  const attachmentsRef = useRef<AttachmentItem[]>([]);
  const attachmentDraftsRef = useRef<Map<string, AttachmentItem[]>>(new Map());
  const attachmentDraftAliasesRef = useRef<Map<string, string>>(new Map());
  const attachmentPreviewUrlsRef = useRef<Set<string>>(new Set());
  const reconciledHydrationControlInputsRef = useRef<{
    revision: number;
    keys: Set<string>;
  }>({ revision: 0, keys: new Set<string>() });

  const resolveAttachmentDraftKey = useCallback((sessionKey: string) => {
    let resolved = sessionKey;
    const visited = new Set<string>();
    while (
      attachmentDraftAliasesRef.current.has(resolved) &&
      !visited.has(resolved)
    ) {
      visited.add(resolved);
      resolved = attachmentDraftAliasesRef.current.get(resolved) || resolved;
    }
    return resolved;
  }, []);

  const activeAttachmentDraftKey = useCallback(
    () =>
      resolveAttachmentDraftKey(
        currentSessionIdRef.current || PENDING_ATTACHMENT_SESSION_KEY,
      ),
    [resolveAttachmentDraftKey],
  );

  const persistAttachmentDraft = useCallback(
    (sessionKey: string, draft: AttachmentItem[]) => {
      if (sessionKey === PENDING_ATTACHMENT_SESSION_KEY) return;
      const ready = draft
        .filter(
          (attachment) =>
            attachment.status === "ready" && Boolean(attachment.url),
        )
        .map<ILantaiChatDraftAttachment>((attachment) => ({
          id: attachment.id,
          name: attachment.name,
          type: attachment.type,
          size: attachment.size,
          url: attachment.url,
          status: "ready",
          fingerprint: attachment.fingerprint,
          role: attachment.role || DEFAULT_ATTACHMENT_ROLE,
          roleConfirmed: Boolean(attachment.roleConfirmed),
          canonicalPath: attachment.canonicalPath,
          storedName: attachment.storedName,
          digest: attachment.digest,
          immutableVersion: attachment.immutableVersion,
          sourceId: attachment.sourceId,
          sourceChatId: attachment.sourceChatId,
          uploadReceipt: attachment.uploadReceipt,
        }));
      if (ready.length > 0) {
        sessionApi?.saveDraftAttachments?.(sessionKey, ready);
      } else {
        sessionApi?.clearDraftAttachments?.(sessionKey);
      }
    },
    [sessionApi],
  );

  const updateAttachmentsForSession = useCallback(
    (
      sessionKey: string,
      update:
        | AttachmentItem[]
        | ((attachments: AttachmentItem[]) => AttachmentItem[]),
    ) => {
      const resolvedSessionKey = resolveAttachmentDraftKey(sessionKey);
      // Keep the imperative draft in sync before React schedules the visual
      // update. Native clipboard recovery can add a File and start its upload
      // in the same event turn; leaving the ref update inside the React state
      // updater creates a race where progress/success targets a draft that is
      // not committed yet.
      const previous =
        attachmentDraftsRef.current.get(resolvedSessionKey) || [];
      const next = typeof update === "function" ? update(previous) : update;
      if (next.length > 0) {
        attachmentDraftsRef.current.set(resolvedSessionKey, next);
      } else {
        attachmentDraftsRef.current.delete(resolvedSessionKey);
      }
      persistAttachmentDraft(resolvedSessionKey, next);
      if (activeAttachmentDraftKey() === resolvedSessionKey) {
        attachmentsRef.current = next;
        setAttachments(next);
      }
    },
    [
      activeAttachmentDraftKey,
      persistAttachmentDraft,
      resolveAttachmentDraftKey,
    ],
  );

  const updateAttachments = useCallback(
    (
      update:
        | AttachmentItem[]
        | ((attachments: AttachmentItem[]) => AttachmentItem[]),
    ) => {
      updateAttachmentsForSession(activeAttachmentDraftKey(), update);
    },
    [activeAttachmentDraftKey, updateAttachmentsForSession],
  );

  const revokeAttachmentPreview = useCallback((url?: string) => {
    if (!url || !attachmentPreviewUrlsRef.current.has(url)) return;
    attachmentPreviewUrlsRef.current.delete(url);
    URL.revokeObjectURL?.(url);
  }, []);

  useEffect(
    () => () => {
      attachmentPreviewUrlsRef.current.forEach((url) => {
        URL.revokeObjectURL?.(url);
      });
      attachmentPreviewUrlsRef.current.clear();
    },
    [],
  );

  const scrollMessagesToEnd = useCallback(
    (options: { force?: boolean } = {}) => {
      if (options.force) {
        autoFollowEnabledRef.current = true;
        pendingScrollForceRef.current = true;
      } else if (!autoFollowEnabledRef.current) {
        return;
      }
      if (scrollFrameRef.current !== null) return;
      scrollFrameRef.current = window.requestAnimationFrame(() => {
        scrollFrameRef.current = null;
        const list = messageListRef.current;
        if (!list) return;
        const force = pendingScrollForceRef.current;
        pendingScrollForceRef.current = false;
        if (!force && !autoFollowEnabledRef.current) return;
        scrollMessageListToEnd(list, { force: true });
      });
    },
    [],
  );

  useEffect(
    () => () => {
      if (scrollFrameRef.current !== null) {
        window.cancelAnimationFrame(scrollFrameRef.current);
        scrollFrameRef.current = null;
      }
    },
    [],
  );

  const positionSubmittedMessageForReading = useCallback(
    (messageId: string) => {
      window.requestAnimationFrame(() => {
        const list = messageListRef.current;
        const composer = composerContainerRef.current;
        if (!list || !composer) return;
        const message = Array.from(
          list.querySelectorAll<HTMLElement>("[data-lantai-message-id]"),
        ).find((node) => node.dataset.lantaiMessageId === messageId);
        if (!message) return;

        const listRect = list.getBoundingClientRect();
        const composerRect = composer.getBoundingClientRect();
        const messageRect = message.getBoundingClientRect();
        const viewportTop = listRect.top + 20;
        const viewportBottom = Math.min(listRect.bottom, composerRect.top) - 20;
        const viewportHeight = Math.max(0, viewportBottom - viewportTop);
        if (viewportHeight <= 0) return;

        const readableMessageHeight = Math.min(
          Math.max(0, messageRect.height),
          viewportHeight,
        );
        const targetTop =
          messageRect.height >= viewportHeight
            ? viewportTop
            : viewportTop +
              (viewportHeight - readableMessageHeight) *
                CHAT_SUBMITTED_MESSAGE_READING_POSITION_RATIO;
        const maxScrollTop = Math.max(0, list.scrollHeight - list.clientHeight);
        const targetScrollTop = Math.min(
          maxScrollTop,
          Math.max(0, list.scrollTop + messageRect.top - targetTop),
        );
        programmaticReadingScrollTargetRef.current = targetScrollTop;
        list.scrollTop = targetScrollTop;
      });
    },
    [],
  );

  const navigateToMessage = useCallback((messageId: string): boolean => {
    const list = messageListRef.current;
    if (!list || !messageId) return false;
    const reducedMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const result = scrollConversationToMessage(list, messageId, {
      behavior: reducedMotion ? "auto" : "smooth",
    });
    if (!result.found) return false;
    autoFollowEnabledRef.current = result.nearEnd;
    userScrollInteractionUntilRef.current =
      performance.now() + CHAT_SCROLL_PRESENTATION_PAUSE_MS;
    return true;
  }, []);

  const attemptPendingMessageNavigation = useCallback(
    (request: PendingChatMessageNavigation | null): boolean => {
      if (!request || !navigateToMessage(request.messageId)) return false;
      consumePendingChatMessageNavigation(request.requestId);
      return true;
    },
    [navigateToMessage],
  );

  useEffect(
    () =>
      subscribeChatMessageNavigation((request) => {
        window.requestAnimationFrame(() => {
          attemptPendingMessageNavigation(request);
        });
      }),
    [attemptPendingMessageNavigation],
  );

  useEffect(() => {
    const request = peekPendingChatMessageNavigation();
    if (!request) return;
    const frame = window.requestAnimationFrame(() => {
      attemptPendingMessageNavigation(request);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [attemptPendingMessageNavigation, currentSessionId, messages]);

  useEffect(() => {
    const list = messageListRef.current;
    const composer = composerContainerRef.current;
    if (!list || !composer) return;

    let frame = 0;
    const updateComposerSafeSpace = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const listRect = list.getBoundingClientRect();
        const composerRect = composer.getBoundingClientRect();
        const occludedHeight = Math.max(0, listRect.bottom - composerRect.top);
        list.style.setProperty(
          "--lt-chat-composer-safe-space",
          `${Math.ceil(occludedHeight + 20)}px`,
        );
        if (autoFollowEnabledRef.current) {
          scrollMessageListToEnd(list, { force: true });
        }
      });
    };

    updateComposerSafeSpace();
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(updateComposerSafeSpace);
    observer?.observe(list);
    observer?.observe(composer);
    window.addEventListener("resize", updateComposerSafeSpace);
    return () => {
      window.cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("resize", updateComposerSafeSpace);
      list.style.removeProperty("--lt-chat-composer-safe-space");
    };
  }, []);

  useEffect(() => {
    const list = messageListRef.current;
    if (!list) return;
    let previousScrollTop = list.scrollTop;
    const handleMessageListScroll = () => {
      const currentScrollTop = list.scrollTop;
      const programmaticTarget = programmaticReadingScrollTargetRef.current;
      if (programmaticTarget !== null) {
        programmaticReadingScrollTargetRef.current = null;
        if (Math.abs(currentScrollTop - programmaticTarget) <= 1) {
          previousScrollTop = currentScrollTop;
          autoFollowEnabledRef.current = true;
          return;
        }
      }
      if (currentScrollTop < previousScrollTop - 1) {
        autoFollowEnabledRef.current = false;
      }
      if (isMessageListNearEnd(list)) {
        autoFollowEnabledRef.current = true;
      }
      previousScrollTop = currentScrollTop;
    };
    list.addEventListener("scroll", handleMessageListScroll, { passive: true });
    return () => {
      list.removeEventListener("scroll", handleMessageListScroll);
    };
  }, []);

  useEffect(() => {
    const handleBoundaryWheel = (event: WheelEvent) => {
      if (
        event.defaultPrevented ||
        event.ctrlKey ||
        Math.abs(event.deltaX) >= Math.abs(event.deltaY)
      ) {
        return;
      }
      const list = messageListRef.current;
      if (!list) return;
      if (!(event.target instanceof Node) || !list.contains(event.target)) {
        return;
      }
      const deltaY =
        event.deltaMode === WheelEvent.DOM_DELTA_LINE
          ? event.deltaY * 16
          : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
            ? event.deltaY * list.clientHeight
            : event.deltaY;
      if (deltaY !== 0) {
        userScrollInteractionUntilRef.current =
          performance.now() + CHAT_SCROLL_PRESENTATION_PAUSE_MS;
      }
      if (deltaY < 0) {
        autoFollowEnabledRef.current = false;
      } else if (deltaY > 0 && isMessageListNearEnd(list)) {
        autoFollowEnabledRef.current = true;
      }
      if (chainChatBoundaryWheel(event.target, deltaY, list)) {
        event.preventDefault();
      }
    };

    document.addEventListener("wheel", handleBoundaryWheel, {
      capture: true,
      passive: false,
    });
    return () => {
      document.removeEventListener("wheel", handleBoundaryWheel, true);
    };
  }, []);

  const setMessages = useCallback(
    (
      next:
        | ILantaiChatMessage[]
        | ((messages: ILantaiChatMessage[]) => ILantaiChatMessage[]),
    ) => {
      if (typeof next === "function") {
        dispatchMessages({ type: "update", update: next });
        return;
      }
      dispatchMessages({ type: "replace", messages: next });
    },
    [],
  );

  messagesRef.current = messages;
  sessionsRef.current = sessions;
  currentSessionIdRef.current = currentSessionId;
  inputValueRef.current = inputValue;
  loadingRef.current = loading;

  const updateInputValue = useCallback(
    (next: string | ((current: string) => string)) => {
      const value =
        typeof next === "function" ? next(inputValueRef.current) : next;
      inputValueRef.current = value;
      setInputValue(value);

      const sessionId = currentSessionIdRef.current;
      if (sessionId) {
        if (value) {
          sessionApi?.saveDraftText?.(sessionId, value);
        } else {
          sessionApi?.clearDraftText?.(sessionId);
        }
      }
    },
    [sessionApi],
  );

  useEffect(() => {
    const applyPendingMarkdownHandoff = () => {
      const envelope = consumeMarkdownHandoff();
      if (!envelope) return;
      const prompt = buildMarkdownHandoffPrompt(envelope);
      updateInputValue((current) =>
        current.trim() ? `${current.trim()}\n\n${prompt}` : prompt,
      );
      window.requestAnimationFrame(() => {
        composerContainerRef.current?.querySelector("textarea")?.focus();
      });
    };
    applyPendingMarkdownHandoff();
    window.addEventListener(
      MARKDOWN_HANDOFF_EVENT,
      applyPendingMarkdownHandoff,
    );
    return () => {
      window.removeEventListener(
        MARKDOWN_HANDOFF_EVENT,
        applyPendingMarkdownHandoff,
      );
    };
  }, [updateInputValue]);

  useLayoutEffect(() => {
    const value = currentSessionId
      ? sessionApi?.loadDraftText?.(currentSessionId) || ""
      : "";
    inputValueRef.current = value;
    inputOwnerSessionIdRef.current = currentSessionId;
    setInputValue(value);
    setSelectedCommand(
      selectedCommandFromDraft(value, senderCommandSuggestionsRef.current),
    );
    setDismissedSuggestionInput(null);
  }, [currentSessionId, sessionApi]);

  useEffect(() => {
    if (
      selectedCommand &&
      !selectedCommandFromDraft(inputValue, [selectedCommand])
    ) {
      setSelectedCommand(null);
    }
  }, [inputValue, selectedCommand]);

  useLayoutEffect(() => {
    if (!currentSessionId) {
      attachmentsRef.current = [];
      setAttachments([]);
      return;
    }

    const resolvedSessionKey = resolveAttachmentDraftKey(currentSessionId);
    const pendingDraft = attachmentDraftsRef.current.get(
      PENDING_ATTACHMENT_SESSION_KEY,
    );
    if (pendingDraft && pendingDraft.length > 0) {
      const existing =
        attachmentDraftsRef.current.get(resolvedSessionKey) ||
        sessionApi?.loadDraftAttachments?.(resolvedSessionKey) ||
        [];
      const fingerprints = new Set(
        existing
          .map((attachment) => attachment.fingerprint)
          .filter((value): value is string => Boolean(value)),
      );
      const migrated = [
        ...existing,
        ...pendingDraft.filter(
          (attachment) =>
            !attachment.fingerprint ||
            !fingerprints.has(attachment.fingerprint),
        ),
      ];
      attachmentDraftAliasesRef.current.set(
        PENDING_ATTACHMENT_SESSION_KEY,
        resolvedSessionKey,
      );
      attachmentDraftsRef.current.delete(PENDING_ATTACHMENT_SESSION_KEY);
      attachmentDraftsRef.current.set(resolvedSessionKey, migrated);
      persistAttachmentDraft(resolvedSessionKey, migrated);
    }

    const restored =
      attachmentDraftsRef.current.get(resolvedSessionKey) ||
      sessionApi?.loadDraftAttachments?.(resolvedSessionKey) ||
      [];
    if (restored.length > 0) {
      attachmentDraftsRef.current.set(resolvedSessionKey, restored);
    }
    attachmentsRef.current = restored;
    setAttachments(restored);
  }, [
    currentSessionId,
    persistAttachmentDraft,
    resolveAttachmentDraftKey,
    sessionApi,
  ]);

  const getSessionRunState = useCallback((sessionId: string) => {
    const stableSession = findSessionByStableIdentity(
      sessionsRef.current,
      sessionId,
    );
    const stateKey = stableSession?.id || sessionId;
    const sessionMessages =
      findSessionByStableIdentity(
        sessionsRef.current,
        currentSessionIdRef.current,
      ) === stableSession
        ? messagesRef.current
        : stableSession?.messages || [];
    const identityKeys = Array.from(
      new Set(
        [
          sessionId,
          stableSession?.id,
          stableSession?.realId,
          stableSession &&
          uniqueSessionAliasOwner(
            sessionsRef.current,
            normalizedSessionIdentity(stableSession.sessionId),
          ) === stableSession
            ? stableSession.sessionId
            : undefined,
        ]
          .map(normalizedSessionIdentity)
          .filter(Boolean),
      ),
    );
    const candidateKeys = Array.from(
      new Set([
        stateKey,
        ...identityKeys,
        ...identityKeys
          .map((key) => sessionRunStateAliasesRef.current.get(key))
          .filter((key): key is string => Boolean(key)),
      ]),
    );
    const candidates = candidateKeys
      .map((key) => ({ key, state: sessionRunStatesRef.current.get(key) }))
      .filter(
        (candidate): candidate is { key: string; state: SessionRunState } =>
          Boolean(candidate.state),
      );
    // Prefer the state that owns live work. Once Stop settles, retain its
    // confirmed runtime identities ahead of an unrelated empty alias state.
    // A temporary route id can be replaced by a backend id and then by a
    // runtime alias during one turn; all of those identities must still reach
    // the original controller or its authoritative Stop acknowledgement.
    let state = candidates
      .map((candidate) => candidate.state)
      .sort(
        (left, right) =>
          (right.controller ||
          right.stopping ||
          right.submitting ||
          right.loading
            ? 2
            : right.confirmedStopTurnKeys.size > 0
              ? 1
              : 0) -
          (left.controller || left.stopping || left.submitting || left.loading
            ? 2
            : left.confirmedStopTurnKeys.size > 0
              ? 1
              : 0),
      )[0];
    if (!state) {
      state = {
        loading: false,
        submitting: false,
        stopping: false,
        controller: null,
        steerControllers: new Set<AbortController>(),
        queue: queuedDraftsFromMessages(sessionMessages),
        bindingGuidanceMessageIds: new Set<string>(),
        awaitingVisibleAssistantOutput: false,
        confirmedStopTurnKeys: new Set<string>(),
      };
    } else {
      const knownMessageIds = new Set(
        state.queue
          .map((draft) => draft.userMessage?.id)
          .filter((id): id is string => Boolean(id)),
      );
      for (const restoredDraft of queuedDraftsFromMessages(sessionMessages)) {
        const messageId = restoredDraft.userMessage?.id;
        if (messageId && !knownMessageIds.has(messageId)) {
          state.queue.push(restoredDraft);
          knownMessageIds.add(messageId);
        }
      }
    }
    for (const candidate of candidates) {
      if (candidate.key !== stateKey) {
        sessionRunStatesRef.current.delete(candidate.key);
      }
    }
    sessionRunStatesRef.current.set(stateKey, state);
    for (const [alias, canonical] of sessionRunStateAliasesRef.current) {
      if (candidateKeys.includes(canonical)) {
        sessionRunStateAliasesRef.current.set(alias, stateKey);
      }
    }
    for (const identity of identityKeys) {
      sessionRunStateAliasesRef.current.set(identity, stateKey);
    }
    return state;
  }, []);

  const isSessionRunActive = useCallback(
    (sessionId: string) => {
      if (!sessionId) return false;
      const runState = getSessionRunState(sessionId);
      const session = findSessionByStableIdentity(
        sessionsRef.current,
        sessionId,
      );
      const locallyActive = Boolean(
        runState.loading ||
        runState.submitting ||
        runState.stopping ||
        runState.controller,
      );
      if (locallyActive) return true;
      if (!sessionHasActiveRun(session)) return false;
      if (runState.confirmedStopTurnKeys.size === 0) return true;
      // The interrupt acknowledgement is authoritative for this turn. Ignore
      // delayed `running` mirrors until a new turn is explicitly submitted.
      // Attempt identities are opaque and may rotate while the stopped attempt
      // settles, so inequality is not a recency signal.
      return false;
    },
    [getSessionRunState],
  );

  const setSessionLoading = useCallback(
    (sessionId: string, next: boolean | string) => {
      const normalized = typeof next === "string" ? next !== "false" : next;
      if (sessionId) {
        getSessionRunState(sessionId).loading = normalized;
      }
      const targetSession = findSessionByStableIdentity(
        sessionsRef.current,
        sessionId,
      );
      const currentSession = findSessionByStableIdentity(
        sessionsRef.current,
        currentSessionIdRef.current,
      );
      if (!sessionId || (targetSession && targetSession === currentSession)) {
        const activeState = sessionId
          ? getSessionRunState(sessionId)
          : undefined;
        const activeBusy = Boolean(normalized || activeState?.submitting);
        loadingRef.current = activeBusy;
        setLoadingState(activeBusy);
      }
    },
    [getSessionRunState],
  );

  const setLoading = useCallback(
    (next: boolean | string) => {
      setSessionLoading(currentSessionIdRef.current || "", next);
    },
    [setSessionLoading],
  );

  const beginFirstTurnWelcomeExit = useCallback(() => {
    if (welcomeExitTimerRef.current) {
      clearTimeout(welcomeExitTimerRef.current);
    }
    setWelcomeExitActive(true);
    const reduceMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    welcomeExitTimerRef.current = setTimeout(
      () => {
        welcomeExitTimerRef.current = null;
        setWelcomeExitActive(false);
      },
      reduceMotion ? 1 : FIRST_TURN_WELCOME_EXIT_MS,
    );
  }, []);

  useEffect(() => {
    if (welcomeExitTimerRef.current) {
      clearTimeout(welcomeExitTimerRef.current);
      welcomeExitTimerRef.current = null;
    }
    setWelcomeExitActive(false);
    if (transientNoticeTimerRef.current) {
      clearTimeout(transientNoticeTimerRef.current);
      transientNoticeTimerRef.current = null;
    }
    setTransientNotice(null);
  }, [currentSessionId]);

  useEffect(
    () => () => {
      if (welcomeExitTimerRef.current) {
        clearTimeout(welcomeExitTimerRef.current);
      }
      if (transientNoticeTimerRef.current) {
        clearTimeout(transientNoticeTimerRef.current);
      }
    },
    [],
  );

  const rememberLocalMessageSessionIds = useCallback((sessionId?: string) => {
    const ids = new Set<string>();
    const add = (value?: string | null) => {
      const normalized = typeof value === "string" ? value.trim() : "";
      if (normalized) ids.add(normalized);
    };
    add(sessionId);
    const active = findSessionByStableIdentity(
      sessionsRef.current,
      sessionId,
    ) as
      | (ILantaiChatSession & { realId?: string; sessionId?: string })
      | undefined;
    add(active?.id);
    add(active?.realId);
    add(active?.sessionId);
    localMessagesSessionIdsRef.current = ids;
  }, []);

  const localMessagesBelongToSession = useCallback(
    (...ids: Array<string | null | undefined>) => {
      const localIds = localMessagesSessionIdsRef.current;
      if (localIds.size === 0) return false;
      return ids.some((id) => {
        const normalized = typeof id === "string" ? id.trim() : "";
        return Boolean(normalized && localIds.has(normalized));
      });
    },
    [],
  );

  const rememberVisibleMessagesSessionIds = useCallback((...ids: unknown[]) => {
    const nextIds = new Set<string>();
    ids.forEach((id) => {
      const normalized = typeof id === "string" ? id.trim() : "";
      if (normalized) nextIds.add(normalized);
    });
    visibleMessagesSessionIdsRef.current = nextIds;
  }, []);

  const visibleMessagesBelongToSession = useCallback((...ids: unknown[]) => {
    const visibleIds = visibleMessagesSessionIdsRef.current;
    if (visibleIds.size === 0) return false;
    return ids.some((id) => visibleSessionOwnerMatches(visibleIds, id));
  }, []);

  const setSessions = useCallback<SessionsState["setSessions"]>(
    (next) => {
      setSessionsState((previous) => {
        const resolved = typeof next === "function" ? next(previous) : next;
        const streamOwned = preservePrimaryStreamOwnership(
          previous,
          normalizeSessionsMessages(resolved),
        );
        const normalized = preserveLocallyStoppedRuntimeStates(
          previous,
          streamOwned,
        );
        sessionsRef.current = normalized;
        const active = findSessionByStableIdentity(
          normalized,
          currentSessionIdRef.current,
        ) as
          | (ILantaiChatSession & { realId?: string; sessionId?: string })
          | undefined;
        if (active?.messages) {
          // Only the canonical session id may authorize reconciliation. Some
          // providers reuse workspace/session aliases across distinct chats;
          // treating any shared alias as ownership would reopen cross-talk.
          const sameVisibleSession = visibleMessagesBelongToSession(active.id);
          const nextMessages = reconcileActiveSessionProjection(
            messagesRef.current,
            active.messages,
            sameVisibleSession,
          );
          messagesRef.current = nextMessages;
          rememberVisibleMessagesSessionIds(
            currentSessionIdRef.current,
            active.id,
            active.realId,
            active.sessionId,
          );
          setMessages(nextMessages);
        }
        return normalized;
      });
    },
    [rememberVisibleMessagesSessionIds, visibleMessagesBelongToSession],
  );

  const setCurrentSessionId = useCallback(
    (sessionId?: string | null) => {
      const normalized = sessionId || undefined;
      sessionApi?.activateSession?.(normalized);
      const previousTarget = findSessionByStableIdentity(
        sessionsRef.current,
        currentSessionIdRef.current,
      );
      const target = findSessionByStableIdentity(
        sessionsRef.current,
        normalized,
      );
      const changed = normalized !== currentSessionIdRef.current;
      const sameStableSession = Boolean(
        previousTarget && target && previousTarget.id === target.id,
      );
      const localMessagesBelongToNextSession =
        sameStableSession && localMessagesBelongToSession(normalized);
      if (changed && !localMessagesBelongToNextSession) {
        localMessagesSessionIdsRef.current = new Set();
      }
      currentSessionIdRef.current = normalized;
      setCurrentSessionIdState(normalized);
      if (changed) {
        const nextMessages = target?.messages
          ? mergeAdjacentAssistantResponseCards(target.messages)
          : localMessagesBelongToNextSession
            ? messagesRef.current
            : [];
        messagesRef.current = nextMessages;
        rememberVisibleMessagesSessionIds(
          normalized,
          target?.id,
          target?.realId,
          target?.sessionId,
        );
        setMessages(nextMessages);
        if (nextMessages.length > 0) {
          scrollMessagesToEnd({ force: true });
        }
      }
      const targetRunState = normalized
        ? getSessionRunState(normalized)
        : undefined;
      const targetBusy = Boolean(
        targetRunState?.loading || targetRunState?.submitting,
      );
      loadingRef.current = targetBusy;
      setLoadingState(targetBusy);
    },
    [
      getSessionRunState,
      localMessagesBelongToSession,
      rememberVisibleMessagesSessionIds,
      scrollMessagesToEnd,
      sessionApi,
    ],
  );

  const createSession = useCallback(
    (seed?: Partial<ILantaiChatSession>) => {
      const requestOwnerId = currentSessionIdRef.current;
      const requestKey = requestOwnerId || "__unselected__";
      const pending = createSessionInFlightRef.current.get(requestKey);
      if (pending) return pending;

      const request: Promise<ILantaiChatSession | null> = (async () => {
        const created = await sessionApi?.createSession?.({
          name: "New Chat",
          messages: [],
          ...seed,
        });
        if (!created) return null;
        const normalizedCreated = normalizeSessionsMessages(created);
        sessionsRef.current = normalizedCreated;
        setSessionsState(normalizedCreated);
        const next = normalizedCreated[0] || null;
        const currentOwnerId = currentSessionIdRef.current;
        if (currentOwnerId !== requestOwnerId && currentOwnerId !== next?.id) {
          return next;
        }
        currentSessionIdRef.current = next?.id || undefined;
        setCurrentSessionIdState(next?.id || undefined);
        const nextMessages = mergeAdjacentAssistantResponseCards(
          next?.messages || [],
        );
        localMessagesSessionIdsRef.current = new Set();
        messagesRef.current = nextMessages;
        rememberVisibleMessagesSessionIds(
          next?.id,
          next?.realId,
          next?.sessionId,
        );
        setMessages(nextMessages);
        return next;
      })().finally(() => {
        if (createSessionInFlightRef.current.get(requestKey) === request) {
          createSessionInFlightRef.current.delete(requestKey);
        }
      });
      createSessionInFlightRef.current.set(requestKey, request);
      return request;
    },
    [rememberVisibleMessagesSessionIds, sessionApi],
  );

  const visibleSessionOwnsIdentifiers = useCallback(
    (ownerSessionIds: string | string[]) => {
      const ownerIds = (
        Array.isArray(ownerSessionIds) ? ownerSessionIds : [ownerSessionIds]
      )
        .map(normalizedSessionIdentity)
        .filter(Boolean);
      if (ownerIds.length === 0) return false;
      const active = findSessionByStableIdentity(
        sessionsRef.current,
        currentSessionIdRef.current,
      );
      if (!active) return false;
      return ownerIds.some((ownerId) => {
        const owner = findSessionByStableIdentity(sessionsRef.current, ownerId);
        return owner === active;
      });
    },
    [],
  );

  const upsertTransientAssistantResponse = useCallback(
    (
      ownerSessionIds: string | string[],
      messageId: string,
      data: ILantaiRuntimeResponse,
    ) => {
      if (!visibleSessionOwnsIdentifiers(ownerSessionIds)) return false;
      const nextMessage = buildAssistantMessage(cloneResponseData(data));
      nextMessage.id = messageId;
      nextMessage.transient = true;
      setMessages((current) => {
        const existingIndex = current.findIndex(
          (message) => message.id === messageId,
        );
        if (existingIndex < 0) return [...current, nextMessage];
        const next = [...current];
        next[existingIndex] = nextMessage;
        return next;
      });
      requestAnimationFrame(() => scrollMessagesToEnd({ force: true }));
      return true;
    },
    [scrollMessagesToEnd, setMessages, visibleSessionOwnsIdentifiers],
  );

  const removeAllMessages = useCallback(
    (ownerSessionIds: string | string[]) => {
      if (!visibleSessionOwnsIdentifiers(ownerSessionIds)) return false;
      messagesRef.current = [];
      setMessages([]);
      return true;
    },
    [setMessages, visibleSessionOwnsIdentifiers],
  );

  useImperativeHandle(
    ref,
    () => ({
      messages: {
        getMessages: () => messagesRef.current,
        removeAllMessages,
        upsertTransientAssistantResponse,
      },
      navigation: {
        scrollToMessage: navigateToMessage,
      },
      branch: {
        createFromLatestUserMessage: () => {
          const sessionId = currentSessionIdRef.current || "";
          const runState = sessionId
            ? getSessionRunState(sessionId)
            : undefined;
          if (runState?.loading || runState?.submitting) return false;
          const latestUserMessage = [...messagesRef.current]
            .reverse()
            .find((message) => message.role === "user");
          if (!latestUserMessage) return false;
          setPendingBranchMessage(latestUserMessage);
          return true;
        },
      },
    }),
    [
      getSessionRunState,
      navigateToMessage,
      removeAllMessages,
      upsertTransientAssistantResponse,
    ],
  );

  useEffect(() => {
    let cancelled = false;
    const initialization = (async () => {
      try {
        // A route such as /chat/:id is authoritative on the first paint. Set
        // the session API owner before the async history list can choose its
        // first entry; otherwise a fast list response can briefly select a
        // different chat and permanently win the page-start race.
        if (initialSessionId) {
          sessionApi?.activateSession?.(initialSessionId);
        }
        const list = normalizeSessionsMessages(
          (await sessionApi?.getSessionList?.()) || [],
        );
        if (cancelled) return;
        sessionsRef.current = list;
        setSessionsState(list);
        if (!currentSessionIdRef.current) {
          if (initialSessionStrategy === "new") {
            await createSession();
          } else {
            const first = list[0];
            if (first?.id) {
              sessionApi?.activateSession?.(first.id);
              currentSessionIdRef.current = first.id;
              setCurrentSessionIdState(first.id);
            } else {
              await createSession();
            }
          }
        }
      } catch (error) {
        if (!cancelled) {
          console.warn("Failed to initialize chat sessions:", error);
        }
      }
    })();
    sessionInitializationRef.current = initialization;
    void initialization;
    return () => {
      cancelled = true;
    };
  }, [createSession, initialSessionId, initialSessionStrategy, sessionApi]);

  useEffect(() => {
    if (!currentSessionId || !sessionApi?.getSession) return;
    let cancelled = false;
    const hydrationRequestedAt = Date.now();
    void (async () => {
      const session = await sessionApi.getSession!(currentSessionId);
      if (cancelled || currentSessionIdRef.current !== currentSessionId) return;
      const normalizedSession = normalizeSessionMessages(session);
      const normalizedSessionWithAliases =
        normalizedSession as ILantaiChatSession & {
          realId?: string;
          sessionId?: string;
        };
      const incomingMessages = normalizedSession.messages || [];
      const activeSession = findSessionByStableIdentity(
        sessionsRef.current,
        currentSessionId,
      ) as
        | (ILantaiChatSession & { realId?: string; sessionId?: string })
        | undefined;
      const activeSessionMessages = activeSession?.messages || [];
      const localMatchesCurrentSession = localMessagesBelongToSession(
        currentSessionId,
        normalizedSessionWithAliases.id,
        normalizedSessionWithAliases.realId,
        normalizedSessionWithAliases.sessionId,
        activeSession?.id,
        activeSession?.realId,
        activeSession?.sessionId,
      );
      const localSessionMessages = localMatchesCurrentSession
        ? messagesRef.current
        : [];
      const routeProjection = transcriptReconciler.reconcileRouteHydration({
        activeSessionMessages,
        localSessionMessages,
        incomingMessages,
        localMatchesCurrentSession,
        durableSessionGenerating: session.generating === true,
        primaryStreamActive: activeSession?.__primaryStreamActive === true,
        runtimeWasActive: sessionHasActiveRun(
          activeSession || normalizedSession,
        ),
      });
      const reconciledSession = routeProjection.terminalAuthority
        ? {
            ...normalizedSession,
            generating: false,
            status: "idle",
            runtimeState: normalizedSession.runtimeState
              ? {
                  ...normalizedSession.runtimeState,
                  status: "completed",
                  can_reconnect: false,
                  can_stop: false,
                }
              : normalizedSession.runtimeState,
            pendingUserMessage: undefined,
            __primaryStreamActive: false,
          }
        : normalizedSession;
      if (
        !routeProjection.preservingExistingMessages &&
        localMatchesCurrentSession
      ) {
        localMessagesSessionIdsRef.current = new Set();
      }
      const nextMessages = routeProjection.messages;
      messagesRef.current = nextMessages;
      rememberVisibleMessagesSessionIds(
        currentSessionId,
        normalizedSessionWithAliases.id,
        normalizedSessionWithAliases.realId,
        normalizedSessionWithAliases.sessionId,
        activeSession?.id,
        activeSession?.realId,
        activeSession?.sessionId,
      );
      setMessages(nextMessages);
      setControlInputHydrationRevision((revision) => revision + 1);
      if (nextMessages.length > 0) {
        scrollMessagesToEnd({ force: true });
      }
      setSessionsState((previous) => {
        const previousSession = findSessionByStableIdentity(
          previous,
          normalizedSession.id,
        );
        const index = previousSession ? previous.indexOf(previousSession) : -1;
        const hydratedSession = preserveLocallySettledRuntimeState(
          previousSession,
          routeProjection.preservingExistingMessages
            ? {
                ...reconciledSession,
                messages: nextMessages,
                __preserveMessages: true,
              }
            : reconciledSession,
          hydrationRequestedAt,
        );
        if (index === -1) {
          const next = [hydratedSession, ...previous];
          sessionsRef.current = next;
          return next;
        }
        const next = [...previous];
        next[index] = {
          ...previousSession,
          ...hydratedSession,
        };
        sessionsRef.current = next;
        return next;
      });
      if (routeProjection.terminalAuthority && sessionApi?.updateSession) {
        void sessionApi
          .updateSession({
            ...reconciledSession,
            messages: nextMessages,
            __preserveMessages: true,
          })
          .catch((error) =>
            console.warn(
              "Failed to persist terminal route reconciliation:",
              error,
            ),
          );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    currentSessionId,
    localMessagesBelongToSession,
    rememberVisibleMessagesSessionIds,
    scrollMessagesToEnd,
    sessionApi,
    setSessionLoading,
  ]);

  const syncSessionMessages = useCallback(
    (
      sessionId: string,
      nextMessages: ILantaiChatMessage[],
      transient?: { primaryStreamActive?: boolean; generating?: boolean },
    ) => {
      if (!sessionId) return;
      const normalizedMessages =
        mergeAdjacentAssistantResponseCards(nextMessages);
      if (currentSessionIdRef.current === sessionId) {
        rememberLocalMessageSessionIds(sessionId);
        const activeSession = findSessionByStableIdentity(
          sessionsRef.current,
          sessionId,
        ) as
          | (ILantaiChatSession & { realId?: string; sessionId?: string })
          | undefined;
        rememberVisibleMessagesSessionIds(
          sessionId,
          activeSession?.id,
          activeSession?.realId,
          activeSession?.sessionId,
        );
        messagesRef.current = normalizedMessages;
      }
      setSessionsState((previous) => {
        const target = findSessionByStableIdentity(previous, sessionId);
        const next = previous.map((session) => {
          if (session !== target) return session;
          const startsNewPrimaryRun = transient?.primaryStreamActive === true;
          const runtimeState = startsNewPrimaryRun
            ? {
                ...(session.runtimeState &&
                typeof session.runtimeState === "object"
                  ? (session.runtimeState as Record<string, unknown>)
                  : {}),
              }
            : session.runtimeState;
          if (
            startsNewPrimaryRun &&
            runtimeState &&
            typeof runtimeState === "object"
          ) {
            // A previous Run/Attempt is never authority for a newly submitted
            // primary turn. Wait for the server's initial identity frame.
            const nextRuntimeState = runtimeState as Record<string, unknown>;
            delete nextRuntimeState.run_id;
            delete nextRuntimeState.attempt_id;
            nextRuntimeState.status = "created";
          }
          return {
            ...session,
            messages: normalizedMessages,
            __preserveMessages: true,
            runtimeState,
            ...(transient?.primaryStreamActive === undefined
              ? {}
              : {
                  __primaryStreamActive: transient.primaryStreamActive,
                  __primaryStreamStartedAt: startsNewPrimaryRun
                    ? Date.now()
                    : session.__primaryStreamStartedAt,
                  __primaryStreamSettledAt:
                    transient.primaryStreamActive === false
                      ? Date.now()
                      : undefined,
                }),
            ...(transient?.generating === undefined
              ? {}
              : {
                  generating: transient.generating,
                  status: transient.generating ? "running" : "idle",
                }),
          };
        });
        sessionsRef.current = next;
        return next;
      });
      void sessionApi?.updateSession?.({
        id: sessionId,
        messages: normalizedMessages,
        __preserveMessages: true,
      } as Partial<ILantaiChatSession>);
    },
    [
      rememberLocalMessageSessionIds,
      rememberVisibleMessagesSessionIds,
      sessionApi,
    ],
  );

  const syncActiveSessionMessages = useCallback(
    (
      nextMessages: ILantaiChatMessage[],
      transient?: { primaryStreamActive?: boolean; generating?: boolean },
    ) => {
      const sessionId = currentSessionIdRef.current;
      if (!sessionId) return;
      syncSessionMessages(sessionId, nextMessages, transient);
    },
    [setMessages, syncSessionMessages],
  );

  const setPrimaryStreamActive = useCallback(
    (
      sessionId: string,
      active: boolean,
      preserveDurableRuntimeActivity = true,
      terminalStatus?: "completed" | "failed" | "interrupted",
    ) => {
      if (!sessionId) return;
      setSessionsState((previous) => {
        const target = findSessionByStableIdentity(previous, sessionId);
        const next = previous.map((session) => {
          if (session !== target) return session;
          const keepRuntimeActive = Boolean(
            !active &&
            preserveDurableRuntimeActivity &&
            sessionHasDurableRuntimeActivity(session),
          );
          const runtimeState =
            !active && !preserveDurableRuntimeActivity
              ? {
                  ...(session.runtimeState &&
                  typeof session.runtimeState === "object"
                    ? (session.runtimeState as Record<string, unknown>)
                    : {}),
                  status: terminalStatus || "cancelled",
                  can_reconnect: false,
                  can_stop: false,
                }
              : session.runtimeState;
          return {
            ...session,
            __primaryStreamActive: active,
            __primaryStreamStartedAt: active
              ? session.__primaryStreamActive === true
                ? session.__primaryStreamStartedAt || Date.now()
                : Date.now()
              : session.__primaryStreamStartedAt,
            __primaryStreamSettledAt: active ? undefined : Date.now(),
            generating: active || keepRuntimeActive,
            status: active || keepRuntimeActive ? "running" : "idle",
            runtimeState,
          };
        });
        sessionsRef.current = next;
        return next;
      });
    },
    [],
  );

  const appendPayloadBatch = useCallback(
    (payloads: Array<Record<string, unknown>>, targetSessionId?: string) => {
      if (payloads.length === 0) return;
      const sessionId = targetSessionId || currentSessionIdRef.current;
      if (!sessionId) return;
      const isActiveSession = currentSessionIdRef.current === sessionId;
      const target = isActiveSession
        ? undefined
        : findSessionByStableIdentity(sessionsRef.current, sessionId);
      const previous = isActiveSession
        ? messagesRef.current
        : target?.messages || [];
      let next = previous;
      for (const payload of payloads) {
        next = applyStreamPayload(next, payload);
      }
      next = mergeAdjacentAssistantResponseCards(next);
      if (next !== previous) {
        if (isActiveSession) {
          messagesRef.current = next;
          setMessages(next);
        }
        // Keep session state synchronization outside the React state updater.
        // Updater functions may be replayed by React; side effects inside them
        // previously duplicated work and caused visible stream stalls.
        syncSessionMessages(sessionId, next);
      }
      const hasVisibleFeedback = payloads.some(
        payloadHasVisibleAssistantFeedback,
      );
      const targetRunState = getSessionRunState(sessionId);
      if (!isActiveSession) {
        if (hasVisibleFeedback) {
          targetRunState.awaitingVisibleAssistantOutput = false;
        }
        recordChatStreamProbe({
          phase: "background_presentation_commit",
          payload_count: payloads.length,
          visible_text_length: payloads.reduce(
            (total, payload) => total + streamPayloadText(payload).length,
            0,
          ),
        });
        return;
      }
      const shouldAutoFollow =
        !targetRunState.awaitingVisibleAssistantOutput || hasVisibleFeedback;
      if (hasVisibleFeedback) {
        targetRunState.awaitingVisibleAssistantOutput = false;
      }
      if (shouldAutoFollow) scrollMessagesToEnd();
      recordChatStreamProbe({
        phase: "presentation_commit",
        payload_count: payloads.length,
        visible_text_length: payloads.reduce(
          (total, payload) => total + streamPayloadText(payload).length,
          0,
        ),
      });
    },
    [getSessionRunState, scrollMessagesToEnd, setMessages, syncSessionMessages],
  );

  const appendPayload = useCallback(
    (payload: Record<string, unknown>, targetSessionId?: string) => {
      appendPayloadBatch([payload], targetSessionId);
    },
    [appendPayloadBatch],
  );

  const appendSyntheticPayload = useCallback(
    (payload: Record<string, unknown>, targetSessionId?: string) => {
      const parser = options.api?.responseParser;
      if (!parser) {
        appendPayload(payload, targetSessionId);
        return;
      }
      try {
        const parsed = parser(JSON.stringify(payload));
        if (parsed && typeof parsed === "object") {
          appendPayload(parsed as Record<string, unknown>, targetSessionId);
          return;
        }
      } catch {
        // Keep the local failure visible even if the host parser rejects it.
      }
      appendPayload(payload, targetSessionId);
    },
    [appendPayload, options.api],
  );

  const consumeResponse = useCallback(
    async (
      response: Response,
      targetSessionId: string,
      signal?: AbortSignal,
    ) => {
      let payloadCount = 0;
      let terminalResponseCount = 0;
      const messageTypes = new Map<string, string>();
      const messageSlots = new Map<string, string>();
      const slotForMessage = (messageId: string) => {
        if (!messageId) return "";
        const existing = messageSlots.get(messageId);
        if (existing) return existing;
        const slot = `m${messageSlots.size + 1}`;
        messageSlots.set(messageId, slot);
        return slot;
      };
      let thinkingPresentationStartedAt = 0;
      let thinkingPresentationBarrier: Promise<void> | null = null;
      const presentation = createStreamPresentationScheduler(
        (payloads) => {
          appendPayloadBatch(payloads, targetSessionId);
        },
        {
          shouldDeferPresentation: () =>
            performance.now() < userScrollInteractionUntilRef.current,
        },
      );
      const appendRawPayload = async (raw: string) => {
        if (signal?.aborted) return;
        const text = raw.trim();
        if (!text || text === "[DONE]") return;
        let payload: Record<string, unknown>;
        try {
          payload = parseRawStreamPayload(text, options.api?.responseParser);
        } catch (error) {
          const diagnostic = streamProtocolDiagnostic(text);
          recordChatStreamProbe({ phase: "protocol_reject", ...diagnostic });
          console.error(
            `[ChatStreamProtocol] Rejected SSE frame ${JSON.stringify(diagnostic)}`,
          );
          throw error;
        }
        payloadCount += 1;
        const object = String(payload.object || "");
        const controlIdentity = initialRuntimeControlIdentity(payload);
        if (controlIdentity) {
          setSessionsState((previous) => {
            const next = applyRuntimeControlIdentityFrame(
              previous,
              targetSessionId,
              payload,
            );
            sessionsRef.current = next;
            return next;
          });
          // A server receipt ends local delivery, but its Run identity belongs
          // to controls. Keep the placeholder unscoped until transcript output
          // supplies the turn identity, which may differ for queued replies.
          const targetMessages =
            currentSessionIdRef.current === targetSessionId
              ? messagesRef.current
              : findSessionByStableIdentity(
                  sessionsRef.current,
                  targetSessionId,
                )?.messages || [];
          const acknowledgedMessages = targetMessages.map((message) => {
            const data = getResponseCardData(message);
            if (
              data?.metadata?.optimistic_pending !== true ||
              !isActiveRuntimeStatus(data.status) ||
              responseTurnKey(data)
            ) {
              return message;
            }
            const nextData = cloneResponseData(data);
            delete nextData.metadata?.optimistic_pending;
            // Preserve the receipt as display evidence without assigning the
            // transport Run to the transcript's logical turn identity.
            nextData.metadata = {
              ...nextData.metadata,
              pending_receipt_run_id: controlIdentity.runId,
            };
            return {
              ...message,
              cards: (message.cards || []).map((card) =>
                card.code === RESPONSE_CARD_CODE
                  ? { ...card, data: nextData }
                  : card,
              ),
            };
          });
          if (
            acknowledgedMessages.some(
              (message, index) => message !== targetMessages[index],
            )
          ) {
            if (currentSessionIdRef.current === targetSessionId) {
              messagesRef.current = acknowledgedMessages;
              setMessages(acknowledgedMessages);
            }
            syncSessionMessages(targetSessionId, acknowledgedMessages);
          }
          recordChatStreamProbe({
            phase: "control_identity",
            object,
            status: String(payload.status || ""),
            run_id: controlIdentity.runId,
          });
          return;
        }
        // Receipts and structured keepalives are control-plane frames. They
        // are valid stream traffic, but do not own transcript content and must
        // never terminate or split the assistant turn currently presented.
        if (STREAM_CONTROL_PAYLOAD_OBJECTS.has(object)) {
          recordChatStreamProbe({
            phase: "control_frame",
            object,
            status: String(payload.status || ""),
            run_id: String(
              (payload.metadata as Record<string, unknown> | undefined)
                ?.run_id ||
                (payload.receipt as Record<string, unknown> | undefined)
                  ?.run_id ||
                "",
            ),
          });
          if (object === "runtime_progress") {
            setSessionsState((previous) => {
              const next = applyRuntimeProgressFrame(
                previous,
                targetSessionId,
                payload,
              );
              sessionsRef.current = next;
              return next;
            });
          }
          return;
        }
        const messageId = streamPayloadMessageId(payload);
        const messageSlot = slotForMessage(messageId);
        if (object === "message" && messageId) {
          messageTypes.set(messageId, normalizeStreamMessageType(payload.type));
        }
        const messageTypeKnown =
          Boolean(messageId) && messageTypes.has(messageId);
        if (object === "content" && messageId) {
          const messageType = messageTypes.get(messageId);
          if (messageType) {
            payload = { ...payload, message_type: messageType };
          }
        }
        const terminalResponseStatus =
          payload.object === "response" &&
          (payload.status === "completed" || payload.status === "failed")
            ? (payload.status as "completed" | "failed")
            : undefined;
        if (terminalResponseStatus) {
          terminalResponseCount += 1;
        }
        const payloadText = streamPayloadText(payload);
        const payloadType = visibleStreamPayloadType(payload);
        const isVisibleReasoning =
          payloadText.length > 0 &&
          payloadType === LantaiRuntimeMessageType.REASONING;
        const isVisibleAnswer =
          payloadText.length > 0 &&
          payloadType === LantaiRuntimeMessageType.MESSAGE;
        recordChatStreamProbe({
          phase: "classified",
          object,
          status: String(payload.status || ""),
          delta: payload.delta === true,
          message_slot: messageSlot,
          message_id_present: Boolean(messageId),
          message_type_known: messageTypeKnown,
          resolved_type: payloadType,
          text_length: payloadText.length,
          visible_reasoning: isVisibleReasoning,
          visible_answer: isVisibleAnswer,
          thinking_started: thinkingPresentationStartedAt > 0,
          live_thinking_in_dom: Boolean(
            document.querySelector(LIVE_THINKING_SELECTOR),
          ),
        });

        if (isVisibleAnswer && thinkingPresentationStartedAt > 0) {
          recordChatStreamProbe({
            phase: "answer_wait_start",
            message_slot: messageSlot,
            barrier_present: Boolean(thinkingPresentationBarrier),
            elapsed_since_thinking_ms:
              Date.now() - thinkingPresentationStartedAt,
            live_thinking_in_dom: Boolean(
              document.querySelector(LIVE_THINKING_SELECTOR),
            ),
          });
          await Promise.all([
            thinkingPresentationBarrier || Promise.resolve(),
            waitForThinkingPresentation(
              MIN_THINKING_PRESENTATION_MS -
                (Date.now() - thinkingPresentationStartedAt),
              signal,
            ),
          ]);
          if (signal?.aborted) return;
          recordChatStreamProbe({
            phase: "answer_wait_end",
            message_slot: messageSlot,
            elapsed_since_thinking_ms:
              Date.now() - thinkingPresentationStartedAt,
            live_thinking_in_dom: Boolean(
              document.querySelector(LIVE_THINKING_SELECTOR),
            ),
          });
          thinkingPresentationStartedAt = 0;
          thinkingPresentationBarrier = null;
        }

        if (isVisibleReasoning && thinkingPresentationStartedAt === 0) {
          const previousThinking = thinkingPresentationSnapshot();
          presentation.enqueue(payload);
          flushSync(() => presentation.flushNow());
          thinkingPresentationStartedAt = Date.now();
          recordChatStreamProbe({
            phase: "reasoning_flush",
            message_slot: messageSlot,
            live_thinking_in_dom: Boolean(
              document.querySelector(LIVE_THINKING_SELECTOR),
            ),
          });
          thinkingPresentationBarrier = waitForCommittedThinkingFrame(
            previousThinking,
            signal,
          );
          return;
        }
        presentation.enqueue(payload);
        if (terminalResponseStatus) {
          // A response terminal is the visible ownership boundary. Do not
          // wait for transport EOF (or a delayed session refresh) before
          // restoring the composer for this attempt.
          presentation.flushNow();
          const targetRunState = getSessionRunState(targetSessionId);
          targetRunState.awaitingVisibleAssistantOutput = false;
          targetRunState.loading = false;
          targetRunState.submitting = false;
          targetRunState.stopping = false;
          setPrimaryStreamActive(
            targetSessionId,
            false,
            false,
            terminalResponseStatus,
          );
          setSessionLoading(targetSessionId, false);
        }
      };
      const appendStreamEndFailure = () => {
        if (payloadCount > 0 && terminalResponseCount > 0) return;
        const message =
          payloadCount === 0
            ? response.ok
              ? "后端没有返回任何响应事件"
              : `请求失败：HTTP ${response.status}`
            : "后端连接已结束但没有返回完成事件";
        appendSyntheticPayload(
          {
            object: "response",
            status: "failed",
            type: "response.failed",
            error: {
              message,
            },
          },
          targetSessionId,
        );
      };
      const contentType = response.headers.get("Content-Type") || "";
      const isEventStream =
        !contentType || contentType.toLowerCase().includes("text/event-stream");

      if (!isEventStream) {
        const text = await response.text();
        await appendRawPayload(text);
        presentation.flushNow();
        await presentation.whenIdle();
        appendStreamEndFailure();
        return;
      }
      if (!response.body) {
        const text = await response.text();
        await appendRawPayload(text);
        presentation.flushNow();
        await presentation.whenIdle();
        appendStreamEndFailure();
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = parseSsePayloads(buffer);
          buffer = parsed.rest;
          for (const raw of parsed.payloads) {
            await appendRawPayload(raw);
          }
          if (terminalResponseCount > 0) {
            // The terminal response is the protocol boundary. Cancel our
            // reader so a server that leaves HTTP open cannot keep this turn
            // active or trap the next message in the guidance queue.
            await reader.cancel();
            break;
          }
        }
        buffer += decoder.decode();
        const parsed = parseSsePayloads(`${buffer}\n\n`);
        for (const raw of parsed.payloads) {
          await appendRawPayload(raw);
        }
        presentation.flushNow();
        await presentation.whenIdle();
      } catch (error) {
        presentation.flushNow();
        throw error;
      }
      appendStreamEndFailure();
    },
    [
      appendPayloadBatch,
      appendSyntheticPayload,
      getSessionRunState,
      options.api,
      setPrimaryStreamActive,
      setSessionLoading,
      syncSessionMessages,
    ],
  );

  const refreshSessionsAfterTurn = useCallback(
    async (sessionId?: string) => {
      try {
        const refreshed = await sessionApi?.getSessionList?.();
        if (refreshed) {
          // A list refresh only owns session metadata. The direct model stream
          // owns visible content until the turn has settled, so a stale list
          // snapshot must never erase an optimistic user card or streamed card.
          setSessions((previous) => {
            return refreshed.map((session) => {
              const previousSession = findSameSessionForStreamPreservation(
                previous,
                previous,
                refreshed,
                session,
              );
              const sessionWithMessages = previousSession?.messages?.length
                ? {
                    ...session,
                    messages: previousSession.messages,
                    __preserveMessages: true,
                    __primaryStreamActive:
                      previousSession.__primaryStreamActive,
                    __primaryStreamSettledAt:
                      previousSession.__primaryStreamSettledAt,
                  }
                : session;
              return preserveLocallySettledRuntimeState(
                previousSession,
                sessionWithMessages,
              );
            });
          });
        }
      } catch {
        // A title refresh must never turn a completed reply into an error.
      }

      if (!sessionId || !sessionApi?.getSession) return;
      try {
        const hydrated = await sessionApi.getSession(sessionId);
        setSessions((previous) => {
          const target = findSessionByStableIdentity(previous, sessionId);
          if (!target) return previous;
          return previous.map((session) => {
            if (session !== target) return session;
            const hydratedSession = {
              ...session,
              ...hydrated,
              id: session.id,
              messages: chooseActiveSessionMessages(
                session.messages || [],
                hydrated.messages || [],
              ),
              __preserveMessages: true,
              __primaryStreamActive: false,
              __primaryStreamSettledAt:
                session.__primaryStreamSettledAt || Date.now(),
            };
            return preserveLocallySettledRuntimeState(session, hydratedSession);
          });
        });
      } catch {
        // The completed live response remains usable when history persistence is
        // briefly unavailable; the runtime ledger can retry reconciliation.
      }
    },
    [sessionApi, setSessions],
  );

  const readCurrentDraft = useCallback((): SubmissionDraft => {
    return {
      text: normalizeText(inputValueRef.current),
      attachments: attachments.map((attachment) => ({ ...attachment })),
      ...(replacingControlInput &&
      replacingControlInput.sessionId === currentSessionIdRef.current
        ? {
            replacesCommandId: replacingControlInput.commandId,
            replacesMessageId: replacingControlInput.messageId,
          }
        : {}),
    };
  }, [attachments, replacingControlInput]);

  const clearInputDraft = useCallback(() => {
    const textarea = document.querySelector<HTMLTextAreaElement>(
      '[class*="sender"] textarea',
    );
    updateInputValue("");
    setSelectedCommand(null);
    if (textarea) textarea.value = "";
    attachmentsRef.current.forEach((attachment) => {
      revokeAttachmentPreview(attachment.previewUrl);
    });
    updateAttachments([]);
    setEditingMessageId(null);
    setReplacingControlInput((current) =>
      current?.sessionId === currentSessionIdRef.current ? null : current,
    );
  }, [revokeAttachmentPreview, updateAttachments, updateInputValue]);

  const updateRuntimeQueueMessage = useCallback(
    (
      sessionId: string,
      messageId: string,
      status: RuntimeQueueStatus,
      commandId?: string,
      waitingReason?: string,
      errorCode?: string | null,
      errorMessage?: string | null,
    ) => {
      const session = sessionsRef.current.find((item) => item.id === sessionId);
      const source =
        currentSessionIdRef.current === sessionId
          ? messagesRef.current
          : session?.messages || [];
      const nextMessages = source.map((message) => {
        if (message.id !== messageId) return message;
        const withStatus = withRuntimeQueueStatus(message, status);
        return commandId ||
          waitingReason ||
          errorCode !== undefined ||
          errorMessage !== undefined
          ? withRuntimeControlMetadata(withStatus, {
              commandId,
              waitingReason,
              errorCode,
              errorMessage,
            })
          : withStatus;
      });
      const updatedMessage = nextMessages.find(
        (message) => message.id === messageId,
      );
      if (updatedMessage) {
        const runState = getSessionRunState(sessionId);
        const queueIndex = runState.queue.findIndex(
          (draft) => draft.userMessage?.id === messageId,
        );
        if (queueIndex >= 0) {
          runState.queue[queueIndex] = {
            ...runState.queue[queueIndex],
            userMessage: updatedMessage,
          };
        }
      }
      if (currentSessionIdRef.current === sessionId) {
        messagesRef.current = nextMessages;
        setMessages(nextMessages);
      }
      syncSessionMessages(sessionId, nextMessages);
    },
    [getSessionRunState, syncSessionMessages],
  );

  const reconcileControlInputStatus = useCallback(
    async (
      ownerSessionId: string,
      messageId: string,
      querySessionId: string,
      queryChatId: string,
      commandId: string,
      pollDelays: number[] = [0, 250, 750, 1500, 3000],
    ): Promise<RuntimeQueueStatus | null> => {
      const readStatus = options.api?.getControlInputStatus;
      if (typeof readStatus !== "function") return null;
      for (const delay of pollDelays) {
        if (delay > 0) {
          await new Promise<void>((resolve) => {
            window.setTimeout(resolve, delay);
          });
        }
        let receipt: Record<string, unknown>;
        try {
          receipt = await readStatus({
            session_id: querySessionId,
            chat_id: queryChatId,
            command_id: commandId,
          });
        } catch {
          continue;
        }
        const status = runtimeQueueStatusFromExecution(
          receipt?.execution_status,
        );
        if (!status) continue;
        updateRuntimeQueueMessage(
          ownerSessionId,
          messageId,
          status,
          undefined,
          String(receipt?.next_waiting_reason || "").trim() || undefined,
        );
        if (["applied", "cancelled", "superseded", "failed"].includes(status)) {
          return status;
        }
      }
      return null;
    },
    [options.api, updateRuntimeQueueMessage],
  );

  useEffect(() => {
    if (
      controlInputHydrationRevision === 0 ||
      !currentSessionId ||
      typeof options.api?.getControlInputStatus !== "function"
    ) {
      return;
    }
    const reconciliationState = reconciledHydrationControlInputsRef.current;
    if (reconciliationState.revision !== controlInputHydrationRevision) {
      reconciliationState.revision = controlInputHydrationRevision;
      reconciliationState.keys.clear();
    }
    const stableSession = findSessionByStableIdentity(
      sessionsRef.current,
      currentSessionId,
    );
    const canonicalSessionId = stableSession?.id || currentSessionId;
    const transportSessionId =
      sessionIdFromSession(stableSession) || currentSessionId;
    for (const message of messagesRef.current) {
      const status = runtimeQueueStatus(message);
      if (
        message.role !== "user" ||
        !["sending", "uncertain", "admitted", "claimed", "bound"].includes(
          String(status),
        )
      ) {
        continue;
      }
      const commandId = runtimeControlCommandId(message);
      if (!commandId) continue;
      const key = `${currentSessionId}:${message.id}:${commandId}`;
      if (reconciliationState.keys.has(key)) continue;
      reconciliationState.keys.add(key);
      if (status === "sending") {
        // A page reload severs the original response transport. Until the
        // durable receipt is found, "sending" is no longer a truthful state.
        updateRuntimeQueueMessage(
          currentSessionId,
          message.id,
          "uncertain",
          commandId,
        );
      }
      void reconcileControlInputStatus(
        currentSessionId,
        message.id,
        transportSessionId,
        canonicalSessionId,
        commandId,
      );
    }
  }, [
    controlInputHydrationRevision,
    currentSessionId,
    options.api,
    reconcileControlInputStatus,
    updateRuntimeQueueMessage,
  ]);

  const enqueueGuidanceDraft = useCallback(
    (sessionId: string, draft: SubmissionDraft) => {
      const runState = getSessionRunState(sessionId);
      const session = sessionsRef.current.find((item) => item.id === sessionId);
      const target = runtimeControlTarget(session);
      const queuedUserMessage = withRuntimeQueueStatus(
        withRuntimeControlMetadata(
          draft.userMessage || buildUserMessage(draft.text, draft.attachments),
          {
            replacesCommandId: draft.replacesCommandId,
            replacesMessageId: draft.replacesMessageId,
            controlIntent: draft.controlIntent,
            expectedRunId: target.expectedRunId,
            expectedAttemptId: target.expectedAttemptId,
          },
        ),
        "queued",
      );
      const baseMessages =
        currentSessionIdRef.current === sessionId
          ? messagesRef.current
          : session?.messages || [];
      const existingIndex = baseMessages.findIndex(
        (message) => message.id === queuedUserMessage.id,
      );
      const nextMessages =
        existingIndex >= 0
          ? baseMessages.map((message, index) =>
              index === existingIndex ? queuedUserMessage : message,
            )
          : [...baseMessages, queuedUserMessage];
      const queuedDraft = {
        ...draft,
        ...target,
        userMessage: queuedUserMessage,
      };
      const queueIndex = runState.queue.findIndex(
        (item) => item.userMessage?.id === queuedUserMessage.id,
      );
      if (queueIndex >= 0) {
        runState.queue[queueIndex] = queuedDraft;
      } else {
        runState.queue.push(queuedDraft);
      }
      if (currentSessionIdRef.current === sessionId) {
        messagesRef.current = nextMessages;
        flushSync(() => setMessages(nextMessages));
      }
      syncSessionMessages(sessionId, nextMessages);
      clearInputDraft();
      return queuedUserMessage;
    },
    [clearInputDraft, getSessionRunState, setMessages, syncSessionMessages],
  );

  const persistGuidanceDraft = useCallback(
    (
      sessionId: string,
      messageId: string,
      update: (draft: SubmissionDraft) => SubmissionDraft,
    ): SubmissionDraft | null => {
      const runState = getSessionRunState(sessionId);
      const queueIndex = runState.queue.findIndex(
        (draft) => draft.userMessage?.id === messageId,
      );
      if (queueIndex < 0) return null;
      const nextDraft = update(runState.queue[queueIndex]);
      if (!nextDraft.userMessage) return null;
      runState.queue[queueIndex] = nextDraft;

      const session = sessionsRef.current.find((item) => item.id === sessionId);
      const source =
        currentSessionIdRef.current === sessionId
          ? messagesRef.current
          : session?.messages || [];
      const nextMessages = source.map((item) =>
        item.id === messageId ? nextDraft.userMessage! : item,
      );
      if (currentSessionIdRef.current === sessionId) {
        messagesRef.current = nextMessages;
        flushSync(() => setMessages(nextMessages));
      }
      syncSessionMessages(sessionId, nextMessages);
      return nextDraft;
    },
    [getSessionRunState, setMessages, syncSessionMessages],
  );

  const removeGuidanceDraft = useCallback(
    (sessionId: string, messageId: string): SubmissionDraft | null => {
      const runState = getSessionRunState(sessionId);
      const queueIndex = runState.queue.findIndex(
        (draft) => draft.userMessage?.id === messageId,
      );
      const [removed] =
        queueIndex >= 0 ? runState.queue.splice(queueIndex, 1) : [];
      const session = sessionsRef.current.find((item) => item.id === sessionId);
      const source =
        currentSessionIdRef.current === sessionId
          ? messagesRef.current
          : session?.messages || [];
      const nextMessages = source.filter((message) => message.id !== messageId);
      if (currentSessionIdRef.current === sessionId) {
        messagesRef.current = nextMessages;
        flushSync(() => setMessages(nextMessages));
      }
      syncSessionMessages(sessionId, nextMessages);
      return removed || null;
    },
    [getSessionRunState, setMessages, syncSessionMessages],
  );

  const steerActiveRun = useCallback(
    async (
      sessionId: string,
      initialDraft: SubmissionDraft,
    ): Promise<SteerActiveRunResult> => {
      const runState = getSessionRunState(sessionId);
      if (!isSessionRunActive(sessionId)) return "inactive";

      let draft = initialDraft;
      if (!draft.text && draft.attachments.length === 0) return "failed";
      if (shouldConfirmAttachmentRoles(draft.attachments)) {
        const confirmed = await confirmAttachmentRoleReview(draft.attachments);
        if (!confirmed) return "failed";
        draft = {
          ...draft,
          attachments: draft.attachments.map((attachment) => ({
            ...attachment,
            role: attachment.role || DEFAULT_ATTACHMENT_ROLE,
            roleConfirmed: true,
          })),
        };
      }

      const userMessage = withRuntimeQueueStatus(
        withRuntimeControlMetadata(
          draft.userMessage || buildUserMessage(draft.text, draft.attachments),
          {
            replacesCommandId: draft.replacesCommandId,
            replacesMessageId: draft.replacesMessageId,
            controlIntent: draft.controlIntent || "redirect",
            waitingReason: null,
            expectedRunId: draft.expectedRunId,
            expectedAttemptId: draft.expectedAttemptId,
            errorCode: null,
            errorMessage: null,
          },
        ),
        "sending",
      );
      const session = sessionsRef.current.find((item) => item.id === sessionId);
      const target = {
        ...runtimeControlTarget(session),
        ...(draft.expectedRunId ? { expectedRunId: draft.expectedRunId } : {}),
        ...(draft.expectedAttemptId
          ? { expectedAttemptId: draft.expectedAttemptId }
          : {}),
      };
      const baseMessages =
        currentSessionIdRef.current === sessionId
          ? messagesRef.current
          : session?.messages || [];
      const queuedMessageIndex = baseMessages.findIndex(
        (message) => message.id === userMessage.id,
      );
      const nextMessages =
        queuedMessageIndex >= 0
          ? baseMessages.map((message, index) =>
              index === queuedMessageIndex ? userMessage : message,
            )
          : [...baseMessages, userMessage];
      if (currentSessionIdRef.current === sessionId) {
        messagesRef.current = nextMessages;
        flushSync(() => {
          setMessages(nextMessages);
        });
        if (queuedMessageIndex < 0) {
          positionSubmittedMessageForReading(userMessage.id);
        }
      }
      syncSessionMessages(sessionId, nextMessages);

      const controller = new AbortController();
      runState.steerControllers.add(controller);
      try {
        const input =
          (userMessage.cards?.[0]?.data?.input as Array<
            Record<string, unknown>
          >) || [];
        const response = await options.api?.fetch?.({
          input: input.map((item) => ({
            ...item,
            session: {
              session_id: sessionIdFromSession(session),
              user_id: String(session?.userId || "default"),
              channel: String(session?.channel || "console"),
            },
          })),
          biz_params: {
            ...requestBizParamsForTurn(options.api, {
              client_turn_id: userMessage.id,
              control_intent: draft.controlIntent || "redirect",
              ...(draft.replacesCommandId
                ? { replaces_command_id: draft.replacesCommandId }
                : {}),
              ...(target.expectedRunId
                ? { expected_run_id: target.expectedRunId }
                : {}),
              ...(target.expectedAttemptId
                ? { expected_attempt_id: target.expectedAttemptId }
                : {}),
            }),
            operation: "steer",
          },
          signal: controller.signal,
        });
        if (!(response instanceof Response)) {
          throw new Error("Steer request returned no response");
        }
        const receipt = await readTurnReceiptStatus(
          response,
          Number(options.api?.turnReceiptTimeoutMs),
        );
        if (receipt.receiptStatus === "accepted") {
          throw new Error("The active run already settled");
        }
        if (receipt.receiptStatus === "admitted") {
          const projectedStatus =
            runtimeQueueStatusFromExecution(receipt.executionStatus) ||
            "admitted";
          updateRuntimeQueueMessage(
            sessionId,
            userMessage.id,
            projectedStatus,
            receipt.commandId,
            receipt.waitingReason,
          );
          if (draft.replacesMessageId) {
            updateRuntimeQueueMessage(
              sessionId,
              draft.replacesMessageId,
              "superseded",
            );
          }
          if (
            !["applied", "cancelled", "superseded", "failed"].includes(
              projectedStatus,
            )
          ) {
            void reconcileControlInputStatus(
              sessionId,
              userMessage.id,
              sessionIdFromSession(session),
              String(session?.id || sessionId),
              receipt.commandId || userMessage.id,
            );
          }
          return "admitted";
        }
        updateRuntimeQueueMessage(sessionId, userMessage.id, "steered");
        return receipt.receiptStatus;
      } catch (error) {
        const explicitHttpRejection =
          error instanceof TurnControlHttpError &&
          [400, 401, 403, 409, 429].includes(error.status);
        const legacyHttpRejection =
          !(error instanceof TurnControlHttpError) &&
          /\bHTTP (400|401|403|409|429)\b/.test(String(error));
        const knownRejection =
          explicitHttpRejection ||
          legacyHttpRejection ||
          error instanceof TurnReceiptProtocolError;
        if (!explicitHttpRejection) {
          const recovered = await reconcileControlInputStatus(
            sessionId,
            userMessage.id,
            sessionIdFromSession(session),
            String(session?.id || sessionId),
            userMessage.id,
            [0],
          );
          if (recovered) return "admitted";
        }
        const errorCode =
          error instanceof TurnControlHttpError
            ? error.code
            : error instanceof TurnReceiptProtocolError
              ? "turn_receipt_protocol_error"
              : /queue_full/i.test(String(error))
                ? "control_queue_full"
                : null;
        const errorMessage =
          error instanceof Error ? error.message : String(error || "");
        updateRuntimeQueueMessage(
          sessionId,
          userMessage.id,
          knownRejection ? "failed" : "uncertain",
          undefined,
          undefined,
          errorCode,
          errorMessage || null,
        );
        if (!knownRejection) {
          void reconcileControlInputStatus(
            sessionId,
            userMessage.id,
            sessionIdFromSession(session),
            String(session?.id || sessionId),
            userMessage.id,
            [250, 750, 1500, 3000],
          );
        }
        return "failed";
      } finally {
        runState.steerControllers.delete(controller);
      }
    },
    [
      getSessionRunState,
      isSessionRunActive,
      options.api,
      options.sender,
      positionSubmittedMessageForReading,
      reconcileControlInputStatus,
      setMessages,
      syncSessionMessages,
      updateRuntimeQueueMessage,
    ],
  );

  const submitGuidanceIntent = useCallback(
    async (message: ILantaiChatMessage, intent: ControlInputIntent) => {
      const sessionId = currentSessionIdRef.current || "";
      if (!sessionId) return;
      const runState = getSessionRunState(sessionId);
      if (!isSessionRunActive(sessionId)) return;
      const queueIndex = runState.queue.findIndex(
        (draft) => draft.userMessage?.id === message.id,
      );
      if (queueIndex < 0) return;
      const session = findSessionByStableIdentity(
        sessionsRef.current,
        sessionId,
      );
      const currentTarget = runtimeControlTarget(session);
      const queuedDraft = runState.queue[queueIndex];
      const target = {
        expectedRunId: queuedDraft.expectedRunId || currentTarget.expectedRunId,
        expectedAttemptId:
          queuedDraft.expectedAttemptId || currentTarget.expectedAttemptId,
      };
      if (!hasCompleteRuntimeControlTarget(target)) {
        persistGuidanceDraft(sessionId, message.id, (draft) => ({
          ...draft,
          ...target,
          controlIntent: intent,
          userMessage: withRuntimeControlMetadata(
            withRuntimeQueueStatus(draft.userMessage || message, "queued"),
            {
              controlIntent: intent,
              waitingReason: "awaiting_run_identity",
              expectedRunId: target.expectedRunId,
              expectedAttemptId: target.expectedAttemptId,
            },
          ),
        }));
        return;
      }
      const draft = persistGuidanceDraft(sessionId, message.id, (current) => ({
        ...current,
        ...target,
        controlIntent: intent,
        userMessage: withRuntimeControlMetadata(
          current.userMessage || message,
          {
            controlIntent: intent,
            waitingReason: null,
            expectedRunId: target.expectedRunId,
            expectedAttemptId: target.expectedAttemptId,
          },
        ),
      })) || { ...queuedDraft, ...target, controlIntent: intent };
      const result = await steerActiveRun(sessionId, draft);
      if (
        result === "admitted" ||
        result === "steered" ||
        result === "queued"
      ) {
        const currentIndex = runState.queue.findIndex(
          (item) => item.userMessage?.id === message.id,
        );
        if (currentIndex >= 0) runState.queue.splice(currentIndex, 1);
      }
    },
    [
      getSessionRunState,
      isSessionRunActive,
      persistGuidanceDraft,
      steerActiveRun,
    ],
  );

  useEffect(() => {
    const sessionId = currentSessionId || "";
    if (!sessionId || !isSessionRunActive(sessionId)) return;
    const session = findSessionByStableIdentity(sessionsRef.current, sessionId);
    const currentTarget = runtimeControlTarget(session);
    if (!hasCompleteRuntimeControlTarget(currentTarget)) return;

    const runState = getSessionRunState(sessionId);
    const pendingDraft = runState.queue.find((draft) => {
      const message = draft.userMessage;
      return Boolean(
        message &&
        draft.controlIntent &&
        runtimeControlWaitingReason(message) === "awaiting_run_identity" &&
        !runState.bindingGuidanceMessageIds.has(message.id),
      );
    });
    const messageId = pendingDraft?.userMessage?.id;
    if (!pendingDraft || !messageId || !pendingDraft.controlIntent) return;
    if (
      (pendingDraft.expectedRunId &&
        pendingDraft.expectedRunId !== currentTarget.expectedRunId) ||
      (pendingDraft.expectedAttemptId &&
        pendingDraft.expectedAttemptId !== currentTarget.expectedAttemptId)
    ) {
      return;
    }

    const boundDraft = persistGuidanceDraft(sessionId, messageId, (draft) => ({
      ...draft,
      ...currentTarget,
      userMessage: withRuntimeControlMetadata(draft.userMessage!, {
        controlIntent: draft.controlIntent,
        waitingReason: null,
        expectedRunId: currentTarget.expectedRunId,
        expectedAttemptId: currentTarget.expectedAttemptId,
      }),
    }));
    if (!boundDraft) return;
    runState.bindingGuidanceMessageIds.add(messageId);
    void steerActiveRun(sessionId, boundDraft)
      .then((result) => {
        if (
          result === "admitted" ||
          result === "steered" ||
          result === "queued"
        ) {
          const queueIndex = runState.queue.findIndex(
            (draft) => draft.userMessage?.id === messageId,
          );
          if (queueIndex >= 0) runState.queue.splice(queueIndex, 1);
        }
      })
      .finally(() => {
        runState.bindingGuidanceMessageIds.delete(messageId);
      });
  }, [
    currentSessionId,
    getSessionRunState,
    isSessionRunActive,
    persistGuidanceDraft,
    sessions,
    steerActiveRun,
  ]);

  const editGuidanceDraft = useCallback(
    (message: ILantaiChatMessage) => {
      const sessionId = currentSessionIdRef.current || "";
      if (!sessionId) return;
      const draft =
        removeGuidanceDraft(sessionId, message.id) ||
        draftFromUserMessage(message);
      updateInputValue(draft.text);
      attachmentsRef.current.forEach((attachment) => {
        revokeAttachmentPreview(attachment.previewUrl);
      });
      updateAttachments(draft.attachments);
      setEditingMessageId(null);
      window.requestAnimationFrame(() => {
        const textarea = document.querySelector<HTMLTextAreaElement>(
          '[class*="sender"] textarea',
        );
        textarea?.focus();
        const end = textarea?.value.length || 0;
        textarea?.setSelectionRange(end, end);
      });
    },
    [
      removeGuidanceDraft,
      revokeAttachmentPreview,
      updateAttachments,
      updateInputValue,
    ],
  );

  const deleteGuidanceDraft = useCallback(
    (message: ILantaiChatMessage) => {
      const sessionId = currentSessionIdRef.current || "";
      if (!sessionId) return;
      removeGuidanceDraft(sessionId, message.id);
    },
    [removeGuidanceDraft],
  );

  const submit = useCallback(
    async (draft?: SubmissionDraft, requestedSessionId?: string) => {
      let submissionSessionId =
        requestedSessionId || currentSessionIdRef.current || "";
      if (!submissionSessionId) {
        if (pendingInitialSubmissionRef.current) return;
        pendingInitialSubmissionRef.current = true;
        await sessionInitializationRef.current;
        pendingInitialSubmissionRef.current = false;
        submissionSessionId = currentSessionIdRef.current || "";
      }
      if (!submissionSessionId) {
        appendSyntheticPayload({
          object: "response",
          status: "failed",
          type: "response.failed",
          error: { message: "会话尚未初始化，请稍后重试" },
        });
        return;
      }

      let runState = getSessionRunState(submissionSessionId);
      if (runState.loading || runState.submitting) return;
      runState.confirmedStopTurnKeys.clear();
      runState.submitting = true;
      runState.loading = true;
      setSessionLoading(submissionSessionId, true);

      const finishBeforeStream = () => {
        runState.submitting = false;
        runState.loading = false;
        setSessionLoading(submissionSessionId, false);
      };

      let allowed: boolean | void;
      try {
        allowed = await options.sender?.beforeSubmit?.();
      } catch (error) {
        finishBeforeStream();
        appendSyntheticPayload(
          {
            object: "response",
            status: "failed",
            type: "response.failed",
            error: {
              message:
                error instanceof Error
                  ? error.message
                  : String(error || "发送前处理失败"),
            },
          },
          submissionSessionId,
        );
        return;
      }
      if (allowed === false) {
        finishBeforeStream();
        return;
      }

      let submissionDraft = draft || readCurrentDraft();
      if (shouldConfirmAttachmentRoles(submissionDraft.attachments)) {
        const confirmed = await confirmAttachmentRoleReview(
          submissionDraft.attachments,
        );
        if (!confirmed) {
          finishBeforeStream();
          return;
        }
        submissionDraft = {
          ...submissionDraft,
          attachments: submissionDraft.attachments.map((attachment) => ({
            ...attachment,
            role: attachment.role || DEFAULT_ATTACHMENT_ROLE,
            roleConfirmed: true,
          })),
        };
      }
      const text = submissionDraft.text;
      const submissionAttachments = submissionDraft.attachments;
      if (!text && submissionAttachments.length === 0) {
        finishBeforeStream();
        return;
      }

      if (submissionDraft.branchFromMessageId) {
        const sourceSessionId = submissionSessionId;
        setBranchingMessageId(submissionDraft.branchFromMessageId);
        scrollMessagesToEnd();
        try {
          const branched = await sessionApi?.branchSession?.(
            sourceSessionId,
            submissionDraft.branchFromMessageId,
          );
          const normalizedBranched = normalizeSessionsMessages(branched || []);
          const branch = normalizedBranched[0];
          if (!branch?.id) {
            throw new Error("无法创建消息分支");
          }
          runState.submitting = false;
          runState.loading = false;
          sessionsRef.current = normalizedBranched;
          setSessionsState(normalizedBranched);
          currentSessionIdRef.current = branch.id;
          setCurrentSessionIdState(branch.id);
          submissionSessionId = branch.id;
          runState = getSessionRunState(submissionSessionId);
          runState.submitting = true;
          runState.loading = true;
          setSessionLoading(submissionSessionId, true);
          const branchMessages = mergeAdjacentAssistantResponseCards(
            branch.messages || [],
          );
          messagesRef.current = branchMessages;
          rememberVisibleMessagesSessionIds(
            branch.id,
            branch.realId,
            branch.sessionId,
          );
          setMessages(branchMessages);
          localMessagesSessionIdsRef.current = new Set();
        } catch (error) {
          finishBeforeStream();
          setBranchingMessageId(null);
          appendSyntheticPayload(
            {
              object: "response",
              status: "failed",
              type: "response.failed",
              error: {
                message:
                  error instanceof Error
                    ? error.message
                    : String(error || "创建消息分支失败"),
              },
            },
            sourceSessionId,
          );
          return;
        }
      }

      const userMessage = submissionDraft.userMessage
        ? withoutRuntimeQueueStatus(submissionDraft.userMessage)
        : buildUserMessage(text, submissionAttachments);
      const pendingAssistantMessage = buildOptimisticAssistantMessage();
      const submissionSession = sessionsRef.current.find(
        (item) => item.id === submissionSessionId,
      );
      const baseMessages =
        currentSessionIdRef.current === submissionSessionId
          ? messagesRef.current
          : submissionSession?.messages || [];
      if (!baseMessages.some((message) => !isQueuedGuidanceMessage(message))) {
        beginFirstTurnWelcomeExit();
      }
      const optimisticUserIndex = submissionDraft.userMessage
        ? baseMessages.findIndex(
            (message) => message.id === submissionDraft.userMessage?.id,
          )
        : -1;
      const nextMessages =
        optimisticUserIndex >= 0
          ? [
              ...baseMessages.slice(0, optimisticUserIndex),
              userMessage,
              pendingAssistantMessage,
              ...baseMessages.slice(optimisticUserIndex + 1),
            ]
          : [...baseMessages, userMessage, pendingAssistantMessage];
      const isActiveSession =
        currentSessionIdRef.current === submissionSessionId;
      runState.awaitingVisibleAssistantOutput = true;
      if (isActiveSession) {
        autoFollowEnabledRef.current = true;
        messagesRef.current = nextMessages;
        flushSync(() => {
          // Commit the new turn's session state in the same paint as its
          // optimistic assistant card. Otherwise a completed previous turn
          // can briefly make the new card look stale and render an empty
          // assistant bubble before the first response event arrives.
          syncSessionMessages(submissionSessionId, nextMessages, {
            primaryStreamActive: true,
            generating: true,
          });
          setMessages(nextMessages);
          setBranchingMessageId(null);
          if (!submissionDraft.userMessage) {
            clearInputDraft();
          }
        });
        positionSubmittedMessageForReading(userMessage.id);
      } else {
        syncSessionMessages(submissionSessionId, nextMessages, {
          primaryStreamActive: true,
          generating: true,
        });
      }
      runState.submitting = false;
      if (isActiveSession) {
        await waitForPendingAssistantPaint();
        positionSubmittedMessageForReading(userMessage.id);
      }

      const controller = new AbortController();
      runState.controller = controller;
      try {
        const session = sessionsRef.current.find(
          (item) => item.id === submissionSessionId,
        );
        const input =
          (userMessage.cards?.[0]?.data?.input as Array<
            Record<string, unknown>
          >) || [];
        const response = await options.api?.fetch?.({
          input: input.map((item) => ({
            ...item,
            session: {
              session_id: sessionIdFromSession(session),
              user_id: String(session?.userId || "default"),
              channel: String(session?.channel || "console"),
            },
          })),
          biz_params: requestBizParamsForTurn(options.api, {
            client_turn_id: userMessage.id,
          }),
          signal: controller.signal,
        });
        if (response instanceof Response) {
          await consumeResponse(
            response,
            submissionSessionId,
            controller.signal,
          );
        } else {
          appendSyntheticPayload(
            {
              object: "response",
              status: "failed",
              type: "response.failed",
              error: {
                message: "请求没有返回可读取的响应",
              },
            },
            submissionSessionId,
          );
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          appendSyntheticPayload(
            {
              object: "response",
              status: "failed",
              type: "response.failed",
              error: {
                message:
                  error instanceof Error
                    ? error.message
                    : String(error || "请求失败"),
              },
            },
            submissionSessionId,
          );
        }
      } finally {
        if (runState.controller === controller) {
          runState.awaitingVisibleAssistantOutput = false;
          setPrimaryStreamActive(submissionSessionId, false);
          runState.controller = null;
          runState.loading = false;
          runState.submitting = false;
          setSessionLoading(submissionSessionId, false);
          const durableRuntimeStillActive = sessionHasDurableRuntimeActivity(
            findSessionByStableIdentity(
              sessionsRef.current,
              submissionSessionId,
            ),
          );
          const nextDraft =
            durableRuntimeStillActive || runState.stopping
              ? undefined
              : takeNextAutoStartableQueuedDraft(runState.queue);
          if (nextDraft) {
            queueMicrotask(() => {
              void submitTaskRef.current(nextDraft, submissionSessionId);
            });
          }
        }
        void refreshSessionsAfterTurn(submissionSessionId);
      }
    },
    [
      appendSyntheticPayload,
      beginFirstTurnWelcomeExit,
      clearInputDraft,
      consumeResponse,
      getSessionRunState,
      options.api,
      options.sender,
      readCurrentDraft,
      refreshSessionsAfterTurn,
      sessionApi,
      setSessionLoading,
      setPrimaryStreamActive,
      positionSubmittedMessageForReading,
      scrollMessagesToEnd,
      syncSessionMessages,
    ],
  );
  submitTaskRef.current = submit;

  const latestUserMessageForRecovery = useCallback(() => {
    for (let index = messagesRef.current.length - 1; index >= 0; index -= 1) {
      const message = messagesRef.current[index];
      if (message.role === "user" && !isGuidanceStripMessage(message)) {
        return { message, index };
      }
    }
    return null;
  }, []);

  const showTransientNotice = useCallback((message: string) => {
    if (transientNoticeTimerRef.current) {
      clearTimeout(transientNoticeTimerRef.current);
    }
    setTransientNotice(message);
    transientNoticeTimerRef.current = setTimeout(() => {
      transientNoticeTimerRef.current = null;
      setTransientNotice(null);
    }, 4_500);
  }, []);

  const focusComposer = useCallback(() => {
    window.requestAnimationFrame(() => {
      const textarea = document.querySelector<HTMLTextAreaElement>(
        '[class*="sender"] textarea',
      );
      textarea?.focus();
      const end = textarea?.value.length || 0;
      textarea?.setSelectionRange(end, end);
    });
  }, []);

  const prepareRecoveryDraft = useCallback(
    (draft: SubmissionDraft, notice: string) => {
      const currentDraft = readCurrentDraft();
      if (currentDraft.text || currentDraft.attachments.length > 0) {
        showTransientNotice("输入框已有草稿，已保留；请检查后发送。");
        focusComposer();
        return false;
      }
      updateInputValue(draft.text);
      attachmentsRef.current.forEach((attachment) => {
        revokeAttachmentPreview(attachment.previewUrl);
      });
      updateAttachments(
        draft.attachments.map((attachment) => ({ ...attachment })),
      );
      showTransientNotice(notice);
      focusComposer();
      return true;
    },
    [
      focusComposer,
      readCurrentDraft,
      revokeAttachmentPreview,
      showTransientNotice,
      updateAttachments,
      updateInputValue,
    ],
  );

  const continueLatestGeneration = useCallback(() => {
    const sessionId = currentSessionIdRef.current || "";
    if (!sessionId || isSessionRunActive(sessionId)) return;
    setEditingMessageId(null);
    prepareRecoveryDraft(
      {
        text: "请从刚才停止的位置继续，不要重复已经完成的内容。",
        attachments: [],
      },
      "继续说明已放入输入框；检查后发送。",
    );
  }, [isSessionRunActive, prepareRecoveryDraft]);

  const retryLatestGeneration = useCallback(() => {
    const sessionId = currentSessionIdRef.current || "";
    if (!sessionId || isSessionRunActive(sessionId)) return;
    const latestUser = latestUserMessageForRecovery();
    if (!latestUser) return;
    const recoveryDraft = draftFromUserMessage(latestUser.message);
    if (
      prepareRecoveryDraft(recoveryDraft, "原请求已放回输入框；检查后发送。")
    ) {
      setEditingMessageId(
        sessionApi?.branchSession ? latestUser.message.id : null,
      );
    }
  }, [
    isSessionRunActive,
    latestUserMessageForRecovery,
    prepareRecoveryDraft,
    sessionApi,
  ]);

  useEffect(() => {
    const sessionId = currentSessionId || "";
    if (!sessionId || isSessionRunActive(sessionId)) return;
    const runState = getSessionRunState(sessionId);
    if (
      runState.loading ||
      runState.submitting ||
      runState.controller ||
      runState.confirmedStopTurnKeys.size > 0 ||
      runState.queue.length === 0
    ) {
      return;
    }
    const nextDraft = takeNextAutoStartableQueuedDraft(runState.queue);
    if (!nextDraft) return;
    queueMicrotask(() => {
      void submitTaskRef.current(nextDraft, sessionId);
    });
  }, [currentSessionId, getSessionRunState, isSessionRunActive, sessions]);

  const editUserMessage = useCallback(
    (message: ILantaiChatMessage) => {
      const draft = draftFromUserMessage(message);
      setEditingMessageId(message.id);
      setReplacingControlInput(null);
      updateInputValue(draft.text);
      attachmentsRef.current.forEach((attachment) => {
        revokeAttachmentPreview(attachment.previewUrl);
      });
      updateAttachments(draft.attachments);
      window.requestAnimationFrame(() => {
        const textarea = document.querySelector<HTMLTextAreaElement>(
          '[class*="sender"] textarea',
        );
        textarea?.focus();
        const end = textarea?.value.length || 0;
        textarea?.setSelectionRange(end, end);
      });
    },
    [revokeAttachmentPreview, updateAttachments, updateInputValue],
  );

  const requestBranchFromUserMessage = useCallback(
    (message: ILantaiChatMessage) => {
      const sessionId = currentSessionIdRef.current || "";
      if (isSessionRunActive(sessionId)) return;
      setPendingBranchMessage(message);
    },
    [isSessionRunActive],
  );

  const confirmBranchFromUserMessage = useCallback(() => {
    const message = pendingBranchMessage;
    setPendingBranchMessage(null);
    const sessionId = currentSessionIdRef.current || "";
    if (!message || isSessionRunActive(sessionId)) {
      return;
    }
    const draft = draftFromUserMessage(message);
    void submit(
      {
        ...draft,
        branchFromMessageId: message.id,
      },
      sessionId,
    );
  }, [isSessionRunActive, pendingBranchMessage, submit]);

  const cancelBranchFromUserMessage = useCallback(() => {
    setPendingBranchMessage(null);
  }, []);

  const cancelEditing = useCallback(() => {
    setEditingMessageId(null);
    updateInputValue("");
    attachmentsRef.current.forEach((attachment) => {
      revokeAttachmentPreview(attachment.previewUrl);
    });
    updateAttachments([]);
  }, [revokeAttachmentPreview, updateAttachments, updateInputValue]);

  const editPendingControlInput = useCallback(
    (message: ILantaiChatMessage) => {
      const sessionId = currentSessionIdRef.current || "";
      const status = runtimeQueueStatus(message);
      if (!sessionId || (status !== "admitted" && status !== "claimed")) {
        return;
      }
      const draft = draftFromUserMessage(message);
      setEditingMessageId(null);
      setReplacingControlInput({
        sessionId,
        messageId: message.id,
        commandId: runtimeControlCommandId(message),
      });
      updateInputValue(draft.text);
      attachmentsRef.current.forEach((attachment) => {
        revokeAttachmentPreview(attachment.previewUrl);
      });
      updateAttachments(draft.attachments);
      focusComposer();
    },
    [
      focusComposer,
      revokeAttachmentPreview,
      updateAttachments,
      updateInputValue,
    ],
  );

  const cancelPendingControlInput = useCallback(
    async (message: ILantaiChatMessage) => {
      const sessionId = currentSessionIdRef.current || "";
      const cancelControlInput = options.api?.cancelControlInput;
      const status = runtimeQueueStatus(message);
      if (
        !sessionId ||
        typeof cancelControlInput !== "function" ||
        (status !== "admitted" && status !== "claimed")
      ) {
        return;
      }
      const commandId = runtimeControlCommandId(message);
      const session = sessionsRef.current.find((item) => item.id === sessionId);
      try {
        const receipt = (await cancelControlInput({
          session_id: sessionIdFromSession(session),
          chat_id: String(session?.id || sessionId),
          command_id: commandId,
        })) as Record<string, unknown>;
        const projectedStatus =
          runtimeQueueStatusFromExecution(receipt?.execution_status) ||
          "cancelled";
        updateRuntimeQueueMessage(
          sessionId,
          message.id,
          projectedStatus,
          commandId,
        );
      } catch (error) {
        const detail = (error as { detail?: Record<string, unknown> })?.detail;
        const conflictStatus = runtimeQueueStatusFromExecution(
          detail?.execution_status,
        );
        if (conflictStatus) {
          updateRuntimeQueueMessage(
            sessionId,
            message.id,
            conflictStatus,
            commandId,
          );
          return;
        }
        const recovered = await reconcileControlInputStatus(
          sessionId,
          message.id,
          sessionIdFromSession(session),
          String(session?.id || sessionId),
          commandId,
          [0],
        );
        if (!recovered) {
          updateRuntimeQueueMessage(
            sessionId,
            message.id,
            "uncertain",
            commandId,
          );
        }
      }
    },
    [options.api, reconcileControlInputStatus, updateRuntimeQueueMessage],
  );

  const submitGuidanceCardIntent = useCallback(
    (message: ILantaiChatMessage, intent: ControlInputIntent) => {
      const status = runtimeQueueStatus(message);
      if (
        (status === "admitted" || status === "claimed") &&
        runtimeControlIntent(message) !== intent
      ) {
        const sessionId = currentSessionIdRef.current || "";
        if (!sessionId || !isSessionRunActive(sessionId)) return;
        const sourceDraft = draftFromUserMessage(message);
        const replacement = enqueueGuidanceDraft(sessionId, {
          ...sourceDraft,
          userMessage: undefined,
          controlIntent: intent,
          replacesCommandId: runtimeControlCommandId(message),
          replacesMessageId: message.id,
        });
        void submitGuidanceIntent(replacement, intent);
        return;
      }
      void submitGuidanceIntent(message, intent);
    },
    [enqueueGuidanceDraft, isSessionRunActive, submitGuidanceIntent],
  );

  const editGuidanceMessage = useCallback(
    (message: ILantaiChatMessage) => {
      const status = runtimeQueueStatus(message);
      if (status === "admitted" || status === "claimed") {
        editPendingControlInput(message);
        return;
      }
      editGuidanceDraft(message);
    },
    [editGuidanceDraft, editPendingControlInput],
  );

  const deleteGuidanceMessage = useCallback(
    (message: ILantaiChatMessage) => {
      const status = runtimeQueueStatus(message);
      if (status === "admitted" || status === "claimed") {
        void cancelPendingControlInput(message);
        return;
      }
      deleteGuidanceDraft(message);
    },
    [cancelPendingControlInput, deleteGuidanceDraft],
  );

  const cancelReplacingControlInput = useCallback(() => {
    setReplacingControlInput(null);
    updateInputValue("");
    attachmentsRef.current.forEach((attachment) => {
      revokeAttachmentPreview(attachment.previewUrl);
    });
    updateAttachments([]);
  }, [revokeAttachmentPreview, updateAttachments, updateInputValue]);

  const stop = useCallback(() => {
    setBranchingMessageId(null);
    const sessionId = currentSessionIdRef.current || "";
    if (!sessionId) return;
    const runState = getSessionRunState(sessionId);
    if (runState.stopping) return;
    runState.stopping = true;
    for (const steerController of runState.steerControllers) {
      steerController.abort();
    }
    runState.steerControllers.clear();
    const queuedMessages = messagesRef.current.map((message) =>
      runtimeQueueStatus(message) === "sending"
        ? withRuntimeQueueStatus(message, "queued")
        : message,
    );
    if (
      queuedMessages.some(
        (message, index) => message !== messagesRef.current[index],
      )
    ) {
      messagesRef.current = queuedMessages;
      setMessages(queuedMessages);
      syncActiveSessionMessages(queuedMessages);
    }
    runState.controller?.abort();
    runState.controller = null;
    runState.awaitingVisibleAssistantOutput = false;
    runState.submitting = false;
    showTransientNotice("正在停止，已生成内容会保留…");

    void (async () => {
      let confirmed = true;
      let cancelResult: unknown;
      try {
        const result = await options.api?.cancel?.({ session_id: sessionId });
        cancelResult = result;
        if (result === false) confirmed = false;
        if (
          result &&
          typeof result === "object" &&
          "stopped" in result &&
          result.stopped === false
        ) {
          confirmed = false;
        }
      } catch {
        confirmed = false;
      }

      if (!confirmed) {
        runState.stopping = false;
        runState.loading = false;
        setPrimaryStreamActive(sessionId, false, true);
        setSessionLoading(sessionId, false);
        showTransientNotice("未能确认停止，已生成内容仍已保留");
        return;
      }

      const stoppedSession = findSessionByStableIdentity(
        sessionsRef.current,
        sessionId,
      );
      const confirmedStopTurnKeys = new Set<string>([
        ...runtimeStateTurnKeys(stoppedSession),
        ...runtimeTurnKeysFromMetadata(cancelResult),
      ]);
      if (confirmedStopTurnKeys.size === 0) {
        confirmedStopTurnKeys.add(`local-stop:${Date.now()}`);
      }
      runState.confirmedStopTurnKeys = confirmedStopTurnKeys;

      // The runtime can publish its durable terminal event before the cancel
      // request resolves. In that ordering the sessions bridge has already
      // made the composer usable again; blindly restoring loading below would
      // resurrect a finished run and leave Stop visible forever.
      const acknowledgedSession = findSessionByStableIdentity(
        sessionsRef.current,
        sessionId,
      );
      const stopWasStillPending = runState.stopping;
      if (
        !stopWasStillPending ||
        isSettledRuntimeStatus(sessionRuntimeStatus(acknowledgedSession))
      ) {
        runState.stopping = false;
        runState.loading = false;
        // A confirmed user stop is authoritative even when the runtime's
        // durable terminal snapshot won the race. Do not preserve a stale
        // `running` runtimeState here: that would make isSessionRunActive()
        // resurrect Stop after the composer had already settled.
        setPrimaryStreamActive(sessionId, false, false, "interrupted");
        setSessionLoading(sessionId, false);
        if (stopWasStillPending) {
          appendSyntheticPayload(
            {
              object: "response",
              status: "failed",
              type: "response.failed",
              error: {
                message: "已停止生成",
                code: "user_stopped",
                user_stopped: true,
              },
            },
            sessionId,
          );
          showTransientNotice("生成已停止，内容已保留");
        }
        return;
      }

      // The Runtime interrupt endpoint only returns `stopped: true` after the
      // backend has settled the producer and persisted the interrupted turn.
      // Clear the optimistic in-progress snapshot here as well so a delayed
      // event-stream reconciliation cannot leave Stop visible indefinitely.
      runState.stopping = false;
      runState.loading = false;
      setPrimaryStreamActive(sessionId, false, false, "interrupted");
      setSessionLoading(sessionId, false);
      appendSyntheticPayload(
        {
          object: "response",
          status: "failed",
          type: "response.failed",
          error: {
            message: "已停止生成",
            code: "user_stopped",
            user_stopped: true,
          },
        },
        sessionId,
      );
      showTransientNotice("生成已停止，内容已保留");
    })();
  }, [
    appendSyntheticPayload,
    getSessionRunState,
    options.api,
    setMessages,
    setPrimaryStreamActive,
    setSessionLoading,
    showTransientNotice,
    syncActiveSessionMessages,
  ]);

  useEffect(() => {
    for (const [sessionId, runState] of sessionRunStatesRef.current) {
      if (!runState.stopping) continue;
      const session = findSessionByStableIdentity(sessions, sessionId);
      const terminalStatus = sessionRuntimeStatus(session);
      if (!isSettledRuntimeStatus(terminalStatus)) continue;

      runState.stopping = false;
      runState.loading = false;
      const terminalTurnKeys = runtimeStateTurnKeys(session);
      for (const key of terminalTurnKeys) {
        runState.confirmedStopTurnKeys.add(key);
      }
      if (runState.confirmedStopTurnKeys.size === 0) {
        runState.confirmedStopTurnKeys.add(`local-stop:${Date.now()}`);
      }
      // If a terminal snapshot arrives while a user stop is pending, the
      // visible lifecycle belongs to that stop request. Clear every local
      // active marker instead of retaining the durable snapshot's stale
      // `running` fields.
      setPrimaryStreamActive(sessionId, false, false, "interrupted");
      setSessionLoading(sessionId, false);
      appendSyntheticPayload(
        {
          object: "response",
          status: "failed",
          type: "response.failed",
          error: {
            message: "已停止生成",
            code: "user_stopped",
            user_stopped: true,
          },
        },
        sessionId,
      );
      showTransientNotice("生成已停止，内容已保留");
    }
  }, [
    appendSyntheticPayload,
    sessions,
    setPrimaryStreamActive,
    setSessionLoading,
    showTransientNotice,
  ]);

  const requestSubmit = useCallback(
    (activeIntent: ControlInputIntent = "supplement") => {
      if (
        attachmentsRef.current.some(
          (attachment) => attachment.status !== "ready",
        )
      ) {
        return;
      }
      const sessionId = currentSessionIdRef.current || "";
      const runState = sessionId ? getSessionRunState(sessionId) : undefined;
      if (runState?.submitting) return;
      const draft = readCurrentDraft();
      if (!isSessionRunActive(sessionId)) {
        void submit(
          {
            ...draft,
            branchFromMessageId: editingMessageId || undefined,
          },
          sessionId || undefined,
        );
        return;
      }
      if (draft.text || draft.attachments.length > 0) {
        const queuedMessage = enqueueGuidanceDraft(sessionId, {
          ...draft,
          controlIntent: activeIntent,
        });
        void submitGuidanceIntent(queuedMessage, activeIntent);
      }
    },
    [
      editingMessageId,
      enqueueGuidanceDraft,
      getSessionRunState,
      isSessionRunActive,
      readCurrentDraft,
      submit,
      submitGuidanceIntent,
    ],
  );

  const uploadAttachment = useCallback(
    (sessionKey: string, attachmentId: string, file: File) => {
      const customRequest = options.sender?.attachments?.customRequest;
      const fail = (error: unknown) => {
        console.warn("[ChatAttachments] Upload failed", {
          name: file.name,
          size: file.size,
          type: file.type,
          error: error instanceof Error ? error.message : String(error || ""),
        });
        updateAttachmentsForSession(sessionKey, (previous) =>
          previous.map((attachment) =>
            attachment.id === attachmentId && attachment.status !== "ready"
              ? {
                  ...attachment,
                  status: "error",
                  error:
                    error instanceof Error
                      ? error.message
                      : String(error || "上传失败，请重试"),
                }
              : attachment,
          ),
        );
      };
      if (typeof customRequest !== "function") {
        fail(new Error("当前会话没有可用的附件上传通道"));
        return;
      }

      try {
        console.info("[ChatAttachments] Upload started", {
          name: file.name,
          size: file.size,
          type: file.type,
        });
        const result = customRequest({
          file,
          onProgress: (event: { percent?: number }) => {
            const progress = Math.max(
              0,
              Math.min(100, Math.round(Number(event?.percent || 0))),
            );
            updateAttachmentsForSession(sessionKey, (previous) =>
              previous.map((attachment) =>
                attachment.id === attachmentId
                  ? { ...attachment, progress }
                  : attachment,
              ),
            );
          },
          onError: fail,
          onSuccess: (body: {
            url?: string;
            thumbUrl?: string;
            canonicalPath?: string;
            storedName?: string;
            digest?: string;
            immutableVersion?: number;
            uploadReceipt?: string;
          }) => {
            const url = body?.url || body?.thumbUrl || "";
            if (!url) {
              fail(new Error("上传完成但没有返回可用的文件地址"));
              return;
            }
            console.info("[ChatAttachments] Upload completed", {
              name: file.name,
              size: file.size,
              type: file.type,
            });
            updateAttachmentsForSession(sessionKey, (previous) =>
              previous.map((attachment) =>
                attachment.id === attachmentId
                  ? {
                      ...attachment,
                      url,
                      canonicalPath: body.canonicalPath,
                      storedName: body.storedName,
                      digest: body.digest,
                      immutableVersion: body.immutableVersion,
                      uploadReceipt: body.uploadReceipt,
                      status: "ready",
                      progress: 100,
                      error: undefined,
                    }
                  : attachment,
              ),
            );
          },
        });
        if (result && typeof result.then === "function") {
          void Promise.resolve(result).catch(fail);
        }
      } catch (error) {
        fail(error);
      }
    },
    [options.sender, updateAttachmentsForSession],
  );

  const handleFiles = useCallback(
    (files: ArrayLike<File> | null) => {
      if (!files?.length) return;
      const sessionKey = activeAttachmentDraftKey();
      const existing = new Set(
        attachmentsRef.current
          .map((attachment) => attachment.fingerprint)
          .filter((fingerprint): fingerprint is string => Boolean(fingerprint)),
      );
      const additions: AttachmentItem[] = [];

      Array.from(files).forEach((originalFile, index) => {
        const file = prepareAttachmentFile(originalFile, index);
        const type = inferAttachmentMimeType(file);
        const fingerprint = attachmentFingerprint(file);
        if (existing.has(fingerprint)) return;
        existing.add(fingerprint);

        let previewUrl: string | undefined;
        if (
          type.startsWith("image/") &&
          typeof URL.createObjectURL === "function"
        ) {
          previewUrl = URL.createObjectURL(file);
          attachmentPreviewUrlsRef.current.add(previewUrl);
        }
        additions.push({
          id: generateId("attachment"),
          name: file.name,
          type,
          size: file.size,
          url: "",
          status: "uploading",
          progress: 0,
          fingerprint,
          role: DEFAULT_ATTACHMENT_ROLE,
          roleConfirmed: false,
          previewUrl,
          sourceFile: file,
        });
      });
      if (additions.length === 0) return;

      updateAttachmentsForSession(sessionKey, (previous) => [
        ...previous,
        ...additions,
      ]);
      // Start transport after the attachment draft is synchronously committed.
      // This also gives Electron's native paste callback a clean microtask
      // boundary before FormData reads the reconstructed File.
      void Promise.resolve().then(() => {
        additions.forEach((attachment) => {
          if (attachment.sourceFile) {
            uploadAttachment(sessionKey, attachment.id, attachment.sourceFile);
          }
        });
      });
    },
    [activeAttachmentDraftKey, updateAttachmentsForSession, uploadAttachment],
  );

  const removeAttachment = useCallback(
    (attachmentId: string) => {
      updateAttachments((previous) => {
        const removed = previous.find(
          (attachment) => attachment.id === attachmentId,
        );
        revokeAttachmentPreview(removed?.previewUrl);
        return previous.filter((attachment) => attachment.id !== attachmentId);
      });
    },
    [revokeAttachmentPreview, updateAttachments],
  );

  const retryAttachment = useCallback(
    (attachmentId: string) => {
      const sessionKey = activeAttachmentDraftKey();
      const attachment = attachmentsRef.current.find(
        (item) => item.id === attachmentId,
      );
      if (!attachment?.sourceFile) return;
      updateAttachments((previous) =>
        previous.map((item) =>
          item.id === attachmentId
            ? {
                ...item,
                status: "uploading",
                progress: 0,
                error: undefined,
              }
            : item,
        ),
      );
      void Promise.resolve().then(() => {
        uploadAttachment(
          sessionKey,
          attachmentId,
          attachment.sourceFile as File,
        );
      });
    },
    [activeAttachmentDraftKey, updateAttachments, uploadAttachment],
  );

  const setAttachmentRole = useCallback(
    (attachmentId: string, role: LantaiAttachmentRole) => {
      updateAttachments((previous) =>
        previous.map((attachment) =>
          attachment.id === attachmentId
            ? {
                ...attachment,
                role,
                roleConfirmed: true,
              }
            : attachment,
        ),
      );
    },
    [updateAttachments],
  );

  // The action button and `requestSubmit` must share the same ownership
  // predicate.  A durable interrupt can reach `sessions` one render before
  // the per-session stop state is reconciled; using only the rendered session
  // here briefly exposed a Send button while `requestSubmit` still considered
  // the run active and queued the fresh turn as guidance.
  const activeSessionRun = Boolean(
    currentSessionId && isSessionRunActive(currentSessionId),
  );
  const optionsContext = useMemo<ChatOptions>(
    () => ({
      ...options,
      recovery: {
        ...options.recovery,
        busy: activeSessionRun,
        continueGeneration: continueLatestGeneration,
        retryGeneration: retryLatestGeneration,
      },
    }),
    [
      activeSessionRun,
      continueLatestGeneration,
      options,
      retryLatestGeneration,
    ],
  );
  loadingRef.current = activeSessionRun;
  const sessionsContext = useMemo(
    () => ({
      sessions,
      currentSessionId,
      setCurrentSessionId,
      setSessions,
      createSession,
    }),
    [
      createSession,
      currentSessionId,
      sessions,
      setCurrentSessionId,
      setSessions,
    ],
  );
  const inputContext = useMemo(
    () => ({
      loading: activeSessionRun,
      setLoading,
      getLoading: () => loadingRef.current,
      appendDraft: (text: string, ownerSessionId: string) => {
        if (
          !text ||
          !ownerSessionId ||
          currentSessionIdRef.current !== ownerSessionId
        )
          return false;
        // createSession updates its owner ref before React loads that owner's draft.
        const current =
          inputOwnerSessionIdRef.current === ownerSessionId
            ? inputValueRef.current
            : sessionApi?.loadDraftText?.(ownerSessionId) || "";
        inputOwnerSessionIdRef.current = ownerSessionId;
        updateInputValue(current.trim() ? `${current}\n\n${text}` : text);
        return true;
      },
    }),
    [activeSessionRun, sessionApi, setLoading, updateInputValue],
  );
  const AttachTrigger = options.sender?.attachments?.trigger;
  const attachmentUploadError = attachments.some(
    (attachment) => attachment.status === "error",
  );
  const attachmentUploadPending = attachments.some(
    (attachment) => attachment.status === "uploading",
  );
  const attachmentUploadBlocked =
    attachmentUploadError || attachmentUploadPending;
  const hasComposerDraft =
    Boolean(normalizeText(inputValue)) || attachments.length > 0;
  const submitLabel = attachmentUploadError
    ? "请重试或删除上传失败的附件"
    : attachmentUploadPending
      ? "附件上传中"
      : "发送";
  const queuedGuidanceMessages = useMemo(
    () => messages.filter(isGuidanceStripMessage),
    [messages],
  );
  const displayedMessages = useMemo(
    () => messages.filter((message) => !isGuidanceStripMessage(message)),
    [messages],
  );
  const commandSuggestions = useMemo(() => {
    if (
      selectedCommand ||
      dismissedSuggestionInput === inputValue ||
      senderCommandSuggestions.length === 0
    ) {
      return [];
    }
    const match = inputValue.match(/^\/([^\s/]*)$/);
    if (!match) return [];
    const query = match[1].toLocaleLowerCase();
    return senderCommandSuggestions.filter(
      (suggestion: { value?: unknown }) => {
        const value =
          typeof suggestion?.value === "string"
            ? suggestion.value.trim().toLocaleLowerCase()
            : "";
        return value.length > 0 && value.startsWith(query);
      },
    );
  }, [
    dismissedSuggestionInput,
    inputValue,
    selectedCommand,
    senderCommandSuggestions,
  ]);

  useEffect(() => {
    setActiveSuggestionIndex((current) =>
      commandSuggestions.length === 0
        ? 0
        : Math.min(current, commandSuggestions.length - 1),
    );
  }, [commandSuggestions.length]);

  const activeSession = findSessionByStableIdentity(sessions, currentSessionId);
  const activeSessionMeta =
    activeSession?.meta && typeof activeSession.meta === "object"
      ? (activeSession.meta as Record<string, unknown>)
      : undefined;
  const legacyRecoveryNotice =
    activeSessionMeta?.state_migration_state === "needs_manual_recovery"
      ? String(
          activeSessionMeta.legacy_history_notice ||
            "检测到旧版混合历史，已隔离，原始内容仍可恢复",
        )
      : "";
  const legacySource =
    activeSessionMeta?.legacy_source_reference &&
    typeof activeSessionMeta.legacy_source_reference === "object"
      ? (activeSessionMeta.legacy_source_reference as Record<string, unknown>)
      : undefined;
  const legacySourceSessionId =
    typeof legacySource?.session_id === "string"
      ? legacySource.session_id.trim()
      : "";

  const recoverLegacyHistory = useCallback(async () => {
    if (!currentSessionId || !sessionApi?.recoverLegacyHistory) return;
    setLegacyRecoveryBusy(true);
    setLegacyRecoveryError(null);
    try {
      const recovered = normalizeSessionsMessages(
        await sessionApi.recoverLegacyHistory(currentSessionId),
      );
      const restoredSession = recovered[0];
      if (!restoredSession?.id) {
        throw new Error("后端没有返回恢复后的独立会话");
      }
      sessionsRef.current = recovered;
      setSessionsState(recovered);
      setCurrentSessionId(restoredSession.id);
    } catch (error) {
      setLegacyRecoveryError(
        error instanceof Error ? error.message : String(error || "恢复失败"),
      );
    } finally {
      setLegacyRecoveryBusy(false);
    }
  }, [currentSessionId, sessionApi, setCurrentSessionId]);

  const selectCommandSuggestion = useCallback(
    (suggestion: CommandSuggestion) => {
      const selected = normalizeCommandSuggestion(suggestion);
      if (!selected) return;
      updateInputValue(selected.command);
      setSelectedCommand(selected);
      setDismissedSuggestionInput(selected.command);
      window.requestAnimationFrame(() => {
        const textarea = document.querySelector<HTMLTextAreaElement>(
          '[class*="sender"] textarea',
        );
        textarea?.focus();
        const end = textarea?.value.length ?? 0;
        textarea?.setSelectionRange(end, end);
      });
    },
    [updateInputValue],
  );

  const composerInputValue = selectedCommand
    ? commandDraftBody(inputValue, selectedCommand)
    : inputValue;
  const updateComposerInputValue = useCallback(
    (next: string | ((current: string) => string)) => {
      const current = selectedCommand
        ? commandDraftBody(inputValueRef.current, selectedCommand)
        : inputValueRef.current;
      const nextValue = typeof next === "function" ? next(current) : next;
      updateInputValue(
        selectedCommand
          ? composeCommandDraft(selectedCommand, nextValue)
          : nextValue,
      );
    },
    [selectedCommand, updateInputValue],
  );
  const removeSelectedCommand = useCallback(() => {
    if (!selectedCommand) return;
    const body = commandDraftBody(inputValueRef.current, selectedCommand);
    setSelectedCommand(null);
    setDismissedSuggestionInput(null);
    updateInputValue(body);
    window.requestAnimationFrame(() => {
      document
        .querySelector<HTMLTextAreaElement>('[class*="sender"] textarea')
        ?.focus();
    });
  }, [selectedCommand, updateInputValue]);

  return React.createElement(
    ChatAnywhereOptionsContext.Provider,
    { value: optionsContext },
    React.createElement(
      ChatAnywhereSessionsContext.Provider,
      { value: sessionsContext },
      React.createElement(
        ChatAnywhereInputContext.Provider,
        { value: inputContext },
        React.createElement(
          "div",
          { className: "lantai-chat-anywhere" },
          React.createElement(Modal, {
            open: Boolean(pendingBranchMessage),
            title: React.createElement(
              "span",
              { className: "lantai-branch-confirm-title" },
              React.createElement(GitBranch, {
                size: 18,
                "aria-hidden": true,
              }),
              React.createElement("span", null, "从此处创建分支？"),
            ),
            rootClassName: "lantai-branch-confirm-modal",
            width: 420,
            centered: true,
            okText: "创建分支",
            cancelText: "取消",
            maskClosable: true,
            keyboard: true,
            destroyOnHidden: true,
            onOk: confirmBranchFromUserMessage,
            onCancel: cancelBranchFromUserMessage,
            children: React.createElement(
              "p",
              { className: "lantai-branch-confirm-copy" },
              "将保留这条消息之前的内容，并在新会话中重新生成后续回复。原会话不会被修改。",
            ),
          }),
          React.createElement(
            "div",
            { className: "lantai-chat-anywhere-layout" },
            React.createElement(
              "div",
              { className: "lantai-chat-anywhere-layout-main" },
              React.createElement(
                "div",
                { className: "lantai-chat-anywhere-default-header" },
                React.createElement(
                  "div",
                  { className: "lantai-chat-anywhere-default-header-left" },
                  options.theme?.leftHeader?.title || "",
                ),
                React.createElement(
                  "div",
                  { className: "lantai-chat-anywhere-default-header-right" },
                  options.theme?.rightHeader,
                ),
              ),
              React.createElement(
                "div",
                { className: "lantai-chat-anywhere-chat" },
                legacyRecoveryNotice
                  ? React.createElement(
                      "div",
                      {
                        className: "lantai-legacy-history-recovery-notice",
                        role: "status",
                        "aria-live": "polite",
                      },
                      React.createElement("strong", null, legacyRecoveryNotice),
                      legacySourceSessionId
                        ? React.createElement(
                            "span",
                            null,
                            `恢复来源：${legacySourceSessionId}`,
                          )
                        : null,
                      sessionApi?.recoverLegacyHistory
                        ? React.createElement(
                            "button",
                            {
                              type: "button",
                              disabled: legacyRecoveryBusy,
                              onClick: recoverLegacyHistory,
                            },
                            legacyRecoveryBusy ? "正在恢复…" : "恢复为独立副本",
                          )
                        : null,
                      React.createElement(
                        "span",
                        { className: "lantai-legacy-history-recovery-help" },
                        "不会覆盖当前会话或删除原始文件。",
                      ),
                      legacyRecoveryError
                        ? React.createElement(
                            "span",
                            {
                              className: "lantai-legacy-history-recovery-error",
                              role: "alert",
                            },
                            legacyRecoveryError,
                          )
                        : null,
                    )
                  : null,
                React.createElement(
                  "div",
                  {
                    className: "lantai-chat-anywhere-message-list",
                    ref: messageListRef,
                  },
                  displayedMessages.length === 0 || welcomeExitActive
                    ? React.createElement(
                        "div",
                        {
                          key: "welcome",
                          className:
                            "lantai-chat-anywhere-message-list-welcome",
                          "data-lantai-welcome-exiting":
                            welcomeExitActive && displayedMessages.length > 0
                              ? "1"
                              : undefined,
                          "aria-hidden":
                            welcomeExitActive && displayedMessages.length > 0
                              ? "true"
                              : undefined,
                        },
                        React.createElement(
                          "div",
                          {
                            className: "lantai-chat-anywhere-welcome-default",
                          },
                          options.welcome?.avatar
                            ? React.createElement(
                                "span",
                                {
                                  className:
                                    "lantai-chat-anywhere-welcome-avatar-frame",
                                  "aria-hidden": "true",
                                },
                                React.createElement("img", {
                                  className:
                                    "lantai-chat-anywhere-welcome-avatar",
                                  src: options.welcome.avatar,
                                  alt: "",
                                }),
                              )
                            : null,
                          options.welcome?.greeting
                            ? React.createElement(
                                "div",
                                {
                                  className:
                                    "lantai-chat-anywhere-welcome-title",
                                },
                                options.welcome.greeting,
                              )
                            : null,
                          options.welcome?.description
                            ? React.createElement(
                                "p",
                                {
                                  className:
                                    "lantai-chat-anywhere-welcome-description",
                                },
                                options.welcome.description,
                              )
                            : null,
                        ),
                      )
                    : null,
                  displayedMessages.length > 0
                    ? React.createElement(
                        "div",
                        { key: "transcript", className: "lantai-bubble-list" },
                        displayedMessages.map((message, index) =>
                          React.createElement(
                            React.Fragment,
                            { key: message.id || index },
                            // The timestamp opens a conversation turn.  It
                            // belongs above the user's request, not above the
                            // following assistant card; placing it on the
                            // assistant made a later user bubble look visually
                            // attached to the previous response while queued
                            // turns were advancing.
                            message.role === "user"
                              ? React.createElement(ConversationTimeAnchor, {
                                  value: messageTimestamp(message),
                                })
                              : null,
                            React.createElement(
                              "div",
                              {
                                className: `lantai-bubble-list-item lantai-bubble-${message.role || "assistant"}`,
                                "data-lantai-message-id": message.id,
                              },
                              renderCard(
                                message,
                                optionsContext,
                                index === displayedMessages.length - 1,
                                {
                                  disabled: activeSessionRun,
                                  onEdit: editUserMessage,
                                  onBranch: requestBranchFromUserMessage,
                                  onReplaceControlInput:
                                    editPendingControlInput,
                                  onCancelControlInput: (controlMessage) =>
                                    void cancelPendingControlInput(
                                      controlMessage,
                                    ),
                                },
                                currentSessionId,
                              ),
                              message.role === "user"
                                ? optionsContext.renderMessageReferences?.(
                                    message,
                                  )
                                : null,
                            ),
                          ),
                        ),
                        branchingMessageId
                          ? React.createElement(
                              "div",
                              {
                                className: "lantai-branch-creation-status",
                                role: "status",
                                "aria-live": "polite",
                                "aria-label": "正在创建分支并重新生成",
                                "data-lantai-branch-creation-pending": "1",
                              },
                              React.createElement(Spin, { size: "small" }),
                              React.createElement(
                                "span",
                                null,
                                "正在创建分支并重新生成…",
                              ),
                            )
                          : null,
                      )
                    : null,
                ),
              ),
              React.createElement(ConversationNavigator, {
                messages: displayedMessages,
                scrollContainerRef: messageListRef,
                onNavigate: navigateToMessage,
              }),
              React.createElement(
                "div",
                {
                  className: "lantai-chat-anywhere-input",
                  "data-has-draft": normalizeText(inputValue)
                    ? "true"
                    : "false",
                  "data-command-menu-open":
                    commandSuggestions.length > 0 ? "true" : "false",
                  "data-selected-command": selectedCommand?.command,
                  ref: composerContainerRef,
                },
                transientNotice
                  ? React.createElement(
                      "div",
                      {
                        className: "lantai-response-toast",
                        role: "status",
                        "aria-live": "polite",
                      },
                      React.createElement(
                        "span",
                        { className: "lantai-response-toast-indicator" },
                        React.createElement(CircleStop, {
                          size: 13,
                          strokeWidth: 1.9,
                          "aria-hidden": true,
                        }),
                      ),
                      React.createElement("span", null, transientNotice),
                      React.createElement(
                        "button",
                        {
                          type: "button",
                          disabled: activeSessionRun,
                          onClick: () => {
                            setTransientNotice(null);
                            continueLatestGeneration();
                          },
                        },
                        "继续",
                      ),
                    )
                  : null,
                commandSuggestions.length > 0
                  ? React.createElement(
                      "div",
                      {
                        id: "lantai-command-suggestions",
                        className: "lantai-command-suggestions",
                        "data-chat-scroll-chain": "1",
                        role: "listbox",
                        "aria-label": "快捷指令",
                      },
                      React.createElement(
                        "div",
                        { className: "lantai-command-suggestions-heading" },
                        React.createElement("span", null, "快捷指令"),
                        React.createElement(
                          "kbd",
                          null,
                          "↑↓ 选择 · Enter 确认 · Esc 关闭",
                        ),
                      ),
                      commandSuggestions.map((suggestion, index) =>
                        React.createElement(
                          "button",
                          {
                            key: `${suggestion.value}-${index}`,
                            id: `lantai-command-suggestion-${index}`,
                            type: "button",
                            role: "option",
                            className: `lantai-command-suggestion${
                              index === activeSuggestionIndex
                                ? " lantai-command-suggestion-active"
                                : ""
                            }`,
                            "aria-selected": index === activeSuggestionIndex,
                            onMouseDown: (
                              event: React.MouseEvent<HTMLButtonElement>,
                            ) => event.preventDefault(),
                            onMouseEnter: () => setActiveSuggestionIndex(index),
                            onClick: () => selectCommandSuggestion(suggestion),
                          },
                          suggestion.label || `/${suggestion.value}`,
                        ),
                      ),
                    )
                  : null,
                options.sender?.outsideContext
                  ? React.createElement(
                      "div",
                      { className: "lantai-sender-outside-context" },
                      options.sender.outsideContext,
                    )
                  : null,
                queuedGuidanceMessages.length > 0
                  ? React.createElement(
                      "section",
                      {
                        className: "lantai-guidance-queue",
                        "aria-label": "待发送引导",
                        "aria-live": "polite",
                      },
                      queuedGuidanceMessages.map((message, index) =>
                        React.createElement(QueuedGuidanceCard, {
                          key: message.id,
                          message,
                          position: index,
                          total: queuedGuidanceMessages.length,
                          onSubmitIntent: submitGuidanceCardIntent,
                          onEdit: editGuidanceMessage,
                          onDelete: deleteGuidanceMessage,
                        }),
                      ),
                    )
                  : null,
                React.createElement(
                  "div",
                  {
                    className: "lantai-sender",
                    onDragOver: (event: React.DragEvent<HTMLDivElement>) => {
                      const carriesFiles =
                        event.dataTransfer.files.length > 0 ||
                        Array.from(event.dataTransfer.types || []).includes(
                          "Files",
                        );
                      if (!carriesFiles) return;
                      event.preventDefault();
                      event.dataTransfer.dropEffect = "copy";
                    },
                    onDrop: (event: React.DragEvent<HTMLDivElement>) => {
                      if (event.dataTransfer.files.length === 0) return;
                      event.preventDefault();
                      handleFiles(event.dataTransfer.files);
                    },
                  },
                  React.createElement(
                    "div",
                    { className: "lantai-sender-content" },
                    options.sender?.context
                      ? React.createElement(
                          "div",
                          { className: "lantai-sender-context" },
                          options.sender.context,
                        )
                      : null,
                    editingMessageId
                      ? React.createElement(
                          "div",
                          {
                            className: "lantai-sender-editing-notice",
                            role: "status",
                          },
                          React.createElement(Pencil, {
                            size: 14,
                            "aria-hidden": true,
                          }),
                          React.createElement(
                            "span",
                            null,
                            "正在编辑历史消息，发送后将创建分支",
                          ),
                          React.createElement(
                            "button",
                            {
                              type: "button",
                              "aria-label": "取消编辑用户消息",
                              onClick: cancelEditing,
                            },
                            React.createElement(X, {
                              size: 14,
                              "aria-hidden": true,
                            }),
                          ),
                        )
                      : null,
                    replacingControlInput?.sessionId === currentSessionId
                      ? React.createElement(
                          "div",
                          {
                            className: "lantai-sender-editing-notice",
                            role: "status",
                          },
                          React.createElement(Pencil, {
                            size: 14,
                            "aria-hidden": true,
                          }),
                          React.createElement(
                            "span",
                            null,
                            "正在修改未应用引导，发送后将原子替换",
                          ),
                          React.createElement(
                            "button",
                            {
                              type: "button",
                              "aria-label": "取消修改未应用引导",
                              onClick: cancelReplacingControlInput,
                            },
                            React.createElement(X, {
                              size: 14,
                              "aria-hidden": true,
                            }),
                          ),
                        )
                      : null,
                    React.createElement(
                      "div",
                      { className: "lantai-sender-content-bottom" },
                      React.createElement(
                        "div",
                        { className: "lantai-sender-prefix" },
                        options.sender?.prefix,
                        React.createElement(
                          "span",
                          { className: "lantai-sender-input-tools" },
                          options.sender?.beforeAttachments,
                          AttachTrigger
                            ? React.createElement(
                                "span",
                                {
                                  className: "lantai-sender-attachment-trigger",
                                  onClick: () => fileInputRef.current?.click(),
                                },
                                React.createElement(AttachTrigger, {
                                  disabled: false,
                                }),
                              )
                            : null,
                          options.sender?.afterAttachments,
                        ),
                        React.createElement("input", {
                          ref: fileInputRef,
                          type: "file",
                          multiple: true,
                          hidden: true,
                          onChange: (event) => {
                            handleFiles(event.currentTarget.files);
                            event.currentTarget.value = "";
                          },
                        }),
                      ),
                      React.createElement(
                        "div",
                        { className: "lantai-sender-actions" },
                        activeSessionRun
                          ? React.createElement(
                              React.Fragment,
                              null,
                              hasComposerDraft
                                ? React.createElement(
                                    React.Fragment,
                                    null,
                                    React.createElement(
                                      Tooltip,
                                      {
                                        title: attachmentUploadBlocked
                                          ? submitLabel
                                          : "补充当前任务：发送新要求，继续当前任务（Enter）",
                                        trigger: ["hover", "focus"],
                                        destroyOnHidden: true,
                                        placement: "topRight",
                                      },
                                      React.createElement(
                                        Button,
                                        {
                                          type: "primary",
                                          className:
                                            "lantai-sender-actions-btn lantai-sender-action-labelled",
                                          "aria-label": "补充当前任务",
                                          disabled: attachmentUploadBlocked,
                                          icon: React.createElement(ArrowUp, {
                                            size: 15,
                                            strokeWidth: 2.2,
                                            "aria-hidden": true,
                                          }),
                                          onClick: () =>
                                            requestSubmit("supplement"),
                                        },
                                        "补充要求",
                                      ),
                                    ),
                                    React.createElement(
                                      Dropdown,
                                      {
                                        trigger: ["click"],
                                        placement: "topRight",
                                        menu: {
                                          items: [
                                            {
                                              key: "redirect",
                                              label: "立即调整方向",
                                            },
                                            {
                                              key: "queue_next",
                                              label: "排队下一任务",
                                            },
                                          ],
                                          onClick: ({ key }) =>
                                            requestSubmit(
                                              key as ControlInputIntent,
                                            ),
                                        },
                                      },
                                      React.createElement(
                                        Tooltip,
                                        {
                                          title: attachmentUploadBlocked
                                            ? submitLabel
                                            : "更多发送方式：立即调整方向或排队下一任务",
                                          trigger: ["hover", "focus"],
                                          destroyOnHidden: true,
                                          placement: "topRight",
                                        },
                                        React.createElement(
                                          "button",
                                          {
                                            type: "button",
                                            className:
                                              "lantai-sender-control-intent-btn lantai-sender-action-labelled",
                                            "aria-label": "选择运行中输入方式",
                                            disabled: attachmentUploadBlocked,
                                          },
                                          React.createElement(
                                            "span",
                                            null,
                                            "其他方式",
                                          ),
                                          React.createElement(ChevronDown, {
                                            size: 14,
                                            "aria-hidden": true,
                                          }),
                                        ),
                                      ),
                                    ),
                                  )
                                : null,
                              React.createElement(
                                Tooltip,
                                {
                                  title: "停止当前任务：结束正在进行的执行",
                                  trigger: ["hover", "focus"],
                                  destroyOnHidden: true,
                                  placement: "topRight",
                                },
                                React.createElement(
                                  "button",
                                  {
                                    type: "button",
                                    className:
                                      "lantai-sender-stop-btn lantai-sender-action-labelled",
                                    "aria-label": "停止当前任务",
                                    onClick: stop,
                                  },
                                  React.createElement(Square, {
                                    size: 12,
                                    strokeWidth: 0,
                                    fill: "currentColor",
                                    "aria-hidden": true,
                                  }),
                                  React.createElement("span", null, "停止"),
                                ),
                              ),
                            )
                          : React.createElement(
                              Tooltip,
                              {
                                title: attachmentUploadBlocked
                                  ? submitLabel
                                  : "发送消息（Enter）",
                                trigger: ["hover", "focus"],
                                destroyOnHidden: true,
                                placement: "topRight",
                              },
                              React.createElement(Button, {
                                type: "primary",
                                className: "lantai-sender-actions-btn",
                                "aria-label": submitLabel,
                                disabled: attachmentUploadBlocked,
                                icon: React.createElement(ArrowUp, {
                                  size: 15,
                                  strokeWidth: 2.2,
                                  "aria-hidden": true,
                                }),
                                onClick: () => requestSubmit(),
                              }),
                            ),
                      ),
                    ),
                    attachments.length
                      ? React.createElement(
                          React.Fragment,
                          null,
                          attachments.length >= 2
                            ? React.createElement(
                                "div",
                                {
                                  className: "lantai-attachment-role-preflight",
                                  role: "status",
                                },
                                React.createElement(
                                  "span",
                                  null,
                                  "请确认附件用途，澜台会据此区分原稿、参考和模板。",
                                ),
                                React.createElement(
                                  "strong",
                                  null,
                                  attachments.some(
                                    (attachment) => !attachment.roleConfirmed,
                                  )
                                    ? `${attachments.length} 个附件 · 有待确认`
                                    : `${attachments.length} 个附件 · 已确认`,
                                ),
                              )
                            : null,
                          React.createElement(
                            "div",
                            {
                              className:
                                "lantai-attachment-list-card lantai-composer-attachment-list",
                              role: "list",
                              "aria-label": "待发送附件",
                            },
                            attachments.map((item) => {
                              const imageUrl = item.type.startsWith("image/")
                                ? item.previewUrl || item.url
                                : "";
                              const statusText =
                                item.status === "uploading"
                                  ? item.progress
                                    ? `上传中 ${item.progress}%`
                                    : "上传中"
                                  : item.status === "error"
                                    ? item.error
                                      ? `上传失败：${item.error}`
                                      : "上传失败"
                                    : formatAttachmentSize(item.size) ||
                                      "已就绪";
                              return React.createElement(
                                "div",
                                {
                                  key: item.id,
                                  className: [
                                    "lantai-composer-attachment",
                                    `is-${item.status}`,
                                  ].join(" "),
                                  role: "listitem",
                                  "aria-busy":
                                    item.status === "uploading"
                                      ? "true"
                                      : undefined,
                                  title: item.error || item.name,
                                },
                                imageUrl
                                  ? React.createElement("img", {
                                      src: imageUrl,
                                      alt: "",
                                      className:
                                        "lantai-composer-attachment-preview",
                                    })
                                  : React.createElement(
                                      "span",
                                      {
                                        className:
                                          "lantai-composer-attachment-file-icon",
                                        "aria-hidden": true,
                                      },
                                      React.createElement(FileIcon, {
                                        size: 18,
                                        strokeWidth: 1.8,
                                      }),
                                    ),
                                React.createElement(
                                  "span",
                                  {
                                    className:
                                      "lantai-composer-attachment-details",
                                  },
                                  React.createElement(
                                    "strong",
                                    {
                                      className:
                                        "lantai-composer-attachment-name",
                                    },
                                    item.name,
                                  ),
                                  React.createElement(
                                    "span",
                                    {
                                      className:
                                        "lantai-composer-attachment-status",
                                      "aria-live": "polite",
                                      "aria-atomic": "true",
                                    },
                                    item.status === "uploading"
                                      ? React.createElement(LoaderCircle, {
                                          size: 12,
                                          className:
                                            "lantai-composer-attachment-spinner",
                                          "aria-hidden": true,
                                        })
                                      : null,
                                    statusText,
                                  ),
                                  React.createElement(
                                    "label",
                                    {
                                      className:
                                        "lantai-composer-attachment-role",
                                    },
                                    React.createElement(
                                      "span",
                                      { className: "sr-only" },
                                      `${item.name} 的用途`,
                                    ),
                                    React.createElement(
                                      "select",
                                      {
                                        value:
                                          item.role || DEFAULT_ATTACHMENT_ROLE,
                                        "aria-label": `${item.name} 的用途`,
                                        onChange: (
                                          event: React.ChangeEvent<HTMLSelectElement>,
                                        ) =>
                                          setAttachmentRole(
                                            item.id,
                                            event.target
                                              .value as LantaiAttachmentRole,
                                          ),
                                      },
                                      ATTACHMENT_ROLE_OPTIONS.map((option) =>
                                        React.createElement(
                                          "option",
                                          {
                                            key: option.value,
                                            value: option.value,
                                            title: option.description,
                                          },
                                          option.label,
                                        ),
                                      ),
                                    ),
                                  ),
                                ),
                                React.createElement(
                                  "span",
                                  {
                                    className:
                                      "lantai-composer-attachment-actions",
                                  },
                                  item.status === "error"
                                    ? React.createElement(
                                        "button",
                                        {
                                          type: "button",
                                          className:
                                            "lantai-composer-attachment-action",
                                          "aria-label": `重试上传 ${item.name}`,
                                          onClick: () =>
                                            retryAttachment(item.id),
                                        },
                                        React.createElement(RotateCcw, {
                                          size: 14,
                                          "aria-hidden": true,
                                        }),
                                      )
                                    : null,
                                  React.createElement(
                                    "button",
                                    {
                                      type: "button",
                                      className:
                                        "lantai-composer-attachment-action",
                                      "aria-label": `移除附件 ${item.name}`,
                                      onClick: () => removeAttachment(item.id),
                                    },
                                    React.createElement(X, {
                                      size: 14,
                                      "aria-hidden": true,
                                    }),
                                  ),
                                ),
                              );
                            }),
                          ),
                        )
                      : null,
                    selectedCommand
                      ? React.createElement(
                          "div",
                          {
                            className: "lantai-composer-command-chip",
                            role: "group",
                            "aria-label": `已选择快捷指令 ${selectedCommand.command}`,
                          },
                          React.createElement(
                            "span",
                            {
                              className: "lantai-composer-command-mark",
                              "aria-hidden": true,
                            },
                            React.createElement(Check, {
                              size: 12,
                              strokeWidth: 2.2,
                            }),
                          ),
                          React.createElement(
                            "span",
                            { className: "lantai-composer-command-name" },
                            selectedCommand.command,
                          ),
                          selectedCommand.description
                            ? React.createElement(
                                "span",
                                {
                                  className:
                                    "lantai-composer-command-description",
                                },
                                selectedCommand.description,
                              )
                            : null,
                          React.createElement(
                            "button",
                            {
                              type: "button",
                              className: "lantai-composer-command-remove",
                              "aria-label": `移除快捷指令 ${selectedCommand.command}`,
                              onClick: removeSelectedCommand,
                            },
                            React.createElement(X, {
                              size: 13,
                              strokeWidth: 1.9,
                              "aria-hidden": true,
                            }),
                          ),
                        )
                      : null,
                    React.createElement(Input.TextArea, {
                      value: composerInputValue,
                      maxLength:
                        typeof options.sender?.maxLength === "number" &&
                        selectedCommand
                          ? Math.max(
                              0,
                              options.sender.maxLength -
                                selectedCommand.command.length -
                                1,
                            )
                          : options.sender?.maxLength,
                      placeholder: options.sender?.placeholder,
                      "aria-label":
                        options.sender?.ariaLabel ||
                        options.sender?.placeholder ||
                        "输入消息",
                      "aria-keyshortcuts": activeSessionRun
                        ? "Enter Meta+Enter Control+Enter Alt+Enter"
                        : "Enter",
                      title: activeSessionRun
                        ? "Enter 补充当前任务；⌘/Ctrl + Enter 调整方向；Alt + Enter 排队下一任务"
                        : undefined,
                      autoSize: { minRows: 1, maxRows: 8 },
                      "aria-autocomplete": "list",
                      "aria-controls":
                        commandSuggestions.length > 0
                          ? "lantai-command-suggestions"
                          : undefined,
                      "aria-expanded": commandSuggestions.length > 0,
                      "aria-activedescendant":
                        commandSuggestions.length > 0
                          ? `lantai-command-suggestion-${activeSuggestionIndex}`
                          : undefined,
                      onChange: (event) => {
                        updateComposerInputValue(event.target.value);
                        setDismissedSuggestionInput(null);
                      },
                      onPaste: (event) => {
                        const files = filesFromClipboardData(
                          event.clipboardData,
                        );
                        if (files.length > 0) {
                          event.preventDefault();
                          handleFiles(files);
                          return;
                        }
                        if (
                          event.defaultPrevented ||
                          !clipboardDataHasAttachmentHint(
                            event.clipboardData,
                          ) ||
                          !window.electronAPI?.readClipboardAttachments
                        ) {
                          return;
                        }

                        event.preventDefault();
                        const plainText =
                          event.clipboardData.getData("text/plain") || "";
                        const valueAtPaste = event.currentTarget.value;
                        const selectionStart =
                          event.currentTarget.selectionStart ??
                          valueAtPaste.length;
                        const selectionEnd =
                          event.currentTarget.selectionEnd ?? selectionStart;
                        const clipboardTypes = Array.from(
                          event.clipboardData.types || [],
                        );

                        void window.electronAPI
                          .readClipboardAttachments({
                            preferredName: plainText.trim(),
                            mimeTypes: clipboardTypes,
                          })
                          .then((result) => {
                            const nativeFiles =
                              filesFromDesktopClipboardAttachments(
                                result?.attachments,
                              );
                            if (nativeFiles.length > 0) {
                              handleFiles(nativeFiles);
                              return;
                            }

                            if (!plainText) return;
                            updateComposerInputValue((current) => {
                              if (current !== valueAtPaste) return current;
                              return (
                                current.slice(0, selectionStart) +
                                plainText +
                                current.slice(selectionEnd)
                              );
                            });
                          })
                          .catch(() => {
                            if (!plainText) return;
                            updateComposerInputValue((current) => {
                              if (current !== valueAtPaste) return current;
                              return (
                                current.slice(0, selectionStart) +
                                plainText +
                                current.slice(selectionEnd)
                              );
                            });
                          });
                      },
                      onKeyDown: (event) => {
                        if (commandSuggestions.length > 0) {
                          if (event.key === "ArrowDown") {
                            event.preventDefault();
                            setActiveSuggestionIndex(
                              (current) =>
                                (current + 1) % commandSuggestions.length,
                            );
                            return;
                          }
                          if (event.key === "ArrowUp") {
                            event.preventDefault();
                            setActiveSuggestionIndex(
                              (current) =>
                                (current - 1 + commandSuggestions.length) %
                                commandSuggestions.length,
                            );
                            return;
                          }
                          if (event.key === "Enter" || event.key === "Tab") {
                            event.preventDefault();
                            selectCommandSuggestion(
                              commandSuggestions[activeSuggestionIndex],
                            );
                            return;
                          }
                          if (event.key === "Escape") {
                            event.preventDefault();
                            setDismissedSuggestionInput(inputValue);
                            return;
                          }
                        }
                        if (
                          selectedCommand &&
                          event.key === "Backspace" &&
                          !composerInputValue &&
                          event.currentTarget.selectionStart === 0 &&
                          event.currentTarget.selectionEnd === 0
                        ) {
                          event.preventDefault();
                          removeSelectedCommand();
                          return;
                        }
                        if (
                          event.key === "Enter" &&
                          !event.shiftKey &&
                          !event.nativeEvent.isComposing
                        ) {
                          event.preventDefault();
                          requestSubmit(
                            activeSessionRun && (event.metaKey || event.ctrlKey)
                              ? "redirect"
                              : activeSessionRun && event.altKey
                                ? "queue_next"
                                : "supplement",
                          );
                        }
                      },
                    }),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    ),
  );
});

export default LantaiRuntimeWebUI;
