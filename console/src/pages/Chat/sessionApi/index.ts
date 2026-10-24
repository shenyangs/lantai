import {
  chooseActiveSessionMessages,
  ILantaiChatSession,
  ILantaiChatSessionAPI,
  ILantaiChatMessage,
  type ILantaiChatDraftAttachment,
  userMessageInputSignature,
} from "@/vendor/lantaiChat";
import { userMessageSubmissionIdentity } from "@/vendor/lantaiTranscriptReconciler";
import api, {
  type ChatSpec,
  type ChatHistory,
  type ChatTurnEventPreview,
  type ChatRuntimeState,
  type ChatStatus,
  type Message,
} from "../../../api";
import {
  setActiveTopicWorkspace,
  updateActiveTopicWorkspaceFromMeta,
} from "../../../utils/topicWorkspace";
import { toDisplayUrl } from "../utils";
import {
  LANTAI_REQUEST_CARD_CODE,
  LANTAI_RESPONSE_CARD_CODE,
} from "../chatCardCodes";
import {
  LantaiRuntimeContentType,
  LantaiRuntimeMessageType,
  LantaiRuntimeRunStatus,
} from "@/vendor/lantaiChatTypes";
import { hydrateQuquContinuations } from "../ququContinuationStore";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_USER_ID = "default";
const DEFAULT_CHANNEL = "console";
const DEFAULT_SESSION_NAME = "New Chat";
const ROLE_TOOL = "tool";
const ROLE_USER = "user";
const ROLE_ASSISTANT = "assistant";
const TYPE_PLUGIN_CALL = "plugin_call";
const TYPE_PLUGIN_CALL_OUTPUT = "plugin_call_output";
const TYPE_THINKING = "thinking";
const TYPE_REASONING = "reasoning";

const RUNTIME_OUTPUT_TYPES = new Set<string>(
  Object.values(LantaiRuntimeMessageType),
);

// ---------------------------------------------------------------------------
// Window globals
// ---------------------------------------------------------------------------

interface CustomWindow extends Window {
  currentSessionId?: string;
  currentUserId?: string;
  currentChannel?: string;
}

declare const window: CustomWindow;

// ---------------------------------------------------------------------------
// Local helper types
// ---------------------------------------------------------------------------

/** A single item inside a message's content array. */
interface ContentItem {
  type: string;
  text?: string;
  [key: string]: unknown;
}

/** A backend message after role-normalisation (output of toOutputMessage). */
interface OutputMessage extends Omit<Message, "role"> {
  role: string;
  metadata: unknown;
  sequence_number?: number;
}

/**
 * Extended session carrying extra fields that the library type does not define
 * but our backend / window globals require.
 */
interface ExtendedSession extends ILantaiChatSession {
  /** Session identifier (channel:user_id format) */
  sessionId: string;
  /** User identifier */
  userId: string;
  /** Channel name */
  channel: string;
  /** Additional metadata */
  meta: Record<string, unknown>;
  /** Real backend UUID, used when id is overridden with a local timestamp. */
  realId?: string;
  /** Conversation status from backend. */
  status?: ChatStatus;
  /** ISO 8601 creation timestamp from backend. */
  createdAt?: string | null;
  /** ISO 8601 last update timestamp from backend. */
  updatedAt?: string | null;
  /** Whether the backend is still generating a response for this session. */
  generating?: boolean;
  /** Whether the chat is pinned to the top. */
  pinned?: boolean;
  /** Whether the chat is hidden from the active conversation list. */
  archived?: boolean;
  /** Lightweight runtime event preview for the current chat. */
  turnEventPreview?: ChatTurnEventPreview | null;
  /** Single backend runtime state used to recover active turns. */
  runtimeState?: ChatRuntimeState | null;
  /** First prompt submitted while the backend history is still catching up. */
  pendingUserMessage?: string;
}

// ---------------------------------------------------------------------------
// Message conversion helpers: backend flat messages → card-based UI format
// ---------------------------------------------------------------------------

function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/** Extract plain text from a message's content array. */
const extractTextFromContent = (content: unknown): string => {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return String(content || "");
  return (content as ContentItem[])
    .filter((c) => c.type === "text")
    .map((c) => c.text || "")
    .filter(Boolean)
    .join("\n");
};

export function resolveContentItemUrl(c: ContentItem): ContentItem {
  if (c.type === "image" && c.image_url) {
    return { ...c, image_url: toDisplayUrl(c.image_url as string) };
  }
  if (c.type === "audio" && c.data) {
    return { ...c, data: toDisplayUrl(c.data as string) };
  }
  if (c.type === "video" && c.video_url) {
    return { ...c, video_url: toDisplayUrl(c.video_url as string) };
  }
  const nestedSource =
    c.source && typeof c.source === "object" && !Array.isArray(c.source)
      ? (c.source as Record<string, unknown>)
      : {};
  const nestedSourceUrl =
    nestedSource.type === "url" && typeof nestedSource.url === "string"
      ? nestedSource.url.trim()
      : "";
  if (c.type === "video" && nestedSourceUrl) {
    return { ...c, video_url: toDisplayUrl(nestedSourceUrl) };
  }
  if (c.type === "file" && (c.file_url || c.file_id || nestedSourceUrl)) {
    const preservedSourceUrl =
      typeof c.file_source_url === "string" ? c.file_source_url.trim() : "";
    const currentFileUrl =
      typeof c.file_url === "string" ? c.file_url.trim() : "";
    const sourceUrl =
      preservedSourceUrl ||
      currentFileUrl ||
      nestedSourceUrl ||
      (c.file_id as string);
    return {
      ...c,
      file_source_url: sourceUrl,
      file_url:
        preservedSourceUrl && currentFileUrl
          ? currentFileUrl
          : toDisplayUrl(sourceUrl),
      file_name: (c.filename as string) || (c.file_name as string) || "file",
    };
  }
  return c;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringifyContentValue(value: unknown): string {
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

function stringField(
  record: Record<string, unknown>,
  ...keys: string[]
): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
  }
  return "";
}

function timestampValue(value: unknown): string | number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") return value.trim() ? value : null;
  return null;
}

function completedTextContent(text: string): ContentItem {
  return {
    type: LantaiRuntimeContentType.TEXT,
    text,
    status: LantaiRuntimeRunStatus.Completed,
  };
}

/** Map backend message content to request card content (text + image + file). */
function contentToRequestParts(
  content: unknown,
): Array<Record<string, unknown>> {
  if (typeof content === "string") {
    return [{ type: "text", text: content, status: "created" }];
  }
  if (!Array.isArray(content)) {
    return [{ type: "text", text: String(content || ""), status: "created" }];
  }
  const parts = (content as ContentItem[])
    .map(resolveContentItemUrl)
    .map((c) => ({ ...c, status: "created" }));

  if (parts.length === 0) {
    return [{ type: "text", text: "", status: "created" }];
  }

  return parts;
}

function isThinkingContentItem(value: unknown): value is ContentItem {
  if (!isRecord(value)) return false;
  const type = typeof value.type === "string" ? value.type : "";
  return (
    type === TYPE_THINKING ||
    type === TYPE_REASONING ||
    typeof value.thinking === "string"
  );
}

function extractThinkingText(value: ContentItem): string {
  return stringField(value, "thinking", "text");
}

function normalizeOutputContentItem(value: unknown): ContentItem {
  if (!isRecord(value))
    return completedTextContent(stringifyContentValue(value));

  const rawType =
    typeof value.type === "string" && value.type.trim()
      ? value.type.trim()
      : LantaiRuntimeContentType.TEXT;
  if (rawType === TYPE_THINKING || rawType === TYPE_REASONING) {
    return completedTextContent(stringField(value, "thinking", "text"));
  }

  const item = resolveContentItemUrl({
    ...(value as Record<string, unknown>),
    type: rawType,
  } as ContentItem);

  if (rawType === LantaiRuntimeContentType.TEXT) {
    return {
      ...item,
      type: LantaiRuntimeContentType.TEXT,
      text: stringField(item, "text"),
      status:
        typeof item.status === "string"
          ? item.status
          : LantaiRuntimeRunStatus.Completed,
    };
  }

  return {
    ...item,
    status:
      typeof item.status === "string"
        ? item.status
        : LantaiRuntimeRunStatus.Completed,
  };
}

function normalizeOutputMessageContent(content: unknown): ContentItem[] {
  if (typeof content === "string") return [completedTextContent(content)];
  if (!Array.isArray(content)) {
    return content == null
      ? []
      : [completedTextContent(stringifyContentValue(content))];
  }
  return content.map(normalizeOutputContentItem);
}

function normalizeOutputMessageType(msg: Message, role: string): string {
  const rawType = typeof msg.type === "string" ? msg.type : "";
  if (rawType === TYPE_THINKING || rawType === TYPE_REASONING) {
    return LantaiRuntimeMessageType.REASONING;
  }
  if (RUNTIME_OUTPUT_TYPES.has(rawType)) return rawType;
  if (role === ROLE_TOOL) return LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT;
  return LantaiRuntimeMessageType.MESSAGE;
}

function restoredLegacyMessageMetadata(
  value: unknown,
): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  const nested = isRecord(value.metadata) ? value.metadata : {};
  return { ...nested, ...value };
}

function restoredMessageClientTurnId(message: Message): string {
  return stringField(
    restoredLegacyMessageMetadata(message.metadata) || {},
    "client_turn_id",
  ).trim();
}

function isPersistedRuntimeControlMessage(message: Message): boolean {
  if (
    message.type === TYPE_PLUGIN_CALL ||
    message.type === TYPE_PLUGIN_CALL_OUTPUT
  ) {
    return true;
  }
  const eventType = stringField(
    restoredLegacyMessageMetadata(message.metadata) || {},
    "runtime_event_type",
  );
  return (
    eventType === "tool.call.started" || eventType === "tool.call.completed"
  );
}

/**
 * Older backends merged the event ledger and memory rows by persistence time.
 * A tool-start event can win that race by milliseconds and appear above the
 * prompt that caused it. Repair only causally identified runtime controls: a
 * row moves below the next user when the following assistant fragment carries
 * the same client_turn_id. Ordinary historical assistant text never moves.
 */
function restorePersistedUserTurnAnchors(messages: Message[]): Message[] {
  const movedIndices = new Set<number>();
  const controlsByUserIndex = new Map<number, Message[]>();

  messages.forEach((message, index) => {
    if (!isPersistedRuntimeControlMessage(message)) return;
    const clientTurnId = restoredMessageClientTurnId(message);
    if (!clientTurnId) return;
    const userIndex = messages.findIndex(
      (candidate, candidateIndex) =>
        candidateIndex > index && candidate.role === ROLE_USER,
    );
    if (userIndex < 0) return;
    const nextUserIndex = messages.findIndex(
      (candidate, candidateIndex) =>
        candidateIndex > userIndex && candidate.role === ROLE_USER,
    );
    const turnEnd = nextUserIndex < 0 ? messages.length : nextUserIndex;
    const belongsToFollowingTurn = messages
      .slice(userIndex + 1, turnEnd)
      .some(
        (candidate) => restoredMessageClientTurnId(candidate) === clientTurnId,
      );
    if (!belongsToFollowingTurn) return;
    movedIndices.add(index);
    const controls = controlsByUserIndex.get(userIndex) || [];
    controls.push(message);
    controlsByUserIndex.set(userIndex, controls);
  });

  if (movedIndices.size === 0) return messages;
  return messages.flatMap((message, index) => {
    if (movedIndices.has(index)) return [];
    return [message, ...(controlsByUserIndex.get(index) || [])];
  });
}

function restoredOutputMessageId(
  msg: Message,
  role: string,
  outputType: string,
  metadata: Record<string, unknown> | null,
): string | undefined {
  const originalId = stringField(metadata || {}, "original_id").trim();
  if (!originalId) return typeof msg.id === "string" ? msg.id : undefined;
  if (role !== ROLE_ASSISTANT) return originalId;
  const suffix =
    outputType === LantaiRuntimeMessageType.REASONING
      ? ":reasoning"
      : outputType === LantaiRuntimeMessageType.MESSAGE
        ? ":message"
        : "";
  if (!suffix) return originalId;
  const baseId = originalId.replace(/:(?:reasoning|message)$/, "");
  return `${baseId}${suffix}`;
}

