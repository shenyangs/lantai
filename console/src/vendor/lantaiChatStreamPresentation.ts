export type LantaiStreamPayload = Record<string, unknown>;

type FrameHandle = number | ReturnType<typeof setTimeout>;

export interface StreamPresentationSchedulerOptions {
  requestFrame?: (callback: () => void) => FrameHandle;
  cancelFrame?: (handle: FrameHandle) => void;
  shouldDeferPresentation?: () => boolean;
  minimumVisibleCharactersPerFrame?: number;
  maximumVisibleCharactersPerFrame?: number;
  targetBacklogFrames?: number;
  /**
   * Browser animation frames may be throttled while the window is occluded or
   * the user visits another chat. Keep a short timer fallback so a live turn
   * never looks frozen until the component is remounted.
   */
  maximumFrameWaitMs?: number;
}

export interface StreamPresentationScheduler {
  enqueue: (payload: LantaiStreamPayload) => void;
  flushNow: () => void;
  whenIdle: () => Promise<void>;
  dispose: () => void;
}

// Markdown rendering and message-card reconciliation are materially more
// expensive than painting plain text. Commit a useful reading unit per frame
// instead of forcing React through an update for every few provider tokens.
const DEFAULT_MINIMUM_VISIBLE_CHARACTERS_PER_FRAME = 32;
const DEFAULT_MAXIMUM_VISIBLE_CHARACTERS_PER_FRAME = 384;
const DEFAULT_TARGET_BACKLOG_FRAMES = 10;
const DEFAULT_MAXIMUM_FRAME_WAIT_MS = 80;

function defaultRequestFrame(callback: () => void): FrameHandle {
  if (
    typeof window !== "undefined" &&
    typeof window.requestAnimationFrame === "function"
  ) {
    return window.requestAnimationFrame(() => callback());
  }
  return setTimeout(callback, 16);
}

function defaultCancelFrame(handle: FrameHandle): void {
  if (
    typeof window !== "undefined" &&
    typeof window.cancelAnimationFrame === "function" &&
    typeof handle === "number"
  ) {
    window.cancelAnimationFrame(handle);
    return;
  }
  clearTimeout(handle);
}

function smoothableTextField(
  payload: LantaiStreamPayload,
): "text" | "thinking" | null {
  if (payload.object !== "content" || payload.delta !== true) return null;
  if (typeof payload.text === "string" && payload.text) return "text";
  if (typeof payload.thinking === "string" && payload.thinking) {
    return "thinking";
  }
  return null;
}

function smoothableCharacterCount(payload: LantaiStreamPayload): number {
  const field = smoothableTextField(payload);
  return field ? Array.from(String(payload[field])).length : 0;
}

function isTerminalPayload(payload: LantaiStreamPayload): boolean {
  if (payload.object !== "response") return false;
  const status = String(payload.status || "").toLowerCase();
  return status === "completed" || status === "failed";
}

function splitSmoothablePayload(
  payload: LantaiStreamPayload,
  characterCount: number,
): {
  emitted: LantaiStreamPayload;
  remaining: LantaiStreamPayload | null;
  emittedCharacterCount: number;
} {
  const field = smoothableTextField(payload);
  if (!field) {
    return {
      emitted: payload,
      remaining: null,
      emittedCharacterCount: 0,
    };
  }
  const characters = Array.from(String(payload[field]));
  const take = Math.max(1, Math.min(characterCount, characters.length));
  const emittedText = characters.slice(0, take).join("");
  const remainingText = characters.slice(take).join("");
  return {
    emitted: { ...payload, [field]: emittedText },
    remaining: remainingText ? { ...payload, [field]: remainingText } : null,
    emittedCharacterCount: take,
  };
}

/**
 * Buffers raw provider chunks and commits them at the browser's paint cadence.
 *
 * Text/reasoning deltas are released over a small adaptive number of frames;
 * control and terminal events retain their original order and can never
 * overtake buffered text.
 */
