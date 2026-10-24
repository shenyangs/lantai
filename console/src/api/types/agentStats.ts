export interface ChannelStats {
  channel: string;
  session_count: number;
  user_messages: number;
  assistant_messages: number;
  total_messages: number;
}

export interface DailyStats {
  date: string;
  chats: number;
  active_sessions: number;
  user_messages: number;
  assistant_messages: number;
  total_messages: number;
  prompt_tokens: number;
  completion_tokens: number;
  llm_calls: number;
  tool_calls: number;
}

export interface ProviderUsageStats {
  provider_id: string;
  model_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  call_count: number;
}

export interface ModelUsageStats {
  provider_id: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  call_count: number;
}

export interface ToolUsageStats {
  name: string;
  total_calls: number;
  completed_calls: number;
  failed_calls: number;
  interrupted_calls: number;
  average_duration_ms: number;
  last_used_at?: string | null;
  last_error?: string | null;
}

export interface ToolHealthSummary {
  total_calls: number;
  completed_calls: number;
  failed_calls: number;
  interrupted_calls: number;
  success_rate: number;
  average_duration_ms: number;
  by_tool: ToolUsageStats[];
}

export interface StorageCategoryStats {
  key: string;
  label: string;
  bytes: number;
  file_count: number;
  directory_count: number;
  cleanable: boolean;
}

export interface StorageSummary {
  root_path: string;
  workspace_path: string;
  used_bytes: number;
  disk_total_bytes: number;
  disk_free_bytes: number;
  file_count: number;
  directory_count: number;
  scanned_at: string;
  truncated: boolean;
  categories: StorageCategoryStats[];
}

export interface AgentStatsSummary {
  total_active_sessions: number;
  total_messages: number;
  total_user_messages: number;
  total_assistant_messages: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_llm_calls: number;
  total_tool_calls: number;
  total_cache_tokens: number;
  cache_reuse_rate: number;
  provider_stats: ProviderUsageStats[];
  model_stats: ModelUsageStats[];
  tool_health: ToolHealthSummary;
  storage?: StorageSummary | null;
  by_date: DailyStats[];
  channel_stats: ChannelStats[];
  start_date: string;
  end_date: string;
}
