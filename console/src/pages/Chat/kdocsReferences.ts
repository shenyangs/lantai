import type { KdocsCoverage, KdocsDocumentItem } from "../../api/types/kdocs";

export type KdocsReferenceStatus =
  | "referenced"
  | "reading"
  | "read"
  | "partial"
  | "original_updated"
  | "failed";

export interface KdocsReference {
  schema_version: 1;
  key: string;
  kind: "file" | "excerpt";
  drive_id: string;
  file_id: string;
  name_snapshot: string;
  path_snapshot?: string;
  suffix?: string;
  original_url?: string;
  read_status: KdocsReferenceStatus;
  coverage?: KdocsCoverage;
  continuation?: string;
  snapshot_id?: string;
  read_snapshot?: {
    version?: string;
    content_hash?: string;
    read_at?: string;
  };
  content_snapshot?: {
    text: string;
    format: string;
    truncated: boolean;
  };
  excerpt?: { text: string; location?: string; start?: number; end?: number };
  error_message?: string;
}

const STORAGE_PREFIX = "lantai:kdocs-references:v1";
export const MAX_KDOCS_REFERENCES = 16;

export function kdocsReferenceStorageKey(
  agentId: string,
  sessionId: string,
): string {
  return `${STORAGE_PREFIX}:${encodeURIComponent(agentId || "default")}:${encodeURIComponent(sessionId || "draft")}`;
}

export function documentReferenceKey(driveId: string, fileId: string): string {
  return `${driveId}:${fileId}:file`;
}

export function documentToKdocsReference(
  document: KdocsDocumentItem,
): KdocsReference {
  return {
    schema_version: 1,
    key: documentReferenceKey(document.drive_id, document.file_id),
    kind: "file",
    drive_id: document.drive_id,
    file_id: document.file_id,
    name_snapshot: document.name,
    ...(document.location ? { path_snapshot: document.location } : {}),
    ...(document.suffix ? { suffix: document.suffix } : {}),
    ...(document.original_url ? { original_url: document.original_url } : {}),
    read_status: "referenced",
    ...((document.version || document.content_hash) && {
      read_snapshot: {
        ...(document.version ? { version: document.version } : {}),
        ...(document.content_hash
          ? { content_hash: document.content_hash }
          : {}),
      },
    }),
  };
}

export function kdocsReferenceToDocument(
  reference: KdocsReference,
): KdocsDocumentItem {
  return {
    key: `${reference.drive_id}:${reference.file_id}`,
    drive_id: reference.drive_id,
    file_id: reference.file_id,
    parent_id: "",
    name: reference.name_snapshot,
    type: reference.suffix || "file",
    suffix: reference.suffix || "",
    is_folder: false,
    size: null,
    version: reference.read_snapshot?.version || "",
    content_hash: reference.read_snapshot?.content_hash || "",
    link_id: "",
    original_url: reference.original_url || "",
    location: reference.path_snapshot || "",
    owner: null,
    modifier: null,
    shared: false,
    permissions: { can_read: true },
  };
}

function excerptFingerprint(value: string): string {
  let hash = 0x811c9dc5;
  for (const character of value) {
    hash ^= character.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193);
  }
  return `${(hash >>> 0).toString(16).padStart(8, "0")}-${value.length}`;
}

export function excerptToKdocsReference(
  document: KdocsDocumentItem,
  text: string,
  location = "正文选区",
): KdocsReference {
  const compact = text.trim().slice(0, 8000);
  return {
    ...documentToKdocsReference(document),
    key: `${document.drive_id}:${document.file_id}:excerpt:${location}:${excerptFingerprint(compact)}`,
    kind: "excerpt",
    read_status: "read",
    excerpt: { text: compact, location },
    read_snapshot: {
      ...(document.version ? { version: document.version } : {}),
      ...(document.content_hash ? { content_hash: document.content_hash } : {}),
      read_at: new Date().toISOString(),
    },
  };
}

export function mergeKdocsReference(
  references: KdocsReference[],
  next: KdocsReference,
): KdocsReference[] {
  const existing = references.findIndex((item) => item.key === next.key);
  if (existing >= 0) {
    return references.map((item, index) =>
      index === existing ? { ...item, ...next } : item,
    );
  }
  return [...references, next].slice(-MAX_KDOCS_REFERENCES);
}

function isReference(value: unknown): value is KdocsReference {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Partial<KdocsReference>;
  return Boolean(
    item.schema_version === 1 &&
    item.drive_id &&
    item.file_id &&
    item.name_snapshot &&
    item.key &&
    (item.kind === "file" || item.kind === "excerpt"),
  );
}

export function loadKdocsReferences(
  agentId: string,
  sessionId: string,
  storage: Pick<Storage, "getItem"> = window.sessionStorage,
): KdocsReference[] {
  try {
    const parsed = JSON.parse(
      storage.getItem(kdocsReferenceStorageKey(agentId, sessionId)) || "[]",
    );
    return Array.isArray(parsed)
      ? parsed.filter(isReference).slice(0, MAX_KDOCS_REFERENCES)
      : [];
  } catch {
    return [];
  }
}

export function saveKdocsReferences(
  agentId: string,
  sessionId: string,
  references: KdocsReference[],
  storage: Pick<Storage, "setItem"> = window.sessionStorage,
): void {
  storage.setItem(
    kdocsReferenceStorageKey(agentId, sessionId),
    JSON.stringify(references.slice(0, MAX_KDOCS_REFERENCES)),
  );
}

export function snapshotKdocsReferencesForRequest(
  references: KdocsReference[],
): KdocsReference[] {
  return references.map((reference) => ({
    ...reference,
    ...(reference.coverage
      ? {
          coverage: {
            ...reference.coverage,
            limitations: [...reference.coverage.limitations],
          },
        }
      : {}),
    ...(reference.read_snapshot
      ? { read_snapshot: { ...reference.read_snapshot } }
      : {}),
    ...(reference.content_snapshot
      ? { content_snapshot: { ...reference.content_snapshot } }
      : {}),
    ...(reference.excerpt ? { excerpt: { ...reference.excerpt } } : {}),
  }));
}

export function resolveKdocsSubmitDraft(
  value: string,
  references: KdocsReference[],
): { blocked: boolean; value: string } {
  if (references.some((reference) => reference.read_status === "reading")) {
    return { blocked: true, value };
  }
  if (!value.trim() && references.length > 0) {
    return {
      blocked: false,
      value: "请阅读并处理我引用的金山云文档。",
    };
  }
  return { blocked: false, value };
}

// Match the server's Unicode code-point and aggregate snapshot budgets.
export function kdocsReferenceBudgets(references: KdocsReference[]) {
  let remaining = 320_000;
  return references.map((reference) => {
    const source = Array.from(
      reference.content_snapshot?.text.trim() || "",
    ).length;
    const included = Math.min(source, 120_000, remaining);
    remaining -= included;
    return { source, included };
  });
}
