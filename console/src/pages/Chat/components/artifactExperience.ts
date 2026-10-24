import type { Message } from "../../../api/types/chat";
import type { WorkItem } from "../../../api/types/works";
import {
  isLantaiAttachmentRole,
  parseAttachmentRoleContext,
  type LantaiAttachmentRole,
} from "../../../vendor/lantaiAttachmentRoles";

export type ArtifactSelectionReason =
  "user-selection" | "current-run" | "single-lineage" | "ambiguous" | "none";

export interface ArtifactOutcomeSelection {
  primary: WorkItem | null;
  alternatives: WorkItem[];
  reason: ArtifactSelectionReason;
}

export interface ArtifactRuntimeProjection {
  status?: string | null;
  run_id?: string | null;
  outcome_status?: string | null;
  delivery_status?: string | null;
}

export interface ArtifactOutcomeProjection {
  availability: NonNullable<WorkItem["availability_status"]>;
  preview: "idle" | "loading" | "ready" | "unsupported" | "error";
  validation: WorkItem["validation"] | null;
  delivery: string;
  execution: string;
}

export interface ArtifactEditTarget {
  schema_version: 1;
  chat_id: string;
  asset_id: string;
  work_id: string;
  edit_path: string;
  base_revision: {
    version: number;
    digest: string | null;
  };
  selection?:
    | { kind: "page"; page: number }
    | { kind: "paragraph"; paragraph: number }
    | { kind: "region"; region: string };
}

export interface CanonicalAttachmentProjection {
  role: LantaiAttachmentRole;
  displayName: string;
}

const STRUCTURED_ATTACHMENT_CONTENT_TYPES = new Set([
  "file",
  "image",
  "video",
  "audio",
]);

function userMessageTextParts(message: Message): string[] {
  if (message.role !== "user") return [];
  if (typeof message.content === "string") return [message.content];
  if (!Array.isArray(message.content)) return [];
  return message.content.flatMap((part) => {
    if (
      !part ||
      typeof part !== "object" ||
      (part as { type?: unknown }).type !== "text" ||
      typeof (part as { text?: unknown }).text !== "string"
    ) {
      return [];
    }
    return [(part as { text: string }).text];
  });
}