/**
 * Convert a backend message to a response output message.
 * Maps system + plugin_call_output → role "tool" and strips metadata.
 */
const toOutputMessage = (msg: Message): OutputMessage => {
  const role =
    msg.type === TYPE_PLUGIN_CALL_OUTPUT && msg.role === "system"
      ? ROLE_TOOL
      : msg.role;
  const type = normalizeOutputMessageType(msg, role);
  const metadata = restoredLegacyMessageMetadata(msg.metadata);
  return {
    ...msg,
    id: restoredOutputMessageId(msg, role, type, metadata),
    role,
    type,
    content: normalizeOutputMessageContent(msg.content),
    status:
      typeof msg.status === "string"
        ? msg.status
        : LantaiRuntimeRunStatus.Completed,
    metadata,
  };
};

function expandOutputMessages(msg: Message): OutputMessage[] {
  if (msg.role !== ROLE_ASSISTANT || !Array.isArray(msg.content)) {
    return [toOutputMessage(msg)];
  }

  const thinkingParts = msg.content.filter(isThinkingContentItem);
  if (!thinkingParts.length) return [toOutputMessage(msg)];

  const answerParts = msg.content.filter(
    (item) => !isThinkingContentItem(item),
  );
  const thinkingText = thinkingParts
    .map(extractThinkingText)
    .filter(Boolean)
    .join("\n");
  const base = toOutputMessage({ ...msg, content: [] });
  const restoredBaseId =
    typeof base.id === "string" && base.id.trim()
      ? base.id.trim().replace(/:(?:reasoning|message)$/, "")
      : "";
  const baseId =
    restoredBaseId ||
    (typeof msg.id === "string" && msg.id.trim()
      ? msg.id.trim().replace(/:(?:reasoning|message)$/, "")
      : generateId());
  const outputs: OutputMessage[] = [];

  if (thinkingText) {
    outputs.push({
      ...base,
      id: `${baseId}:reasoning`,
      type: LantaiRuntimeMessageType.REASONING,
      content: [completedTextContent(thinkingText)],
      status: LantaiRuntimeRunStatus.Completed,
    });
  }

  if (answerParts.length) {
    outputs.push({
      ...base,
      id: `${baseId}:message`,
      type: LantaiRuntimeMessageType.MESSAGE,
      content: normalizeOutputMessageContent(answerParts),
      status: LantaiRuntimeRunStatus.Completed,
    });
  }

  return outputs.length ? outputs : [toOutputMessage(msg)];
}

