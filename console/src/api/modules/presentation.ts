import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import { request } from "../request";

export type PresentationJobStatus =
  | "pending"
  | "running"
  | "paused"
  | "completed"
  | "cancelled"
  | "failed";

export interface PresentationJobRequest {
  conversation_id: string;
  user_request: string;
  audience?: string;
  slide_count?: number;
  language?: string;
  mode?: "stable" | "intelligent" | "custom_template";
  theme_id?: string;
  model_id?: string | null;
  vision_critique_required?: boolean;
  composition_engine?: "legacy_python_pptx" | "pptxgenjs_v3";
  materials?: Array<{ name: string; path?: string; media_type?: string }>;
}

export interface PresentationJobRecord {
  job_id: string;
  conversation_id: string;
  status: PresentationJobStatus;
  stage: string;
  request: PresentationJobRequest;
  created_at: string;
  updated_at: string;
  last_sequence: number;
  checkpoints: Record<string, string>;
  error?: string | null;
}

export interface PresentationJobEvent {
  job_id: string;
  sequence: number;
  stage: string;
  status: PresentationJobStatus;
  slide_id?: string | null;
  message: string;
  checkpoint?: string | null;
  recoverable?: boolean;
  created_at: string;
}

export interface PresentationArtifacts {
  record: PresentationJobRecord;
  brief?: Record<string, unknown>;
  plan?: { slides?: Array<Record<string, unknown>> };
  deck_ir?: {
    slides?: Array<{
      id: string;
      title?: string;
      layout_id?: string;
      objects?: Array<{
        id: string;
        kind: string;
        role?: string;
        slot?: string;
        text?: string;
      }>;
    }>;
  };
  quality?: {
    passed: boolean;
    delivery_status: "passed" | "degraded" | "failed";
    visual_verified: boolean;
    output_sha256?: string | null;
    render_set_id?: string | null;
  };
  thumbnails?: string[];
}

const query = (conversationId: string) =>
  `conversation_id=${encodeURIComponent(conversationId)}`;

export const presentationApi = {
  create: (payload: PresentationJobRequest) =>
    request<PresentationJobRecord>("/presentations/jobs", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  list: (conversationId: string) =>
    request<PresentationJobRecord[]>(
      `/presentations/jobs?${query(conversationId)}`,
    ),
  get: (jobId: string, conversationId: string) =>
    request<PresentationJobRecord>(
      `/presentations/jobs/${encodeURIComponent(jobId)}?${query(conversationId)}`,
    ),
  events: (jobId: string, conversationId: string, afterSequence = 0) =>
    request<PresentationJobEvent[]>(
      `/presentations/jobs/${encodeURIComponent(jobId)}/events?${query(conversationId)}&after_sequence=${afterSequence}`,
    ),
  artifacts: (jobId: string, conversationId: string) =>
    request<PresentationArtifacts>(
      `/presentations/jobs/${encodeURIComponent(jobId)}/artifacts?${query(conversationId)}`,
    ),
  action: (
    jobId: string,
    conversationId: string,
    action: "pause" | "resume" | "cancel",
  ) =>
    request<PresentationJobEvent>(
      `/presentations/jobs/${encodeURIComponent(jobId)}/${action}`,
      {
        method: "POST",
        body: JSON.stringify({ conversation_id: conversationId }),
      },
    ),
  redoSlide: (
    jobId: string,
    conversationId: string,
    slideId: string,
    instruction: string,
    baseOutputSha256: string,
  ) =>
    request<PresentationJobRecord>(
      `/presentations/jobs/${encodeURIComponent(jobId)}/redo-slide`,
      {
        method: "POST",
        body: JSON.stringify({
          conversation_id: conversationId,
          slide_id: slideId,
          instruction,
          base_output_sha256: baseOutputSha256,
        }),
      },
    ),
  outputBlob: async (jobId: string, conversationId: string) => {
    const response = await fetch(
      getApiUrl(
        `/presentations/jobs/${encodeURIComponent(jobId)}/output?${query(conversationId)}`,
      ),
      { headers: buildAuthHeaders() },
    );
    if (!response.ok) throw new Error("演示文稿尚未生成完成");
    return response.blob();
  },
};
