const RETRY_DRAFT_PREFIX = "lantai:retry-draft:";

function buildKey(chatId: string) {
  return `${RETRY_DRAFT_PREFIX}${chatId.trim()}`;
}

export function saveRetryDraft(chatId: string, text: string) {
  const normalizedChatId = chatId.trim();
  const normalizedText = text.trim();
  if (!normalizedChatId || !normalizedText) return;
  window.sessionStorage.setItem(buildKey(normalizedChatId), normalizedText);
}

export function consumeRetryDraft(chatId: string): string {
  const normalizedChatId = chatId.trim();
  if (!normalizedChatId) return "";
  const key = buildKey(normalizedChatId);
  const value = window.sessionStorage.getItem(key) || "";
  window.sessionStorage.removeItem(key);
  return value.trim();
}
