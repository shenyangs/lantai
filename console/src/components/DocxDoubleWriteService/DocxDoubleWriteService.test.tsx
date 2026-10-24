import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docxDoubleWriteApi } from "../../api/modules/docxDoubleWrite";
import DocxDoubleWriteService from "./index";

vi.mock("../../api/modules/docxDoubleWrite", () => ({
  docxDoubleWriteApi: { rewrite: vi.fn() },
}));

describe("DocxDoubleWriteService", () => {
  beforeEach(() => vi.clearAllMocks());

  it("bridges an editor request to the active-Agent rewrite endpoint", async () => {
    let listener: ((envelope: any) => void) | undefined;
    const respond = vi.fn();
    vi.mocked(docxDoubleWriteApi.rewrite).mockResolvedValue({
      success: true,
      replacement_text: "new",
      summary: "clearer",
      model: "provider/model",
    });
    Object.assign(window, {
      electronAPI: {
        onDocxRewriteRequest: vi.fn((callback) => {
          listener = callback;
          return vi.fn();
        }),
        respondDocxRewriteRequest: respond,
      },
    });
    render(<DocxDoubleWriteService />);

    await act(async () => {
      listener?.({
        request_id: "req-1",
        payload: {
          success: true,
          instruction: "clearer",
          selected_text: "old",
          document_before: "before",
          document_after: "after",
          current_draft: "current draft",
          document_name: "report.docx",
          source_chat_id: "chat-source-1",
        },
      });
    });

    expect(docxDoubleWriteApi.rewrite).toHaveBeenCalledWith({
      instruction: "clearer",
      selected_text: "old",
      document_before: "before",
      document_after: "after",
      current_draft: "current draft",
      document_name: "report.docx",
      source_chat_id: "chat-source-1",
    });
    await waitFor(() =>
      expect(respond).toHaveBeenCalledWith({
        request_id: "req-1",
        result: {
          success: true,
          replacement_text: "new",
          summary: "clearer",
          model: "provider/model",
        },
      }),
    );
  });
});
