import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CreateBackupRequest } from "@/api/types/backup";
import { useBackupRunner } from "./useBackupRunner";

const { createBackupStream, message } = vi.hoisted(() => ({
  createBackupStream: vi.fn(),
  message: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

vi.mock("@/api", () => ({
  default: { createBackupStream },
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const request: CreateBackupRequest = {
  name: "before-restore",
  scope: "full",
  agents: [],
};

describe("useBackupRunner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("closes only the progress observer and keeps the same backup running", async () => {
    let resolveStream!: () => void;
    let signal!: AbortSignal;
    createBackupStream.mockImplementation(
      (_data, _onProgress, nextSignal: AbortSignal) => {
        signal = nextSignal;
        return new Promise<void>((resolve) => {
          resolveStream = resolve;
        });
      },
    );
    const onClose = vi.fn();
    const onSuccess = vi.fn();
    const { result } = renderHook(() =>
      useBackupRunner({ onClose, onSuccess }),
    );

    let startPromise!: Promise<void>;
    act(() => {
      startPromise = result.current.start(request);
    });
    await waitFor(() => expect(result.current.phase).toBe("running"));

    act(() => result.current.closeObservation());

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(signal.aborted).toBe(false);
    expect(result.current.phase).toBe("running");
    expect(createBackupStream).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveStream();
      await startPromise;
    });
    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("idle");
  });

  it("marks an unconfirmed stop without closing or starting a second backup", async () => {
    createBackupStream.mockImplementation(
      (_data, _onProgress, signal: AbortSignal) =>
        new Promise<void>((_resolve, reject) => {
          signal.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );
    const onClose = vi.fn();
    const { result } = renderHook(() => useBackupRunner({ onClose }));

    let startPromise!: Promise<void>;
    act(() => {
      startPromise = result.current.start(request);
    });
    await waitFor(() => expect(result.current.phase).toBe("running"));

    act(() => result.current.requestStop());
    await act(async () => {
      await startPromise;
    });

    expect(result.current.phase).toBe("stop_requested");
    expect(result.current.progressMsg).toContain("待核实");
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.start(request);
    });
    expect(createBackupStream).toHaveBeenCalledTimes(1);
  });
});
