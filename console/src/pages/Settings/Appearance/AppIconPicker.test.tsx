import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/common_setup";
import AppIconPicker from "./AppIconPicker";

const iconState: DesktopAppIconState = {
  ok: true,
  supported: true,
  selectedId: "default",
  defaultIcon: {
    id: "default",
    name: "黑白澜台",
    group: "default",
    previewUrl: "/lantai-mark.png",
  },
  builtinIcons: [
    {
      id: "brush-classic",
      name: "经典澜台",
      group: "brush",
      previewUrl: "/app-icons/brush-classic.png",
    },
    {
      id: "brush-cobalt-glass",
      name: "冰蓝墨",
      group: "brush",
      previewUrl: "/app-icons/brush-cobalt-glass.png",
    },
    {
      id: "ink-horizon",
      name: "墨潮地平线",
      group: "signature",
      previewUrl: "/app-icons/ink-horizon.png",
    },
  ],
  customIcon: {
    id: "custom",
    name: "我的图标",
    group: "custom",
    previewUrl: "",
    available: false,
  },
  designSpec: "1024 × 1024 PNG",
};

afterEach(() => {
  delete window.electronAPI;
  vi.restoreAllMocks();
});

describe("AppIconPicker", () => {
  it("loads the built-in catalog and applies a selected icon", async () => {
    const selectAppIcon = vi.fn().mockResolvedValue({
      ...iconState,
      selectedId: "brush-cobalt-glass",
    });
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      getAppIcon: vi.fn().mockResolvedValue(iconState),
      selectAppIcon,
    };
    const user = userEvent.setup();

    renderWithProviders(<AppIconPicker />);
    const option = await screen.findByRole("button", {
      name: "选择冰蓝墨应用图标",
    });
    await user.click(option);

    expect(selectAppIcon).toHaveBeenCalledWith("brush-cobalt-glass");
    await waitFor(() => expect(option).toHaveAttribute("aria-pressed", "true"));
  });

  it("keeps the appearance page usable outside the desktop shell", () => {
    renderWithProviders(<AppIconPicker />);

    expect(
      screen.getByText("应用图标只能在澜台 macOS 桌面端中更换。"),
    ).toBeInTheDocument();
  });

  it("switches back to the classic icon from a built-in variant", async () => {
    const selectAppIcon = vi.fn().mockResolvedValue({
      ...iconState,
      selectedId: "brush-classic",
    });
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      getAppIcon: vi.fn().mockResolvedValue({
        ...iconState,
        selectedId: "brush-cobalt-glass",
      }),
      selectAppIcon,
    };
    const user = userEvent.setup();

    renderWithProviders(<AppIconPicker />);
    await user.click(
      await screen.findByRole("button", {
        name: "选择经典澜台应用图标",
      }),
    );

    expect(selectAppIcon).toHaveBeenCalledWith("brush-classic");
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "选择经典澜台应用图标",
        }),
      ).toHaveAttribute("aria-pressed", "true"),
    );
  });

  it("shows the new default separately from the curated classic and can return to it", async () => {
    const selectAppIcon = vi.fn().mockResolvedValue(iconState);
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      getAppIcon: vi
        .fn()
        .mockResolvedValue({ ...iconState, selectedId: "brush-classic" }),
      selectAppIcon,
    };
    const user = userEvent.setup();

    renderWithProviders(<AppIconPicker />);
    const defaultOption = await screen.findByRole("button", {
      name: "选择黑白澜台应用图标",
    });
    expect(screen.getByText("默认")).toBeInTheDocument();
    const brushGroup =
      screen.getByText("澜字精选").parentElement!.parentElement!;
    expect(
      within(brushGroup).getByRole("button", {
        name: "选择经典澜台应用图标",
      }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      within(brushGroup).queryByRole("button", {
        name: "选择黑白澜台应用图标",
      }),
    ).not.toBeInTheDocument();

    await user.click(defaultOption);
    expect(selectAppIcon).toHaveBeenCalledWith("default");
    await waitFor(() =>
      expect(defaultOption).toHaveAttribute("aria-pressed", "true"),
    );
  });
});
