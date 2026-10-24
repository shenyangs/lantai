import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "../request";
import { docxDoubleWriteApi } from "./docxDoubleWrite";

vi.mock("../request", () => ({ request: vi.fn() }));

describe("docxDoubleWriteApi", () => {
  beforeEach(() => vi.mocked(request).mockReset());

  it("posts the complete live document and source chat identity", async () => {
    vi.mocked(request).mockResolvedValue({
      success: true,
      replacement_text: "new",
      summary: "clearer",
      model: "provider/model",
    });
    const payload = {
      instruction: "make it clear",
      selected_text: "old",
      document_before: "before".repeat(5000),
      document_after: "after".repeat(5000),
      current_draft: "",
      document_name: "report.docx",
      source_chat_id: "chat-source-1",
    };

    await docxDoubleWriteApi.rewrite(payload);

    expect(request).toHaveBeenCalledWith("/console/docx/rewrite", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  });
});
