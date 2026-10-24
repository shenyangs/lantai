import type {
  ChatRuntimeState,
  ChatTurnEventPreview,
  ChatTurnEventPreviewEvent,
  RuntimeEvent,
  RuntimeEventsResponse,
} from "../../api/types/chat";
import type { ILantaiChatMessage } from "../../vendor/lantaiChat";
import { LANTAI_RESPONSE_CARD_CODE } from "./chatCardCodes";

const MAX_STREAM_PREVIEW_EVENTS = 32;
const MAX_RESPONSE_PROJECTION_EVENTS = 80;
const ROLE_ASSISTANT = "assistant";

type ChatUiMessage = ILantaiChatMessage;

type RuntimeOutputMessage = Record<string, unknown> & {
  id?: string;
  role?: string;
  type?: string;
  status?: string;
  content?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
};

function stringField(
  source: Record<string, unknown>,
  ...keys: string[]
): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
  }
  return "";
}

function numberField(
  source: Record<string, unknown>,
  ...keys: string[]
): number | null {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function runtimeEventKind(event: RuntimeEvent): string {
  return stringField(event.payload || {}, "legacy_kind") || event.type || "";
}

export function isNativeMessageStreamEvent(event: RuntimeEvent): boolean {
  const kind = runtimeEventKind(event);
  return (
    event.type === "message.started" ||
    event.type === "message.delta" ||
    event.type === "message.completed" ||
    kind === "chat.message.started" ||
    kind === "chat.message.delta" ||
    kind === "chat.message.completed"
  );
}

export function isNativeToolLifecycleEvent(event: RuntimeEvent): boolean {
  const type = String(event.type || "").toLowerCase();
  const kind = runtimeEventKind(event).toLowerCase();
  return (
    type === "tool.call.started" ||
    type === "tool.call.delta" ||
    type === "tool.call.completed" ||
    kind === "chat.tool_call.started" ||
    kind === "chat.tool_call.delta" ||
    kind === "chat.tool_call.finished" ||
    kind === "chat.tool_call.completed"
  );
}

function runtimeEventLabel(event: RuntimeEvent): string {
  const payload = event.payload || {};
  return (
    stringField(payload, "trace_title", "label", "summary") ||
    event.type ||
    "运行事件"
  );
}

function runtimeEventDetail(event: RuntimeEvent): string {
  const payload = event.payload || {};
  return stringField(payload, "trace_detail", "detail", "summary");
}

const TERMINAL_RUNTIME_STATUSES = new Set([
  "completed",
  "failed",
  "interrupted",
  "stalled",
]);

function runtimeEventRunId(event: RuntimeEvent): string {
  return event.turn_id || stringField(event.payload || {}, "turn_id", "run_id");
}

function runtimeEventParentRunId(event: RuntimeEvent): string {
  const payload = event.payload || {};
  const subRun = recordField(payload, "sub_run");
  return (
    stringField(payload, "parent_run_id", "source_run_id") ||
    stringField(subRun, "parent_run_id")
  );
}

function runtimeEventGroupRunId(event: RuntimeEvent): string {
  return runtimeEventParentRunId(event) || runtimeEventRunId(event);
}

function runtimeEventTimeMs(event: RuntimeEvent): number {
  const parsed = Date.parse(event.created_at || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function isForegroundTurnBoundary(event: RuntimeEvent): boolean {
  if (!isTurnStartOrContinuationEvent(event)) return false;
  if (runtimeEventParentRunId(event)) return false;
  const directRunId = runtimeEventRunId(event);
  const groupRunId = runtimeEventGroupRunId(event);
  return !directRunId || !groupRunId || directRunId === groupRunId;
}

function runtimeEventExplicitClientTurnId(event: RuntimeEvent): string {
  const payload = event.payload || {};
  const message = recordField(payload, "message");
  const messageMetadata = recordField(message, "metadata");
  return (
    stringField(payload, "client_turn_id") ||
    stringField(messageMetadata, "client_turn_id")
  );
}

function isLateBoundPostAnswerEvent(event: RuntimeEvent): boolean {
  const details = recordField(event.payload || {}, "details");
  return stringField(details, "delivery") === "late_bound_sidecar";
}

function lateBoundOriginalRunId(event: RuntimeEvent): string {
  if (!isLateBoundPostAnswerEvent(event)) return "";
  return stringField(
    recordField(event.payload || {}, "details"),
    "original_run_id",
  );
}

function isTurnStartOrContinuationEvent(event: RuntimeEvent): boolean {
  const type = String(event.type || "").toLowerCase();
  const kind = runtimeEventKind(event).toLowerCase();
  return (
    type === "turn.started" ||
    type === "turn.continue_requested" ||
    kind === "chat.turn.started" ||
    kind === "chat.turn.continue_requested"
  );
}

function boundaryIsStaleForLatestUser(
  event: RuntimeEvent,
  latestUser?: UserTurnIdentity,
): boolean {
  if (!latestUser || latestUser.startedAtMs <= 0) {
    return false;
  }
  const boundaryClientTurnId = runtimeEventExplicitClientTurnId(event);
  if (latestUser.id && boundaryClientTurnId === latestUser.id) return false;
  if (latestUser.strong && latestUser.id && boundaryClientTurnId) return true;
  const eventTimeMs = runtimeEventTimeMs(event);
  return eventTimeMs > 0 && eventTimeMs < latestUser.startedAtMs;
}

function currentRuntimeEventSegment(
  events: RuntimeEvent[],
  latestUser?: UserTurnIdentity,
): {
  events: RuntimeEvent[];
  hasBoundary: boolean;
} {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (
      isForegroundTurnBoundary(events[index]) &&
      !boundaryIsStaleForLatestUser(events[index], latestUser)
    ) {
      return { events: events.slice(index), hasBoundary: true };
    }
  }
  return { events, hasBoundary: false };
}

function eventsBelongToExistingRun(
  events: RuntimeEvent[],
  existingRunId?: string | null,
): boolean {
  if (events.length > 0 && events.every(isLateBoundPostAnswerEvent)) {
    return true;
  }
  const incomingRunIds = events
    .map(runtimeEventGroupRunId)
    .filter((value): value is string => Boolean(value));
  if (incomingRunIds.length === 0) return true;
  if (!existingRunId) return false;
  return incomingRunIds.every((runId) => runId === existingRunId);
}

function selectForegroundRunId(
  events: RuntimeEvent[],
  existingState?: ChatRuntimeState | null,
  latestUser?: UserTurnIdentity,
): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (
      !isForegroundTurnBoundary(event) ||
      boundaryIsStaleForLatestUser(event, latestUser)
    ) {
      continue;
    }
    const runId = runtimeEventRunId(event) || runtimeEventGroupRunId(event);
    if (runId) return runId;
  }

  const existingRunId = String(existingState?.run_id || "").trim();
  if (existingRunId) {
    if (latestUser?.strong && latestUser.id) {
      for (let index = events.length - 1; index >= 0; index -= 1) {
        const event = events[index];
        if (
          runtimeEventParentRunId(event) ||
          isLateBoundPostAnswerEvent(event) ||
          runtimeEventExplicitClientTurnId(event) !== latestUser.id
        ) {
          continue;
        }
        const runId = runtimeEventRunId(event);
        if (runId) return runId;
      }
    }
    // An established foreground turn can only be replaced by an explicit
    // root boundary or an event carrying the exact authoritative client id of
    // the latest user turn. Late terminal, tool and nested-agent frames from
    // another run may have a newer sequence number but no such proof.
    return existingRunId;
  }

  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (isLateBoundPostAnswerEvent(events[index])) continue;
    const runId = runtimeEventGroupRunId(events[index]);
    if (runId) return runId;
  }
  return existingRunId;
}

function selectForegroundClientTurnId(
  events: RuntimeEvent[],
  runId: string,
  existingState?: ChatRuntimeState | null,
  latestUser: UserTurnIdentity = { id: "", startedAtMs: 0, strong: false },
  logicalUserBoundary = false,
): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (
      !isForegroundTurnBoundary(event) ||
      runtimeEventRunId(event) !== runId ||
      boundaryIsStaleForLatestUser(event, latestUser)
    ) {
      continue;
    }
    const clientTurnId = runtimeEventExplicitClientTurnId(event);
    if (clientTurnId) return clientTurnId;
  }

  const existingClientTurnId = String(
    existingState?.client_turn_id || "",
  ).trim();
  if (latestUser.id && latestUser.strong) {
    return latestUser.id;
  }

  // A terminal event is the value being fenced. Never let it select the
  // active client identity against which it will be checked. Prefer fresh
  // non-terminal activity, then the reducer's already-established identity.
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (
      isTerminalRuntimeEvent(event) ||
      runtimeEventRunId(event) !== runId ||
      (isForegroundTurnBoundary(event) &&
        boundaryIsStaleForLatestUser(event, latestUser))
    ) {
      continue;
    }
    const clientTurnId = runtimeEventExplicitClientTurnId(event);
    if (clientTurnId) return clientTurnId;
  }
  if (logicalUserBoundary) return latestUser.id;
  const existingRunId = String(existingState?.run_id || "").trim();
  if (existingClientTurnId && existingRunId === runId) {
    return existingClientTurnId;
  }
  const preview = existingState?.events_preview || [];
  for (let index = preview.length - 1; index >= 0; index -= 1) {
    const event = preview[index];
    const eventRunId = String(event.run_id || event.turn_id || "").trim();
    if (eventRunId !== runId || isPreviewTerminalRuntimeEvent(event)) continue;
    const clientTurnId = String(
      event.client_turn_id ||
        recordField(event.payload || {}, "message").client_turn_id ||
        "",
    ).trim();
    if (clientTurnId) return clientTurnId;
  }

  // A cold/resynced snapshot can contain only terminal rows. With no user,
  // boundary, live activity or persisted identity to own the run, use the
  // newest direct terminal's explicit identity and keep the whole reduction
  // inside that single client-turn cohort.
  if (!logicalUserBoundary) {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if (
        !isTerminalRuntimeEvent(event) ||
        !eventIsDirectlyForRun(event, runId)
      ) {
        continue;
      }
      const clientTurnId = runtimeEventExplicitClientTurnId(event);
      if (clientTurnId) return clientTurnId;
    }
  }
  return "";
}

