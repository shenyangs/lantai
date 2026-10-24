import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "../request";
import { imageGenerationApi } from "./imageGeneration";

vi.mock("../request", () => ({ request: vi.fn() }));

const mockedRequest = vi.mocked(request);

describe("imageGenerationApi", () => {
  beforeEach(() => {
    mockedRequest.mockReset();
  });

  it("loads and updates in-chat image generation routing", async () => {
    const config = {
      enabled: true,
      models: [
        { provider_id: "provider-1", model: "image-1" },
        { provider_id: "provider-2", model: "image-2" },
      ],
      timeout_seconds: 120,
      works_cover_enabled: false,
      works_cover_model: { provider_id: "", model: "" },
    };

    await imageGenerationApi.getImageGenerationConfig();
    await imageGenerationApi.updateImageGenerationConfig(config);

    expect(mockedRequest).toHaveBeenNthCalledWith(
      1,
      "/image-generation/config",
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      2,
      "/image-generation/config",
      {
        method: "PUT",
        body: JSON.stringify(config),
      },
    );
  });
});
