import { kdocsApi } from "../../api/modules/kdocs";
import type {
  KdocsCoverage,
  KdocsDocumentItem,
  KdocsDocumentReadResponse,
  KdocsReadDocumentRequest,
} from "../../api/types/kdocs";
import {
  documentToKdocsReference,
  type KdocsReference,
} from "./kdocsReferences";

type ReadDocument = (
  request: KdocsReadDocumentRequest,
) => Promise<KdocsDocumentReadResponse>;

export interface KdocsReadOptions {
  readDocument?: ReadDocument;
  wait?: (milliseconds: number) => Promise<void>;
  maxPolls?: number;
  maxChars?: number;
  previous?: KdocsReference;
}

const FORMAT_LIMIT_MESSAGE =
  "正文已经可以使用；部分块级结构、精细排版或嵌入内容未展开。需要完整呈现时，请查看在线原文。";
const LENGTH_LIMIT_MESSAGE =
  "正文已经可以使用；文档较长，当前仅保留本次可读取的部分。需要完整内容时，请查看在线原文或重新读取。";

const wait = (milliseconds: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));

export function kdocsErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message.trim().slice(0, 400);
  }
  return "读取金山文档时遇到问题，请稍后重试。";
}

export function kdocsPartialReadMessage({
  truncated = false,
  warning = "",
  coverage,
}: {
  truncated?: boolean;
  warning?: string;
  coverage?: KdocsCoverage;
} = {}): string {
  if (coverage) {
    const messages = [];
    if (truncated || coverage.body === "partial")
      messages.push("正文仅载入部分，当前搜索与引用只覆盖已载入内容。");
    else if (coverage.body === "unknown")
      messages.push("来源未确认正文是否完整。");
    if (coverage.limitations.includes("images"))
      messages.push("图片内容尚未读取，回答不能依据图片中的信息。");
    if (coverage.limitations.includes("formatting"))
      messages.push("正文可使用，精细排版未展开。");
    if (coverage.limitations.includes("unknown"))
      messages.push("来源报告了未明确说明的读取限制。");
    if (messages.length) return messages.join("");
  }
  if (truncated) return LENGTH_LIMIT_MESSAGE;
  const normalized = warning.trim();
  if (!normalized) return FORMAT_LIMIT_MESSAGE;
  if (
    normalized === FORMAT_LIMIT_MESSAGE ||
    normalized === LENGTH_LIMIT_MESSAGE
  ) {
    return normalized;
  }
  const technicalWarning = normalized.toLowerCase();
  if (
    technicalWarning.includes("otl.block_query") ||
    technicalWarning.includes("fine-grained") ||
    technicalWarning.includes("full formatting")
  ) {
    return FORMAT_LIMIT_MESSAGE;
  }
  if (/[\u3400-\u9fff]/u.test(normalized)) {
    return `正文已经可以使用；${normalized}`.slice(0, 400);
  }
  return "正文可使用，但来源未说明读取限制：" + normalized.slice(0, 240);
}

export async function readKdocsDocument(
  document: KdocsDocumentItem,
  options: KdocsReadOptions = {},
): Promise<KdocsReference> {
  const readDocument = options.readDocument ?? kdocsApi.readDocument;
  const pause = options.wait ?? wait;
  const maxPolls = Math.max(1, options.maxPolls ?? 18);
  const maxChars = Math.max(1, options.maxChars ?? 120_000);
  let taskId = "";

  for (let attempt = 0; attempt < maxPolls; attempt += 1) {
    const result = await readDocument({
      file_id: document.file_id,
      format: "markdown",
      max_chars: maxChars,
      ...(options.previous?.continuation
        ? { continuation: options.previous.continuation }
        : {}),
      ...(taskId ? { task_id: taskId } : {}),
    });
    const status = String(result.status || "done").toLowerCase();
    if (status === "pending" || status === "queued" || status === "running") {
      taskId = result.task_id || taskId;
      if (!taskId) {
        throw new Error("金山文档没有返回可继续读取的任务编号。");
      }
      if (attempt === maxPolls - 1) break;
      await pause(Math.min(700 + attempt * 120, 1_800));
      continue;
    }
    if (["failed", "error", "cancelled"].includes(status)) {
      throw new Error("金山文档未能完成正文读取，请重试。");
    }

    const partial = Boolean(
      result.truncated ||
      result.warnings?.length ||
      result.coverage?.limitations.length ||
      result.coverage?.body === "partial" ||
      result.coverage?.body === "unknown",
    );
    return {
      ...documentToKdocsReference(document),
      coverage: result.coverage,
      continuation: result.continuation,
      snapshot_id: result.snapshot_id,
      read_status: partial ? "partial" : "read",
      read_snapshot: options.previous?.read_snapshot ?? {
        ...(document.version ? { version: document.version } : {}),
        ...(document.content_hash
          ? { content_hash: document.content_hash }
          : {}),
        read_at: new Date().toISOString(),
      },
      content_snapshot: {
        text:
          (options.previous?.content_snapshot?.text || "") +
          (result.content || ""),
        format: result.content_format || "markdown",
        truncated: Boolean(result.truncated),
      },
      ...(partial
        ? {
            error_message: kdocsPartialReadMessage({
              truncated: Boolean(result.truncated),
              warning: result.warnings?.join("；") || "",
              coverage: result.coverage,
            }),
          }
        : {}),
    };
  }

  throw new Error("文档仍在准备正文，请稍后重试。");
}

export async function readKdocsDocumentSafely(
  document: KdocsDocumentItem,
  options: KdocsReadOptions = {},
): Promise<KdocsReference> {
  try {
    return await readKdocsDocument(document, options);
  } catch (error) {
    return {
      ...documentToKdocsReference(document),
      read_status: "failed",
      error_message: kdocsErrorMessage(error),
    };
  }
}
