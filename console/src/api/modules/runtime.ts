import { request } from "../request";
import { encodeHeaderValue } from "../authHeaders";
import type {
  RuntimeEffectRepairRequest,
  RuntimeEffectRepairResponse,
  RuntimeEffectResolutionRequest,
  RuntimeEffectResolutionResponse,
  RuntimeRunActionResponse,
  RuntimeAutonomyGrantRequest,
  RuntimeAutonomyGrantResponse,
  RuntimeAutonomyRevokeRequest,
  RuntimeRunBudgetUpdateRequest,
  RuntimeRunBudgetUpdateResponse,
  RuntimeRunControlRequest,
  RuntimeRunContinueRequest,
  RuntimeRunDeleteRequest,
  RuntimeRunExportRequest,
  RuntimeRunExportResponse,
  RuntimePreviewInspectRequest,
  RuntimeRunEventsResponse,
  RuntimeRunsResponse,
  CreateRuntimeTaskRequest,
  CreateRuntimeTaskResponse,
  RuntimeTaskBoardResponse,
  RuntimeTaskLane,
  RuntimeTaskStartResponse,
  PreviewHarnessReport,
} from "../types/runtime";

export const runtimeApi = {
  getTaskBoard: (params?: {
    view?: "board" | "history" | "all";
    source?: string;
    lane?: RuntimeTaskLane;
    query?: string;
    limit?: number;
  }) => {
    const query = new URLSearchParams();
    if (params?.view) query.set("view", params.view);
    if (params?.source) query.set("source", params.source);
    if (params?.lane) query.set("lane", params.lane);
    if (params?.query) query.set("query", params.query);
    if (params?.limit) query.set("limit", String(params.limit));
    const qs = query.toString();
    return request<RuntimeTaskBoardResponse>(
      `/runtime/task-board${qs ? `?${qs}` : ""}`,
    );
  },

  createTask: (body: CreateRuntimeTaskRequest) =>
    request<CreateRuntimeTaskResponse>("/runtime/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  startTask: (runId: string) =>
    request<RuntimeTaskStartResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/start`,
      { method: "POST", body: JSON.stringify({}) },
    ),

  listRuns: (params?: {
    source?: string;
    status?: string;
    sessionId?: string;
    agentId?: string;
    limit?: number;
  }) => {
    const query = new URLSearchParams();
    if (params?.source) query.set("source", params.source);
    if (params?.status) query.set("status", params.status);
    if (params?.sessionId) query.set("session_id", params.sessionId);
    if (params?.limit) query.set("limit", String(params.limit));
    const qs = query.toString();
    const path = `/runtime/runs${qs ? `?${qs}` : ""}`;
    return params?.agentId
      ? request<RuntimeRunsResponse>(path, {
          headers: { "X-Agent-Id": encodeHeaderValue(params.agentId) },
        })
      : request<RuntimeRunsResponse>(path);
  },

  getRunEvents: (
    runId: string,
    afterSeq = 0,
    params?: { limit?: number; attemptId?: string; agentId?: string },
  ) => {
    const query = new URLSearchParams();
    query.set("after_seq", String(afterSeq));
    if (params?.limit) query.set("limit", String(params.limit));
    if (params?.attemptId) query.set("attempt_id", params.attemptId);
    const path = `/runtime/runs/${encodeURIComponent(runId)}/events?${query.toString()}`;
    return params?.agentId
      ? request<RuntimeRunEventsResponse>(path, {
          headers: { "X-Agent-Id": encodeHeaderValue(params.agentId) },
        })
      : request<RuntimeRunEventsResponse>(path);
  },

  cancelAgentTask: (taskId: string, agentId: string) =>
    request<{ task_id: string; status: string }>(
      `/agent/process/task/${encodeURIComponent(taskId)}/cancel`,
      {
        method: "POST",
        body: JSON.stringify({}),
        headers: { "X-Agent-Id": encodeHeaderValue(agentId) },
      },
    ),

  resolveRunEffect: (
    runId: string,
    effectId: string,
    body: RuntimeEffectResolutionRequest,
  ) =>
    request<RuntimeEffectResolutionResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/effects/${encodeURIComponent(effectId)}/resolve`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  repairRunEffect: (
    runId: string,
    effectId: string,
    body: RuntimeEffectRepairRequest,
  ) =>
    request<RuntimeEffectRepairResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/effects/${encodeURIComponent(effectId)}/repair`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  interruptRun: (runId: string, body: RuntimeRunControlRequest = {}) =>
    request<RuntimeRunActionResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/interrupt`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  retryRun: (runId: string, body: RuntimeRunControlRequest = {}) =>
    request<RuntimeRunActionResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/retry`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  pauseRun: (runId: string, body: RuntimeRunControlRequest = {}) =>
    request<RuntimeRunActionResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/pause`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  resumeRun: (runId: string, body: RuntimeRunControlRequest = {}) =>
    request<RuntimeRunActionResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/resume`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  grantRunAutonomy: (runId: string, body: RuntimeAutonomyGrantRequest) =>
    request<RuntimeAutonomyGrantResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/autonomy-grant`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  revokeRunAutonomy: (runId: string, body: RuntimeAutonomyRevokeRequest) =>
    request<RuntimeAutonomyGrantResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/autonomy-grant/revoke`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  archiveRun: (runId: string, body: RuntimeRunControlRequest = {}) =>
    request<RuntimeRunActionResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/archive`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  exportRun: (runId: string, body: RuntimeRunExportRequest) =>
    request<RuntimeRunExportResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/export`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  updateRunBudget: (runId: string, body: RuntimeRunBudgetUpdateRequest) =>
    request<RuntimeRunBudgetUpdateResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/budget`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  deleteRun: (runId: string, body: RuntimeRunDeleteRequest) =>
    request<RuntimeRunActionResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}`,
      {
        method: "DELETE",
        body: JSON.stringify(body),
      },
    ),

  continueRun: (runId: string, body: RuntimeRunContinueRequest = {}) =>
    request<RuntimeRunActionResponse>(
      `/runtime/runs/${encodeURIComponent(runId)}/continue`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  inspectPreview: (body: RuntimePreviewInspectRequest) =>
    request<PreviewHarnessReport>("/runtime/preview/inspect", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
