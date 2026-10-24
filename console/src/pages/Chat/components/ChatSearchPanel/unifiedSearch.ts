import { chatApi } from "@/api/modules/chat";
import { workspaceApi } from "@/api/modules/workspace";
import { worksApi } from "@/api/modules/works";
import type {
  ChatSearchHit,
  UserMemoryEntry,
  WikiSearchHit,
} from "@/api/types";
import type { WorkItem } from "@/api/types/works";

export type UnifiedSearchKind = "chat" | "memory" | "knowledge" | "work";

export interface UnifiedSearchResult {
  id: string;
  kind: UnifiedSearchKind;
  groupLabel: string;
  title: string;
  excerpt: string;
  meta: string;
  timestamp?: string | null;
  path: string;
  chatId?: string;
  messageId?: string;
}

export interface UnifiedSearchSources {
  chats: ChatSearchHit[];
  memories: UserMemoryEntry[];
  knowledge: WikiSearchHit[];
  works: WorkItem[];
}

export interface UnifiedSearchResponse {
  results: UnifiedSearchResult[];
  failedSources: string[];
}

export const SEARCH_GROUP_ORDER: UnifiedSearchKind[] = [
  "chat",
  "memory",
  "knowledge",
  "work",
];

const GROUP_LABELS: Record<UnifiedSearchKind, string> = {
  chat: "聊天",
  memory: "记忆",
  knowledge: "知识库",
  work: "作品",
};

function contains(value: unknown, query: string) {
  return String(value || "")
    .toLocaleLowerCase()
    .includes(query);
}

function clip(value: unknown, length = 180) {
  const normalized = String(value || "")
    .replace(/\s+/g, " ")
    .trim();
  return normalized.length > length
    ? `${normalized.slice(0, length).trimEnd()}…`
    : normalized;
}

function memoryTitle(entry: UserMemoryEntry) {
  return (
    entry.source_title ||
    (entry.subject === "project" ? "项目记忆" : "长期记忆")
  );
}

export function buildUnifiedSearchResults(
  rawQuery: string,
  sources: UnifiedSearchSources,
): UnifiedSearchResult[] {
  const query = rawQuery.trim().toLocaleLowerCase();
  if (!query) return [];

  const candidates: UnifiedSearchResult[] = [
    ...sources.chats.map((hit) => ({
      id: `chat:${hit.id}`,
      kind: "chat" as const,
      groupLabel: GROUP_LABELS.chat,
      title: hit.chat_name,
      excerpt: clip(hit.excerpt),
      meta: [
        hit.archived ? "已归档" : hit.coding ? "编程会话" : "",
        hit.kind === "title"
          ? "会话标题"
          : hit.role === "user"
            ? "你的消息"
            : "沈行知的回答",
      ]
        .filter(Boolean)
        .join(" · "),
      timestamp: hit.timestamp,
      path: `/chat/${encodeURIComponent(hit.chat_id)}`,
      chatId: hit.chat_id,
      messageId: hit.message_id || undefined,
    })),
    ...sources.memories
      .filter(
        (entry) =>
          entry.status !== "archived" &&
          (contains(entry.content, query) ||
            contains(entry.source_title, query) ||
            contains(entry.reason, query)),
      )
      .map((entry) => ({
        id: `memory:${entry.id}`,
        kind: "memory" as const,
        groupLabel: GROUP_LABELS.memory,
        title: memoryTitle(entry),
        excerpt: clip(entry.content),
        meta: entry.scope === "global" ? "长期记忆" : "相关记忆",
        timestamp: entry.updated_at,
        path: `/memory?query=${encodeURIComponent(entry.content.slice(0, 80))}`,
      })),
    ...sources.knowledge.map((hit) => ({
      id: `knowledge:${hit.kind}:${hit.path}`,
      kind: "knowledge" as const,
      groupLabel: GROUP_LABELS.knowledge,
      title: hit.title || hit.filename,
      excerpt: clip(hit.snippet),
      meta: hit.kind === "source" ? "原始资料" : "知识笔记",
      path: `/workspace?query=${encodeURIComponent(hit.title || hit.filename)}`,
    })),
    ...sources.works
      .filter(
        (work) =>
          contains(work.title, query) ||
          contains(work.path, query) ||
          contains(work.kind, query),
      )
      .map((work) => ({
        id: `work:${work.id}`,
        kind: "work" as const,
        groupLabel: GROUP_LABELS.work,
        title: work.title,
        excerpt: `版本 ${work.version} · ${work.kind}`,
        meta: "收藏作品",
        timestamp: work.updated_at,
        path: `/works?work=${encodeURIComponent(work.id)}`,
      })),
  ];

  const seen = new Set<string>();
  return candidates.filter((item) => {
    const owner = item.chatId || item.path;
    const identity = `${item.kind}:${owner}:${item.meta}:${item.title.toLocaleLowerCase()}:${item.excerpt.toLocaleLowerCase()}`;
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

export async function loadUnifiedSearch(
  query: string,
): Promise<UnifiedSearchResponse> {
  const settled = await Promise.allSettled([
    chatApi.searchChats(query),
    workspaceApi.listUserMemory(),
    workspaceApi.searchWiki(query),
    worksApi.list(),
  ]);
  const labels = ["聊天", "记忆", "知识库", "作品"];
  const failedSources = settled.flatMap((result, index) =>
    result.status === "rejected" ? [labels[index]] : [],
  );
  const value = <T>(index: number, fallback: T): T => {
    const result = settled[index];
    return result.status === "fulfilled" ? (result.value as T) : fallback;
  };
  const memory = value<Awaited<ReturnType<typeof workspaceApi.listUserMemory>>>(
    1,
    { entries: [] } as unknown as Awaited<
      ReturnType<typeof workspaceApi.listUserMemory>
    >,
  );
  const wiki = value<Awaited<ReturnType<typeof workspaceApi.searchWiki>>>(2, {
    query,
    hits: [],
  });
  return {
    results: buildUnifiedSearchResults(query, {
      chats: value(0, []),
      memories: memory.entries || [],
      knowledge: wiki.hits || [],
      works: value(3, []),
    }),
    failedSources,
  };
}