function structuredOutputItems(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string" || !value.trim()) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function structuredRecord(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) return value;
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function localFileSourceUrl(value: string): string {
  const path = value.trim();
  if (!path || /^(?:https?:|file:)/i.test(path)) return path;
  return path.startsWith("/") ? `file://${path}` : path;
}

function deliveryFileName(filePath: string, title: string): string {
  const pathName = filePath.split(/[\\/]/).filter(Boolean).pop() || "file";
  const extension = pathName.match(/(\.[^.\\/]+)$/)?.[1] || "";
  const cleanTitle = title.trim();
  if (!cleanTitle) return pathName;
  return extension &&
    !cleanTitle.toLowerCase().endsWith(extension.toLowerCase())
    ? `${cleanTitle}${extension}`
    : cleanTitle;
}

function deliveredContentFromArguments(value: unknown): ContentItem | null {
  const argumentsRecord = structuredRecord(value);
  if (!argumentsRecord) return null;
  const filePath = stringField(
    argumentsRecord,
    "file_path",
    "path",
    "file_url",
  ).trim();
  if (!filePath) return null;
  const sourceUrl = localFileSourceUrl(filePath);
  return normalizedDeliveredContent({
    type: "file",
    file_name: deliveryFileName(
      filePath,
      stringField(argumentsRecord, "title", "filename", "file_name"),
    ),
    file_source_url: sourceUrl,
    file_url: sourceUrl,
    source: { type: "url", url: sourceUrl },
  });
}

function normalizedDeliveredContent(value: unknown): ContentItem | null {
  if (
    !isRecord(value) ||
    !["file", "image", "audio", "video"].includes(String(value.type || ""))
  ) {
    return null;
  }
  const normalized = normalizeOutputContentItem(value);
  const identity = deliveredContentIdentity(normalized);
  return identity ? normalized : null;
}

function deliveredContentIdentity(content: ContentItem): string {
  const type = String(content.type || "");
  const source = isRecord(content.source) ? content.source : {};
  const nestedSourceUrl = stringField(source, "url").trim();
  const url =
    type === "file"
      ? stringField(content, "file_source_url", "file_url", "file_id").trim()
      : type === "video"
        ? stringField(content, "video_url").trim() || nestedSourceUrl
        : type === "image"
          ? stringField(content, "image_url").trim() || nestedSourceUrl
          : type === "audio"
            ? stringField(content, "data", "audio_url").trim() ||
              nestedSourceUrl
            : "";
  return type && url ? `${type}:${url}` : "";
}

/**
 * `send_file_to_user` may persist a structured plugin output or only the
 * successful call arguments. During the live turn the runtime stream projects
 * the delivery into an assistant message; do the same when rebuilding history
 * so file cards and media survive editor navigation and reopen.
 */
function projectPersistedDeliveredMedia(
  outputMessages: OutputMessage[],
): OutputMessage[] {
  const visibleDeliveries = new Set<string>();
  const failedDeliveryCallIds = new Set<string>();
  for (const message of outputMessages) {
    if (!Array.isArray(message.content)) continue;
    for (const value of message.content) {
      const content = normalizedDeliveredContent(value);
      const identity = content ? deliveredContentIdentity(content) : "";
      if (identity) visibleDeliveries.add(identity);
      if (
        message.type === LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT &&
        isRecord(value) &&
        isRecord(value.data) &&
        stringField(value.data, "name") === "send_file_to_user" &&
        (/(fail|error|cancel|reject|interrupt|stall)/i.test(
          String(message.status || ""),
        ) ||
          Boolean(stringField(value.data, "error", "error_message")))
      ) {
        const callId = stringField(
          value.data,
          "call_id",
          "tool_call_id",
          "callId",
        );
        if (callId) failedDeliveryCallIds.add(callId);
      }
    }
  }

  const projected: OutputMessage[] = [];
  for (const message of outputMessages) {
    projected.push(message);
    if (!Array.isArray(message.content)) {
      continue;
    }

    const deliveredMedia = message.content.flatMap((value) => {
      if (!isRecord(value) || !isRecord(value.data)) return [];
      if (stringField(value.data, "name") !== "send_file_to_user") return [];
      if (message.type === LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT) {
        return structuredOutputItems(value.data.output)
          .map(normalizedDeliveredContent)
          .filter((content): content is ContentItem => Boolean(content));
      }
      if (message.type === LantaiRuntimeMessageType.PLUGIN_CALL) {
        const callId = stringField(
          value.data,
          "call_id",
          "tool_call_id",
          "callId",
        );
        if (callId && failedDeliveryCallIds.has(callId)) return [];
        const content = deliveredContentFromArguments(value.data.arguments);
        return content ? [content] : [];
      }
      return [];
    });

    deliveredMedia.forEach((content, index) => {
      const identity = deliveredContentIdentity(content);
      if (!identity || visibleDeliveries.has(identity)) return;
      visibleDeliveries.add(identity);
      const sourceId =
        typeof message.id === "string" && message.id.trim()
          ? message.id
          : generateId();
      projected.push({
        ...message,
        id: `${sourceId}:delivered-${content.type}:${index}`,
        role: ROLE_ASSISTANT,
        type: LantaiRuntimeMessageType.MESSAGE,
        content: [
          {
            ...content,
            metadata: {
              ...(isRecord(content.metadata) ? content.metadata : {}),
              durable: true,
              restored_from_delivery: true,
            },
          },
        ],
        status: LantaiRuntimeRunStatus.Completed,
        metadata: {
          ...(isRecord(message.metadata) ? message.metadata : {}),
          restored_delivery_projection: true,
        },
      });
    });
  }
  return projected;
}

/** Build a user card from a user message. */
function buildUserCard(msg: Message): ILantaiChatMessage {
  const contentParts = contentToRequestParts(msg.content);
  const metadata = restoredLegacyMessageMetadata(msg.metadata) || {};
  const originalId = stringField(metadata, "original_id");
  const explicitClientTurnId = stringField(metadata, "client_turn_id");
  const clientTurnId =
    explicitClientTurnId || originalId || String(msg.id || "");
  const canonicalMetadata = clientTurnId
    ? {
        ...metadata,
        client_turn_id: clientTurnId,
        ...(!explicitClientTurnId
          ? { client_turn_id_origin: "message_id_fallback" }
          : {}),
      }
    : metadata;
  return {
    id: originalId || (msg.id as string) || generateId(),
    role: "user",
    createdAt: timestampValue(msg.created_at),
    metadata: canonicalMetadata,
    cards: [
      {
        code: LANTAI_REQUEST_CARD_CODE,
        data: {
          ...(clientTurnId
            ? {
                client_turn_id: clientTurnId,
                client_turn_id_origin: canonicalMetadata.client_turn_id_origin,
                metadata: canonicalMetadata,
              }
            : {}),
          input: [
            {
              role: "user",
              type: "message",
              content: contentParts,
              ...(clientTurnId ? { metadata: canonicalMetadata } : {}),
            },
          ],
        },
      },
    ],
  };
}

/**
 * Build an assistant response card wrapping a group of consecutive
 * non-user output messages.
 */
const buildResponseCard = (
  outputMessages: OutputMessage[],
): ILantaiChatMessage => {
  const maxSeq = outputMessages.reduce(
    (max, m) => Math.max(max, m.sequence_number || 0),
    0,
  );
  const createdAt =
    outputMessages
      .map((message) => timestampValue(message.created_at))
      .find((value) => value != null) ?? null;
  const completedAt =
    [...outputMessages]
      .reverse()
      .map((message) => timestampValue(message.created_at))
      .find((value) => value != null) ?? createdAt;

  const normalizedMessages = outputMessages.map((msg) => ({
    ...msg,
    content: normalizeOutputMessageContent(msg.content),
  }));
  const responseMetadata = [
    "client_turn_id",
    "native_runtime_turn_id",
    "runtime_turn_id",
    "turn_id",
    "run_id",
  ].reduce<Record<string, string>>((metadata, key) => {
    const value = outputMessages
      .map((message) =>
        isRecord(message.metadata) ? stringField(message.metadata, key) : "",
      )
      .find(Boolean);
    if (value) metadata[key] = value;
    return metadata;
  }, {});
  const hasCanonicalTerminalReply = outputMessages.some(
    (message) =>
      isRecord(message.metadata) &&
      message.metadata.canonical_terminal_reply === true,
  );

  return {
    id: generateId(),
    role: ROLE_ASSISTANT,
    createdAt,
    cards: [
      {
        code: LANTAI_RESPONSE_CARD_CODE,
        data: {
          id: `response_${generateId()}`,
          output: normalizedMessages,
          object: "response",
          status: "completed",
          created_at: createdAt,
          sequence_number: maxSeq + 1,
          error: null,
          completed_at: completedAt,
          usage: null,
          metadata: {
            ...responseMetadata,
            // The backend history array is the canonical transcript ledger.
            // Live SSE output may arrive through multiple transports and use a
            // different temporary order; reconciliation must let this snapshot
            // own row positions without discarding richer live content.
            canonical_history_timeline: true,
            ...(hasCanonicalTerminalReply
              ? { canonical_terminal_reply: true }
              : {}),
          },
        },
      },
    ],
    msgStatus: "finished",
  };
};

/**
 * Convert flat backend messages into the card-based format expected by
 * the @/vendor/lantaiChat component.
 *
 * - User messages become request cards.
 * - Consecutive non-user messages (assistant / system / tool) are grouped
 *   into a single response card with all output messages.
 */
const convertMessages = (messages: Message[]): ILantaiChatMessage[] => {
  const result: ILantaiChatMessage[] = [];
  const anchoredMessages = restorePersistedUserTurnAnchors(messages);
  let i = 0;

  while (i < anchoredMessages.length) {
    if (anchoredMessages[i].role === ROLE_USER) {
      result.push(buildUserCard(anchoredMessages[i++]));
    } else {
      const outputMsgs: OutputMessage[] = [];
      while (
        i < anchoredMessages.length &&
        anchoredMessages[i].role !== ROLE_USER
      ) {
        outputMsgs.push(...expandOutputMessages(anchoredMessages[i++]));
      }
      if (outputMsgs.length) {
        result.push(
          buildResponseCard(projectPersistedDeliveredMedia(outputMsgs)),
        );
      }
    }
  }

  return result;
};

const chatSpecToSession = (chat: ChatSpec): ExtendedSession =>
  ({
    id: chat.id,
    name: chat.name || DEFAULT_SESSION_NAME,
    sessionId: chat.session_id,
    userId: chat.user_id,
    channel: chat.channel,
    messages: [],
    meta: chat.meta || {},
    status: chat.status ?? "idle",
    generating: chat.status === "running",
    createdAt: chat.created_at ?? null,
    updatedAt: chat.updated_at ?? null,
    pinned: chat.pinned ?? false,
    archived: chat.archived ?? false,
  }) as ExtendedSession;

function keepSessionListMessages(
  messages?: ILantaiChatMessage[],
): ILantaiChatMessage[] {
  if (!Array.isArray(messages)) return [];
  return messages.filter((message) => message.role === ROLE_USER);
}

function preservedMessageIdentity(message: ILantaiChatMessage): string {
  if (message.role === ROLE_USER) {
    const identity = userMessageSubmissionIdentity(message);
    if (identity.explicit && identity.key) return `turn:${identity.key}`;
    const turnId = messageClientTurnId(message);
    if (turnId) return `turn:${turnId}`;
  }
  if (message.role === ROLE_ASSISTANT && Array.isArray(message.cards)) {
    for (const card of message.cards) {
      if (card?.code !== LANTAI_RESPONSE_CARD_CODE || !isRecord(card.data)) {
        continue;
      }
      const metadata = isRecord(card.data.metadata) ? card.data.metadata : {};
      const turnId = stringField(
        metadata,
        "native_runtime_turn_id",
        "turn_id",
        "run_id",
        "client_turn_id",
      ).trim();
      if (turnId) return `turn:${turnId}`;
    }
  }
  const id = typeof message.id === "string" ? message.id.trim() : "";
  return id ? `id:${id}` : "";
}

function canonicalIdentityValue(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) {
    return `[${value.map(canonicalIdentityValue).join(",")}]`;
  }
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .filter(
        (key) =>
          !["id", "status", "metadata", "created_at", "updated_at"].includes(
            key,
          ),
      )
      .sort()
      .map(
        (key) => `${JSON.stringify(key)}:${canonicalIdentityValue(value[key])}`,
      )
      .join(",")}}`;
  }
  if (value === undefined) return "undefined";
  return JSON.stringify(value) ?? String(value);
}

function preservedMessageFallbackIdentity(message: ILantaiChatMessage): string {
  if (message.role !== ROLE_USER || !Array.isArray(message.cards)) return "";
  const contentSignature = userMessageInputSignature(message);
  if (contentSignature) return `user:${contentSignature}`;
  const requestInputs = message.cards
    .filter(
      (card) => card?.code === LANTAI_REQUEST_CARD_CODE && isRecord(card.data),
    )
    .map((card) => (card.data as Record<string, unknown>).input);
  return requestInputs.length > 0
    ? `user:${canonicalIdentityValue(requestInputs)}`
    : "";
}

function preservedMessagesMatch(
  existing: ILantaiChatMessage,
  incoming: ILantaiChatMessage,
): boolean {
  if (existing.role !== incoming.role) return false;
  const existingIdentity = preservedMessageIdentity(existing);
  const incomingIdentity = preservedMessageIdentity(incoming);
  if (existingIdentity || incomingIdentity) {
    return Boolean(existingIdentity && existingIdentity === incomingIdentity);
  }
  const fallback = preservedMessageFallbackIdentity(existing);
  return Boolean(
    fallback && fallback === preservedMessageFallbackIdentity(incoming),
  );
}

function mergePreservedSessionMessages(
  existingMessages?: ILantaiChatMessage[],
  incomingMessages?: ILantaiChatMessage[],
): ILantaiChatMessage[] {
  const existing = Array.isArray(existingMessages) ? existingMessages : [];
  const incoming = Array.isArray(incomingMessages) ? incomingMessages : [];
  if (existing.length === 0) return incoming;
  if (incoming.length === 0) return existing;

  // The direct /console/chat response stream owns live assistant content.
  // Session refreshes may preserve accepted user cards, but must never append
  // stale assistant snapshots into an active streamed turn.
  const merged: {
    message: ILantaiChatMessage;
    incomingIndex?: number;
  }[] = incoming.map((message, incomingIndex) => ({ message, incomingIndex }));
  const consumedIncoming = new Set<number>();
  let insertionIndex = 0;
  for (const existingMessage of existing) {
    // Match the immutable incoming snapshot. Insertion into merged must not
    // move consumed indexes and make one server message match twice.
    const incomingIndex = incoming.findIndex((incomingMessage, index) => {
      if (consumedIncoming.has(index)) return false;
      return preservedMessagesMatch(existingMessage, incomingMessage);
    });
    if (incomingIndex !== -1) {
      consumedIncoming.add(incomingIndex);
      insertionIndex =
        merged.findIndex((entry) => entry.incomingIndex === incomingIndex) + 1;
      continue;
    }
    if (existingMessage.role === ROLE_USER) {
      merged.splice(insertionIndex, 0, { message: existingMessage });
      insertionIndex += 1;
    }
  }
  return merged.map((entry) => entry.message);
}

function latestUserMessageIndex(messages: ILantaiChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === ROLE_USER) return index;
  }
  return -1;
}

function messageClientTurnId(message: ILantaiChatMessage): string {
  const messageRecord = message as Record<string, unknown>;
  const messageMetadata = isRecord(messageRecord.metadata)
    ? messageRecord.metadata
    : {};
  const direct =
    stringField(messageRecord, "client_turn_id").trim() ||
    stringField(messageMetadata, "client_turn_id").trim();
  if (direct) return direct;

  for (const card of message.cards || []) {
    if (!isRecord(card?.data)) continue;
    const data = card.data;
    const dataMetadata = isRecord(data.metadata) ? data.metadata : {};
    const cardIdentity = stringField(
      {
        client_turn_id:
          data.client_turn_id || dataMetadata.client_turn_id || "",
      },
      "client_turn_id",
    ).trim();
    if (cardIdentity) return cardIdentity;

    for (const field of ["input", "output"] as const) {
      const entries = Array.isArray(data[field]) ? data[field] : [];
      for (const entry of entries) {
        if (!isRecord(entry)) continue;
        const entryMetadata = isRecord(entry.metadata) ? entry.metadata : {};
        const entryIdentity = stringField(
          {
            client_turn_id:
              entry.client_turn_id || entryMetadata.client_turn_id || "",
          },
          "client_turn_id",
        ).trim();
        if (entryIdentity) return entryIdentity;
      }
    }
  }
  return "";
}

function isCompletedAssistantSnapshot(message: ILantaiChatMessage): boolean {
  if (message.role !== ROLE_ASSISTANT || !Array.isArray(message.cards)) {
    return false;
  }
  if (message.msgStatus === "finished") return true;
  return message.cards.some((card) => {
    if (card?.code !== LANTAI_RESPONSE_CARD_CODE || !isRecord(card.data)) {
      return false;
    }
    return ["completed", "finished"].includes(
      String(card.data.status || "").toLowerCase(),
    );
  });
}

function containsEveryCachedUser(
  cachedMessages: ILantaiChatMessage[],
  historyMessages: ILantaiChatMessage[],
): boolean {
  const remainingHistoryUsers = historyMessages.filter(
    (message) => message.role === ROLE_USER,
  );
  return cachedMessages
    .filter((message) => message.role === ROLE_USER)
    .every((cachedUser) => {
      const matchIndex = remainingHistoryUsers.findIndex((historyUser) =>
        preservedMessagesMatch(cachedUser, historyUser),
      );
      if (matchIndex < 0) return false;
      remainingHistoryUsers.splice(matchIndex, 1);
      return true;
    });
}

/**
 * Keep the exact terminal response the user already saw when a route change
 * remounts Chat. The history endpoint can trail the direct response stream by
 * one write and must not replace a completed answer or its delivered files.
 */
function reconcileHydratedSessionMessages(
  cachedMessages: ILantaiChatMessage[] | undefined,
  historyMessages: ILantaiChatMessage[],
  generating: boolean,
): ILantaiChatMessage[] {
  const cached = Array.isArray(cachedMessages) ? cachedMessages : [];
  if (cached.length === 0) return historyMessages;
  if (historyMessages.length === 0) return cached;

  const reconciled = chooseActiveSessionMessages(cached, historyMessages);
  if (generating) return reconciled;

  const cachedUserIndex = latestUserMessageIndex(cached);
  const historyUserIndex = latestUserMessageIndex(historyMessages);
  if (cachedUserIndex >= 0 && historyUserIndex >= 0) {
    const cachedUserSignature = userMessageInputSignature(
      cached[cachedUserIndex],
    );
    const historyUserSignature = userMessageInputSignature(
      historyMessages[historyUserIndex],
    );
    const sameLatestTurn = Boolean(
      cachedUserSignature && cachedUserSignature === historyUserSignature,
    );
    const cachedClientTurnId = messageClientTurnId(cached[cachedUserIndex]);
    const historyClientTurnId = messageClientTurnId(
      historyMessages[historyUserIndex],
    );
    const stableClientTurnId =
      sameLatestTurn &&
      cachedClientTurnId &&
      cachedClientTurnId === historyClientTurnId
        ? cachedClientTurnId
        : "";
    if (!stableClientTurnId) return reconciled;

    const cachedTerminalSnapshots = cached.filter(
      (message) =>
        messageClientTurnId(message) === stableClientTurnId &&
        isCompletedAssistantSnapshot(message),
    );
    if (cachedTerminalSnapshots.length === 0) return reconciled;

    const baseline = containsEveryCachedUser(cached, historyMessages)
      ? historyMessages
      : reconciled;
    const preserved: ILantaiChatMessage[] = [];
    let replacedHistorySnapshot = false;
    for (const message of baseline) {
      const isSameTurnAssistant =
        message.role === ROLE_ASSISTANT &&
        messageClientTurnId(message) === stableClientTurnId;
      if (!isSameTurnAssistant) {
        preserved.push(message);
        continue;
      }
      if (!replacedHistorySnapshot) {
        preserved.push(...cachedTerminalSnapshots);
        replacedHistorySnapshot = true;
      }
    }
    if (!replacedHistorySnapshot) preserved.push(...cachedTerminalSnapshots);
    return preserved;
  }

  return reconciled;
}

function pendingTextFromSessionName(name: unknown): string {
  if (typeof name !== "string") return "";
  const text = name.split("\u200B").join("").trim();
  if (!text || text === DEFAULT_SESSION_NAME) return "";
  return text;
}

/** Returns true for legacy timestamp ids and collision-safe local ids. */
const isLocalTimestamp = (id: string): boolean =>
  /^\d+$/.test(id) || id.startsWith("local:");

function createLocalSessionId(): string {
  const randomId = globalThis.crypto?.randomUUID?.();
  if (randomId) return `local:${randomId}`;
  return `local:${Date.now()}:${Math.random().toString(36).slice(2, 14)}`;
}

/** Detect if backend is still generating content for this chat. */
const isGenerating = (chatHistory: ChatHistory): boolean => {
  const runtimeStatus = chatHistory.runtime_state?.status;
  if (runtimeStatus) return runtimeStatus === "running";
  if (chatHistory.status === "running") return true;
  if (chatHistory.status === "idle") return false;
  const msgs = chatHistory.messages || [];
  if (msgs.length === 0) return false;
  const last = msgs[msgs.length - 1];
  return last.role === ROLE_USER;
};

/**
 * History is addressed by canonical chat UUID. A transport session id can be
 * reused by independent chats, so never project a response into the requested
 * conversation until the backend confirms the canonical owner.
 */
function assertChatHistoryOwner(
  requestedChatId: string,
  chatHistory: ChatHistory,
): void {
  const requested = requestedChatId.trim();
  const actual = chatHistory.chat_id?.trim() || "";
  if (!requested || !actual || actual !== requested) {
    throw new Error(
      `Chat history belongs to a different chat (requested=${requested || "missing"}, actual=${actual || "missing"})`,
    );
  }
}

/**
 * Resolve and persist the real backend UUID for a local timestamp session.
 * Stores the real UUID as realId while keeping the timestamp as id, so the
 * library's internal currentSessionId (timestamp) remains valid.
 * Returns the resolved real UUID, or null if not found.
 */
const resolveRealId = (
  sessionList: ILantaiChatSession[],
  tempSessionId: string,
): { list: ILantaiChatSession[]; realId: string | null } => {
  const realSession = sessionList.find(
    (s) => (s as ExtendedSession).sessionId === tempSessionId,
  );
  if (!realSession) return { list: sessionList, realId: null };

  const realUUID = realSession.id;
  (realSession as ExtendedSession).realId = realUUID;
  realSession.id = tempSessionId;
  return {
    list: [realSession, ...sessionList.filter((s) => s !== realSession)],
    realId: realUUID,
  };
};

// ---------------------------------------------------------------------------
// Per-session user message persistence (survives page refresh)
// ---------------------------------------------------------------------------

const STORAGE_PREFIX = "lantai_pending_user_msg_";
const ACCEPTED_USER_MESSAGES_PREFIX = "lantai_accepted_user_messages_v1_";
const ALIAS_PREFIX = "lantai_session_alias_";
const DRAFT_PREFIX = "lantai_chat_draft_v1_";
const REAL_ID_WAIT_TIMEOUT_MS = 4000;
const MAX_ACCEPTED_USER_MESSAGES = 48;
const MAX_ACCEPTED_USER_MESSAGE_CHARS = 12_000;
const DUPLICATE_ACCEPTED_MESSAGE_WINDOW_MS = 5_000;

type AcceptedUserMessage = {
  id: string;
  clientTurnId?: string;
  text: string;
  createdAt: number;
};

let acceptedUserMessageSequence = 0;

function uniqueIds(ids: Array<string | null | undefined>): string[] {
  return Array.from(
    new Set(
      ids
        .map((id) => (typeof id === "string" ? id.trim() : ""))
        .filter(Boolean),
    ),
  );
}

function conversationStorageKey(prefix: string, sessionId: string): string {
  return `${prefix}${encodeURIComponent(getSelectedAgentId())}:${encodeURIComponent(
    sessionId,
  )}`;
}

function readConversationStorage(
  prefix: string,
  sessionId: string,
): string | null {
  const scopedKey = conversationStorageKey(prefix, sessionId);
  const scoped = sessionStorage.getItem(scopedKey);
  if (scoped !== null) return scoped;
  const legacyKey = `${prefix}${sessionId}`;
  const legacy = sessionStorage.getItem(legacyKey);
  if (legacy !== null) {
    sessionStorage.setItem(scopedKey, legacy);
    sessionStorage.removeItem(legacyKey);
  }
  return legacy;
}

function saveSessionAliases(ids: Array<string | null | undefined>): string[] {
  const aliases = uniqueIds(ids);
  if (aliases.length <= 1) return aliases;
  try {
    const payload = JSON.stringify(aliases);
    aliases.forEach((id) => {
      sessionStorage.setItem(conversationStorageKey(ALIAS_PREFIX, id), payload);
    });
  } catch {
    /* ignore */
  }
  return aliases;
}

function loadSessionAliases(sessionId: string): string[] {
  if (!sessionId) return [];
  try {
    const raw = readConversationStorage(ALIAS_PREFIX, sessionId);
    if (!raw) return [sessionId];
    const parsed = JSON.parse(raw);
    return uniqueIds(Array.isArray(parsed) ? parsed : [sessionId]);
  } catch {
    return [sessionId];
  }
}

function mergeSessionAliases(ids: Array<string | null | undefined>): string[] {
  return saveSessionAliases(ids);
}

function exactConversationIds(
  sessionIds: string | Array<string | null | undefined>,
): string[] {
  return uniqueIds(Array.isArray(sessionIds) ? sessionIds : [sessionIds]);
}

function savePendingUserMessage(
  sessionIds: string | Array<string | null | undefined>,
  text: string,
): void {
  const ids = exactConversationIds(sessionIds);
  if (ids.length === 0) return;
  try {
    ids.forEach((id) => {
      sessionStorage.setItem(conversationStorageKey(STORAGE_PREFIX, id), text);
    });
  } catch {
    /* quota exceeded – ignore */
  }
}

function loadPendingUserMessage(
  sessionIds: string | Array<string | null | undefined>,
): string {
  const ids = exactConversationIds(sessionIds);
  try {
    for (const id of ids) {
      const text = readConversationStorage(STORAGE_PREFIX, id) || "";
      if (text) return text;
    }
    return "";
  } catch {
    return "";
  }
}

function clearPendingUserMessage(
  sessionIds: string | Array<string | null | undefined>,
): void {
  const ids = exactConversationIds(sessionIds);
  try {
    ids.forEach((id) => {
      sessionStorage.removeItem(conversationStorageKey(STORAGE_PREFIX, id));
      sessionStorage.removeItem(`${STORAGE_PREFIX}${id}`);
    });
  } catch {
    /* ignore */
  }
}

function acceptedUserMessageStorageIds(
  sessionIds: string | Array<string | null | undefined>,
): string[] {
  return exactConversationIds(sessionIds);
}

function parseAcceptedUserMessages(raw: string | null): AcceptedUserMessage[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((value): AcceptedUserMessage[] => {
      if (!isRecord(value)) return [];
      const id = typeof value.id === "string" ? value.id.trim() : "";
      const text = typeof value.text === "string" ? value.text : "";
      const clientTurnId =
        typeof value.clientTurnId === "string"
          ? value.clientTurnId.trim()
          : id.startsWith("client-turn:")
            ? id.slice("client-turn:".length).trim()
            : "";
      const createdAt =
        typeof value.createdAt === "number" && Number.isFinite(value.createdAt)
          ? value.createdAt
          : 0;
      if (!id || !text || !createdAt) return [];
      return [
        {
          id,
          ...(clientTurnId ? { clientTurnId } : {}),
          text,
          createdAt,
        },
      ];
    });
  } catch {
    return [];
  }
}

function loadAcceptedUserMessages(
  sessionIds: string | Array<string | null | undefined>,
): AcceptedUserMessage[] {
  const ids = acceptedUserMessageStorageIds(sessionIds);
  const byId = new Map<string, AcceptedUserMessage>();
  try {
    ids.forEach((id) => {
      const raw = readConversationStorage(ACCEPTED_USER_MESSAGES_PREFIX, id);
      parseAcceptedUserMessages(raw).forEach((message) => {
        if (!byId.has(message.id)) byId.set(message.id, message);
      });
    });
  } catch {
    return [];
  }
  return Array.from(byId.values())
    .sort((left, right) => left.createdAt - right.createdAt)
    .slice(-MAX_ACCEPTED_USER_MESSAGES);
}

function saveAcceptedUserMessages(
  sessionIds: string | Array<string | null | undefined>,
  messages: AcceptedUserMessage[],
): void {
  const ids = acceptedUserMessageStorageIds(sessionIds);
  if (ids.length === 0) return;
  const payload = JSON.stringify(messages.slice(-MAX_ACCEPTED_USER_MESSAGES));
  try {
    ids.forEach((id) => {
      sessionStorage.setItem(
        conversationStorageKey(ACCEPTED_USER_MESSAGES_PREFIX, id),
        payload,
      );
    });
  } catch {
    /* quota exceeded – retain the regular pending-message fallback */
  }
}

function rememberAcceptedUserMessage(
  sessionIds: string | Array<string | null | undefined>,
  text: string,
  clientTurnId?: string,
): void {
  const normalizedText = text.slice(0, MAX_ACCEPTED_USER_MESSAGE_CHARS);
  if (!normalizedText) return;

  const messages = loadAcceptedUserMessages(sessionIds);
  const now = Date.now();
  const normalizedClientTurnId = clientTurnId?.trim() || "";
  if (normalizedClientTurnId) {
    const stableMessageId = `client-turn:${normalizedClientTurnId}`;
    const existing = messages.find((message) => message.id === stableMessageId);
    if (existing) {
      // The same submission is cached against temporary, ensured and backend
      // aliases. Its client turn id is the durable idempotency key; elapsed
      // time and repeated route hydration must never create another bubble.
      existing.text = normalizedText;
      existing.clientTurnId = normalizedClientTurnId;
      saveAcceptedUserMessages(sessionIds, messages);
      return;
    }
    messages.push({
      id: stableMessageId,
      clientTurnId: normalizedClientTurnId,
      text: normalizedText,
      createdAt: now,
    });
    saveAcceptedUserMessages(sessionIds, messages);
    return;
  }

  const latest = messages[messages.length - 1];
  // A submission is deliberately cached before and after its workspace is
  // resolved. Treat those immediate writes as one accepted user turn while
  // still allowing a user to intentionally send the same text again later.
  if (
    latest?.text === normalizedText &&
    now - latest.createdAt < DUPLICATE_ACCEPTED_MESSAGE_WINDOW_MS
  ) {
    saveAcceptedUserMessages(sessionIds, messages);
    return;
  }

  messages.push({
    id: `${now.toString(36)}-${(++acceptedUserMessageSequence).toString(36)}`,
    text: normalizedText,
    createdAt: now,
  });
  saveAcceptedUserMessages(sessionIds, messages);
}

function synchronizeAcceptedUserMessages(
  sessionIds: Array<string | null | undefined>,
): void {
  const messages = loadAcceptedUserMessages(sessionIds);
  if (messages.length > 0) saveAcceptedUserMessages(sessionIds, messages);
}

function clearAcceptedUserMessages(
  sessionIds: string | Array<string | null | undefined>,
): void {
  const ids = acceptedUserMessageStorageIds(sessionIds);
  try {
    ids.forEach((id) => {
      sessionStorage.removeItem(
        conversationStorageKey(ACCEPTED_USER_MESSAGES_PREFIX, id),
      );
      sessionStorage.removeItem(`${ACCEPTED_USER_MESSAGES_PREFIX}${id}`);
    });
  } catch {
    /* ignore unavailable storage */
  }
}

function getSelectedAgentId(): string {
  try {
    const raw =
      sessionStorage.getItem("lantai-agent-storage") ||
      localStorage.getItem("lantai-agent-storage");
    if (!raw) return "default";
    const parsed = JSON.parse(raw);
    return parsed?.state?.selectedAgent || "default";
  } catch {
    return "default";
  }
}

function draftStorageKey(agentId: string, sessionId: string): string {
  return `${DRAFT_PREFIX}${encodeURIComponent(agentId)}:${encodeURIComponent(
    sessionId,
  )}`;
}

function draftAttachmentStorageKey(agentId: string, sessionId: string): string {
  return `${DRAFT_PREFIX}attachments:${encodeURIComponent(
    agentId,
  )}:${encodeURIComponent(sessionId)}`;
}

function isDraftAttachment(
  value: unknown,
): value is ILantaiChatDraftAttachment {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === "string" &&
    typeof value.name === "string" &&
    typeof value.type === "string" &&
    typeof value.size === "number" &&
    typeof value.url === "string" &&
    Boolean(value.url) &&
    value.status === "ready"
  );
}

function saveSessionDraftAttachments(
  sessionIds: string | Array<string | null | undefined>,
  attachments: ILantaiChatDraftAttachment[],
): void {
  const ids = exactConversationIds(sessionIds);
  if (ids.length === 0) return;
  const agentId = getSelectedAgentId();
  try {
    ids.forEach((id) => {
      sessionStorage.setItem(
        draftAttachmentStorageKey(agentId, id),
        JSON.stringify(attachments),
      );
    });
  } catch {
    /* quota exceeded – ignore */
  }
}

function loadSessionDraftAttachments(
  sessionIds: string | Array<string | null | undefined>,
): ILantaiChatDraftAttachment[] {
  const ids = exactConversationIds(sessionIds);
  const agentId = getSelectedAgentId();
  try {
    for (const id of ids) {
      const raw = sessionStorage.getItem(
        draftAttachmentStorageKey(agentId, id),
      );
      if (!raw) continue;
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) continue;
      return parsed.filter(isDraftAttachment);
    }
  } catch {
    /* ignore corrupt or unavailable storage */
  }
  return [];
}

function clearSessionDraftAttachments(
  sessionIds: string | Array<string | null | undefined>,
): void {
  const ids = exactConversationIds(sessionIds);
  const agentId = getSelectedAgentId();
  try {
    ids.forEach((id) => {
      sessionStorage.removeItem(draftAttachmentStorageKey(agentId, id));
    });
  } catch {
    /* ignore */
  }
}

function saveSessionDraft(
  sessionIds: string | Array<string | null | undefined>,
  text: string,
): void {
  const ids = exactConversationIds(sessionIds);
  if (ids.length === 0) return;
  const agentId = getSelectedAgentId();
  try {
    ids.forEach((id) => {
      sessionStorage.setItem(draftStorageKey(agentId, id), text);
    });
  } catch {
    /* quota exceeded – ignore */
  }
}

function loadSessionDraft(
  sessionIds: string | Array<string | null | undefined>,
): string {
  const ids = exactConversationIds(sessionIds);
  const agentId = getSelectedAgentId();
  try {
    for (const id of ids) {
      const text = sessionStorage.getItem(draftStorageKey(agentId, id));
      if (text !== null) return text;
    }
  } catch {
    /* ignore */
  }
  return "";
}

function clearSessionDraft(
  sessionIds: string | Array<string | null | undefined>,
): void {
  const ids = exactConversationIds(sessionIds);
  const agentId = getSelectedAgentId();
  try {
    ids.forEach((id) => {
      sessionStorage.removeItem(draftStorageKey(agentId, id));
    });
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// SessionApi
// ---------------------------------------------------------------------------

const SESSION_LIST_CACHE_TTL_MS = 15_000;

class SessionApi implements ILantaiChatSessionAPI {
  private sessionList: ILantaiChatSession[] = [];
  private activeAgentId = getSelectedAgentId();
  private agentContextGeneration = 0;
  private selectionEpoch = 0;
  private activeSelection: {
    agentId: string;
    agentGeneration: number;
    epoch: number;
    sessionId: string;
  } | null = null;

  /**
   * A SessionApi instance is shared by the whole renderer, but its mutable
   * conversation state belongs to exactly one agent at a time.
   */
  activateAgent(agentId: string): void {
    const normalizedAgentId = agentId.trim() || "default";
    if (this.activeAgentId === normalizedAgentId) return;

    this.activeAgentId = normalizedAgentId;
    this.agentContextGeneration += 1;
    this.sessionList = [];
    this.sessionListRequest = null;
    this.sessionListFetchedAt = 0;
    this.sessionRequests.clear();
    this.preferredChatId = null;
    this.lastSelectedSessionId = null;
    this.selectionEpoch += 1;
    this.activeSelection = null;
    window.currentSessionId = "";
    window.currentUserId = DEFAULT_USER_ID;
    window.currentChannel = DEFAULT_CHANNEL;
    setActiveTopicWorkspace(null);
    for (const resolvers of this.realIdResolvers.values()) {
      for (const resolve of resolvers) resolve();
    }
    this.realIdResolvers.clear();
  }

  private captureAgentContext(): { agentId: string; generation: number } {
    const agentId = getSelectedAgentId();
    this.activateAgent(agentId);
    return { agentId, generation: this.agentContextGeneration };
  }

  private ownsAgentContext(scope: {
    agentId: string;
    generation: number;
  }): boolean {
    return (
      this.activeAgentId === scope.agentId &&
      this.agentContextGeneration === scope.generation &&
      getSelectedAgentId() === scope.agentId
    );
  }

  private assertOwnsAgentContext(scope: {
    agentId: string;
    generation: number;
  }): void {
    if (!this.ownsAgentContext(scope)) {
      throw new Error("Conversation request no longer owns the active agent");
    }
  }

  /**
   * Establish the single renderer-owned conversation identity before any
   * asynchronous history request can project channel or send metadata.
   */
  activateSession(sessionId?: string | null): void {
    const scope = this.captureAgentContext();
    const normalized = typeof sessionId === "string" ? sessionId.trim() : "";
    this.selectionEpoch += 1;
    this.activeSelection = normalized
      ? {
          agentId: scope.agentId,
          agentGeneration: scope.generation,
          epoch: this.selectionEpoch,
          sessionId: normalized,
        }
      : null;
    if (!normalized) {
      window.currentSessionId = "";
      window.currentUserId = DEFAULT_USER_ID;
      window.currentChannel = DEFAULT_CHANNEL;
      setActiveTopicWorkspace(null);
    }
  }

  private selectionOwnsSession(session: ExtendedSession): boolean {
    const selection = this.activeSelection;
    if (
      !selection ||
      selection.agentId !== this.activeAgentId ||
      selection.agentGeneration !== this.agentContextGeneration ||
      selection.epoch !== this.selectionEpoch
    ) {
      return false;
    }
    return uniqueIds([session.id, session.realId, session.sessionId]).includes(
      selection.sessionId,
    );
  }

  /**
   * Resolve every persistence operation to one authoritative conversation.
   *
   * Older builds stored aliases as transitive cliques. If one shared backend
   * session id was ever reused, that could permanently join two chats in
   * sessionStorage. Expand legacy aliases only as evidence, then retain IDs
   * that belong to one unambiguous chat. An ambiguous alias on its own fails
   * closed instead of selecting whichever chat happens to be listed first.
   */
  private safeConversationIds(
    sessionIds: string | Array<string | null | undefined>,
  ): string[] {
    const seeds = exactConversationIds(sessionIds);
    if (seeds.length === 0) return [];

    const expanded = uniqueIds(
      seeds.flatMap((sessionId) => loadSessionAliases(sessionId)),
    );
    const ownerFor = (
      candidate: string,
    ): ExtendedSession | null | undefined => {
      const exactMatches = this.sessionList.filter((session) => {
        const extended = session as ExtendedSession;
        return session.id === candidate || extended.realId === candidate;
      });
      if (exactMatches.length === 1) {
        return exactMatches[0] as ExtendedSession;
      }
      if (exactMatches.length > 1) return null;
      return undefined;
    };
    const ownerKey = (session: ExtendedSession): string =>
      session.realId || session.id;
    const seedOwners = new Map<string, ExtendedSession>();
    for (const seed of seeds) {
      const owner = ownerFor(seed);
      if (owner) seedOwners.set(ownerKey(owner), owner);
      if (owner === null && seeds.length === 1) return [];
    }
    if (seedOwners.size > 1) return [];

    const expandedOwners = new Map<string, ExtendedSession>();
    for (const candidate of expanded) {
      const owner = ownerFor(candidate);
      if (owner) expandedOwners.set(ownerKey(owner), owner);
    }
    const selectedOwner =
      seedOwners.values().next().value ||
      (expandedOwners.size === 1
        ? expandedOwners.values().next().value
        : undefined);
    if (!selectedOwner) return seeds;

    const selectedOwnerKey = ownerKey(selectedOwner);
    const safe = expanded.filter((candidate) => {
      const owner = ownerFor(candidate);
      return Boolean(owner && ownerKey(owner) === selectedOwnerKey);
    });
    return uniqueIds([...safe, selectedOwner.id, selectedOwner.realId]);
  }

  /**
   * Pending resolvers waiting for a specific session's realId.
   * Used to replace setTimeout-based busy-wait with event-driven notification.
   */
  private realIdResolvers: Map<string, Array<() => void>> = new Map();

  /** Notify any pending waiters that a session's realId has been resolved. */
  private notifyRealIdResolved(sessionId: string): void {
    const resolvers = this.realIdResolvers.get(sessionId);
    if (resolvers) {
      this.realIdResolvers.delete(sessionId);
      for (const resolve of resolvers) resolve();
    }
  }

  /** Wait until a session's realId is available (set by updateSession). */
  private waitForRealId(sessionId: string): Promise<boolean> {
    const session = this.sessionList.find((x) => x.id === sessionId) as
      | ExtendedSession
      | undefined;
    if (session?.realId) return Promise.resolve(true);

    return new Promise<boolean>((resolve) => {
      let settled = false;
      let onResolved: (() => void) | null = null;
      const timer = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        if (onResolved) {
          const pending = this.realIdResolvers.get(sessionId) || [];
          const next = pending.filter((item) => item !== onResolved);
          if (next.length > 0) {
            this.realIdResolvers.set(sessionId, next);
          } else {
            this.realIdResolvers.delete(sessionId);
          }
        }
        resolve(false);
      }, REAL_ID_WAIT_TIMEOUT_MS);
      onResolved = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        resolve(true);
      };
      const existing = this.realIdResolvers.get(sessionId) || [];
      existing.push(onResolved);
      this.realIdResolvers.set(sessionId, existing);
    });
  }

  /**
   * When set, getSessionList will move the matching session to the front on the first call,
   * so the library's useMount auto-selects it instead of always defaulting to sessions[0].
   * Cleared after first use.
   */
  preferredChatId: string | null = null;

  /**
   * Cache the latest user message for a chat so it can be patched into
   * history during reconnect (the backend only persists it after generation
   * completes). Persisted to sessionStorage so it survives page refresh.
   */
  setLastUserMessage(
    sessionId: string | Array<string | null | undefined>,
    text: string,
    clientTurnId?: string,
  ): void {
    if (!text) return;
    const ownerIds = this.safeConversationIds(sessionId);
    if (ownerIds.length === 0) return;
    savePendingUserMessage(ownerIds, text);
    rememberAcceptedUserMessage(ownerIds, text, clientTurnId);
  }

  /** Stage an outgoing prompt before the backend has accepted the turn. */
  stageLastUserMessage(
    sessionId: string | Array<string | null | undefined>,
    text: string,
  ): void {
    if (!text) return;
    const ownerIds = this.safeConversationIds(sessionId);
    if (ownerIds.length === 0) return;
    savePendingUserMessage(ownerIds, text);
  }

  /** Promote a staged prompt only after the chat endpoint accepted it. */
  acceptLastUserMessage(
    sessionId: string | Array<string | null | undefined>,
    text: string,
    clientTurnId?: string,
  ): void {
    if (!text) return;
    const ownerIds = this.safeConversationIds(sessionId);
    if (ownerIds.length === 0) return;
    savePendingUserMessage(ownerIds, text);
    rememberAcceptedUserMessage(ownerIds, text, clientTurnId);
  }

  /** Remove an unaccepted prompt after an HTTP or active-run conflict. */
  discardPendingUserMessage(
    sessionId: string | Array<string | null | undefined>,
  ): void {
    clearPendingUserMessage(this.safeConversationIds(sessionId));
  }

  loadDraftText(sessionId: string): string {
    return loadSessionDraft(this.safeConversationIds(sessionId));
  }

  saveDraftText(sessionId: string, text: string): void {
    if (!text) {
      clearSessionDraft(this.safeConversationIds(sessionId));
      return;
    }
    saveSessionDraft(this.safeConversationIds(sessionId), text);
  }

  clearDraftText(sessionId: string): void {
    clearSessionDraft(this.safeConversationIds(sessionId));
  }

  loadDraftAttachments(sessionId: string): ILantaiChatDraftAttachment[] {
    return loadSessionDraftAttachments(this.safeConversationIds(sessionId));
  }

  saveDraftAttachments(
    sessionId: string,
    attachments: ILantaiChatDraftAttachment[],
  ): void {
    if (attachments.length === 0) {
      clearSessionDraftAttachments(this.safeConversationIds(sessionId));
      return;
    }
    saveSessionDraftAttachments(
      this.safeConversationIds(sessionId),
      attachments,
    );
  }

  clearDraftAttachments(sessionId: string): void {
    clearSessionDraftAttachments(this.safeConversationIds(sessionId));
  }

  /** Persist all known aliases for one logical conversation. */
  rememberSessionAlias(...sessionIds: Array<string | null | undefined>): void {
    const candidates = this.safeConversationIds(sessionIds);
    if (candidates.length === 0) return;
    const authoritativeOwners = new Set(
      candidates.flatMap((candidate) =>
        this.sessionList
          .filter(
            (session) =>
              session.id === candidate ||
              (session as ExtendedSession).realId === candidate,
          )
          .map(
            (session) =>
              (session as ExtendedSession).realId || session.id || "",
          )
          .filter(Boolean),
      ),
    );
    if (authoritativeOwners.size > 1) {
      console.warn(
        "Rejected conversation alias merge across authoritative chats",
        candidates,
      );
      return;
    }
    const safeCandidates = candidates.filter((candidate) => {
      const authoritative = this.sessionList.some(
        (session) =>
          session.id === candidate ||
          (session as ExtendedSession).realId === candidate,
      );
      if (authoritative) return true;
      const aliasOwners = this.sessionList.filter(
        (session) => (session as ExtendedSession).sessionId === candidate,
      );
      return aliasOwners.length <= 1;
    });
    const aliases = mergeSessionAliases(safeCandidates);
    synchronizeAcceptedUserMessages(aliases);
  }

  /**
   * Resolve local timestamp ids as soon as topic-workspace creation returns a
   * real backend chat id. Otherwise a brand-new chat can wait forever on the
   * loading placeholder before the actual /console/chat request starts.
   */
  resolveEnsuredSession(
    localSessionIds: string | Array<string | null | undefined>,
    realChatId: string | null | undefined,
    backendSessionId?: string | null,
  ): void {
    const realId = typeof realChatId === "string" ? realChatId.trim() : "";
    if (!realId) return;

    const localIds = uniqueIds(
      Array.isArray(localSessionIds) ? localSessionIds : [localSessionIds],
    );
    this.rememberSessionAlias(...localIds, realId);

    for (const localId of localIds) {
      if (!localId || localId === realId) continue;

      const index = this.sessionList.findIndex(
        (s) => s.id === localId || (s as ExtendedSession).sessionId === localId,
      );
      const existing =
        index > -1 ? (this.sessionList[index] as ExtendedSession) : undefined;
      const nextSessionId = backendSessionId || existing?.sessionId || localId;
      const shouldUseAliasAsUiId =
        isLocalTimestamp(localId) || existing?.id === localId;

      if (existing) {
        this.sessionList[index] = {
          ...existing,
          id: shouldUseAliasAsUiId ? localId : existing.id,
          sessionId: nextSessionId,
          realId,
          messages: keepSessionListMessages(existing.messages),
        } as ExtendedSession;
      } else if (isLocalTimestamp(localId)) {
        this.sessionList.unshift({
          id: localId,
          name: DEFAULT_SESSION_NAME,
          sessionId: nextSessionId,
          userId: DEFAULT_USER_ID,
          channel: DEFAULT_CHANNEL,
          messages: [],
          meta: {},
          realId,
        } as ExtendedSession);
      }

      if (shouldUseAliasAsUiId || isLocalTimestamp(localId)) {
        this.notifyRealIdResolved(localId);
      }
      if (isLocalTimestamp(localId)) {
        this.onSessionIdResolved?.(localId, realId);
      }
    }
  }

  /**
   * Deduplicates concurrent getSessionList calls so that two parallel
   * invocations share one network request and write sessionList only once,
   * preserving any realId mappings that were already resolved.
   */
  private sessionListRequest: Promise<ILantaiChatSession[]> | null = null;
  private sessionListFetchedAt = 0;

  /**
   * Deduplicates concurrent getSession calls for the same sessionId.
   * Key: sessionId, Value: in-flight promise for getSession.
   */
  private sessionRequests: Map<string, Promise<ILantaiChatSession>> = new Map();

  /**
   * Called when a temporary timestamp session id is resolved to a real backend
   * UUID. Consumers (e.g. Chat/index.tsx) can register here to update the URL.
   */
  onSessionIdResolved: ((tempId: string, realId: string) => void) | null = null;

  /**
   * Called after a session is removed. Consumers can register here to clear
   * the session id from the URL.
   */
  onSessionRemoved: ((removedId: string) => void) | null = null;

  /**
   * Called when a session is selected from the session list.
   * Consumers can register here to update the URL when switching sessions.
   */
  onSessionSelected:
    | ((sessionId: string | null | undefined, realId: string | null) => void)
    | null = null;

  /**
   * Called when a new session is created.
   * Consumers can register here to update the URL with the new session id.
   */
  onSessionCreated: ((sessionId: string) => void) | null = null;

  /**
   * User-initiated session selection. Passive history loads also call
   * getSession(), so routing must only be driven from explicit UI selection.
   */
  selectSession(sessionId: string | null | undefined): void {
    if (!sessionId) return;

    const session = this.findSessionByIdentifier(sessionId);
    const localSessionId = session?.id || sessionId;
    const realId = session?.realId || null;
    const targetId = realId || localSessionId;
    this.activateSession(localSessionId);
    if (targetId === this.lastSelectedSessionId) return;

    this.lastSelectedSessionId = targetId;
    this.onSessionSelected?.(localSessionId, realId);
  }

  /**
   * When reconnecting to a running conversation, the backend history may not
   * include the latest user message (it's only persisted after generation
   * completes). If generating, look up the cached text from sessionStorage
   * and patch it into the message list.
   *
   * When not generating the conversation is done — clear the cached entry.
   */
  private patchLastUserMessage(
    messages: ILantaiChatMessage[],
    generating: boolean,
    backendSessionIds: string | Array<string | null | undefined>,
    backendPendingText?: string | null,
  ): void {
    const acceptedUserMessages = loadAcceptedUserMessages(backendSessionIds);
    if (
      acceptedUserMessages.length > 0 &&
      !messages.some((message) => message.role === ROLE_USER)
    ) {
      const restoredMessages = acceptedUserMessages.map((cachedMessage) =>
        buildUserCard({
          id: cachedMessage.id,
          content: [{ type: "text", text: cachedMessage.text }],
          created_at: new Date(cachedMessage.createdAt).toISOString(),
          role: ROLE_USER,
          metadata: {
            client_turn_id: cachedMessage.clientTurnId || cachedMessage.id,
          },
        } as Message),
      );
      messages.splice(0, 0, ...restoredMessages);
    }

    const cachedText =
      (typeof backendPendingText === "string"
        ? backendPendingText.trim()
        : "") || loadPendingUserMessage(backendSessionIds);

    const hasCachedUserMessage = () =>
      Boolean(
        cachedText &&
        messages.some((message) => {
          if (message.role !== ROLE_USER) return false;
          const text = extractTextFromContent(
            message.cards?.[0]?.data?.input?.[0]?.content,
          );
          return text === cachedText;
        }),
      );
    const insertCachedUserMessage = () => {
      if (!cachedText || hasCachedUserMessage()) return;
      const userMessage = buildUserCard({
        content: [{ type: "text", text: cachedText }],
        role: ROLE_USER,
      } as Message);
      let insertionIndex = messages.length;
      while (
        insertionIndex > 0 &&
        messages[insertionIndex - 1]?.role !== ROLE_USER
      ) {
        insertionIndex -= 1;
      }
      messages.splice(insertionIndex, 0, userMessage);
    };

    if (!generating) {
      // The completed history can expose an assistant response before its
      // originating user record. Keep the accepted prompt visible first.
      insertCachedUserMessage();
      clearPendingUserMessage(backendSessionIds);
      return;
    }

    if (!cachedText) return;

    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.role === ROLE_USER) {
      const text = extractTextFromContent(
        lastMsg?.cards?.[0]?.data?.input?.[0]?.content,
      );
      if (!text) {
        lastMsg.cards = buildUserCard({
          content: [{ type: "text", text: cachedText }],
          role: ROLE_USER,
        } as Message).cards;
      }
      return;
    }
    insertCachedUserMessage();
  }

  private createEmptySession(sessionId: string): ExtendedSession {
    return {
      id: sessionId,
      name: DEFAULT_SESSION_NAME,
      sessionId,
      userId: DEFAULT_USER_ID,
      channel: DEFAULT_CHANNEL,
      messages: [],
      meta: {},
    } as ExtendedSession;
  }

  private updateWindowVariables(
    session: ExtendedSession,
    expectedAgentId = this.activeAgentId,
  ): void {
    if (
      getSelectedAgentId() !== expectedAgentId ||
      !this.selectionOwnsSession(session)
    ) {
      return;
    }
    window.currentSessionId = session.sessionId || "";
    window.currentUserId = session.userId || DEFAULT_USER_ID;
    window.currentChannel = session.channel || DEFAULT_CHANNEL;
    const active = updateActiveTopicWorkspaceFromMeta(
      getSelectedAgentId(),
      session.sessionId || session.id || "",
      session.meta,
      session.realId || session.id,
    );
    if (!active && session.sessionId) {
      setActiveTopicWorkspace(null);
    }
  }

  private getLocalSession(sessionId: string): ILantaiChatSession {
    const local = this.sessionList.find((s) => s.id === sessionId);
    if (local) return local;
    return this.createEmptySession(sessionId);
  }

  /**
   * Returns the real backend UUID for a session identified by id (which may be
   * a local timestamp). Returns null when not yet resolved or not found.
   */
  getRealIdForSession(sessionId: string): string | null {
    const s = this.findSessionByIdentifier(sessionId);
    return s?.realId ?? null;
  }

  private findSessionByIdentifier(
    sessionId: string | null | undefined,
  ): ExtendedSession | undefined {
    const targetId = typeof sessionId === "string" ? sessionId.trim() : "";
    if (!targetId) return undefined;
    const exact = this.sessionList.find((session) => session.id === targetId);
    if (exact) return exact as ExtendedSession;
    const realMatches = this.sessionList.filter(
      (session) => (session as ExtendedSession).realId === targetId,
    );
    if (realMatches.length === 1) return realMatches[0] as ExtendedSession;
    const aliasMatches = this.sessionList.filter(
      (session) => (session as ExtendedSession).sessionId === targetId,
    );
    return aliasMatches.length === 1
      ? (aliasMatches[0] as ExtendedSession)
      : undefined;
  }

  private findSessionIndexByIdentifier(
    sessionId: string | null | undefined,
  ): number {
    const targetId = typeof sessionId === "string" ? sessionId.trim() : "";
    if (!targetId) return -1;
    const exactIndex = this.sessionList.findIndex(
      (session) => session.id === targetId,
    );
    if (exactIndex > -1) return exactIndex;
    const realIndices = this.sessionList.flatMap((session, index) =>
      (session as ExtendedSession).realId === targetId ? [index] : [],
    );
    if (realIndices.length === 1) return realIndices[0];
    const aliasIndices = this.sessionList.flatMap((session, index) =>
      (session as ExtendedSession).sessionId === targetId ? [index] : [],
    );
    return aliasIndices.length === 1 ? aliasIndices[0] : -1;
  }

  /** Apply listChats to sessionList without joining chats by transport id. */
  private applyChatsToSessionList(chats: ChatSpec[]): ILantaiChatSession[] {
    const chatList = Array.isArray(chats) ? chats : [];
    const newList = chatList
      .filter((c) => c.id && c.id !== "undefined" && c.id !== "null")
      .map(chatSpecToSession)
      .reverse();

    const previousList = this.sessionList;
    this.sessionList = newList.map((s) => {
      const incoming = s as ExtendedSession;
      const authoritativeExisting = previousList.find((item) => {
        const existing = item as ExtendedSession;
        return item.id === incoming.id || existing.realId === incoming.id;
      }) as ExtendedSession | undefined;
      const existing = authoritativeExisting;
      if (!existing) return s;
      const next = { ...s } as ExtendedSession;
      if (existing.realId) {
        next.id = existing.id;
        next.realId = existing.realId;
      }
      const backendRunning = next.status === "running";
      next.generating = backendRunning;
      if (backendRunning) {
        if (existing.turnEventPreview !== undefined) {
          next.turnEventPreview = existing.turnEventPreview;
        }
        if (existing.runtimeState !== undefined) {
          next.runtimeState = existing.runtimeState;
        }
        if (existing.pendingUserMessage) {
          next.pendingUserMessage = existing.pendingUserMessage;
        }
      }
      const optimisticMessages = Array.isArray(existing.messages)
        ? existing.messages
        : [];
      if (optimisticMessages.length > 0) {
        next.messages = optimisticMessages;
      }
      return next as ILantaiChatSession;
    });
    if (this.preferredChatId) {
      const preferredId = this.preferredChatId;
      this.preferredChatId = null;
      const idx = this.sessionList.findIndex((s) => {
        const extended = s as ExtendedSession;
        return (
          s.id === preferredId ||
          extended.realId === preferredId ||
          extended.sessionId === preferredId
        );
      });
      if (idx > 0) {
        const [preferred] = this.sessionList.splice(idx, 1);
        this.sessionList.unshift(preferred);
      }
    }
    return [...this.sessionList];
  }

  async getSessionList(options: { force?: boolean } = {}) {
    const scope = this.captureAgentContext();
    if (this.sessionListRequest) return this.sessionListRequest;
    if (
      !options.force &&
      this.sessionList.length > 0 &&
      Date.now() - this.sessionListFetchedAt < SESSION_LIST_CACHE_TTL_MS
    ) {
      return [...this.sessionList];
    }

    let request: Promise<ILantaiChatSession[]> | null = null;
    request = (async () => {
      try {
        const chats = await api.listChats({
          mode: "all",
          include_archived: true,
        });
        if (!this.ownsAgentContext(scope)) return [];
        const sessions = this.applyChatsToSessionList(chats);
        this.sessionListFetchedAt = Date.now();
        return sessions;
      } finally {
        if (this.sessionListRequest === request) {
          this.sessionListRequest = null;
        }
      }
    })();
    this.sessionListRequest = request;

    return request;
  }

  /** Track the last session ID that triggered onSessionSelected to avoid duplicate calls. */
  private lastSelectedSessionId: string | null = null;

  async getSession(sessionId: string) {
    const scope = this.captureAgentContext();
    const existingRequest = this.sessionRequests.get(sessionId);
    if (existingRequest) return existingRequest;

    const requestPromise = this._doGetSession(sessionId, scope);
    this.sessionRequests.set(sessionId, requestPromise);

    try {
      const session = await requestPromise;
      this.assertOwnsAgentContext(scope);
      this.updateWindowVariables(session as ExtendedSession, scope.agentId);
      return session;
    } finally {
      if (this.sessionRequests.get(sessionId) === requestPromise) {
        this.sessionRequests.delete(sessionId);
      }
    }
  }

  private async _doGetSession(
    sessionId: string,
    scope: { agentId: string; generation: number },
  ): Promise<ILantaiChatSession> {
    // --- Local timestamp ID (New Chat before first reply) ---
    if (isLocalTimestamp(sessionId)) {
      const fromList = this.sessionList.find((s) => s.id === sessionId) as
        | ExtendedSession
        | undefined;

      // If realId is already resolved, use it directly to fetch history.
      if (fromList?.realId) {
        this.rememberSessionAlias(sessionId, fromList.realId);
        const chatHistory = await api.getChat(fromList.realId);
        this.assertOwnsAgentContext(scope);
        assertChatHistoryOwner(fromList.realId, chatHistory);
        hydrateQuquContinuations(chatHistory.ququ_continuations);
        const generating = isGenerating(chatHistory);
        const historyMessages = convertMessages(chatHistory.messages || []);
        this.patchLastUserMessage(
          historyMessages,
          generating,
          [sessionId, fromList.realId],
          chatHistory.pending_user_message || fromList.pendingUserMessage,
        );
        const messages = reconcileHydratedSessionMessages(
          fromList.messages,
          historyMessages,
          generating,
        );
        const session: ExtendedSession = {
          id: sessionId,
          name: chatHistory.name || fromList.name || DEFAULT_SESSION_NAME,
          sessionId: chatHistory.session_id || fromList.sessionId || sessionId,
          userId: chatHistory.user_id || fromList.userId || DEFAULT_USER_ID,
          channel: chatHistory.channel || fromList.channel || DEFAULT_CHANNEL,
          messages,
          meta: chatHistory.meta || fromList.meta || {},
          turnEventPreview: chatHistory.turn_event_preview ?? null,
          runtimeState: chatHistory.runtime_state ?? null,
          realId: fromList.realId,
          generating,
          status:
            (chatHistory.status as ChatStatus | undefined) || fromList.status,
          createdAt: chatHistory.created_at ?? fromList.createdAt,
          updatedAt: chatHistory.updated_at ?? fromList.updatedAt,
          pinned: chatHistory.pinned ?? fromList.pinned,
          archived: chatHistory.archived ?? fromList.archived,
          pendingUserMessage: generating
            ? fromList.pendingUserMessage
            : undefined,
        };
        return session;
      }

      // Pure local session (not yet sent to backend): wait until updateSession
      // resolves the realId, then fetch history with the real UUID.
      await this.waitForRealId(sessionId);
      this.assertOwnsAgentContext(scope);

      const refreshed = this.sessionList.find((s) => s.id === sessionId) as
        | ExtendedSession
        | undefined;
      if (refreshed?.realId) {
        this.rememberSessionAlias(sessionId, refreshed.realId);
        const chatHistory = await api.getChat(refreshed.realId);
        this.assertOwnsAgentContext(scope);
        assertChatHistoryOwner(refreshed.realId, chatHistory);
        hydrateQuquContinuations(chatHistory.ququ_continuations);
        const generating = isGenerating(chatHistory);
        const historyMessages = convertMessages(chatHistory.messages || []);
        this.patchLastUserMessage(
          historyMessages,
          generating,
          [sessionId, refreshed.realId],
          chatHistory.pending_user_message || refreshed.pendingUserMessage,
        );
        const messages = reconcileHydratedSessionMessages(
          refreshed.messages,
          historyMessages,
          generating,
        );
        const session: ExtendedSession = {
          id: sessionId,
          name: chatHistory.name || refreshed.name || DEFAULT_SESSION_NAME,
          sessionId: chatHistory.session_id || refreshed.sessionId || sessionId,
          userId: chatHistory.user_id || refreshed.userId || DEFAULT_USER_ID,
          channel: chatHistory.channel || refreshed.channel || DEFAULT_CHANNEL,
          messages,
          meta: chatHistory.meta || refreshed.meta || {},
          turnEventPreview: chatHistory.turn_event_preview ?? null,
          runtimeState: chatHistory.runtime_state ?? null,
          realId: refreshed.realId,
          generating,
          status:
            (chatHistory.status as ChatStatus | undefined) || refreshed.status,
          createdAt: chatHistory.created_at ?? refreshed.createdAt,
          updatedAt: chatHistory.updated_at ?? refreshed.updatedAt,
          pinned: chatHistory.pinned ?? refreshed.pinned,
          archived: chatHistory.archived ?? refreshed.archived,
          pendingUserMessage: generating
            ? refreshed.pendingUserMessage
            : undefined,
        };
        return session;
      }

      return this.getLocalSession(sessionId);
    }

    // --- No session selected (e.g. after delete) ---
    if (!sessionId || sessionId === "undefined" || sessionId === "null") {
      return this.createEmptySession(createLocalSessionId());
    }

    // --- Regular backend UUID ---
    const fromList = this.sessionList.find((s) => s.id === sessionId) as
      | ExtendedSession
      | undefined;

    const chatHistory = await api.getChat(sessionId);
    this.assertOwnsAgentContext(scope);
    assertChatHistoryOwner(sessionId, chatHistory);
    hydrateQuquContinuations(chatHistory.ququ_continuations);
    this.rememberSessionAlias(sessionId, chatHistory.chat_id);
    const generating = isGenerating(chatHistory);
    const historyMessages = convertMessages(chatHistory.messages || []);
    this.patchLastUserMessage(
      historyMessages,
      generating,
      [sessionId, chatHistory.chat_id],
      chatHistory.pending_user_message || fromList?.pendingUserMessage,
    );
    const messages = reconcileHydratedSessionMessages(
      fromList?.messages,
      historyMessages,
      generating,
    );
    const session: ExtendedSession = {
      id: sessionId,
      name: chatHistory.name || fromList?.name || sessionId,
      sessionId: chatHistory.session_id || fromList?.sessionId || sessionId,
      userId: chatHistory.user_id || fromList?.userId || DEFAULT_USER_ID,
      channel: chatHistory.channel || fromList?.channel || DEFAULT_CHANNEL,
      messages,
      meta: chatHistory.meta || fromList?.meta || {},
      turnEventPreview: chatHistory.turn_event_preview ?? null,
      runtimeState: chatHistory.runtime_state ?? null,
      generating,
      status:
        (chatHistory.status as ChatStatus | undefined) || fromList?.status,
      createdAt: chatHistory.created_at ?? fromList?.createdAt,
      updatedAt: chatHistory.updated_at ?? fromList?.updatedAt,
      pinned: chatHistory.pinned ?? fromList?.pinned,
      archived: chatHistory.archived ?? fromList?.archived,
      pendingUserMessage: generating ? fromList?.pendingUserMessage : undefined,
    };

    return session;
  }

  async updateSession(session: Partial<ILantaiChatSession>) {
    const targetId = typeof session.id === "string" ? session.id.trim() : "";
    const index = this.findSessionIndexByIdentifier(targetId);
    const preserveMessages = Boolean(
      (
        session as Partial<ILantaiChatSession> & {
          __preserveMessages?: boolean;
        }
      ).__preserveMessages,
    );
    const hasMessagePatch = Object.prototype.hasOwnProperty.call(
      session,
      "messages",
    );
    const buildSessionForList = (
      existingSession?: ILantaiChatSession,
    ): Partial<ILantaiChatSession> => {
      const sessionForList = {
        ...session,
        messages: preserveMessages
          ? mergePreservedSessionMessages(
              existingSession?.messages,
              session.messages,
            )
          : hasMessagePatch
            ? keepSessionListMessages(session.messages)
            : existingSession?.messages || [],
      };
      delete (sessionForList as Record<string, unknown>).__preserveMessages;
      return sessionForList;
    };

    const applyPatchToIndex = (targetIndex: number): void => {
      const existing = this.sessionList[targetIndex] as ExtendedSession;
      const sessionForList = buildSessionForList(
        existing,
      ) as Partial<ExtendedSession>;
      if (
        existing.id &&
        sessionForList.id &&
        sessionForList.id !== existing.id
      ) {
        sessionForList.id = existing.id;
      }
      if (existing.realId && !sessionForList.realId) {
        sessionForList.realId = existing.realId;
      }
      if (existing.sessionId && !sessionForList.sessionId) {
        sessionForList.sessionId = existing.sessionId;
      }
      if (existing.userId && !sessionForList.userId) {
        sessionForList.userId = existing.userId;
      }
      if (existing.channel && !sessionForList.channel) {
        sessionForList.channel = existing.channel;
      }
      if (existing.meta && !sessionForList.meta) {
        sessionForList.meta = existing.meta;
      }
      this.sessionList[targetIndex] = {
        ...existing,
        ...sessionForList,
      } as ILantaiChatSession;
    };

    if (index > -1) {
      applyPatchToIndex(index);

      const existing = this.sessionList[index] as ExtendedSession;
      if (isLocalTimestamp(existing.id) && !existing.realId) {
        const tempId = existing.id;
        this.getSessionList().then(() => {
          const { list, realId } = resolveRealId(this.sessionList, tempId);
          this.sessionList = list;
          if (realId) {
            this.rememberSessionAlias(tempId, realId);
            this.notifyRealIdResolved(tempId);
            this.onSessionIdResolved?.(tempId, realId);
          }
        });
      }
    } else {
      const tempId = targetId || session.id!;
      await this.getSessionList().then(() => {
        const refreshedIndex = this.findSessionIndexByIdentifier(tempId);
        if (refreshedIndex > -1) {
          applyPatchToIndex(refreshedIndex);
          return;
        }
        const { list, realId } = resolveRealId(this.sessionList, tempId);
        this.sessionList = list;
        if (realId) {
          this.rememberSessionAlias(tempId, realId);
          this.notifyRealIdResolved(tempId);
          this.onSessionIdResolved?.(tempId, realId);
          const realIndex = this.findSessionIndexByIdentifier(realId);
          if (realIndex > -1) {
            applyPatchToIndex(realIndex);
          }
          return;
        }
        if (!isLocalTimestamp(tempId)) {
          const sessionForList =
            buildSessionForList() as Partial<ExtendedSession>;
          this.sessionList.unshift({
            id: tempId,
            name: DEFAULT_SESSION_NAME,
            sessionId: tempId,
            userId: DEFAULT_USER_ID,
            channel: DEFAULT_CHANNEL,
            messages: [],
            meta: {},
            ...sessionForList,
          } as ExtendedSession);
        }
      });
    }

    return [...this.sessionList];
  }

  async createSession(session: Partial<ILantaiChatSession>) {
    const scope = this.captureAgentContext();
    session.id = createLocalSessionId();
    const requestedMeta = isRecord(session.meta) ? { ...session.meta } : {};
    const requestedKnowledgeSource = isRecord(requestedMeta.knowledge_source)
      ? requestedMeta.knowledge_source
      : null;
    const rawRequestedKnowledgeSources = requestedMeta.knowledge_sources;
    const hasRequestedKnowledgeSources = Array.isArray(
      rawRequestedKnowledgeSources,
    );
    const requestedKnowledgeSources = hasRequestedKnowledgeSources
      ? rawRequestedKnowledgeSources.filter(isRecord).slice(0, 20)
      : requestedKnowledgeSource
        ? [requestedKnowledgeSource]
        : [];

    const extended: ExtendedSession = {
      ...session,
      sessionId: session.id,
      userId: DEFAULT_USER_ID,
      channel: DEFAULT_CHANNEL,
      meta: requestedMeta,
      pendingUserMessage: pendingTextFromSessionName(session.name),
    } as ExtendedSession;

    const researchProjectId = requestedMeta.research_project_id;
    if (researchProjectId !== undefined) {
      if (typeof researchProjectId !== "string" || !researchProjectId.trim()) {
        throw new Error("研究项目关联无效");
      }
      // Research budget admission depends on durable server metadata. Do not
      // expose a sendable local draft before the server confirms the binding.
      const workspace = await api.ensureTopicWorkspace({
        session_id: extended.sessionId,
        user_id: extended.userId,
        channel: extended.channel,
        name: extended.name || DEFAULT_SESSION_NAME,
        research_project_id: researchProjectId,
      });
      if (!this.ownsAgentContext(scope)) return [...this.sessionList];
      if (workspace.chat.meta?.research_project_id !== researchProjectId) {
        throw new Error("研究项目关联未保存，请重试");
      }
      extended.realId = workspace.chat.id;
      extended.sessionId = workspace.chat.session_id || extended.sessionId;
      extended.meta = { ...requestedMeta, ...workspace.chat.meta };
      extended.status = workspace.chat.status ?? "idle";
      extended.createdAt = workspace.chat.created_at ?? null;
      extended.updatedAt = workspace.chat.updated_at ?? null;
      this.rememberSessionAlias(extended.id, workspace.chat.id, extended.sessionId);
    }

    // Opening a blank conversation is a local UI action. Put the draft into
    // the session list immediately; filesystem/workspace preparation is
    // allowed to finish in the background and is also idempotently ensured
    // before the first message is submitted.
    this.activateSession(extended.id);
    this.updateWindowVariables(extended);
    this.sessionList = [
      extended,
      ...this.sessionList.filter(
        (s) =>
          s.id !== extended.id &&
          (s as ExtendedSession).sessionId !== extended.sessionId,
      ),
    ];
    this.onSessionCreated?.(session.id);

    if (researchProjectId !== undefined) return [...this.sessionList];

    const localSessionId = String(extended.id || "");
    void api
      .ensureTopicWorkspace({
        session_id: extended.sessionId,
        user_id: extended.userId,
        channel: extended.channel,
        name:
          typeof extended.name === "string" && extended.name.trim()
            ? extended.name
            : DEFAULT_SESSION_NAME,
        ...(hasRequestedKnowledgeSources && requestedKnowledgeSources.length
          ? {
              knowledge_sources: requestedKnowledgeSources.map((source) => ({
                path: String(source.path || ""),
                filename: String(source.filename || ""),
                kind: String(source.kind || "") || undefined,
              })),
              knowledge_scope_mode:
                requestedMeta.knowledge_scope_mode === "expanded"
                  ? "expanded"
                  : "exact",
              ...(requestedKnowledgeSources.length === 1
                ? {
                    knowledge_source: {
                      path: String(requestedKnowledgeSources[0].path || ""),
                      filename: String(
                        requestedKnowledgeSources[0].filename || "",
                      ),
                      kind:
                        String(requestedKnowledgeSources[0].kind || "") ||
                        undefined,
                    },
                  }
                : {}),
            }
          : requestedKnowledgeSource
            ? {
                knowledge_source: {
                  path: String(requestedKnowledgeSource.path || ""),
                  filename: String(requestedKnowledgeSource.filename || ""),
                  kind:
                    String(requestedKnowledgeSource.kind || "") || undefined,
                },
              }
            : {}),
      })
      .then((workspace) => {
        if (!this.ownsAgentContext(scope)) return;
        const backendSessionId =
          workspace.chat.session_id || extended.sessionId;
        this.resolveEnsuredSession(
          [localSessionId, extended.sessionId],
          workspace.chat.id,
          backendSessionId,
        );
        const resolved = this.findSessionByIdentifier(localSessionId) as
          | ExtendedSession
          | undefined;
        if (!resolved) return;
        resolved.meta = {
          ...requestedMeta,
          ...(workspace.chat.meta || {}),
        };
        resolved.status = workspace.chat.status ?? "idle";
        resolved.createdAt = workspace.chat.created_at ?? null;
        resolved.updatedAt = workspace.chat.updated_at ?? null;
        if (this.selectionOwnsSession(resolved)) {
          updateActiveTopicWorkspaceFromMeta(
            scope.agentId,
            backendSessionId,
            resolved.meta,
            workspace.chat.id,
          );
          this.updateWindowVariables(resolved, scope.agentId);
        }
      })
      .catch((error) => {
        if (this.ownsAgentContext(scope)) {
          console.warn("Failed to create topic workspace:", error);
        }
      });

    return [...this.sessionList];
  }

  async branchSession(sessionId: string, userMessageId: string) {
    let source = this.findSessionByIdentifier(sessionId);
    if (source && isLocalTimestamp(source.id) && !source.realId) {
      await this.waitForRealId(source.id);
      source = this.findSessionByIdentifier(sessionId);
    }
    const sourceChatId =
      source?.realId ||
      (source?.id && !isLocalTimestamp(source.id) ? source.id : null);
    if (!sourceChatId) {
      throw new Error("当前会话尚未完成初始化，暂时无法编辑历史消息");
    }

    const history = await api.branchChat(sourceChatId, {
      user_message_id: userMessageId,
    });
    const branchChatId = history.chat_id?.trim();
    const branchSessionId = history.session_id?.trim();
    if (!branchChatId || !branchSessionId) {
      throw new Error("后端没有返回有效的分支会话");
    }
    hydrateQuquContinuations(history.ququ_continuations);

    const branch: ExtendedSession = {
      id: branchChatId,
      realId: branchChatId,
      name: history.name || `${source?.name || DEFAULT_SESSION_NAME} · 分支`,
      sessionId: branchSessionId,
      userId: history.user_id || source?.userId || DEFAULT_USER_ID,
      channel: history.channel || source?.channel || DEFAULT_CHANNEL,
      messages: convertMessages(history.messages || []),
      meta: history.meta || source?.meta || {},
      status: history.status || "idle",
      generating: false,
      createdAt: history.created_at ?? null,
      updatedAt: history.updated_at ?? null,
      pinned: history.pinned ?? false,
      archived: history.archived ?? false,
      turnEventPreview: null,
      runtimeState: null,
    };
    this.rememberSessionAlias(branch.id, branch.realId);
    this.sessionList = [
      branch,
      ...this.sessionList.filter(
        (item) =>
          item.id !== branch.id &&
          (item as ExtendedSession).realId !== branch.realId &&
          (item as ExtendedSession).sessionId !== branch.sessionId,
      ),
    ];
    this.selectSession(branch.id);
    this.updateWindowVariables(branch);
    return [...this.sessionList];
  }

  async recoverLegacyHistory(sessionId: string) {
    let source = this.findSessionByIdentifier(sessionId);
    if (source && isLocalTimestamp(source.id) && !source.realId) {
      await this.waitForRealId(source.id);
      source = this.findSessionByIdentifier(sessionId);
    }
    const sourceChatId =
      source?.realId ||
      (source?.id && !isLocalTimestamp(source.id) ? source.id : null);
    if (!sourceChatId) {
      throw new Error("当前会话尚未完成初始化，暂时无法恢复历史");
    }

    const history = await api.recoverLegacyHistory(sourceChatId);
    const recoveredChatId = history.chat_id?.trim();
    const recoveredSessionId = history.session_id?.trim();
    if (!recoveredChatId || !recoveredSessionId) {
      throw new Error("后端没有返回有效的恢复会话");
    }
    hydrateQuquContinuations(history.ququ_continuations);

    const recovered: ExtendedSession = {
      id: recoveredChatId,
      realId: recoveredChatId,
      name: history.name || `${source?.name || "旧会话"} · 恢复副本`,
      sessionId: recoveredSessionId,
      userId: history.user_id || source?.userId || DEFAULT_USER_ID,
      channel: history.channel || source?.channel || DEFAULT_CHANNEL,
      messages: convertMessages(history.messages || []),
      meta: history.meta || {},
      status: history.status || "idle",
      generating: false,
      createdAt: history.created_at ?? null,
      updatedAt: history.updated_at ?? null,
      pinned: history.pinned ?? false,
      archived: history.archived ?? false,
      turnEventPreview: null,
      runtimeState: null,
    };
    this.rememberSessionAlias(recovered.id, recovered.realId);
    this.sessionList = [
      recovered,
      ...this.sessionList.filter(
        (item) =>
          item.id !== recovered.id &&
          (item as ExtendedSession).realId !== recovered.realId &&
          (item as ExtendedSession).sessionId !== recovered.sessionId,
      ),
    ];
    this.selectSession(recovered.id);
    this.updateWindowVariables(recovered);
    return [...this.sessionList];
  }

  async removeSession(session: Partial<ILantaiChatSession>) {
    if (!session.id) return [...this.sessionList];

    const { id: sessionId } = session;

    const existing = this.sessionList.find((s) => s.id === sessionId) as
      | ExtendedSession
      | undefined;

    const deleteId =
      existing?.realId ?? (isLocalTimestamp(sessionId) ? null : sessionId);

    if (deleteId) await api.deleteChat(deleteId);

    this.sessionList = this.sessionList.filter((s) => s.id !== sessionId);

    const sessionAliases = mergeSessionAliases([
      sessionId,
      existing?.id,
      existing?.realId,
      existing?.sessionId,
    ]);
    sessionAliases.forEach((id) => {
      clearPendingUserMessage(id);
      clearAcceptedUserMessages(id);
      clearSessionDraft(id);
      clearSessionDraftAttachments(id);
      try {
        sessionStorage.removeItem(`${ALIAS_PREFIX}${id}`);
      } catch {
        /* ignore unavailable storage */
      }
    });

    const resolvedId = existing?.realId ?? sessionId;
    if (existing && this.selectionOwnsSession(existing)) {
      this.activateSession(null);
    }
    this.onSessionRemoved?.(resolvedId);

    return [...this.sessionList];
  }
}

export default new SessionApi();
