import { chatApi } from "../../api/modules/chat";
import type {
  ChatEventsSubscriptionOptions,
  ChatEventsTransportDetail,
  ChatEventsTransportStatus,
} from "../../api/modules/chat";
import type { RuntimeEventsResponse } from "../../api/types/chat";

type RuntimeUpdateListener = (
  response: RuntimeEventsResponse,
) => boolean | void;

export type RuntimeEventSubscriberRole = "state-applier" | "observer";

export interface RuntimeEventSubscriptionOptions extends ChatEventsSubscriptionOptions {
  role?: RuntimeEventSubscriberRole;
}

interface RuntimeEventSubscriber {
  onUpdate: RuntimeUpdateListener;
  role: RuntimeEventSubscriberRole;
  onError?: (error: unknown) => void;
  onTransportStatus?: (
    status: ChatEventsTransportStatus,
    detail?: ChatEventsTransportDetail,
  ) => void;
}

interface RuntimeEventSubscriptionEntry {
  key: string;
  chatId: string;
  subscribers: Map<symbol, RuntimeEventSubscriber>;
  closeTransport: () => void;
  closeTimer: ReturnType<typeof setTimeout> | null;
  lastResponse: RuntimeEventsResponse | null;
}

interface RuntimeEventStreamCheckpoint {
  streamEpoch: string;
  latestSeq: number;
}

const CLOSE_GRACE_MS = 250;
const subscriptions = new Map<string, RuntimeEventSubscriptionEntry>();
// The backend owns the running turn; this renderer registry only owns a
// detachable observation cursor. Keep that cursor after the last React view
// unsubscribes so a settings/session detour resumes from the last delivered
// event instead of replaying the ledger from zero.
const checkpoints = new Map<string, RuntimeEventStreamCheckpoint>();

function responseLatestSeq(response: RuntimeEventsResponse): number {
  if (Number.isFinite(response.latest_seq)) return response.latest_seq;
  return response.events.reduce(
    (latest, event) =>
      typeof event.seq === "number" ? Math.max(latest, event.seq) : latest,
    0,
  );
}

function updateCheckpoint(key: string, response: RuntimeEventsResponse): void {
  const existing = checkpoints.get(key);
  const responseEpoch = String(response.stream_epoch || "").trim();
  const streamEpoch = responseEpoch || existing?.streamEpoch || "";
  const latestSeq = responseLatestSeq(response);
  const sameLedger =
    !existing ||
    !existing.streamEpoch ||
    !streamEpoch ||
    existing.streamEpoch === streamEpoch;
  checkpoints.set(key, {
    streamEpoch,
    latestSeq: sameLedger
      ? Math.max(existing?.latestSeq || 0, latestSeq)
      : latestSeq,
  });
}

function resumeOptions(
  key: string,
  options: ChatEventsSubscriptionOptions,
): ChatEventsSubscriptionOptions {
  const checkpoint = checkpoints.get(key);
  if (!checkpoint) return options;

  const requestedEpoch = String(options.streamEpoch || "").trim();
  if (
    requestedEpoch &&
    checkpoint.streamEpoch &&
    requestedEpoch !== checkpoint.streamEpoch
  ) {
    return options;
  }

  return {
    ...options,
    afterSeq: Math.max(options.afterSeq || 0, checkpoint.latestSeq),
    streamEpoch: requestedEpoch || checkpoint.streamEpoch,
  };
}

function closeEntry(entry: RuntimeEventSubscriptionEntry): void {
  if (entry.closeTimer) {
    clearTimeout(entry.closeTimer);
    entry.closeTimer = null;
  }
  entry.closeTransport();
  if (subscriptions.get(entry.key) === entry) {
    subscriptions.delete(entry.key);
  }
}

function createEntry(
  key: string,
  chatId: string,
  options: ChatEventsSubscriptionOptions,
): RuntimeEventSubscriptionEntry {
  const entry: RuntimeEventSubscriptionEntry = {
    key,
    chatId,
    subscribers: new Map(),
    closeTransport: () => {},
    closeTimer: null,
    lastResponse: null,
  };
  entry.closeTransport = chatApi.subscribeChatEvents(
    chatId,
    (response) => {
      if (response.chat_id.trim() !== entry.chatId) return true;
      const stateAppliers = [...entry.subscribers.values()].filter(
        (subscriber) => subscriber.role === "state-applier",
      );
      for (const subscriber of stateAppliers) {
        const applied = subscriber.onUpdate(response);
        if (applied === false) return false;
      }

      // This checkpoint is the applied cursor. Presentation observers run
      // only after the central state projection accepted the whole batch.
      updateCheckpoint(key, response);
      entry.lastResponse = response;
      for (const subscriber of entry.subscribers.values()) {
        if (subscriber.role !== "observer") continue;
        try {
          subscriber.onUpdate(response);
        } catch (error) {
          subscriber.onError?.(error);
        }
      }
      return true;
    },
    {
      ...resumeOptions(key, options),
      onError: (error) => {
        for (const subscriber of entry.subscribers.values()) {
          subscriber.onError?.(error);
        }
      },
      onTransportStatus: (status, detail) => {
        for (const subscriber of entry.subscribers.values()) {
          subscriber.onTransportStatus?.(status, detail);
        }
      },
    },
  );
  subscriptions.set(key, entry);
  return entry;
}

export function subscribeRuntimeEventSession(
  chatId: string,
  onUpdate: RuntimeUpdateListener,
  options: RuntimeEventSubscriptionOptions = {},
): () => void {
  const normalizedChatId = chatId.trim();
  if (!normalizedChatId) return () => {};
  const { role = "state-applier", ...transportOptions } = options;
  const key = `${transportOptions.agentId?.trim() || "default"}:${normalizedChatId}`;
  const entry =
    subscriptions.get(key) ||
    createEntry(key, normalizedChatId, transportOptions);
  if (entry.closeTimer) {
    clearTimeout(entry.closeTimer);
    entry.closeTimer = null;
  }

  const token = Symbol(normalizedChatId);
  entry.subscribers.set(token, {
    onUpdate,
    role,
    onError: transportOptions.onError,
    onTransportStatus: transportOptions.onTransportStatus,
  });
  if (entry.lastResponse) {
    try {
      onUpdate(entry.lastResponse);
    } catch (error) {
      transportOptions.onError?.(error);
    }
  }

  let released = false;
  return () => {
    if (released) return;
    released = true;
    entry.subscribers.delete(token);
    if (entry.subscribers.size > 0 || entry.closeTimer) return;
    entry.closeTimer = setTimeout(() => {
      entry.closeTimer = null;
      if (entry.subscribers.size === 0) closeEntry(entry);
    }, CLOSE_GRACE_MS);
  };
}

export function resetRuntimeEventSubscriptionRegistryForTests(): void {
  for (const entry of subscriptions.values()) closeEntry(entry);
  subscriptions.clear();
  checkpoints.clear();
}
