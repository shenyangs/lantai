import type {
  CollaborationBatchSnapshot,
  CollaborationMemberState,
  CollaborationMemberStatus,
  ChatTurnEventPreviewEvent,
  RuntimeEvent,
} from "../../api/types/chat";
import type { RuntimeRunControlRequest } from "../../api/types/runtime";

type Listener = () => void;

const snapshotsByCall = new Map<string, CollaborationBatchSnapshot>();
const listenersByCall = new Map<string, Set<Listener>>();

const MEMBER_STATUSES = new Set<CollaborationMemberStatus>([
  "queued",
  "running",
  "retrying",
  "waiting_resource",
  "completed",
  "failed",
  "cancelled",
]);

export interface CollaborationPreparationState {
  phase: string;
  title: string;
  detail: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function finiteNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function bool(value: unknown): boolean {
  return value === true;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(text).filter(Boolean) : [];
}

function recordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value
        .map(record)
        .filter((item): item is Record<string, unknown> => Boolean(item))
    : [];
}

function memberState(value: unknown): CollaborationMemberState | null {
  const source = record(value);
  if (!source) return null;
  const memberId = text(source.member_id);
  const agentId = text(source.agent_id);
  const rawStatus = text(source.status) as CollaborationMemberStatus;
  if (!memberId || !agentId || !MEMBER_STATUSES.has(rawStatus)) return null;
  return {
    member_id: memberId,
    agent_id: agentId,
    display_name: text(source.display_name) || agentId,
    role: text(source.role),
    label: text(source.label),
    status: rawStatus,
    attempt: finiteNumber(source.attempt),
    duration_ms: finiteNumber(source.duration_ms),
    submission_ms: finiteNumber(source.submission_ms),
    remote_wait_ms: finiteNumber(source.remote_wait_ms),
    error_code: text(source.error_code) || null,
    session_id: text(source.session_id) || null,
    task_id: text(source.task_id) || null,
    execution_task_id: text(source.execution_task_id) || null,
    run_id: text(source.run_id) || null,
    chat_id: text(source.chat_id) || null,
    depends_on: stringList(source.depends_on),
    result: text(source.result) || null,
    result_truncated: bool(source.result_truncated),
    error_message: text(source.error_message) || null,
    round_completed: finiteNumber(source.round_completed) || undefined,
    review_status: text(source.review_status) || null,
    execution_mode: text(source.execution_mode) || undefined,
    permission_profile: text(source.permission_profile) || undefined,
    workspace_scope: text(source.workspace_scope) || undefined,
    allowed_tools: stringList(source.allowed_tools),
    resource_claims: stringList(source.resource_claims),
    resource_leases: recordList(source.resource_leases).map((item) => ({
      resource: text(item.resource),
      owner_id: text(item.owner_id),
      generation: finiteNumber(item.generation),
      acquired_at_epoch_seconds: finiteNumber(item.acquired_at_epoch_seconds),
      expires_at_epoch_seconds: finiteNumber(item.expires_at_epoch_seconds),
    })),
    expected_artifacts: recordList(source.expected_artifacts).map((item) => ({
      path: text(item.path),
      media_type: text(item.media_type) || undefined,
      minimum_bytes: finiteNumber(item.minimum_bytes) || undefined,
    })),
    artifacts: recordList(source.artifacts).map((item) => ({
      path: text(item.path),
      media_type: text(item.media_type),
      size_bytes: finiteNumber(item.size_bytes),
      sha256: text(item.sha256),
    })),
  };
}