function normalizeLocalAttachmentPath(value: unknown): string {
  const source = String(value || "").trim();
  if (!source) return "";
  const marker = "/files/preview/";
  let path: string;

  if (source.startsWith("//") || /^[a-z][a-z\d+.-]*:/i.test(source)) {
    try {
      const currentOrigin =
        typeof window === "undefined"
          ? "http://localhost"
          : window.location.origin;
      const url = new URL(source, currentOrigin);
      if (url.protocol === "file:") {
        if (url.hostname && url.hostname !== "localhost") return "";
        path = url.pathname;
      } else if (["http:", "https:"].includes(url.protocol)) {
        const markerIndex = url.pathname.indexOf(marker);
        if (markerIndex < 0) return "";
        const isLoopback = ["localhost", "127.0.0.1", "[::1]"].includes(
          url.hostname,
        );
        const isSameOrigin =
          Boolean(currentOrigin && currentOrigin !== "null") &&
          url.origin === currentOrigin;
        if (!isSameOrigin && !isLoopback) return "";
        path = `/${url.pathname
          .slice(markerIndex + marker.length)
          .replace(/^\/+/, "")}`;
      } else {
        return "";
      }
    } catch {
      return "";
    }
  } else if (source.startsWith("/")) {
    const pathname = source.split(/[?#]/, 1)[0];
    const markerIndex = pathname.indexOf(marker);
    path =
      markerIndex >= 0
        ? `/${pathname.slice(markerIndex + marker.length).replace(/^\/+/, "")}`
        : pathname;
  } else if (/^[a-z]:[\\/]/i.test(source)) {
    path = source.split(/[?#]/, 1)[0];
  } else {
    return "";
  }
  try {
    return decodeURIComponent(path).replace(/\\/g, "/").replace(/\/$/, "");
  } catch {
    return "";
  }
}

function attachmentStoredName(
  attachment: Record<string, unknown>,
  trustedPaths: Set<string>,
): string {
  for (const candidate of [attachment.file_source_url, attachment.file_url]) {
    const path = normalizeLocalAttachmentPath(candidate);
    if (!path || !trustedPaths.has(path)) continue;
    const parts = path.split("/").filter(Boolean);
    if (parts.length < 2 || parts[parts.length - 2] !== "media") continue;
    const pathStoredName = parts[parts.length - 1] || "";
    const declaredStoredName = String(attachment.stored_name || "").trim();
    const digest = String(attachment.attachment_digest || "").trim();
    const digestMatch = /^sha256:([0-9a-f]{64})$/i.exec(digest);
    const hasBoundReceipt =
      attachment.attachment_schema_version === 1 &&
      attachment.attachment_version === 1 &&
      attachment.attachment_grant_state === "bound" &&
      Boolean(String(attachment.attachment_source_id || "").trim()) &&
      Boolean(String(attachment.attachment_source_chat_id || "").trim()) &&
      Boolean(String(attachment.attachment_source_command_id || "").trim()) &&
      Boolean(String(attachment.attachment_grant_ref || "").trim());
    if (
      hasBoundReceipt &&
      digestMatch &&
      declaredStoredName === pathStoredName &&
      pathStoredName
        .toLowerCase()
        .startsWith(`${digestMatch[1].toLowerCase()}_`)
    ) {
      return declaredStoredName;
    }
    const storedName = pathStoredName;
    if (/^(?:[0-9a-f]{32}|[0-9a-f]{64})_.+/i.test(storedName)) {
      return storedName;
    }
  }
  return "";
}

export function attachmentRolesFromMessages(
  messages: Message[],
  trustedAttachmentPaths: unknown[] = [],
): Map<string, CanonicalAttachmentProjection> {
  const trustedPaths = new Set(
    trustedAttachmentPaths
      .map(normalizeLocalAttachmentPath)
      .filter((path): path is string => Boolean(path)),
  );
  if (trustedPaths.size === 0) return new Map();
  const roles = new Map<string, CanonicalAttachmentProjection>();
  for (const message of messages) {
    if (message.role !== "user" || !Array.isArray(message.content)) continue;
    const textParts = userMessageTextParts(message);
    const declaredRoles = new Map<string, LantaiAttachmentRole>();
    for (const text of textParts) {
      for (const [name, role] of parseAttachmentRoleContext(text)) {
        declaredRoles.set(name, role);
      }
    }
    for (const part of message.content) {
      if (!part || typeof part !== "object") continue;
      const attachment = part as Record<string, unknown>;
      if (!STRUCTURED_ATTACHMENT_CONTENT_TYPES.has(String(attachment.type))) {
        continue;
      }
      const displayName = String(
        attachment.filename || attachment.file_name || "",
      ).trim();
      if (!displayName) continue;
      const declaredRole = attachment.attachment_role;
      const role = isLantaiAttachmentRole(declaredRole)
        ? declaredRole
        : declaredRoles.get(displayName);
      if (!role) continue;
      const storedName = attachmentStoredName(attachment, trustedPaths);
      if (!storedName) continue;
      roles.set(storedName, { role, displayName });
    }
  }
  return roles;
}

function latestPerLineage(works: WorkItem[]): WorkItem[] {
  const latest = new Map<string, WorkItem>();
  for (const work of works) {
    const key = work.lineage_root_id || work.id;
    const current = latest.get(key);
    if (
      !current ||
      work.version > current.version ||
      (work.version === current.version && work.updated_at > current.updated_at)
    ) {
      latest.set(key, work);
    }
  }
  return [...latest.values()].sort((left, right) =>
    right.updated_at.localeCompare(left.updated_at),
  );
}

/**
 * Selects only from durable Works relationships. Workspace file order and
 * extensions are intentionally absent: they are not evidence of an outcome.
 */
export function selectPrimaryOutcome(input: {
  works: WorkItem[];
  chatId: string;
  currentRunId?: string | null;
  pinnedWorkId?: string | null;
}): ArtifactOutcomeSelection {
  const chatWorks = input.works.filter(
    (work) => work.source_chat_id === input.chatId,
  );
  const eligible = latestPerLineage(chatWorks);
  const automatic = eligible.filter(
    (work) =>
      !work.derivation &&
      (work.artifact_role === "primary" || work.artifact_role === "delivery"),
  );

  if (input.pinnedWorkId) {
    const pinned = chatWorks.find((work) => work.id === input.pinnedWorkId);
    if (pinned) {
      const lineage = pinned.lineage_root_id || pinned.id;
      return {
        // A user's explicit revision choice is stable. Newer revisions remain
        // visible in history but must not steal focus or silently change the
        // bytes used by preview, download, or edit conflict checks.
        primary: pinned,
        alternatives: eligible.filter(
          (work) => (work.lineage_root_id || work.id) !== lineage,
        ),
        reason: "user-selection",
      };
    }
  }

  if (input.currentRunId) {
    const currentRun = latestPerLineage(
      chatWorks.filter(
        (work) =>
          work.source_run_id === input.currentRunId &&
          !work.derivation &&
          (work.artifact_role === "primary" ||
            work.artifact_role === "delivery"),
      ),
    );
    if (currentRun.length === 1) {
      return {
        primary: currentRun[0],
        alternatives: eligible.filter((work) => work.id !== currentRun[0].id),
        reason: "current-run",
      };
    }
    if (currentRun.length > 1) {
      return { primary: null, alternatives: currentRun, reason: "ambiguous" };
    }
  }

  if (automatic.length === 1) {
    return {
      primary: automatic[0],
      alternatives: eligible.filter((work) => work.id !== automatic[0].id),
      reason: "single-lineage",
    };
  }
  if (automatic.length > 1) {
    return { primary: null, alternatives: automatic, reason: "ambiguous" };
  }
  // Durable Works with no canonical primary/delivery role remain visible as
  // alternatives. They may be process files, references, or merely
  // unclassified; the panel must not silently promote them to an outcome.
  return { primary: null, alternatives: eligible, reason: "none" };
}

export function projectArtifactOutcome(input: {
  work: WorkItem;
  runtime?: ArtifactRuntimeProjection | null;
  previewStatus: ArtifactOutcomeProjection["preview"];
}): ArtifactOutcomeProjection {
  const exactRuntimeMatch =
    Boolean(input.work.completion_bound) &&
    Boolean(input.work.source_run_id) &&
    input.runtime?.run_id === input.work.source_run_id;
  const delivery = input.work.completion_bound
    ? input.work.delivery_status ||
      (exactRuntimeMatch ? input.runtime?.delivery_status : null) ||
      "unknown"
    : "unknown";
  return {
    availability: input.work.availability_status || "unknown",
    preview: input.previewStatus,
    // Validation is exact-version Runtime evidence projected by Works. Never
    // infer it from previewability, file integrity, delivery, or model prose.
    validation: input.work.validation || null,
    delivery,
    execution:
      input.runtime?.run_id === input.work.source_run_id
        ? input.runtime?.status || "unknown"
        : "unknown",
  };
}

export function buildArtifactEditTarget(input: {
  chatId: string;
  work: WorkItem;
  editPath: string;
  page?: number;
  paragraph?: number;
  region?: string;
}): ArtifactEditTarget {
  let selection: ArtifactEditTarget["selection"];
  if (input.page != null) {
    selection = { kind: "page", page: input.page };
  } else if (input.paragraph != null) {
    selection = { kind: "paragraph", paragraph: input.paragraph };
  } else if (input.region) {
    selection = { kind: "region", region: input.region };
  }
  return {
    schema_version: 1,
    chat_id: input.chatId,
    asset_id: input.work.artifact_id,
    work_id: input.work.id,
    edit_path: input.editPath,
    base_revision: {
      version: input.work.version,
      digest: input.work.source_digest || null,
    },
    ...(selection ? { selection } : {}),
  };
}
