import { describe, expect, it, vi } from "vitest";

import {
  hasUsableUniverViewport,
  MIN_UNIVER_VIEWPORT_HEIGHT,
  MIN_UNIVER_VIEWPORT_WIDTH,
  observeUsableUniverViewport,
} from "../src/renderer/univer-viewport";

describe("Univer embedded viewport readiness", () => {
  it("rejects the zero-height canvas host used during hidden BrowserView startup", () => {
    expect(
      hasUsableUniverViewport({
        clientWidth: 900,
        clientHeight: 0,
      }),
    ).toBe(false);
  });

  it("accepts the host only after both interactive dimensions are available", () => {
    expect(
      hasUsableUniverViewport({
        clientWidth: MIN_UNIVER_VIEWPORT_WIDTH,
        clientHeight: MIN_UNIVER_VIEWPORT_HEIGHT,
      }),
    ).toBe(true);
    expect(
      hasUsableUniverViewport({
        clientWidth: MIN_UNIVER_VIEWPORT_WIDTH - 1,
        clientHeight: MIN_UNIVER_VIEWPORT_HEIGHT,
      }),
    ).toBe(false);
  });
});

describe("Univer viewport lifecycle", () => {
  it("moves 0→positive exactly once and continues resize refits", () => {
    const dimensions = {
      clientWidth: 900,
      clientHeight: 0,
    };
    const element = dimensions as HTMLElement;
    let observerCheck: () => void = () => undefined;
    let pollCheck: () => void = () => undefined;
    const disconnect = vi.fn();
    const clearInterval = vi.fn();
    const onReady = vi.fn();
    const onResize = vi.fn();
    const dispose = observeUsableUniverViewport(element, onReady, onResize, {
      createObserver: (callback) => {
        observerCheck = callback;
        return { observe: vi.fn(), disconnect };
      },
      setInterval: (callback) => {
        pollCheck = callback;
        return 19;
      },
      clearInterval,
    });

    observerCheck();
    pollCheck();
    expect(onReady).not.toHaveBeenCalled();
    dimensions.clientHeight = 560;
    pollCheck();
    observerCheck();
    observerCheck();
    expect(onReady).toHaveBeenCalledTimes(1);
    expect(onResize).toHaveBeenCalledTimes(2);
    expect(clearInterval).toHaveBeenCalledWith(19);

    dispose();
    expect(disconnect).toHaveBeenCalledOnce();
  });
});
