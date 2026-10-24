import { useCallback, useEffect, useRef, useState } from "react";
import { setTextareaValue } from "../utils";

const MAX_CAPTURED_DRAFTS = 3;
const DEDUP_WINDOW_MS = 5_000;

export interface PreviewFixDraft {
  prompt: string;
  summary: string;
  source: string;
  count: number;
}

interface PreviewBackfeedMessage {
  type?: string;
  source?: string;
  summary?: string;
  message?: string;
  detail?: string;
  stack?: string;
  componentStack?: string;
  consoleErrors?: unknown;
  networkErrors?: unknown;
  url?: string;
}

interface UsePreviewErrorBackfeedOptions {
  chatId?: string | null;
  getTextarea: () => HTMLTextAreaElement | null;
  onInfo?: (message: string) => void;
  onWarning?: (message: string) => void;
}

function sanitize(value: unknown, limit = 1200): string {
  return String(value || "")
    .replace(/\bsk-[A-Za-z0-9_-]{16,}\b/g, "[redacted_key]")
    .replace(/\b[A-Za-z0-9_-]{32,}\b/g, "[redacted_id]")
    .trim()
    .slice(0, limit);
}

export function isPreviewErrorType(type: unknown): boolean {
  return (
    type === "preview:error" ||
    type === "preview-error" ||
    type === "preview:build:error" ||
    type === "preview:runtime:error"
  );
}

export function isAllowedPreviewOrigin(origin: string): boolean {
  if (!origin || origin === "null") return true;
  try {
    const url = new URL(origin);
    if (url.protocol === "file:") return true;
    if (url.origin === window.location.origin) return true;
    return ["localhost", "127.0.0.1", "::1"].includes(url.hostname);
  } catch {
    return false;
  }
}

export function buildPreviewFixPrompt(input: {
  source: string;
  summary: string;
  detail?: string;
  url?: string;
}): string {
  return [
    "预览页面报错了，请定位并修复：",
    "",
    `来源：${input.source}`,
    input.url ? `URL：${input.url}` : "",
    input.summary,
    input.detail ? ["", input.detail].join("\n") : "",
    "",
    "请优先检查 dev server 是否仍在运行、端口是否一致、浏览器控制台首个错误和失败的网络请求；修复后重新打开预览验证。",
  ]
    .filter(Boolean)
    .join("\n");
}

function serializePreviewDetail(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function draftFromMessage(
  data: PreviewBackfeedMessage,
): Omit<PreviewFixDraft, "count"> | null {
  if (!isPreviewErrorType(data.type)) return null;
  const summary = sanitize(data.summary || data.message);
  if (!summary) return null;
  const source = sanitize(data.source || data.type || "preview", 120);
  const detail = sanitize(
    [
      data.stack,
      data.componentStack,
      data.detail,
      serializePreviewDetail(data.consoleErrors),
      serializePreviewDetail(data.networkErrors),
    ]
      .filter(Boolean)
      .join("\n\n"),
    1800,
  );
  const url = sanitize(data.url, 400);
  return {
    source,
    summary,
    prompt: buildPreviewFixPrompt({ source, summary, detail, url }),
  };
}

function fingerprint(draft: Omit<PreviewFixDraft, "count">): string {
  return `${draft.source}|${draft.summary.slice(0, 220)}`;
}

export function usePreviewErrorBackfeed({
  chatId,
  getTextarea,
  onInfo,
  onWarning,
}: UsePreviewErrorBackfeedOptions) {
  const [draft, setDraft] = useState<PreviewFixDraft | null>(null);
  const draftCountRef = useRef(0);
  const lastRef = useRef<{ fp: string; at: number } | null>(null);

  const applyDraft = useCallback(
    (targetDraft: PreviewFixDraft | null = draft) => {
      if (!targetDraft) return false;
      const textarea = getTextarea();
      if (!textarea) return false;
      setTextareaValue(textarea, targetDraft.prompt);
      textarea.focus();
      setDraft(null);
      onInfo?.("已把预览错误回填到输入框，确认后可让 Agent 修复。");
      return true;
    },
    [draft, getTextarea, onInfo],
  );

  const dismissDraft = useCallback(() => {
    setDraft(null);
  }, []);

  useEffect(() => {
    draftCountRef.current = 0;
    lastRef.current = null;
    setDraft(null);
  }, [chatId]);

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (!isAllowedPreviewOrigin(event.origin)) return;
      if (!event.data || typeof event.data !== "object") return;

      const next = draftFromMessage(event.data as PreviewBackfeedMessage);
      if (!next) return;

      const fp = fingerprint(next);
      const now = Date.now();
      const last = lastRef.current;
      if (last && last.fp === fp && now - last.at < DEDUP_WINDOW_MS) return;
      lastRef.current = { fp, at: now };

      if (draftCountRef.current >= MAX_CAPTURED_DRAFTS) {
        onWarning?.(`已捕获 ${MAX_CAPTURED_DRAFTS} 次预览错误，请先手动检查。`);
        return;
      }

      draftCountRef.current += 1;
      const countedDraft = { ...next, count: draftCountRef.current };
      setDraft(countedDraft);
      onInfo?.("已捕获预览错误；需要 Agent 处理时，可手动回填输入框。");
    };

    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [onInfo, onWarning]);

  return {
    draft,
    applyDraft,
    dismissDraft,
    maxCapturedDrafts: MAX_CAPTURED_DRAFTS,
  };
}
