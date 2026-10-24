import { request } from "../request";
import type { AgentStatsSummary } from "../types/agentStats";

export interface GetAgentStatsParams {
  start_date: string;
  end_date: string;
}

const EMPTY_TOOL_HEALTH = {
  total_calls: 0,
  completed_calls: 0,
  failed_calls: 0,
  interrupted_calls: 0,
  success_rate: 0,
  average_duration_ms: 0,
  by_tool: [],
};

/**
 * Keep the statistics page compatible with a backend that is still finishing
 * an in-place upgrade. Older runtimes return the original session/token
 * fields but do not yet include tool health, cache, provider, or storage data.
 */
export function normalizeAgentStatsSummary(
  summary: AgentStatsSummary,
): AgentStatsSummary {
  const partial = summary as Partial<AgentStatsSummary>;
  return {
    ...summary,
    total_cache_tokens: partial.total_cache_tokens ?? 0,
    cache_reuse_rate: partial.cache_reuse_rate ?? 0,
    provider_stats: partial.provider_stats ?? [],
    model_stats: partial.model_stats ?? [],
    tool_health: {
      ...EMPTY_TOOL_HEALTH,
      ...(partial.tool_health ?? {}),
      by_tool: partial.tool_health?.by_tool ?? [],
    },
    storage: partial.storage ?? null,
    by_date: partial.by_date ?? [],
    channel_stats: partial.channel_stats ?? [],
  };
}

export const agentStatsApi = {
  getAgentStats: (params: GetAgentStatsParams) =>
    request<AgentStatsSummary>(
      `/agent-stats?start_date=${encodeURIComponent(
        params.start_date,
      )}&end_date=${encodeURIComponent(params.end_date)}`,
    ).then(normalizeAgentStatsSummary),
};
