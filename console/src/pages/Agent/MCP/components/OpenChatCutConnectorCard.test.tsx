import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../../../../test/common_setup";
import { OpenChatCutConnectorCard } from "./OpenChatCutConnectorCard";

const apiMock = vi.hoisted(() => ({
  getOpenChatCutMCP: vi.fn(),
  configureOpenChatCutMCP: vi.fn(),
  probeMCPConnector: vi.fn(),
}));
const messageMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));
const tMock = vi.hoisted(() => (key: string) => key);

vi.mock("@/api", () => ({ api: apiMock }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: tMock }),
}));
vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: messageMock,
  }),
}));

const unconfigured = {
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
} as const;

describe("OpenChatCutConnectorCard", () => {
  beforeEach(() => {
    Object.values(apiMock).forEach((mock) => mock.mockReset());
    apiMock.getOpenChatCutMCP.mockResolvedValue(unconfigured);
    apiMock.configureOpenChatCutMCP.mockResolvedValue({
      ...unconfigured,
      configured: true,
      enabled: true,
      token_configured: true,
      connection_status: "connecting",
    });
    apiMock.probeMCPConnector.mockResolvedValue({
      client_key: "openchatcut",
      status: "ready",
      auth_status: "not_required",
      connection_status: "ready",
      tool_count: 12,
      allowed_tools: [],
    });
  });

  it("connects the local video workbench with an endpoint and bearer token", async () => {
    const user = userEvent.setup();
    const onClientsChanged = vi.fn();
    renderWithProviders(
      <OpenChatCutConnectorCard onClientsChanged={onClientsChanged} />,
    );

    expect(
      await screen.findByText("视频工程 · OpenChatCut"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "mcp.catalogConnect" }),
    );
    await user.type(
      screen.getByLabelText("mcp.openChatCutTokenLabel"),
      "local-token",
    );
    await user.click(
      screen.getByRole("button", { name: "mcp.openChatCutSave" }),
    );

    await waitFor(() =>
      expect(apiMock.configureOpenChatCutMCP).toHaveBeenCalledWith({
        endpoint: "http://127.0.0.1:5199/api/external-mcp/mcp",
        token: "local-token",
        enabled: true,
      }),
    );
    expect(apiMock.probeMCPConnector).toHaveBeenCalledWith("openchatcut");
    expect(onClientsChanged).toHaveBeenCalled();
    expect(screen.getByText("mcp.openChatCutReady")).toBeInTheDocument();
  });
});
