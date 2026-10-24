import { useEffect } from "react";

import { docxDoubleWriteApi } from "../../api/modules/docxDoubleWrite";

export default function DocxDoubleWriteService() {
  useEffect(() => {
    const desktop = window.electronAPI;
    if (!desktop?.onDocxRewriteRequest || !desktop.respondDocxRewriteRequest) {
      return;
    }

    return desktop.onDocxRewriteRequest((envelope) => {
      const requestId = String(envelope?.request_id || "");
      if (!requestId) return;
      const payload = envelope.payload;
      void docxDoubleWriteApi
        .rewrite({
          instruction: payload.instruction,
          selected_text: payload.selected_text,
          document_before: payload.document_before,
          document_after: payload.document_after,
          current_draft: payload.current_draft,
          document_name: payload.document_name,
          source_chat_id: payload.source_chat_id,
        })
        .then((result) => {
          desktop.respondDocxRewriteRequest?.({
            request_id: requestId,
            result,
          });
        })
        .catch((reason) => {
          desktop.respondDocxRewriteRequest?.({
            request_id: requestId,
            result: {
              success: false,
              error:
                reason instanceof Error ? reason.message : "沈行知改写失败",
              error_code: "docx_rewrite_failed",
            },
          });
        });
    });
  }, []);

  return null;
}
