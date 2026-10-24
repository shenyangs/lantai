// Multi-agent management types

import type { ModelSlotConfig } from "./provider";

export interface AgentSummary {
  id: string;
  name: string;
  description: string;
  workspace_dir: string;
  enabled: boolean;
  active_model?: ModelSlotConfig | null;
  workspace_access_required?: boolean;
}

export interface AgentListResponse {
  agents: AgentSummary[];
}

export interface ReorderAgentsResponse {
  success: boolean;
  agent_ids: string[];
}

export interface RestoreWorkspaceResponse {
  success: boolean;
  agent_id: string;
  workspace_dir: string;
  backup_dir?: string | null;
  source_preserved: boolean;
}

export interface AgentWorkspaceLifecycleReport {
  agent_id: string;
  workspace_dir: string;
  workspace_exists: boolean;
  workspace_deleted: boolean;
  artifact_archive?: Record<string, unknown> | null;
  artifact_delete_dry_run?: Record<string, unknown> | null;
  approval_memory_lifecycle?: Record<string, unknown> | null;
  file_grant_lifecycle?: Record<string, unknown> | null;
  runtime_archives?: Array<Record<string, unknown>>;
}

export interface DeleteAgentResponse {
  success: boolean;
  agent_id: string;
  workspace_lifecycle?: AgentWorkspaceLifecycleReport;
}

export interface AgentProfileConfig {
  id: string;
  name: string;
  description?: string;
  workspace_dir?: string;
  approval_level?: string;
  enableQuqu?: boolean;
  active_model?: ModelSlotConfig | null;
  channels?: unknown;
  mcp?: unknown;
  heartbeat?: unknown;
  running?: unknown;
  llm_routing?: unknown;
  system_prompt_files?: string[];
  tools?: unknown;
  security?: unknown;
}

export interface CreateAgentRequest {
  id?: string;
  name: string;
  description?: string;
  workspace_dir?: string;
  language?: string;
  skill_names?: string[];
  active_model?: ModelSlotConfig | null;
  role_template_id?: string;
  role_template_markdown?: string;
  role_template_source_name?: string;
}

export interface AgentProfileRef {
  id: string;
  workspace_dir: string;
}

export type RoleTemplateCheckStatus = "pass" | "warning" | "error";

export interface RoleTemplateCheck {
  key: string;
  label: string;
  status: RoleTemplateCheckStatus;
  detail: string;
}

export interface RoleTemplateSummary {
  id: string;
  name: string;
  description: string;
  category: string;
  tags: string[];
  accent_color: string;
  source: "builtin" | "imported";
  suggested_agent_id: string;
  checks: RoleTemplateCheck[];
  body_preview: string;
  recommended_skills: string[];
}

export interface RoleTemplateDetail extends RoleTemplateSummary {
  body: string;
  markdown: string;
}

export interface RoleTemplateListResponse {
  templates: RoleTemplateSummary[];
}

export interface RoleTemplatePreviewRequest {
  content: string;
  source_name?: string;
}