type UserTurnIdentity = {
  id: string;
  startedAtMs: number;
  strong: boolean;
};

function messageTimestampMs(message: ChatUiMessage): number {
  const record = message as unknown as Record<string, unknown>;
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

function messageClientTurnIdentity(message: ChatUiMessage): {
  id: string;
  strong: boolean;
} {
  const record = message as unknown as Record<string, unknown>;
  const messageId =
    typeof record.id === "string" ? record.id.trim() : String(record.id || "");
  const candidates: Array<{ id: string; origin: string }> = [];
  const metadata = recordField(record, "metadata");
  const direct = stringField(metadata, "client_turn_id");
  if (direct) {
    candidates.push({
      id: direct,
      origin: stringField(metadata, "client_turn_id_origin"),
    });
  }
  const cards = Array.isArray(record.cards) ? record.cards : [];
  for (const rawCard of cards) {
    const card =
      rawCard && typeof rawCard === "object" && !Array.isArray(rawCard)
        ? (rawCard as Record<string, unknown>)
        : {};
    const data = recordField(card, "data");
    const dataMetadata = recordField(data, "metadata");
    const dataIdentity =
      stringField(data, "client_turn_id") ||
      stringField(dataMetadata, "client_turn_id");
    if (dataIdentity) {
      candidates.push({
        id: dataIdentity,
        origin:
          stringField(data, "client_turn_id_origin") ||
          stringField(dataMetadata, "client_turn_id_origin"),
      });
    }
    const inputs = Array.isArray(data.input) ? data.input : [];
    for (const rawInput of inputs) {
      const input =
        rawInput && typeof rawInput === "object" && !Array.isArray(rawInput)
          ? (rawInput as Record<string, unknown>)
          : {};
      const inputIdentity = stringField(
        recordField(input, "metadata"),
        "client_turn_id",
      );
      if (inputIdentity) {
        const inputMetadata = recordField(input, "metadata");
        candidates.push({
          id: inputIdentity,
          origin: stringField(inputMetadata, "client_turn_id_origin"),
        });
      }
    }
  }
  const candidate = candidates.find((value) => Boolean(value.id));
  if (!candidate) return { id: "", strong: false };
  // Hydration explicitly marks the only weak identity: a client turn id that
  // had to be synthesized from a legacy message id. Optimistic submissions
  // intentionally use `user-*` for both ids and remain authoritative.
  return {
    id: candidate.id,
    strong:
      candidate.origin === "message_id_fallback"
        ? false
        : Boolean(candidate.origin) ||
          candidate.id.startsWith("client-turn-") ||
          Boolean(messageId && candidate.id !== messageId),
  };
}

function latestUserTurnIdentity(messages?: ChatUiMessage[]): UserTurnIdentity {
  for (let index = (messages?.length || 0) - 1; index >= 0; index -= 1) {
    const message = messages![index];
    if (String(message.role || "").toLowerCase() !== "user") continue;
    const firstCard = Array.isArray(message.cards) ? message.cards[0] : null;
    const queueStatus =
      stringField(
        recordField(recordField(firstCard || {}, "data"), "metadata"),
        "runtime_queue_status",
      ) ||
      stringField(recordField(firstCard || {}, "data"), "runtime_queue_status");
    if (["queued", "sending", "failed"].includes(queueStatus)) continue;
    const identity = messageClientTurnIdentity(message);
    return {
      ...identity,
      startedAtMs: messageTimestampMs(message),
    };
  }
  return { id: "", startedAtMs: 0, strong: false };
}

function latestRuntimeStateTimeMs(state?: ChatRuntimeState | null): number {
  const timestamps = [state?.started_at, state?.updated_at]
    .map((value) => Date.parse(String(value || "")))
    .filter((value) => Number.isFinite(value));
  for (const event of state?.events_preview || []) {
    const parsed = Date.parse(String(event.ts || ""));
    if (Number.isFinite(parsed)) timestamps.push(parsed);
  }
  return timestamps.length ? Math.max(...timestamps) : 0;
}

function runtimeEventRootRunResolver(
  events: RuntimeEvent[],
  existingState?: ChatRuntimeState | null,
): (event: RuntimeEvent) => string {
  const parentByRun = new Map<string, string>();
  for (const event of events) {
    const runId = runtimeEventRunId(event);
    const parentRunId = runtimeEventParentRunId(event);
    if (runId && parentRunId && runId !== parentRunId) {
      parentByRun.set(runId, parentRunId);
    }
  }
  for (const event of existingState?.events_preview || []) {
    const runId = String(event.run_id || event.turn_id || "").trim();
    const payload = event.payload || {};
    const parentRunId =
      stringField(payload, "parent_run_id", "source_run_id") ||
      stringField(recordField(payload, "sub_run"), "parent_run_id");
    if (runId && parentRunId && runId !== parentRunId) {
      parentByRun.set(runId, parentRunId);
    }
  }

  const rootRunId = (runId: string): string => {
    let current = runId;
    const seen = new Set<string>();
    while (current && !seen.has(current)) {
      seen.add(current);
      const parent = parentByRun.get(current);
      if (!parent) break;
      current = parent;
    }
    return current;
  };

  return (event) => {
    const lateBoundRunId = lateBoundOriginalRunId(event);
    if (lateBoundRunId) return rootRunId(lateBoundRunId);
    return rootRunId(runtimeEventGroupRunId(event));
  };
}

function eventBelongsToForegroundClient(
  event: RuntimeEvent,
  clientTurnId: string,
): boolean {
  if (!clientTurnId) return true;
  if (runtimeEventParentRunId(event) || isLateBoundPostAnswerEvent(event)) {
    return true;
  }
  const eventClientTurnId = runtimeEventExplicitClientTurnId(event);
  return !eventClientTurnId || eventClientTurnId === clientTurnId;
}

function isPreviewTerminalRuntimeEvent(
  event: ChatTurnEventPreviewEvent,
): boolean {
  const type = String(event.type || "").toLowerCase();
  const kind = String(event.kind || "").toLowerCase();
  return (
    [
      "turn.completed",
      "turn.failed",
      "turn.interrupted",
      "turn.stalled",
    ].includes(type) ||
    [
      "chat.final_answer",
      "chat.turn.settled",
      "chat.turn.failed",
      "chat.turn.interrupted",
      "chat.turn.stalled",
    ].includes(kind)
  );
}

function previewEventClientTurnId(event: ChatTurnEventPreviewEvent): string {
  const payload = event.payload || {};
  const message = recordField(payload, "message");
  const messageMetadata = recordField(message, "metadata");
  return (
    String(event.client_turn_id || "").trim() ||
    stringField(payload, "client_turn_id") ||
    stringField(messageMetadata, "client_turn_id")
  );
}

function latestPrecedingClientTurnId(
  events: RuntimeEvent[],
  eventIndex: number,
  runId: string,
  existingState?: ChatRuntimeState | null,
): string {
  for (let index = eventIndex - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (isTerminalRuntimeEvent(event) || runtimeEventRunId(event) !== runId) {
      continue;
    }
    const clientTurnId = runtimeEventExplicitClientTurnId(event);
    if (clientTurnId) return clientTurnId;
  }
  const preview = existingState?.events_preview || [];
  for (let index = preview.length - 1; index >= 0; index -= 1) {
    const event = preview[index];
    const eventRunId = String(event.run_id || event.turn_id || "").trim();
    if (eventRunId !== runId || isPreviewTerminalRuntimeEvent(event)) continue;
    const clientTurnId = previewEventClientTurnId(event);
    if (clientTurnId) return clientTurnId;
  }
  return "";
}

function eventIsDirectlyForRun(event: RuntimeEvent, runId: string): boolean {
  if (runtimeEventParentRunId(event)) return false;
  const directRunId = runtimeEventRunId(event);
  return !runId || !directRunId || directRunId === runId;
}

function terminalEventAppliesToForeground(
  event: RuntimeEvent,
  runId: string,
  clientTurnId: string,
  context: {
    existingClientTurnId: string;
    latestUser: UserTurnIdentity;
    precedingClientTurnId: string;
    logicalUserBoundary: boolean;
  },
): boolean {
  if (!isTerminalRuntimeEvent(event) || !eventIsDirectlyForRun(event, runId)) {
    return false;
  }
  const eventClientTurnId = runtimeEventExplicitClientTurnId(event);
  const eventTimeMs = Date.parse(event.created_at || "");
  if (
    context.latestUser.startedAtMs > 0 &&
    Number.isFinite(eventTimeMs) &&
    eventTimeMs < context.latestUser.startedAtMs &&
    !(
      context.latestUser.strong &&
      context.latestUser.id &&
      eventClientTurnId === context.latestUser.id
    )
  ) {
    return false;
  }
  if (eventClientTurnId) {
    if (
      context.logicalUserBoundary &&
      !clientTurnId &&
      context.existingClientTurnId &&
      eventClientTurnId === context.existingClientTurnId
    ) {
      return false;
    }
    return !clientTurnId || eventClientTurnId === clientTurnId;
  }
  if (
    clientTurnId &&
    context.precedingClientTurnId &&
    context.precedingClientTurnId !== clientTurnId
  ) {
    return false;
  }
  if (
    context.latestUser.strong &&
    context.latestUser.id &&
    context.existingClientTurnId &&
    context.latestUser.id !== context.existingClientTurnId &&
    context.precedingClientTurnId !== context.latestUser.id
  ) {
    return false;
  }
  return true;
}

function inferRuntimeStatus(
  events: RuntimeEvent[],
  existingState?: ChatRuntimeState | null,
): ChatRuntimeState["status"] {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const kind = runtimeEventKind(event);
    const type = String(event.type || "").toLowerCase();
    const payload = event.payload || {};
    const finalTaskStatus = stringField(payload, "final_task_status");

    if (finalTaskStatus === "failed") return "failed";
    if (type === "turn.stalled" || finalTaskStatus === "stalled") {
      return "stalled";
    }
    if (type === "turn.failed") return "failed";
    if (type === "turn.interrupted" || finalTaskStatus === "interrupted") {
      return "interrupted";
    }
    if (
      type === "turn.completed" ||
      (kind === "chat.turn.settled" && finalTaskStatus !== "interrupted")
    ) {
      const turnId = event.turn_id;
      const failedEarlierInSameTurn = events
        .slice(0, index)
        .some((previous) => {
          if (turnId && previous.turn_id && previous.turn_id !== turnId) {
            return false;
          }
          const previousType = String(previous.type || "").toLowerCase();
          const previousFinalStatus = stringField(
            previous.payload || {},
            "final_task_status",
          );
          return (
            previousType === "turn.failed" || previousFinalStatus === "failed"
          );
        });
      if (failedEarlierInSameTurn) return "failed";
      return "completed";
    }
    if (kind === "chat.final_answer" || kind === "chat.turn.settled") {
      return "completed";
    }
  }

  const existingStatus = String(existingState?.status || "").toLowerCase();
  if (
    events.length > 0 &&
    TERMINAL_RUNTIME_STATUSES.has(existingStatus) &&
    !events.some(isTerminalRuntimeEvent) &&
    !events.some(isForegroundTurnBoundary) &&
    eventsBelongToExistingRun(events, existingState?.run_id)
  ) {
    return existingState?.status || existingStatus;
  }

  if (events.length > 0) return "running";
  return existingState?.status || "idle";
}

