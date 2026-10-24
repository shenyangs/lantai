import { request } from "../request";
import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import type {
  MdFileInfo,
  MdFileContent,
  DailyMemoryFile,
  LayeredMemorySnapshot,
  MemoryClearPreview,
  MemoryClearResult,
  MemoryDreamingRun,
  MemoryDreamingStatus,
  MemoryFactSnapshot,
  MemoryFactSourcesResponse,
  MemoryFactScope,
  MemoryImportCommitResult,
  MemoryImportPreview,
  MemoryImportPrompt,
  MemoryPackReport,
  MemoryRecallTrace,
  RecentMemoryImport,
  ProactiveOpportunitySnapshot,
  UserUnderstandingSnapshot,
  UserMemorySnapshot,
  UserMemoryType,
  UserMemoryWriteResult,
  WikiIndexResponse,
  WikiInsightsResponse,
  WikiInsight,
  WikiLintIssue,
  WikiLintResponse,
  WikiGraphEdge,
  WikiGraphNode,
  WikiHomeResponse,
  WikiPageDetail,
  WikiPageSummary,
  WikiPurposeResponse,
  WikiQueueSummary,
  WikiQueryResponse,
  WikiReviewItem,
  WikiReviewResponse,
  WikiSchemaField,
  WikiSchemaPage,
  WikiSchemaResponse,
  WikiSearchResponse,
  WikiSearchHit,
  WikiSourceImportDetail,
  WikiSourceInfo,
  WikiSourceContent,
  WikiArchivedSourceInfo,
  WikiTrashItem,
  WikiCollection,
} from "../types";

function getSelectedAgentId(): string {
  try {
    // Read from sessionStorage first (per-tab agent), fall back to localStorage
    const agentStorage =
      sessionStorage.getItem("lantai-agent-storage") ||
      localStorage.getItem("lantai-agent-storage");
    if (agentStorage) {
      const parsed = JSON.parse(agentStorage);
      const selectedAgent = parsed?.state?.selectedAgent;
      if (selectedAgent) {
        return selectedAgent;
      }
    }
  } catch (error) {
    console.warn("Failed to get selected agent from storage:", error);
  }
  return "default";
}

function generateFallbackFilename(): string {
  const agentId = getSelectedAgentId();
  const now = new Date();
  const timestamp = now
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\..+/, "")
    .replace("T", "_")
    .slice(0, 15); // YYYYMMDD_HHMMSS
  return `lantai_workspace_${agentId}_${timestamp}.zip`;
}

function generateMemoryPackFilename(): string {
  const agentId = getSelectedAgentId();
  const timestamp = new Date()
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\..+/, "")
    .replace("T", "_")
    .slice(0, 15);
  return `lantai_memory_${agentId}_${timestamp}.lantai-memory.zip`;
}

function downloadFilename(response: Response, fallback: string): string {
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded);
    } catch {
      return encoded;
    }
  }
  return disposition.match(/filename="([^"]+)"/i)?.[1] || fallback;
}

async function responseError(response: Response, action: string) {
  const body = await response.text().catch(() => "");
  let detail = body;
  try {
    const payload = JSON.parse(body) as { detail?: string };
    detail = payload.detail || body;
  } catch {
    // Non-JSON errors are already preserved in ``body``.
  }
  return new Error(
    `${action}失败：${detail || `${response.status} ${response.statusText}`}`,
  );
}

export interface WorkspaceDownloadResult {
  blob: Blob;
  filename: string;
}

export interface WikiSourceUploadResult {
  imported: WikiSourceInfo[];
  skipped: WikiSourceInfo[];
  pages: string[];
  details?: WikiSourceImportDetail[];
  queue_summary?: WikiQueueSummary;
}

const withUpdatedAt = <T extends MdFileInfo>(
  file: T,
): T & { updated_at: number } => ({
  ...file,
  updated_at: new Date(file.modified_time).getTime(),
});

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

const asArray = <T>(value: unknown): T[] =>
  Array.isArray(value) ? (value as T[]) : [];

const fallbackMarkdownFile = (filename: string): MdFileInfo => {
  const now = new Date().toISOString();
  return {
    filename,
    path: filename,
    size: 0,
    created_time: now,
    modified_time: now,
  };
};

const fallbackWikiQueueSummary = (): WikiQueueSummary => ({
  pending: 0,
  running: 0,
  finished: 0,
  failed: 0,
  cancelled: 0,
  total: 0,
  latest: [],
});

