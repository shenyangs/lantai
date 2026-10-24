import type { AiWelcomeGreeting } from "./types";

const STORAGE_PREFIX = "lantai-welcome-v1";
const RECENT_LIMIT = 8;

interface WelcomeCache {
  recentGreetingIds: string[];
  aiCandidates: AiWelcomeGreeting[];
  preparedAt?: string;
}

const emptyCache = (): WelcomeCache => ({
  recentGreetingIds: [],
  aiCandidates: [],
});

function cacheKey(agentId: string, locale: string): string {
  return `${STORAGE_PREFIX}:${encodeURIComponent(agentId)}:${encodeURIComponent(locale)}`;
}

function isAiCandidate(value: unknown): value is AiWelcomeGreeting {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AiWelcomeGreeting>;
  return (
    typeof candidate.id === "string" &&
    typeof candidate.title === "string" &&
    typeof candidate.subtitle === "string" &&
    candidate.source === "ai" &&
    candidate.scope === "new_chat_empty_welcome" &&
    candidate.canEnterChatHistory === false &&
    candidate.reviewStatus === "approved" &&
    Array.isArray(candidate.scenarioKeys)
  );
}

export function readWelcomeCache(
  storage: Storage,
  agentId: string,
  locale: string,
): WelcomeCache {
  try {
    const raw = storage.getItem(cacheKey(agentId, locale));
    if (!raw) return emptyCache();
    const parsed = JSON.parse(raw) as Partial<WelcomeCache>;
    return {
      recentGreetingIds: Array.isArray(parsed.recentGreetingIds)
        ? parsed.recentGreetingIds
            .filter((value): value is string => typeof value === "string")
            .slice(-RECENT_LIMIT)
        : [],
      aiCandidates: Array.isArray(parsed.aiCandidates)
        ? parsed.aiCandidates.filter(isAiCandidate).slice(0, 8)
        : [],
      ...(typeof parsed.preparedAt === "string"
        ? { preparedAt: parsed.preparedAt }
        : {}),
    };
  } catch {
    return emptyCache();
  }
}

function writeWelcomeCache(
  storage: Storage,
  agentId: string,
  locale: string,
  cache: WelcomeCache,
): void {
  try {
    storage.setItem(cacheKey(agentId, locale), JSON.stringify(cache));
  } catch {
    // The fixed library continues to work when storage is unavailable.
  }
}

export function recordDisplayedGreeting(
  storage: Storage,
  agentId: string,
  locale: string,
  greetingId: string,
): void {
  const cache = readWelcomeCache(storage, agentId, locale);
  const recentGreetingIds = cache.recentGreetingIds.filter(
    (value) => value !== greetingId,
  );
  recentGreetingIds.push(greetingId);
  writeWelcomeCache(storage, agentId, locale, {
    ...cache,
    recentGreetingIds: recentGreetingIds.slice(-RECENT_LIMIT),
  });
}

export function storePreparedCandidates(
  storage: Storage,
  agentId: string,
  locale: string,
  candidates: AiWelcomeGreeting[],
): void {
  const cache = readWelcomeCache(storage, agentId, locale);
  writeWelcomeCache(storage, agentId, locale, {
    ...cache,
    aiCandidates: candidates.filter(isAiCandidate).slice(0, 8),
    preparedAt: new Date().toISOString(),
  });
}

export function createWelcomeEntryId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const values = new Uint32Array(4);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(values);
  } else {
    for (let index = 0; index < values.length; index += 1) {
      values[index] = Math.floor(Math.random() * 0xffffffff);
    }
  }
  return [...values]
    .map((value) => value.toString(16).padStart(8, "0"))
    .join("");
}
