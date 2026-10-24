import type { TFunction } from "i18next";
import type { AgentSummary } from "../api/types/agents";

export const DEFAULT_AGENT_ID = "default";
export const DEFAULT_AGENT_DISPLAY_NAME = "Default Agent";
const STALE_DEFAULT_AGENT_NAMES = new Set(["澜小问", "小澜"]);

/** UI label for an agent; `default` id uses i18n, others use API `name` (fallback: id). */
export function getAgentDisplayName(
  agent: Pick<AgentSummary, "id" | "name">,
  t: TFunction,
): string {
  // For default agent, preserve i18n unless explicitly customized
  if (agent.id === DEFAULT_AGENT_ID) {
    if (agent.name && STALE_DEFAULT_AGENT_NAMES.has(agent.name)) {
      return t("agent.defaultDisplayName");
    }
    // If name is customized (not the default placeholder), show custom name
    if (agent.name && agent.name !== DEFAULT_AGENT_DISPLAY_NAME) {
      return agent.name;
    }
    // Otherwise, fall back to localized default name
    return t("agent.defaultDisplayName");
  }
  // For other agents, use user-defined name or fallback to id
  return agent.name || agent.id;
}
