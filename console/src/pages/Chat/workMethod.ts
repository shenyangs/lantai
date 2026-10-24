import type { WorkMethod } from "../../api/types/workMethod";

export const DEFAULT_WORK_METHODS: WorkMethod[] = [
  {
    id: "standard",
    name: "标准",
    summary: "默认对话方式。自然回答，只在用户需要行动时调用工具。",
    instructions: [],
    scope: [],
    acceptance: [],
    max_no_progress_iterations: 3,
    built_in: true,
  },
  {
    id: "deep_research",
    name: "深度研究",
    summary:
      "适合需要查证、比较或形成判断的问题。先交叉核验，再给出带依据的结论。",
    instructions: [],
    scope: [],
    acceptance: [],
    max_no_progress_iterations: 3,
    built_in: true,
  },
  {
    id: "research_delivery",
    name: "材料到成果",
    summary: "把材料整理为可追溯证据，再生成、校验并交付可编辑成果。",
    instructions: [],
    scope: [],
    acceptance: [],
    max_no_progress_iterations: 3,
    built_in: true,
  },
  {
    id: "sustained_execution",
    name: "持续执行",
    summary:
      "适合目标明确但步骤较长的任务。持续推进到终点，失败时诊断重试并保留进度。",
    instructions: [],
    scope: [],
    acceptance: [],
    max_no_progress_iterations: 3,
    built_in: true,
  },
  {
    id: "collaboration",
    name: "协作",
    summary: "沈行知主持 · 2–4 位协作者 · 无需参会，最终统一交付。",
    instructions: [],
    scope: [],
    acceptance: [],
    max_no_progress_iterations: 3,
    built_in: true,
  },
];

function storageKey(agentId: string, sessionKey: string): string {
  return `lantai:work-method:v1:${agentId}:${sessionKey}`;
}

/**
 * Returns the conversation's saved work method. `null` means it has not been
 * given one yet, so the caller can apply the current default.
 */
export function readPersistedWorkMethodId(
  agentId: string,
  sessionKey: string,
): string | null {
  try {
    return localStorage.getItem(storageKey(agentId, sessionKey));
  } catch {
    return null;
  }
}

export function readWorkMethodId(agentId: string, sessionKey: string): string {
  const persisted = readPersistedWorkMethodId(agentId, sessionKey);
  // `auto` used to invoke a second model and a keyword fallback before every
  // message. Migrate that retired preflight mode to the model-native standard
  // loop instead of preserving a hidden classifier in old conversations.
  return persisted === "auto" ? "standard" : persisted || "standard";
}

export function persistWorkMethodId(
  agentId: string,
  sessionKeys: Array<string | null | undefined>,
  methodId: string,
): void {
  try {
    for (const sessionKey of new Set(sessionKeys.filter(Boolean))) {
      localStorage.setItem(storageKey(agentId, String(sessionKey)), methodId);
    }
  } catch {
    // Selection persistence is an enhancement; the request remains authoritative.
  }
}
