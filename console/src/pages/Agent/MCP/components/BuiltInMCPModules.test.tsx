import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../../../../test/common_setup";
import { BuiltInMCPModules } from "./BuiltInMCPModules";

const apiMock = vi.hoisted(() => ({
  getChromeDevToolsMCP: vi.fn(),
  getOfficeCliMCP: vi.fn(),
  setChromeDevToolsMCP: vi.fn(),
  setOfficeCliMCP: vi.fn(),
}));

vi.mock("@/api", () => ({ api: apiMock }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: { success: vi.fn(), error: vi.fn() },
  }),
}));

const officeCli = {
  client_key: "officecli",
  name: "OfficeCLI",
  description: "Office documents",
  enabled: true,
  configured: false,
  available: true,
  executable_path: "/usr/local/bin/officecli",
  executable_source: "path",
  version: "1.0.143",
  package_message: "ready",
  repository_url: "https://github.com/iOfficeAI/OfficeCLI",
  docs_url: "https://iofficeai.github.io/OfficeCLI/",
  tools: [],
};

const chromeDevTools = {
  client_key: "chrome-devtools",
  name: "Chrome DevTools",
  description: "Chrome debugging",
  enabled: false,
  configured: false,
  available: true,
  capability_status: "disabled",
  connected: false,
  runtime_attempt_count: 0,
  runtime_error_code: null,
  runtime_message: "bundled",
  runtime_updated_at: null,
  package_version: "1.8.0",
  runtime_source: "source",
  repository_url: "https://github.com/ChromeDevTools/chrome-devtools-mcp",
  docs_url: "https://github.com/ChromeDevTools/chrome-devtools-mcp",
  license: "Apache-2.0",
  profile_mode: "persistent_dedicated",
  attaches_existing_chrome: false,
  usage_statistics_enabled: false,
  update_checks_enabled: false,
  performance_crux_enabled: true,
  network_headers_redacted: false,
  unrestricted_paths_enabled: true,
  filesystem_access: "host_authorized",
  screencast_enabled: true,
  allowed_tools: ["list_pages", "evaluate_script", "upload_file"],
};

describe("BuiltInMCPModules", () => {
  beforeEach(() => {
    Object.values(apiMock).forEach((mock) => mock.mockReset());
    apiMock.getChromeDevToolsMCP.mockResolvedValue(chromeDevTools);
    apiMock.getOfficeCliMCP.mockResolvedValue(officeCli);
    apiMock.setChromeDevToolsMCP.mockResolvedValue({
      ...chromeDevTools,
      enabled: true,
      configured: true,
      capability_status: "starting",
    });
    apiMock.setOfficeCliMCP.mockResolvedValue({
      ...officeCli,
      enabled: false,
      configured: true,
    });
  });

  it("shows the pinned full-capability Chrome DevTools module", async () => {
    renderWithProviders(<BuiltInMCPModules />);

    expect(await screen.findByText("Chrome DevTools")).toBeInTheDocument();
    expect(
      screen.getByText("mcp.chromeDevToolsDescription"),
    ).toBeInTheDocument();
    expect(screen.getByText("mcp.chromeDevToolsPrivacy")).toBeInTheDocument();
    expect(screen.getByText("mcp.chromeDevToolsBundled")).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "mcp.chromeDevToolsToggle" }),
    ).not.toBeChecked();
  });

  it("shows OfficeCLI as an enabled built-in capability", async () => {
    renderWithProviders(<BuiltInMCPModules />);

    expect(await screen.findByText("OfficeCLI")).toBeInTheDocument();
    expect(screen.getByText("1.0.143")).toBeInTheDocument();
    expect(screen.getByText("mcp.officeCliDescription")).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "mcp.officeCliToggle" }),
    ).toBeChecked();
  });

  it("persists an explicit disable from the lightweight switch", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BuiltInMCPModules />);

    const toggle = await screen.findByRole("switch", {
      name: "mcp.officeCliToggle",
    });
    await user.click(toggle);

    await waitFor(() =>
      expect(apiMock.setOfficeCliMCP).toHaveBeenCalledWith(false),
    );
    expect(toggle).not.toBeChecked();
  });

  it("enables Chrome DevTools through the reserved module endpoint", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BuiltInMCPModules />);

    const toggle = await screen.findByRole("switch", {
      name: "mcp.chromeDevToolsToggle",
    });
    await user.click(toggle);

    await waitFor(() =>
      expect(apiMock.setChromeDevToolsMCP).toHaveBeenCalledWith(true),
    );
    expect(toggle).toBeChecked();
  });

  it("shows a real connection failure instead of package presence", async () => {
    apiMock.getChromeDevToolsMCP.mockResolvedValue({
      ...chromeDevTools,
      enabled: true,
      capability_status: "error",
      runtime_error_code: "ConnectionError",
    });

    renderWithProviders(<BuiltInMCPModules />);

    expect(
      await screen.findByText("mcp.chromeDevToolsError"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "mcp.chromeDevToolsToggle" }),
    ).toBeChecked();
  });
});
