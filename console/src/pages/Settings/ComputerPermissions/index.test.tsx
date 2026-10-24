import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/common_setup";
import ComputerPermissionsPage from "./index";

const permissionsApiMock = vi.hoisted(() => ({
  getComputerPermissions: vi.fn(),
  requestComputerPermission: vi.fn(),
  openComputerPermissionSettings: vi.fn(),
}));

vi.mock("../../../api/modules/computerPermissions", () => ({
  computerPermissionsApi: permissionsApiMock,
}));

const backendStatus = {
  platform: "Darwin",
  is_macos: true,
  app_path: null,
  permissions: [
    "accessibility",
    "full_disk_access",
    "screen_recording",
    "automation",
  ].map((key) => ({
    key,
    status: "unknown",
    can_check: false,
    can_request: true,
  })),
};

function desktopStatus(accessibility: "granted" | "denied") {
  return {
    ok: true,
    platform: "darwin",
    app_path: null,
    permissions: [
      { key: "accessibility", status: accessibility, can_check: true },
      { key: "screen_recording", status: "denied", can_check: true },
    ],
  };
}

function permissionCard(title: string) {
  const card = screen.getByText(title).closest(".ant-card");
  expect(card).not.toBeNull();
  return within(card as HTMLElement);
}

beforeEach(() => {
  vi.clearAllMocks();
  permissionsApiMock.getComputerPermissions.mockResolvedValue(backendStatus);
  permissionsApiMock.requestComputerPermission.mockResolvedValue({ ok: true });
  permissionsApiMock.openComputerPermissionSettings.mockResolvedValue({
    ok: true,
  });
  delete window.electronAPI;
});

describe("ComputerPermissionsPage", () => {
  it("does not expose the removed microphone permission", async () => {
    renderWithProviders(<ComputerPermissionsPage />, {
      initialEntries: ["/computer-permissions"],
    });

    await screen.findByText("辅助功能");
    expect(screen.queryByText("麦克风")).not.toBeInTheDocument();
    expect(screen.queryByText(/语音转写/)).not.toBeInTheDocument();
  });

  it("shows the signed Mac Use helper's own permission status", async () => {
    window.electronAPI = {
      isDesktop: true,
      platform: "darwin",
      apiBaseUrl: "",
      getComputerPermissionStatus: vi
        .fn()
        .mockResolvedValue(desktopStatus("granted")),
      getMacUseStatus: vi.fn().mockResolvedValue({
        ok: true,
        effect_status: "not_applicable",
        dispatch_status: "not_sent",
        data: {
          accessibility: "granted",
          screen_recording: "unknown",
        },
      }),
      requestMacUsePermission: vi.fn().mockResolvedValue({ ok: true }),
    };

    const user = userEvent.setup();
    renderWithProviders(<ComputerPermissionsPage />, {
      initialEntries: ["/computer-permissions"],
    });

    expect(
      await screen.findByText("Mac 应用操作桥已就绪（实验）"),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "请求 Mac 操作权限" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "请求窗口读取权限" }));
    expect(window.electronAPI.requestMacUsePermission).toHaveBeenCalledWith(
      "screen_recording",
    );
  });

  it("does not offer to request a permission that is already granted", async () => {
    window.electronAPI = {
      isDesktop: true,
      platform: "darwin",
      apiBaseUrl: "",
      getComputerPermissionStatus: vi
        .fn()
        .mockResolvedValue(desktopStatus("granted")),
    };

    renderWithProviders(<ComputerPermissionsPage />, {
      initialEntries: ["/computer-permissions"],
    });

    const card = permissionCard("辅助功能");
    const grantedButton = await card.findByRole("button", { name: /已允许/ });
    expect(grantedButton).toBeDisabled();
    expect(
      card.queryByRole("button", { name: "打开辅助功能设置" }),
    ).not.toBeInTheDocument();
  });

  it("refreshes native permission state when the app regains focus", async () => {
    const getComputerPermissionStatus = vi
      .fn()
      .mockResolvedValueOnce(desktopStatus("denied"))
      .mockResolvedValue(desktopStatus("granted"));
    window.electronAPI = {
      isDesktop: true,
      platform: "darwin",
      apiBaseUrl: "",
      getComputerPermissionStatus,
    };

    renderWithProviders(<ComputerPermissionsPage />, {
      initialEntries: ["/computer-permissions"],
    });

    expect(
      await permissionCard("辅助功能").findByRole("button", {
        name: /打开辅助功能设置/,
      }),
    ).toBeEnabled();

    act(() => window.dispatchEvent(new Event("focus")));

    await waitFor(() =>
      expect(getComputerPermissionStatus).toHaveBeenCalledTimes(2),
    );
    expect(
      permissionCard("辅助功能").getByRole("button", { name: /已允许/ }),
    ).toBeDisabled();
  });

  it("does not show or copy a machine-specific fallback app path", async () => {
    window.electronAPI = {
      isDesktop: true,
      platform: "darwin",
      apiBaseUrl: "",
      getComputerPermissionStatus: vi
        .fn()
        .mockResolvedValue(desktopStatus("denied")),
    };

    renderWithProviders(<ComputerPermissionsPage />, {
      initialEntries: ["/computer-permissions"],
    });

    expect(
      await screen.findByText("未检测到当前应用包路径"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("/Users/example/Downloads/澜台.app"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /复制/ })).toBeDisabled();
  });
});
