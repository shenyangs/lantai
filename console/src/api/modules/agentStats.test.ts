import { describe, expect, it } from "vitest";
import type { AgentStatsSummary } from "../types/agentStats";
import { normalizeAgentStatsSummary } from "./agentStats";

describe("normalizeAgentStatsSummary", () => {
  it("adds safe defaults for a response from an older backend", () => {
    const legacySummary = {
      total_active_sessions: 1,
      total_messages: 2,
      total_user_messages: 1,
      total_assistant_messages: 1,
      total_prompt_tokens: 10,
      total_completion_tokens: 5,
      total_llm_calls: 1,
      total_tool_calls: 0,
      by_date: [],
      channel_stats: [],
      start_date: "2026-07-20",
      end_date: "2026-07-27",
    } as AgentStatsSummary;

    const normalized = normalizeAgentStatsSummary(legacySummary);

    expect(normalized.total_cache_tokens).toBe(0);
    expect(normalized.provider_stats).toEqual([]);
    expect(normalized.tool_health).toEqual({
      total_calls: 0,
      completed_calls: 0,
      failed_calls: 0,
      interrupted_calls: 0,
      success_rate: 0,
      average_duration_ms: 0,
      by_tool: [],
    });
    expect(normalized.storage).toBeNull();
  });
});
