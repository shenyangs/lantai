import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWindowMaterial } from "./useWindowMaterial";
import { resolveInterfaceMaterial, type SidebarMaterial } from "./appearance";
const theme = vi.hoisted(() => ({
  displaySettings: { sidebarMaterial: "solid" as SidebarMaterial },
  isDark: false,
}));
vi.mock("../contexts/ThemeContext", () => ({ useTheme: () => theme }));
let reduced: MediaQueryList;
beforeEach(() => {
  theme.displaySettings.sidebarMaterial = "mist-glass";
  theme.isDark = false;
  reduced = Object.assign(new EventTarget(), {
    matches: false,
  }) as MediaQueryList;
  vi.spyOn(window, "matchMedia").mockImplementation(() => reduced);
});
afterEach(() => {
  vi.restoreAllMocks();
  delete window.electronAPI;
  delete document.documentElement.dataset.windowMaterial;
});
describe("application material", () => {
  it("maps all saved glass presets to the one glass choice", () => {
    for (const value of ["light-glass", "mist-glass", "deep-glass"] as const)
      expect(resolveInterfaceMaterial(value)).toBe("mist-glass");
    expect(resolveInterfaceMaterial("solid")).toBe("solid");
  });
  it("uses a readable browser fallback and responds to reduced transparency", () => {
    const { unmount } = renderHook(useWindowMaterial);
    expect(document.documentElement.dataset.windowMaterial).toBe("fallback");
    act(() => {
      Object.assign(reduced, { matches: true });
      reduced.dispatchEvent(new Event("change"));
    });
    expect(document.documentElement.dataset.windowMaterial).toBe("solid");
    unmount();
  });
  it("retains an active native material while its theme update is pending", async () => {
    const setWindowMaterial = vi
      .fn()
      .mockResolvedValueOnce("native")
      .mockImplementation(() => new Promise(() => {}));
    window.electronAPI = { setWindowMaterial } as unknown as LantaiElectronAPI;
    const { rerender } = renderHook(useWindowMaterial);
    await waitFor(() =>
      expect(document.documentElement.dataset.windowMaterial).toBe("native"),
    );
    theme.isDark = true;
    rerender();
    expect(document.documentElement.dataset.windowMaterial).toBe("native");
    expect(setWindowMaterial).toHaveBeenLastCalledWith({
      enabled: true,
      dark: true,
    });
  });
  it("ignores stale IPC responses after changing material and unsubscribes", async () => {
    let resolveFirst: (mode: WindowMaterialMode) => void = () => {};
    const unsubscribe = vi.fn();
    const setWindowMaterial = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<WindowMaterialMode>((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockResolvedValue("solid");
    window.electronAPI = {
      setWindowMaterial,
      onWindowMaterialChanged: () => unsubscribe,
    } as unknown as LantaiElectronAPI;
    const { rerender, unmount } = renderHook(useWindowMaterial);
    theme.displaySettings.sidebarMaterial = "solid";
    rerender();
    await waitFor(() =>
      expect(setWindowMaterial).toHaveBeenLastCalledWith({
        enabled: false,
        dark: false,
      }),
    );
    await act(async () => {
      resolveFirst("native");
    });
    expect(document.documentElement.dataset.windowMaterial).toBe("solid");
    unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(2);
  });
  it("restores the readable fallback if updating an active native material fails", async () => {
    const setWindowMaterial = vi
      .fn()
      .mockResolvedValueOnce("native")
      .mockRejectedValueOnce(new Error("window unavailable"));
    window.electronAPI = { setWindowMaterial } as unknown as LantaiElectronAPI;
    const { rerender } = renderHook(useWindowMaterial);
    await waitFor(() =>
      expect(document.documentElement.dataset.windowMaterial).toBe("native"),
    );
    theme.isDark = true;
    rerender();
    await waitFor(() =>
      expect(document.documentElement.dataset.windowMaterial).toBe("fallback"),
    );
  });
});
