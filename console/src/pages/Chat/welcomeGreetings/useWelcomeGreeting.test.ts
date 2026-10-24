import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { defaultDisplaySettings } from "../../../theme/appearance";
import { readWelcomeCache, storePreparedCandidates } from "./state";
import { useWelcomeGreeting } from "./useWelcomeGreeting";

const { prepareMock } = vi.hoisted(() => ({
  prepareMock: vi.fn(),
}));

vi.mock("../../../api/modules/welcomeGreetings", () => ({
  welcomeGreetingsApi: {
    prepare: prepareMock,
  },
}));

const preparedCandidate = {
  id: "ai.prepared.next-entry",
  locale: "zh-CN" as const,
  group: "universal",
  title: "今天也留一点余地",
  subtitle: "想从哪里开始都可以。",
  source: "ai" as const,
  version: 1,
  scope: "new_chat_empty_welcome" as const,
  tone: "natural" as const,
  canEnterChatHistory: false as const,
  validFrom: "2000-01-01T00:00:00+08:00",
  validUntil: "3000-01-01T00:00:00+08:00",
  scenarioKeys: ["universal"],
  reviewStatus: "approved" as const,
  policyVersion: "welcome-v1.2026-09-27",
};

describe("useWelcomeGreeting", () => {
  beforeEach(() => {
    window.localStorage.clear();
    prepareMock.mockReset();
  });

  it("stores AI copy for a later entry without changing the visible snapshot", async () => {
    prepareMock.mockResolvedValue({
      status: "ready",
      candidates: [preparedCandidate],
      policy_version: "welcome-v1.2026-09-27",
    });

    const { result } = renderHook(() =>
      useWelcomeGreeting({
        agentId: "default",
        displaySettings: defaultDisplaySettings,
        enabled: true,
        entryId: "entry-current",
        fallbackTitle: "今天想做点什么？",
        fallbackSubtitle: "从这里开始。",
        locale: "zh-CN",
      }),
    );
    const visibleGreeting = result.current;

    await waitFor(() => {
      expect(
        readWelcomeCache(window.localStorage, "default", "zh-CN").aiCandidates,
      ).toEqual([preparedCandidate]);
    });

    expect(result.current).toBe(visibleGreeting);
    expect(result.current.id).not.toBe(preparedCandidate.id);
    expect(prepareMock).toHaveBeenCalledTimes(1);
  });

  it("does not call the model preparation API when persona is off", async () => {
    const { result } = renderHook(() =>
      useWelcomeGreeting({
        agentId: "default",
        displaySettings: {
          ...defaultDisplaySettings,
          personaMode: "off",
        },
        enabled: true,
        entryId: "entry-persona-off",
        fallbackTitle: "今天想做点什么？",
        fallbackSubtitle: "从这里开始。",
        locale: "zh-CN",
      }),
    );

    await Promise.resolve();

    expect(result.current.source).toBe("neutral");
    expect(prepareMock).not.toHaveBeenCalled();
  });

  it("does not select a cached AI candidate when AI welcome is off", async () => {
    storePreparedCandidates(window.localStorage, "default", "zh-CN", [
      preparedCandidate,
    ]);

    const { result } = renderHook(() =>
      useWelcomeGreeting({
        agentId: "default",
        displaySettings: {
          ...defaultDisplaySettings,
          aiWelcome: false,
        },
        enabled: true,
        entryId: "entry-ai-off",
        fallbackTitle: "今天想做点什么？",
        fallbackSubtitle: "从这里开始。",
        locale: "zh-CN",
      }),
    );

    await Promise.resolve();

    expect(result.current.source).toBe("fixed");
    expect(result.current.id).not.toBe(preparedCandidate.id);
    expect(prepareMock).not.toHaveBeenCalled();
  });

  it("does not cache an in-flight result after preparation is disabled", async () => {
    let resolvePreparation: ((value: unknown) => void) | undefined;
    prepareMock.mockReturnValue(
      new Promise((resolve) => {
        resolvePreparation = resolve;
      }),
    );
    const { rerender } = renderHook(
      ({ aiWelcome }) =>
        useWelcomeGreeting({
          agentId: "default",
          displaySettings: { ...defaultDisplaySettings, aiWelcome },
          enabled: true,
          entryId: "entry-cancelled",
          fallbackTitle: "今天想做点什么？",
          fallbackSubtitle: "从这里开始。",
          locale: "zh-CN",
        }),
      { initialProps: { aiWelcome: true } },
    );

    rerender({ aiWelcome: false });
    resolvePreparation?.({
      status: "ready",
      candidates: [preparedCandidate],
      policy_version: "welcome-v1.2026-09-27",
    });

    await waitFor(() => expect(prepareMock).toHaveBeenCalledTimes(1));
    expect(
      readWelcomeCache(window.localStorage, "default", "zh-CN").aiCandidates,
    ).toEqual([]);
  });

  it("does not prepare copy outside a new empty chat", async () => {
    renderHook(() =>
      useWelcomeGreeting({
        agentId: "default",
        displaySettings: defaultDisplaySettings,
        enabled: false,
        entryId: "existing-chat",
        fallbackTitle: "今天想做点什么？",
        fallbackSubtitle: "从这里开始。",
        locale: "zh-CN",
      }),
    );

    await Promise.resolve();

    expect(prepareMock).not.toHaveBeenCalled();
  });

  it("falls back without preparing when the locale is unavailable", async () => {
    const { result } = renderHook(() =>
      useWelcomeGreeting({
        agentId: "default",
        displaySettings: defaultDisplaySettings,
        enabled: true,
        entryId: "entry-missing-locale",
        fallbackTitle: "Welcome",
        fallbackSubtitle: "Start here.",
        locale: undefined as unknown as string,
      }),
    );

    await Promise.resolve();

    expect(result.current.title).toBe("Welcome");
    expect(result.current.subtitle).toBe("Start here.");
    expect(result.current.locale).toBe("und");
    expect(prepareMock).not.toHaveBeenCalled();
  });
});