function batchSnapshot(value: unknown): CollaborationBatchSnapshot | null {
  const source = record(value);
  if (!source || !Array.isArray(source.members)) return null;
  const batchId = text(source.batch_id);
  const revision = finiteNumber(source.revision);
  const members = source.members
    .map(memberState)
    .filter((member): member is CollaborationMemberState => Boolean(member));
  if (!batchId || revision < 0 || members.length === 0) return null;
  const host = record(source.host);
  const closure = record(source.closure);
  const controls = record(source.controls);
  const executionScope = record(source.execution_scope);
  const budget = record(source.budget);
  const budgetUsage = record(source.budget_usage);
  return {
    schema_version: finiteNumber(source.schema_version) || 1,
    batch_id: batchId,
    revision,
    conversation_revision: finiteNumber(source.conversation_revision),
    status: text(source.status) || "running",
    phase: text(source.phase),
    current_round: finiteNumber(source.current_round) || 1,
    participation_mode: text(source.participation_mode) || "autonomous",
    protocol: text(source.protocol) || "parallel_consultation",
    host: host
      ? {
          agent_id: text(host.agent_id) || "default",
          display_name: text(host.display_name) || "沈行知",
          role: text(host.role) || "host_and_final_owner",
          responsibilities: stringList(host.responsibilities),
        }
      : undefined,
    goal: text(source.goal),
    why_parallel: text(source.why_parallel),
    acceptance_criteria: stringList(source.acceptance_criteria),
    execution_scope:
      executionScope &&
      (text(executionScope.parent_run_id) || text(executionScope.root_run_id))
        ? {
            root_run_id: text(executionScope.root_run_id) || null,
            parent_run_id: text(executionScope.parent_run_id) || null,
            parent_attempt_id: text(executionScope.parent_attempt_id) || null,
            parent_effect_id: text(executionScope.parent_effect_id) || null,
            attempt_id: text(executionScope.attempt_id) || null,
            runtime_generation:
              executionScope.runtime_generation === undefined
                ? null
                : finiteNumber(executionScope.runtime_generation),
            chat_id: text(executionScope.chat_id) || null,
            session_id: text(executionScope.session_id) || null,
            workspace_dir: text(executionScope.workspace_dir) || null,
          }
        : undefined,
    budget: budget
      ? {
          max_member_attempts: finiteNumber(budget.max_member_attempts),
          max_wall_seconds: finiteNumber(budget.max_wall_seconds),
          reserved_host_seconds: finiteNumber(budget.reserved_host_seconds),
          max_artifact_bytes: finiteNumber(budget.max_artifact_bytes),
          max_member_response_tokens: finiteNumber(
            budget.max_member_response_tokens,
          ),
        }
      : undefined,
    budget_usage: budgetUsage
      ? {
          member_attempts_started: finiteNumber(
            budgetUsage.member_attempts_started,
          ),
          verified_artifact_bytes: finiteNumber(
            budgetUsage.verified_artifact_bytes,
          ),
          elapsed_wall_seconds: finiteNumber(budgetUsage.elapsed_wall_seconds),
          started_at_epoch_seconds:
            budgetUsage.started_at_epoch_seconds === undefined
              ? undefined
              : finiteNumber(budgetUsage.started_at_epoch_seconds),
        }
      : undefined,
    quiet_summary: text(source.quiet_summary),
    user_action_required: bool(source.user_action_required),
    unattended: source.unattended !== false,
    members,
    messages: recordList(source.messages).map((item) => ({
      message_id: text(item.message_id),
      member_id: text(item.member_id),
      display_name: text(item.display_name),
      kind: text(item.kind),
      safe_summary: text(item.safe_summary),
      base_revision: finiteNumber(item.base_revision),
      committed_revision: finiteNumber(item.committed_revision),
    })),
    held_drafts: recordList(source.held_drafts).map((item) => ({
      draft_id: text(item.draft_id),
      member_id: text(item.member_id),
      display_name: text(item.display_name),
      kind: text(item.kind),
      safe_summary: text(item.safe_summary),
      base_revision: finiteNumber(item.base_revision),
      current_revision: finiteNumber(item.current_revision),
      resolution: text(item.resolution),
    })),
    conflicts: recordList(source.conflicts).map((item) => ({
      conflict_id: text(item.conflict_id),
      safe_summary: text(item.safe_summary),
      member_ids: stringList(item.member_ids),
      status: text(item.status),
      resolution_summary: text(item.resolution_summary),
    })),
    host_decisions: recordList(source.host_decisions).map((item) => ({
      decision_id: text(item.decision_id),
      phase: text(item.phase),
      safe_summary: text(item.safe_summary),
      reason: text(item.reason),
      revision: finiteNumber(item.revision),
    })),
    closure: closure
      ? {
          outcome: text(closure.outcome),
          synthesis_ready: bool(closure.synthesis_ready),
          deliverable_ready: bool(closure.deliverable_ready),
          user_action_required: bool(closure.user_action_required),
          completed_members: finiteNumber(closure.completed_members),
          failed_members: finiteNumber(closure.failed_members),
          cancelled_members: finiteNumber(closure.cancelled_members),
          open_conflicts: finiteNumber(closure.open_conflicts),
          acceptance_met: stringList(closure.acceptance_met),
          acceptance_unmet: stringList(closure.acceptance_unmet),
          summary: text(closure.summary),
        }
      : null,
    controls: controls
      ? {
          can_takeover: bool(controls.can_takeover),
          can_stop: bool(controls.can_stop),
          can_pause: bool(controls.can_pause),
        }
      : undefined,
  };
}