export function createStreamPresentationScheduler(
  commit: (payloads: LantaiStreamPayload[]) => void,
  options: StreamPresentationSchedulerOptions = {},
): StreamPresentationScheduler {
  const requestFrame = options.requestFrame || defaultRequestFrame;
  const cancelFrame = options.cancelFrame || defaultCancelFrame;
  const shouldDeferPresentation =
    options.shouldDeferPresentation || (() => false);
  const minimumVisibleCharactersPerFrame = Math.max(
    1,
    options.minimumVisibleCharactersPerFrame ??
      DEFAULT_MINIMUM_VISIBLE_CHARACTERS_PER_FRAME,
  );
  const maximumVisibleCharactersPerFrame = Math.max(
    minimumVisibleCharactersPerFrame,
    options.maximumVisibleCharactersPerFrame ??
      DEFAULT_MAXIMUM_VISIBLE_CHARACTERS_PER_FRAME,
  );
  const targetBacklogFrames = Math.max(
    1,
    options.targetBacklogFrames ?? DEFAULT_TARGET_BACKLOG_FRAMES,
  );
  const maximumFrameWaitMs = Math.max(
    0,
    options.maximumFrameWaitMs ??
      (options.requestFrame ? 0 : DEFAULT_MAXIMUM_FRAME_WAIT_MS),
  );

  let queue: LantaiStreamPayload[] = [];
  let pendingVisibleCharacters = 0;
  let drainVisibleBudget = minimumVisibleCharactersPerFrame;
  let scheduledFrame: FrameHandle | null = null;
  let scheduledFrameWatchdog: ReturnType<typeof setTimeout> | null = null;
  let scheduledFrameGeneration = 0;
  let disposed = false;
  let idleResolvers: Array<() => void> = [];

  const settleIdle = () => {
    if (queue.length > 0 || scheduledFrame !== null) return;
    const resolvers = idleResolvers;
    idleResolvers = [];
    resolvers.forEach((resolve) => resolve());
  };

  const takePresentationBatch = (): LantaiStreamPayload[] => {
    const visibleBudget = Math.min(
      maximumVisibleCharactersPerFrame,
      Math.max(
        minimumVisibleCharactersPerFrame,
        drainVisibleBudget,
      ),
    );
    const batch: LantaiStreamPayload[] = [];
    let visibleCharacters = 0;

    while (queue.length > 0) {
      const payload = queue[0];
      const characterCount = smoothableCharacterCount(payload);
      if (characterCount === 0) {
        batch.push(payload);
        queue.shift();
        continue;
      }
      if (visibleCharacters >= visibleBudget) break;

      const split = splitSmoothablePayload(
        payload,
        visibleBudget - visibleCharacters,
      );
      batch.push(split.emitted);
      visibleCharacters += split.emittedCharacterCount;
      pendingVisibleCharacters = Math.max(
        0,
        pendingVisibleCharacters - split.emittedCharacterCount,
      );
      if (split.remaining) {
        queue[0] = split.remaining;
      } else {
        queue.shift();
      }
    }
    if (queue.length === 0) {
      drainVisibleBudget = minimumVisibleCharactersPerFrame;
    }
    return batch;
  };

  const schedule = () => {
    if (disposed || scheduledFrame !== null || queue.length === 0) return;
    const generation = ++scheduledFrameGeneration;
    const presentFrame = () => {
      if (generation !== scheduledFrameGeneration) return;
      const frame = scheduledFrame;
      scheduledFrame = null;
      if (scheduledFrameWatchdog !== null) {
        clearTimeout(scheduledFrameWatchdog);
        scheduledFrameWatchdog = null;
      }
      if (frame !== null) cancelFrame(frame);
      if (disposed) {
        settleIdle();
        return;
      }
      if (shouldDeferPresentation()) {
        schedule();
        return;
      }
      const batch = takePresentationBatch();
      if (batch.length > 0) commit(batch);
      if (queue.length > 0) schedule();
      settleIdle();
    };
    scheduledFrame = requestFrame(presentFrame);
    if (maximumFrameWaitMs > 0) {
      scheduledFrameWatchdog = setTimeout(presentFrame, maximumFrameWaitMs);
    }
  };

  const flushQueuedNow = () => {
    scheduledFrameGeneration += 1;
    if (scheduledFrame !== null) {
      cancelFrame(scheduledFrame);
      scheduledFrame = null;
    }
    if (scheduledFrameWatchdog !== null) {
      clearTimeout(scheduledFrameWatchdog);
      scheduledFrameWatchdog = null;
    }
    if (queue.length > 0) {
      const pending = queue;
      queue = [];
      pendingVisibleCharacters = 0;
      drainVisibleBudget = minimumVisibleCharactersPerFrame;
      commit(pending);
    }
    settleIdle();
  };

  return {
    enqueue(payload) {
      if (disposed) return;
      queue.push(payload);
      pendingVisibleCharacters += smoothableCharacterCount(payload);
      drainVisibleBudget = Math.max(
        drainVisibleBudget,
        Math.ceil(pendingVisibleCharacters / targetBacklogFrames),
      );
      if (isTerminalPayload(payload)) {
        flushQueuedNow();
        return;
      }
      schedule();
    },
    flushNow: flushQueuedNow,
    whenIdle() {
      if (queue.length === 0 && scheduledFrame === null) {
        return Promise.resolve();
      }
      return new Promise<void>((resolve) => {
        idleResolvers.push(resolve);
      });
    },
    dispose() {
      disposed = true;
      scheduledFrameGeneration += 1;
      if (scheduledFrame !== null) {
        cancelFrame(scheduledFrame);
        scheduledFrame = null;
      }
      if (scheduledFrameWatchdog !== null) {
        clearTimeout(scheduledFrameWatchdog);
        scheduledFrameWatchdog = null;
      }
      queue = [];
      pendingVisibleCharacters = 0;
      settleIdle();
    },
  };
}
