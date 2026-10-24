export interface ThemeCssCandidate {
  css: string;
  variables: string[];
}

const THEME_VARIABLE_PATTERN = /(--lt-[a-zA-Z0-9_-]+)\s*:\s*[^;{}]+;/g;
const FENCED_CODE_PATTERN = /```([a-zA-Z0-9_-]*)[^\n]*\n([\s\S]*?)```/g;
const THEME_SELECTOR_PATTERN = /(:root|html\s*\[|data-theme|data-palette)/;

function unique(values: string[]) {
  return Array.from(new Set(values));
}

function normalizeCssBlock(css: string) {
  return css.replace(/\r\n/g, "\n").trim();
}

function readThemeVariables(css: string) {
  const variables: string[] = [];
  for (const match of css.matchAll(THEME_VARIABLE_PATTERN)) {
    variables.push(match[1]);
  }
  return unique(variables);
}

function isCssLikeLanguage(language: string) {
  if (!language) return true;
  return ["css", "less", "scss"].includes(language.toLowerCase());
}

export function extractThemeCssCandidates(
  markdown: string,
): ThemeCssCandidate[] {
  const candidates: ThemeCssCandidate[] = [];

  for (const match of markdown.matchAll(FENCED_CODE_PATTERN)) {
    const language = match[1] || "";
    if (!isCssLikeLanguage(language)) continue;

    const css = normalizeCssBlock(match[2] || "");
    if (!css || !THEME_SELECTOR_PATTERN.test(css)) continue;

    const variables = readThemeVariables(css);
    if (variables.length === 0) continue;

    candidates.push({ css, variables });
  }

  return candidates;
}

export function extractLatestThemeCssCandidate(
  markdown: string,
): ThemeCssCandidate | null {
  const candidates = extractThemeCssCandidates(markdown);
  return candidates[candidates.length - 1] ?? null;
}
