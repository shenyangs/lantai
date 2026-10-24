import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import { useLocation } from "react-router-dom";

const { status, startWatcha, pollWatcha, cancelWatcha, logout, open, close } =
  vi.hoisted(() => ({
    status: vi.fn(),
    startWatcha: vi.fn(),
    pollWatcha: vi.fn(),
    cancelWatcha: vi.fn(),
    logout: vi.fn(),
    open: vi.fn(),
    close: vi.fn(),
  }));

vi.mock("../../../api/modules/cloudAccount", () => ({
  cloudAccountApi: { status, startWatcha, pollWatcha, cancelWatcha, logout },
}));

vi.mock("../../../utils/openExternalUrl", () => ({
  prepareExternalNavigation: () => ({ open, close }),
}));

import AccountSettingsPage from "./index";

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location-probe">
      {location.pathname}
      {location.search}
    </div>
  );
}

describe("AccountSettingsPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps local Lantai usable when no cloud account is connected", async () => {
    status.mockResolvedValue({ connected: false });
    renderWithProviders(<AccountSettingsPage />, {
      initialEntries: ["/account"],
    });

    expect(await screen.findByText("获得你的澜台 UID")).toBeVisible();
    expect(screen.getByText(/不登录也能继续使用全部本地功能/)).toBeVisible();
    expect(screen.getByRole("button", { name: /使用观猹登录/ })).toBeVisible();
    expect(
      screen.getByText(/聊天、文件、记忆、工作区内容或 Provider 密钥/),
    ).toBeVisible();
  });

  it("shows the verified Watcha identity without exposing tokens", async () => {
    status.mockResolvedValue({
      connected: true,
      provider: "watcha",
      user: { id: "user-1", uid: 11, displayName: "观察员" },
      membership: { plan: "free", status: "active" },
    });
    renderWithProviders(<AccountSettingsPage />, {
      initialEntries: ["/account"],
    });

    expect(await screen.findByText("观察员")).toBeVisible();
    expect(screen.getByText("已连接")).toBeVisible();
    expect(screen.getByText("观猹")).toBeVisible();
    expect(screen.getByText("11")).toBeVisible();
    expect(screen.queryByText("当前方案")).toBeNull();
    expect(screen.queryByText("Free")).toBeNull();
    expect(screen.queryByText(/会员服务/)).toBeNull();
    expect(screen.queryByText(/token/i)).toBeNull();
  });

  it("makes the browser handoff visible and cancellable", async () => {
    status.mockResolvedValue({ connected: false });
    startWatcha.mockResolvedValue({
      attempt_id: "attempt-1",
      authorization_url: "https://watcha.cn/oauth/authorize",
      expires_at: "2030-01-01T00:00:00Z",
      poll_interval_seconds: 30,
    });
    cancelWatcha.mockResolvedValue({ cancelled: true });
    renderWithProviders(<AccountSettingsPage />, {
      initialEntries: ["/account"],
    });

    fireEvent.click(
      await screen.findByRole("button", { name: /使用观猹登录/ }),
    );
    expect(await screen.findByText("正在等待观猹确认")).toBeVisible();
    expect(open).toHaveBeenCalledWith("https://watcha.cn/oauth/authorize");

    fireEvent.click(screen.getByRole("button", { name: /取消等待/ }));
    await waitFor(() => expect(cancelWatcha).toHaveBeenCalledWith("attempt-1"));
    expect(screen.getByText("获得你的澜台 UID")).toBeVisible();
  });

  it("keeps a recoverable error next to the account action", async () => {
    status.mockRejectedValue(new Error("账号服务暂时不可用"));
    renderWithProviders(<AccountSettingsPage />, {
      initialEntries: ["/account"],
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "账号服务暂时不可用",
    );
    expect(screen.getByRole("button", { name: /使用观猹登录/ })).toBeVisible();
  });

  it("continues a guided setup to TokenDance when Watcha is already connected", async () => {
    status.mockResolvedValue({
      connected: true,
      provider: "watcha",
      user: { id: "user-1", uid: 11, displayName: "观察员" },
    });
    renderWithProviders(
      <>
        <AccountSettingsPage />
        <LocationProbe />
      </>,
      {
        initialEntries: [
          "/account?setup=watcha&returnTo=%2Fchat%2Fthread-setup",
        ],
      },
    );

    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/models?setup=watcha&returnTo=%2Fchat%2Fthread-setup",
      ),
    );
  });

  it("explains the separate Watcha and TokenDance consent steps", async () => {
    status.mockResolvedValue({ connected: false });
    renderWithProviders(<AccountSettingsPage />, {
      initialEntries: ["/account?setup=watcha&returnTo=%2Fchat"],
    });

    expect(await screen.findByText("先确认观猹身份")).toBeVisible();
    expect(screen.getByText(/身份登录不会自动授权模型消费/)).toBeVisible();
  });
});
