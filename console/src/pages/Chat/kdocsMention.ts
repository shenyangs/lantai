export interface KdocsMentionMatch {
  start: number;
  end: number;
  query: string;
}

const MENTION_PATTERN = /(^|[\s({"'，。！？、；：]|\[)@([^@\n]{0,60})$/u;

export function matchKdocsMention(
  value: string,
  cursor: number,
): KdocsMentionMatch | null {
  const safeCursor = Math.max(0, Math.min(cursor, value.length));
  const beforeCursor = value.slice(0, safeCursor);
  const match = beforeCursor.match(MENTION_PATTERN);
  if (!match || match.index === undefined) return null;
  const boundary = match[1] || "";
  const query = match[2] || "";
  const start = match.index + boundary.length;
  return { start, end: safeCursor, query };
}

export function replaceKdocsMention(
  value: string,
  match: KdocsMentionMatch,
  documentName: string,
): { value: string; cursor: number } {
  const mention = `@《${documentName.trim() || "未命名文档"}》 `;
  const next = `${value.slice(0, match.start)}${mention}${value.slice(match.end)}`;
  return { value: next, cursor: match.start + mention.length };
}
