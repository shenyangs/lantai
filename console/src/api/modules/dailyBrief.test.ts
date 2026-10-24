import { beforeEach, describe, expect, it, vi } from "vitest";
import { dailyBriefApi } from "./dailyBrief";

vi.mock("../request", () => ({ request: vi.fn() }));

import { request } from "../request";

describe("dailyBriefApi", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(request).mockResolvedValue({});
  });

  it("keeps home and workspace reads explicitly read-only", async () => {
    await dailyBriefApi.getHomeSnapshot();
    await dailyBriefApi.getWorkspaceSnapshot();

    expect(request).toHaveBeenNthCalledWith(
      1,
      "/daily-brief/home",
      {},
      { operation: "read" },
    );
    expect(request).toHaveBeenNthCalledWith(
      2,
      "/daily-brief/workspace",
      {},
      { operation: "read" },
    );
  });

  it("routes explicit authorization and preview actions through mutation policy", async () => {
    const grant = {
      explicit_consent: true,
      one_time: true,
      topic_ids: ["topic/1"],
      public_source_discovery: true,
      public_source_read: true,
      cloud_processing: "off" as const,
      allowed_context_refs: [],
      search_provider: "approved-search",
      model_provider: "",
      budget: {
        max_searches: 4,
        max_source_reads: 6,
        max_model_tokens: 4_000,
        max_cost_usd: 0.5,
      },
      expires_at: null,
      action_id: "grant-action",
    };
    await dailyBriefApi.createGrant(grant);
    await dailyBriefApi.createPreview({
      grant_id: "grant/1",
      local_date: "2026-09-21",
      timezone: "Asia/Shanghai",
      action_id: "preview-action",
    });

    expect(request).toHaveBeenNthCalledWith(
      1,
      "/daily-brief/grants",
      { method: "POST", body: JSON.stringify(grant) },
      { operation: "mutation" },
    );
    expect(request).toHaveBeenNthCalledWith(
      2,
      "/daily-brief/previews",
      {
        method: "POST",
        body: JSON.stringify({
          grant_id: "grant/1",
          local_date: "2026-09-21",
          timezone: "Asia/Shanghai",
          action_id: "preview-action",
        }),
      },
      { operation: "mutation" },
    );
  });

  it("uses one product-level activation mutation for automatic briefs", async () => {
    const activation = {
      explicit_consent: true,
      mode: "daily" as const,
      topic_ids: [],
      use_memory_clues: true,
      memory_context_processing_consent: true,
      timezone: "Asia/Shanghai",
      local_time: "08:00",
      home_visible: true,
      native_notifications: false,
      quiet_hours_start: null,
      quiet_hours_end: null,
      action_id: "activate-action",
    };

    await dailyBriefApi.activate(activation);

    expect(request).toHaveBeenCalledWith(
      "/daily-brief/activate",
      { method: "POST", body: JSON.stringify(activation) },
      { operation: "mutation" },
    );
  });

  it("encodes version-fenced subscription updates and grant revocation", async () => {
    await dailyBriefApi.updateSubscription("sub/1", {
      expected_revision: 3,
      status: "paused",
      action_id: "pause-action",
    });
    await dailyBriefApi.revokeGrant("grant/1", {
      expected_revision: 2,
      action_id: "revoke-action",
    });

    expect(request).toHaveBeenNthCalledWith(
      1,
      "/daily-brief/subscriptions/sub%2F1",
      {
        method: "PATCH",
        body: JSON.stringify({
          expected_revision: 3,
          status: "paused",
          action_id: "pause-action",
        }),
      },
      { operation: "mutation" },
    );
    expect(request).toHaveBeenNthCalledWith(
      2,
      "/daily-brief/grants/grant%2F1/revoke",
      {
        method: "POST",
        body: JSON.stringify({
          expected_revision: 2,
          action_id: "revoke-action",
        }),
      },
      { operation: "mutation" },
    );
  });
});
