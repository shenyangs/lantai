export function getDomNodeText(node: unknown): string {
  if (!node || typeof node !== "object") return "";
  const record = node as {
    type?: string;
    data?: string;
    children?: unknown[];
  };

  if (record.type === "text") return record.data || "";
  if (!Array.isArray(record.children)) return "";

  return record.children.map(getDomNodeText).join("");
}

export function normalizeSegmentText(text: string): string {
  return text
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function looksLikeCommand(text: string): boolean {
  return /^(?:\.\/|~\/|\/|npx\b|npm\b|pnpm\b|yarn\b|bun\b|node\b|python3?\b|pip3?\b|uv\b|git\b|gh\b|docker\b|docker-compose\b|curl\b|ssh\b|scp\b|rsync\b|brew\b|make\b|pytest\b|ruff\b|mypy\b|chmod\b|codesign\b|xcrun\b|hdiutil\b|defaults\b|launchctl\b|sqlite3\b|psql\b|mysql\b|open\b)/i.test(
    text.trim(),
  );
}

export function looksLikeReusableLongText(text: string): boolean {
  const value = text.trim();
  if (value.length < 72) return false;

  const asciiCount = Array.from(value).filter(
    (char) => char.charCodeAt(0) < 128,
  ).length;
  const asciiRatio = asciiCount / Math.max(value.length, 1);
  const englishSeparatorCount = (value.match(/[,;]/g) || []).length;
  const promptTerms =
    /prompt|midjourney|stable diffusion|即梦|提示词|反向提示|negative prompt|--ar|--v|style|background|lighting|render|composition/i;

  return (
    promptTerms.test(value) || (asciiRatio > 0.62 && englishSeparatorCount >= 4)
  );
}

export function shouldOfferSegmentCopy(text: string): boolean {
  const value = text.trim();
  if (value.length < 4) return false;
  return looksLikeCommand(value) || value.length >= 24;
}

export function shouldRenderDenseSegment(text: string): boolean {
  return looksLikeCommand(text) || looksLikeReusableLongText(text);
}
