import { request } from "../request";
import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import type {
  BrowserObservation,
  BrowserFileGrantRequest,
  BrowserFileGrantResponse,
  BrowserTaskArtifacts,
  BrowserTaskEvent,
  BrowserTaskRun,
  BrowserTakeoverReceipt,
  BrowserProfile,
  BrowserRuntime,
  BrowserExportApproval,
  BrowserReplayExport,
  BrowserWorkbenchAggregateState,
  BrowserWorkbenchConfig,
  BrowserSkillStatus,
  ComputerActionType,
  ComputerObservation,
  ContentSource,
  ContentSourceDetail,
  DomainSkillDraftListResponse,
  DomainSkillDraftRecord,
  DomainSkillDraftResponse,
  LantaiCliAdapterManifest,
  LantaiCliAdapterVerifyResult,
  PublishingQueueDetail,
  ReplayResearchPackResult,
  WechatEditorProbeResponse,
  WechatDraftWriteResponse,
  WechatSeriesPrepareResult,
} from "../types";

const exportApproval = (reason: string): BrowserExportApproval => ({
  approved: true,
  reason,
  requested_by: "browser_workbench_ui",
  scope: "single_export",
  created_at: new Date().toISOString(),
});

export interface BrowserProfilePatch {
  name?: string;
  description?: string;
  allowed_domains?: string[];
  agent_control_enabled?: boolean;
  risk_policy?: "never" | "write" | "always";
  metadata?: Record<string, unknown>;
}

export interface BrowserActionRequest {
  action: string;
  profile_id?: string;
  url?: string;
  page_id?: string;
  selector?: string;
  ref?: string;
  text?: string;
  code?: string;
  path?: string;
  access?: "read" | "write" | "sensitive";
  reason?: string;
  run_id?: string;
  confirmed?: boolean;
  runtime?: BrowserRuntime;
  metadata?: Record<string, unknown>;
}

export interface BrowserTaskCreateRequest {
  profile_id?: string;
  target_domains?: string[];
  access?: "read" | "write" | "sensitive";
  mode?: "background_observable" | "foreground_observable";
  instruction?: string;
  url?: string;
  run_id?: string;
  actions?: BrowserActionRequest[];
  requires_foreground_confirmation?: boolean;
  max_actions?: number;
  max_duration_seconds?: number;
  metadata?: Record<string, unknown>;
}

export interface BrowserTaskCleanupRequest {
  max_runs?: number;
  max_age_days?: number;
  include_active?: boolean;
}

export interface BrowserTaskCleanupResult {
  ok: boolean;
  removed_count: number;
  removed_run_ids: string[];
  max_runs: number;
  max_age_days: number;
  include_active: boolean;
  message?: string;
}

export interface BrowserTaskConfirmationRequest {
  approved: boolean;
  reason?: string;
}

export interface ComputerActionRequest {
  action: ComputerActionType;
  app_allowlist?: string[];
  app_bundle_id?: string;
  window_id?: string;
  process_id?: number;
  window_title?: string;
  x?: number;
  y?: number;
  text?: string;
  key?: string;
  confirmed?: boolean;
  run_id?: string;
  reason?: string;
  metadata?: Record<string, unknown>;
}

export interface BrowserActionConfirmationRequest {
  action: BrowserActionRequest;
  approved: boolean;
  reason?: string;
}

export interface WechatSeriesPrepareRequest {
  title: string;
  audience?: string;
  positioning?: string;
  cadence?: string;
  article_count?: number;
  topics?: string[];
  sources?: ContentSource[];
  profile_id?: string;
  start_date?: string;
  publish_time?: string;
  metadata?: Record<string, unknown>;
}

export interface DomainSkillFromReplayRequest {
  title?: string;
  summary?: string;
  save?: boolean;
  failure_modes?: string[];
  metadata?: Record<string, unknown>;
}

export interface DomainSkillDraftPatch {
  title?: string;
  summary?: string;
  url_patterns?: string[];
  selectors?: string[];
  wait_conditions?: string[];
  failure_modes?: string[];
  enabled?: boolean;
  metadata?: Record<string, unknown>;
}

export interface DomainSkillSelectorProbeRequest {
  profile_id?: string;
  page_id?: string;
  url?: string;
  selectors?: string[];
  run_id?: string;
}

