import {
  useChatAnywhereSessionsState,
  type ILantaiChatSession,
} from "@/vendor/lantaiChat";
import { Tooltip } from "antd";
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  Circle,
  CircleStop,
  Clock3,
  FilePenLine,
  FolderOpen,
  Loader2,
  PanelRightClose,
  Search,
  ShieldCheck,
  Terminal,
  ArrowDown,
  MessageSquareText,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { runtimeApi } from "../../../api/modules/runtime";
import type { ChatEventsTransportStatus } from "../../../api/modules/chat";
import type {
  ChatRuntimeState,
  CollaborationBatchSnapshot,
  RuntimeEvent,
} from "../../../api/types/chat";
import type { RuntimeRunSummary } from "../../../api/types/runtime";
import { deriveCollaborationParticipants } from "../../Control/TaskCenter/collaborationProjection";
import { BeverageAgentAvatar } from "../../../components/BeverageAgentAvatar";
import { runtimeEventToPreviewEvent } from "../runtimeEventStream";
import { subscribeRuntimeEventSession } from "../runtimeEventSubscriptionRegistry";
import {
  collaborationSnapshotForCall,
  subscribeCollaborationCall,
} from "../collaborationEventStore";
import { buildProcessTrace, type TraceStep } from "./TurnEventPreview";
import styles from "./SubAgentInspectorPanel.module.less";
import HostedCollaborationRoomPanel from "./HostedCollaborationRoomPanel";

export interface SubAgentInspectionTarget {
  id: string;
  agentId: string;
  agentName: string;
  role: string;
  task: string;
  status: string;
  result: string;
  toolName: string;
  toolCallId: string;
  memberId?: string;
  sessionId?: string;
  runId?: string;
  chatId?: string;
  taskId?: string;
  kind?: "member" | "room";
  roomSnapshot?: CollaborationBatchSnapshot;
}

interface ExtendedSession extends ILantaiChatSession {
  runtimeState?: ChatRuntimeState | null;
}

interface InspectionSnapshot {
  events: RuntimeEvent[];
  result: string;
  status: string;
  runId: string;
  attemptId: string;
  chatId: string;
  error: string;
  lastSyncedAt: string;
  transportStatus: InspectorTransportStatus;
}

type InspectorTransportStatus =
  | ChatEventsTransportStatus
  | "discovering"
  | "polling";

interface SubAgentInspectorPanelProps {
  target: SubAgentInspectionTarget;
  onClose: () => void;
  onOpenArtifacts: () => void;
  onTakeover?: () => void;
  onStop?: () => void;
}

const MAX_RUNTIME_EVENTS = 400;
const REFRESH_INTERVAL_MS = 15_000;
const INSPECTOR_WIDTH_STORAGE_KEY = "lantai:collaboration-inspector-width";
const inspectorMemory = new Map<
  string,
  { view: "process" | "result"; scrollTop: number }
