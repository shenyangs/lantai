import { request } from "../request";
import type {
  OfficeCliConfigUpdate,
  OfficeCliStatus,
  OfficeDocumentCompare,
  OfficeDocumentCompareRequest,
  OfficeDocumentPreview,
  OfficeDocumentPreviewRequest,
  OfficeTaskTemplatesResponse,
} from "../types";

export const officeDocumentApi = {
  getOfficeCliStatus: () =>
    request<OfficeCliStatus>("/office-documents/status"),

  updateOfficeCliConfig: (settings: OfficeCliConfigUpdate) =>
    request<OfficeCliStatus>("/office-documents/config", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),

  previewOfficeDocument: (payload: OfficeDocumentPreviewRequest) =>
    request<OfficeDocumentPreview>("/office-documents/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  compareOfficeDocuments: (payload: OfficeDocumentCompareRequest) =>
    request<OfficeDocumentCompare>("/office-documents/compare", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listOfficeTaskTemplates: () =>
    request<OfficeTaskTemplatesResponse>("/office-documents/templates"),
};