export interface ReplayResearchPackRequest {
  topic?: string;
  brief?: string;
  profile_id?: string;
  source_type?: ContentSource["source_type"];
  metadata?: Record<string, unknown>;
}

export interface UrlResearchPackRunRequest {
  topic: string;
  urls: string[];
  brief?: string;
  profile_id?: string;
  run_id?: string;
  source_type?: ContentSource["source_type"];
  screenshot_each?: boolean;
  max_urls?: number;
  metadata?: Record<string, unknown>;
}

export interface CapabilityFromReplayRequest {
  name?: string;
  description?: string;
  profile_id?: string;
  access?: "read" | "write" | "sensitive";
  instruction_template?: string;
  save?: boolean;
}

export interface WechatDraftWriteRequest {
  profile_id?: string;
  confirmed?: boolean;
  run_id?: string;
  queue_item_id?: string;
  open_url?: string;
  title_selector?: string;
  body_selector?: string;
  summary_selector?: string;
  save_after_fill?: boolean;
  save_selector?: string;
}

export interface WechatEditorProbeRequest {
  profile_id?: string;
  page_id?: string;
  url?: string;
  run_id?: string;
}

export const browserWorkbenchApi = {
  getState: () =>
    request<BrowserWorkbenchAggregateState>("/browser-workbench/state"),

  getConfig: () => request<BrowserWorkbenchConfig>("/browser-workbench/config"),

  getBrowserSkillStatus: () =>
    request<BrowserSkillStatus>("/browser-workbench/browser-skill/status"),

  updateConfig: (body: Partial<BrowserWorkbenchConfig>) =>
    request<BrowserWorkbenchConfig>("/browser-workbench/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  listProfiles: () => request<BrowserProfile[]>("/browser-workbench/profiles"),

  upsertProfile: (body: BrowserProfile) =>
    request<BrowserProfile>("/browser-workbench/profiles", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  patchProfile: (profileId: string, body: BrowserProfilePatch) =>
    request<BrowserProfile>(
      `/browser-workbench/profiles/${encodeURIComponent(profileId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    ),

  deleteProfile: (profileId: string) =>
    request<{ removed: boolean }>(
      `/browser-workbench/profiles/${encodeURIComponent(profileId)}`,
      { method: "DELETE" },
    ),

  runAction: (body: BrowserActionRequest) =>
    request<BrowserObservation>("/browser-workbench/actions", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  confirmAction: (body: BrowserActionConfirmationRequest) =>
    request<BrowserObservation>("/browser-workbench/actions/confirm", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  createFileGrant: (body: BrowserFileGrantRequest) =>
    request<BrowserFileGrantResponse>("/browser-workbench/file-grants", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  createTask: (body: BrowserTaskCreateRequest) =>
    request<BrowserTaskRun>("/browser-workbench/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listTasks: () => request<BrowserTaskRun[]>("/browser-workbench/tasks"),

  cleanupTaskHistory: (body: BrowserTaskCleanupRequest) =>
    request<BrowserTaskCleanupResult>("/browser-workbench/tasks/cleanup", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getTask: (runId: string) =>
    request<BrowserTaskRun>(
      `/browser-workbench/tasks/${encodeURIComponent(runId)}`,
    ),

  listTaskEvents: (runId: string) =>
    request<BrowserTaskEvent[]>(
      `/browser-workbench/tasks/${encodeURIComponent(runId)}/events`,
    ),

  taskEventsStreamUrl: (runId: string, afterId?: string) => {
    const path = `/browser-workbench/tasks/${encodeURIComponent(runId)}/events/stream`;
    if (!afterId) {
      return getApiUrl(path);
    }
    return getApiUrl(`${path}?after_id=${encodeURIComponent(afterId)}`);
  },

  subscribeTaskEvents: (
    runId: string,
    onEvent: (event: BrowserTaskEvent) => void,
    options: { afterId?: string; onError?: (error: unknown) => void } = {},
  ) => {
    let aborted = false;
    let cursor = options.afterId || "";
    let terminal = false;
    let controller: AbortController | null = null;
    const terminalStatuses = new Set([
      "completed",
      "failed",
      "cancelled",
      "manual_takeover_required",
    ]);

    const parseFrame = (frame: string): BrowserTaskEvent | null => {
      const lines = frame.split("\n");
      const data = lines
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.replace(/^data:\s*/, ""))
        .join("\n");
      if (!data) return null;
      try {
        return JSON.parse(data) as BrowserTaskEvent;
      } catch {
        return null;
      }
    };

    async function connect() {
      while (!aborted && !terminal) {
        controller = new AbortController();
        try {
          const query = new URLSearchParams({ timeout_seconds: "60" });
          if (cursor) query.set("after_id", cursor);
          const response = await fetch(
            getApiUrl(
              `/browser-workbench/tasks/${encodeURIComponent(
                runId,
              )}/events/stream?${query.toString()}`,
            ),
            {
              headers: buildAuthHeaders(),
              signal: controller.signal,
            },
          );
          if (!response.ok || !response.body) {
            throw new Error(`Browser task stream failed: ${response.status}`);
          }

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!aborted && !terminal) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const frames = buffer.split("\n\n");
            buffer = frames.pop() || "";
            for (const frame of frames) {
              const event = parseFrame(frame);
              if (!event) continue;
              cursor = event.id || cursor;
              onEvent(event);
              terminal = terminalStatuses.has(event.status);
              if (terminal) break;
            }
          }
        } catch (error) {
          if (aborted) return;
          options.onError?.(error);
        }
        if (!aborted && !terminal) {
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
        }
      }
    }

    void connect();
    return () => {
      aborted = true;
      controller?.abort();
    };
  },

  pauseTask: (runId: string) =>
    request<BrowserTaskRun>(
      `/browser-workbench/tasks/${encodeURIComponent(runId)}/pause`,
      { method: "POST" },
    ),

  requestTaskTakeover: (runId: string) =>
    request<BrowserTakeoverReceipt>(
      `/browser-workbench/tasks/${encodeURIComponent(runId)}/takeover`,
      { method: "POST" },
    ),

  resumeTask: (runId: string) =>
    request<BrowserTaskRun>(
      `/browser-workbench/tasks/${encodeURIComponent(runId)}/resume`,
      { method: "POST" },
    ),

  cancelTask: (runId: string) =>
    request<BrowserTaskRun>(
      `/browser-workbench/tasks/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" },
    ),

  confirmTask: (runId: string, body: BrowserTaskConfirmationRequest) =>
    request<BrowserTaskRun>(
      `/browser-workbench/tasks/${encodeURIComponent(runId)}/confirm`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  getTaskArtifacts: (runId: string) =>
    request<BrowserTaskArtifacts>(
      `/browser-workbench/tasks/${encodeURIComponent(runId)}/artifacts`,
    ),

  runComputerAction: (body: ComputerActionRequest) =>
    request<ComputerObservation>(
      "/browser-workbench/computer-actions/actions",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  listAdapters: () =>
    request<LantaiCliAdapterManifest[]>("/browser-workbench/adapters"),

  verifyAdapter: (
    adapterId: string,
    body: {
      profile_id?: string;
      inputs?: Record<string, unknown>;
      confirmed?: boolean;
      run_id?: string;
    },
  ) =>
    request<LantaiCliAdapterVerifyResult>(
      `/browser-workbench/adapters/${encodeURIComponent(adapterId)}/verify`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  listReplay: (runId: string) =>
    request<{ run_id: string; events: Record<string, unknown>[] }>(
      `/browser-workbench/replay/${encodeURIComponent(runId)}`,
    ),

  exportReplay: (runId: string) =>
    request<BrowserReplayExport>(
      `/browser-workbench/replay/${encodeURIComponent(runId)}/export`,
      {
        method: "POST",
        body: JSON.stringify(
          exportApproval("Export Browser Workbench replay evidence bundle"),
        ),
      },
    ),

  buildDomainSkillFromReplay: (
    runId: string,
    body: DomainSkillFromReplayRequest,
  ) =>
    request<DomainSkillDraftResponse>(
      `/browser-workbench/domain-skills/drafts/from-replay/${encodeURIComponent(runId)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  listDomainSkillDrafts: (domain?: string) =>
    request<DomainSkillDraftListResponse>(
      domain
        ? `/browser-workbench/domain-skills/drafts?domain=${encodeURIComponent(domain)}`
        : "/browser-workbench/domain-skills/drafts",
    ),

  updateDomainSkillDraft: (draftId: string, body: DomainSkillDraftPatch) =>
    request<DomainSkillDraftRecord>(
      `/browser-workbench/domain-skills/drafts/${encodeURIComponent(draftId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    ),

  probeDomainSkillSelectors: (
    draftId: string,
    body: DomainSkillSelectorProbeRequest,
  ) =>
    request<BrowserObservation>(
      `/browser-workbench/domain-skills/drafts/${encodeURIComponent(draftId)}/probe-selectors`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  buildResearchPackFromReplay: (
    runId: string,
    body: ReplayResearchPackRequest,
  ) =>
    request<ReplayResearchPackResult>(
      `/browser-workbench/content/sources/from-replay/${encodeURIComponent(runId)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  buildCapabilityFromReplay: (
    runId: string,
    body: CapabilityFromReplayRequest,
  ) =>
    request<Record<string, unknown>>(
      `/browser-workbench/capabilities/from-replay/${encodeURIComponent(runId)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  prepareWechatSeries: (body: WechatSeriesPrepareRequest) =>
    request<WechatSeriesPrepareResult>(
      "/browser-workbench/content/workflows/wechat-series/prepare",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  runResearchPackWorkflow: (body: UrlResearchPackRunRequest) =>
    request<ReplayResearchPackResult>(
      "/browser-workbench/content/workflows/research-pack/run",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  exportDossierToWiki: (dossierId: string) =>
    request<Record<string, unknown>>(
      `/browser-workbench/content/dossiers/${encodeURIComponent(dossierId)}/export-wiki`,
      {
        method: "POST",
        body: JSON.stringify(
          exportApproval("Export content dossier markdown to Wiki"),
        ),
      },
    ),

  exportDraftToWiki: (draftId: string) =>
    request<Record<string, unknown>>(
      `/browser-workbench/publishing/drafts/${encodeURIComponent(draftId)}/export-wiki`,
      {
        method: "POST",
        body: JSON.stringify(
          exportApproval("Export publishing draft markdown to Wiki"),
        ),
      },
    ),

  requestWechatDraftWrite: (draftId: string, body: WechatDraftWriteRequest) =>
    request<WechatDraftWriteResponse>(
      `/browser-workbench/publishing/drafts/${encodeURIComponent(draftId)}/wechat/write-request`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  probeWechatEditorOptions: (body: WechatEditorProbeRequest) =>
    request<WechatEditorProbeResponse>(
      "/browser-workbench/publishing/wechat/editor-probe",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  listContentWorkflows: () =>
    request<{ items: Record<string, unknown>[] }>(
      "/browser-workbench/content/workflows",
    ),

  listContentSources: () =>
    request<{ items: ContentSource[] }>("/browser-workbench/content/sources"),

  getContentSource: (sourceId: string) =>
    request<ContentSourceDetail>(
      `/browser-workbench/content/sources/${encodeURIComponent(sourceId)}`,
    ),

  exportSourceToWiki: (sourceId: string) =>
    request<Record<string, unknown>>(
      `/browser-workbench/content/sources/${encodeURIComponent(sourceId)}/export-wiki`,
      {
        method: "POST",
        body: JSON.stringify(
          exportApproval("Export content source markdown to Wiki"),
        ),
      },
    ),

  listContentDossiers: () =>
    request<{ items: Record<string, unknown>[] }>(
      "/browser-workbench/content/dossiers",
    ),

  listPublishingDrafts: () =>
    request<{ items: Record<string, unknown>[] }>(
      "/browser-workbench/publishing/drafts",
    ),

  listPublishingQueue: () =>
    request<{ items: Record<string, unknown>[] }>(
      "/browser-workbench/publishing/queue",
    ),

  getPublishingQueueItem: (queueItemId: string) =>
    request<PublishingQueueDetail>(
      `/browser-workbench/publishing/queue/${encodeURIComponent(queueItemId)}`,
    ),

  patchPublishingQueueItem: (
    queueItemId: string,
    body: {
      status?: string;
      requires_confirmation?: boolean;
      note?: string;
      metadata?: Record<string, unknown>;
    },
  ) =>
    request<PublishingQueueDetail>(
      `/browser-workbench/publishing/queue/${encodeURIComponent(queueItemId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    ),
};
