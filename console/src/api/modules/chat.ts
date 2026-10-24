import { request } from "../request";
import { getApiUrl, getApiToken } from "../config";
import { buildAuthHeaders, encodeHeaderValue } from "../authHeaders";
import type {
  ChatSpec,
  ChatBranchRequest,
  ChatActivity,
  ChatSearchHit,
  ChatHistory,
  ChatContextStats,
  ChatDeleteResponse,
  ChatTurnInterruptResponse,
  ChatTurnRetryResponse,
  ChatUpdateRequest,
  ChatWorkspaceRequest,
  ChatWorkspaceResponse,
  RuntimeEventsResponse,
  QuquContinuationRequest,
  QuquContinuationResponse,
  Session,
} from "../types";

/** Response from POST /console/upload. url = filename only; agent_id from header. */
export interface ChatUploadResponse {
  url: string;
  file_name: string;
  stored_name?: string;
  size: number;
  digest: string;
  version: number;
  upload_receipt: string;
}

export interface AnalysisChartResponse {
  analysis_id: string;
  dataset_id: string;
  source_name: string;
  source_sha256: string;
  source_row_count: number;
  sheet_name: string | null;
  analysis_plan: Record<string, unknown>;
  calculated_at: string;
  source_status: "current" | "changed" | "unavailable";
  semantic_review: "pending" | "passed" | "failed";
  warnings: string[];
  x_column: string;
  y_column: string;
  points: Array<{ label: string; value: string }>;
}

export interface ChatAttachmentUploadSuccess {
  url: string;
  canonicalPath: string;
  storedName?: string;
  digest: string;
  immutableVersion: number;
  uploadReceipt: string;
}

export interface StopChatResponse {
  stopped: boolean;
}

export type ChatEventsTransportStatus =
  "connecting" | "connected" | "reconnecting" | "closed";

export interface ChatEventsTransportDetail {
  message?: string;
  attempts?: number;
  error?: unknown;
}

export interface ChatEventsSubscriptionOptions {
  agentId?: string;
  afterSeq?: number;
  streamEpoch?: string;
  reconnectMs?: number;
  maxReconnectAttempts?: number;
  onError?: (error: unknown) => void;
  onTransportStatus?: (
    status: ChatEventsTransportStatus,
    detail?: ChatEventsTransportDetail,
  ) => void;
}

export type ChatEventsUpdateListener = (
  response: RuntimeEventsResponse,
) => boolean | void;

const FILES_PREVIEW = "/files/preview";
const INITIAL_WORKSPACE_STREAM_RETRY_MS = 100;
const INITIAL_WORKSPACE_STREAM_RETRY_WINDOW_MS = 5000;

type RuntimeEventStreamHttpError = Error & { status: number };

function runtimeEventStreamHttpError(
  status: number,
): RuntimeEventStreamHttpError {
  const error = new Error(
    `Runtime event stream failed: ${status}`,
  ) as RuntimeEventStreamHttpError;
  error.status = status;
  return error;
}

function isInitialWorkspaceStreamRace(
  error: unknown,
  options: { hasConnected: boolean; deadline: number },
): boolean {
  return (
    !options.hasConnected &&
    Date.now() < options.deadline &&
    typeof error === "object" &&
    error !== null &&
    (error as Partial<RuntimeEventStreamHttpError>).status === 404
  );
}

export function parseRuntimeEventStreamFrames(
  buffer: string,
  options: { flush?: boolean } = {},
): { payloads: string[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const chunks = normalized.split("\n\n");
  const completeChunks = options.flush ? chunks : chunks.slice(0, -1);
  const rest = options.flush ? "" : chunks[chunks.length - 1] || "";
  const payloads = completeChunks
    .map((chunk) => {
      const dataLines = chunk
        .split("\n")
        .map((line) => line.replace(/\r$/, ""))
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.replace(/^data:\s?/, ""));
      return dataLines.length ? dataLines.join("\n") : "";
    })
    .filter(Boolean);
  return { payloads, rest };
}

