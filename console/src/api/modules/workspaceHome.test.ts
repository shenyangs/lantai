import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../request", () => ({ request: vi.fn() }));

import { request } from "../request";
import { workspaceApi } from "./workspace";

describe("workspaceApi.getWikiHome", () => {
  beforeEach(() => vi.clearAllMocks());

  it("normalizes the single knowledge-workspace projection", async () => {
    vi.mocked(request).mockResolvedValue({
      wiki: {
        pages: [
          {
            filename: "note.md",
            path: ".wiki/pages/note.md",
            size: 10,
            created_time: "2026-08-12T08:00:00Z",
            modified_time: "2026-08-12T09:00:00Z",
            title: "Note",
            summary: "Summary",
            links: [],
            backlinks: [],
            sources: [],
            page_type: "page",
            category: "root",
          },
        ],
        sources: [],
        graph: { nodes: [], edges: [] },
        stats: { pages: 1 },
        overview: null,
        queue_summary: {
          pending: 0,
          running: 0,
          finished: 0,
          failed: 0,
          cancelled: 0,
          total: 0,
          latest: [],
        },
      },
      purpose: {
        filename: "purpose.md",
        path: ".wiki/purpose.md",
        size: 10,
        created_time: "2026-08-12T08:00:00Z",
        modified_time: "2026-08-12T09:00:00Z",
        content: "# Purpose",
      },
      lint: { issues: [], stats: {} },
      insights: { insights: [], stats: {} },
      review: { items: [], stats: { total: 0 } },
      structure: { pages: [], fields: [], stats: {} },
    });

    const home = await workspaceApi.getWikiHome();

    expect(request).toHaveBeenCalledWith("/workspace/wiki/home");
    expect(home.wiki.pages[0].updated_at).toBe(
      Date.parse("2026-08-12T09:00:00Z"),
    );
    expect(home.purpose.content).toBe("# Purpose");
    expect(home.wiki.queue_summary.failed).toBe(0);
    expect(home.structure.pages).toEqual([]);
  });
});
