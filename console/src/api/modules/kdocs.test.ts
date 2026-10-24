import { beforeEach, describe, expect, it, vi } from "vitest";

const requestMock = vi.hoisted(() => vi.fn());
vi.mock("../request", () => ({ request: requestMock }));

import { kdocsApi } from "./kdocs";

describe("kdocsApi", () => {
  beforeEach(() => requestMock.mockReset());

  it("encodes browse parameters without leaking empty values", async () => {
    requestMock.mockResolvedValue({ items: [] });
    await kdocsApi.listDocuments({
      view: "folder",
      drive_id: "drive / 一",
      parent_id: "folder-1",
      query: "",
      page_size: 50,
    });
    expect(requestMock.mock.calls[0][0]).toBe(
      "/skills/integrations/kdocs/documents?view=folder&drive_id=drive+%2F+%E4%B8%80&parent_id=folder-1&page_size=50",
    );
  });

  it("marks reads as bounded read operations", async () => {
    requestMock.mockResolvedValue({ status: "done" });
    await kdocsApi.readDocument({ file_id: "file-1", max_chars: 120000 });
    expect(requestMock).toHaveBeenCalledWith(
      "/skills/integrations/kdocs/documents/read",
      expect.objectContaining({ method: "POST" }),
      { operation: "read", timeoutMs: 130_000 },
    );
  });
});