function inferOutcomeStatus(
  events: RuntimeEvent[],
  existingState?: ChatRuntimeState | null,
): NonNullable<ChatRuntimeState["outcome_status"]> {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const outcome = stringField(
      events[index].payload || {},
      "outcome_status",
    ).toLowerCase();
    if (outcome) return outcome;
  }
  return existingState?.outcome_status || "unknown";
}

function inferDeliveryStatus(
  events: RuntimeEvent[],
  existingState?: ChatRuntimeState | null,
): NonNullable<ChatRuntimeState["delivery_status"]> {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const delivery = stringField(
      events[index].payload || {},
      "delivery_status",
    ).toLowerCase();
    if (delivery) return delivery;
  }
  return existingState?.delivery_status || "pending";
}

export function runtimeEventToPreviewEvent(
  event: RuntimeEvent,
): ChatTurnEventPreviewEvent {
  const payload = event.payload || {};
  const label = runtimeEventLabel(event);
  const detail = runtimeEventDetail(event);
  const turnId = event.turn_id || stringField(payload, "turn_id") || null;
  const message = recordField(payload, "message");
  const messageMetadata = recordField(message, "metadata");
  return {
    id: event.id,
    ts: event.created_at,
    session_id: event.session_id || stringField(payload, "session_id") || null,
    run_id: stringField(payload, "run_id") || turnId,
    client_turn_id:
      stringField(payload, "client_turn_id") ||
      stringField(messageMetadata, "client_turn_id") ||
      null,
    turn_id: turnId,
    item_id: event.item_id || stringField(payload, "item_id") || null,
    seq: event.seq,
    type: event.type,
    kind: runtimeEventKind(event) || event.type,
    status: event.status || "info",
    label,
    detail,
    usage: event.usage ?? null,
    phase: stringField(payload, "phase") || null,
    trace_title: stringField(payload, "trace_title") || label,
    trace_detail: stringField(payload, "trace_detail") || detail,
    duration_ms: numberField(payload, "duration_ms"),
    payload,
  };
}

export function mergePreviewEvents(
  existing: ChatTurnEventPreviewEvent[] | undefined,
  incoming: ChatTurnEventPreviewEvent[],
  limit = MAX_STREAM_PREVIEW_EVENTS,
): ChatTurnEventPreviewEvent[] {
  const byId = new Map<string, ChatTurnEventPreviewEvent>();
  for (const event of [...(existing || []), ...incoming]) {
    if (!event?.id) continue;
    byId.set(event.id, event);
  }
  return [...byId.values()]
    .sort((left, right) => (left.seq ?? 0) - (right.seq ?? 0))
    .slice(-limit);
}

export function latestPreviewSeq(
  state?: ChatRuntimeState | null,
  preview?: ChatTurnEventPreview | null,
): number {
  const cursorValue = String(state?.stream_cursor || "").trim();
  const cursorParts = cursorValue.split(":");
  const cursorSeq = Number(cursorParts[cursorParts.length - 1]);
  const seqs = [...(state?.events_preview || []), ...(preview?.events || [])]
    .map((event) => event.seq)
    .filter((seq): seq is number => typeof seq === "number");
  if (
    typeof state?.stream_latest_seq === "number" &&
    Number.isFinite(state.stream_latest_seq)
  ) {
    seqs.push(state.stream_latest_seq);
  }
  if (cursorValue && Number.isFinite(cursorSeq)) seqs.push(cursorSeq);
  return seqs.length ? Math.max(...seqs) : 0;
}

function cloneChatMessages(messages?: ChatUiMessage[]): ChatUiMessage[] {
  if (!Array.isArray(messages)) return [];
  try {
    return JSON.parse(JSON.stringify(messages)) as ChatUiMessage[];
  } catch {
    return messages.map((message) => ({ ...message }));
  }
}

function responseDataForMessage(
  message: ChatUiMessage,
): Record<string, unknown> | null {
  const card = (message.cards || []).find(
    (item) => item.code === LANTAI_RESPONSE_CARD_CODE,
  );
  return card?.data || null;
}

function discardIncompleteModelOutputForRecovery(
  messages: ChatUiMessage[],
  event: RuntimeEvent,
): void {
  const payload = event.payload || {};
  if (
    event.type !== "runtime.warning" ||
    stringField(payload, "kind") !== "model_output_recovery"
  ) {
    return;
  }
  const details = recordField(payload, "details");
  const rawIds = details.discard_message_ids;
  if (!Array.isArray(rawIds)) return;
  const discardIds = new Set(
    rawIds.filter(
      (value): value is string => typeof value === "string" && Boolean(value),
    ),
  );
  if (!discardIds.size) return;
  const turnId = runtimeEventClientTurnId(event);

  for (const message of messages) {
    if (message.role !== ROLE_ASSISTANT) continue;
    const data = responseDataForMessage(message);
    if (!data) continue;
    if (turnId && responseTurnId(data) !== turnId) continue;
    const outputs = Array.isArray(data.output)
      ? (data.output as RuntimeOutputMessage[])
      : [];
    const filtered = outputs.filter((output) => {
      const metadata = output.metadata || {};
      return ![
        output.id,
        metadata.native_runtime_source_id,
        metadata.native_runtime_logical_source_id,
        metadata.source_message_id,
      ].some(
        (sourceId) => typeof sourceId === "string" && discardIds.has(sourceId),
      );
    });
    if (filtered.length === outputs.length) continue;
    data.output = filtered;
    data.status = "in_progress";
    data.completed_at = null;
    message.msgStatus = "loading";
  }
}

