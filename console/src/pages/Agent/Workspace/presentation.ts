import type {
  WikiPageSummary,
  WikiQueueSummary,
  WikiReviewResponse,
  WikiSourceInfo,
} from "../../../api/types";

export type KnowledgeItemKind = "page" | "source";
export type KnowledgeItemStatus =
  | "ready"
  | "saved"
  | "processing"
  | "attention"
  | "failed";

export interface KnowledgeListItem {
  id: string;
  kind: KnowledgeItemKind;
  title: string;
  description: string;
  path: string;
  updatedAt: number;
  status: KnowledgeItemStatus;
  source?: WikiSourceInfo;
  page?: WikiPageSummary;
}

export interface KnowledgeSummary {
  ready: number;
  saved: number;
  processing: number;
  attention: number;
  failed: number;
  total: number;
}

const PROCESSING_STATUSES = new Set([
  "pending",
  "queued",
  "running",
  "processing",
  "extracting",
  "indexing",
]);

const FAILED_STATUSES = new Set(["failed", "error"]);
const SEARCHABLE_EXTRACTION_STATUSES = new Set([
  "text_extracted",
  "ocr_extracted",
]);

export function knowledgeSourceStatus(
  source: WikiSourceInfo,
): KnowledgeItemStatus {
  const primaryStatuses = [source.status, source.extraction_status]
    .filter(Boolean)
    .map((value) => String(value).toLowerCase());
  const semanticStatus = String(source.semantic_status || "").toLowerCase();

  if (primaryStatuses.some((status) => FAILED_STATUSES.has(status)))
    return "failed";
  if (
    primaryStatuses.some((status) => PROCESSING_STATUSES.has(status)) ||
    PROCESSING_STATUSES.has(semanticStatus)
  ) {
    return "processing";
  }
  if (semanticStatus === "failed") return "attention";
  const searchable =
    source.linked_pages.length > 0 &&
    (Number(source.extracted_chars || 0) > 0 ||
      primaryStatuses.some((status) =>
        SEARCHABLE_EXTRACTION_STATUSES.has(status),
      ) ||
      semanticStatus === "local_structured" ||
      Boolean(source.semantic_summary));
  if (searchable) return "ready";
  if (
    primaryStatuses.some((status) =>
      ["metadata_only", "no_text", "needs_attention", "unsupported"].includes(
        status,
      ),
    )
  ) {
    return "attention";
  }
  return "saved";
}

function sourceDescription(source: WikiSourceInfo): string {
  if (source.semantic_summary) return source.semantic_summary;
  if (source.understanding_summary) return source.understanding_summary;
  if (source.extraction_message) return source.extraction_message;
  if (source.linked_pages.length) {
    return `已整理出 ${source.linked_pages.length} 条可引用内容`;
  }
  return "原始资料已保留，澜台会在需要时从中查找信息";
}

function pageDescription(page: WikiPageSummary): string {
  if (page.summary) return page.summary;
  if (page.category === "queries" || page.page_type === "query") {
    return "从对话中整理并保留的知识";
  }
  return "可在聊天中检索和引用的知识内容";
}

function conciseTitle(value: string, fallback: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) return fallback;
  const sentence = normalized.split(/[。！？!?\n]/, 1)[0]?.trim() || normalized;
  return sentence.length > 48
    ? `${sentence.slice(0, 48).trimEnd()}…`
    : sentence;
}

function pageDisplayTitle(page: WikiPageSummary): string {
  const title = (page.title || page.filename).trim();
  if (/^(?:搜了一下|找到的应该是|可能是这一件)/.test(title)) {
    return conciseTitle(page.summary, page.filename);
  }
  return title;
}

function sourceDisplayTitle(source: WikiSourceInfo): string {
  if (source.display_title?.trim()) return source.display_title.trim();
  const technicalWebName =
    source.kind === "web" &&
    /^(?:www\.)?[a-z0-9.-]+-[a-f0-9]{6,}\.md$/i.test(source.filename);
  if (technicalWebName) {
    return conciseTitle(
      source.semantic_summary || source.understanding_summary || "",
      source.original_path || source.filename,
    );
  }
  return source.filename;
}

export function buildKnowledgeItems(
  pages: WikiPageSummary[],
  sources: WikiSourceInfo[],
): KnowledgeListItem[] {
  const sourceGeneratedPages = new Set(
    sources.flatMap((source) => source.linked_pages),
  );

  const pageItems = pages
    .filter(
      (page) =>
        page.category !== "sources" || !sourceGeneratedPages.has(page.filename),
    )
    .map<KnowledgeListItem>((page) => ({
      id: `page:${page.filename}`,
      kind: "page",
      title: pageDisplayTitle(page),
      description: pageDescription(page),
      path: page.path,
      updatedAt: page.updated_at,
      status: "ready",
      page,
    }));

  const sourceItems = sources.map<KnowledgeListItem>((source) => ({
    id: `source:${source.path}`,
    kind: "source",
    title: sourceDisplayTitle(source),
    description: sourceDescription(source),
    path: source.path,
    updatedAt: source.updated_at,
    status: knowledgeSourceStatus(source),
    source,
  }));

  return [...pageItems, ...sourceItems].sort(
    (left, right) => right.updatedAt - left.updatedAt,
  );
}

export function summarizeKnowledge(
  items: KnowledgeListItem[],
  queue: WikiQueueSummary,
  review: WikiReviewResponse | null,
): KnowledgeSummary {
  const summary: KnowledgeSummary = {
    ready: 0,
    saved: 0,
    processing: 0,
    attention: review?.stats?.pending ?? 0,
    failed: 0,
    total: items.length,
  };

  for (const item of items) {
    summary[item.status] += 1;
  }

  // Queue activity can exist before a source appears in the catalog. Avoid
  // double-counting source rows that already expose a processing state.
  summary.processing = Math.max(
    summary.processing,
    queue.pending + queue.running,
  );
  summary.failed = Math.max(summary.failed, queue.failed);
  return summary;
}

export function knowledgeKindLabel(item: KnowledgeListItem): string {
  if (item.kind === "source") {
    if (item.source?.kind === "web") return "网页";
    return "资料";
  }
  if (item.page?.category === "queries" || item.page?.page_type === "query") {
    return "对话整理";
  }
  return "知识";
}

export function knowledgeStatusLabel(status: KnowledgeItemStatus): string {
  if (status === "saved") return "已保存，待整理";
  if (status === "processing") return "正在整理";
  if (status === "attention") return "需要确认";
  if (status === "failed") return "整理失败";
  return "已可用";
}
