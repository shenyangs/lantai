import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "../request";
import { utilityIntelligenceApi } from "./utilityIntelligence";

vi.mock("../request", () => ({ request: vi.fn() }));

const mockedRequest = vi.mocked(request);

describe("utilityIntelligenceApi", () => {
  beforeEach(() => {
    mockedRequest.mockReset();
  });

  it("loads and updates the isolated utility model config", async () => {
    const config = {
      mode: "dedicated" as const,
      background_processing_enabled: true,
      model: { provider_id: "provider-1", model: "model-1" },
      timeout_seconds: 12,
    };

    await utilityIntelligenceApi.getUtilityIntelligenceConfig();
    await utilityIntelligenceApi.updateUtilityIntelligenceConfig(config);

    expect(mockedRequest).toHaveBeenNthCalledWith(
      1,
      "/utility-intelligence/config",
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      2,
      "/utility-intelligence/config",
      {
        method: "PUT",
        body: JSON.stringify(config),
      },
    );
  });
});