function responseTurnId(data: Record<string, unknown>): string {
  const metadata = data.metadata;
  const responseMetadata =
    metadata && typeof metadata === "object"
      ? (metadata as Record<string, unknown>)
      : {};
  const outputs = Array.isArray(data.output)
    ? (data.output as RuntimeOutputMessage[])
    : [];

  // The direct chat stream has historically attached client_turn_id either
  // to the response envelope or only to its first output item. Treat both as
  // the same identity so the runtime-ledger recovery path can claim the
  // existing card instead of rendering a second assistant turn.
  for (const key of [
    "client_turn_id",
    "native_runtime_turn_id",
    "turn_id",
    "run_id",
  ] as const) {
    const envelopeValue = stringField(responseMetadata, key);
    if (envelopeValue) return envelopeValue;
    for (const output of outputs) {
      const outputMetadata = output.metadata || {};
      const outputValue = stringField(outputMetadata, key);
      if (outputValue) return outputValue;
    }
  }
  return "";
}

function markResponseDataForTurn(
  data: Record<string, unknown>,
  turnId: string,
  transportRunId = "",
): void {
  data.metadata = {
    ...(data.metadata && typeof data.metadata === "object"
      ? (data.metadata as Record<string, unknown>)
      : {}),
    native_runtime_stream: true,
    native_runtime_turn_id: turnId,
    client_turn_id: turnId,
    ...(transportRunId
      ? { turn_id: transportRunId, run_id: transportRunId }
      : {}),
  };
}

function getOrCreateResponseData(
  messages: ChatUiMessage[],
  turnId: string,
  strictTurnIdentity = false,
  transportRunId = "",
): { message: ChatUiMessage; data: Record<string, unknown> } {
  const responseId = `native-runtime-response-${turnId || "turn"}`;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== ROLE_ASSISTANT) continue;
    const data = responseDataForMessage(message);
    if (!data) continue;
    const existingTurnId = responseTurnId(data);
    if (
      data.id === responseId ||
      existingTurnId === turnId ||
      (!strictTurnIdentity && data.status === "in_progress")
    ) {
      markResponseDataForTurn(data, turnId, transportRunId);
      return { message, data };
    }
  }

  // The direct chat stream already creates an anonymous loading card on
  // submit. Claim that card for this run instead of appending another empty
  // bubble; a card that belongs to another run is never eligible.
  if (strictTurnIdentity) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message.role !== ROLE_ASSISTANT) continue;
      const data = responseDataForMessage(message);
      if (data && data.status === "in_progress" && !responseTurnId(data)) {
        markResponseDataForTurn(data, turnId, transportRunId);
        message.msgStatus = "loading";
        return { message, data };
      }
    }
  }

  const now = Math.floor(Date.now() / 1000);
  const data: Record<string, unknown> = {
    id: responseId,
    object: "response",
    status: "in_progress",
    output: [],
    created_at: now,
    sequence_number: 0,
    error: null,
    completed_at: null,
    usage: null,
    metadata: {
      native_runtime_stream: true,
      native_runtime_turn_id: turnId,
      client_turn_id: turnId,
      ...(transportRunId
        ? { turn_id: transportRunId, run_id: transportRunId }
        : {}),
    },
  };
  const message: ChatUiMessage = {
    id: `native-runtime-${turnId || now}`,
    role: ROLE_ASSISTANT,
    cards: [{ code: LANTAI_RESPONSE_CARD_CODE, data }],
    msgStatus: "loading",
  };
  messages.push(message);
  return { message, data };
}

function ensureNativeRuntimeTurnPlaceholderInPlace(
  messages: ChatUiMessage[],
  turnId: string,
  strictTurnIdentity = false,
): boolean {
  if (!turnId) return false;
  const beforeCount = messages.length;
  getOrCreateResponseData(messages, turnId, strictTurnIdentity);
  return messages.length > beforeCount;
}

function projectionPayload(event: RuntimeEvent): Record<string, unknown> {
  const payload = event.payload || {};
  const safeKeys = [
    "phase",
    "trace_title",
    "trace_detail",
    "summary",
    "detail",
    "label",
    "name",
    "tool_name",
    "ok",
    "error_code",
    "failure_reason",
    "outcome_status",
    "delivery_status",
    "checkpoint_id",
    "strategy_change",
    "next_action",
    "mode",
    "current_step",
    "task_contract",
  ] as const;
  return Object.fromEntries(
    safeKeys.flatMap((key) =>
      payload[key] === undefined ? [] : [[key, payload[key]]],
    ),
  );
}

function applyRuntimeProjectionEventToResponse(
  messages: ChatUiMessage[],
  event: RuntimeEvent,
  strictTurnIdentity: boolean,
): void {
  const turnId = runtimeEventClientTurnId(event);
  if (!turnId) return;
  const transportRunId = runtimeEventTransportRunId(event);
  const existingTarget = [...messages].reverse().find((message) => {
    const data = responseDataForMessage(message);
    if (!data) return false;
    const metadata = recordField(data, "metadata");
    return Boolean(
      responseTurnId(data) === turnId ||
      (transportRunId &&
        stringField(metadata, "run_id", "turn_id") === transportRunId),
    );
  });
  const existingData = existingTarget
    ? responseDataForMessage(existingTarget)
    : null;
  const target =
    existingTarget && existingData
      ? { message: existingTarget, data: existingData }
      : getOrCreateResponseData(
          messages,
          turnId,
          strictTurnIdentity,
          transportRunId,
        );
  const metadata =
    target.data.metadata && typeof target.data.metadata === "object"
      ? (target.data.metadata as Record<string, unknown>)
      : {};
  const contract = recordField(event.payload || {}, "task_contract");
  const workMethodId =
    stringField(contract, "work_method_id") ||
    stringField(metadata, "work_method_id");
  const existing = Array.isArray(metadata.runtime_projection_events)
    ? (metadata.runtime_projection_events as Array<Record<string, unknown>>)
    : [];
  const next = [
    ...existing.filter((item) => item.id !== event.id),
    {
      id: event.id,
      seq: event.seq,
      type: event.type,
      status: event.status || "info",
      source: event.source,
      created_at: event.created_at,
      payload: projectionPayload(event),
    },
  ]
    .sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0))
    .slice(-MAX_RESPONSE_PROJECTION_EVENTS);
  target.data.metadata = {
    ...metadata,
    ...(workMethodId ? { work_method_id: workMethodId } : {}),
    runtime_projection_events: next,
  };
}

function recordField(
  source: Record<string, unknown>,
  key: string,
): Record<string, unknown> {
  const value = source[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/**
 * Materialize the control-plane's turn start as a local-only assistant card.
 * The direct chat SSE stream remains the owner of durable transcript content;
 * this card only makes an already-running task visible until that stream emits
 * its first model or tool event.
 */
export function ensureNativeRuntimeTurnPlaceholder(
  existingMessages: ChatUiMessage[] | undefined,
  turnId: string,
  options?: { strictTurnIdentity?: boolean },
): { messages: ChatUiMessage[]; inserted: boolean } {
  const messages = cloneChatMessages(existingMessages);
  const inserted = ensureNativeRuntimeTurnPlaceholderInPlace(
    messages,
    turnId,
    options?.strictTurnIdentity === true,
  );
  return { messages, inserted };
}

function messagePayload(event: RuntimeEvent): Record<string, unknown> {
  const payload = event.payload || {};
  const message = payload.message;
  return message && typeof message === "object"
    ? (message as Record<string, unknown>)
    : {};
}

function runtimeEventClientTurnId(event: RuntimeEvent): string {
  const payload = event.payload || {};
  const message = messagePayload(event);
  const messageMetadata =
    message.metadata && typeof message.metadata === "object"
      ? (message.metadata as Record<string, unknown>)
      : {};
  return (
    stringField(payload, "client_turn_id") ||
    stringField(messageMetadata, "client_turn_id") ||
    event.turn_id ||
    stringField(payload, "turn_id", "run_id")
  );
}

function runtimeEventTransportRunId(event: RuntimeEvent): string {
  return event.turn_id || stringField(event.payload || {}, "turn_id", "run_id");
}

function readRuntimeText(message: Record<string, unknown>): string {
  const content = message.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => {
      if (!block || typeof block !== "object") return "";
      const record = block as Record<string, unknown>;
      return stringField(record, "text", "thinking");
    })
    .join("");
}

function readRuntimeTextForContentType(
  message: Record<string, unknown>,
  contentType: "thinking" | "text",
): string {
  const content = message.content;
  if (typeof content === "string") {
    return contentType === "text" ? content : "";
  }
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => {
      if (!block || typeof block !== "object") return "";
      const record = block as Record<string, unknown>;
      if (record.type !== contentType) return "";
      return stringField(record, "text", "thinking");
    })
    .join("");
}

function firstRuntimeContentType(message: Record<string, unknown>): string {
  const content = message.content;
  if (Array.isArray(content)) {
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      const type = (block as Record<string, unknown>).type;
      if (typeof type === "string" && type) return type;
    }
  }
  return "";
}

function outputText(output: RuntimeOutputMessage): string {
  const content = output.content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => stringField(block, "text", "thinking"))
    .join("");
}

function isExactSnapshotReplay(
  liveText: string,
  terminalText: string,
): boolean {
  if (!terminalText || liveText.length <= terminalText.length) return false;
  if (liveText.length % terminalText.length !== 0) return false;
  return (
    liveText === terminalText.repeat(liveText.length / terminalText.length)
  );
}

