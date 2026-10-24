import { request } from "../request";
import type { WorkMethod, WorkMethodDraft } from "../types/workMethod";

export const workMethodApi = {
  listWorkMethods: () => request<WorkMethod[]>("/work-methods"),

  saveWorkMethod: (methodId: string, body: WorkMethodDraft) =>
    request<WorkMethod>(`/work-methods/${encodeURIComponent(methodId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  deleteWorkMethod: (methodId: string) =>
    request<void>(`/work-methods/${encodeURIComponent(methodId)}`, {
      method: "DELETE",
    }),
};
