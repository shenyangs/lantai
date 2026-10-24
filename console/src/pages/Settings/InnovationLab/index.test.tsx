import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/common_setup";
import { appearancePalettes } from "../../../theme/appearance";
import InnovationLabSettingsPage from "./index";

const themeMock = vi.hoisted(() => ({
  setThemeMode: vi.fn(),
  setAppearancePalette: vi.fn(),
  setCustomThemeCss: vi.fn(),
  setDisplaySetting: vi.fn(),
  scrollIntoView: vi.fn(),
}));

const privateEditorMock = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
}));

vi.mock("../../../api/modules/privateEditor", () => ({
  privateEditorApi: privateEditorMock,
}));

vi.mock("../../../contexts/ThemeContext", () => ({
  useTheme: () => ({
    themeMode: "system",
    appearancePalette: "mist",
    customThemeCss: "",
    displaySettings: {
      sidebarMaterial: "solid",
    },
    isDark: false,
    setThemeMode: themeMock.setThemeMode,
    setAppearancePalette: themeMock.setAppearancePalette,
    setCustomThemeCss: themeMock.setCustomThemeCss,
    setDisplaySetting: themeMock.setDisplaySetting,
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  privateEditorMock.getSettings.mockResolvedValue({
    enabled: false,
    research_consent: false,
    revocation_epoch: 0,
    updated_at: "2026-09-19T00:00:00Z",
  });
  privateEditorMock.updateSettings.mockImplementation(async (payload) => ({
    enabled: payload.enabled ?? false,
    research_consent: payload.research_consent ?? false,
    revocation_epoch: 0,
    updated_at: "2026-09-19T00:00:01Z",
  }));
  delete window.electronAPI;
  Object.defineProperty(window, "requestAnimationFrame", {
    configurable: true,
    value: (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(0), 0),
  });
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: themeMock.scrollIntoView,
  });
});

describe("InnovationLabSettingsPage", () => {
  it("collects themes, materials, app icons, and CSS customization", async () => {
    const user = userEvent.setup();
    renderWithProviders(<InnovationLabSettingsPage />, {
      initialEntries: ["/innovation-lab"],
    });

    expect(screen.getByText("外观与个性化")).toBeVisible();
    expect(screen.getByText("主题色")).toBeVisible();
    expect(screen.getByText("明暗模式")).toBeVisible();
    expect(screen.getByText("界面材质")).toBeVisible();
    expect(screen.getByText("应用图标")).toBeVisible();
    expect(screen.queryByText("高级自定义")).not.toBeInTheDocument();
    await user.click(screen.getByText("高级个性化"));
    expect(await screen.findByText("高级自定义")).toBeVisible();
    expect(
      screen.queryByTestId("innovation-palette-preview"),
    ).not.toBeInTheDocument();
    expect(appearancePalettes.slice(0, 3).map((palette) => palette.id)).toEqual(
      ["mist", "pine", "lantai"],
    );
  });

  it("keeps named color choices and keyboard selection accessible", async () => {
    const user = userEvent.setup();
    renderWithProviders(<InnovationLabSettingsPage />, {
      initialEntries: ["/innovation-lab"],
    });
    expect(screen.getByRole("button", { name: "石墨" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const pine = screen.getByRole("button", { name: "澜绿" });
    pine.focus();
    await user.keyboard("{Enter}");
    expect(themeMock.setAppearancePalette).toHaveBeenCalledWith("pine");
    expect(themeMock.setThemeMode).not.toHaveBeenCalled();
  });

  it("changes display mode independently from the selected accent", async () => {
    const user = userEvent.setup();
    renderWithProviders(<InnovationLabSettingsPage />, {
      initialEntries: ["/innovation-lab"],
    });

    await user.click(screen.getByText("深色"));

    expect(themeMock.setThemeMode).toHaveBeenCalledWith("dark");
    expect(themeMock.setAppearancePalette).not.toHaveBeenCalled();
  });

  it("opens the CSS editor when the custom palette is selected", async () => {
    const user = userEvent.setup();
    renderWithProviders(<InnovationLabSettingsPage />, {
      initialEntries: ["/innovation-lab"],
    });

    await user.click(screen.getByRole("button", { name: "自定义…" }));

    expect(themeMock.setAppearancePalette).toHaveBeenCalledWith("custom");
    await waitFor(() => expect(themeMock.scrollIntoView).toHaveBeenCalled());
    expect(screen.getByLabelText("自定义主题设计令牌")).toBeVisible();
  });

  it("offers two application materials without changing light or dark mode", async () => {
    const user = userEvent.setup();
    renderWithProviders(<InnovationLabSettingsPage />, {
      initialEntries: ["/innovation-lab"],
    });

    expect(screen.queryByText("侧栏材质")).not.toBeInTheDocument();
    for (const label of ["轻玻璃", "雾面", "深玻璃"]) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
    await user.click(screen.getByText("悬浮玻璃"));

    expect(themeMock.setDisplaySetting).toHaveBeenCalledWith(
      "sidebarMaterial",
      "mist-glass",
    );
    expect(themeMock.setThemeMode).not.toHaveBeenCalled();
  });

  it("keeps private editor and research consent off until each is explicitly enabled", async () => {
    const user = userEvent.setup();
    renderWithProviders(<InnovationLabSettingsPage />, {
      initialEntries: ["/innovation-lab"],
    });

    const experimentSwitch = await screen.findByRole("switch", {
      name: "启用私人编辑实验",
    });
    const researchSwitch = screen.getByRole("switch", {
      name: "同意导出匿名研究诊断",
    });
    expect(experimentSwitch).not.toBeChecked();
    expect(researchSwitch).not.toBeChecked();
    expect(researchSwitch).toBeDisabled();

    await user.click(experimentSwitch);
    await waitFor(() =>
      expect(privateEditorMock.updateSettings).toHaveBeenCalledWith({
        enabled: true,
      }),
    );
    expect(
      screen.getByText(/仍会按当前 Provider 设置发送给模型/),
    ).toBeVisible();
    expect(researchSwitch).not.toBeDisabled();
    expect(researchSwitch).not.toBeChecked();
  });
});
