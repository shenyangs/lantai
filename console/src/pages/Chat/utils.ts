// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
import { chatApi } from "../../api/modules/chat";
import { stripQuquFromArtifactContent } from "./ququ";
export const ATTACHMENT_ONLY_SUBMIT_MARKER = "\u200B";

export type CopyableContent = {
  type?: string;
  text?: string;
  refusal?: string;
};

export type CopyableMessage = {
  type?: string;
  role?: string;
  content?: string | CopyableContent[];
};

export type CopyableResponse = {
  output?: CopyableMessage[];
};

export type RuntimeLoadingBridgeApi = {
  getLoading?: () => boolean | string;
  setLoading?: (loading: boolean | string) => void;
};

/** Returns true when id is a pure numeric local timestamp placeholder. */
export function isLocalTimestampSessionId(id: string | null | undefined): boolean {
  return /^\d+$/.test(String(id || "").trim());
}

/**
 * Pick the first usable backend chat id and ignore local timestamp placeholders.
 */
export function resolveStableChatId(
  ...candidates: Array<string | null | undefined>
): string {
  for (const candidate of candidates) {
    const value = String(candidate || "").trim();
    if (!value || isLocalTimestampSessionId(value)) continue;
    return value;
  }
  return "";
}

// ---------------------------------------------------------------------------
// Text extraction utilities
// ---------------------------------------------------------------------------

/** Remove the invisible marker used to enable attachment-only submits. */
export function stripAttachmentSubmitMarker(text: string): string {
  return text.split(ATTACHMENT_ONLY_SUBMIT_MARKER).join("");
}

/** Extract copyable text from assistant response. */
export function extractCopyableText(response: CopyableResponse): string {
  const collectText = (assistantOnly: boolean) => {
    const chunks = (response.output || []).flatMap((item: CopyableMessage) => {
      if (assistantOnly && item.role !== "assistant") return [];
      if (item.type && item.type !== "message") return [];

      if (typeof item.content === "string") {
        return [stripQuquFromArtifactContent(item.content)];
      }

      if (!Array.isArray(item.content)) {
        return [];
      }

      return item.content.flatMap((content: CopyableContent) => {
        if (content.type === "text" && typeof content.text === "string") {
          return [stripQuquFromArtifactContent(content.text)];
        }

        if (content.type === "refusal" && typeof content.refusal === "string") {
          return [content.refusal];
        }

        return [];
      });
    });

    return chunks.filter(Boolean).join("\n\n").trim();
  };

  return collectText(true);
}

/** Extract plain text from user message content. */
export function extractUserMessageText(m: any): string {
  if (typeof m.content === "string")
    return stripAttachmentSubmitMarker(m.content);
  if (!Array.isArray(m.content)) return "";
  return m.content
    .filter((p: any) => p.type === "text")
    .map((p: any) =>
      typeof p.text === "string" ? stripAttachmentSubmitMarker(p.text) : "",
    )
    .join("\n");
}

export function extractTextFromMessage(msg: any): string {
  const innerMessage = msg?.cards?.[0]?.data?.input?.[0];
  if (!innerMessage) return "";
  return extractUserMessageText(innerMessage);
}

// ---------------------------------------------------------------------------
// Clipboard utilities
// ---------------------------------------------------------------------------

type ClipboardDataLike = {
  getData: (type: string) => string;
};

/** Copy text to clipboard with fallback for non-secure contexts. */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "absolute";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);

  let copied: boolean;
  try {
    textarea.focus();
    textarea.select();
    copied = document.execCommand("copy");
  } finally {
    document.body.removeChild(textarea);
  }

  if (!copied) {
    throw new Error("Failed to copy text");
  }
}

const BLOCK_HTML_TAGS = new Set([
  "address",
  "article",
  "aside",
  "blockquote",
  "dd",
  "details",
  "dialog",
  "div",
  "dl",
  "dt",
  "figcaption",
  "figure",
  "footer",
  "form",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "header",
  "hr",
  "li",
  "main",
  "nav",
  "ol",
  "p",
  "pre",
  "section",
  "table",
  "tbody",
  "tfoot",
  "thead",
  "tr",
  "ul",
]);

function appendClipboardText(parts: string[], text: string) {
  const normalized = text.replace(/\u00a0/g, " ").replace(/[ \t\f\v]+/g, " ");
  if (normalized) parts.push(normalized);
}

function appendClipboardNewline(parts: string[]) {
  if (parts.length === 0) return;
  const last = parts[parts.length - 1];
  if (last.endsWith("\n")) return;
  parts.push("\n");
}

function normalizeClipboardLayoutText(text: string): string {
  return text
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .split("\n")
    .map((line) => line.replace(/[ \t]+$/g, "").trimStart())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function countLineBreaks(text: string): number {
  return (text.match(/\n/g) || []).length;
}

/** Convert rich clipboard HTML into plain text while preserving block layout. */
export function htmlToPlainTextWithLayout(html: string): string {
  if (!html.trim() || typeof DOMParser === "undefined") return "";

  const document = new DOMParser().parseFromString(html, "text/html");
  const parts: string[] = [];

  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      appendClipboardText(parts, node.textContent || "");
      return;
    }

    if (node.nodeType !== Node.ELEMENT_NODE) {
      Array.from(node.childNodes).forEach(walk);
      return;
    }

    const element = node as Element;
    const tag = element.tagName.toLowerCase();

    if (tag === "br") {
      appendClipboardNewline(parts);
      return;
    }

    const isBlock = BLOCK_HTML_TAGS.has(tag);
    if (isBlock) appendClipboardNewline(parts);
    if (tag === "li") parts.push("- ");

    Array.from(element.childNodes).forEach(walk);

    if (tag === "td" || tag === "th") {
      parts.push("\t");
      return;
    }

    if (isBlock) appendClipboardNewline(parts);
  };

  Array.from(document.body.childNodes).forEach(walk);
  return normalizeClipboardLayoutText(parts.join(""));
}

