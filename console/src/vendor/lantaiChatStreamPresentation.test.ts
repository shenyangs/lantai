import { describe, expect, it, vi } from "vitest";

import { createStreamPresentationScheduler } from "./lantaiChatStreamPresentation";

describe("createStreamPresentationScheduler", () => {
  it("turns a provider burst into a bounded number of ordered paint commits", async () => {
    const frames: Array<() => void> = [];
    const commits: Array<Array<Record<string, unknown>>> = [];
    const scheduler = createStreamPresentationScheduler(
      (payloads) => commits.push(payloads),
      {
        requestFrame: (callback) => {
          frames.push(callback);
          return frames.length;
        },
        cancelFrame: vi.fn(),
      },
    );
    const text =
      "澜台的流式输出应该稳定顺滑地逐帧呈现，而不是跟着网络突发卡顿。";

    scheduler.enqueue({
      object: "message",
      id: "answer-1",
      type: "message",
      status: "in_progress",
    });
    Array.from(text).forEach((character) => {
      scheduler.enqueue({
        object: "content",
        msg_id: "answer-1",
        delta: true,
        text: character,
        status: "in_progress",
      });
    });
    scheduler.enqueue({
      object: "response",
      id: "response-1",
      status: "completed",
    });

    // A terminal frame is a correctness boundary: it flushes the remaining
    // backlog even if the browser never grants another animation frame.
    expect(commits).toHaveLength(1);
    while (frames.length > 0) frames.shift()!();
    await scheduler.whenIdle();

    const flattened = commits.flat();
    expect(
      flattened
        .filter((payload) => payload.object === "content")
        .map((payload) => payload.text)
        .join(""),
    ).toBe(text);
    expect(flattened.at(0)?.object).toBe("message");
    expect(flattened.at(-1)?.object).toBe("response");
    expect(commits.length).toBeGreaterThan(0);
    expect(commits.length).toBeLessThan(Array.from(text).length);
  });

  it("flushes pending control frames and first visible reasoning atomically", () => {
    const frames: Array<() => void> = [];
    const commits: Array<Array<Record<string, unknown>>> = [];
    const cancelFrame = vi.fn();
    const scheduler = createStreamPresentationScheduler(
      (payloads) => commits.push(payloads),
      {
        requestFrame: (callback) => {
          frames.push(callback);
          return frames.length;
        },
        cancelFrame,
      },
    );

    scheduler.enqueue({
      object: "message",
      id: "reasoning-1",
      type: "reasoning",
      status: "in_progress",
    });
    scheduler.enqueue({
      object: "content",
      msg_id: "reasoning-1",
      delta: true,
      thinking: "先展示 Thinking",
      status: "in_progress",
    });
    scheduler.flushNow();

    expect(cancelFrame).toHaveBeenCalledTimes(1);
    expect(commits).toEqual([
      [
        expect.objectContaining({ object: "message", id: "reasoning-1" }),
        expect.objectContaining({
          object: "content",
          thinking: "先展示 Thinking",
        }),
      ],
    ]);
  });

  it("flushes all buffered text immediately when the terminal response arrives", async () => {
    const frames: Array<() => void> = [];
    const commits: Array<Array<Record<string, unknown>>> = [];
    const scheduler = createStreamPresentationScheduler(
      (payloads) => commits.push(payloads),
      {
        requestFrame: (callback) => {
          frames.push(callback);
          return frames.length;
        },
        cancelFrame: vi.fn(),
        shouldDeferPresentation: () => true,
      },
    );

    scheduler.enqueue({
      object: "content",
      msg_id: "answer-terminal",
      delta: true,
      text: "即使窗口失焦，最终结果也必须立即出现",
      status: "in_progress",
    });
    scheduler.enqueue({
      object: "response",
      id: "response-terminal",
      status: "completed",
    });

    await scheduler.whenIdle();
    expect(commits.flat()).toEqual([
      expect.objectContaining({ object: "content" }),
      expect.objectContaining({ object: "response", status: "completed" }),
    ]);
  });

  it("keeps the DOM stable during a user scroll gesture and resumes afterward", async () => {
    const frames: Array<() => void> = [];
    const commits: Array<Array<Record<string, unknown>>> = [];
    let userIsScrolling = true;
    const scheduler = createStreamPresentationScheduler(
      (payloads) => commits.push(payloads),
      {
        requestFrame: (callback) => {
          frames.push(callback);
          return frames.length;
        },
        cancelFrame: vi.fn(),
        shouldDeferPresentation: () => userIsScrolling,
      },
    );

    scheduler.enqueue({
      object: "content",
      msg_id: "answer-scroll",
      delta: true,
      text: "滚动时先缓冲",
      status: "in_progress",
    });

    frames.shift()!();
    expect(commits).toHaveLength(0);
    expect(frames).toHaveLength(1);

    userIsScrolling = false;
    frames.shift()!();
    await scheduler.whenIdle();

    expect(commits).toEqual([
      [
        expect.objectContaining({
          object: "content",
          text: "滚动时先缓冲",
        }),
      ],
    ]);
  });

  it("uses a watchdog when an occluded window receives no animation frame", async () => {
    vi.useFakeTimers();
    try {
      const commits: Array<Array<Record<string, unknown>>> = [];
      const scheduler = createStreamPresentationScheduler(
        (payloads) => commits.push(payloads),
        {
          requestFrame: () => 77,
          cancelFrame: vi.fn(),
          maximumFrameWaitMs: 120,
        },
      );

      scheduler.enqueue({
        object: "content",
        msg_id: "background-answer",
        delta: true,
        text: "后台会话仍然持续更新",
        status: "in_progress",
      });
      expect(commits).toHaveLength(0);

      await vi.advanceTimersByTimeAsync(120);
      scheduler.flushNow();
      await scheduler.whenIdle();

      expect(
        commits
          .flat()
          .map((payload) => payload.text || "")
          .join(""),
      ).toBe("后台会话仍然持续更新");
    } finally {
      vi.useRealTimers();
    }
  });

  it("coalesces token-sized chunks before committing Markdown-heavy messages", async () => {
    const frames: Array<() => void> = [];
    const commits: Array<Array<Record<string, unknown>>> = [];
    const scheduler = createStreamPresentationScheduler(
      (payloads) => commits.push(payloads),
      {
        requestFrame: (callback) => {
          frames.push(callback);
          return frames.length;
        },
        cancelFrame: vi.fn(),
      },
    );
    const text = "澜台需要让长回答持续可见，而不是把主线程耗在逐 token 重渲染上。".repeat(40);

    Array.from(text).forEach((character) => {
      scheduler.enqueue({
        object: "content",
        msg_id: "long-answer",
        delta: true,
        text: character,
        status: "in_progress",
      });
    });
    while (frames.length > 0) frames.shift()!();
    await scheduler.whenIdle();

    expect(
      commits
        .flat()
        .map((payload) => payload.text || "")
        .join(""),
    ).toBe(text);
    expect(commits.length).toBeLessThanOrEqual(12);
  });
});