export const workspaceApi = {
  listFiles: () =>
    request<MdFileInfo[]>("/workspace/files").then((files) =>
      asArray<MdFileInfo>(files).map((file) => withUpdatedAt(file)),
    ),

  loadFile: (fileName: string) =>
    request<MdFileContent>(`/workspace/files/${encodeURIComponent(fileName)}`),

  saveFile: (fileName: string, content: string) =>
    request<Record<string, unknown>>(
      `/workspace/files/${encodeURIComponent(fileName)}`,
      {
        method: "PUT",
        body: JSON.stringify({ content }),
      },
    ),

  getWikiIndex: () =>
    request<WikiIndexResponse>("/workspace/wiki").then((wiki) => {
      const data = asRecord(wiki);
      const graph = asRecord(data.graph);
      return {
        pages: asArray<WikiPageSummary>(data.pages).map((page) =>
          withUpdatedAt(page),
        ),
        sources: asArray<WikiSourceInfo>(data.sources).map((source) =>
          withUpdatedAt(source),
        ),
        graph: {
          nodes: asArray<WikiGraphNode>(graph.nodes),
          edges: asArray<WikiGraphEdge>(graph.edges),
        },
        stats: asRecord(data.stats) as Record<string, number>,
        overview:
          data.overview && typeof data.overview === "object"
            ? (data.overview as WikiIndexResponse["overview"])
            : null,
        queue_summary:
          data.queue_summary && typeof data.queue_summary === "object"
            ? (data.queue_summary as WikiQueueSummary)
            : fallbackWikiQueueSummary(),
        collections: asArray<WikiCollection>(data.collections),
      };
    }),

  getWikiHome: () =>
    request<WikiHomeResponse>("/workspace/wiki/home").then((home) => {
      const data = asRecord(home);
      const wikiData = asRecord(data.wiki);
      const graph = asRecord(wikiData.graph);
      const purposeData = asRecord(data.purpose);
      const lintData = asRecord(data.lint);
      const insightsData = asRecord(data.insights);
      const reviewData = asRecord(data.review);
      const schemaData = asRecord(data.structure);
      return {
        wiki: {
          pages: asArray<WikiPageSummary>(wikiData.pages).map((page) =>
            withUpdatedAt(page),
          ),
          sources: asArray<WikiSourceInfo>(wikiData.sources).map((source) =>
            withUpdatedAt(source),
          ),
          graph: {
            nodes: asArray<WikiGraphNode>(graph.nodes),
            edges: asArray<WikiGraphEdge>(graph.edges),
          },
          stats: asRecord(wikiData.stats) as Record<string, number>,
          overview:
            wikiData.overview && typeof wikiData.overview === "object"
              ? (wikiData.overview as WikiIndexResponse["overview"])
              : null,
          queue_summary:
            wikiData.queue_summary && typeof wikiData.queue_summary === "object"
              ? (wikiData.queue_summary as WikiQueueSummary)
              : fallbackWikiQueueSummary(),
          collections: asArray<WikiCollection>(wikiData.collections),
        },
        purpose: withUpdatedAt({
          ...fallbackMarkdownFile("purpose.md"),
          ...purposeData,
          content:
            typeof purposeData.content === "string" ? purposeData.content : "",
        } as WikiPurposeResponse),
        lint: {
          issues: asArray<WikiLintIssue>(lintData.issues),
          stats: asRecord(lintData.stats) as Record<string, number>,
        },
        insights: {
          insights: asArray<WikiInsight>(insightsData.insights),
          stats: asRecord(insightsData.stats) as Record<string, number>,
        },
        review: {
          items: asArray<WikiReviewItem>(reviewData.items),
          stats: asRecord(reviewData.stats) as Record<string, number>,
        },
        structure: {
          pages: asArray<WikiSchemaPage>(schemaData.pages),
          fields: asArray<WikiSchemaField>(schemaData.fields),
          stats: asRecord(schemaData.stats) as Record<string, number>,
        },
      } satisfies WikiHomeResponse;
    }),

  loadWikiPage: (fileName: string) =>
    request<WikiPageDetail>(
      `/workspace/wiki/pages/${encodeURIComponent(fileName)}`,
    ).then((page) => withUpdatedAt(page)),

  saveWikiPage: (fileName: string, content: string) =>
    request<Record<string, unknown>>(
      `/workspace/wiki/pages/${encodeURIComponent(fileName)}`,
      {
        method: "PUT",
        body: JSON.stringify({ content }),
      },
    ),

  deleteWikiPage: (fileName: string) =>
    request<Record<string, unknown>>(
      `/workspace/wiki/pages/${encodeURIComponent(fileName)}`,
      {
        method: "DELETE",
      },
    ),

  getWikiPurpose: () =>
    request<WikiPurposeResponse>("/workspace/wiki/purpose").then((purpose) => {
      const data = asRecord(purpose);
      return withUpdatedAt({
        ...fallbackMarkdownFile("purpose.md"),
        ...data,
        content: typeof data.content === "string" ? data.content : "",
      } as WikiPurposeResponse);
    }),

  saveWikiPurpose: (content: string) =>
    request<Record<string, unknown>>("/workspace/wiki/purpose", {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),

  searchWiki: (query: string) =>
    request<WikiSearchResponse>(
      `/workspace/wiki/search?q=${encodeURIComponent(query)}`,
    ),

  queryWiki: (query: string, limit = 8, sourcePaths: string[] = []) =>
    request<WikiQueryResponse>("/workspace/wiki/query", {
      method: "POST",
      body: JSON.stringify({ query, limit, source_paths: sourcePaths }),
    }),

  runWikiIngest: (mode: "fast" | "deep" = "fast", sourcePath = "") =>
    request<Record<string, unknown>>("/workspace/wiki/ingest/run", {
      method: "POST",
      body: JSON.stringify({ mode, source_path: sourcePath }),
    }),

  getWikiIngestStatus: () =>
    request<WikiQueueSummary>("/workspace/wiki/ingest/status"),

  getWikiReview: () =>
    request<WikiReviewResponse>("/workspace/wiki/review").then((review) => {
      const data = asRecord(review);
      return {
        items: asArray<WikiReviewItem>(data.items),
        stats: asRecord(data.stats) as Record<string, number>,
      };
    }),

  resolveWikiReview: (
    itemId: string,
    status: "accepted" | "skipped" | "resolved" = "accepted",
    note = "",
  ) =>
    request<WikiReviewItem>(
      `/workspace/wiki/review/${encodeURIComponent(itemId)}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({ status, note }),
      },
    ),

  saveQueryToWiki: (title: string, content: string) =>
    request<Record<string, unknown>>("/workspace/wiki/save-query", {
      method: "POST",
      body: JSON.stringify({ title, content }),
    }),

  getWikiLint: () =>
    request<WikiLintResponse>("/workspace/wiki/lint").then((lint) => {
      const data = asRecord(lint);
      return {
        issues: asArray<WikiLintIssue>(data.issues),
        stats: asRecord(data.stats) as Record<string, number>,
      };
    }),

  getWikiInsights: () =>
    request<WikiInsightsResponse>("/workspace/wiki/insights").then(
      (insights) => {
        const data = asRecord(insights);
        return {
          insights: asArray<WikiInsight>(data.insights),
          stats: asRecord(data.stats) as Record<string, number>,
        };
      },
    ),

  getWikiSchema: () =>
    request<WikiSchemaResponse>("/workspace/wiki/schema").then((schema) => {
      const data = asRecord(schema);
      return {
        pages: asArray<WikiSchemaPage>(data.pages),
        fields: asArray<WikiSchemaField>(data.fields),
        stats: asRecord(data.stats) as Record<string, number>,
      };
    }),

  uploadWikiSource: async (file: File): Promise<WikiSourceUploadResult> => {
    const formData = new FormData();
    formData.append("file", file);

    const response = await fetch(getApiUrl("/workspace/wiki/sources/upload"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(
        `Wiki source upload failed: ${response.status} ${response.statusText} - ${errorText}`,
      );
    }

    const result = (await response.json()) as WikiSourceUploadResult;
    return {
      ...result,
      imported: result.imported.map((source) => withUpdatedAt(source)),
      skipped: (result.skipped ?? []).map((source) => withUpdatedAt(source)),
      details: result.details ?? [],
      queue_summary: result.queue_summary ?? fallbackWikiQueueSummary(),
    };
  },

  importWikiUrl: (url: string) =>
    request<WikiSourceUploadResult>("/workspace/wiki/sources/url", {
      method: "POST",
      body: JSON.stringify({ url }),
    }).then((result) => ({
      ...result,
      imported: result.imported.map((source) => withUpdatedAt(source)),
      skipped: (result.skipped ?? []).map((source) => withUpdatedAt(source)),
      details: result.details ?? [],
      queue_summary: result.queue_summary ?? fallbackWikiQueueSummary(),
    })),

  loadWikiSourceContent: (
    path: string,
    evidence?: Pick<WikiSearchHit, "source_revision" | "source_text_hash" | "source_start" | "source_end">,
  ) =>
    request<WikiSourceContent>(
      `/workspace/wiki/sources/content?${new URLSearchParams({
        path,
        ...(evidence?.source_revision ? { expected_revision: evidence.source_revision } : {}),
        ...(evidence?.source_text_hash ? { expected_text_hash: evidence.source_text_hash } : {}),
        ...(evidence?.source_start != null ? { source_start: String(evidence.source_start) } : {}),
        ...(evidence?.source_end != null ? { source_end: String(evidence.source_end) } : {}),
      }).toString()}`,
    ),

  updateWikiSource: (
    path: string,
    update: { display_title?: string; collection_ids?: string[] },
  ) =>
    request<WikiSourceInfo>(
      `/workspace/wiki/sources/metadata?path=${encodeURIComponent(path)}`,
      { method: "PATCH", body: JSON.stringify(update) },
    ).then((source) => withUpdatedAt(source)),

  archiveWikiSource: (path: string) =>
    request<Record<string, unknown>>(
      `/workspace/wiki/sources?path=${encodeURIComponent(path)}`,
      { method: "DELETE" },
    ),

  listArchivedWikiSources: () =>
    request<WikiArchivedSourceInfo[]>("/workspace/wiki/sources/archived").then(
      (sources) => asArray<WikiArchivedSourceInfo>(sources),
    ),

  restoreArchivedWikiSource: (id: string) =>
    request<WikiSourceInfo>(
      `/workspace/wiki/sources/archived/${encodeURIComponent(id)}/restore`,
      { method: "POST" },
    ).then((source) => withUpdatedAt(source)),

  listWikiTrash: () =>
    request<WikiTrashItem[]>("/workspace/wiki/trash").then((items) =>
      asArray<WikiTrashItem>(items),
    ),

  restoreWikiTrashItem: (id: string) =>
    request<{ kind: "source" | "page"; item: Record<string, unknown> }>(
      `/workspace/wiki/trash/${encodeURIComponent(id)}/restore`,
      { method: "POST" },
    ),

  purgeWikiTrashItem: (id: string) =>
    request<{ deleted: boolean; id: string }>(
      `/workspace/wiki/trash/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),

  createWikiCollection: (name: string, description = "") =>
    request<WikiCollection>("/workspace/wiki/collections", {
      method: "POST",
      body: JSON.stringify({ name, description }),
    }),

  updateWikiCollection: (
    id: string,
    update: { name?: string; description?: string; source_paths?: string[] },
  ) =>
    request<WikiCollection>(
      `/workspace/wiki/collections/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify(update) },
    ),

  deleteWikiCollection: (id: string) =>
    request<Record<string, unknown>>(
      `/workspace/wiki/collections/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),

  // Workspace package download
  downloadWorkspace: async (): Promise<WorkspaceDownloadResult> => {
    const response = await fetch(getApiUrl("/workspace/download"), {
      method: "GET",
      headers: buildAuthHeaders(),
    });

    if (!response.ok) {
      throw new Error(
        `Workspace download failed: ${response.status} ${response.statusText}`,
      );
    }

    const blob = await response.blob();

    // Extract filename from Content-Disposition header
    const disposition = response.headers.get("Content-Disposition");
    let filename: string;

    if (disposition) {
      const filenameMatch = disposition.match(/filename="(.+?)"/);
      if (filenameMatch && filenameMatch[1]) {
        filename = filenameMatch[1];
      } else {
        filename = generateFallbackFilename();
      }
    } else {
      filename = generateFallbackFilename();
    }

    return { blob, filename };
  },

  exportMemoryPack: async (): Promise<WorkspaceDownloadResult> => {
    const response = await fetch(getApiUrl("/workspace/memory-pack/export"), {
      method: "GET",
      headers: buildAuthHeaders(),
    });
    if (!response.ok) {
      throw await responseError(response, "记忆包导出");
    }
    return {
      blob: await response.blob(),
      filename: downloadFilename(response, generateMemoryPackFilename()),
    };
  },

  inspectMemoryPack: async (file: File): Promise<MemoryPackReport> => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(getApiUrl("/workspace/memory-pack/inspect"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
    });
    if (!response.ok) {
      throw await responseError(response, "记忆包校验");
    }
    return (await response.json()) as MemoryPackReport;
  },

  adoptMemoryPack: async (
    file: File,
    options: { dryRun: boolean; confirmation?: string },
  ): Promise<MemoryPackReport> => {
    const formData = new FormData();
    formData.append("file", file);
    const query = new URLSearchParams({
      dry_run: String(options.dryRun),
      confirmation: options.confirmation || "",
    });
    const response = await fetch(
      getApiUrl(`/workspace/memory-pack/adopt?${query.toString()}`),
      {
        method: "POST",
        headers: buildAuthHeaders(),
        body: formData,
      },
    );
    if (!response.ok) {
      throw await responseError(response, "记忆包采用");
    }
    return (await response.json()) as MemoryPackReport;
  },

  // File upload functionality
  uploadFile: async (
    file: File,
  ): Promise<{ success: boolean; message: string }> => {
    const formData = new FormData();
    formData.append("file", file);

    const response = await fetch(getApiUrl("/workspace/upload"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(
        `Upload failed: ${response.status} ${response.statusText} - ${errorText}`,
      );
    }

    return await response.json();
  },

  listDailyMemory: () =>
    request<MdFileInfo[]>("/workspace/memory").then((files) =>
      files.map((file) => {
        const date = file.filename.replace(".md", "");
        return {
          ...file,
          date,
          updated_at: new Date(file.modified_time).getTime(),
        } as DailyMemoryFile;
      }),
    ),

  loadDailyMemory: (date: string) =>
    request<MdFileContent>(`/workspace/memory/${encodeURIComponent(date)}.md`),

  saveDailyMemory: (date: string, content: string) =>
    request<Record<string, unknown>>(
      `/workspace/memory/${encodeURIComponent(date)}.md`,
      {
        method: "PUT",
        body: JSON.stringify({ content }),
      },
    ),

  listUserMemory: (includeArchived = false) =>
    request<UserMemorySnapshot>(
      `/workspace/user-memory${
        includeArchived ? "?include_archived=true" : ""
      }`,
    ),

  getUserUnderstanding: () =>
    request<UserUnderstandingSnapshot>("/workspace/user-understanding"),

  feedbackUserUnderstanding: (
    atomId: string,
    verdict: "confirmed" | "corrected" | "ignored",
  ) =>
    request<Record<string, unknown>>(
      `/workspace/user-understanding/${encodeURIComponent(atomId)}/feedback`,
      {
        method: "POST",
        body: JSON.stringify({ verdict }),
      },
    ),

  deleteUserUnderstanding: (atomId: string) =>
    request<{ deleted: boolean }>(
      `/workspace/user-understanding/${encodeURIComponent(atomId)}`,
      { method: "DELETE" },
    ),

  listProactiveOpportunities: () =>
    request<ProactiveOpportunitySnapshot>("/workspace/proactive-opportunities"),

  feedbackProactiveOpportunity: (
    opportunityId: string,
    verdict: "presented" | "ignored" | "completed" | "dismissed",
  ) =>
    request<Record<string, unknown>>(
      `/workspace/proactive-opportunities/${encodeURIComponent(opportunityId)}/feedback`,
      {
        method: "POST",
        body: JSON.stringify({ verdict }),
      },
    ),

  listMemorySuggestions: () =>
    request<MemoryFactSnapshot>(
      "/workspace/memory-facts?status=pending&limit=3",
    ),

  openMemoryFactSources: (factId: string) =>
    request<MemoryFactSourcesResponse>(
      `/workspace/memory-facts/${encodeURIComponent(factId)}/sources`,
    ),

  deleteMemoryFactSource: (factId: string, eventId: string) =>
    request<{ deleted: boolean }>(
      `/workspace/memory-facts/${encodeURIComponent(factId)}/sources/${encodeURIComponent(eventId)}`,
      { method: "DELETE" },
    ),

  getMemoryPreferences: () =>
    request<{ runtime_enabled: boolean; capture_enabled: boolean }>(
      "/workspace/memory-preferences",
    ),

  getLastMemoryRecall: () =>
    request<MemoryRecallTrace>("/workspace/memory-recall/last"),

  getMemoryClearPreview: () =>
    request<MemoryClearPreview>("/workspace/memory-clear/preview"),

  clearAllMemory: (payload: { confirmation: string; create_backup: boolean }) =>
    request<MemoryClearResult>("/workspace/memory-clear", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getMemoryDreaming: () =>
    request<MemoryDreamingStatus>("/workspace/memory-dreaming"),

  updateMemoryDreaming: (payload: {
    enabled: boolean;
    schedule: string;
    rem_enabled: boolean;
    min_score: number;
  }) =>
    request<{
      enabled: boolean;
      schedule: string;
      rem_enabled: boolean;
      min_score: number;
    }>("/workspace/memory-dreaming", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  runMemoryDreaming: (payload: { shadow: boolean }) =>
    request<MemoryDreamingRun>("/workspace/memory-dreaming/run", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  rollbackMemoryDreaming: (runId: string) =>
    request<{
      run_id: string;
      rolled_back: boolean;
      restored_count: number;
      skipped_count: number;
      rolled_back_at?: string;
    }>(
      `/workspace/memory-dreaming/runs/${encodeURIComponent(runId)}/rollback`,
      { method: "POST" },
    ),

  getMemoryImportPrompt: () =>
    request<MemoryImportPrompt>("/workspace/memory-imports/prompt"),

  previewMemoryImport: (payload: { text: string; provider: string }) =>
    request<MemoryImportPreview>("/workspace/memory-imports/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  commitMemoryImport: (batchId: string, selectedIds: string[]) =>
    request<MemoryImportCommitResult>(
      `/workspace/memory-imports/${encodeURIComponent(batchId)}/commit`,
      {
        method: "POST",
        body: JSON.stringify({ selected_ids: selectedIds }),
      },
    ),

  getRecentMemoryImport: () =>
    request<RecentMemoryImport>("/workspace/memory-imports/recent"),

  undoMemoryImport: (batchId: string) =>
    request<{
      batch_id: string;
      status: "undone";
      removed_count: number;
      restored_count: number;
      forgotten_fact_count: number;
      undone_at: string;
    }>(`/workspace/memory-imports/${encodeURIComponent(batchId)}/undo`, {
      method: "POST",
    }),

  updateMemoryPreferences: (payload: {
    runtime_enabled?: boolean;
    capture_enabled?: boolean;
  }) =>
    request<{ runtime_enabled: boolean; capture_enabled: boolean }>(
      "/workspace/memory-preferences",
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    ),

  confirmMemoryFact: (
    factId: string,
    scope: Extract<MemoryFactScope, "project" | "global">,
    scopeId = "",
  ) =>
    request<Record<string, unknown>>(
      `/workspace/memory-facts/${encodeURIComponent(factId)}/confirm`,
      {
        method: "POST",
        body: JSON.stringify({ scope, scope_id: scopeId }),
      },
    ),

  rejectMemoryFact: (factId: string, reason = "用户忽略") =>
    request<Record<string, unknown>>(
      `/workspace/memory-facts/${encodeURIComponent(factId)}/reject`,
      {
        method: "POST",
        body: JSON.stringify({ reason }),
      },
    ),

  addUserMemory: (
    content: string,
    type?: UserMemoryType,
    reason = "用户手动补充",
  ) =>
    request<UserMemoryWriteResult>("/workspace/user-memory", {
      method: "POST",
      body: JSON.stringify({ content, type, reason, confidence: 1 }),
    }),

  updateUserMemory: (
    entryId: string,
    payload: {
      content?: string;
      type?: UserMemoryType;
      status?: "active" | "archived" | "conflict";
      confidence?: number;
    },
  ) =>
    request<{ updated: boolean; entry: UserMemorySnapshot["entries"][number] }>(
      `/workspace/user-memory/${encodeURIComponent(entryId)}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    ),

  archiveUserMemory: (entryId: string) =>
    request<{ archived: boolean }>(
      `/workspace/user-memory/${encodeURIComponent(entryId)}/archive`,
      {
        method: "POST",
      },
    ),

  deleteUserMemory: (entryId: string) =>
    request<{ deleted: boolean }>(
      `/workspace/user-memory/${encodeURIComponent(entryId)}`,
      {
        method: "DELETE",
      },
    ),

  getLayeredMemory: () =>
    request<LayeredMemorySnapshot>("/workspace/layered-memory"),

  openLayeredMemorySource: (sourceId: string) =>
    request<Record<string, unknown>>(
      `/workspace/layered-memory/source/${encodeURIComponent(sourceId)}`,
    ),

  // System prompt files management
  getSystemPromptFiles: () =>
    request<string[]>("/workspace/system-prompt-files"),

  setSystemPromptFiles: (files: string[]) =>
    request<string[]>("/workspace/system-prompt-files", {
      method: "PUT",
      body: JSON.stringify(files),
    }),
};
