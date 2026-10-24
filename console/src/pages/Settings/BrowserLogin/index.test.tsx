import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";

const { appMessage, getBrowserSkillStatus, getConfig, updateConfig } =
  vi.hoisted(() => ({
    appMessage: {
      error: vi.fn(),
      success: vi.fn(),
      warning: vi.fn(),
    },
    getBrowserSkillStatus: vi.fn(),
    getConfig: vi.fn(),
    updateConfig: vi.fn(),
  }));

vi.mock("../../../api/modules/browserSession", () => ({
  browserWorkbenchApi: {
    getBrowserSkillStatus,
    getConfig,
    updateConfig,
  },
}));

vi.mock("../../../api/modules/browserPairing", () => ({
  browserPairingApi: { createBrowserPairingCode: vi.fn() },
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: appMessage }),
}));

import BrowserLoginPage from "./index";

const electronConfig = {
  agent_control_enabled: true,
  default_profile_id: "default",
  require_confirmation_for_write: true,
  enable_scheduled_browser_tasks: false,
  primary_runtime: "electron_bridge" as const,
  optional_engines_enabled: true,
};

describe("BrowserLoginPage BrowserSkill companion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getBrowserSkillStatus.mockResolvedValue({
      ok: true,
      installed: true,
      connected: true,
      ready: true,
      status: "available",
      runtime: "browser_skill",
      message: "BrowserSkill 已连接",
      browsers: [{ instance_id: "chrome-main" }],
      sessions: [],
      repository_url: "https://github.com/Tencent/BrowserSkill",
      upstream_commit: "47ac947f",
    });
    getConfig.mockResolvedValue(electronConfig);
    updateConfig.mockResolvedValue({
      ...electronConfig,
      primary_runtime: "browser_skill",
    });
  });

  it("仅在设置页管理伴随连接，任务仍从主聊天发起", async () => {
    renderWithProviders(<BrowserLoginPage />, {
      initialEntries: ["/settings/browser-login"],
    });

    expect(
      screen.getByText(
        "分别管理“在浏览器里使用澜台”和“让澜台操作已登录浏览器”。",
      ),
    ).toBeVisible();
    expect(await screen.findByText("已登录浏览器伴随")).toBeVisible();
    expect(screen.getByText("已连接")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /设为首选/ }));

    await waitFor(() =>
      expect(updateConfig).toHaveBeenCalledWith({
        primary_runtime: "browser_skill",
      }),
    );
    expect(
      await screen.findByRole("button", { name: /恢复内建浏览器/ }),
    ).toBeVisible();
  });
});
