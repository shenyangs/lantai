import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { Routes, Route } from "react-router-dom";
import { renderWithProviders } from "../../test/common_setup";
import zh from "../../locales/zh.json";
const { appMessage } = vi.hoisted(() => ({
  appMessage: { error: vi.fn(), success: vi.fn(), info: vi.fn() },
}));
vi.mock("../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: appMessage }),
}));
vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => "/api" + path,
  setAuthToken: (token: string) =>
    localStorage.setItem("lantai_auth_token", token),
}));
vi.mock("react-i18next", async () => {
  const { default: messages } = await import("../../locales/zh.json");
  return {
    useTranslation: () => ({
      t: (key: string) =>
        key.split(".").reduce((value: any, part) => value?.[part], messages) ??
        key,
    }),
  };
});
import LoginPage from "./index";
function response(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}
function renderLogin() {
  return renderWithProviders(
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/chat" element={<span data-testid="chat-destination" />} />
    </Routes>,
    { initialEntries: ["/login"] },
  );
}
function statusFetch(status: number, hasUsers = true) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/auth/status")
      return response(status, { enabled: true, has_users: hasUsers });
    if (url === "/api/auth/login" || url === "/api/auth/register")
      return response(200, {
        token: "fixture-new-session",
        username: "fixture-user",
      });
    throw new Error("Unexpected fixture endpoint");
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
describe("LoginPage authentication recovery", () => {
  it.each([false, true])(
    "ends a stalled status check and ignores its late response; desktop=%s",
    async (desktop) => {
      vi.useFakeTimers();
      vi.stubGlobal("electronAPI", { isDesktop: desktop });
      let complete!: (value: unknown) => void;
      let requestSignal: AbortSignal | undefined;
      const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
        requestSignal = init?.signal ?? undefined;
        return new Promise((resolve) => {
          complete = resolve;
        });
      });
      vi.stubGlobal("fetch", fetchMock);
      renderLogin();
      expect(
        screen.queryByPlaceholderText(zh.login.usernamePlaceholder),
      ).toBeNull();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(desktop ? 6001 : 3001);
      });
      expect(screen.getByRole("alert")).toHaveTextContent("无法确认账户状态");
      expect(screen.getByRole("button", { name: "重新检查" })).toBeVisible();
      expect(requestSignal?.aborted).toBe(true);
      await act(async () =>
        complete(response(200, { enabled: true, has_users: false })),
      );
      expect(screen.getByRole("alert")).toBeVisible();
      expect(screen.queryByText(zh.login.firstUserHint)).toBeNull();
      expect(
        screen.queryByPlaceholderText(zh.login.usernamePlaceholder),
      ).toBeNull();
      expect(localStorage.getItem("lantai_auth_token")).toBe(
        "fixture-existing-session",
      );
    },
  );
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem("lantai_auth_token", "fixture-existing-session");
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
  it("shows backup recovery on 503, keeps the token, and offers no login or registration form", async () => {
    const fetchMock = statusFetch(503, false);
    renderLogin();
    expect(await screen.findByRole("alert")).toHaveTextContent("auth.json.bak");
    expect(screen.getByRole("button", { name: "重新检查" })).toBeVisible();
    expect(
      screen.queryByPlaceholderText(zh.login.usernamePlaceholder),
    ).toBeNull();
    expect(screen.queryByText(zh.login.firstUserHint)).toBeNull();
    expect(localStorage.getItem("lantai_auth_token")).toBe(
      "fixture-existing-session",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(appMessage.error).not.toHaveBeenCalled();
  });
  it("reports a network failure and offers retry without treating the failure as an empty store", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Fixture offline")),
    );
    renderLogin();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "无法确认账户状态",
    );
    expect(screen.getByRole("button", { name: "重新检查" })).toBeVisible();
    expect(
      screen.queryByPlaceholderText(zh.login.passwordPlaceholder),
    ).toBeNull();
    expect(localStorage.getItem("lantai_auth_token")).toBe(
      "fixture-existing-session",
    );
  });
  it("does not expose a submit form while the account check is pending", async () => {
    let complete!: (value: unknown) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise((resolve) => {
            complete = resolve;
          }),
      ),
    );
    renderLogin();
    expect(
      screen.queryByPlaceholderText(zh.login.usernamePlaceholder),
    ).toBeNull();
    await act(async () =>
      complete(response(200, { enabled: true, has_users: true })),
    );
    expect(
      await screen.findByPlaceholderText(zh.login.usernamePlaceholder),
    ).toBeVisible();
  });
  it("rechecks repaired data and preserves ordinary password login", async () => {
    const fetchMock = statusFetch(503);
    renderLogin();
    await screen.findByRole("alert");
    fetchMock.mockImplementation(async (url: string) =>
      url === "/api/auth/status"
        ? response(200, { enabled: true, has_users: true })
        : response(200, {
            token: "fixture-new-session",
            username: "fixture-user",
          }),
    );
    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
    fireEvent.change(
      await screen.findByPlaceholderText(zh.login.usernamePlaceholder),
      { target: { value: "fixture-user" } },
    );
    fireEvent.change(
      screen.getByPlaceholderText(zh.login.passwordPlaceholder),
      { target: { value: "fixture-password" } },
    );
    fireEvent.submit(document.querySelector("form")!);
    expect(await screen.findByTestId("chat-destination")).toBeVisible();
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/auth/login"),
    ).toHaveLength(1);
    expect(
      fetchMock.mock.calls.some(([url]) => url === "/api/auth/register"),
    ).toBe(false);
  });
  it("keeps repeated recovery failures visible and does not create a new account", async () => {
    const fetchMock = statusFetch(503, false);
    renderLogin();
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("alert")).toHaveTextContent("auth.json.bak");
    expect(screen.queryByText(zh.login.firstUserHint)).toBeNull();
    expect(
      screen.queryByPlaceholderText(zh.login.usernamePlaceholder),
    ).toBeNull();
  });
  it("preserves legitimate first-account registration after a successful empty-store check", async () => {
    const fetchMock = statusFetch(200, false);
    renderLogin();
    expect(await screen.findByText(zh.login.firstUserHint)).toBeVisible();
    fireEvent.change(
      screen.getByPlaceholderText(zh.login.usernamePlaceholder),
      { target: { value: "fixture-user" } },
    );
    fireEvent.change(
      screen.getByPlaceholderText(zh.login.passwordPlaceholder),
      { target: { value: "fixture-password" } },
    );
    fireEvent.submit(document.querySelector("form")!);
    expect(await screen.findByTestId("chat-destination")).toBeVisible();
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/auth/register"),
    ).toHaveLength(1);
    expect(
      fetchMock.mock.calls.some(([url]) => url === "/api/auth/login"),
    ).toBe(false);
    expect(appMessage.success).toHaveBeenCalledWith(zh.login.registerSuccess);
  });
  it("preserves the route to chat when authentication is explicitly disabled", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(response(200, { enabled: false, has_users: false })),
    );
    renderLogin();
    expect(await screen.findByTestId("chat-destination")).toBeVisible();
  });
});
