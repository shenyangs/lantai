const STORAGE_KEY = "lantai:daily-brief-return-draft:v1";
const MAX_AGE_MS = 12 * 60 * 60 * 1000;

export interface DailyBriefReturnDraft {
  returnTo: string;
  agentId: string;
  sessionKey: string;
  composerText: string;
  savedAt: number;
}

export function persistDailyBriefReturnDraft(
  draft: Omit<DailyBriefReturnDraft, "savedAt"> & { savedAt?: number },
): void {
  if (typeof window === "undefined") return;
  const payload: DailyBriefReturnDraft = {
    ...draft,
    savedAt: draft.savedAt ?? Date.now(),
  };
  window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

export function consumeDailyBriefReturnDraft({
  returnTo,
  agentId,
  sessionKey,
  now = Date.now(),
}: {
  returnTo: string;
  agentId: string;
  sessionKey: string;
  now?: number;
}): DailyBriefReturnDraft | null {
  if (typeof window === "undefined") return null;
  const raw = window.sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const draft = JSON.parse(raw) as Partial<DailyBriefReturnDraft>;
    if (
      typeof draft.returnTo !== "string" ||
      typeof draft.agentId !== "string" ||
      typeof draft.sessionKey !== "string" ||
      typeof draft.composerText !== "string" ||
      typeof draft.savedAt !== "number" ||
      now - draft.savedAt > MAX_AGE_MS
    ) {
      window.sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
    if (
      draft.returnTo !== returnTo ||
      draft.agentId !== agentId ||
      draft.sessionKey !== sessionKey
    ) {
      return null;
    }
    window.sessionStorage.removeItem(STORAGE_KEY);
    return draft as DailyBriefReturnDraft;
  } catch {
    window.sessionStorage.removeItem(STORAGE_KEY);
    return null;
  }
}

/** Keep the original chat and its unsent input until Chat consumes the draft. */
export function dailyBriefReturnPath(now = Date.now()): string {
  if (typeof window === "undefined") return "/chat";
  try {
    const draft = JSON.parse(
      window.sessionStorage.getItem(STORAGE_KEY) || "null",
    ) as DailyBriefReturnDraft | null;
    return draft &&
      typeof draft.savedAt === "number" &&
      now - draft.savedAt <= MAX_AGE_MS &&
      /^\/chat(?:\/[^?#]*)?(?:\?.*)?$/.test(draft.returnTo)
      ? draft.returnTo
      : "/chat";
  } catch {
    return "/chat";
  }
}
