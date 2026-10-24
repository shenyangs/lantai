export type ArtifactWorkbenchView = "files" | "source" | "preview";

export interface ArtifactWorkbenchState {
  openPaths: string[];
  activePath: string | null;
  expanded: string[];
  view: ArtifactWorkbenchView;
  selectedWorkId: string | null;
  selectedPageByWorkId: Record<string, number>;
}

export const ARTIFACT_WORKBENCH_MAX_TABS = 8;
const STORAGE_PREFIX = "lantai:artifact-workbench:v1";

export function emptyArtifactWorkbenchState(): ArtifactWorkbenchState {
  return {
    openPaths: [],
    activePath: null,
    expanded: [],
    view: "files",
    selectedWorkId: null,
    selectedPageByWorkId: {},
  };
}

function sanitizeSelectedPages(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(
        ([workId, page]) =>
          Boolean(workId.trim()) &&
          typeof page === "number" &&
          Number.isSafeInteger(page) &&
          page > 0 &&
          page <= 10_000,
      )
      .slice(-64),
  ) as Record<string, number>;
}

function uniquePaths(values: string[], limit = Number.POSITIVE_INFINITY) {
  const seen = new Set<string>();
  const paths: string[] = [];
  for (const value of values) {
    const path = value.trim();
    if (!path || seen.has(path)) continue;
    seen.add(path);
    paths.push(path);
    if (paths.length >= limit) break;
  }
  return paths;
}

function sanitizeArtifactWorkbenchState(
  value: unknown,
): ArtifactWorkbenchState | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (
    !Array.isArray(record.openPaths) ||
    record.openPaths.some((path) => typeof path !== "string") ||
    !Array.isArray(record.expanded) ||
    record.expanded.some((path) => typeof path !== "string")
  ) {
    return null;
  }
  if (record.activePath !== null && typeof record.activePath !== "string") {
    return null;
  }
  if (
    record.view !== "files" &&
    record.view !== "source" &&
    record.view !== "preview"
  ) {
    return null;
  }

  const openPaths = uniquePaths(
    record.openPaths as string[],
    ARTIFACT_WORKBENCH_MAX_TABS,
  );
  const requestedActive =
    typeof record.activePath === "string" ? record.activePath.trim() : null;
  const activePath =
    requestedActive && openPaths.includes(requestedActive)
      ? requestedActive
      : openPaths[openPaths.length - 1] || null;

  return {
    openPaths,
    activePath,
    expanded: uniquePaths(record.expanded as string[]),
    view: activePath ? record.view : "files",
    selectedWorkId:
      typeof record.selectedWorkId === "string" && record.selectedWorkId.trim()
        ? record.selectedWorkId.trim()
        : null,
    selectedPageByWorkId: sanitizeSelectedPages(record.selectedPageByWorkId),
  };
}

export function artifactWorkbenchStorageKey(chatId: string): string {
  return `${STORAGE_PREFIX}:${chatId}`;
}

export function loadArtifactWorkbenchState(
  chatId?: string,
): ArtifactWorkbenchState {
  if (!chatId?.trim()) return emptyArtifactWorkbenchState();
  const key = artifactWorkbenchStorageKey(chatId);
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return emptyArtifactWorkbenchState();
    const state = sanitizeArtifactWorkbenchState(JSON.parse(raw));
    if (state) return state;
    window.localStorage.removeItem(key);
  } catch {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // A clean in-memory state remains available when storage is blocked.
    }
  }
  return emptyArtifactWorkbenchState();
}

export function saveArtifactWorkbenchState(
  chatId: string | undefined,
  state: ArtifactWorkbenchState,
): void {
  if (!chatId?.trim()) return;
  try {
    window.localStorage.setItem(
      artifactWorkbenchStorageKey(chatId),
      JSON.stringify(state),
    );
  } catch {
    // The workbench stays usable when persistence is unavailable.
  }
}

export function migrateArtifactWorkbenchState(
  temporaryChatId: string,
  canonicalChatId: string,
): boolean {
  const sourceId = temporaryChatId.trim();
  const targetId = canonicalChatId.trim();
  if (!sourceId || !targetId || sourceId === targetId) return false;

  const sourceKey = artifactWorkbenchStorageKey(sourceId);
  const targetKey = artifactWorkbenchStorageKey(targetId);
  let sourceRaw: string | null;
  try {
    sourceRaw = window.localStorage.getItem(sourceKey);
  } catch {
    return false;
  }
  if (sourceRaw === null) return false;

  try {
    if (window.localStorage.getItem(targetKey) !== null) {
      window.localStorage.removeItem(sourceKey);
      return false;
    }
  } catch {
    return false;
  }

  let sourceState: ArtifactWorkbenchState | null = null;
  try {
    sourceState = sanitizeArtifactWorkbenchState(JSON.parse(sourceRaw));
  } catch {
    // A corrupt alias can never be migrated successfully.
  }
  if (!sourceState) {
    try {
      window.localStorage.removeItem(sourceKey);
    } catch {
      // Leave storage untouched when deletion is blocked.
    }
    return false;
  }

  try {
    window.localStorage.setItem(targetKey, JSON.stringify(sourceState));
  } catch {
    // Preserve the source alias until a future resolution can retry safely.
    return false;
  }
  try {
    window.localStorage.removeItem(sourceKey);
  } catch {
    // The canonical copy is durable; a later migration will only clear the alias.
  }
  return true;
}

export function openArtifactTab(
  state: ArtifactWorkbenchState,
  path: string,
  view: ArtifactWorkbenchView = "source",
): ArtifactWorkbenchState {
  const normalized = path.trim();
  if (!normalized) return state;
  if (state.openPaths.includes(normalized)) {
    return { ...state, activePath: normalized, view };
  }
  const openPaths = [...state.openPaths, normalized].slice(
    -ARTIFACT_WORKBENCH_MAX_TABS,
  );
  return { ...state, openPaths, activePath: normalized, view };
}

export function activateArtifactTab(
  state: ArtifactWorkbenchState,
  path: string,
): ArtifactWorkbenchState {
  return state.openPaths.includes(path)
    ? { ...state, activePath: path }
    : state;
}

export function closeArtifactTab(
  state: ArtifactWorkbenchState,
  path: string,
): ArtifactWorkbenchState {
  const index = state.openPaths.indexOf(path);
  if (index < 0) return state;
  const openPaths = state.openPaths.filter((item) => item !== path);
  if (state.activePath !== path) return { ...state, openPaths };
  const activePath = openPaths[Math.min(index, openPaths.length - 1)] || null;
  return {
    ...state,
    openPaths,
    activePath,
    view: activePath ? state.view : "files",
    selectedWorkId: state.selectedWorkId,
    selectedPageByWorkId: state.selectedPageByWorkId,
  };
}

export function reconcileArtifactWorkbenchState(
  state: ArtifactWorkbenchState,
  availableFiles: ReadonlySet<string>,
  availableDirectories: ReadonlySet<string>,
): ArtifactWorkbenchState {
  const openPaths = state.openPaths.filter((path) => availableFiles.has(path));
  const activePath =
    state.activePath && openPaths.includes(state.activePath)
      ? state.activePath
      : openPaths[openPaths.length - 1] || null;
  const expanded = state.expanded.filter((path) =>
    availableDirectories.has(path),
  );
  return {
    openPaths,
    activePath,
    expanded,
    view: activePath ? state.view : "files",
    selectedWorkId: state.selectedWorkId,
    selectedPageByWorkId: state.selectedPageByWorkId,
  };
}