export const chatApi = {
  continueQuqu: (payload: QuquContinuationRequest) =>
    request<QuquContinuationResponse>("/console/ququ/continue", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** Upload a file for chat attachment. Returns URL path for content. */
  uploadFile: async (
    file: File,
    options: { signal?: AbortSignal } = {},
  ): Promise<ChatUploadResponse> => {
    const desktopUpload =
      typeof window !== "undefined"
        ? window.electronAPI?.uploadChatAttachment
        : undefined;
    if (typeof desktopUpload === "function") {
      if (options.signal?.aborted) {
        throw (
          options.signal.reason || new DOMException("Aborted", "AbortError")
        );
      }
      const authHeaders = buildAuthHeaders();
      const result = await desktopUpload({
        fileName: file.name,
        mimeType: file.type || "application/octet-stream",
        bytes: await file.arrayBuffer(),
        agentId: authHeaders["X-Agent-Id"],
        workspaceGrantId: authHeaders["X-Workspace-Grant-Id"],
      });
      if (options.signal?.aborted) {
        throw (
          options.signal.reason || new DOMException("Aborted", "AbortError")
        );
      }
      if (!result?.ok || !result.url) {
        const errorMessages: Record<string, string> = {
          backend_unavailable: "附件服务尚未就绪，请稍后重试",
          file_too_large: "附件超过 150MB，无法上传",
          invalid_attachment_bytes: "无法读取附件内容，请重新选择文件",
          upload_timeout: "附件上传超时，请重试",
        };
        const error = new Error(
          errorMessages[result?.error_code || ""] ||
            result?.error ||
            "附件上传失败，请重试",
        );
        error.name =
          result?.error_code === "upload_timeout"
            ? "TimeoutError"
            : "UploadError";
        throw error;
      }
      return {
        url: result.url,
        file_name: result.file_name || file.name,
        stored_name: result.stored_name,
        size: Number(result.size ?? file.size),
        digest: String(result.digest || ""),
        version: Number(result.version || 0),
        upload_receipt: String(result.upload_receipt || ""),
      };
    }

    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(getApiUrl("/console/upload"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
      signal: options.signal,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(
        `Upload failed: ${response.status} ${response.statusText}${
          text ? ` - ${text}` : ""
        }`,
      );
    }
    return response.json();
  },

  filePreviewUrl: (filename: string): string => {
    if (!filename) return "";
    if (filename.startsWith("http://") || filename.startsWith("https://"))
      return filename;
    const cleaned = filename.replace(/^\/+/, "");
    const path = `${FILES_PREVIEW}/${cleaned}`;
    const url = getApiUrl(path);

    const token = getApiToken();
    if (token) {
      return `${url}?token=${encodeURIComponent(token)}`;
    }

    return url;
  },
  listChats: (params?: {
    user_id?: string;
    channel?: string;
    mode?: "all" | "chat" | "coding";
    include_archived?: boolean;
    include_blank?: boolean;
  }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    if (params?.mode) searchParams.append("mode", params.mode);
    if (params?.include_archived)
      searchParams.append("include_archived", "true");
    if (params?.include_blank) searchParams.append("include_blank", "true");
    const query = searchParams.toString();
    return request<ChatSpec[]>(`/chats${query ? `?${query}` : ""}`);
  },

  listChatActivity: () => request<ChatActivity[]>("/chats/activity"),

  searchChats: (query: string, limit = 60) =>
    request<ChatSearchHit[]>(
      `/chats/search?q=${encodeURIComponent(query)}&limit=${limit}`,
    ),

  createChat: (chat: Partial<ChatSpec>) =>
    request<ChatSpec>("/chats", {
      method: "POST",
      body: JSON.stringify(chat),
    }),

  getChat: (chatId: string) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(chatId)}`),

  getAnalysisChart: (
    chatId: string,
    analysisId: string,
    xColumn: string,
    yColumn: string,
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams({ x_column: xColumn, y_column: yColumn });
    return request<AnalysisChartResponse>(
      `/chats/${encodeURIComponent(chatId)}/analysis/${encodeURIComponent(analysisId)}/chart?${query.toString()}`,
      { signal },
    );
  },

  branchChat: (chatId: string, payload: ChatBranchRequest) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(chatId)}/branch`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  recoverLegacyHistory: (chatId: string) =>
    request<ChatHistory>(
      `/chats/${encodeURIComponent(chatId)}/legacy-history-recovery`,
      { method: "POST" },
    ),

  getChatContextStats: (chatId: string) =>
    request<ChatContextStats>(
      `/chats/${encodeURIComponent(chatId)}/context-stats`,
    ),

  getChatEvents: (
    chatId: string,
    afterSeq = 0,
    params?: {
      limit?: number;
      eventTypes?: string[];
      signal?: AbortSignal;
    },
  ) => {
    const query = new URLSearchParams();
    query.set("after_seq", String(afterSeq));
    if (params?.limit) query.set("limit", String(params.limit));
    for (const eventType of params?.eventTypes || []) {
      if (eventType.trim()) query.append("event_type", eventType.trim());
    }
    const path = `/chats/${encodeURIComponent(chatId)}/events?${query.toString()}`;
    return params?.signal
      ? request<RuntimeEventsResponse>(path, { signal: params.signal })
      : request<RuntimeEventsResponse>(path);
  },

  subscribeChatEvents: (
    chatId: string,
    onUpdate: ChatEventsUpdateListener,
    options?: ChatEventsSubscriptionOptions,
  ) => subscribeChatEvents(chatId, onUpdate, options),

  interruptTurn: (chatId: string, turnId: string) =>
    request<ChatTurnInterruptResponse>(
      `/chats/${encodeURIComponent(chatId)}/turns/${encodeURIComponent(
        turnId,
      )}/interrupt`,
      { method: "POST" },
    ),

  retryTurn: (chatId: string, turnId: string) =>
    request<ChatTurnRetryResponse>(
      `/chats/${encodeURIComponent(chatId)}/turns/${encodeURIComponent(
        turnId,
      )}/retry`,
      { method: "POST" },
    ),

  updateChat: (chatId: string, chat: ChatUpdateRequest) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}`, {
      method: "PUT",
      body: JSON.stringify(chat),
    }),

  ensureTopicWorkspace: (payload: ChatWorkspaceRequest) =>
    request<ChatWorkspaceResponse>("/chats/topic-workspace", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  deleteChat: (chatId: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(chatId)}`, {
      method: "DELETE",
    }),

  batchDeleteChats: (chatIds: string[]) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(chatIds),
      },
    ),

  stopChat: (chatId: string, turnId?: string | null) => {
    const query = new URLSearchParams({ chat_id: chatId });
    if (turnId?.trim()) query.set("turn_id", turnId.trim());
    return request<StopChatResponse>(`/console/chat/stop?${query.toString()}`, {
      method: "POST",
    });
  },
};

