import { describe, expect, it } from "vitest";

import type { AiWelcomeGreeting } from "./types";
import {
  readWelcomeCache,
  recordDisplayedGreeting,
  storePreparedCandidates,
} from "./state";

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };
}

const candidate: AiWelcomeGreeting = {
  id: "ai.safe.1",
  locale: "zh-CN",
  group: "universal",
  title: "今天也可以慢慢开始",
  subtitle: "先放下一句话，后面的路会从这里长出来。",
  source: "ai",
  version: 1,
  scope: "new_chat_empty_welcome",
  tone: "natural",
  canEnterChatHistory: false,
  validFrom: "2026-09-27T00:00:00+08:00",
  validUntil: "2026-09-28T00:00:00+08:00",
  scenarioKeys: ["universal"],
  reviewStatus: "approved",
  policyVersion: "welcome-v1.2026-09-27",
};

describe("welcome greeting local cache", () => {
  it("keeps only the latest eight distinct displayed ids", () => {
    const storage = memoryStorage();
    for (let index = 0; index < 10; index += 1) {
      recordDisplayedGreeting(storage, "default", "zh-CN", `fixed.${index}`);
    }
    recordDisplayedGreeting(storage, "default", "zh-CN", "fixed.7");

    expect(
      readWelcomeCache(storage, "default", "zh-CN").recentGreetingIds,
    ).toEqual([
      "fixed.2",
      "fixed.3",
      "fixed.4",
      "fixed.5",
      "fixed.6",
      "fixed.8",
      "fixed.9",
      "fixed.7",
    ]);
  });

  it("stores reviewed candidates for a later entry without storing a snapshot", () => {
    const storage = memoryStorage();
    storePreparedCandidates(storage, "default", "zh-CN", [candidate]);

    const cache = readWelcomeCache(storage, "default", "zh-CN");
    expect(cache.aiCandidates).toEqual([candidate]);
    expect(cache).not.toHaveProperty("snapshot");
  });

  it("isolates cache data by agent and locale", () => {
    const storage = memoryStorage();
    recordDisplayedGreeting(storage, "default", "zh-CN", "fixed.one");

    expect(
      readWelcomeCache(storage, "writer", "zh-CN").recentGreetingIds,
    ).toEqual([]);
    expect(
      readWelcomeCache(storage, "default", "en-US").recentGreetingIds,
    ).toEqual([]);
  });
});
