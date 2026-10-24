import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePlanState } from "./usePlanState";

const { mockGetCurrentPlan, mockSubscribe, planSubscriber, sessionState } =
  vi.hoisted(() => {
    const planSubscriber = {
      current: undefined as
        | ((plan: Record<string, unknown> | null, sessionId?: string) => void)
        | undefined,
    };
    return {
      mockGetCurrentPlan: vi.fn(),
      mockSubscribe: vi.fn((subscriber: typeof planSubscriber.current) => {
        planSubscriber.current = subscriber;
        return () => undefined;
      }),
      planSubscriber,
      sessionState: { currentSessionId: "chat-1" },
    };
  });

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: () => sessionState,
}));

vi.mock("../../api/modules/plan", () => ({
  planApi: { getCurrentPlan: mockGetCurrentPlan },
  subscribePlanUpdates: mockSubscribe,
}));

describe("usePlanState", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    planSubscriber.current = undefined;
    sessionState.currentSessionId = "chat-1";
    (window as any).currentSessionId = "chat-1";
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("serializes fallback refreshes when the backend is slow", async () => {
    let resolveFirst: ((value: unknown) => void) | undefined;
    mockGetCurrentPlan.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );
    mockGetCurrentPlan.mockResolvedValue(null);

    renderHook(() => usePlanState({ pollIntervalMs: 1000 }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockGetCurrentPlan).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(mockGetCurrentPlan).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst?.(null);
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(mockGetCurrentPlan).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(mockGetCurrentPlan).toHaveBeenCalledTimes(2);
  });

  it("clears the previous session plan while the new session is loading", async () => {
    mockGetCurrentPlan.mockResolvedValueOnce({
      id: "plan-chat-1",
      name: "旧会话目标",
      description: "只属于 chat-1",
      expected_outcome: "",
      state: "in_progress",
      subtasks: [],
      created_at: null,
      finished_at: null,
      outcome: null,
    });

    const { result, rerender } = renderHook(() =>
      usePlanState({ pollIntervalMs: 0 }),
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.plan?.name).toBe("旧会话目标");

    mockGetCurrentPlan.mockImplementationOnce(() => new Promise(() => {}));
    sessionState.currentSessionId = "chat-2";
    (window as any).currentSessionId = "chat-2";
    rerender();
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.loading).toBe(true);
    expect(result.current.plan).toBeNull();
  });

  it("does not let a late plan response overwrite a newer session", async () => {
    let resolveOld: ((value: unknown) => void) | undefined;
    mockGetCurrentPlan.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    mockGetCurrentPlan.mockResolvedValueOnce(null);

    const { result, rerender } = renderHook(() =>
      usePlanState({ pollIntervalMs: 0 }),
    );
    await act(async () => {
      await Promise.resolve();
    });

    sessionState.currentSessionId = "chat-2";
    (window as any).currentSessionId = "chat-2";
    rerender();
    await act(async () => {
      await Promise.resolve();
    });

    await act(async () => {
      resolveOld?.({
        id: "late-plan-chat-1",
        name: "迟到的旧目标",
        description: "不应进入 chat-2",
        expected_outcome: "",
        state: "in_progress",
        subtasks: [],
        created_at: null,
        finished_at: null,
        outcome: null,
      });
      await Promise.resolve();
    });

    expect(mockGetCurrentPlan).toHaveBeenCalledTimes(2);
    expect(result.current.plan).toBeNull();
  });

  it("ignores a session-scoped plan event while session identity is unavailable", async () => {
    mockGetCurrentPlan.mockResolvedValue(null);
    const { result } = renderHook(() => usePlanState({ pollIntervalMs: 0 }));
    await act(async () => {
      await Promise.resolve();
    });

    (window as any).currentSessionId = "";
    act(() => {
      planSubscriber.current?.(
        {
          id: "plan-other-session",
          name: "其他会话目标",
          description: "不应在切换窗口闪现",
          expected_outcome: "",
          state: "in_progress",
          subtasks: [],
          created_at: null,
          finished_at: null,
          outcome: null,
        },
        "chat-other",
      );
    });

    expect(result.current.plan).toBeNull();
  });

  it("does not report abandoned steps as completed progress", async () => {
    mockGetCurrentPlan.mockResolvedValue({
      id: "plan-progress",
      name: "计划进度",
      description: "区分完成与放弃",
      expected_outcome: "",
      state: "in_progress",
      subtasks: [
        {
          name: "已完成",
          description: "",
          expected_outcome: "",
          outcome: "完成",
          state: "done",
          created_at: null,
          finished_at: null,
        },
        {
          name: "已放弃",
          description: "",
          expected_outcome: "",
          outcome: null,
          state: "abandoned",
          created_at: null,
          finished_at: null,
        },
        {
          name: "待处理",
          description: "",
          expected_outcome: "",
          outcome: null,
          state: "todo",
          created_at: null,
          finished_at: null,
        },
      ],
      created_at: null,
      finished_at: null,
      outcome: null,
    });

    const { result } = renderHook(() => usePlanState({ pollIntervalMs: 0 }));
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.doneCount).toBe(1);
    expect(result.current.totalCount).toBe(3);
    expect(result.current.percent).toBe(33);
  });
});
