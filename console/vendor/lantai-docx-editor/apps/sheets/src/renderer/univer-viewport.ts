export const MIN_UNIVER_VIEWPORT_WIDTH = 320;
export const MIN_UNIVER_VIEWPORT_HEIGHT = 200;

/**
 * Univer snapshots its canvas size during construction. Electron may mount an
 * embedded Office view while its BrowserView is still 0×0; constructing at
 * that point leaves a visually painted but non-hit-testable 0px-high canvas
 * after the host reveals the view. Delay construction until the real workbook
 * viewport has arrived instead of trying to repair a poisoned render scene.
 */
export function hasUsableUniverViewport(
  element: Pick<HTMLElement, "clientWidth" | "clientHeight">,
): boolean {
  return (
    element.clientWidth >= MIN_UNIVER_VIEWPORT_WIDTH &&
    element.clientHeight >= MIN_UNIVER_VIEWPORT_HEIGHT
  );
}

type ViewportObserver = {
  observe(element: Element): void;
  disconnect(): void;
};

type ViewportWatchOptions = {
  createObserver?: (callback: () => void) => ViewportObserver;
  setInterval?: (callback: () => void, milliseconds: number) => number;
  clearInterval?: (id: number) => void;
  pollMilliseconds?: number;
};

/**
 * Wait for a genuinely interactive canvas host. The ready callback is
 * one-shot, while later ResizeObserver notifications remain available to
 * refit an already-created Univer scene.
 */
export function observeUsableUniverViewport(
  element: HTMLElement,
  onReady: () => void,
  onResize: () => void,
  options: ViewportWatchOptions = {},
): () => void {
  const createObserver =
    options.createObserver ??
    ((callback) => new ResizeObserver(() => callback()));
  const scheduleInterval = options.setInterval ?? window.setInterval.bind(window);
  const cancelInterval =
    options.clearInterval ?? window.clearInterval.bind(window);
  let ready = false;
  let disposed = false;
  let timer: number | undefined;
  const check = () => {
    if (disposed || !hasUsableUniverViewport(element)) return;
    if (!ready) {
      ready = true;
      if (timer !== undefined) {
        cancelInterval(timer);
        timer = undefined;
      }
      onReady();
      return;
    }
    onResize();
  };
  const observer = createObserver(check);
  observer.observe(element);
  timer = scheduleInterval(check, options.pollMilliseconds ?? 200);
  check();
  return () => {
    disposed = true;
    observer.disconnect();
    if (timer !== undefined) cancelInterval(timer);
  };
}
