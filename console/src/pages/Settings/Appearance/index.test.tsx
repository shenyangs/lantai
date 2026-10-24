import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/common_setup";
import { defaultDisplaySettings } from "../../../theme/appearance";
import AppearanceSettingsPage from "./index";

const themeMock = vi.hoisted(() => ({
  setThemeMode: vi.fn(),
  setDisplaySetting: vi.fn(),
  setDisplaySettings: vi.fn(),
}));

vi.mock("../../../contexts/ThemeContext", () => ({
  useTheme: () => ({
    themeMode: "system",
    appearancePalette: "mist",
    customThemeCss: "",
    isDark: false,
    displaySettings: {
      showInnerVoice: true,
      personaMode: "full",
      showThinking: true,
      showToolCalls: true,
      showTokenUsage: true,
      dynamicWelcome: true,
      aiWelcome: true,
      welcomeCulturalCalendar: "none",
      welcomeTone: "natural",
      composerSize: "comfortable",
      sidebarMaterial: "mist-glass",
    },
    setDisplaySetting: themeMock.setDisplaySetting,
    setDisplaySettings: themeMock.setDisplaySettings,
    setThemeMode: themeMock.setThemeMode,
    setAppearancePalette: vi.fn(),
    setCustomThemeCss: vi.fn(),
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AppearanceSettingsPage", () => {
  it("keeps routine display and personalization in one entry", () => {
    renderWithProviders(<AppearanceSettingsPage />, {
      initialEntries: ["/appearance"],
    });

    expect(screen.getByText("左侧栏")).toBeInTheDocument();
    expect(screen.getByText("简报")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "隐藏简报入口" })).toBeChecked();
    expect(screen.getByText("对话显示")).toBeInTheDocument();
    expect(screen.getByText("蛐蛐")).toBeInTheDocument();
    expect(screen.getByText("沈行知的表达风格")).toBeInTheDocument();
    expect(screen.getByText("显示工作过程")).toBeInTheDocument();
    expect(screen.getByText("显示模型用量")).toBeInTheDocument();
    expect(screen.getByText("动态开场")).toBeInTheDocument();
    expect(screen.getByText("AI 个性化开场")).toBeInTheDocument();
    expect(screen.getByText("节日问候")).toBeInTheDocument();
    expect(screen.getByText("开场语气")).toBeInTheDocument();
    expect(screen.queryByText("沈行知人格存在感")).not.toBeInTheDocument();
    expect(screen.queryByText(/1\/3 回合/)).not.toBeInTheDocument();
    expect(screen.queryByText(/正式产物之外/)).not.toBeInTheDocument();
    expect(screen.getByText("主题色")).toBeInTheDocument();
    expect(screen.getByText("明暗模式")).toBeInTheDocument();
    expect(screen.getByText("应用图标")).toBeInTheDocument();
    expect(screen.getByText("高级个性化")).toBeInTheDocument();
    expect(screen.queryByText("高级自定义")).not.toBeInTheDocument();
  });

  it("keeps reasoning and tool progress visible for new or reset profiles", () => {
    expect(defaultDisplaySettings.showThinking).toBe(true);
    expect(defaultDisplaySettings.showToolCalls).toBe(true);
  });

  it("updates the composer size from the appearance page", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppearanceSettingsPage />, {
      initialEntries: ["/appearance"],
    });

    await user.click(screen.getByText("沉浸"));

    expect(themeMock.setDisplaySetting).toHaveBeenCalledWith(
      "composerSize",
      "spacious",
    );
  });

  it("persists explicit welcome preferences without inferring a cultural pack", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppearanceSettingsPage />, {
      initialEntries: ["/appearance"],
    });

    expect(screen.getByRole("switch", { name: "动态开场" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "AI 个性化开场" })).toBeChecked();
    expect(screen.getByText("不使用").closest("label")).toHaveClass(
      "ant-segmented-item-selected",
    );

    await user.click(screen.getByText("中国节日"));
    await user.click(screen.getByText("短一点"));

    expect(themeMock.setDisplaySetting).toHaveBeenCalledWith(
      "welcomeCulturalCalendar",
      "china",
    );
    expect(themeMock.setDisplaySetting).toHaveBeenCalledWith(
      "welcomeTone",
      "concise",
    );
  });

  it("resets only display controls and preserves the experimental material", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppearanceSettingsPage />, {
      initialEntries: ["/appearance"],
    });

    await user.click(screen.getByRole("button", { name: "恢复默认显示" }));

    expect(themeMock.setDisplaySettings).toHaveBeenCalledWith(
      expect.objectContaining({
        showInnerVoice: true,
        personaMode: "full",
        showThinking: true,
        showToolCalls: true,
        showTokenUsage: true,
        composerSize: "comfortable",
        sidebarMaterial: "mist-glass",
      }),
    );
  });
});
