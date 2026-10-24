import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "../request";
import { webSearchApi } from "./webSearch";

vi.mock("../request", () => ({ request: vi.fn() }));

describe("webSearchApi", () => {
  beforeEach(() => vi.mocked(request).mockReset());

  it("uses a dedicated config endpoint instead of the DeepSeek provider", async () => {
    vi.mocked(request).mockResolvedValue({
      configured: true,
      enabled: true,
      provider: "deepseek",
      search_count: 0,
      fetch_count: 0,
      migrated_from_quick_search: false,
      credential_error: false,
      last_used_at: "",
    });

    await webSearchApi.updateConfig({
      api_key: "sk-separate",
      enabled: true,
    });

    expect(request).toHaveBeenCalledWith("/web-search/config", {
      method: "PUT",
      body: JSON.stringify({ api_key: "sk-separate", enabled: true }),
    });
  });
});
