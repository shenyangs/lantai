import { describe, expect, it } from "vitest";

import { FIXED_WELCOME_CATALOG } from "./catalog.zh-CN";
import {
  buildWelcomeSnapshot,
  eligibleScenarioKeys,
  isSafeAiWelcomeGreeting,
  type WelcomeSnapshot,
} from "./policy";
import type { AiWelcomeGreeting } from "./types";

const baseInput = {
  agentId: "default",
  locale: "zh-CN",
  personaMode: "balanced" as const,
  culturalCalendar: "none" as const,
  recentGreetingIds: [] as string[],
  aiCandidates: [],
};

describe("dynamic welcome policy", () => {
  it("ships 80 reviewed atomic fixed pairs across every fallback layer", () => {
    expect(FIXED_WELCOME_CATALOG).toHaveLength(80);
    expect(
      FIXED_WELCOME_CATALOG.filter((item) => item.group === "universal"),
    ).toHaveLength(20);
    expect(
      FIXED_WELCOME_CATALOG.filter((item) => item.group.startsWith("time.")),
    ).toHaveLength(30);
    expect(
      FIXED_WELCOME_CATALOG.filter((item) => item.group.startsWith("rhythm.")),
    ).toHaveLength(10);
    expect(
      FIXED_WELCOME_CATALOG.filter((item) =>
        item.group.startsWith("festival."),
      ),
    ).toHaveLength(20);

    for (const item of FIXED_WELCOME_CATALOG) {
      expect(item.title.trim()).not.toBe("");
      expect(item.subtitle.trim()).not.toBe("");
      expect(item.title.length).toBeLessThanOrEqual(24);
      expect(item.subtitle.length).toBeLessThanOrEqual(48);
      expect(item.scope).toBe("new_chat_empty_welcome");
      expect(item.canEnterChatHistory).toBe(false);
      expect(item.requiresCulturalPack).toBe(
        item.group.startsWith("festival."),
      );
    }

    expect(new Set(FIXED_WELCOME_CATALOG.map((item) => item.id)).size).toBe(
      FIXED_WELCOME_CATALOG.length,
    );
    expect(
      new Set(
        FIXED_WELCOME_CATALOG.map(
          (item) => `${item.title}\u0000${item.subtitle}`,
        ),
      ).size,
    ).toBe(FIXED_WELCOME_CATALOG.length);

    const expectedGroupSizes: Record<string, number> = {
      universal: 20,
      "time.dawn": 5,
      "time.morning": 5,
      "time.noon": 5,
      "time.afternoon": 5,
      "time.evening": 5,
      "time.late": 5,
      "rhythm.week_start": 2,
      "rhythm.week_middle": 2,
      "rhythm.weekend": 2,
      "rhythm.month_start": 2,
      "rhythm.month_end": 2,
      "festival.new_year": 2,
      "festival.lunar_eve": 2,
      "festival.spring_festival": 2,
      "festival.lantern": 2,
      "festival.dragon_boat": 2,
      "festival.qixi": 2,
      "festival.mid_autumn": 2,
      "festival.national_day": 2,
      "festival.christmas": 2,
      "festival.winter_solstice": 2,
    };
    const actualGroupSizes = Object.fromEntries(
      Object.keys(expectedGroupSizes).map((group) => [
        group,
        FIXED_WELCOME_CATALOG.filter((item) => item.group === group).length,
      ]),
    );
    expect(actualGroupSizes).toEqual(expectedGroupSizes);
  });

  it("keeps every fixed pair free of inferred state and prior-chat continuity", () => {
    const unsafeContinuity =
      /欢迎回来|沈行知|上次|继续|接着|没做完|仍然|还是|任务|作品|工作到|熬夜|凌晨|辛苦了|看起来你|似乎你|感觉你|这次/;

    for (const item of FIXED_WELCOME_CATALOG) {
      expect(`${item.title}${item.subtitle}`).not.toMatch(unsafeContinuity);
    }
  });

  it("uses only current clock and explicit calendar permission for eligibility", () => {
    expect(eligibleScenarioKeys(new Date(2026, 8, 27, 6, 30), "none")).toEqual(
      expect.arrayContaining(["universal", "time.dawn", "rhythm.weekend"]),
    );
    expect(
      eligibleScenarioKeys(new Date(2026, 11, 25, 10, 30), "none"),
    ).not.toContain("festival.christmas");
    expect(
      eligibleScenarioKeys(new Date(2026, 11, 25, 10, 30), "global"),
    ).toContain("festival.christmas");
  });

  it("keeps an entry snapshot stable even when time crosses a scenario boundary", () => {
    const first = buildWelcomeSnapshot({
      ...baseInput,
      entryId: "entry-1",
      now: new Date("2026-09-27T08:59:50+08:00"),
    });
    const reused = buildWelcomeSnapshot({
      ...baseInput,
      entryId: "entry-1",
      now: new Date("2026-09-27T09:01:00+08:00"),
      previousSnapshot: first,
    });

    expect(reused).toBe(first);
  });

  it("prioritizes a festival only after its cultural calendar is enabled", () => {
    const enabled = buildWelcomeSnapshot({
      ...baseInput,
      culturalCalendar: "global",
      entryId: "christmas-enabled",
      now: new Date("2026-12-25T10:30:00+08:00"),
    });
    const disabled = buildWelcomeSnapshot({
      ...baseInput,
      culturalCalendar: "none",
      entryId: "christmas-disabled",
      now: new Date("2026-12-25T10:30:00+08:00"),
    });

    expect(enabled.greeting.group).toBe("festival.christmas");
    expect(disabled.greeting.group).not.toBe("festival.christmas");
  });

  it("starts a new entry without carrying task state or chat history", () => {
    const previous: WelcomeSnapshot = {
      entryId: "old-entry",
      selectedAt: "2026-09-27T09:00:00.000Z",
      expiresAt: "2026-09-27T09:30:00.000Z",
      greeting: {
        id: "ai.old-task",
        title: "那份方案，我们接着改",
        subtitle: "上次没做完的第三节还在这里。",
        group: "universal",
        source: "ai",
        scope: "new_chat_empty_welcome",
        locale: "zh-CN",
        tone: "natural",
        version: 1,
        canEnterChatHistory: false,
      },
    };

    const next = buildWelcomeSnapshot({
      ...baseInput,
      entryId: "new-entry",
      now: new Date("2026-09-27T09:05:00+08:00"),
      previousSnapshot: previous,
    });

    expect(next).not.toBe(previous);
    expect(`${next.greeting.title}${next.greeting.subtitle}`).not.toMatch(
      /上次|继续|接着|没做完|方案|任务|作品/,
    );
  });

  it("falls back to neutral copy when persona is off", () => {
    const snapshot = buildWelcomeSnapshot({
      ...baseInput,
      personaMode: "off",
      entryId: "entry-off",
      now: new Date("2026-09-27T21:00:00+08:00"),
    });

    expect(snapshot.greeting.source).toBe("neutral");
    expect(snapshot.greeting.title).toBe("今天想做点什么？");
  });

  it("applies persona privacy changes without waiting for a new entry", () => {
    const dynamic = buildWelcomeSnapshot({
      ...baseInput,
      entryId: "entry-privacy-change",
      now: new Date("2026-09-27T21:00:00+08:00"),
    });
    const neutral = buildWelcomeSnapshot({
      ...baseInput,
      personaMode: "off",
      entryId: "entry-privacy-change",
      now: new Date("2026-09-27T21:01:00+08:00"),
      previousSnapshot: dynamic,
    });
    const restored = buildWelcomeSnapshot({
      ...baseInput,
      entryId: "entry-privacy-change",
      now: new Date("2026-09-27T21:02:00+08:00"),
      previousSnapshot: neutral,
    });

    expect(dynamic.greeting.source).not.toBe("neutral");
    expect(neutral.greeting.source).toBe("neutral");
    expect(neutral).not.toBe(dynamic);
    expect(restored.greeting.source).not.toBe("neutral");
    expect(restored).not.toBe(neutral);
  });

  it.each([
    "欢迎回来",
    "今天辛苦了",
    "看起来你有点累",
    "这次慢慢来",
    "沈行知在这里",
  ])(
    "rejects AI copy that infers presence, state, or continuity: %s",
    (title) => {
      const candidate: AiWelcomeGreeting = {
        id: "ai.unsafe",
        locale: "zh-CN",
        group: "universal",
        title,
        subtitle: "从这里开始。",
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

      expect(
        isSafeAiWelcomeGreeting(
          candidate,
          new Date("2026-09-27T09:00:00+08:00"),
          ["universal", "time.morning"],
          "zh-CN",
          "natural",
        ),
      ).toBe(false);
    },
  );

  it("invalidates cached festival AI copy as soon as its calendar is disabled", () => {
    const candidate: AiWelcomeGreeting = {
      id: "ai.christmas.opt-in",
      locale: "zh-CN",
      group: "festival.christmas",
      title: "圣诞快乐。",
      subtitle: "愿今天有一点惊喜。",
      source: "ai",
      version: 1,
      scope: "new_chat_empty_welcome",
      tone: "natural",
      canEnterChatHistory: false,
      validFrom: "2026-12-25T00:00:00+08:00",
      validUntil: "2026-12-26T00:00:00+08:00",
      scenarioKeys: ["universal", "festival.christmas"],
      reviewStatus: "approved",
      policyVersion: "welcome-v1.2026-09-27",
    };
    const now = new Date("2026-12-25T09:00:00+08:00");

    expect(
      isSafeAiWelcomeGreeting(
        candidate,
        now,
        ["universal", "time.morning", "festival.christmas"],
        "zh-CN",
        "natural",
      ),
    ).toBe(true);
    expect(
      isSafeAiWelcomeGreeting(
        candidate,
        now,
        ["universal", "time.morning"],
        "zh-CN",
        "natural",
      ),
    ).toBe(false);
  });

  it("rejects festival wording that an AI candidate labels as universal", () => {
    const candidate: AiWelcomeGreeting = {
      id: "ai.christmas.mislabeled",
      locale: "zh-CN",
      group: "universal",
      title: "圣诞快乐。",
      subtitle: "愿今天有一点惊喜。",
      source: "ai",
      version: 1,
      scope: "new_chat_empty_welcome",
      tone: "natural",
      canEnterChatHistory: false,
      validFrom: "2026-12-25T00:00:00+08:00",
      validUntil: "2026-12-26T00:00:00+08:00",
      scenarioKeys: ["universal"],
      reviewStatus: "approved",
      policyVersion: "welcome-v1.2026-09-27",
    };

    expect(
      isSafeAiWelcomeGreeting(
        candidate,
        new Date("2026-12-25T09:00:00+08:00"),
        ["universal", "time.morning"],
        "zh-CN",
        "natural",
      ),
    ).toBe(false);
  });

  it("accepts 新年 only under an explicitly eligible New Year scenario", () => {
    const candidate: AiWelcomeGreeting = {
      id: "ai.new-year",
      locale: "zh-CN",
      group: "festival.new_year",
      title: "新年好。",
      subtitle: "愿今天有个轻松的开头。",
      source: "ai",
      version: 1,
      scope: "new_chat_empty_welcome",
      tone: "natural",
      canEnterChatHistory: false,
      validFrom: "2027-01-01T00:00:00+08:00",
      validUntil: "2027-01-02T00:00:00+08:00",
      scenarioKeys: ["festival.new_year"],
      reviewStatus: "approved",
      policyVersion: "welcome-v1.2026-09-27",
    };
    const now = new Date("2027-01-01T09:00:00+08:00");

    expect(
      isSafeAiWelcomeGreeting(
        candidate,
        now,
        ["universal", "time.morning", "festival.new_year"],
        "zh-CN",
        "natural",
      ),
    ).toBe(true);
    expect(
      isSafeAiWelcomeGreeting(
        { ...candidate, group: "universal", scenarioKeys: ["universal"] },
        now,
        ["universal", "time.morning"],
        "zh-CN",
        "natural",
      ),
    ).toBe(false);
  });
});
