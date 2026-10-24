export type ChatRuntimeMessageSource = "canonical-runtime" | "direct";

export const CHAT_RUNTIME_MESSAGE_SOURCE_KEY =
  "lantai.chat.runtime_message_source";

export function resolveChatRuntimeMessageSource(
  storage?: Pick<Storage, "getItem"> | null,
): ChatRuntimeMessageSource {
  let configured: string;
  try {
    configured = String(storage?.getItem(CHAT_RUNTIME_MESSAGE_SOURCE_KEY) || "")
      .trim()
      .toLowerCase();
  } catch {
    configured = "";
  }
  // The model SSE endpoint is the only live transcript writer. Older builds
  // stored `canonical-runtime`; retain the setting key for compatibility, but
  // never let a stale browser preference route visible messages through the
  // delayed Runtime-event ledger again.
  if (configured === "direct") return "direct";
  return "direct";
}
