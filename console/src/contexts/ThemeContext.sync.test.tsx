import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useTheme } from "./ThemeContext";
import { settingsApi } from "../api/modules/settings";
import {
  customizationApi,
  CUSTOMIZATION_CHANGED_EVENT,
} from "../api/modules/customization";

vi.mock("../api/modules/settings", () => ({
  settingsApi: {
    getAppearanceSettings: vi.fn(),
    updateAppearanceSettings: vi.fn(),
  },
}));
vi.mock("../api/modules/customization", () => ({
  CUSTOMIZATION_CHANGED_EVENT: "lantai:customization-changed",
  notifyCustomizationChanged: () =>
    window.dispatchEvent(new Event("lantai:customization-changed")),
  customizationApi: Object.fromEntries(
    [
      "getEffectiveState",
      "getState",
      "listProfiles",
      "listSlots",
      "previewPlan",
      "applyPlan",
      "activateProfile",
    ].map((name) => [name, vi.fn()]),
  ),
}));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
const effective = (mode: string) => ({
  values: { "appearance.theme_mode": mode },
  sources: {},
  ignored: {},
  warnings: [],
});
beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("lantai-theme", "light");
  vi.mocked(settingsApi.getAppearanceSettings).mockImplementation(
    () => new Promise(() => {}),
  );
  vi.mocked(settingsApi.updateAppearanceSettings).mockResolvedValue(
    {} as never,
  );
  vi.mocked(customizationApi.getEffectiveState).mockImplementation(
    () => new Promise(() => {}),
  );
  vi.mocked(customizationApi.getState).mockResolvedValue({
    active_profile_id: "test",
  } as never);
  vi.mocked(customizationApi.listProfiles).mockResolvedValue([
    { id: "test", revision: 1 },
  ] as never);
  vi.mocked(customizationApi.listSlots).mockResolvedValue([]);
  vi.mocked(customizationApi.previewPlan).mockResolvedValue({
    plan: { id: "plan" },
    requires_confirmation: false,
  } as never);
  vi.mocked(customizationApi.applyPlan).mockResolvedValue({
    status: "committed",
  });
  vi.mocked(customizationApi.activateProfile).mockResolvedValue({} as never);
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("theme request ordering", () => {
  it("keeps the effective profile theme over a slower legacy settings read", async () => {
    const read =
      deferred<Awaited<ReturnType<typeof settingsApi.getAppearanceSettings>>>();
    vi.mocked(settingsApi.getAppearanceSettings).mockReturnValue(read.promise);
    vi.mocked(customizationApi.getEffectiveState).mockResolvedValue(
      effective("dark"),
    );
    const { result } = renderHook(useTheme, { wrapper: ThemeProvider });
    await waitFor(() => expect(result.current.isDark).toBe(true));
    await act(async () => read.resolve({ theme_mode: "light" } as never));
    expect(result.current.isDark).toBe(true);
    expect(localStorage.getItem("lantai-theme")).toBe("dark");
  });
  it("keeps a user's choice when the initial appearance read arrives late", async () => {
    const read =
      deferred<Awaited<ReturnType<typeof settingsApi.getAppearanceSettings>>>();
    vi.mocked(settingsApi.getAppearanceSettings).mockReturnValue(read.promise);
    const { result } = renderHook(useTheme, { wrapper: ThemeProvider });
    act(() => result.current.setThemeMode("dark"));
    await act(async () => read.resolve({ theme_mode: "light" } as never));
    expect(result.current.themeMode).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("lantai-theme")).toBe("dark");
  });
  it("discards effective reads completed out of order", async () => {
    const old = deferred<ReturnType<typeof effective>>();
    vi.mocked(customizationApi.getEffectiveState)
      .mockReturnValueOnce(old.promise)
      .mockResolvedValue(effective("dark"));
    const { result } = renderHook(useTheme, { wrapper: ThemeProvider });
    await act(async () =>
      window.dispatchEvent(new Event(CUSTOMIZATION_CHANGED_EVENT)),
    );
    expect(result.current.isDark).toBe(true);
    await act(async () => old.resolve(effective("light")));
    expect(result.current.isDark).toBe(true);
  });
  it("does not apply stale server theme while the new selection is saving", async () => {
    const save =
      deferred<
        Awaited<ReturnType<typeof settingsApi.updateAppearanceSettings>>
      >();
    vi.mocked(settingsApi.updateAppearanceSettings).mockReturnValue(
      save.promise,
    );
    vi.mocked(customizationApi.getEffectiveState).mockResolvedValue(
      effective("light"),
    );
    const { result } = renderHook(useTheme, { wrapper: ThemeProvider });
    await act(async () => {});
    act(() => result.current.setThemeMode("dark"));
    await act(async () =>
      window.dispatchEvent(new Event(CUSTOMIZATION_CHANGED_EVENT)),
    );
    expect(result.current.isDark).toBe(true);
  });
  it("serializes quick selections so both persistence paths finish on the last choice", async () => {
    const save =
      deferred<
        Awaited<ReturnType<typeof settingsApi.updateAppearanceSettings>>
      >();
    vi.mocked(settingsApi.updateAppearanceSettings)
      .mockReturnValueOnce(save.promise)
      .mockResolvedValue({} as never);
    const { result } = renderHook(useTheme, { wrapper: ThemeProvider });
    act(() => {
      result.current.setThemeMode("dark");
      result.current.setThemeMode("light");
    });
    await act(async () => {});
    expect(settingsApi.updateAppearanceSettings).toHaveBeenCalledTimes(1);
    await act(async () => save.resolve({} as never));
    await waitFor(() =>
      expect(customizationApi.applyPlan).toHaveBeenCalledTimes(2),
    );
    expect(settingsApi.updateAppearanceSettings).toHaveBeenLastCalledWith({
      theme_mode: "light",
    });
    expect(customizationApi.previewPlan).toHaveBeenLastCalledWith(
      expect.objectContaining({
        operations: [
          { op: "set", path: "appearance.theme_mode", value: "light" },
        ],
      }),
    );
    expect(result.current.isDark).toBe(false);
  });
  it("allows a later external profile change after saving completes", async () => {
    const { result } = renderHook(useTheme, { wrapper: ThemeProvider });
    act(() => result.current.setThemeMode("dark"));
    await waitFor(() =>
      expect(customizationApi.activateProfile).toHaveBeenCalled(),
    );
    vi.mocked(customizationApi.getEffectiveState).mockResolvedValue(
      effective("light"),
    );
    await act(async () =>
      window.dispatchEvent(new Event(CUSTOMIZATION_CHANGED_EVENT)),
    );
    expect(result.current.isDark).toBe(false);
  });
  it("keeps the local choice offline and allows the next save to recover", async () => {
    vi.mocked(settingsApi.updateAppearanceSettings).mockRejectedValueOnce(
      new Error("offline"),
    );
    vi.mocked(customizationApi.getState).mockRejectedValueOnce(
      new Error("offline"),
    );
    const { result } = renderHook(useTheme, { wrapper: ThemeProvider });
    await act(async () => result.current.setThemeMode("dark"));
    expect(result.current.isDark).toBe(true);
    expect(localStorage.getItem("lantai-theme")).toBe("dark");
    await act(async () => result.current.setThemeMode("light"));
    expect(customizationApi.activateProfile).toHaveBeenCalledTimes(1);
    expect(result.current.isDark).toBe(false);
  });
  it("continues following OS changes in system mode but ignores them in a fixed mode", async () => {
    const media = Object.assign(new EventTarget(), {
      matches: true,
    }) as MediaQueryList;
    vi.spyOn(window, "matchMedia").mockReturnValue(media);
    const { result } = renderHook(useTheme, { wrapper: ThemeProvider });
    await act(async () => result.current.setThemeMode("system"));
    expect(result.current.isDark).toBe(true);
    act(() =>
      media.dispatchEvent(
        Object.assign(new Event("change"), { matches: false }),
      ),
    );
    expect(result.current.isDark).toBe(false);
    await act(async () => result.current.setThemeMode("dark"));
    act(() =>
      media.dispatchEvent(
        Object.assign(new Event("change"), { matches: false }),
      ),
    );
    expect(result.current.isDark).toBe(true);
  });
});