function runtimeEventPublicProgressId(event: RuntimeEvent): string {
  const progress = recordField(event.payload || {}, "public_progress");
  const revision = progress.revision;
  const id = progress.id;
  const toolCallIds = progress.tool_call_ids;
  if (
    progress.schema_version !== 2 ||
    progress.source !== "runtime_facts" ||
    progress.text_origin !== "assistant_public_text" ||
    typeof id !== "string" ||
    !id.trim() ||
    typeof revision !== "number" ||
    !Number.isInteger(revision) ||
    revision <= 0 ||
    !(
      toolCallIds === undefined ||
      (Array.isArray(toolCallIds) &&
        toolCallIds.length > 0 &&
        toolCallIds.every(
          (value) => typeof value === "string" && Boolean(value.trim()),
        ))
    )
  ) {
    return "";
  }
  return id.trim();
}

function runtimeEventPublicProgressMetadata(
  event: RuntimeEvent,
  messageType: string,
): Record<string, unknown> {
  if (messageType !== "reasoning") return {};
  const progress = recordField(event.payload || {}, "public_progress");
  const progressId = runtimeEventPublicProgressId(event);
  const revision = progress.revision;
  const toolCallIds = Array.isArray(progress.tool_call_ids)
    ? progress.tool_call_ids.map((value) => String(value).trim())
    : [];
  if (
    !progressId ||
    typeof revision !== "number" ||
    !Number.isInteger(revision) ||
    revision <= 0
  ) {
    return {};
  }
  return {
    public_projection: "runtime_progress_v2",
    source: "runtime_facts",
    private_reasoning_exposed: false,
    public_progress_id: progressId,
    public_progress_revision: revision,
    text_origin: "assistant_public_text",
    ...(toolCallIds.length > 0 ? { tool_call_ids: toolCallIds } : {}),
  };
}

const RESERVED_PUBLIC_PROGRESS_METADATA_KEYS = [
  "public_progress",
  "public_projection",
  "public_progress_id",
  "public_progress_revision",
  "private_reasoning_exposed",
  "text_origin",
  "tool_call_ids",
  "runtime_presentation_only",
] as const;

function withoutReservedPublicProgressMetadata(
  value: Record<string, unknown>,
): Record<string, unknown> {
  const metadata = { ...value };
  for (const key of RESERVED_PUBLIC_PROGRESS_METADATA_KEYS) {
    delete metadata[key];
  }
  return metadata;
}

function runtimeEventSourceMessageId(
  event: RuntimeEvent,
  messageType: string,
): string {
  if (messageType === "reasoning") {
    const progressId = runtimeEventPublicProgressId(event);
    if (progressId) return progressId;
  }
  const payload = event.payload || {};
  const message = messagePayload(event);
  const messageMetadata =
    message.metadata && typeof message.metadata === "object"
      ? (message.metadata as Record<string, unknown>)
      : {};
  return (
    stringField(payload, "source_message_id") ||
    stringField(message, "source_message_id") ||
    stringField(messageMetadata, "source_message_id")
  );
}

function outputSourceMessageId(output: RuntimeOutputMessage): string {
  const metadata = output.metadata || {};
  return stringField(
    metadata,
    "native_runtime_logical_source_id",
    "source_message_id",
  );
}

function dedupeExactNativeRuntimeOutputs(
  outputs: RuntimeOutputMessage[],
): RuntimeOutputMessage[] {
  const deduped: RuntimeOutputMessage[] = [];
  for (const output of outputs) {
    const type = String(output.type || "message");
    const sourceMessageId = outputSourceMessageId(output);
    const duplicateIndex = sourceMessageId
      ? deduped.findIndex((candidate) => {
          return (
            String(candidate.type || "message") === type &&
            outputSourceMessageId(candidate) === sourceMessageId
          );
        })
      : -1;
    if (duplicateIndex < 0) {
      deduped.push(output);
      continue;
    }
    const previous = deduped[duplicateIndex];
    const previousMetadata = previous.metadata || {};
    const incomingMetadata = output.metadata || {};
    const eventIds = new Set<string>();
    for (const value of [
      previousMetadata.runtime_event_ids,
      incomingMetadata.runtime_event_ids,
    ]) {
      if (!Array.isArray(value)) continue;
      for (const eventId of value) {
        if (typeof eventId === "string" && eventId) eventIds.add(eventId);
      }
    }
    previous.metadata = {
      ...incomingMetadata,
      ...previousMetadata,
      ...(eventIds.size > 0 ? { runtime_event_ids: [...eventIds] } : {}),
    };
    if (output.status === "failed" || previous.status === "failed") {
      previous.status = "failed";
    } else if (
      output.status === "completed" ||
      previous.status === "completed"
    ) {
      previous.status = "completed";
    }
  }
  return deduped;
}

function streamMessageType(event: RuntimeEvent): string {
  const payload = event.payload || {};
  const message = messagePayload(event);
  const hinted = stringField(payload, "stream_message_type", "message_type");
  if (hinted) return hinted;
  const contentType =
    stringField(payload, "content_type") || firstRuntimeContentType(message);
  return contentType === "thinking" ? "reasoning" : "message";
}

function rawStreamOutputId(event: RuntimeEvent, messageType: string): string {
  const payload = event.payload || {};
  const message = messagePayload(event);
  const progressId =
    messageType === "reasoning" ? runtimeEventPublicProgressId(event) : "";
  const rawBaseId =
    progressId ||
    event.item_id ||
    stringField(message, "id") ||
    stringField(payload, "message_id") ||
    `${event.turn_id || "turn"}:${event.id}`;
  const suffix = messageType === "reasoning" ? "reasoning" : "message";
  return rawBaseId.endsWith(`:${suffix}`)
    ? rawBaseId
    : `${rawBaseId}:${suffix}`;
}

function streamOutputId(event: RuntimeEvent, messageType: string): string {
  const rawId = rawStreamOutputId(event, messageType);
  const turnId = runtimeEventClientTurnId(event);
  return turnId && !rawId.startsWith(`${turnId}:`)
    ? `${turnId}:${rawId}`
    : rawId;
}

function normalizedStreamSourceId(
  value: unknown,
  turnId: string,
  messageType: string,
): string {
  let sourceId = typeof value === "string" ? value.trim() : "";
  if (!sourceId) return "";
  if (turnId && sourceId.startsWith(`${turnId}:`)) {
    sourceId = sourceId.slice(turnId.length + 1);
  }
  const suffix = messageType === "reasoning" ? ":reasoning" : ":message";
  return sourceId.endsWith(suffix)
    ? sourceId.slice(0, -suffix.length)
    : sourceId;
}

function completedStreamParts(
  message: Record<string, unknown>,
): Array<{ messageType: string; text: string }> {
  const content = message.content;
  if (!Array.isArray(content)) return [];
  const parts: Array<{ messageType: string; text: string }> = [];
  const thinking = readRuntimeTextForContentType(message, "thinking");
  if (thinking) {
    parts.push({ messageType: "reasoning", text: thinking });
  }
  const text = readRuntimeTextForContentType(message, "text");
  if (text) {
    parts.push({ messageType: "message", text });
  }
  return parts.length > 1 ? parts : [];
}

function isTerminalRuntimeEvent(event: RuntimeEvent): boolean {
  const kind = runtimeEventKind(event);
  const type = String(event.type || "").toLowerCase();
  const finalTaskStatus = stringField(event.payload || {}, "final_task_status");
  return (
    type === "turn.completed" ||
    type === "turn.failed" ||
    type === "turn.interrupted" ||
    type === "turn.stalled" ||
    kind === "chat.final_answer" ||
    kind === "chat.turn.settled" ||
    finalTaskStatus === "failed" ||
    finalTaskStatus === "interrupted" ||
    finalTaskStatus === "stalled"
  );
}

export function runtimeResponseHasTerminalEvent(
  response: RuntimeEventsResponse,
): boolean {
  const segment = currentRuntimeEventSegment(response.events).events;
  const foregroundRunId = selectForegroundRunId(segment);
  return segment.some(
    (event) =>
      isTerminalRuntimeEvent(event) &&
      eventIsDirectlyForRun(event, foregroundRunId),
  );
}

function finalizeNativeRuntimeStreamMessages(
  messages: ChatUiMessage[],
  status: ChatRuntimeState["status"],
): void {
  const finalOutputStatus = status === "failed" ? "failed" : "completed";
  for (const message of messages) {
    if (message.role !== ROLE_ASSISTANT) continue;
    const data = responseDataForMessage(message);
    if (!data) continue;
    const output = Array.isArray(data.output)
      ? (data.output as RuntimeOutputMessage[])
      : [];
    let touched = false;
    for (const item of output) {
      const metadata = item.metadata || {};
      if (metadata.native_runtime_stream !== true) continue;
      if (item.status !== "completed" && item.status !== "failed") {
        item.status = finalOutputStatus;
        touched = true;
      }
      if (Array.isArray(item.content)) {
        for (const block of item.content) {
          if (!block || typeof block !== "object") continue;
          if (block.status !== "completed" && block.status !== "failed") {
            block.status = finalOutputStatus;
            touched = true;
          }
        }
      }
    }
    if (!touched) continue;
    const anyRunning = output.some(
      (item) => item.status !== "completed" && item.status !== "failed",
    );
    data.status = anyRunning ? "in_progress" : finalOutputStatus;
    if (!anyRunning) data.completed_at = Math.floor(Date.now() / 1000);
    message.msgStatus = anyRunning ? "loading" : "finished";
  }
}