/**
 * Choose the best plain text representation from clipboard data.
 * Native plain text wins when it already contains layout; HTML is used only
 * when the plain text has been collapsed by the source app.
 */
export function extractClipboardTextWithLayout(
  clipboardData: ClipboardDataLike | null | undefined,
): string {
  if (!clipboardData) return "";
  const plain = clipboardData.getData("text/plain") || "";
  const html = clipboardData.getData("text/html") || "";
  if (!html.trim()) return plain;

  const htmlText = htmlToPlainTextWithLayout(html);
  if (!htmlText) return plain;
  if (!plain.trim()) return htmlText;
  if (countLineBreaks(plain) >= countLineBreaks(htmlText)) return plain;
  return htmlText;
}

// ---------------------------------------------------------------------------
// Error response utilities
// ---------------------------------------------------------------------------

export function buildFailedChatPayload({
  errorCode,
  message,
}: {
  errorCode: string;
  message: string;
}): Record<string, unknown> {
  const nowMs = Date.now();
  const now = Math.floor(nowMs / 1000);
  return {
    id: `response_error_${nowMs}`,
    object: "response",
    created_at: now,
    status: "failed",
    type: "response.failed",
    output: [],
    error: {
      code: errorCode,
      message,
    },
    message,
    usage: null,
  };
}

export function buildFailedChatResponse({
  statusCode,
  errorCode,
  message,
}: {
  statusCode: number;
  errorCode: string;
  message: string;
}): Response {
  return new Response(
    JSON.stringify(buildFailedChatPayload({ errorCode, message })),
    {
      status: statusCode,
      headers: { "Content-Type": "application/json" },
    },
  );
}

/** Build a 400 error response when model is not configured. */
export function buildModelError(): Response {
  return buildFailedChatResponse({
    statusCode: 400,
    errorCode: "Model not configured",
    message: "Please configure a model first",
  });
}

// ---------------------------------------------------------------------------
// URL normalization utilities
// ---------------------------------------------------------------------------

/** Decode each path segment; keeps `/` delimiters (including repeated `/`). */
function decodeUriPathSegments(path: string): string {
  return path
    .split("/")
    .map((segment) => {
      if (!segment) return segment;
      try {
        return decodeURIComponent(segment);
      } catch {
        return segment;
      }
    })
    .join("/");
}

/** Convert file URL to stored path for backend: keep full path after `/files/preview/`. */
export function toStoredName(v: string): string {
  const marker = "/files/preview/";
  const idx = v.indexOf(marker);
  if (idx !== -1) {
    let rest = v.slice(idx + marker.length);
    const q = rest.indexOf("?");
    if (q !== -1) rest = rest.slice(0, q);
    const h = rest.indexOf("#");
    if (h !== -1) rest = rest.slice(0, h);
    if (rest) {
      const decoded = decodeUriPathSegments(rest);
      // Windows absolute path: C:\... or C:/...
      const isWindowsAbsolute = /^[a-zA-Z]:[\\/]/.test(decoded);
      if (isWindowsAbsolute) return decoded;
      return decoded.startsWith("/") ? decoded : `/${decoded}`;
    }
  }
  return v;
}

/** Convert content part URLs to stored name format. */
export function normalizeContentUrls(part: any): any {
  const p = { ...part };
  if (p.type === "image" && typeof p.image_url === "string")
    p.image_url = toStoredName(p.image_url);
  if (p.type === "file" && typeof p.file_url === "string")
    p.file_url = toStoredName(p.file_url);
  if (p.type === "audio" && typeof p.data === "string")
    p.data = toStoredName(p.data);
  if (p.type === "video" && typeof p.video_url === "string")
    p.video_url = toStoredName(p.video_url);
  return p;
}

/** Turn a backend content URL (path or full URL) into a full URL for display. */
export function toDisplayUrl(url: string | undefined): string {
  if (!url) return "";
  if (
    url.startsWith("http://") ||
    url.startsWith("https://") ||
    url.startsWith("data:") ||
    url.startsWith("blob:")
  ) {
    return url;
  }
  if (url.startsWith("file://")) url = url.replace("file://", "");
  return chatApi.filePreviewUrl(url.startsWith("/") ? url : `/${url}`);
}

// ---------------------------------------------------------------------------
// DOM utilities
// ---------------------------------------------------------------------------

/** Set textarea value and trigger input event for React state sync.
 * Uses native value setter to bypass React's internal value tracker.
 */
export function setTextareaValue(
  textarea: HTMLTextAreaElement,
  value: string,
  selectionStart = value.length,
  selectionEnd = selectionStart,
) {
  const nativeValueSetter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  if (nativeValueSetter) {
    nativeValueSetter.call(textarea, value);
  } else {
    textarea.value = value;
  }
  textarea.selectionStart = selectionStart;
  textarea.selectionEnd = selectionEnd;
  const event = new Event("input", { bubbles: true });
  textarea.dispatchEvent(event);
}
