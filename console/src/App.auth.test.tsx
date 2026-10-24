import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
const { clearToken } = vi.hoisted(() => ({ clearToken: vi.fn() }));
vi.mock("./api/config", () => ({
  getApiUrl: (path: string) => "/api" + path,
  getApiToken: () => localStorage.getItem("lantai_auth_token") || "",
  clearAuthToken: () => {
    clearToken();
    localStorage.removeItem("lantai_auth_token");
  },
}));
vi.mock("./layouts/MainLayout", () => ({
  default: () => <span data-testid="protected-workbench" />,
}));
vi.mock("./contexts/ThemeContext", () => ({
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
  useTheme: () => ({ isDark: false, appearancePalette: "default" }),
}));
vi.mock("./plugins/PluginContext", () => ({
  PluginProvider: ({ children }: { children: ReactNode }) => children,
  usePlugins: () => ({ loading: false }),
}));
vi.mock("./contexts/ApprovalContext", () => ({
  ApprovalProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("./utils/lazyWithRetry", () => ({
  lazyImportWithRetry: () => () => <span data-testid="login-destination" />,
}));
vi.mock("./theme/antdTheme", () => ({ getLantaiAntdTheme: () => ({}) }));
vi.mock("./api/modules/language", () => ({
  languageApi: { getLanguage: async () => ({ language: "zh" }) },
}));
vi.mock("antd-style", () => ({ createGlobalStyle: () => () => null }));
vi.mock("@/vendor/lantaiDesign", () => ({
  Button: ({
    children,
    onClick,
  }: {
    children: ReactNode;
    onClick: () => void;
  }) => <button onClick={onClick}>{children}</button>,
  ConfigProvider: ({ children }: { children: ReactNode }) => children,
  bailianTheme: {},
  bailianDarkTheme: {},
}));
vi.mock("react-i18next", async () => {
  const { default: messages } = await import("./locales/zh.json");
  return {
    useTranslation: () => ({
      t: (key: string) =>
        key.split(".").reduce((value: any, part) => value?.[part], messages) ??
        key,
      i18n: {
        resolvedLanguage: "zh",
        language: "zh",
        on: vi.fn(),
        off: vi.fn(),
        changeLanguage: vi.fn(),
      },
    }),
  };
});
import App from "./App";
function response(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}
function authFetch(status: number, verifyStatus = 200, enabled = true) {
  const fetchMock = vi.fn(async (url: string) =>
    url === "/api/auth/status"
      ? response(status, { enabled, has_users: true })
      : response(verifyStatus, {}),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
describe("App authentication admission and recovery", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem("language", "zh");
    localStorage.setItem("lantai_auth_token", "fixture-existing-session");
    window.location.hash = "#/chat";
    delete window.electronAPI;
  });
  afterEach(() => vi.unstubAllGlobals());
  it("shows backup recovery for HTTP 503 without opening protected routes or clearing the token", async () => {
    const fetchMock = authFetch(503);
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("auth.json.bak");
    expect(screen.getByRole("button", { name: "重新检查" })).toBeVisible();
    expect(screen.queryByTestId("protected-workbench")).toBeNull();
    expect(clearToken).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("preserves the existing network failure explanation and retry action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Fixture offline")),
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "澜台暂时无法连接认证服务。为保护本机数据，本次不会跳过身份校验。",
    );
    expect(screen.getByRole("button", { name: "重新检查" })).toBeVisible();
    expect(clearToken).not.toHaveBeenCalled();
  });
  it("admits the workbench only after a valid login token is verified", async () => {
    const fetchMock = authFetch(200);
    render(<App />);
    expect(await screen.findByTestId("protected-workbench")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/verify", {
      headers: { Authorization: "Bearer fixture-existing-session" },
    });
    expect(clearToken).not.toHaveBeenCalled();
  });
  it("clears a rejected login token and preserves the redirect to login", async () => {
    authFetch(200, 401);
    render(<App />);
    expect(await screen.findByTestId("login-destination")).toBeVisible();
    expect(clearToken).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("protected-workbench")).toBeNull();
  });
  it("admits repaired data only after rechecking status and verifying the original token", async () => {
    const fetchMock = authFetch(503);
    render(<App />);
    await screen.findByRole("alert");
    fetchMock.mockImplementation(async (url: string) =>
      response(
        200,
        url === "/api/auth/status" ? { enabled: true, has_users: true } : {},
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
    expect(await screen.findByTestId("protected-workbench")).toBeVisible();
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/auth/verify"),
    ).toHaveLength(1);
    expect(clearToken).not.toHaveBeenCalled();
  });
  it("keeps protected routes closed on a server failure from verify", async () => {
    authFetch(200, 500);
    render(<App />);
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByTestId("protected-workbench")).toBeNull();
    expect(clearToken).not.toHaveBeenCalled();
  });
  it("preserves explicitly disabled authentication without verifying a token", async () => {
    const fetchMock = authFetch(200, 200, false);
    render(<App />);
    expect(await screen.findByTestId("protected-workbench")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