/**
 * Providers can switch from reasoning deltas to answer deltas without a
 * separate reasoning-completed event. The renderer must close that visible
 * Thinking phase before it exposes the first answer token for the same turn.
 */
function completeNativeReasoningOutputsForTurn(
  messages: ChatUiMessage[],
  turnId: string,
): void {
  for (const message of messages) {
    if (message.role !== ROLE_ASSISTANT) continue;
    const data = responseDataForMessage(message);
    if (!data) continue;
    const output = Array.isArray(data.output)
      ? (data.output as RuntimeOutputMessage[])
      : [];
    for (const item of output) {
      const metadata = item.metadata || {};
      if (
        metadata.native_runtime_stream !== true ||
        metadata.native_runtime_turn_id !== turnId ||
        item.type !== "reasoning" ||
        item.status === "completed" ||
        item.status === "failed"
      ) {
        continue;
      }
      item.status = "completed";
      if (Array.isArray(item.content)) {
        for (const block of item.content) {
          if (!block || typeof block !== "object") continue;
          block.status = "completed";
        }
      }
    }
  }
}

function applyNativeMessageEventToOutput(
  messages: ChatUiMessage[],
  event: RuntimeEvent,
  incomingType: string,
  completedText?: string,
  strictTurnIdentity = false,
): void {
  const payload = event.payload || {};
  const message = messagePayload(event);
  if (event.type === "message.started") return;
  const itemId = streamOutputId(event, incomingType);
  const rawItemId = rawStreamOutputId(event, incomingType);
  const turnId = runtimeEventClientTurnId(event) || itemId;
  const transportRunId = runtimeEventTransportRunId(event);
  const target = getOrCreateResponseData(
    messages,
    turnId,
    strictTurnIdentity,
    transportRunId,
  );
  const data = target.data;
  const outputs = Array.isArray(data.output)
    ? (data.output as RuntimeOutputMessage[])
    : [];
  if (!Array.isArray(data.output)) data.output = outputs;
  const normalizedSourceId = normalizedStreamSourceId(
    rawItemId,
    turnId,
    incomingType,
  );
  const logicalSourceId = runtimeEventSourceMessageId(event, incomingType);

  // The low-latency chat SSE uses the provider item id directly, while the
  // durable runtime ledger namespaces that same id by logical client turn.
  // Search both aliases inside the already matched response card so a live
  // recovery frame patches the original node instead of creating a duplicate.
  let output = outputs.find((candidate) => {
    if (candidate.id === itemId || candidate.id === rawItemId) return true;
    const metadata = candidate.metadata || {};
    if (
      logicalSourceId &&
      outputSourceMessageId(candidate) === logicalSourceId &&
      String(candidate.type || "message") === incomingType
    ) {
      return true;
    }
    if (metadata.native_runtime_source_id === rawItemId) return true;
    const candidateType = String(candidate.type || "message");
    if (candidateType !== incomingType || !normalizedSourceId) return false;
    return [candidate.id, metadata.native_runtime_source_id].some(
      (candidateSourceId) =>
        normalizedStreamSourceId(candidateSourceId, turnId, incomingType) ===
        normalizedSourceId,
    );
  });
  if (!output) {
    const unclaimedDirectOutputs = outputs.filter((candidate) => {
      const candidateType = candidate.type || "message";
      return (
        candidateType === incomingType &&
        candidate.metadata?.native_runtime_stream !== true &&
        !outputText(candidate).trim()
      );
    });
    // The direct /console/chat SSE and the durable runtime ledger may expose
    // different provider item ids for the same logical output. An empty
    // anonymous placeholder is safe to claim; visible content is not. Visible
    // rows require an id or explicit source_message_id match so legitimate
    // same-type outputs are never silently overwritten.
    if (unclaimedDirectOutputs.length === 1) {
      output = unclaimedDirectOutputs[0];
    }
  }
  const wasExistingOutput = Boolean(output);
  if (!output) {
    output = {
      id: itemId,
      object: "message",
      status: "in_progress",
      type: streamMessageType(event),
      role: stringField(message, "role") || ROLE_ASSISTANT,
      content: [],
      metadata: {
        native_runtime_stream: true,
        native_runtime_turn_id: turnId,
        native_runtime_source_id: rawItemId,
        ...(logicalSourceId
          ? {
              native_runtime_logical_source_id: logicalSourceId,
              source_message_id: logicalSourceId,
            }
          : {}),
        client_turn_id: turnId,
        ...(transportRunId ? { run_id: transportRunId } : {}),
      },
    };
    outputs.push(output);
  }

  const metadata = withoutReservedPublicProgressMetadata(output.metadata || {});
  const messageMetadata =
    message.metadata && typeof message.metadata === "object"
      ? withoutReservedPublicProgressMetadata(
          message.metadata as Record<string, unknown>,
        )
      : {};
  const publicProgressMetadata = runtimeEventPublicProgressMetadata(
    event,
    incomingType,
  );
  const applied = Array.isArray(metadata.runtime_event_ids)
    ? [...(metadata.runtime_event_ids as string[])]
    : [];
  if (applied.includes(event.id)) return;

  // A completed frame owns the authoritative full message. During route
  // recovery the durable ledger can briefly replay an older delta after the
  // low-latency stream has already completed; appending it would corrupt a
  // correct answer (for example `answer1` becoming `answer11`). A genuine
  // continuation receives a new item/turn identity and is unaffected.
  if (event.type === "message.delta" && output.status === "completed") {
    output.metadata = {
      ...metadata,
      runtime_event_ids: [...applied, event.id],
    };
    return;
  }

  const currentText = outputText(output);
  const eventText = completedText ?? readRuntimeText(message);
  const delta = stringField(payload, "delta");
  const reconciledDirectStream =
    metadata.native_runtime_direct_stream_reconciled === true ||
    (wasExistingOutput && metadata.native_runtime_stream !== true);
  let nextText = currentText;
  let status = "in_progress";

  if (event.type === "message.completed") {
    const completedValue = eventText || stringField(payload, "text");
    // Route recovery can deliver a durable completed snapshot that trails the
    // low-latency stream by one safely buffered suffix. Completion owns the
    // terminal status, but a strict prefix must not make already visible text
    // move backwards. A genuinely divergent completion still replaces the
    // live text below.
    nextText =
      completedValue &&
      currentText.startsWith(completedValue) &&
      !isExactSnapshotReplay(currentText, completedValue)
        ? currentText
        : completedValue || currentText;
    status = "completed";
  } else if (event.type === "message.delta") {
    if (eventText && (!currentText || eventText.startsWith(currentText))) {
      nextText = eventText;
    } else if (eventText && currentText.startsWith(eventText)) {
      // The low-latency stream can be ahead of the ledger mirror. A shorter
      // cumulative ledger snapshot is already contained in the visible text;
      // keep the newer visible value rather than appending the snapshot.
      nextText = currentText;
    } else if (delta) {
      const nativeOwned = metadata.native_runtime_stream === true;
      nextText =
        ((wasExistingOutput && !nativeOwned) || reconciledDirectStream) &&
        currentText.endsWith(delta)
          ? currentText
          : currentText + delta;
    } else if (eventText) {
      nextText = currentText ? currentText + eventText : eventText;
    }
  }

  applied.push(event.id);
  output.status = status;
  if (
    !output.type ||
    (output.type === "message" && incomingType === "reasoning")
  ) {
    output.type = incomingType;
  }
  output.role = output.role || ROLE_ASSISTANT;
  output.content = [
    {
      object: "content",
      status,
      type: "text",
      index: 0,
      text: nextText,
      ...(Object.keys(publicProgressMetadata).length
        ? { metadata: publicProgressMetadata }
        : {}),
    },
  ];
  output.metadata = {
    ...metadata,
    ...messageMetadata,
    ...publicProgressMetadata,
    runtime_event_ids: applied,
    native_runtime_turn_id: turnId,
    native_runtime_source_id: rawItemId,
    ...(reconciledDirectStream
      ? { native_runtime_direct_stream_reconciled: true }
      : {}),
    ...(logicalSourceId
      ? {
          native_runtime_logical_source_id: logicalSourceId,
          source_message_id: logicalSourceId,
        }
      : {}),
    client_turn_id: turnId,
    ...(transportRunId ? { run_id: transportRunId } : {}),
    // This output has now been reconciled against the durable runtime stream,
    // even when it was originally created by the low-latency direct SSE.
    // Marking it lets later answer frames close the matching reasoning item
    // and makes repeated recovery deltas idempotent.
    native_runtime_stream: true,
  };

  data.output = dedupeExactNativeRuntimeOutputs(outputs);

  if (incomingType === "message" && nextText) {
    completeNativeReasoningOutputsForTurn(messages, turnId);
  }

  const anyRunning = outputs.some((item) => item.status !== "completed");
  data.status = anyRunning ? "in_progress" : "completed";
  if (!anyRunning) data.completed_at = Math.floor(Date.now() / 1000);
  target.message.msgStatus = anyRunning ? "loading" : "finished";
}

