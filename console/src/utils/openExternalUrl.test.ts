import { afterEach, describe, expect, it, vi } from "vitest";

import { openExternalUrl, prepareExternalNavigation } from "./openExternalUrl";

describe("openExternalUrl", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.electronAPI = undefined;
  });

  it("uses the trusted desktop bridge without creating an Electron popup", async () => {
    const openExternal = vi.fn().mockResolvedValue({ success: true });
    const windowOpen = vi.spyOn(window, "open").mockReturnValue(null);
    window.electronAPI = { openExternal };

    await openExternalUrl("https://www.kdocs.cn/latest");

    expect(openExternal).toHaveBeenCalledWith("https://www.kdocs.cn/latest");
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it("reserves a browser popup synchronously for a later OAuth URL", async () => {
    const popup = {
      opener: window,
      location: { href: "" },
      document: { title: "" },
      close: vi.fn(),
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    const navigation = prepareExternalNavigation();
    await navigation?.open("https://mcp.notion.com/authorize");

    expect(popup.opener).toBeNull();
    expect(popup.location.href).toBe("https://mcp.notion.com/authorize");
  });

  it("rejects non-web protocols before invoking the desktop bridge", async () => {
    const openExternal = vi.fn().mockResolvedValue({ success: true });
    window.electronAPI = { openExternal };

    await expect(openExternalUrl("javascript:alert(1)")).rejects.toThrow(
      "Only HTTP and HTTPS",
    );
    expect(openExternal).not.toHaveBeenCalled();
  });
});
