import type { AgentSummary } from "../../api/types/agents";
import type { ToolPresentation } from "../../api/types/chat";
import { isSubAgentToolName } from "./components/SubAgentToolCard";

const HIDDEN_INTERNAL_TOOL_NAMES = new Set([
  "extract_memory_candidates",
  "summary_memory",
]);

interface ToolDataWithPresentation {
  presentation?: ToolPresentation | Record<string, unknown> | null;
  content?: Array<{
    data?: {
      name?: string;
      presentation?: ToolPresentation | Record<string, unknown> | null;
    };
  }>;
}

function asToolPresentation(value: unknown): ToolPresentation | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const visibility = String(record.visibility || "").trim();
  const surface = String(record.surface || "").trim();
  const risk_level = String(record.risk_level || "").trim();
  const display_name = String(record.display_name || "").trim();
  const summary = String(record.summary || "").trim();
  if (!visibility || !surface) return null;
  return {
    visibility,
    surface,
    risk_level: risk_level || "low",
    display_name,
    summary,
  };
}

export function readToolPresentation(
  data: ToolDataWithPresentation | undefined,
): ToolPresentation | null {
  const direct = asToolPresentation(data?.presentation);
  if (direct) return direct;

  const content = Array.isArray(data?.content) ? data.content : [];
  for (const item of content) {
    const presentation = asToolPresentation(item?.data?.presentation);
    if (presentation) return presentation;
  }
  return null;
}

export function legacyToolPresentation(
  toolName: string,
  agents: AgentSummary[] = [],
): ToolPresentation | null {
  if (
    toolName.startsWith("memory_") ||
    HIDDEN_INTERNAL_TOOL_NAMES.has(toolName)
  ) {
    return {
      visibility: "hidden",
      surface: "debug",
      risk_level: "low",
      display_name: toolName,
      summary: toolName,
    };
  }

  if (toolName.startsWith("wiki_")) {
    return {
      visibility: "quiet",
      surface: "service",
      risk_level: "low",
      display_name: toolName,
      summary: toolName,
    };
  }

  if (isSubAgentToolName(toolName, agents)) {
    return {
      visibility: "prominent",
      surface: "subagent",
      risk_level: "medium",
      display_name: toolName,
      summary: toolName,
    };
  }

  return {
    visibility: "visible",
    surface: "tool",
    risk_level: "low",
    display_name: toolName,
    summary: toolName,
  };
}

export function resolveToolPresentation(
  toolName: string,
  data: ToolDataWithPresentation | undefined,
  agents: AgentSummary[] = [],
): ToolPresentation | null {
  return readToolPresentation(data) || legacyToolPresentation(toolName, agents);
}
