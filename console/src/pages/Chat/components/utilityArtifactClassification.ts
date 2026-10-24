import { utilityIntelligenceApi } from "@/api";
import type { UtilityArtifactClassificationResponse } from "@/api/types";

const MAX_CACHE_ENTRIES = 128;
const PERSISTED_CACHE_STORAGE_KEY = "lantai:utility-output-presentation:v2";
const PERSISTED_CACHE_VERSION = 2;

interface CachedClassification {
  promise: Promise<UtilityArtifactClassificationResponse>;
}

interface PersistedClassification {
  fingerprint: string;
  result: UtilityArtifactClassificationResponse;
  updatedAt: number;
}

interface PersistedClassificationStore {
  version: typeof PERSISTED_CACHE_VERSION;
  records: Record<string, PersistedClassification>;
}

const classificationCache = new Map<string, CachedClassification>();

function deleteIfCurrent(text: string, entry: CachedClassification): void {
  if (classificationCache.get(text) === entry) {
    classificationCache.delete(text);
  }
}

function trimClassificationCache(): void {
  while (classificationCache.size > MAX_CACHE_ENTRIES) {
    const oldestKey = classificationCache.keys().next().value;
    if (typeof oldestKey !== "string") return;
    classificationCache.delete(oldestKey);
  }
}

function fingerprintText(text: string): string {
  let first = 2166136261;
  let second = 5381;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    first = Math.imul(first ^ code, 16777619);
    second = Math.imul(second, 33) ^ code;
  }
  return `${text.length}:${(first >>> 0).toString(36)}:${(
    second >>> 0
  ).toString(36)}`;
}

function isClassifiedResult(
  value: unknown,
): value is UtilityArtifactClassificationResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<UtilityArtifactClassificationResponse>;
  const advisory = candidate.advisory;
  const advisoryIsValid =
    advisory == null ||
    (typeof advisory === "object" &&
      (advisory.label === "注意" || advisory.label === "提示") &&
      typeof advisory.body === "string");
  return (
    candidate.status === "classified" &&
    typeof candidate.model === "string" &&
    Array.isArray(candidate.artifacts) &&
    advisoryIsValid
  );
}

function getLocalStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function emptyPersistedStore(): PersistedClassificationStore {
  return {
    version: PERSISTED_CACHE_VERSION,
    records: {},
  };
}

function readPersistedStore(): PersistedClassificationStore {
  const storage = getLocalStorage();
  if (!storage) return emptyPersistedStore();
  try {
    const raw = storage.getItem(PERSISTED_CACHE_STORAGE_KEY);
    if (!raw) return emptyPersistedStore();
    const parsed = JSON.parse(raw) as Partial<PersistedClassificationStore>;
    if (
      parsed.version !== PERSISTED_CACHE_VERSION ||
      !parsed.records ||
      typeof parsed.records !== "object"
    ) {
      return emptyPersistedStore();
    }
    return {
      version: PERSISTED_CACHE_VERSION,
      records: parsed.records,
    };
  } catch {
    return emptyPersistedStore();
  }
}

function newestPersistedRecords(
  records: Record<string, PersistedClassification>,
  limit: number,
): Record<string, PersistedClassification> {
  return Object.fromEntries(
    Object.entries(records)
      .filter(([, record]) => {
        return (
          record &&
          typeof record.fingerprint === "string" &&
          typeof record.updatedAt === "number" &&
          isClassifiedResult(record.result)
        );
      })
      .sort((left, right) => right[1].updatedAt - left[1].updatedAt)
      .slice(0, limit),
  );
}

function writePersistedStore(store: PersistedClassificationStore): void {
  const storage = getLocalStorage();
  if (!storage) return;

  const boundedStore: PersistedClassificationStore = {
    version: PERSISTED_CACHE_VERSION,
    records: newestPersistedRecords(store.records, MAX_CACHE_ENTRIES),
  };
  try {
    storage.setItem(PERSISTED_CACHE_STORAGE_KEY, JSON.stringify(boundedStore));
  } catch {
    try {
      storage.setItem(
        PERSISTED_CACHE_STORAGE_KEY,
        JSON.stringify({
          version: PERSISTED_CACHE_VERSION,
          records: newestPersistedRecords(
            boundedStore.records,
            Math.max(1, Math.floor(MAX_CACHE_ENTRIES / 4)),
          ),
        } satisfies PersistedClassificationStore),
      );
    } catch {
      // Private browsing and storage quotas may disable persistence.
    }
  }
}

function persistClassification(
  classificationKey: string,
  text: string,
  result: UtilityArtifactClassificationResponse,
): void {
  if (!classificationKey || !isClassifiedResult(result)) return;
  const store = readPersistedStore();
  store.records[classificationKey] = {
    fingerprint: fingerprintText(text),
    result,
    updatedAt: Date.now(),
  };
  writePersistedStore(store);
}

export function getPersistedUtilityArtifactClassification(
  classificationKey: string,
  text: string,
): UtilityArtifactClassificationResponse | null {
  if (!classificationKey || !text) return null;
  const record = readPersistedStore().records[classificationKey];
  if (
    !record ||
    record.fingerprint !== fingerprintText(text) ||
    !isClassifiedResult(record.result)
  ) {
    return null;
  }
  return record.result;
}

export function shouldClassifyUtilityArtifacts(text: string): boolean {
  // This is a structural empty guard, not semantic classification. The
  // auxiliary model decides whether non-empty assistant text contains a work.
  return text.trim().length > 0;
}

export function classifyUtilityArtifactsCached(
  text: string,
  classificationKey?: string,
  signal?: AbortSignal,
): Promise<UtilityArtifactClassificationResponse> {
  if (classificationKey) {
    const persisted = getPersistedUtilityArtifactClassification(
      classificationKey,
      text,
    );
    if (persisted) return Promise.resolve(persisted);
  }

  const existing = classificationCache.get(text);
  if (existing) {
    classificationCache.delete(text);
    classificationCache.set(text, existing);
    return existing.promise.then((result) => {
      if (classificationKey) {
        persistClassification(classificationKey, text, result);
      }
      return result;
    });
  }

  const entry: CachedClassification = {
    promise: Promise.resolve({
      status: "failed",
      model: "",
      artifacts: [],
    }),
  };
  entry.promise = utilityIntelligenceApi
    .classifyUtilityArtifacts(text, signal)
    .then((result) => {
      if (result.status !== "classified") {
        deleteIfCurrent(text, entry);
      }
      return result;
    })
    .catch((error) => {
      deleteIfCurrent(text, entry);
      throw error;
    });

  classificationCache.set(text, entry);
  trimClassificationCache();
  return entry.promise.then((result) => {
    if (classificationKey) {
      persistClassification(classificationKey, text, result);
    }
    return result;
  });
}

export function clearUtilityArtifactClassificationMemoryCache(): void {
  classificationCache.clear();
}

export function clearUtilityArtifactClassificationCache(): void {
  clearUtilityArtifactClassificationMemoryCache();
  const storage = getLocalStorage();
  if (!storage) return;
  try {
    storage.removeItem(PERSISTED_CACHE_STORAGE_KEY);
  } catch {
    // Storage is optional; the in-memory cache is already clear.
  }
}
