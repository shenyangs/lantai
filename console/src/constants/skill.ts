// ─── Skill import URL validation ──────────────────────────────────────────────

export const SUPPORTED_SKILL_URL_PREFIXES: string[] = [];

export function isSupportedSkillUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

// ─── Search / filter ──────────────────────────────────────────────────────────

/** Prefix used to distinguish tag-filter tokens from plain text queries */
export const SKILL_TAG_FILTER_PREFIX = "tag:";