/** Project the authoritative upload receipt into the chat vendor callback. */
export function chatUploadResponseToAttachmentSuccess(
  response: ChatUploadResponse,
): ChatAttachmentUploadSuccess {
  return {
    url: chatApi.filePreviewUrl(response.url),
    canonicalPath: response.url,
    storedName: response.stored_name,
    digest: response.digest,
    immutableVersion: response.version,
    uploadReceipt: response.upload_receipt,
  };
}

/**
 * Replay existing RuntimeEvent rows after `afterSeq`, then keep tailing the
 * current chat event stream. Uses fetch streaming so desktop auth headers work.
 */
export function subscribeChatEvents(
  chatId: string,
  onUpdate: ChatEventsUpdateListener,
  options: ChatEventsSubscriptionOptions = {},
): () => void {
  let aborted = false;
  let cursor = options.afterSeq ?? 0;
  let streamEpoch = options.streamEpoch?.trim() || "";
  let controller: AbortController | null = null;
  const reconnectMs = options.reconnectMs ?? 2000;
  const maxReconnectAttempts = options.maxReconnectAttempts ?? Infinity;
  let reconnectAttempts = 0;
  let hasConnected = false;
  const initialWorkspaceRetryDeadline =
    Date.now() + INITIAL_WORKSPACE_STREAM_RETRY_WINDOW_MS;
  let seenEvents = new Set<string>();

  const transportDetail = (
    error: unknown,
    attempts = reconnectAttempts,
  ): ChatEventsTransportDetail => {
    const message =
      error instanceof Error && error.message.trim()
        ? error.message.trim()
        : typeof error === "string" && error.trim()
          ? error.trim()
          : "Runtime event stream disconnected";
    return { message, attempts, error };
  };

  const emitTransportStatus = (
    status: ChatEventsTransportStatus,
    detail?: ChatEventsTransportDetail,
  ) => {
    options.onTransportStatus?.(status, detail);
  };

  const payloadErrorMessage = (payload: unknown): string => {
    if (!payload || typeof payload !== "object") {
      return "Malformed runtime event stream payload";
    }
    const record = payload as Record<string, unknown>;
    const detail = record.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    const message = record.message;
    if (typeof message === "string" && message.trim()) return message.trim();
    const error = record.error;
    if (typeof error === "string" && error.trim()) return error.trim();
    if (error && typeof error === "object") {
      const nested = (error as Record<string, unknown>).message;
      if (typeof nested === "string" && nested.trim()) return nested.trim();
    }
    return "Runtime event stream payload missing events";
  };

  const handlePayload = (payload: unknown) => {
    if (!payload || typeof payload !== "object") {
      options.onError?.(new Error(payloadErrorMessage(payload)));
      return;
    }
    const response = payload as RuntimeEventsResponse;
    if (!Array.isArray(response.events)) {
      options.onError?.(new Error(payloadErrorMessage(payload)));
      return;
    }
    if (String(response.chat_id || "").trim() !== chatId.trim()) {
      options.onError?.(
        new Error("Runtime event stream returned a different chat"),
      );
      return;
    }
    const nextSeenEvents = response.resync_required
      ? new Set<string>()
      : new Set(seenEvents);
    let nextCursor = response.resync_required ? 0 : cursor;
    let nextStreamEpoch = streamEpoch;
    if (typeof response.stream_epoch === "string") {
      nextStreamEpoch = response.stream_epoch.trim();
    }
    if (typeof response.latest_seq === "number") {
      nextCursor = Math.max(nextCursor, response.latest_seq);
    } else {
      for (const event of response.events) {
        if (typeof event.seq === "number") {
          nextCursor = Math.max(nextCursor, event.seq);
        }
      }
    }
    const events = response.events.filter((event) => {
      const key =
        typeof event.id === "string" && event.id.trim()
          ? `id:${event.id}`
          : typeof event.seq === "number"
            ? `seq:${event.seq}`
            : "";
      if (!key) return true;
      if (nextSeenEvents.has(key)) return false;
      nextSeenEvents.add(key);
      return true;
    });
    if (response.events.length > 0 && events.length === 0) {
      cursor = nextCursor;
      streamEpoch = nextStreamEpoch;
      seenEvents = nextSeenEvents;
      return;
    }
    const applied = onUpdate({ ...response, events });
    if (applied === false) {
      throw new Error("Runtime event response was not applied");
    }
    cursor = nextCursor;
    streamEpoch = nextStreamEpoch;
    seenEvents = nextSeenEvents;
  };

  async function connect() {
    while (!aborted) {
      controller = new AbortController();
      emitTransportStatus(
        reconnectAttempts === 0 ? "connecting" : "reconnecting",
      );
      try {
        const query = new URLSearchParams();
        query.set("after_seq", String(cursor));
        if (streamEpoch) query.set("stream_epoch", streamEpoch);
        const url = getApiUrl(
          `/chats/${encodeURIComponent(chatId)}/events/stream?${query.toString()}`,
        );
        const headers = buildAuthHeaders();
        if (options.agentId?.trim()) {
          headers["X-Agent-Id"] = encodeHeaderValue(options.agentId);
        }
        if (streamEpoch) headers["Last-Event-ID"] = `${streamEpoch}:${cursor}`;
        const response = await fetch(url, {
          headers,
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          throw runtimeEventStreamHttpError(response.status);
        }

        hasConnected = true;
        reconnectAttempts = 0;
        emitTransportStatus("connected");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        const handleRawPayload = (raw: string) => {
          let payload: unknown;
          try {
            payload = JSON.parse(raw);
          } catch (error) {
            options.onError?.(
              error instanceof Error
                ? error
                : new Error("Malformed runtime event stream frame"),
            );
            return;
          }
          // State-application failures must escape to the reconnect loop so
          // the transport resumes from the last applied cursor.
          handlePayload(payload);
        };

        while (!aborted) {
          const { done, value } = await reader.read();
          if (done) {
            buffer += decoder.decode();
            const parsed = parseRuntimeEventStreamFrames(buffer, {
              flush: true,
            });
            buffer = parsed.rest;
            for (const raw of parsed.payloads) {
              handleRawPayload(raw);
            }
            break;
          }

          buffer += decoder.decode(value, { stream: true });
          const parsed = parseRuntimeEventStreamFrames(buffer);
          buffer = parsed.rest;
          for (const raw of parsed.payloads) {
            handleRawPayload(raw);
          }
        }
        if (!aborted) {
          emitTransportStatus("reconnecting");
          await new Promise((resolve) =>
            window.setTimeout(resolve, reconnectMs),
          );
        }
      } catch (error) {
        if (aborted) return;
        controller?.abort();
        if (
          isInitialWorkspaceStreamRace(error, {
            hasConnected,
            deadline: initialWorkspaceRetryDeadline,
          })
        ) {
          await new Promise((resolve) =>
            window.setTimeout(resolve, INITIAL_WORKSPACE_STREAM_RETRY_MS),
          );
          continue;
        }
        reconnectAttempts += 1;
        options.onError?.(error);
        emitTransportStatus(
          "reconnecting",
          transportDetail(error, reconnectAttempts),
        );
        if (reconnectAttempts >= maxReconnectAttempts) {
          emitTransportStatus(
            "closed",
            transportDetail(error, reconnectAttempts),
          );
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, reconnectMs));
      }
    }
  }

  void connect();

  return () => {
    aborted = true;
    controller?.abort();
  };
}

export const sessionApi = {
  listSessions: (params?: { user_id?: string; channel?: string }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    const query = searchParams.toString();
    return request<Session[]>(`/chats${query ? `?${query}` : ""}`);
  },

  getSession: (sessionId: string) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(sessionId)}`),

  deleteSession: (sessionId: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),

  createSession: (session: Partial<Session>) =>
    request<Session>("/chats", {
      method: "POST",
      body: JSON.stringify(session),
    }),

  updateSession: (sessionId: string, session: ChatUpdateRequest) =>
    request<Session>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "PUT",
      body: JSON.stringify(session),
    }),

  batchDeleteSessions: (sessionIds: string[]) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(sessionIds),
      },
    ),
};