export interface CollaborationRuntimeControlTarget {
  runId: string;
  request: RuntimeRunControlRequest;
}

export function collaborationRuntimeControlTarget(
  snapshot?: CollaborationBatchSnapshot,
): CollaborationRuntimeControlTarget | undefined {
  const scope = snapshot?.execution_scope;
  const runId = text(scope?.parent_run_id);
  if (!scope || !runId) return undefined;
  return {
    runId,
    request: {
      ...(text(scope.attempt_id)
        ? { expected_attempt_id: text(scope.attempt_id) }
        : {}),
      ...(text(scope.chat_id) ? { chat_id: text(scope.chat_id) } : {}),
      ...(text(scope.session_id) ? { session_id: text(scope.session_id) } : {}),
      ...(text(scope.workspace_dir)
        ? { workspace_dir: text(scope.workspace_dir) }
        : {}),
    },
  };
}

export function collaborationParentChatTarget(
  snapshot?: CollaborationBatchSnapshot,
): string {
  const scope = snapshot?.execution_scope;
  return text(scope?.chat_id) || text(scope?.session_id);
}

function callId(event: RuntimeEvent): string {
  const payload = event.payload || {};
  return (
    text(event.item_id) ||
    text(payload.id) ||
    text(payload.call_id) ||
    text(payload.tool_call_id)
  );
}

function notify(call: string): void {
  for (const listener of listenersByCall.get(call) || []) listener();
}

export function ingestCollaborationRuntimeEvents(events: RuntimeEvent[]): void {
  for (const event of events) {
    if (
      event.type !== "tool.call.started" &&
      event.type !== "tool.call.delta" &&
      event.type !== "tool.call.completed"
    ) {
      continue;
    }
    const call = callId(event);
    const snapshot = batchSnapshot(event.payload?.collaboration);
    if (!call || !snapshot) continue;
    const current = snapshotsByCall.get(call);
    if (
      current &&
      current.batch_id === snapshot.batch_id &&
      current.revision >= snapshot.revision
    ) {
      continue;
    }
    snapshotsByCall.set(call, snapshot);
    notify(call);
  }
}

export function collaborationSnapshotForCall(
  callIdValue: string,
): CollaborationBatchSnapshot | undefined {
  return snapshotsByCall.get(callIdValue);
}

export function latestCollaborationSnapshot(
  events: RuntimeEvent[],
  preferredBatchId = "",
): CollaborationBatchSnapshot | undefined {
  let latest: CollaborationBatchSnapshot | undefined;
  for (const event of events) {
    const snapshot = batchSnapshot(event.payload?.collaboration);
    if (!snapshot) continue;
    if (preferredBatchId && snapshot.batch_id !== preferredBatchId) continue;
    if (!latest || snapshot.revision > latest.revision) latest = snapshot;
  }
  return latest;
}

export function latestCollaborationPreparation(
  events: ChatTurnEventPreviewEvent[],
): CollaborationPreparationState | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const payload = record(event.payload) || {};
    const snapshot = batchSnapshot(payload.collaboration);
    if (snapshot) {
      if (
        snapshot.phase === "closed" ||
        ["completed", "partial", "failed", "cancelled"].includes(
          snapshot.status,
        )
      ) {
        return undefined;
      }
      return {
        phase: snapshot.phase || "working",
        title: "协作正在进行",
        detail:
          snapshot.quiet_summary ||
          `沈行知正在主持 ${snapshot.members.length} 位协作者`,
      };
    }

    const kind = text(payload.kind) || text(event.kind);
    if (kind === "required_capability_retrying") {
      return {
        phase: "staffing",
        title:
          text(payload.trace_title) || text(event.label) || "正在修正协作配置",
        detail:
          text(payload.trace_detail) ||
          text(event.detail) ||
          "沈行知正在重新选择成员与分工",
      };
    }
    if (kind === "collaboration_preparing") {
      return {
        phase: "preparing",
        title: "协作请求已接收",
        detail:
          text(payload.trace_detail) ||
          text(event.detail) ||
          "沈行知正在选择成员与分工",
      };
    }
  }
  return undefined;
}

export function subscribeCollaborationCall(
  callIdValue: string,
  listener: Listener,
): () => void {
  if (!callIdValue) return () => undefined;
  const listeners = listenersByCall.get(callIdValue) || new Set<Listener>();
  listeners.add(listener);
  listenersByCall.set(callIdValue, listeners);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) listenersByCall.delete(callIdValue);
  };
}

export function resetCollaborationEventStoreForTests(): void {
  snapshotsByCall.clear();
  listenersByCall.clear();
}
