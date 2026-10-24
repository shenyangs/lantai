import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../../../../test/common_setup";
import { OfficialConnectorCatalog } from "./OfficialConnectorCatalog";

const apiMock = vi.hoisted(() => ({
  listOfficialMCPConnectors: vi.fn(),
  getKdocsConnectionStatus: vi.fn(),
  getWpsNoteConnectionStatus: vi.fn(),
  connectWpsNote: vi.fn(),
  installOfficialMCPConnector: vi.fn(),
  startMCPOAuth: vi.fn(),
  getOpenChatCutMCP: vi.fn(),
  configureOpenChatCutMCP: vi.fn(),
  probeMCPConnector: vi.fn(),
}));

vi.mock("@/api", () => ({ api: apiMock }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));
vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
  }),
}));

const remotes = [
  {
    key: "notion",
    name: "Notion",
    description: "Notion connector",
    url: "https://mcp.notion.com/mcp",
    docs_url: "https://developers.notion.com",
    official: true,
    configured: false,
  },
  {
    key: "linear",
    name: "Linear",
    description: "Linear connector",
    url: "https://mcp.linear.app/mcp",
    docs_url: "https://linear.app/docs/mcp",
    official: true,
    configured: false,
  },
  {
    key: "figma",
    name: "Figma",
    description: "Figma connector",
    url: "https://mcp.figma.com/mcp",
    docs_url: "https://help.figma.com",
    official: true,
    configured: false,
  },
];

