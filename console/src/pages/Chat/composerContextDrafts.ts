import type { ArtifactEditDraft } from "./components/ArtifactEditTargetChip";
import type { ArtifactEditTarget } from "./components/artifactExperience";

export interface ComposerDraftOwner {
  agentId: string;
  sessionId: string;
}

export interface ComposerContextDraft<T> extends ComposerDraftOwner {
  revision: string;
  value: T;
  persisted: boolean;
}

type DraftKind = "artifact-edit" | "model-setup-input";
type SubmittedInput = Array<Record<string, unknown>>;
const STORAGE_PREFIX = "lantai:composer-context:v1";
const CHANGE_EVENT = "lantai:composer-context-changed";

export function sameComposerDraftOwner(
  left: ComposerDraftOwner,
  right: ComposerDraftOwner,
): boolean {
  return left.agentId === right.agentId && left.sessionId === right.sessionId;
}

export function composerContextStorageKey(
  owner: ComposerDraftOwner,
  kind: DraftKind,
): string {
  return `${STORAGE_PREFIX}:${kind}:${encodeURIComponent(owner.agentId)}:${encodeURIComponent(owner.sessionId)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isNonemptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function parseArtifactDraft(
  value: unknown,
  owner: ComposerDraftOwner,
): ArtifactEditDraft | null {
  if (!isRecord(value) || !isNonemptyString(value.title)) return null;
  const target = value.target;
  if (
    !isRecord(target) ||
    target.schema_version !== 1 ||
    target.chat_id !== owner.sessionId ||
    !isNonemptyString(target.asset_id) ||
    !isNonemptyString(target.work_id) ||
    !isNonemptyString(target.edit_path) ||
    !isRecord(target.base_revision) ||
    !isPositiveInteger(target.base_revision.version) ||
    !(
      target.base_revision.digest === null ||
      isNonemptyString(target.base_revision.digest)
    )
  )
    return null;
  let selection: ArtifactEditTarget["selection"];
  if (target.selection !== undefined) {
    const candidate = target.selection;
    if (!isRecord(candidate)) return null;
    if (candidate.kind === "page" && isPositiveInteger(candidate.page)) {
      selection = { kind: "page", page: candidate.page };
    } else if (
      candidate.kind === "paragraph" &&
      isPositiveInteger(candidate.paragraph)
    ) {
      selection = { kind: "paragraph", paragraph: candidate.paragraph };
    } else if (
      candidate.kind === "region" &&
      isNonemptyString(candidate.region)
    ) {
      selection = { kind: "region", region: candidate.region };
    } else return null;
  }
  return {
    title: value.title,
    target: {
      schema_version: 1,
      chat_id: owner.sessionId,
      asset_id: target.asset_id,
      work_id: target.work_id,
      edit_path: target.edit_path,
      base_revision: {
        version: target.base_revision.version,
        digest: target.base_revision.digest,
      },
      ...(selection ? { selection } : {}),
    },
  };
}

function parseSubmittedInput(value: unknown): SubmittedInput | null {
  return Array.isArray(value) && value.length > 0 && value.every(isRecord)
    ? value
    : null;
}

function loadDraft<T>(
  owner: ComposerDraftOwner,
  kind: DraftKind,
  parseValue: (value: unknown, owner: ComposerDraftOwner) => T | null,
): ComposerContextDraft<T> | null {
  try {
    const raw = window.sessionStorage.getItem(
      composerContextStorageKey(owner, kind),
    );
    if (!raw) return null;
    const stored: unknown = JSON.parse(raw);
    if (
      !isRecord(stored) ||
      stored.schema_version !== 1 ||
      stored.agentId !== owner.agentId ||
      stored.sessionId !== owner.sessionId ||
      !isNonemptyString(stored.revision)
    )
      return null;
    const value = parseValue(stored.value, owner);
    return value === null
      ? null
      : { ...owner, revision: stored.revision, value, persisted: true };
  } catch {
    return null;
  }
}

function notifyChange(owner: ComposerDraftOwner, kind: DraftKind): void {
  window.dispatchEvent(
    new CustomEvent(CHANGE_EVENT, { detail: { ...owner, kind } }),
  );
}

function saveDraft<T>(
  owner: ComposerDraftOwner,
  kind: DraftKind,
  value: T,
): ComposerContextDraft<T> {
  const revision =
    window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  const draft = { ...owner, revision, value, persisted: false };
  try {
    const raw = JSON.stringify({
      schema_version: 1,
      ...owner,
      revision,
      value,
    });
    window.sessionStorage.setItem(composerContextStorageKey(owner, kind), raw);
    draft.value = JSON.parse(raw).value as T;
    draft.persisted = true;
    notifyChange(owner, kind);
  } catch {
    // Keep the current mounted composer usable when browser storage is blocked.
  }
  return draft;
}

function clearDraft(
  owner: ComposerDraftOwner,
  kind: DraftKind,
  expectedRevision?: string,
): void {
  try {
    const key = composerContextStorageKey(owner, kind);
    if (expectedRevision) {
      const raw = window.sessionStorage.getItem(key);
      if (!raw) return;
      const stored: unknown = JSON.parse(raw);
      if (
        !isRecord(stored) ||
        stored.revision !== expectedRevision ||
        stored.agentId !== owner.agentId ||
        stored.sessionId !== owner.sessionId
      )
        return;
    }
    window.sessionStorage.removeItem(key);
    notifyChange(owner, kind);
  } catch {
    // A request result must not fail because browser storage is unavailable.
  }
}

export function subscribeComposerContextDrafts(
  owner: ComposerDraftOwner,
  onChange: (kind: DraftKind) => void,
): () => void {
  const listener = (event: Event) => {
    const detail: unknown = (event as CustomEvent).detail;
    if (
      isRecord(detail) &&
      detail.agentId === owner.agentId &&
      detail.sessionId === owner.sessionId &&
      (detail.kind === "artifact-edit" || detail.kind === "model-setup-input")
    ) {
      onChange(detail.kind);
    }
  };
  window.addEventListener(CHANGE_EVENT, listener);
  return () => window.removeEventListener(CHANGE_EVENT, listener);
}

export const loadArtifactEditDraft = (owner: ComposerDraftOwner) =>
  loadDraft(owner, "artifact-edit", parseArtifactDraft);
export const saveArtifactEditDraft = (
  owner: ComposerDraftOwner,
  value: ArtifactEditDraft,
) => saveDraft(owner, "artifact-edit", value);
export const clearStoredArtifactEditDraft = (
  owner: ComposerDraftOwner,
  revision?: string,
) => clearDraft(owner, "artifact-edit", revision);
export const loadModelSetupInputDraft = (owner: ComposerDraftOwner) =>
  loadDraft(owner, "model-setup-input", parseSubmittedInput);
export const saveModelSetupInputDraft = (
  owner: ComposerDraftOwner,
  value: SubmittedInput,
) => saveDraft(owner, "model-setup-input", value);
export const clearModelSetupInputDraft = (
  owner: ComposerDraftOwner,
  revision?: string,
) => clearDraft(owner, "model-setup-input", revision);
