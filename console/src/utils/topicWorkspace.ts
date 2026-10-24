export const TOPIC_WORKSPACE_CHANGED_EVENT =
  "lantai:topic-workspace-changed";

const ACTIVE_TOPIC_WORKSPACE_KEY = "lantai:active-topic-workspace:v1";
const TOPIC_WORKSPACE_MAP_KEY = "lantai:topic-workspace-map:v1";

export interface TopicWorkspaceState {
  agentId: string;
  sessionId: string;
  chatId?: string;
  topicWorkspaceDir: string;
  manualWorkspaceDir?: string | null;
  effectiveWorkspaceDir: string;
  projectWorkspaceDir?: string | null;
  workspaceGrantId?: string | null;
  workspaceAccessMode?: "read_only" | "read_write" | null;
  workspaceTrustState?: "trusted" | "restricted" | null;
}

type TopicWorkspaceMap = Record<string, TopicWorkspaceState>;

function hasStorage(): boolean {
  return typeof window !== "undefined";
}

function storageKey(agentId: string, sessionId: string): string {
  return `${agentId}:${sessionId}`;
}

function readJson<T>(storage: Storage, key: string, fallback: T): T {
  try {
    const raw = storage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function writeJson(storage: Storage, key: string, value: unknown): void {
  try {
    storage.setItem(key, JSON.stringify(value));
  } catch {
    // ignore quota / private-mode failures
  }
}

function readMap(): TopicWorkspaceMap {
  if (!hasStorage()) return {};
  return readJson<TopicWorkspaceMap>(
    window.localStorage,
    TOPIC_WORKSPACE_MAP_KEY,
    {},
  );
}

function writeMap(map: TopicWorkspaceMap): void {
  if (!hasStorage()) return;
  writeJson(window.localStorage, TOPIC_WORKSPACE_MAP_KEY, map);
}

function getMetaString(
  meta: Record<string, unknown> | undefined,
  key: string,
): string {
  const value = meta?.[key];
  return typeof value === "string" ? value.trim() : "";
}

export function buildTopicWorkspaceState(
  agentId: string,
  sessionId: string,
  meta: Record<string, unknown> | undefined,
  chatId?: string,
): TopicWorkspaceState | null {
  const topicWorkspaceDir = getMetaString(meta, "topic_workspace_dir");
  if (!topicWorkspaceDir) return null;
  const manualWorkspaceDir = getMetaString(meta, "manual_workspace_dir") || null;
  const workspaceGrantId = getMetaString(meta, "workspace_grant_id") || null;
  const remembered = hasStorage()
    ? readMap()[storageKey(agentId, sessionId)]
    : null;
  const rememberedGrantWorkspace =
    workspaceGrantId &&
    remembered?.workspaceGrantId === workspaceGrantId
      ? remembered
      : null;
  return {
    agentId,
    sessionId,
    chatId,
    topicWorkspaceDir,
    manualWorkspaceDir,
    effectiveWorkspaceDir:
      rememberedGrantWorkspace?.projectWorkspaceDir ||
      manualWorkspaceDir ||
      topicWorkspaceDir,
    projectWorkspaceDir:
      rememberedGrantWorkspace?.projectWorkspaceDir || null,
    workspaceGrantId,
    workspaceAccessMode:
      rememberedGrantWorkspace?.workspaceAccessMode || null,
    workspaceTrustState:
      rememberedGrantWorkspace?.workspaceTrustState || null,
  };
}

export function rememberTopicWorkspace(state: TopicWorkspaceState): void {
  if (!hasStorage()) return;
  const map = readMap();
  map[storageKey(state.agentId, state.sessionId)] = state;
  writeMap(map);
}

export function getRememberedTopicWorkspace(
  agentId: string,
  sessionId: string,
): TopicWorkspaceState | null {
  if (!hasStorage() || !agentId || !sessionId) return null;
  return readMap()[storageKey(agentId, sessionId)] ?? null;
}

export function setActiveTopicWorkspace(
  state: TopicWorkspaceState | null,
): void {
  if (!hasStorage()) return;
  if (state) {
    rememberTopicWorkspace(state);
    writeJson(window.sessionStorage, ACTIVE_TOPIC_WORKSPACE_KEY, state);
  } else {
    window.sessionStorage.removeItem(ACTIVE_TOPIC_WORKSPACE_KEY);
  }
  window.dispatchEvent(new Event(TOPIC_WORKSPACE_CHANGED_EVENT));
}

export function getActiveTopicWorkspace(): TopicWorkspaceState | null {
  if (!hasStorage()) return null;
  return readJson<TopicWorkspaceState | null>(
    window.sessionStorage,
    ACTIVE_TOPIC_WORKSPACE_KEY,
    null,
  );
}

export function updateActiveTopicWorkspaceFromMeta(
  agentId: string,
  sessionId: string,
  meta: Record<string, unknown> | undefined,
  chatId?: string,
): TopicWorkspaceState | null {
  const state = buildTopicWorkspaceState(agentId, sessionId, meta, chatId);
  if (state) {
    setActiveTopicWorkspace(state);
  }
  return state;
}