describe("OfficialConnectorCatalog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.electronAPI = undefined;
    Object.values(apiMock).forEach((mock) => mock.mockReset());
    apiMock.listOfficialMCPConnectors.mockResolvedValue(remotes);
    apiMock.getKdocsConnectionStatus.mockResolvedValue({
      installed: true,
      authenticated: true,
      cli_version: "2.6.7",
      token_url: "https://www.kdocs.cn/latest",
    });
    apiMock.getWpsNoteConnectionStatus.mockResolvedValue({
      installed: false,
      desktop_installed: true,
      connected: false,
      cli_version: "",
      tool_count: 0,
      install_method: "wps_note_desktop",
      repository_url: "https://github.com/wpsnote/wpsnote-skills",
      download_url: "https://ainote.kdocs.cn/wps-notes-hub/",
      next_step: "enable_in_wps_note",
      launch_requested: false,
    });
    apiMock.connectWpsNote.mockResolvedValue({
      installed: false,
      desktop_installed: true,
      connected: false,
      cli_version: "",
      tool_count: 0,
      install_method: "wps_note_desktop",
      repository_url: "https://github.com/wpsnote/wpsnote-skills",
      download_url: "https://ainote.kdocs.cn/wps-notes-hub/",
      next_step: "enable_in_wps_note",
      launch_requested: true,
    });
    apiMock.getOpenChatCutMCP.mockResolvedValue({
      client_key: "openchatcut",
      name: "视频工程 · OpenChatCut",
      description: "Local video workbench",
      configured: false,
      enabled: false,
      endpoint: "http://127.0.0.1:5199/api/external-mcp/mcp",
      token_configured: false,
      connection_status: "not_configured",
      repository_url: "https://github.com/0xsline/OpenChatCut",
      docs_url: "https://github.com/0xsline/OpenChatCut/blob/main/README_ZH.md",
      license: "AGPL-3.0-or-later",
      approval_mode: "manual",
    });
    apiMock.installOfficialMCPConnector.mockResolvedValue({ key: "notion" });
    apiMock.startMCPOAuth.mockResolvedValue({
      authorization_url: "https://mcp.notion.com/authorize",
      state: "state",
      expires_at: 123,
    });
  });

  it("shows local WPS connectors and only verified official remote entries", async () => {
    renderWithProviders(
      <OfficialConnectorCatalog onClientsChanged={vi.fn()} />,
    );

    expect(await screen.findByText("mcp.kdocsName")).toBeInTheDocument();
    expect(screen.getByText("mcp.wpsNoteName")).toBeInTheDocument();
    expect(screen.getByText("视频工程 · OpenChatCut")).toBeInTheDocument();
    expect(screen.getByText("Notion")).toBeInTheDocument();
    expect(screen.getByText("Linear")).toBeInTheDocument();
    expect(screen.getByText("Figma")).toBeInTheDocument();
    expect(screen.getByText(/v2\.6\.7/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "mcp.kdocsName" }).tagName).toBe(
      "svg",
    );
    expect(
      screen.getByRole("img", { name: "mcp.wpsNoteName" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Notion" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Linear" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Figma" })).toBeInTheDocument();
  });

  it("registers OAuth before opening an official remote authorization", async () => {
    const user = userEvent.setup();
    const authWindow = {
      document: { title: "" },
      location: { href: "" },
      close: vi.fn(),
    };
    vi.spyOn(window, "open").mockReturnValue(authWindow as unknown as Window);
    const onClientsChanged = vi.fn();
    renderWithProviders(
      <OfficialConnectorCatalog onClientsChanged={onClientsChanged} />,
    );

    await screen.findByText("Notion");
    const notionCard = screen.getByText("Notion").closest("article");
    expect(notionCard).not.toBeNull();
    await user.click(
      within(notionCard as HTMLElement).getByRole("button", {
        name: "mcp.catalogConnect",
      }),
    );

    await waitFor(() =>
      expect(apiMock.installOfficialMCPConnector).toHaveBeenCalledWith(
        "notion",
      ),
    );
    expect(apiMock.startMCPOAuth).toHaveBeenCalledWith("notion");
    expect(authWindow.location.href).toBe("https://mcp.notion.com/authorize");
    expect(onClientsChanged).toHaveBeenCalled();
  });

  it("opens OAuth through the desktop external-link bridge", async () => {
    const user = userEvent.setup();
    const openExternal = vi.fn().mockResolvedValue({ success: true });
    window.electronAPI = { openExternal };
    const windowOpen = vi.spyOn(window, "open").mockReturnValue(null);
    renderWithProviders(
      <OfficialConnectorCatalog onClientsChanged={vi.fn()} />,
    );

    await screen.findByText("Notion");
    const notionCard = screen.getByText("Notion").closest("article");
    expect(notionCard).not.toBeNull();
    await user.click(
      within(notionCard as HTMLElement).getByRole("button", {
        name: "mcp.catalogConnect",
      }),
    );

    await waitFor(() =>
      expect(openExternal).toHaveBeenCalledWith(
        "https://mcp.notion.com/authorize",
      ),
    );
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it("connects WPS Note in-product instead of opening GitHub", async () => {
    const user = userEvent.setup();
    const openExternal = vi.fn();
    window.electronAPI = { openExternal };
    renderWithProviders(
      <OfficialConnectorCatalog onClientsChanged={vi.fn()} />,
    );

    await screen.findByText("mcp.wpsNoteName");
    const card = screen.getByText("mcp.wpsNoteName").closest("article");
    expect(card).not.toBeNull();
    await user.click(
      within(card as HTMLElement).getByRole("button", {
        name: "mcp.catalogConnect",
      }),
    );

    await waitFor(() => expect(apiMock.connectWpsNote).toHaveBeenCalled());
    expect(openExternal).not.toHaveBeenCalled();
    expect(screen.getByText("mcp.wpsNoteConnectTitle")).toBeInTheDocument();
  });

  it("opens only the official WPS download when the desktop app is missing", async () => {
    const user = userEvent.setup();
    const openExternal = vi.fn().mockResolvedValue({ success: true });
    window.electronAPI = { openExternal };
    apiMock.connectWpsNote.mockResolvedValueOnce({
      installed: false,
      desktop_installed: false,
      connected: false,
      cli_version: "",
      tool_count: 0,
      install_method: "wps_note_desktop",
      repository_url: "https://github.com/wpsnote/wpsnote-skills",
      download_url: "https://ainote.kdocs.cn/wps-notes-hub/",
      next_step: "install_wps_note",
      launch_requested: false,
    });
    renderWithProviders(
      <OfficialConnectorCatalog onClientsChanged={vi.fn()} />,
    );

    await screen.findByText("mcp.wpsNoteName");
    const card = screen.getByText("mcp.wpsNoteName").closest("article");
    await user.click(
      within(card as HTMLElement).getByRole("button", {
        name: "mcp.catalogConnect",
      }),
    );

    await waitFor(() =>
      expect(openExternal).toHaveBeenCalledWith(
        "https://ainote.kdocs.cn/wps-notes-hub/",
      ),
    );
    expect(openExternal).not.toHaveBeenCalledWith(
      "https://github.com/wpsnote/wpsnote-skills",
    );
  });
});
