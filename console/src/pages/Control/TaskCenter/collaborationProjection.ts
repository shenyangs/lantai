import type { CollaborationBatchSnapshot } from "../../../api/types/chat";
import type { RuntimeEvent } from "../../../api/types/runtime";
import { latestCollaborationSnapshot } from "../../Chat/collaborationEventStore";

export interface CollaborationParticipant {
  id: string;
  runId: string | null;
  agentName: string;
  role: string;
  delegationReason: string;
  permissionProfile: string;
  executionMode?: string;
  workspaceScope?: string;
  allowedTools?: string[];
  executionTaskId?: string;
  resourceClaims?: string[];
  resourceLeases?: Array<{
    resource: string;
    owner_id: string;
    generation: number;
    acquired_at_epoch_seconds: number;
    expires_at_epoch_seconds: number;
  }>;
  artifacts?: Array<{
    path: string;
    media_type: string;
    size_bytes: number;
    sha256: string;
  }>;
  contextSummary: string;
  currentTool: string;
  status: string;
  resultSummary: string;
  failureReason: string;
  eventCount: number;
  updatedAt: string;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function text(
  primary: Record<string, unknown>,
  secondary: Record<string, unknown>,
  ...keys: string[]
): string {
  for (const source of [primary, secondary]) {
    for (const key of keys) {
      const value = source[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  return "";
}

function childRunId(
  event: RuntimeEvent,
  parentRunId: string,
  payload: Record<string, unknown>,
  subRun: Record<string, unknown>,
): string | null {
  const candidate =
    text(payload, subRun, "child_run_id", "run_id", "sub_run_id") ||
    String(event.turn_id || "").trim();
  return candidate && candidate !== parentRunId ? candidate : null;
}

export function deriveCollaborationParticipants(
  events: RuntimeEvent[],
  parentRunId: string,
): CollaborationParticipant[] {
  const byId = new Map<string, CollaborationParticipant>();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const payload = record(event.payload);
    const subRun = record(payload.sub_run);
    const isSubtask =
      String(payload.phase || "").toLowerCase() === "subtask" ||
      event.presentation?.surface === "subagent" ||
      Object.keys(subRun).length > 0;
    if (!isSubtask) continue;

    const runId = childRunId(event, parentRunId, payload, subRun);
    const agentName =
      text(payload, subRun, "agent_name", "source_agent_name") || "协作者";
    const role = text(payload, subRun, "agent_role", "role");
    const currentTool = text(payload, subRun, "current_tool", "tool_name");
    const identity =
      runId ||
      [agentName, role, currentTool, text(payload, subRun, "delegation_reason")]
        .filter(Boolean)
        .join(":") ||
      event.id;
    const previous = byId.get(identity);
    const nextValue = (
      value: string,
      previousValue: string | undefined,
    ): string => value || previousValue || "";

    byId.set(identity, {
      id: identity,
      runId: runId || previous?.runId || null,
      agentName: nextValue(agentName, previous?.agentName),
      role: nextValue(
        text(payload, subRun, "agent_role", "role"),
        previous?.role,
      ),
      delegationReason: nextValue(
        text(payload, subRun, "delegation_reason", "reason"),
        previous?.delegationReason,
      ),
      permissionProfile: nextValue(
        text(payload, subRun, "permission_profile"),
        previous?.permissionProfile,
      ),
      contextSummary: nextValue(
        text(payload, subRun, "inherited_context_summary", "context_summary"),
        previous?.contextSummary,
      ),
      currentTool: nextValue(
        text(payload, subRun, "current_tool", "tool_name"),
        previous?.currentTool,
      ),
      status: nextValue(
        text(subRun, payload, "status") || String(event.status || "").trim(),
        previous?.status,
      ),
      resultSummary: nextValue(
        text(payload, subRun, "result_summary"),
        previous?.resultSummary,
      ),
      failureReason: nextValue(
        text(payload, subRun, "failure_reason", "error"),
        previous?.failureReason,
      ),
      eventCount: (previous?.eventCount || 0) + 1,
      updatedAt: event.created_at || previous?.updatedAt || "",
    });
  }
  return [...byId.values()].sort((a, b) =>
    b.updatedAt.localeCompare(a.updatedAt),
  );
}

export function deriveHostedCollaborationProjection(
  events: RuntimeEvent[],
): CollaborationBatchSnapshot | undefined {
  return latestCollaborationSnapshot(events);
}