function applyNativeMessageEvent(
  messages: ChatUiMessage[],
  event: RuntimeEvent,
  strictTurnIdentity = false,
): void {
  const message = messagePayload(event);
  const incomingType = streamMessageType(event);
  if (event.type === "message.completed") {
    const parts = completedStreamParts(message);
    if (parts.length > 1) {
      for (const part of parts) {
        applyNativeMessageEventToOutput(
          messages,
          event,
          part.messageType,
          part.text,
          strictTurnIdentity,
        );
      }
      return;
    }
  }
  applyNativeMessageEventToOutput(
    messages,
    event,
    incomingType,
    undefined,
    strictTurnIdentity,
  );
}

function toolCallId(event: RuntimeEvent): string {
  const payload = event.payload || {};
  return (
    event.item_id ||
    stringField(payload, "call_id", "tool_call_id", "id") ||
    event.id
  );
}

function artifactFileName(value: string): string {
  const normalized = value.replace(/\\/g, "/");
  const segments = normalized.split("/").filter(Boolean);
  const basename = segments.length ? segments[segments.length - 1] : "";
  try {
    return decodeURIComponent(basename);
  } catch {
    return basename;
  }
}

function normalizedArtifactRelativePath(value: string): string {
  const raw = value.replace(/\\/g, "/").trim();
  if (!raw || raw.startsWith("/")) return "";
  const segments = raw.split("/");
  if (
    segments.some((segment) => !segment || segment === "." || segment === "..")
  ) {
    return "";
  }
  try {
    return segments.map((segment) => decodeURIComponent(segment)).join("/");
  } catch {
    return "";
  }
}

function normalizedArtifactFileLocation(value: string): string {
  let location = value.trim();
  if (!location) return "";
  try {
    location = new URL(location).pathname;
  } catch {
    // A canonical tool may return a local path instead of a file URL.
  }
  location = location.replace(/\\/g, "/");
  try {
    return decodeURIComponent(location);
  } catch {
    return "";
  }
}

function projectDeliveredArtifacts(
  outputs: RuntimeOutputMessage[],
  payload: Record<string, unknown>,
  callMetadata: Record<string, unknown>,
): void {
  const artifactEffects = Array.isArray(payload.artifact_effects)
    ? payload.artifact_effects
    : [];
  const toolOutput = Array.isArray(payload.output) ? payload.output : [];
  const artifactBlocks = toolOutput.filter(
    (block): block is Record<string, unknown> =>
      Boolean(
        block &&
        typeof block === "object" &&
        !Array.isArray(block) &&
        ["file", "video"].includes(
          stringField(block as Record<string, unknown>, "type"),
        ),
      ),
  );

  for (const rawEffect of artifactEffects) {
    if (
      !rawEffect ||
      typeof rawEffect !== "object" ||
      Array.isArray(rawEffect)
    ) {
      continue;
    }
    const effect = rawEffect as Record<string, unknown>;
    if (
      stringField(effect, "artifact_role") !== "delivery" ||
      stringField(effect, "status") !== "delivered"
    ) {
      continue;
    }
    const artifactId = stringField(effect, "artifact_id");
    const effectId = stringField(effect, "artifact_effect_id");
    const sha256 = stringField(effect, "sha256");
    const relativePath = normalizedArtifactRelativePath(
      stringField(effect, "relative_path"),
    );
    const expectedName = artifactFileName(relativePath);
    if (!artifactId || !effectId || !sha256 || !relativePath || !expectedName) {
      continue;
    }
    if (
      outputs.some(
        (item) =>
          stringField(item.metadata || {}, "runtime_artifact_id") ===
          artifactId,
      )
    ) {
      continue;
    }
    const matchingArtifactBlocks = artifactBlocks.filter((block) => {
      const source = recordField(block, "source");
      const candidateName =
        stringField(block, "filename", "file_name", "name") ||
        artifactFileName(stringField(source, "url"));
      const candidateLocation = normalizedArtifactFileLocation(
        stringField(block, "file_url", "video_url") ||
          stringField(source, "url"),
      );
      return (
        candidateName === expectedName &&
        (candidateLocation === relativePath ||
          candidateLocation.endsWith(`/${relativePath}`))
      );
    });
    if (matchingArtifactBlocks.length !== 1) continue;
    const artifactBlock = matchingArtifactBlocks[0];
    const artifactType = stringField(artifactBlock, "type");
    const source = recordField(artifactBlock, "source");
    const artifactUrl =
      stringField(artifactBlock, "file_url", "video_url") ||
      stringField(source, "url");
    if (!artifactUrl) continue;
    const sizeBytes = numberField(effect, "size_bytes");
    const content =
      artifactType === "video"
        ? {
            type: "video",
            status: "completed",
            video_url: artifactUrl,
            source: { type: "url", url: artifactUrl },
            metadata: { durable: true },
          }
        : {
            type: "file",
            status: "completed",
            file_url: artifactUrl,
            file_source_url: artifactUrl,
            file_name: expectedName,
            ...(sizeBytes === null ? {} : { file_size: sizeBytes }),
          };
    outputs.push({
      id: `native-runtime-artifact-${artifactId}`,
      role: ROLE_ASSISTANT,
      type: "message",
      status: "completed",
      content: [content],
      metadata: {
        ...callMetadata,
        runtime_artifact_id: artifactId,
        runtime_artifact_sha256: sha256,
        runtime_artifact_effect_id: effectId,
      },
    });
  }
}

function applyNativeToolLifecycleEvent(
  messages: ChatUiMessage[],
  event: RuntimeEvent,
  strictTurnIdentity = false,
): void {
  const turnId = runtimeEventClientTurnId(event);
  if (!turnId) return;
  const transportRunId = runtimeEventTransportRunId(event);
  const target = getOrCreateResponseData(
    messages,
    turnId,
    strictTurnIdentity,
    transportRunId,
  );
  const outputs = Array.isArray(target.data.output)
    ? (target.data.output as RuntimeOutputMessage[])
    : [];
  const payload = event.payload || {};
  const callId = toolCallId(event);
  const toolName = stringField(payload, "tool_name", "name") || "tool";
  const isCompleted =
    event.type === "tool.call.completed" ||
    ["completed", "failed", "error"].includes(
      String(event.status || "").toLowerCase(),
    ) ||
    ["chat.tool_call.finished", "chat.tool_call.completed"].includes(
      runtimeEventKind(event).toLowerCase(),
    );
  const callMetadata = {
    native_runtime_stream: true,
    native_runtime_turn_id: turnId,
    client_turn_id: turnId,
    runtime_tool_call_id: callId,
    ...(transportRunId ? { run_id: transportRunId } : {}),
  };
  let call = outputs.find((item) => {
    const metadata = item.metadata || {};
    return (
      item.type === "plugin_call" &&
      (stringField(metadata, "runtime_tool_call_id") === callId ||
        stringField(recordField(item.content?.[0] || {}, "data"), "call_id") ===
          callId)
    );
  });
  if (!call) {
    call = {
      id: `native-runtime-tool-${turnId}-${callId}`,
      role: ROLE_ASSISTANT,
      type: "plugin_call",
      status: isCompleted ? "completed" : "in_progress",
      content: [
        {
          type: "function_call",
          data: {
            call_id: callId,
            name: toolName,
            arguments:
              payload.arguments ?? payload.input ?? payload.params ?? {},
          },
        },
      ],
      metadata: callMetadata,
    };
    outputs.push(call);
  } else if (isCompleted) {
    call.status = "completed";
  }

  if (isCompleted) {
    const alreadyHasOutput = outputs.some((item) => {
      const metadata = item.metadata || {};
      return (
        item.type === "plugin_call_output" &&
        stringField(metadata, "runtime_tool_call_id") === callId
      );
    });
    if (!alreadyHasOutput) {
      const failed = ["failed", "error"].includes(
        String(event.status || "").toLowerCase(),
      );
      outputs.push({
        id: `native-runtime-tool-output-${turnId}-${callId}`,
        role: "tool",
        type: "plugin_call_output",
        status: failed ? "failed" : "completed",
        content: [
          {
            type: "function_call_output",
            data: {
              call_id: callId,
              name: toolName,
              output:
                payload.output ??
                payload.result ??
                payload.error ??
                payload.failure_reason ??
                "",
            },
          },
        ],
        metadata: callMetadata,
      });
    }
    projectDeliveredArtifacts(outputs, payload, callMetadata);
  }
  target.data.output = outputs;
  target.data.status = "in_progress";
  target.message.msgStatus = "loading";
}

export function buildNativeRuntimeStreamMessages(
  events: RuntimeEvent[],
  existingMessages?: ChatUiMessage[],
  options?: { strictTurnIdentity?: boolean },
): ChatUiMessage[] {
  const messages = cloneChatMessages(existingMessages);
  const hasNativeMessageFrame = events.some(isNativeMessageStreamEvent);
  for (const event of events) {
    discardIncompleteModelOutputForRecovery(messages, event);
    if (isTurnStartOrContinuationEvent(event) && !hasNativeMessageFrame) {
      ensureNativeRuntimeTurnPlaceholderInPlace(
        messages,
        runtimeEventRunId(event),
        options?.strictTurnIdentity === true,
      );
    }
    if (isNativeMessageStreamEvent(event)) {
      applyNativeMessageEvent(
        messages,
        event,
        options?.strictTurnIdentity === true,
      );
    } else if (isNativeToolLifecycleEvent(event)) {
      applyNativeToolLifecycleEvent(
        messages,
        event,
        options?.strictTurnIdentity === true,
      );
    }
  }
  for (const event of events) {
    applyRuntimeProjectionEventToResponse(
      messages,
      event,
      options?.strictTurnIdentity === true,
    );
  }
  return messages;
}

