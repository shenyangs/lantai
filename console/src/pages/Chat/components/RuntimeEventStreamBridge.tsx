import {
  chooseActiveSessionMessages,
  useChatAnywhereSessionsState,
  type ILantaiChatSession,
} from "@/vendor/lantaiChat";
import { durableTranscriptHydrationIsRequired } from "@/vendor/lantaiTranscriptReconciler";
import { useEffect, useMemo, useRef } from "react";
import type {
  ChatRuntimeState,
  ChatTurnEventPreview,
  RuntimeEventsResponse,
} from "../../../api/types/chat";
import {
  buildRuntimeEventSessionPatch,
  isNativeMessageStreamEvent,
  isNativeToolLifecycleEvent,
  latestPreviewSeq,
  runtimeResponseHasTerminalEvent,
} from "../runtimeEventStream";
import { ingestCollaborationRuntimeEvents } from "../collaborationEventStore";
import { chatApi } from "../../../api/modules/chat";
import sessionApi from "../sessionApi";
import { subscribeRuntimeEventSession } from "../runtimeEventSubscriptionRegistry";

interface ExtendedSession extends ILantaiChatSession {
  realId?: string;
  sessionId?: string;
  generating?: boolean;
  status?: string;
  runtimeState?: ChatRuntimeState | null;
  turnEventPreview?: ChatTurnEventPreview | null;
}

interface RuntimeEventStreamBridgeProps {
  chatId?: string | null;
  refreshKey?: number;
  onUpdate?: (response: RuntimeEventsResponse) => void;
}

const RUNTIME_STREAM_MAX_RECONNECT_ATTEMPTS = 5;
const RUNTIME_RECONCILE_INTERVAL_MS = 12_000;
const RUNTIME_RECONCILE_DEADLINE_MS = 5_000;
// Give the primary transcript stream a brief chance to settle, but keep the
// durable-history backfill inside the two-second UI convergence budget. A
// longer grace recreates the user-visible "finished only after switching
// chats" failure when Chromium suspends the primary stream.
const PRIMARY_STREAM_TERMINAL_GRACE_MS = 1_200;
const COLLABORATION_EVENT_TYPES = [
  "tool.call.started",
  "tool.call.delta",
  "tool.call.completed",
];
const TERMINAL_RUNTIME_STATUSES = new Set([
  "completed",
  "failed",
  "interrupted",
  "stalled",
]);

function isProbablyBackendChatId(value?: string | null): value is string {
  if (!value || /^\d{11,}$/.test(value)) return false;
  return value !== "undefined" && value !== "null";
}

function sessionMatchesBackendChatId(
  session: ExtendedSession | undefined,
  backendChatId: string | null,
): boolean {
  if (!session || !backendChatId) return false;
  return [session.id, session.realId, session.sessionId].some(
    (value) => value === backendChatId,
  );
}

function streamErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim())
    return error.message.trim();
  if (typeof error === "string" && error.trim()) return error.trim();
  return "Runtime event stream disconnected.";
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function nonEmptyString(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function latestUserTurnFingerprint(
  messages?: ILantaiChatSession["messages"],
): string {
  for (let index = (messages?.length || 0) - 1; index >= 0; index -= 1) {
    const message = messages![index];
    if (String(message.role || "").toLowerCase() !== "user") continue;
    const record = objectValue(message);
    const messageId = nonEmptyString(record.id);
    const cards = Array.isArray(record.cards) ? record.cards : [];
    const firstCardData = objectValue(objectValue(cards[0]).data);
    const queueStatus = nonEmptyString(firstCardData.runtime_queue_status);
    if (["queued", "sending", "failed"].includes(queueStatus)) continue;
    const identities: Array<{ id: string; origin: string }> = [];
    const metadata = objectValue(record.metadata);
    const directIdentity = nonEmptyString(metadata.client_turn_id);
    if (directIdentity) {
      identities.push({
        id: directIdentity,
        origin: nonEmptyString(metadata.client_turn_id_origin),
      });
    }
    for (const rawCard of cards) {
      const data = objectValue(objectValue(rawCard).data);
      const dataMetadata = objectValue(data.metadata);
      const dataIdentity =
        nonEmptyString(data.client_turn_id) ||
        nonEmptyString(dataMetadata.client_turn_id);
      if (dataIdentity) {
        identities.push({
          id: dataIdentity,
          origin:
            nonEmptyString(data.client_turn_id_origin) ||
            nonEmptyString(dataMetadata.client_turn_id_origin),
        });
      }
      const inputs = Array.isArray(data.input) ? data.input : [];
      for (const rawInput of inputs) {
        const inputMetadata = objectValue(objectValue(rawInput).metadata);
        const inputIdentity = nonEmptyString(inputMetadata.client_turn_id);
        if (inputIdentity) {
          identities.push({
            id: inputIdentity,
            origin: nonEmptyString(inputMetadata.client_turn_id_origin),
          });
        }
      }
    }
    const identity = identities.find((candidate) => Boolean(candidate.id));
    const clientTurnId = identity?.id || "";
    const strongClientTurnId = Boolean(
      clientTurnId &&
      (identity?.origin === "message_id_fallback"
        ? false
        : Boolean(identity?.origin) ||
          clientTurnId.startsWith("client-turn-") ||
          clientTurnId !== messageId),
    );
    // History conversion may synthesize client_turn_id from the message id.
    // Normalize that weak fallback to the same fingerprint as the raw legacy
    // row so a harmless hydration representation change does not cancel work.
    if (strongClientTurnId) {
      return `client:${clientTurnId}`;
    }
    if (messageId) return `message:${messageId}`;
    const createdAt =
      nonEmptyString(record.createdAt) ||
      nonEmptyString(record.created_at) ||
      nonEmptyString(record.timestamp);
    return createdAt ? `time:${createdAt}` : "user:unknown";
  }
  return "";
}

function isSessionRuntimeActive(session?: ExtendedSession): boolean {
  const runtimeStatus = String(
    session?.runtimeState?.status || "",
  ).toLowerCase();
  const status = String(session?.status || "").toLowerCase();
  // Durable runtime state owns the lifecycle. Renderer flags can lag after a
  // backgrounded stream or route remount and must never resurrect a terminal
  // run into polling, stopping, or spinner state.
  if (TERMINAL_RUNTIME_STATUSES.has(runtimeStatus)) return false;
  return (
    session?.__primaryStreamActive === true ||
    session?.generating === true ||
    runtimeStatus === "running" ||
    runtimeStatus === "stopping" ||
    status === "running"
  );
}

/**
 * Runtime events are the durable live ledger for a turn. The direct
 * `/console/chat` SSE remains the low-latency path, but Chromium can throttle
 * or silently stall that connection while the task drawer keeps receiving
 * runtime events. Mirror native message events into the local transcript so
 * thinking and answer progress never disappear. Event ids and strict turn
 * identity make this path an idempotent backfill rather than a second turn.
 */
export default function RuntimeEventStreamBridge({
  chatId,
  refreshKey,
  onUpdate,
}: RuntimeEventStreamBridgeProps) {
  const { sessions, currentSessionId, setSessions } =
    useChatAnywhereSessionsState();
  const currentSession = useMemo(
    () =>
      sessions.find((session) => session.id === currentSessionId) as
        | ExtendedSession
        | undefined,
    [currentSessionId, sessions],
  );
  const currentSessionRef = useRef<ExtendedSession | undefined>(currentSession);
  const onUpdateRef = useRef(onUpdate);

  useEffect(() => {
    currentSessionRef.current = currentSession;
  }, [currentSession]);

  useEffect(() => {
    onUpdateRef.current = onUpdate;
  }, [onUpdate]);

  const routeChatId = isProbablyBackendChatId(chatId) ? chatId : null;
  const streamChatId =
    routeChatId ||
    currentSession?.realId ||
    sessionApi.getRealIdForSession(String(currentSessionId || ""));

  useEffect(() => {
    if (!streamChatId) return;
    const resolvedCurrentSessionId = sessionApi.getRealIdForSession(
      String(currentSessionId || ""),
    );
    const initialSession =
      sessionMatchesBackendChatId(currentSessionRef.current, streamChatId) ||
      resolvedCurrentSessionId === streamChatId
        ? currentSessionRef.current
        : undefined;
    const targetSessionId = initialSession?.id || streamChatId;
    if (!targetSessionId) return;

    let mounted = true;
    let terminalHydrationTimer: number | null = null;
    let terminalHydrationIdentity: {
      runId: string;
      clientTurnId: string;
      userFingerprint: string;
    } | null = null;
    let reconcileInFlight = false;
    let reconcileGeneration = 0;
    let reconcileController: AbortController | null = null;
    let latestRuntimeSeq = latestPreviewSeq(
      initialSession?.runtimeState,
      initialSession?.turnEventPreview,
    );
    const matchesTarget = (session: ExtendedSession | undefined) =>
      session?.id === targetSessionId ||
      sessionMatchesBackendChatId(session, streamChatId);

    const commitMetadata = (
      patch: Pick<
        ExtendedSession,
        "generating" | "status" | "runtimeState" | "turnEventPreview"
      >,
      failureMessage: string,
      transientMessages?: ILantaiChatSession["messages"],
      projectTransientMessages?: (
        session?: ExtendedSession,
      ) => ILantaiChatSession["messages"],
      snapshotBase?: ExtendedSession,
    ) => {
      if (!mounted) return;
      const current = matchesTarget(currentSessionRef.current)
        ? currentSessionRef.current
        : undefined;
      const optimisticMessages = projectTransientMessages
        ? projectTransientMessages(current)
        : transientMessages
          ? chooseActiveSessionMessages(
              current?.messages || [],
              transientMessages,
            )
          : undefined;
      const optimistic = {
        ...(current || {}),
        ...(snapshotBase || {}),
        id: targetSessionId,
        ...patch,
        ...(optimisticMessages !== undefined
          ? { messages: optimisticMessages }
          : {}),
      } as ExtendedSession;
      currentSessionRef.current = optimistic;
      setSessions((previous) => {
        let matched = false;
        const next = previous.map((session) => {
          if (!matchesTarget(session as ExtendedSession)) return session;
          matched = true;
          // The direct transcript stream and runtime tool ledger update the
          // same session independently. `currentSessionRef` can be one React
          // render behind this functional updater. Rebuild the tool projection
          // from the updater's latest transcript instead of trying to merge a
          // stale projection after the fact; runtime events may add chronology,
          // never remove live narration rows.
          const latestMessages = projectTransientMessages
            ? projectTransientMessages(session as ExtendedSession)
            : transientMessages
              ? chooseActiveSessionMessages(
                  (session as ExtendedSession).messages || [],
                  transientMessages,
                )
              : undefined;
          return {
            ...session,
            ...(snapshotBase || {}),
            id: session.id,
            ...patch,
            ...(latestMessages !== undefined
              ? { messages: latestMessages }
              : {}),
          } as ExtendedSession;
        });
        if (!matched) {
          return [optimistic, ...next] as ILantaiChatSession[];
        }
        return next;
      });
      // Runtime-mirrored messages are intentionally local-only. Durable chat
      // history is still written by the backend/direct stream, so a renderer
      // recovery path can never overwrite the canonical transcript.
      void sessionApi
        .updateSession({
          id: targetSessionId,
          ...patch,
          __preserveMessages: false,
        })
        .catch((error) => console.warn(failureMessage, error));
    };

    const applyRuntimeResponse = (response: RuntimeEventsResponse) => {
      const responseLatestSeq =
        typeof response.latest_seq === "number"
          ? response.latest_seq
          : response.events.reduce(
              (latest, event) => Math.max(latest, event.seq || 0),
              0,
            );
      const current = matchesTarget(currentSessionRef.current)
        ? currentSessionRef.current
        : undefined;
      const hasNativeTranscriptFrame = response.events.some(
        isNativeMessageStreamEvent,
      );
      const hasNativeToolFrame = response.events.some(
        isNativeToolLifecycleEvent,
      );
      const hasTerminalEvent = runtimeResponseHasTerminalEvent(response);
      // The primary /console/chat stream owns low-latency text while active.
      // Applying its durable mirror token-by-token would deep-clone the whole
      // transcript and rebuild session state twice for every token. Keep the
      // ledger transport as a silent backup until the primary stream settles
      // or stalls; terminal hydration remains the final recovery path.
      const shouldProjectNativeMessages =
        hasNativeToolFrame ||
        (hasNativeTranscriptFrame &&
          (current?.__primaryStreamActive !== true || hasTerminalEvent));
      const projectionOptions = {
        // Backfill native reasoning/answer frames as they arrive. The
        // runtime reducer records applied event ids and claims only the
        // matching turn, so replay/reconciliation remains idempotent.
        mirrorNativeMessages: true,
        strictTurnIdentity: true,
      } as const;
      const runtimePatch = buildRuntimeEventSessionPatch(
        response,
        current?.runtimeState,
        current?.turnEventPreview,
        shouldProjectNativeMessages ? current?.messages : undefined,
        {
          ...projectionOptions,
          identityMessages: current?.messages,
        },
      );
      commitMetadata(
        {
          generating: runtimePatch.generating,
          status: runtimePatch.generating ? "running" : "idle",
          runtimeState: runtimePatch.runtimeState,
          turnEventPreview: runtimePatch.turnEventPreview,
        },
        "Failed to apply runtime event metadata:",
        runtimePatch.messages,
        shouldProjectNativeMessages
          ? (latestSession) =>
              buildRuntimeEventSessionPatch(
                response,
                latestSession?.runtimeState,
                latestSession?.turnEventPreview,
                latestSession?.messages,
                {
                  ...projectionOptions,
                  identityMessages: latestSession?.messages,
                },
              ).messages
          : undefined,
      );
      latestRuntimeSeq = response.resync_required
        ? responseLatestSeq
        : Math.max(latestRuntimeSeq, responseLatestSeq);
      ingestCollaborationRuntimeEvents(response.events);

      if (
        !runtimePatch.acceptedTerminal &&
        runtimePatch.generating &&
        terminalHydrationIdentity
      ) {
        if (terminalHydrationTimer !== null) {
          window.clearTimeout(terminalHydrationTimer);
        }
        terminalHydrationTimer = null;
        terminalHydrationIdentity = null;
      }

      // A terminal control-plane event is authoritative. The direct chat SSE
      // normally owns live cards, but it may be suspended by Chromium while a
      // long task is backgrounded. Hydrate the durable transcript after a
      // short grace period so the result appears without forcing the user to
      // switch conversations away and back.
      if (runtimePatch.acceptedTerminal) {
        if (terminalHydrationTimer !== null) {
          window.clearTimeout(terminalHydrationTimer);
        }
        const acceptedIdentity = {
          runId: String(runtimePatch.runtimeState.run_id || "").trim(),
          clientTurnId: String(
            runtimePatch.runtimeState.client_turn_id || "",
          ).trim(),
          userFingerprint: latestUserTurnFingerprint(current?.messages),
        };
        terminalHydrationIdentity = acceptedIdentity;
        const primaryWasActiveAtTerminal =
          current?.__primaryStreamActive === true;
        terminalHydrationTimer = window.setTimeout(() => {
          terminalHydrationTimer = null;
          if (terminalHydrationIdentity !== acceptedIdentity) return;
          const latestCurrent = matchesTarget(currentSessionRef.current)
            ? currentSessionRef.current
            : undefined;
          const latestRuntimeState = latestCurrent?.runtimeState;
          const latestRuntimeStatus = String(
            latestRuntimeState?.status || "",
          ).toLowerCase();
          const latestRunId = String(latestRuntimeState?.run_id || "").trim();
          const latestClientTurnId = String(
            latestRuntimeState?.client_turn_id || "",
          ).trim();
          const latestUserFingerprint = latestUserTurnFingerprint(
            latestCurrent?.messages,
          );
          if (
            !["completed", "failed", "interrupted", "stalled"].includes(
              latestRuntimeStatus,
            ) ||
            (acceptedIdentity.runId &&
              latestRunId &&
              latestRunId !== acceptedIdentity.runId) ||
            (acceptedIdentity.clientTurnId &&
              latestClientTurnId &&
              latestClientTurnId !== acceptedIdentity.clientTurnId) ||
            latestUserFingerprint !== acceptedIdentity.userFingerprint
          ) {
            terminalHydrationIdentity = null;
            return;
          }
          // The direct stream normally settles within this grace period. Its
          // transcript is already authoritative, so a durable reload here
          // would append a historical copy. An active stream is hydrated only
          // when it was already active at the terminal event and remains so
          // after the grace window—the stalled/backgrounded recovery path.
          if (
            !durableTranscriptHydrationIsRequired({
              messages: latestCurrent?.messages || [],
              primaryStreamActive:
                latestCurrent?.__primaryStreamActive === true,
              primaryWasActiveAtTerminal,
            })
          )
            return;
          void sessionApi
            .getSession(targetSessionId)
            .then((hydrated) => {
              if (!mounted) return;
              // The history request can outlive the direct SSE stream. Check
              // ownership again immediately before applying the snapshot;
              // otherwise a slow durable read can overwrite a newer live
              // transcript and duplicate already-rendered answers.
              const latestBeforeApply = matchesTarget(currentSessionRef.current)
                ? currentSessionRef.current
                : undefined;
              if (terminalHydrationIdentity !== acceptedIdentity) return;
              const latestBeforeApplyState = latestBeforeApply?.runtimeState;
              const latestBeforeApplyStatus = String(
                latestBeforeApplyState?.status || "",
              ).toLowerCase();
              const latestBeforeApplyRunId = String(
                latestBeforeApplyState?.run_id || "",
              ).trim();
              const latestBeforeApplyClientTurnId = String(
                latestBeforeApplyState?.client_turn_id || "",
              ).trim();
              const latestBeforeApplyUserFingerprint =
                latestUserTurnFingerprint(latestBeforeApply?.messages);
              if (
                !["completed", "failed", "interrupted", "stalled"].includes(
                  latestBeforeApplyStatus,
                ) ||
                (acceptedIdentity.runId &&
                  latestBeforeApplyRunId &&
                  latestBeforeApplyRunId !== acceptedIdentity.runId) ||
                (acceptedIdentity.clientTurnId &&
                  latestBeforeApplyClientTurnId &&
                  latestBeforeApplyClientTurnId !==
                    acceptedIdentity.clientTurnId) ||
                latestBeforeApplyUserFingerprint !==
                  acceptedIdentity.userFingerprint
              ) {
                return;
              }
              if (
                !durableTranscriptHydrationIsRequired({
                  messages: latestBeforeApply?.messages || [],
                  primaryStreamActive:
                    latestBeforeApply?.__primaryStreamActive === true,
                  primaryWasActiveAtTerminal,
                })
              )
                return;
              // Durable history may arrive after ordinary assistant narration
              // has already been rendered by the direct stream. Reconcile the
              // two projections instead of replacing the live transcript:
              // completed visible narration is part of the turn timeline and
              // hydration is only allowed to add missing durable events.
              const reconciledMessages = chooseActiveSessionMessages(
                latestBeforeApply?.messages || [],
                hydrated.messages,
              );
              const authoritativeRuntimeState = latestBeforeApply?.runtimeState;
              const authoritativeTurnEventPreview =
                latestBeforeApply?.turnEventPreview;
              const nextCurrent = {
                ...(latestBeforeApply || {}),
                ...hydrated,
                id: targetSessionId,
                messages: reconciledMessages,
                generating: false,
                status: "idle",
                runtimeState: authoritativeRuntimeState,
                turnEventPreview: authoritativeTurnEventPreview,
              } as ExtendedSession;
              currentSessionRef.current = nextCurrent;
              setSessions((previous) => {
                let matched = false;
                const next = previous.map((session) => {
                  const liveSession = session as ExtendedSession;
                  if (!matchesTarget(liveSession)) return session;
                  matched = true;
                  const liveState = liveSession.runtimeState;
                  const liveRunId = String(liveState?.run_id || "").trim();
                  const liveClientTurnId = String(
                    liveState?.client_turn_id || "",
                  ).trim();
                  if (
                    (acceptedIdentity.runId &&
                      liveRunId &&
                      liveRunId !== acceptedIdentity.runId) ||
                    (acceptedIdentity.clientTurnId &&
                      liveClientTurnId &&
                      liveClientTurnId !== acceptedIdentity.clientTurnId) ||
                    latestUserTurnFingerprint(liveSession.messages) !==
                      acceptedIdentity.userFingerprint
                  ) {
                    return session;
                  }
                  const liveStatus = String(
                    liveState?.status || "",
                  ).toLowerCase();
                  const liveHasTerminalState = [
                    "completed",
                    "failed",
                    "interrupted",
                    "stalled",
                  ].includes(liveStatus);
                  if (!liveHasTerminalState) return session;
                  return {
                    ...session,
                    ...hydrated,
                    id: session.id,
                    messages: chooseActiveSessionMessages(
                      liveSession.messages || [],
                      hydrated.messages,
                    ),
                    generating: false,
                    status: "idle",
                    runtimeState: liveHasTerminalState
                      ? liveState
                      : authoritativeRuntimeState,
                    turnEventPreview: liveHasTerminalState
                      ? liveSession.turnEventPreview
                      : authoritativeTurnEventPreview,
                  } as ExtendedSession;
                });
                return matched
                  ? next
                  : ([nextCurrent, ...next] as ILantaiChatSession[]);
              });
              if (terminalHydrationIdentity === acceptedIdentity) {
                terminalHydrationIdentity = null;
              }
            })
            .catch((error) => {
              if (terminalHydrationIdentity === acceptedIdentity) {
                terminalHydrationIdentity = null;
              }
              console.warn("Failed to hydrate terminal task result:", error);
            });
        }, PRIMARY_STREAM_TERMINAL_GRACE_MS);
      }
      return true;
    };

    const applyRuntimeSnapshot = (
      response: RuntimeEventsResponse,
      rawSnapshot: ILantaiChatSession,
    ) => {
      const snapshot = rawSnapshot as ExtendedSession;
      const current = matchesTarget(currentSessionRef.current)
        ? currentSessionRef.current
        : undefined;
      const snapshotState: ChatRuntimeState = snapshot.runtimeState || {
        status: isSessionRuntimeActive(snapshot) ? "running" : "idle",
      };
      const currentState = current?.runtimeState;
      const currentStatus = String(currentState?.status || "").toLowerCase();
      const snapshotStatus = String(snapshotState.status || "").toLowerCase();
      const currentRunId = String(currentState?.run_id || "").trim();
      const snapshotRunId = String(snapshotState.run_id || "").trim();
      const currentClientTurnId = String(
        currentState?.client_turn_id || "",
      ).trim();
      const snapshotClientTurnId = String(
        snapshotState.client_turn_id || "",
      ).trim();
      const identityConflict = Boolean(
        (currentRunId && snapshotRunId && currentRunId !== snapshotRunId) ||
        (currentClientTurnId &&
          snapshotClientTurnId &&
          currentClientTurnId !== snapshotClientTurnId),
      );
      // A late history row from the same logical turn cannot roll a terminal
      // projection back to running. A different run/turn identity is a real
      // new generation and the snapshot is then allowed to replace it.
      const preserveCurrentTerminal = Boolean(
        currentState &&
        TERMINAL_RUNTIME_STATUSES.has(currentStatus) &&
        !TERMINAL_RUNTIME_STATUSES.has(snapshotStatus) &&
        !identityConflict,
      );
      const authoritativeState = preserveCurrentTerminal
        ? currentState!
        : snapshotState;
      const authoritativePreview = preserveCurrentTerminal
        ? current?.turnEventPreview
        : snapshot.turnEventPreview;
      const runtimePatch = buildRuntimeEventSessionPatch(
        response,
        authoritativeState,
        authoritativePreview,
        undefined,
        {
          mirrorNativeMessages: true,
          strictTurnIdentity: true,
          identityMessages: snapshot.messages || current?.messages,
        },
      );
      const snapshotEvents =
        authoritativeState.events_preview || authoritativePreview?.events || [];
      const runtimeState: ChatRuntimeState = {
        ...runtimePatch.runtimeState,
        summary:
          authoritativeState.summary ||
          authoritativePreview?.summary_line ||
          runtimePatch.runtimeState.summary,
        events_preview: snapshotEvents,
      };
      const terminal = TERMINAL_RUNTIME_STATUSES.has(
        String(runtimeState.status || "").toLowerCase(),
      );
      const generating = !terminal && runtimeState.status === "running";
      commitMetadata(
        {
          generating,
          status: generating ? "running" : "idle",
          runtimeState,
          turnEventPreview: {
            workspace_dir:
              authoritativePreview?.workspace_dir ||
              runtimeState.workspace_dir ||
              null,
            summary_line:
              authoritativePreview?.summary_line ||
              runtimeState.summary ||
              null,
            events: snapshotEvents,
          },
        },
        "Failed to rebuild runtime state from the durable snapshot:",
        snapshot.messages,
        undefined,
        snapshot,
      );
      latestRuntimeSeq =
        typeof response.latest_seq === "number"
          ? response.latest_seq
          : latestPreviewSeq(runtimeState, authoritativePreview);
      return true;
    };

    const notifyPresentation = (response: RuntimeEventsResponse) => {
      try {
        onUpdateRef.current?.(response);
      } catch (error) {
        console.warn("Runtime event presentation update failed:", error);
      }
    };

    const applyTransportInterruption = (
      status: "reconnecting" | "closed",
      detail?: { message?: string; error?: unknown },
    ) => {
      const current = matchesTarget(currentSessionRef.current)
        ? currentSessionRef.current
        : undefined;
      if (!isSessionRuntimeActive(current)) return;
      const message =
        detail?.message ||
        (detail?.error ? streamErrorMessage(detail.error) : "") ||
        (status === "closed"
          ? "Runtime event stream closed before a terminal event."
          : "Runtime event stream disconnected.");
      const runtimeState: ChatRuntimeState = {
        ...(current?.runtimeState || { status: "running" }),
        status: "running",
        updated_at: new Date().toISOString(),
        summary: message,
        can_reconnect: true,
        can_stop: true,
        transport_status: status,
        transport_error: message,
      };
      const turnEventPreview: ChatTurnEventPreview = {
        workspace_dir:
          current?.turnEventPreview?.workspace_dir ||
          current?.runtimeState?.workspace_dir ||
          null,
        summary_line: message,
        events: (
          current?.turnEventPreview?.events ||
          current?.runtimeState?.events_preview ||
          []
        ).slice(-8),
      };
      commitMetadata(
        {
          generating: true,
          status: "running",
          runtimeState,
          turnEventPreview,
        },
        "Failed to apply runtime stream transport state:",
      );
    };

    const afterSeq = latestRuntimeSeq;
    void chatApi
      .getChatEvents(streamChatId, 0, {
        eventTypes: COLLABORATION_EVENT_TYPES,
      })
      .then((response) => {
        if (mounted) ingestCollaborationRuntimeEvents(response.events);
      })
      .catch((error) => {
        if (mounted) {
          console.warn(
            "Failed to restore collaboration event snapshots:",
            error,
          );
        }
      });
    const unsubscribe = subscribeRuntimeEventSession(
      streamChatId,
      (response) => {
        applyRuntimeResponse(response);
        notifyPresentation(response);
        return true;
      },
      {
        role: "state-applier",
        afterSeq,
        streamEpoch: initialSession?.runtimeState?.stream_epoch || "",
        maxReconnectAttempts: RUNTIME_STREAM_MAX_RECONNECT_ATTEMPTS,
        onError: (error) => {
          console.warn("Runtime event stream disconnected:", error);
          applyTransportInterruption("reconnecting", { error });
        },
        onTransportStatus: (status, detail) => {
          if (status === "reconnecting" || status === "closed") {
            applyTransportInterruption(status, detail);
          } else if (status === "connected") {
            const current = matchesTarget(currentSessionRef.current)
              ? currentSessionRef.current
              : undefined;
            const previous = current?.runtimeState;
            if (
              !isSessionRuntimeActive(current) ||
              !previous ||
              !["reconnecting", "closed"].includes(
                previous.transport_status || "",
              )
            ) {
              return;
            }
            // Reopening the event stream is transport evidence, even when the
            // model has no new ledger event yet. Preserve task state/transcript.
            commitMetadata(
              {
                runtimeState: {
                  ...previous,
                  updated_at: new Date().toISOString(),
                  transport_status: "connected",
                  transport_error: undefined,
                  summary:
                    previous.summary === previous.transport_error
                      ? undefined
                      : previous.summary,
                },
                ...(current?.turnEventPreview &&
                current.turnEventPreview.summary_line ===
                  previous.transport_error
                  ? {
                      turnEventPreview: {
                        ...current.turnEventPreview,
                        summary_line: undefined,
                      },
                    }
                  : {}),
              },
              "Failed to restore runtime stream transport state:",
            );
          }
        },
      },
    );

    // Backstop the event stream with a cheap cursor-based reconciliation.
    // This covers background-tab throttling, laptop sleep, and a connection
    // that looks open but stopped delivering events.
    const reconcile = async () => {
      if (!mounted || reconcileInFlight) return;
      const current = matchesTarget(currentSessionRef.current)
        ? currentSessionRef.current
        : undefined;
      // The direct stream and durable terminal event already settle an idle
      // conversation. Cursor reconciliation is only a recovery mechanism for
      // an active turn; polling a completed chat forever creates avoidable
      // renderer work, HTTP traffic, and backend event-store reads.
      if (!isSessionRuntimeActive(current)) return;
      reconcileInFlight = true;
      const generation = ++reconcileGeneration;
      const controller = new AbortController();
      reconcileController = controller;
      let deadlineTimer: number | null = null;
      const deadline = new Promise<never>((_resolve, reject) => {
        deadlineTimer = window.setTimeout(() => {
          if (generation === reconcileGeneration) reconcileGeneration += 1;
          controller.abort();
          reject(new Error("Runtime reconciliation timed out."));
        }, RUNTIME_RECONCILE_DEADLINE_MS);
      });
      try {
        const cursor = Math.max(
          latestRuntimeSeq,
          latestPreviewSeq(current?.runtimeState, current?.turnEventPreview),
        );
        const response = await Promise.race([
          chatApi.getChatEvents(streamChatId, cursor, {
            signal: controller.signal,
          }),
          deadline,
        ]);
        const snapshot =
          response.resync_required && response.events.length === 0
            ? await Promise.race([
                sessionApi.getSession(targetSessionId),
                deadline,
              ])
            : null;
        if (!mounted || generation !== reconcileGeneration) return;
        if (snapshot) {
          applyRuntimeSnapshot(response, snapshot);
          notifyPresentation(response);
        } else if (response.events.length > 0 || response.resync_required) {
          applyRuntimeResponse(response);
          notifyPresentation(response);
        }
      } catch (error) {
        if (mounted) console.warn("Runtime reconciliation failed:", error);
      } finally {
        if (deadlineTimer !== null) window.clearTimeout(deadlineTimer);
        if (reconcileController === controller) reconcileController = null;
        reconcileInFlight = false;
      }
    };
    const reconcileTimer = window.setInterval(
      () => void reconcile(),
      RUNTIME_RECONCILE_INTERVAL_MS,
    );
    const reconcileOnResume = () => {
      if (document.visibilityState === "visible") void reconcile();
    };
    document.addEventListener("visibilitychange", reconcileOnResume);
    window.addEventListener("focus", reconcileOnResume);

    return () => {
      mounted = false;
      reconcileGeneration += 1;
      reconcileController?.abort();
      reconcileController = null;
      reconcileInFlight = false;
      if (terminalHydrationTimer !== null) {
        window.clearTimeout(terminalHydrationTimer);
      }
      terminalHydrationIdentity = null;
      window.clearInterval(reconcileTimer);
      document.removeEventListener("visibilitychange", reconcileOnResume);
      window.removeEventListener("focus", reconcileOnResume);
      unsubscribe();
    };
  }, [
    currentSession?.id,
    currentSessionId,
    refreshKey,
    setSessions,
    streamChatId,
  ]);

  return null;
}
