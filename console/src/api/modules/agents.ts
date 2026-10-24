import { request } from "../request";
import type {
  AgentListResponse,
  AgentProfileConfig,
  CreateAgentRequest,
  DeleteAgentResponse,
  AgentProfileRef,
  ReorderAgentsResponse,
  RestoreWorkspaceResponse,
  RoleTemplateDetail,
  RoleTemplateListResponse,
  RoleTemplatePreviewRequest,
} from "../types/agents";

// Multi-agent management API
export const agentsApi = {
  // List all agents
  listAgents: () => request<AgentListResponse>("/agents"),

  // Get agent details
  getAgent: (agentId: string) =>
    request<AgentProfileConfig>(`/agents/${agentId}`),

  // List role templates for creating collaborator agents
  listRoleTemplates: () =>
    request<RoleTemplateListResponse>("/agents/role-templates"),

  // Get role template detail
  getRoleTemplate: (templateId: string) =>
    request<RoleTemplateDetail>(`/agents/role-templates/${templateId}`),

  // Preview imported markdown role template
  previewRoleTemplate: (payload: RoleTemplatePreviewRequest) =>
    request<RoleTemplateDetail>("/agents/role-templates/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // Create new agent
  createAgent: (agent: CreateAgentRequest) =>
    request<AgentProfileRef>("/agents", {
      method: "POST",
      body: JSON.stringify(agent),
    }),

  // Update agent configuration
  updateAgent: (agentId: string, agent: AgentProfileConfig) =>
    request<AgentProfileConfig>(`/agents/${agentId}`, {
      method: "PUT",
      body: JSON.stringify(agent),
    }),

  // Delete agent
  deleteAgent: (agentId: string) =>
    request<DeleteAgentResponse>(`/agents/${agentId}`, {
      method: "DELETE",
    }),

  // Persist ordered agent ids
  reorderAgents: (agentIds: string[]) =>
    request<ReorderAgentsResponse>("/agents/order", {
      method: "PUT",
      body: JSON.stringify({ agent_ids: agentIds }),
    }),

  restoreWorkspace: (
    agentId: string,
    payload: { expected_source_dir: string; staged_workspace_dir: string },
  ) =>
    request<RestoreWorkspaceResponse>(`/agents/${agentId}/restore-workspace`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // Toggle agent enabled state
  toggleAgentEnabled: (agentId: string, enabled: boolean) =>
    request<{ success: boolean; agent_id: string; enabled: boolean }>(
      `/agents/${agentId}/toggle`,
      {
        method: "PATCH",
        body: JSON.stringify({ enabled }),
      },
    ),
};