export function runtimeResponseNeedsVisibleTurnPlaceholder(
  response: RuntimeEventsResponse,
): boolean {
  return (
    response.events.some(isTurnStartOrContinuationEvent) &&
    !response.events.some(isNativeMessageStreamEvent)
  );
}

export function buildRuntimeEventSessionPatch(
  response: RuntimeEventsResponse,
  existingState?: ChatRuntimeState | null,
  existingPreview?: ChatTurnEventPreview | null,
  existingMessages?: ChatUiMessage[],
  options?: {
    mirrorNativeMessages?: boolean;
    strictTurnIdentity?: boolean;
    identityMessages?: ChatUiMessage[];
  },
): {
  generating: boolean;
  acceptedTerminal: boolean;
  runtimeState: ChatRuntimeState;
  turnEventPreview: ChatTurnEventPreview;
  messages?: ChatUiMessage[];
} {
  const latestUser = latestUserTurnIdentity(
    options?.identityMessages || existingMessages,
  );
  const currentSegment = currentRuntimeEventSegment(
    response.events,
    latestUser,
  );
  const currentEvents = currentSegment.events;
  const lateBoundRunId = [...currentEvents]
    .reverse()
    .map(lateBoundOriginalRunId)
    .find((value): value is string => Boolean(value));
  const foregroundRunId = selectForegroundRunId(
    currentEvents,
    existingState,
    latestUser,
  );
  const runId =
    foregroundRunId || lateBoundRunId || existingState?.run_id || null;
  const persistedClientTurnId = String(
    existingState?.client_turn_id || "",
  ).trim();
  const existingClientTurnId =
    persistedClientTurnId ||
    latestPrecedingClientTurnId([], 0, runId || "", existingState);
  const latestUserIsNewerThanRuntime = Boolean(
    existingState &&
    latestUser.startedAtMs > 0 &&
    latestRuntimeStateTimeMs(existingState) > 0 &&
    latestUser.startedAtMs > latestRuntimeStateTimeMs(existingState),
  );
  const logicalUserBoundary = Boolean(
    !currentSegment.hasBoundary &&
    existingState &&
    ((latestUser.strong &&
      latestUser.id &&
      latestUser.id !== existingClientTurnId) ||
      latestUserIsNewerThanRuntime),
  );
  const selectedClientTurnId = selectForegroundClientTurnId(
    currentEvents,
    runId || "",
    existingState,
    latestUser,
    logicalUserBoundary,
  );
  const previousState =
    currentSegment.hasBoundary || logicalUserBoundary
      ? undefined
      : existingState;

  const resolveRootRunId = runtimeEventRootRunResolver(
    currentEvents,
    existingState,
  );
  const foregroundEvents = currentEvents.filter((event) => {
    const eventRootRunId = resolveRootRunId(event);
    if (runId && eventRootRunId && eventRootRunId !== runId) return false;
    if (
      logicalUserBoundary &&
      !runtimeEventParentRunId(event) &&
      !isLateBoundPostAnswerEvent(event) &&
      latestUser.startedAtMs > 0 &&
      runtimeEventTimeMs(event) > 0 &&
      runtimeEventTimeMs(event) < latestUser.startedAtMs
    ) {
      return false;
    }
    return eventBelongsToForegroundClient(event, selectedClientTurnId);
  });

  const acceptedTerminalEvents = foregroundEvents.filter((event, index) =>
    terminalEventAppliesToForeground(event, runId || "", selectedClientTurnId, {
      existingClientTurnId,
      latestUser,
      logicalUserBoundary,
      precedingClientTurnId: latestPrecedingClientTurnId(
        foregroundEvents,
        index,
        runId || "",
        existingState,
      ),
    }),
  );
  const acceptedTerminalSet = new Set(acceptedTerminalEvents);
  const previewEvents = foregroundEvents.filter((event) => {
    if (isNativeMessageStreamEvent(event)) return false;
    if (
      isTerminalRuntimeEvent(event) &&
      eventIsDirectlyForRun(event, runId || "") &&
      !acceptedTerminalSet.has(event)
    ) {
      return false;
    }
    return true;
  });
  const incoming = previewEvents.map(runtimeEventToPreviewEvent);
  const events = mergePreviewEvents(
    response.resync_required ||
      currentSegment.hasBoundary ||
      logicalUserBoundary
      ? []
      : existingState?.events_preview || existingPreview?.events,
    incoming,
  );
  const latestEvent = previewEvents[previewEvents.length - 1];
  const latestPreview = events[events.length - 1];
  const statusEvents = foregroundEvents.filter(
    (event) => !isTerminalRuntimeEvent(event) || acceptedTerminalSet.has(event),
  );
  let status = inferRuntimeStatus(statusEvents, previousState);
  const previousStatus = String(previousState?.status || "").toLowerCase();
  if (
    TERMINAL_RUNTIME_STATUSES.has(previousStatus) &&
    runId === String(previousState?.run_id || "").trim() &&
    acceptedTerminalEvents.length === 0 &&
    !currentSegment.hasBoundary &&
    !logicalUserBoundary
  ) {
    status = previousState?.status || previousStatus;
  }
  if (
    currentEvents.length > 0 &&
    statusEvents.length === 0 &&
    !TERMINAL_RUNTIME_STATUSES.has(
      String(previousState?.status || "").toLowerCase(),
    )
  ) {
    // A child terminal is still evidence that the parent task is active; it
    // may close a stage, but it must not settle the parent itself.
    status = "running";
  }
  if (logicalUserBoundary && status === "idle") status = "running";
  const outcomeStatus = inferOutcomeStatus(
    acceptedTerminalEvents,
    previousState,
  );
  const deliveryStatus = inferDeliveryStatus(
    acceptedTerminalEvents,
    previousState,
  );
  const terminalClientTurnId = acceptedTerminalEvents
    .map(runtimeEventExplicitClientTurnId)
    .find((value) => Boolean(value));
  const clientTurnId = selectedClientTurnId || terminalClientTurnId || null;
  const foregroundBoundary = foregroundEvents.find(
    (event) =>
      isForegroundTurnBoundary(event) &&
      !boundaryIsStaleForLatestUser(event, latestUser),
  );
  const startedAt =
    foregroundBoundary?.created_at ||
    (logicalUserBoundary && latestUser.startedAtMs > 0
      ? new Date(latestUser.startedAtMs).toISOString()
      : existingState?.started_at || null);
  const summary =
    (latestEvent ? runtimeEventLabel(latestEvent) : "") ||
    latestPreview?.label ||
    previousState?.summary ||
    (currentSegment.hasBoundary || logicalUserBoundary
      ? ""
      : existingPreview?.summary_line) ||
    "运行事件已更新";
  const workspaceDir =
    response.workspace_dir ??
    existingState?.workspace_dir ??
    existingPreview?.workspace_dir ??
    null;

  const runtimeState: ChatRuntimeState = {
    ...(existingState || { status: "idle" }),
    status,
    outcome_status: outcomeStatus,
    delivery_status: status === "running" ? "pending" : deliveryStatus,
    run_id: runId,
    client_turn_id: clientTurnId,
    started_at: startedAt,
    updated_at: new Date().toISOString(),
    summary,
    events_preview: events,
    workspace_dir: workspaceDir,
    stream_epoch: response.stream_epoch || existingState?.stream_epoch || null,
    stream_cursor: response.cursor || existingState?.stream_cursor || null,
    stream_latest_seq:
      typeof response.latest_seq === "number"
        ? response.latest_seq
        : existingState?.stream_latest_seq || null,
    can_reconnect: status === "running",
    can_stop: status === "running",
    transport_status: status === "running" ? "connected" : undefined,
    transport_error: null,
  };

  const nativeMessages =
    options?.mirrorNativeMessages !== false &&
    existingMessages !== undefined &&
    (foregroundEvents.some(
      (event) =>
        isNativeMessageStreamEvent(event) || isNativeToolLifecycleEvent(event),
    ) ||
      (status === "running" &&
        foregroundEvents.some((event) =>
          isTurnStartOrContinuationEvent(event),
        )))
      ? buildNativeRuntimeStreamMessages(foregroundEvents, existingMessages, {
          strictTurnIdentity: options?.strictTurnIdentity === true,
        })
      : undefined;
  if (
    nativeMessages &&
    status !== "running" &&
    acceptedTerminalEvents.length > 0
  ) {
    finalizeNativeRuntimeStreamMessages(nativeMessages, status);
  }

  return {
    generating: status === "running",
    acceptedTerminal: acceptedTerminalEvents.length > 0,
    runtimeState,
    turnEventPreview: {
      workspace_dir: workspaceDir,
      summary_line: summary,
      events,
    },
    messages: nativeMessages,
  };
}
