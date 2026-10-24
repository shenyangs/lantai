import { request } from "../request";

export interface DocxDoubleWriteRequest {
  instruction: string;
  selected_text: string;
  document_before: string;
  document_after: string;
  current_draft: string;
  document_name: string;
  source_chat_id: string;
}

export interface DocxDoubleWriteResponse {
  success: true;
  replacement_text: string;
  summary: string;
  model: string;
}

export const docxDoubleWriteApi = {
  rewrite: (payload: DocxDoubleWriteRequest) =>
    request<DocxDoubleWriteResponse>("/console/docx/rewrite", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};
