import type { ChatHistory } from "../../api/types/chat";

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function pushUnique(target: string[], value: string) {
  if (!value || target.includes(value)) return;
  target.push(value);
}

export interface CollaborationDraftAnchor {
  runId: string;
  responseId?: string;
}

function collectMetadataRecords(value: unknown): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = [];
  const queue: unknown[] = [value];
  const seen = new Set<object>();
  while (queue.length > 0) {
    const record = asRecord(queue.shift());
    if (!record || seen.has(record)) continue;
    seen.add(record);
    records.push(record);
    queue.push(record.metadata, record.meta, record.runtime);
    if (Array.isArray(record.output)) queue.push(...record.output);
  }
  return records;
}

export function resolveCollaborationDraftAnchor(
  response: unknown,
): CollaborationDraftAnchor | null {
  const root = asRecord(response);
  const responseId = asString(root?.id);
  const keys = [
    "native_runtime_turn_id",
    "runtime_turn_id",
    "turn_id",
    "run_id",
    "last_turn_run_id",
    "client_turn_id",
  ];
  for (const record of collectMetadataRecords(response)) {
    for (const key of keys) {
      const raw = asString(record[key]);
      if (!raw) continue;
      return {
        runId: raw.startsWith("experience:") ? raw.slice(11) : raw,
        responseId: responseId || undefined,
      };
    }
  }
  return null;
}

export function buildCapabilityDraftTaskRunIds(
  activeChatId: string,
  history?: Pick<ChatHistory, "chat_id" | "meta"> | null,
): string[] {
  const ids: string[] = [];
  const meta = asRecord(history?.meta);
  const lastTurnRunId = asString(meta?.last_turn_run_id);
  if (lastTurnRunId) pushUnique(ids, `experience:${lastTurnRunId}`);

  const pendingTurn = asRecord(meta?.pending_turn);
  const pendingRunId = asString(pendingTurn?.run_id);
  if (pendingRunId) pushUnique(ids, `experience:${pendingRunId}`);

  const chatId = asString(history?.chat_id) || activeChatId.trim();
  if (chatId) {
    pushUnique(
      ids,
      chatId.startsWith("chat:") || chatId.startsWith("experience:")
        ? chatId
        : `chat:${chatId}`,
    );
  }
  return ids;
}
