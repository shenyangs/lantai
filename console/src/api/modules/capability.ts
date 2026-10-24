import { request } from "../request";
import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import type {
  Capability,
  CapabilityDraft,
  CapabilityPolicy,
  CapabilityRun,
  CapabilityRunRequest,
  CollaborationDraftRequest,
  CollaborationDraftStreamEvent,
  EvaluationReport,
  EvolutionProposal,
  EvolutionProposalStatus,
  Pipeline,
  PipelineRun,
  PipelineRunRequest,
  TaskRun,
} from "../types";

export const capabilityApi = {
  getPolicy: () => request<CapabilityPolicy>("/capabilities/policy"),

  listCapabilities: () => request<Capability[]>("/capabilities"),

  createCapability: (capability: Capability) =>
    request<Capability>("/capabilities", {
      method: "POST",
      body: JSON.stringify(capability),
    }),

  getCapability: (capabilityId: string) =>
    request<Capability>(`/capabilities/${encodeURIComponent(capabilityId)}`),

  updateCapability: (capabilityId: string, capability: Capability) =>
    request<Capability>(`/capabilities/${encodeURIComponent(capabilityId)}`, {
      method: "PUT",
      body: JSON.stringify(capability),
    }),

  deleteCapability: (capabilityId: string) =>
    request<void>(`/capabilities/${encodeURIComponent(capabilityId)}`, {
      method: "DELETE",
    }),

  runCapability: (capabilityId: string, payload: CapabilityRunRequest) =>
    request<CapabilityRun>(
      `/capabilities/${encodeURIComponent(capabilityId)}/run`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),

  getCapabilityRun: (runId: string) =>
    request<CapabilityRun>(`/capabilities/runs/${encodeURIComponent(runId)}`),

  listPipelines: () => request<Pipeline[]>("/pipelines"),

  createPipeline: (pipeline: Pipeline) =>
    request<Pipeline>("/pipelines", {
      method: "POST",
      body: JSON.stringify(pipeline),
    }),

  getPipeline: (pipelineId: string) =>
    request<Pipeline>(`/pipelines/${encodeURIComponent(pipelineId)}`),

  updatePipeline: (pipelineId: string, pipeline: Pipeline) =>
    request<Pipeline>(`/pipelines/${encodeURIComponent(pipelineId)}`, {
      method: "PUT",
      body: JSON.stringify(pipeline),
    }),

  deletePipeline: (pipelineId: string) =>
    request<void>(`/pipelines/${encodeURIComponent(pipelineId)}`, {
      method: "DELETE",
    }),

  runPipeline: (pipelineId: string, payload: PipelineRunRequest) =>
    request<PipelineRun>(`/pipelines/${encodeURIComponent(pipelineId)}/run`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getPipelineRun: (runId: string) =>
    request<PipelineRun>(`/pipelines/runs/${encodeURIComponent(runId)}`),

  listTaskRuns: () => request<TaskRun[]>("/task-runs"),

  listEvolutionProposals: () =>
    request<EvolutionProposal[]>("/task-runs/evolution-proposals"),

  draftCapabilityFromEvolutionProposal: (proposalId: string) =>
    request<CapabilityDraft>(
      `/task-runs/evolution-proposals/${encodeURIComponent(proposalId)}/draft`,
      { method: "POST" },
    ),

  reviewEvolutionProposal: (
    proposalId: string,
    payload: { status: EvolutionProposalStatus; capability_id?: string | null },
  ) =>
    request<EvolutionProposal>(
      `/task-runs/evolution-proposals/${encodeURIComponent(proposalId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),

  getTaskRun: (runId: string) =>
    request<TaskRun>(`/task-runs/${encodeURIComponent(runId)}`),

  listTaskRunEvaluations: (runId: string) =>
    request<EvaluationReport[]>(
      `/task-runs/${encodeURIComponent(runId)}/evaluations`,
    ),

  draftCapabilityFromTask: (runId: string) =>
    request<CapabilityDraft>(
      `/task-runs/${encodeURIComponent(runId)}/draft-capability`,
      { method: "POST" },
    ),

  streamCollaborationDraft: async (
    payload: CollaborationDraftRequest,
    options: {
      onEvent?: (event: CollaborationDraftStreamEvent) => void;
      signal?: AbortSignal;
    } = {},
  ): Promise<CapabilityDraft> => {
    const response = await fetch(
      getApiUrl("/task-runs/collaboration-drafts/stream"),
      {
        method: "POST",
        headers: {
          ...buildAuthHeaders(),
          "Content-Type": "application/json",
        },
        credentials: "same-origin",
        body: JSON.stringify(payload),
        signal: options.signal,
      },
    );
    if (!response.ok || !response.body) {
      const detail = await response.text().catch(() => "");
      throw new Error(detail || `沉淀请求失败（${response.status}）`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result: CapabilityDraft | null = null;

    const consumeFrame = (frame: string) => {
      const data = frame
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!data) return;
      const event = JSON.parse(data) as CollaborationDraftStreamEvent;
      options.onEvent?.(event);
      if (event.type === "error") {
        throw new Error(event.message || "沉淀过程未完成。");
      }
      if (event.type === "result") result = event.draft;
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() || "";
        for (const frame of frames) consumeFrame(frame);
        if (done) break;
      }
      if (buffer.trim()) consumeFrame(buffer);
    } finally {
      reader.releaseLock();
    }
    if (!result) throw new Error("沉淀已结束，但没有返回可复核草稿。");
    return result;
  },
};
