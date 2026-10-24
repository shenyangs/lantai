import { request } from "../request";
import type {
  KdocsDocumentItem,
  KdocsDocumentLinkResponse,
  KdocsDocumentListResponse,
  KdocsListDocumentsParams,
  KdocsReadDocumentRequest,
  KdocsDocumentReadResponse,
} from "../types/kdocs";

function listQuery(params: KdocsListDocumentsParams): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export const kdocsApi = {
  listDocuments: (params: KdocsListDocumentsParams = {}) =>
    request<KdocsDocumentListResponse>(
      `/skills/integrations/kdocs/documents${listQuery(params)}`,
      undefined,
      { operation: "read", timeoutMs: 45_000 },
    ),

  readDocument: (body: KdocsReadDocumentRequest) =>
    request<KdocsDocumentReadResponse>(
      "/skills/integrations/kdocs/documents/read",
      { method: "POST", body: JSON.stringify(body) },
      { operation: "read", timeoutMs: 130_000 },
    ),

  getDocumentInfo: (fileId: string) =>
    request<KdocsDocumentItem>(
      "/skills/integrations/kdocs/documents/info",
      { method: "POST", body: JSON.stringify({ file_id: fileId }) },
      { operation: "read", timeoutMs: 35_000 },
    ),

  getDocumentLink: (fileId: string) =>
    request<KdocsDocumentLinkResponse>(
      "/skills/integrations/kdocs/documents/link",
      { method: "POST", body: JSON.stringify({ file_id: fileId }) },
      { operation: "read", timeoutMs: 35_000 },
    ),
};
