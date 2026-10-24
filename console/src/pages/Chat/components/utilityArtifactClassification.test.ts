import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { utilityIntelligenceApi } from "@/api";
import {
  classifyUtilityArtifactsCached,
  clearUtilityArtifactClassificationCache,
  clearUtilityArtifactClassificationMemoryCache,
  shouldClassifyUtilityArtifacts,
} from "./utilityArtifactClassification";

vi.mock("@/api", () => ({
  utilityIntelligenceApi: {
    classifyUtilityArtifacts: vi.fn(),
  },
}));

const classifyUtilityArtifacts = vi.mocked(
  utilityIntelligenceApi.classifyUtilityArtifacts,
);

describe("utility artifact classification cache", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    clearUtilityArtifactClassificationCache();
    classifyUtilityArtifacts.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shares one request and never retries a failed hidden enhancement", async () => {
    classifyUtilityArtifacts.mockResolvedValue({
      status: "failed",
      model: "utility/model",
      artifacts: [],
    });

    const first = classifyUtilityArtifactsCached("一段作品");
    const second = classifyUtilityArtifactsCached("一段作品");

    await expect(first).resolves.toMatchObject({ status: "failed" });
    await expect(second).resolves.toMatchObject({ status: "failed" });
    expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(1);
  });

  it("filters short casual chat before any auxiliary model request", () => {
    expect(shouldClassifyUtilityArtifacts("行，那就没事。")).toBe(true);
    expect(shouldClassifyUtilityArtifacts("   ")).toBe(false);
    expect(
      shouldClassifyUtilityArtifacts("《夜航》\n\n灯落下。\n船还在走。"),
    ).toBe(true);
    expect(
      shouldClassifyUtilityArtifacts(
        "这是一段足够完整的长文本，用来判断其中是否包含独立的诗歌、信函、文案或脚本作品。".repeat(
          3,
        ),
      ),
    ).toBe(true);
  });

  it("does not retry a successful empty classification", async () => {
    classifyUtilityArtifacts.mockResolvedValue({
      status: "classified",
      model: "utility/model",
      artifacts: [],
    });

    await expect(
      classifyUtilityArtifactsCached("普通回答"),
    ).resolves.toMatchObject({ status: "classified", artifacts: [] });

    expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(1);
  });

  it("restores a classified card after memory is reset", async () => {
    const text = "《陶城记》\n\n一杯土在这里待了很久。";
    classifyUtilityArtifacts.mockResolvedValue({
      status: "classified",
      model: "utility/model",
      artifacts: [
        {
          kind: "creative",
          title: "陶城记",
          source_text: text,
          start: 0,
          end: text.length,
        },
      ],
    });

    await classifyUtilityArtifactsCached(text, "message-poem:0");
    clearUtilityArtifactClassificationMemoryCache();

    await expect(
      classifyUtilityArtifactsCached(text, "message-poem:0"),
    ).resolves.toMatchObject({
      status: "classified",
      artifacts: [{ title: "陶城记" }],
    });
    expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(1);
  });

  it("persists a classified empty result so plain replies are not rechecked", async () => {
    const text = "这是普通回答，不是独立作品。";
    classifyUtilityArtifacts.mockResolvedValue({
      status: "classified",
      model: "utility/model",
      artifacts: [],
    });

    await classifyUtilityArtifactsCached(text, "message-plain:0");
    clearUtilityArtifactClassificationMemoryCache();

    await expect(
      classifyUtilityArtifactsCached(text, "message-plain:0"),
    ).resolves.toMatchObject({
      status: "classified",
      artifacts: [],
    });
    expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(1);
  });

  it("reclassifies when the text changes under the same message key", async () => {
    classifyUtilityArtifacts.mockResolvedValue({
      status: "classified",
      model: "utility/model",
      artifacts: [],
    });

    await classifyUtilityArtifactsCached("第一版内容", "message-edited:0");
    clearUtilityArtifactClassificationMemoryCache();
    await classifyUtilityArtifactsCached("第二版内容", "message-edited:0");

    expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(2);
  });
});
