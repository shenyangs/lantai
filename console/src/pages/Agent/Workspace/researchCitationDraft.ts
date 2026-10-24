import type { ReportClaim } from "@/api/modules/research";

// Browser selections use UTF-16; persisted research anchors use Unicode codepoints.
export const codepointSlice = (text: string, start: number, end?: number) => Array.from(text).slice(start, end).join("");
export const selectionSpan = (text: string, start: number, end: number) => ({
  start: Array.from(text.slice(0, start)).length,
  end: Array.from(text.slice(0, end)).length,
});

export function rebaseClaims(before: string, after: string, claims: ReportClaim[]): ReportClaim[] {
  if (before === after) return claims;
  const old = Array.from(before), next = Array.from(after);
  let prefix = 0, suffix = 0;
  while (prefix < old.length && prefix < next.length && old[prefix] === next[prefix]) prefix++;
  while (suffix < old.length - prefix && suffix < next.length - prefix && old[old.length - 1 - suffix] === next[next.length - 1 - suffix]) suffix++;
  const end = old.length - suffix, delta = next.length - old.length;
  // Keep only anchors wholly outside the changed range. This preserves provenance,
  // never a review result: every saved version still requires a fresh review.
  return claims.flatMap((claim) => {
    if (claim.end <= prefix) return [claim];
    if (claim.start >= end) return [{ ...claim, start: claim.start + delta, end: claim.end + delta }];
    return [];
  });
}
