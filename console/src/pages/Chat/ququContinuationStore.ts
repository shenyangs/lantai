import { useEffect, useState, useSyncExternalStore } from "react";

import type { QuquContinuationRecord } from "@/api/types";

export interface QuquContinuationLocator {
  chat_id: string;
  source_message_id: string;
  source_message_digest: string;
  ququ_ordinal: number;
  placement: "lead" | "echo";
}

type ContinuationSnapshot = ReadonlyMap<string, QuquContinuationRecord>;

const listeners = new Set<() => void>();
let snapshot: ContinuationSnapshot = new Map();

function normalizeSourceMessageId(value: string): string {
  return String(value || "")
    .trim()
    .replace(/(?::(?:message|reasoning))+$/i, "");
}

export function ququContinuationKey(locator: QuquContinuationLocator): string {
  return JSON.stringify([
    String(locator.chat_id || "").trim(),
    normalizeSourceMessageId(locator.source_message_id),
    String(locator.source_message_digest || "").trim().toLowerCase(),
    Number(locator.ququ_ordinal),
    locator.placement,
  ]);
}

export async function digestQuquSourceMessage(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return "";
  const payload = new TextEncoder().encode(JSON.stringify([String(text || "")]));
  const digest = await subtle.digest("SHA-256", payload);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function useQuquSourceMessageDigest(text: string): string {
  const [resolved, setResolved] = useState({ text: "", digest: "" });

  useEffect(() => {
    let active = true;
    void digestQuquSourceMessage(text).then((digest) => {
      if (active) setResolved({ text, digest });
    });
    return () => {
      active = false;
    };
  }, [text]);

  return resolved.text === text ? resolved.digest : "";
}

function emit(next: Map<string, QuquContinuationRecord>) {
  snapshot = next;
  listeners.forEach((listener) => listener());
}

function isNewerOrSame(
  incoming: QuquContinuationRecord,
  current: QuquContinuationRecord,
): boolean {
  if (incoming.continuation_id === current.continuation_id) {
    return String(incoming.updated_at || "") >= String(current.updated_at || "");
  }
  return String(incoming.updated_at || "") > String(current.updated_at || "");
}

export function upsertQuquContinuation(record: QuquContinuationRecord): void {
  if (record.status !== "completed" || !record.text?.trim()) return;
  const key = ququContinuationKey(record);
  const current = snapshot.get(key);
  if (current && !isNewerOrSame(record, current)) return;
  const next = new Map(snapshot);
  next.set(key, { ...record, text: record.text.trim() });
  emit(next);
}

export function hydrateQuquContinuations(
  records: readonly QuquContinuationRecord[] | null | undefined,
): void {
  if (!Array.isArray(records)) return;
  records.forEach(upsertQuquContinuation);
}

export function getQuquContinuation(
  locator: QuquContinuationLocator,
): QuquContinuationRecord | undefined {
  return snapshot.get(ququContinuationKey(locator));
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useQuquContinuation(
  locator: QuquContinuationLocator,
): QuquContinuationRecord | undefined {
  const key = ququContinuationKey(locator);
  const current = useSyncExternalStore(
    subscribe,
    () => snapshot,
    () => snapshot,
  );
  return current.get(key);
}

export function resetQuquContinuationStoreForTests(): void {
  emit(new Map());
}
