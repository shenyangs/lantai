import { beforeEach, describe, expect, it, vi } from "vitest";
import { researchApi } from "./research";
import { request } from "../request";

vi.mock("../request", () => ({ request: vi.fn() }));

describe("research report saving", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(request).mockResolvedValue({});
  });

  it("resaves retrieved evidence without sending read-only source locations", async () => {
    const evidence = { source_version_id: "source-1", start: 12, end: 20, quote: "原始证据不能丢失", source_block_index: 3 };
    const body = {
      revision: 2, text: "修订后的报告", expected_report_id: "report-old", idempotency_key: "save-edit",
      claims: [{ start: 0, end: 7, evidence: [evidence] }],
    };
    const before = JSON.stringify(body);

    await researchApi.save("project/1", body);

    expect(request).toHaveBeenCalledWith("/workspace/research/projects/project%2F1/reports", {
      method: "POST",
      body: JSON.stringify({ ...body, claims: [{ start: 0, end: 7, evidence: [{
        source_version_id: "source-1", start: 12, end: 20, quote: "原始证据不能丢失",
      }] }] }),
    });
    expect(JSON.stringify(body)).toBe(before);
  });

  it("preserves a first report with no citation anchors", async () => {
    const body = { revision: 1, text: "草稿", claims: [], expected_report_id: null, idempotency_key: "save-first" };
    await researchApi.save("project-1", body);
    expect(request).toHaveBeenCalledWith("/workspace/research/projects/project-1/reports", {
      method: "POST", body: JSON.stringify(body),
    });
  });
});
