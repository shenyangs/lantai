import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import { request } from "../request";
import type { ChatSpec } from "../types/chat";
import type {
  AddWorkRequest,
  WorkEditCopy,
  WorkDerivationRequest,
  WorkItem,
  WorkPreview,
  WorkTextPreview,
  WorksIndexInspection,
  WorksIndexRecoveryPreview,
  WorksIndexRestoreResult,
} from "../types/works";

export const worksApi = {
  list: (chatId?: string) => {
    const query = chatId ? `?${new URLSearchParams({ chat_id: chatId })}` : "";
    return request<WorkItem[]>(`/works${query}`);
  },

  inspectIndex: () => request<WorksIndexInspection>("/works/index/diagnostics"),

  previewIndexRecovery: (snapshotDigest: string) =>
    request<WorksIndexRecoveryPreview>(
      `/works/index/recovery-preview?${new URLSearchParams({
        snapshot_digest: snapshotDigest,
      })}`,
    ),

  restoreIndex: (expectedCurrentDigest: string, snapshotDigest: string) =>
    request<WorksIndexRestoreResult>("/works/index/recovery", {
      method: "POST",
      body: JSON.stringify({
        expected_current_digest: expectedCurrentDigest,
        snapshot_digest: snapshotDigest,
        confirm: true,
      }),
    }),

  add: (payload: AddWorkRequest) =>
    request<WorkItem>("/works", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  remove: (workId: string) =>
    request<{ removed: boolean; source_preserved: boolean }>(
      `/works/${encodeURIComponent(workId)}`,
      { method: "DELETE" },
    ),

  createEditSession: (workId: string) =>
    request<ChatSpec>(`/works/${encodeURIComponent(workId)}/edit-session`, {
      method: "POST",
    }),

  createEditCopy: (workId: string, chatId: string) =>
    request<WorkEditCopy>(`/works/${encodeURIComponent(workId)}/edit-copy`, {
      method: "POST",
      body: JSON.stringify({ chat_id: chatId }),
    }),

  derive: (workId: string, payload: WorkDerivationRequest) =>
    request<WorkItem>(`/works/${encodeURIComponent(workId)}/derivations`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  preview: (workId: string, chatId?: string) => {
    const query = chatId ? `?${new URLSearchParams({ chat_id: chatId })}` : "";
    return request<WorkPreview>(
      `/works/${encodeURIComponent(workId)}/preview${query}`,
    );
  },

  textPreview: (workId: string, chatId?: string) => {
    const query = chatId ? `?${new URLSearchParams({ chat_id: chatId })}` : "";
    return request<WorkTextPreview>(
      `/works/${encodeURIComponent(workId)}/text-preview${query}`,
    );
  },

  previewPageBlob: async (
    workId: string,
    page: number,
    sourceDigest: string,
    chatId?: string,
  ): Promise<Blob> => {
    const query = new URLSearchParams({
      digest: sourceDigest,
    });
    if (chatId) query.set("chat_id", chatId);
    const response = await fetch(
      getApiUrl(
        `/works/${encodeURIComponent(workId)}/preview/pages/${page}?${query}`,
      ),
      { headers: buildAuthHeaders() },
    );
    if (!response.ok) throw new Error("无法读取该页预览");
    return response.blob();
  },

  generateCover: (workId: string) =>
    request<WorkItem>(`/works/${encodeURIComponent(workId)}/cover`, {
      method: "POST",
    }),

  contentBlob: async (workId: string, chatId?: string): Promise<Blob> => {
    const query = chatId ? `?${new URLSearchParams({ chat_id: chatId })}` : "";
    const response = await fetch(
      getApiUrl(`/works/${encodeURIComponent(workId)}/content${query}`),
      { headers: buildAuthHeaders() },
    );
    if (!response.ok) {
      throw new Error(
        response.status === 410 ? "作品文件已丢失" : "无法读取作品预览",
      );
    }
    return response.blob();
  },

  coverBlob: async (workId: string): Promise<Blob> => {
    const response = await fetch(
      getApiUrl(`/works/${encodeURIComponent(workId)}/cover`),
      { headers: buildAuthHeaders() },
    );
    if (!response.ok) throw new Error("无法读取作品封面");
    return response.blob();
  },
};
