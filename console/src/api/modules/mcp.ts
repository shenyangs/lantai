import { request } from "../request";
import type {
  MCPClientInfo,
  MCPClientCreateRequest,
  MCPClientUpdateRequest,
  MCPToolInfo,
  ChromeDevToolsMCPInfo,
  GuanlanMCPInfo,
  OfficeCliMCPInfo,
  MCPConnectorLifecycleInfo,
  MCPOAuthStartResponse,
  OfficialMCPConnectorInfo,
  OpenChatCutMCPConfigureRequest,
  OpenChatCutMCPInfo,
} from "../types";

export const mcpApi = {
  /**
   * List all MCP clients
   */
  listMCPClients: () => request<MCPClientInfo[]>("/mcp"),

  /**
   * Get details of a specific MCP client
   */
  getMCPClient: (clientKey: string) =>
    request<MCPClientInfo>(`/mcp/${encodeURIComponent(clientKey)}`),

  /**
   * Create a new MCP client
   */
  createMCPClient: (body: MCPClientCreateRequest) =>
    request<MCPClientInfo>("/mcp", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Update an existing MCP client
   */
  updateMCPClient: (clientKey: string, body: MCPClientUpdateRequest) =>
    request<MCPClientInfo>(`/mcp/${encodeURIComponent(clientKey)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  /**
   * Toggle MCP client enabled status
   */
  toggleMCPClient: (clientKey: string) =>
    request<MCPClientInfo>(`/mcp/${encodeURIComponent(clientKey)}/toggle`, {
      method: "PATCH",
    }),

  /**
   * Delete an MCP client
   */
  deleteMCPClient: (clientKey: string) =>
    request<{ message: string }>(`/mcp/${encodeURIComponent(clientKey)}`, {
      method: "DELETE",
    }),

  /**
   * List tools from a connected MCP server
   */
  listMCPTools: (clientKey: string) =>
    request<MCPToolInfo[]>(`/mcp/${encodeURIComponent(clientKey)}/tools`),

  getMCPLifecycle: (clientKey: string) =>
    request<MCPConnectorLifecycleInfo>(
      `/mcp/${encodeURIComponent(clientKey)}/lifecycle`,
    ),

  probeMCPConnector: (clientKey: string) =>
    request<MCPConnectorLifecycleInfo>(
      `/mcp/${encodeURIComponent(clientKey)}/probe`,
      { method: "POST" },
    ),

  startMCPOAuth: (clientKey: string) =>
    request<MCPOAuthStartResponse>(
      `/mcp/${encodeURIComponent(clientKey)}/oauth/start`,
      { method: "POST" },
    ),

  listOfficialMCPConnectors: () =>
    request<OfficialMCPConnectorInfo[]>("/mcp/catalog"),

  installOfficialMCPConnector: (catalogKey: string) =>
    request<MCPClientInfo>(
      `/mcp/catalog/${encodeURIComponent(catalogKey)}/install`,
      { method: "POST" },
    ),

  getOpenChatCutMCP: () => request<OpenChatCutMCPInfo>("/mcp/openchatcut"),

  configureOpenChatCutMCP: (body: OpenChatCutMCPConfigureRequest) =>
    request<OpenChatCutMCPInfo>("/mcp/openchatcut", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  /**
   * Get the built-in Guanlan MCP module status
   */
  getGuanlanMCP: () => request<GuanlanMCPInfo>("/mcp/guanlan"),

  /**
   * Enable or disable the built-in Guanlan MCP module
   */
  setGuanlanMCP: (enabled: boolean) =>
    request<GuanlanMCPInfo>("/mcp/guanlan", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),

  /**
   * Get the built-in OfficeCLI MCP module status
   */
  getOfficeCliMCP: () => request<OfficeCliMCPInfo>("/mcp/officecli"),

  /**
   * Enable or disable the built-in OfficeCLI MCP module
   */
  setOfficeCliMCP: (enabled: boolean) =>
    request<OfficeCliMCPInfo>("/mcp/officecli", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),

  /** Get the bundled Chrome DevTools MCP module status. */
  getChromeDevToolsMCP: () =>
    request<ChromeDevToolsMCPInfo>("/mcp/chrome-devtools"),

  /** Enable or disable the bundled Chrome DevTools MCP module. */
  setChromeDevToolsMCP: (enabled: boolean) =>
    request<ChromeDevToolsMCPInfo>("/mcp/chrome-devtools", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
};