>();

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function normalized(value: unknown): string {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function textField(source: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function collaborationMemberForTarget(
  event: RuntimeEvent,
  target: SubAgentInspectionTarget,
): Record<string, unknown> | null {
  const collaboration = record(event.payload?.collaboration);
  const members = Array.isArray(collaboration.members)
    ? collaboration.members
    : [];
  const targetIds = new Set(
    [target.memberId, target.agentId, target.agentName]
      .map(normalized)
      .filter(Boolean),
  );
  for (const candidate of members) {
    const member = record(candidate);
    const values = [member.member_id, member.agent_id, member.display_name].map(
      normalized,
    );
    if (values.some((value) => value && targetIds.has(value))) return member;
  }
  return null;
}

function collaborationSnapshotMemberForTarget(
  snapshot: CollaborationBatchSnapshot | undefined,
  target: SubAgentInspectionTarget,
) {
  if (!snapshot) return undefined;
  const targetIds = new Set(
    [target.memberId, target.agentId, target.agentName]
      .map(normalized)
      .filter(Boolean),
  );
  return snapshot.members.find((member) =>
    [member.member_id, member.agent_id, member.display_name]
      .map(normalized)
      .some((value) => value && targetIds.has(value)),
  );
}

export function runtimeEventMatchesSubAgent(
  event: RuntimeEvent,
  target: SubAgentInspectionTarget,
): boolean {
  const payload = record(event.payload);
  const subRun = record(payload.sub_run);
  const toolInput = record(
    payload.tool_input || payload.arguments || payload.input,
  );
  const targetIds = new Set(
    [
      target.agentId,
      target.agentName,
      target.memberId,
      target.sessionId,
      target.runId,
    ]
      .map(normalized)
      .filter(Boolean),
  );
  const eventIds = [
    event.session_id,
    event.turn_id,
    payload.run_id,
    payload.child_run_id,
    payload.session_id,
    payload.agent_id,
    payload.agent_name,
    payload.source_agent_name,
    payload.collaboration_member_id,
    subRun.run_id,
    subRun.child_run_id,
    subRun.session_id,
    subRun.agent_id,
    subRun.agent_name,
    subRun.member_id,
    toolInput.to_agent,
    toolInput.target_agent,
    toolInput.agent_id,
    toolInput.agent_name,
  ].map(normalized);
  if (eventIds.some((value) => value && targetIds.has(value))) return true;
  if (collaborationMemberForTarget(event, target)) return true;

  if (!target.memberId && target.toolCallId) {
    const callId = normalized(
      payload.call_id || payload.tool_call_id || payload.effect_id,
    );
    return Boolean(callId && callId === normalized(target.toolCallId));
  }
  return false;
}

function tryParseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed || !/^[{[]/.test(trimmed)) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function stringifyValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function unwrapText(value: unknown): string {
  const parsed = tryParseJson(value);
  if (typeof parsed === "string") return parsed.trim();
  if (typeof parsed === "number" || typeof parsed === "boolean") {
    return String(parsed);
  }
  if (Array.isArray(parsed)) {
    return parsed.map(unwrapText).filter(Boolean).join("\n\n");
  }
  if (!parsed || typeof parsed !== "object") return "";
  const source = parsed as Record<string, unknown>;
  if (Array.isArray(source.content)) {
    return source.content
      .map(record)
      .filter((block) => {
        const type = textField(block, "type").toLowerCase();
        return !["thinking", "reasoning"].includes(type);
      })
      .map((block) => unwrapText(block.text ?? block.content))
      .filter(Boolean)
      .join("\n\n");
  }
  for (const key of ["result", "output", "text", "content", "message"]) {
    if (source[key] !== undefined) return unwrapText(source[key]);
  }
  return stringifyValue(parsed);
}

function resultRowForTarget(
  value: unknown,
  target: SubAgentInspectionTarget,
): Record<string, unknown> | null {
  const parsed = tryParseJson(value);
  const source = record(parsed);
  const rows = Array.isArray(source.results) ? source.results : [];
  const targetIds = new Set(
    [target.memberId, target.agentId, target.agentName]
      .map(normalized)
      .filter(Boolean),
  );
  for (const candidate of rows) {
    const row = record(candidate);
    const values = [row.member_id, row.agent_id, row.display_name].map(
      normalized,
    );
    if (values.some((item) => item && targetIds.has(item))) return row;
  }
  return null;
}

export function resultFromSubAgentEvents(
  events: RuntimeEvent[],
  target: SubAgentInspectionTarget,
): string {
  for (const event of [...events].reverse()) {
    const payload = record(event.payload);
    const member = collaborationMemberForTarget(event, target);
    if (member) {
      const memberResult = unwrapText(
        member.result || member.result_summary || member.output,
      ).trim();
      if (memberResult) return memberResult;
      const memberError = textField(member, "error_message");
      if (memberError) return memberError;
    }
    const terminalMessage = event.type === "message.completed";
    const candidates = [
      { value: payload.result, direct: true },
      { value: payload.result_summary, direct: true },
      { value: record(payload.sub_run).result_summary, direct: true },
      { value: payload.output, direct: terminalMessage },
      { value: payload.message, direct: terminalMessage },
    ];
    for (const { value: candidate, direct } of candidates) {
      const row = resultRowForTarget(candidate, target);
      if (row) {
        return unwrapText(row.result || row.error).trim();
      }
      if (direct && runtimeEventMatchesSubAgent(event, target)) {
        const text = unwrapText(candidate)
          .replace(/^\[(?:SESSION|TASK_ID|STATUS):[^\]]+\]\s*$/gm, "")
          .replace(
            /^(?:Task completed\.|Task submitted successfully\.)\s*$/gim,
            "",
          )
          .replace(/\n{3,}/g, "\n\n")
          .trim();
        if (text) return text;
      }
    }
  }
  return target.result.trim();
}

function runtimeEventKind(event: RuntimeEvent): string {
  return textField(record(event.payload), "legacy_kind") || event.type || "";
}

function isMessageLifecycleEvent(event: RuntimeEvent): boolean {
  const kind = runtimeEventKind(event).toLowerCase();
  const type = String(event.type || "").toLowerCase();
  return type.startsWith("message.") || kind.startsWith("chat.message.");
}

function isMessageCompletionEvent(event: RuntimeEvent): boolean {
  const kind = runtimeEventKind(event).toLowerCase();
  return (
    String(event.type || "").toLowerCase() === "message.completed" ||
    kind === "chat.message.completed" ||
    kind === "chat.final_answer"
  );
}

function publicProgressForEvent(
  event: RuntimeEvent,
): Record<string, unknown> | null {
  const progress = record(event.payload?.public_progress);
  return textField(progress, "id") ? progress : null;
}

function messageTextForContentType(
  event: RuntimeEvent,
  contentType: "thinking" | "text",
): string {
  const message = record(event.payload?.message);
  if (typeof message.content === "string") {
    return contentType === "text" ? message.content.trim() : "";
  }
  if (!Array.isArray(message.content)) return "";
  return message.content
    .map(record)
    .filter((block) => textField(block, "type") === contentType)
    .map((block) => textField(block, "text", "thinking"))
    .filter(Boolean)
    .join("");
}

function reconcilePublicProgressText(
  current: string,
  incoming: string,
): string {
  if (!incoming) return current;
  if (!current || incoming.startsWith(current)) return incoming;
  if (current.startsWith(incoming) || current.endsWith(incoming))
    return current;
  return `${current}${incoming}`;
}

/**
 * The member inspector is a semantic activity log, not a raw token ledger.
 * Only Runtime-authored public progress may surface as thought-like content;
 * provider reasoning without that marker remains private. Normal answer deltas
 * belong to the result projection and must never become one timeline row each.
 */
export function semanticSubAgentProcessEvents(
  events: RuntimeEvent[],
): RuntimeEvent[] {
  const semantic: RuntimeEvent[] = [];
  const publicProgressIndex = new Map<string, number>();
  let resultIndex = -1;

  for (const event of [...events].sort((left, right) => left.seq - right.seq)) {
    if (!isMessageLifecycleEvent(event)) {
      if (event.type === "turn.completed" && resultIndex >= 0) continue;
      semantic.push(event);
      continue;
    }

    const publicProgress = publicProgressForEvent(event);
    if (publicProgress) {
      const progressId = textField(publicProgress, "id");
      const payload = record(event.payload);
      const progressText =
        messageTextForContentType(event, "thinking") ||
        textField(payload, "delta", "summary");
      if (progressText) {
        const safePayload = { ...payload };
        delete safePayload.message;
        delete safePayload.delta;
        const existingIndex = publicProgressIndex.get(progressId);
        const previous =
          existingIndex === undefined ? undefined : semantic[existingIndex];
        const previousDetail = previous
          ? textField(record(previous.payload), "trace_detail")
          : "";
        const projected: RuntimeEvent = {
          ...event,
          id: `public-progress:${progressId}`,
          type: "reasoning.progress",
          status: isMessageCompletionEvent(event)
            ? "completed"
            : event.status || "running",
          payload: {
            ...safePayload,
            phase: "model",
            trace_title: "思考",
            trace_detail: reconcilePublicProgressText(
              previousDetail,
              progressText,
            ),
          },
        };
        if (existingIndex === undefined) {
          publicProgressIndex.set(progressId, semantic.length);
          semantic.push(projected);
        } else {
          semantic[existingIndex] = projected;
        }
      }
    }

    if (!isMessageCompletionEvent(event)) continue;
    const resultText = messageTextForContentType(event, "text");
    if (!resultText) continue;
    const safePayload = { ...record(event.payload) };
    delete safePayload.message;
    delete safePayload.delta;
    const projected: RuntimeEvent = {
      ...event,
      id: `result:${event.turn_id || event.id}`,
      type: "message.completed",
      status: "completed",
      payload: {
        ...safePayload,
        phase: "result",
        trace_title: "结果已形成",
        trace_detail: "完整内容见结果页",
      },
    };
    if (resultIndex < 0) {
      resultIndex = semantic.length;
      semantic.push(projected);
    } else {
      semantic[resultIndex] = projected;
    }
  }

  return semantic;
}

function statusFromSubAgentEvents(
  events: RuntimeEvent[],
  target: SubAgentInspectionTarget,
): string {
  for (const event of [...events].reverse()) {
    const member = collaborationMemberForTarget(event, target);
    if (member) {
      const status = textField(member, "status");
      if (status) return status;
    }
    if (!runtimeEventMatchesSubAgent(event, target)) continue;
    const subRun = record(event.payload?.sub_run);
    const status =
      textField(subRun, "status") ||
      textField(record(event.payload), "status", "final_task_status") ||
      String(event.status || "").trim();
    if (status) return status;
  }
  return target.status;
}

export function findSubAgentRun(
  runs: RuntimeRunSummary[],
  target: SubAgentInspectionTarget,
  parentRunId: string,
  parentEvents: RuntimeEvent[],
): RuntimeRunSummary | null {
  if (target.runId) {
    const exact = runs.find((run) => run.id === target.runId);
    if (exact) return exact;
  }
  if (target.sessionId) {
    const bySession = runs.find(
      (run) => normalized(run.session_id) === normalized(target.sessionId),
    );
    if (bySession) return bySession;
  }
  if (parentRunId) {
    const participant = deriveCollaborationParticipants(
      parentEvents,
      parentRunId,
    ).find((item) =>
      [item.id, item.agentName]
        .map(normalized)
        .some((value) =>
          [target.memberId, target.agentId, target.agentName]
            .map(normalized)
            .includes(value),
        ),
    );
    if (participant?.runId) {
      return runs.find((run) => run.id === participant.runId) || null;
    }
  }
  return null;
}

function collapseTraceSteps(steps: TraceStep[]): TraceStep[] {
  const order: string[] = [];
  const byKey = new Map<string, TraceStep>();
  for (const step of steps) {
    const payload = record(step.debug.payload);
    const lifecycleKey = textField(
      payload,
      "call_id",
      "tool_call_id",
      "effect_id",
      "message_id",
      "checkpoint_id",
    );
    const key = lifecycleKey ? `${step.phase}:${lifecycleKey}` : step.id;
    if (!byKey.has(key)) order.push(key);
    byKey.set(key, step);
  }
  return order.flatMap((key) => {
    const step = byKey.get(key);
    return step ? [step] : [];
  });
}

function traceSteps(events: RuntimeEvent[]): TraceStep[] {
  if (!events.length) return [];
  const semanticEvents = semanticSubAgentProcessEvents(events);
  const preview = {
    summary_line: "协作执行记录",
    events: semanticEvents.map(runtimeEventToPreviewEvent),
  };
  return collapseTraceSteps(buildProcessTrace(preview).steps);
}

function mergeRuntimeEvents(
  existing: RuntimeEvent[],
  incoming: RuntimeEvent[],
): RuntimeEvent[] {
  const byId = new Map<string, RuntimeEvent>();
  for (const event of [...existing, ...incoming]) byId.set(event.id, event);
  return [...byId.values()]
    .sort((left, right) => left.seq - right.seq)
    .slice(-MAX_RUNTIME_EVENTS);
}

export function runtimeEventBelongsToChildRun(
  event: RuntimeEvent,
  runId: string,
  sessionId: string,
): boolean {
  if (sessionId && normalized(event.session_id) === normalized(sessionId)) {
    return true;
  }
  const payload = record(event.payload);
  const eventRunId =
    textField(payload, "run_id", "turn_id") || event.turn_id || "";
  return Boolean(runId && normalized(eventRunId) === normalized(runId));
}

function statusFromChildRuntimeEvents(
  events: RuntimeEvent[],
  fallback: string,
): string {
  for (const event of [...events].reverse()) {
    if (event.type === "turn.completed") return "completed";
    if (event.type === "turn.failed") return "failed";
    if (event.type === "turn.interrupted") return "interrupted";
    if (event.type === "turn.stalled") return "stalled";
  }
  return events.length ? "running" : fallback;
}

function statusLabel(status: string): string {
  const normalizedStatus = status.toLowerCase();
  if (["running", "started", "progress"].includes(normalizedStatus)) {
    return "工作中";
  }
  if (["submitted", "queued", "pending"].includes(normalizedStatus)) {
    return "等待中";
  }
  if (
    ["completed", "finished", "done", "succeeded"].includes(normalizedStatus)
  ) {
    return "已完成";
  }
  if (["failed", "error"].includes(normalizedStatus)) return "失败";
  if (["cancelled", "interrupted"].includes(normalizedStatus)) return "已中断";
  if (normalizedStatus === "stalled") return "等待处理";
  return status || "已记录";
}

function isRunningStatus(status: string): boolean {
  return [
    "running",
    "started",
    "progress",
    "submitted",
    "queued",
    "pending",
  ].includes(status.toLowerCase());
}

function statusIcon(status: string): LucideIcon {
  const normalizedStatus = status.toLowerCase();
  if (isRunningStatus(status)) return Loader2;
  if (
    ["failed", "error", "cancelled", "interrupted"].includes(normalizedStatus)
  ) {
    return AlertCircle;
  }
  return CheckCircle2;
}

function stepIcon(step: TraceStep): LucideIcon {
  if (/failed|error|stalled|interrupted/i.test(step.status)) return AlertCircle;
  if (step.phase === "search") return Search;
  if (step.phase === "command") return Terminal;
  if (step.phase === "file" || step.phase === "artifact") return FilePenLine;
  if (step.phase === "approval") return ShieldCheck;
  if (step.phase === "subtask") return Bot;
  if (step.phase === "tool") return Wrench;
  return CheckCircle2;
}

function formatDuration(durationMs?: number | null): string {
  if (!durationMs || durationMs < 1) return "";
  const seconds = Math.round(durationMs / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function MemberSubAgentInspectorPanel({
  target,
  onClose,
  onOpenArtifacts,
  onTakeover,
  onStop,
}: SubAgentInspectorPanelProps) {
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  const subscribeToRoom = useCallback(
    (listener: () => void) =>
      subscribeCollaborationCall(target.toolCallId, listener),
    [target.toolCallId],
  );
  const roomSnapshot = useSyncExternalStore(
    subscribeToRoom,
    () => collaborationSnapshotForCall(target.toolCallId),
    () => undefined,
  );
  const liveMember = useMemo(
    () => collaborationSnapshotMemberForTarget(roomSnapshot, target),
    [roomSnapshot, target],
  );
  const effectiveTarget = useMemo<SubAgentInspectionTarget>(
    () => ({
      id: target.id,
      agentId: target.agentId,
      agentName: target.agentName,
      role: target.role,
      task: target.task,
      status: liveMember?.status || target.status,
      result: liveMember?.result || liveMember?.error_message || target.result,
      toolName: target.toolName,
      toolCallId: target.toolCallId,
      memberId: target.memberId,
      sessionId: liveMember?.session_id || target.sessionId,
      runId: liveMember?.run_id || target.runId,
      chatId: liveMember?.chat_id || target.chatId,
      kind: target.kind,
      roomSnapshot: target.roomSnapshot,
    }),
    [
      liveMember?.chat_id,
      liveMember?.error_message,
      liveMember?.result,
      liveMember?.run_id,
      liveMember?.session_id,
      liveMember?.status,
      target.agentId,
      target.agentName,
      target.chatId,
      target.id,
      target.kind,
      target.memberId,
      target.result,
      target.role,
      target.roomSnapshot,
      target.runId,
      target.sessionId,
      target.status,
      target.task,
      target.toolCallId,
      target.toolName,
    ],
  );
  const [view, setView] = useState<"process" | "result">(
    () => inspectorMemory.get(target.id)?.view || "process",
  );
  const [loading, setLoading] = useState(true);
  const [followLatest, setFollowLatest] = useState(true);
  const [panelWidth, setPanelWidth] = useState(() => {
    const stored = Number(
      window.localStorage.getItem(INSPECTOR_WIDTH_STORAGE_KEY),
    );
    return Number.isFinite(stored) && stored >= 380 && stored <= 640
      ? stored
      : 440;
  });
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [snapshot, setSnapshot] = useState<InspectionSnapshot>({
    events: [],
    result: target.result,
    status: target.status,
    runId: target.runId || "",
    attemptId: "",
    chatId: "",
    error: "",
    lastSyncedAt: "",
    transportStatus: "discovering",
  });
  const snapshotRef = useRef(snapshot);
  const targetRef = useRef(target);
  snapshotRef.current = snapshot;
  targetRef.current = effectiveTarget;

  const currentSession = useMemo(
    () =>
      sessions.find((session) => session.id === currentSessionId) as
        | ExtendedSession
        | undefined,
    [currentSessionId, sessions],
  );
  const parentRunId = currentSession?.runtimeState?.run_id?.trim() || "";

  useEffect(() => {
    const remembered = inspectorMemory.get(target.id);
    const currentTarget = targetRef.current;
    setView(remembered?.view || "process");
    setFollowLatest(true);
    setSnapshot({
      events: [],
      result: currentTarget.result,
      status: currentTarget.status,
      runId: currentTarget.runId || "",
      attemptId: "",
      chatId: currentTarget.chatId || "",
      error: "",
      lastSyncedAt: "",
      transportStatus: currentTarget.chatId ? "connecting" : "discovering",
    });
  }, [target.id]);

  useEffect(() => {
    setSnapshot((current) => {
      const nextChatId = effectiveTarget.chatId || current.chatId;
      return {
        ...current,
        result: effectiveTarget.result || current.result,
        status: effectiveTarget.status || current.status,
        runId: effectiveTarget.runId || current.runId,
        chatId: nextChatId,
        transportStatus:
          nextChatId && nextChatId !== current.chatId
            ? "connecting"
            : current.transportStatus,
      };
    });
  }, [
    effectiveTarget.chatId,
    effectiveTarget.result,
    effectiveTarget.runId,
    effectiveTarget.status,
  ]);

  useEffect(() => {
    let disposed = false;
    let firstLoad = true;
    let shouldRefresh = isRunningStatus(effectiveTarget.status);
    let missingResultRefreshes = 0;
    let timer: number | null = null;

    const load = async () => {
      if (firstLoad) setLoading(true);
      try {
        const [runsResponse, parentResponse] = await Promise.all([
          runtimeApi
            .listRuns({
              source: "chat",
              sessionId: effectiveTarget.sessionId,
              agentId: effectiveTarget.agentId,
              limit: effectiveTarget.sessionId ? 20 : 100,
            })
            .catch(() => ({
              total: 0,
              items: [],
            })),
          parentRunId
            ? runtimeApi
                .getRunEvents(parentRunId, 0, { limit: MAX_RUNTIME_EVENTS })
                .catch(() => null)
            : Promise.resolve(null),
        ]);
        const parentEvents = parentResponse?.events || [];
        const childRun = findSubAgentRun(
          runsResponse.items,
          effectiveTarget,
          parentRunId,
          parentEvents,
        );
        const childResponse = childRun?.id
          ? await runtimeApi
              .getRunEvents(childRun.id, 0, {
                limit: MAX_RUNTIME_EVENTS,
                agentId: effectiveTarget.agentId,
              })
              .catch(() => null)
          : null;
        const childEvents = childResponse?.events || [];
        const visibleEvents = childEvents.length
          ? childEvents
          : parentEvents.filter((event) =>
              runtimeEventMatchesSubAgent(event, effectiveTarget),
            );
        if (disposed) return;
        const nextStatus =
          childRun?.status ||
          statusFromSubAgentEvents(parentEvents, effectiveTarget) ||
          effectiveTarget.status;
        const nextResult =
          resultFromSubAgentEvents(
            [...parentEvents, ...childEvents],
            effectiveTarget,
          ) || effectiveTarget.result;
        const completedWithoutResult =
          ["completed", "finished", "done", "succeeded"].includes(
            nextStatus.toLowerCase(),
          ) && !nextResult;
        if (completedWithoutResult) missingResultRefreshes += 1;
        else missingResultRefreshes = 0;
        shouldRefresh =
          isRunningStatus(nextStatus) ||
          (completedWithoutResult && missingResultRefreshes < 8);
        setSnapshot((current) => {
          const childChatId = childRun?.chat_id || effectiveTarget.chatId || "";
          return {
            events: visibleEvents,
            result: nextResult,
            status: nextStatus,
            runId: childRun?.id || effectiveTarget.runId || "",
            attemptId: childRun?.attempt_id || "",
            chatId: childChatId,
            error: "",
            lastSyncedAt: new Date().toISOString(),
            transportStatus:
              childChatId && current.chatId === childChatId
                ? current.transportStatus
                : childChatId
                  ? "connecting"
                  : isRunningStatus(nextStatus)
                    ? "discovering"
                    : "polling",
          };
        });
      } catch (error) {
        if (disposed) return;
        setSnapshot((current) => ({
          ...current,
          error:
            error instanceof Error ? error.message : "暂时无法读取执行记录",
        }));
      } finally {
        if (!disposed) setLoading(false);
        firstLoad = false;
      }
    };

    const poll = async () => {
      await load();
      if (!disposed && shouldRefresh) {
        timer = window.setTimeout(() => void poll(), REFRESH_INTERVAL_MS);
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [effectiveTarget, parentRunId]);

  useEffect(() => {
    if (!snapshot.chatId) return;
    const afterSeq = snapshotRef.current.events.reduce(
      (latest, event) => Math.max(latest, event.seq || 0),
      0,
    );
    const unsubscribe = subscribeRuntimeEventSession(
      snapshot.chatId,
      (response) => {
        const incoming = response.events.filter((event) =>
          runtimeEventBelongsToChildRun(
            event,
            snapshot.runId,
            targetRef.current.sessionId || "",
          ),
        );
        if (!incoming.length) return;
        setSnapshot((current) => {
          if (current.chatId !== response.chat_id) return current;
          const events = mergeRuntimeEvents(current.events, incoming);
          return {
            ...current,
            events,
            result:
              resultFromSubAgentEvents(events, targetRef.current) ||
              current.result,
            status: statusFromChildRuntimeEvents(events, current.status),
            error: "",
            lastSyncedAt: new Date().toISOString(),
            transportStatus: "connected",
          };
        });
      },
      {
        role: "observer",
        agentId: target.agentId,
        afterSeq,
        onError: (error) => {
          setSnapshot((current) => ({
            ...current,
            error:
              error instanceof Error
                ? error.message
                : "实时连接暂时中断，正在保留并重连已有记录。",
            transportStatus: "reconnecting",
          }));
        },
        onTransportStatus: (status, detail) => {
          setSnapshot((current) => ({
            ...current,
            transportStatus: status,
            error:
              status === "closed"
                ? detail?.message || "实时连接已关闭，已保留最后同步记录。"
                : status === "connected"
                  ? ""
                  : current.error,
          }));
        },
      },
    );
    return unsubscribe;
  }, [
    snapshot.chatId,
    snapshot.runId,
    target.agentId,
    target.id,
    target.sessionId,
  ]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body || view !== "process") return;
    const remembered = inspectorMemory.get(target.id);
    if (
      remembered &&
      remembered.scrollTop > 0 &&
      snapshot.events.length === 0
    ) {
      body.scrollTop = remembered.scrollTop;
      return;
    }
    if (followLatest) body.scrollTop = body.scrollHeight;
  }, [followLatest, snapshot.events.length, target.id, view]);

  const steps = useMemo(() => traceSteps(snapshot.events), [snapshot.events]);
  const StatusIcon = statusIcon(snapshot.status);
  const syncLabel =
    snapshot.transportStatus === "reconnecting"
      ? "连接中断，正在重连"
      : snapshot.transportStatus === "discovering"
        ? "正在定位该协作者的实时记录"
        : snapshot.transportStatus === "polling"
          ? "已从运行台账同步"
          : snapshot.transportStatus === "closed" &&
              isRunningStatus(snapshot.status)
            ? "实时连接已关闭，显示最后记录"
            : snapshot.lastSyncedAt
              ? `刚刚同步 · ${new Date(
                  snapshot.lastSyncedAt,
                ).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                })}`
              : "正在建立实时连接";

  const beginResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (window.matchMedia("(max-width: 1100px)").matches) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth;
    const move = (moveEvent: PointerEvent) => {
      const viewportLimit = Math.max(380, window.innerWidth - 520);
      const next = Math.min(
        640,
        viewportLimit,
        Math.max(380, startWidth + startX - moveEvent.clientX),
      );
      setPanelWidth(next);
    };
    const finish = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish, { once: true });
  };

  useEffect(() => {
    window.localStorage.setItem(
      INSPECTOR_WIDTH_STORAGE_KEY,
      String(Math.round(panelWidth)),
    );
  }, [panelWidth]);

  return (
    <aside
      className={styles.panel}
      style={{ "--inspector-width": `${panelWidth}px` } as CSSProperties}
      aria-label={`${target.agentName}的协作详情`}
      data-testid="sub-agent-inspector"
    >
      <div
        className={styles.resizeHandle}
        role="separator"
        aria-label="调整协作详情宽度"
        aria-orientation="vertical"
        tabIndex={0}
        onPointerDown={beginResize}
        onKeyDown={(event) => {
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          event.preventDefault();
          setPanelWidth((current) =>
            Math.min(
              640,
              Math.max(380, current + (event.key === "ArrowLeft" ? 24 : -24)),
            ),
          );
        }}
      />
      <header className={styles.header}>
        <span className={styles.avatar} aria-hidden="true">
          <BeverageAgentAvatar agentId={target.agentId} size={38} />
        </span>
        <span className={styles.identity}>
          <small>协作详情</small>
          <strong>{target.agentName}</strong>
          <em>{target.role || "专业协作智能体"}</em>
        </span>
        <span
          className={styles.status}
          data-running={isRunningStatus(snapshot.status) ? "true" : "false"}
        >
          <StatusIcon
            size={14}
            className={isRunningStatus(snapshot.status) ? styles.spin : ""}
            aria-hidden="true"
          />
          {statusLabel(snapshot.status)}
        </span>
        <Tooltip title="查看本轮产物">
          <button
            type="button"
            className={styles.iconButton}
            aria-label="查看本轮产物"
            onClick={onOpenArtifacts}
          >
            <FolderOpen size={16} aria-hidden="true" />
          </button>
        </Tooltip>
        <Tooltip title="收起协作详情">
          <button
            type="button"
            className={styles.iconButton}
            aria-label="收起协作详情"
            onClick={onClose}
          >
            <PanelRightClose size={17} aria-hidden="true" />
          </button>
        </Tooltip>
      </header>

      <section className={styles.taskBlock}>
        <small>任务</small>
        <p>{target.task || "处理本次专业协作任务"}</p>
      </section>

      <nav className={styles.tabs} aria-label="协作详情视图">
        <button
          type="button"
          className={view === "process" ? styles.tabActive : ""}
          aria-current={view === "process" ? "page" : undefined}
          onClick={() => {
            setView("process");
            inspectorMemory.set(target.id, {
              view: "process",
              scrollTop: inspectorMemory.get(target.id)?.scrollTop || 0,
            });
          }}
        >
          过程
          {steps.length ? <span>{steps.length}</span> : null}
        </button>
        <button
          type="button"
          className={view === "result" ? styles.tabActive : ""}
          aria-current={view === "result" ? "page" : undefined}
          onClick={() => {
            setView("result");
            inspectorMemory.set(target.id, {
              view: "result",
              scrollTop: inspectorMemory.get(target.id)?.scrollTop || 0,
            });
          }}
        >
          结果
          {snapshot.result ? (
            <CheckCircle2 size={13} aria-hidden="true" />
          ) : null}
        </button>
      </nav>

      <div
        className={styles.body}
        ref={bodyRef}
        onScroll={(event) => {
          const body = event.currentTarget;
          const nearBottom =
            body.scrollHeight - body.scrollTop - body.clientHeight < 36;
          setFollowLatest(nearBottom);
          inspectorMemory.set(target.id, {
            view,
            scrollTop: body.scrollTop,
          });
        }}
      >
        {view === "process" ? (
          <div className={styles.processView} aria-live="polite">
            <div className={styles.processSummary}>
              <span className={styles.summaryIcon}>
                <StatusIcon
                  size={17}
                  className={
                    isRunningStatus(snapshot.status) ? styles.spin : ""
                  }
                  aria-hidden="true"
                />
              </span>
              <span>
                <strong>{statusLabel(snapshot.status)}</strong>
                <em>
                  {isRunningStatus(snapshot.status)
                    ? "正在推进这项专业任务"
                    : snapshot.result
                      ? "结果已回到当前对话"
                      : "本次协作已记录"}
                </em>
              </span>
            </div>

            <div
              className={styles.syncState}
              data-state={snapshot.transportStatus}
            >
              <span>{syncLabel}</span>
              <em>展示公开思考、工作摘要与工具状态；私密思维链不会外显。</em>
            </div>

            {snapshot.error ? (
              <div className={styles.notice}>
                <AlertCircle size={14} aria-hidden="true" />
                <span>{snapshot.error}</span>
              </div>
            ) : null}

            {loading && !steps.length ? (
              <div className={styles.loadingState}>
                <Loader2 className={styles.spin} size={18} aria-hidden="true" />
                <span>正在同步协作记录…</span>
              </div>
            ) : steps.length ? (
              <div className={styles.timeline}>
                {steps.map((step) => {
                  const Icon = stepIcon(step);
                  const duration = formatDuration(step.durationMs);
                  const running = /running|started|progress/i.test(step.status);
                  return (
                    <div className={styles.timelineItem} key={step.id}>
                      <span className={styles.timelineMarker}>
                        <Icon
                          size={14}
                          className={running ? styles.spin : ""}
                          aria-hidden="true"
                        />
                      </span>
                      <span className={styles.timelineCopy}>
                        <span className={styles.timelineHeading}>
                          <strong>{step.title}</strong>
                          <em>{duration || step.time}</em>
                        </span>
                        {step.detail ? <small>{step.detail}</small> : null}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className={styles.quietState}>
                <Circle size={16} aria-hidden="true" />
                <span>
                  <strong>等待更多过程记录</strong>
                  <em>当前已收到任务状态，结果会在这里继续更新。</em>
                </span>
              </div>
            )}
          </div>
        ) : snapshot.result ? (
          <article className={styles.resultView}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {snapshot.result}
            </ReactMarkdown>
          </article>
        ) : (
          <div className={styles.emptyResult}>
            <Clock3 size={20} aria-hidden="true" />
            <strong>
              {statusLabel(snapshot.status) === "已完成"
                ? "正在同步已完成结果"
                : "结果还在准备中"}
            </strong>
            <span>
              {statusLabel(snapshot.status) === "已完成"
                ? "已收到完成状态，正在从运行台账恢复公开结果。"
                : "协作完成后会自动出现在这里。"}
            </span>
          </div>
        )}
        {!followLatest && view === "process" ? (
          <button
            type="button"
            className={styles.jumpLatest}
            onClick={() => {
              setFollowLatest(true);
              if (bodyRef.current) {
                bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
              }
            }}
          >
            <ArrowDown size={13} aria-hidden="true" />
            回到最新
          </button>
        ) : null}
      </div>

      {onTakeover || (onStop && target.taskId) ? (
        <footer className={styles.footer}>
          {onTakeover ? (
            <button type="button" onClick={onTakeover}>
              <MessageSquareText size={13} aria-hidden="true" />
              给沈行知调整意见
            </button>
          ) : null}
          {onStop && target.taskId && isRunningStatus(snapshot.status) ? (
            <button type="button" onClick={onStop} aria-label="停止该协作者">
              <CircleStop size={13} aria-hidden="true" />
              停止该协作者
            </button>
          ) : null}
        </footer>
      ) : null}
    </aside>
  );
}

export default function SubAgentInspectorPanel(
  props: SubAgentInspectorPanelProps,
) {
  if (props.target.kind === "room" && props.target.roomSnapshot) {
    return (
      <HostedCollaborationRoomPanel
        callId={props.target.toolCallId || props.target.roomSnapshot.batch_id}
        initialSnapshot={props.target.roomSnapshot}
        onClose={props.onClose}
        onTakeover={props.onTakeover}
      />
    );
  }
  return <MemberSubAgentInspectorPanel {...props} />;
}
