import { request } from "../request";

export interface ToolConfigField {
  name: string;
  label: string;
  type: "text" | "password" | "number" | "boolean" | "select" | "textarea";
  required: boolean;
  placeholder?: string;
  help?: string;
  options?: string[];
  default?: any;
  min?: number;
  max?: number;
}

export interface ToolInfo {
  name: string;
  enabled: boolean;
  revision: number;
  description: string;
  async_execution: boolean;
  icon: string;
  display_name?: string;
  category?: string;
  category_name?: string;
  category_description?: string;
  category_order?: number;
  source?: "builtin" | "plugin" | string;
  source_label?: string;
  locked?: boolean;
  risk_hint?: "read_only" | "workspace_change" | "external_action" | string;
  risk_label?: string;
  requires_config?: boolean;
  config_fields?: ToolConfigField[];
  config_values?: Record<string, any>;
}

export interface ToolPermissionProfile {
  id: "observe" | "workspace" | "full";
  name: string;
  description: string;
  examples: string[];
}

export const toolsApi = {
  /**
   * List all built-in tools
   */
  listTools: () => request<ToolInfo[]>("/tools"),

  /**
   * List built-in tool permission profiles for Runtime v1.
   */
  listPermissionProfiles: () =>
    request<ToolPermissionProfile[]>("/tools/permission-profiles"),

  /**
   * Toggle tool enabled status
   */
  toggleTool: (toolName: string) =>
    request<ToolInfo>(`/tools/${encodeURIComponent(toolName)}/toggle`, {
      method: "PATCH",
    }),

  /**
   * Set an explicit enabled state against the last state shown to the user.
   */
  setToolEnabled: (
    toolName: string,
    enabled: boolean,
    expectedRevision: number,
  ) =>
    request<ToolInfo>(`/tools/${encodeURIComponent(toolName)}/enabled`, {
      method: "PATCH",
      body: JSON.stringify({
        enabled,
        expected_revision: expectedRevision,
      }),
    }),

  /**
   * Update tool async_execution setting
   */
  updateAsyncExecution: (toolName: string, asyncExecution: boolean) =>
    request<ToolInfo>(
      `/tools/${encodeURIComponent(toolName)}/async-execution`,
      {
        method: "PATCH",
        body: JSON.stringify({ async_execution: asyncExecution }),
      },
    ),

  /**
   * Get tool configuration
   */
  getToolConfig: (toolName: string) =>
    request<Record<string, any>>(
      `/tools/${encodeURIComponent(toolName)}/config`,
    ),

  /**
   * Update tool configuration
   */
  updateToolConfig: (toolName: string, config: Record<string, any>) =>
    request<{ status: string; message: string }>(
      `/tools/${encodeURIComponent(toolName)}/config`,
      {
        method: "POST",
        body: JSON.stringify({ config }),
      },
    ),
};
