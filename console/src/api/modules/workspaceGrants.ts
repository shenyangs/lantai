import { request } from "../request";

export type WorkspaceAccessMode = "read_only" | "read_write";
export type WorkspaceTrustState = "trusted" | "restricted";

export interface WorkspaceGrantResponse {
  id: string;
  display_name: string;
  project_root: string;
  access_mode: WorkspaceAccessMode;
  trust_state: WorkspaceTrustState;
  status: "active" | "revoked";
}

export const workspaceGrantsApi = {
  claim: (selectionReceipt: string) =>
    request<WorkspaceGrantResponse>("/workspace-grants/claim", {
      method: "POST",
      body: JSON.stringify({ selection_receipt: selectionReceipt }),
    }),
  list: () => request<WorkspaceGrantResponse[]>("/workspace-grants"),
  revoke: (grantId: string) =>
    request<WorkspaceGrantResponse>(
      `/workspace-grants/${encodeURIComponent(grantId)}`,
      { method: "DELETE" },
    ),
};
