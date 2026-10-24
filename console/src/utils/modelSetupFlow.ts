export type ModelSetupMode = "watcha" | "own" | "local";

const CHAT_FALLBACK = "/chat";
const DRAFT_STORAGE_PREFIX = "lantai:model-setup-draft:";
const FIRST_MODEL_TASK =
  "请把下面的会议记录整理成一份简短纪要，保留待确认项，等我确认后再生成正式稿。\n\n- 下周一确认上线范围\n- 需要补充验收人和截止日期\n- 预算数字尚未定稿";

export interface ModelSetupDraftTaskContext {
  agentId?: string;
  selectedSkillName?: string;
  deepThinking?: {
    enabled: boolean;
    effort: string;
  };
}

export interface ModelSetupDraftSnapshot {
  version: 2;
  returnTo: string;
  composerText: string;
  submittedInput: Array<Record<string, unknown>>;
  taskContext?: ModelSetupDraftTaskContext;
  createdAt: number;
}

export interface ModelSetupDraftInput {
  composerText: string;
  submittedInput?: Array<Record<string, unknown>>;
  taskContext?: ModelSetupDraftTaskContext;
}

export function parseModelSetupMode(
  value: string | null | undefined,
): ModelSetupMode | null {
  return value === "watcha" || value === "own" || value === "local"
    ? value
    : null;
}

export function normalizeModelSetupReturnTo(
  value: string | null | undefined,
): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return CHAT_FALLBACK;
  }

  try {
    const url = new URL(value, "https://lantai.local");
    const isChatRoute =
      url.pathname === CHAT_FALLBACK || url.pathname.startsWith("/chat/");
    if (url.origin !== "https://lantai.local" || !isChatRoute || url.hash) {
      return CHAT_FALLBACK;
    }
    return `${url.pathname}${url.search}`;
  } catch {
    return CHAT_FALLBACK;
  }
}

function withSetupParams(
  pathname: string,
  mode: ModelSetupMode,
  returnTo: string,
): string {
  const params = new URLSearchParams({
    setup: mode,
    returnTo: normalizeModelSetupReturnTo(returnTo),
  });
  return `${pathname}?${params.toString()}`;
}

export function buildModelsSetupPath(
  mode: ModelSetupMode,
  returnTo: string,
): string {
  return withSetupParams("/models", mode, returnTo);
}

export function buildAccountSetupPath(returnTo: string): string {
  return withSetupParams("/account", "watcha", returnTo);
}

function draftStorageKey(returnTo: string): string {
  return `${DRAFT_STORAGE_PREFIX}${normalizeModelSetupReturnTo(returnTo)}`;
}

function cloneInput(
  input: Array<Record<string, unknown>> | undefined,
): Array<Record<string, unknown>> {
  if (!input?.length) return [];
  try {
    const cloned = JSON.parse(JSON.stringify(input));
    return Array.isArray(cloned) ? cloned : [];
  } catch {
    return [];
  }
}

function parseStoredSnapshot(
  raw: string,
  expectedReturnTo: string,
): ModelSetupDraftSnapshot | null {
  try {
    const parsed = JSON.parse(raw) as Partial<ModelSetupDraftSnapshot>;
    if (
      parsed?.version !== 2 ||
      parsed.returnTo !== expectedReturnTo ||
      typeof parsed.composerText !== "string" ||
      !Array.isArray(parsed.submittedInput) ||
      typeof parsed.createdAt !== "number"
    ) {
      return null;
    }
    return {
      version: 2,
      returnTo: parsed.returnTo,
      composerText: parsed.composerText,
      submittedInput: cloneInput(parsed.submittedInput),
      ...(parsed.taskContext && typeof parsed.taskContext === "object"
        ? { taskContext: parsed.taskContext }
        : {}),
      createdAt: parsed.createdAt,
    };
  } catch {
    return null;
  }
}

export function persistModelSetupDraftSnapshot(
  returnTo: string,
  draft: ModelSetupDraftInput,
): void {
  const normalizedReturnTo = normalizeModelSetupReturnTo(returnTo);
  const composerText = draft.composerText.trim();
  const submittedInput = cloneInput(draft.submittedInput);
  if (!composerText && submittedInput.length === 0) return;
  const snapshot: ModelSetupDraftSnapshot = {
    version: 2,
    returnTo: normalizedReturnTo,
    composerText,
    submittedInput,
    ...(draft.taskContext ? { taskContext: draft.taskContext } : {}),
    createdAt: Date.now(),
  };
  try {
    window.sessionStorage.setItem(
      draftStorageKey(normalizedReturnTo),
      JSON.stringify(snapshot),
    );
  } catch {
    // A blocked storage API must not prevent the model setup flow.
  }
}

export function persistModelSetupDraft(returnTo: string, draft: string): void {
  const value = draft.trim();
  if (!value) return;
  try {
    window.sessionStorage.setItem(draftStorageKey(returnTo), value);
  } catch {
    // A blocked storage API must not prevent the model setup flow.
  }
}

export function consumeModelSetupDraftSnapshot(
  returnTo: string,
): ModelSetupDraftSnapshot | null {
  try {
    const normalizedReturnTo = normalizeModelSetupReturnTo(returnTo);
    const key = draftStorageKey(normalizedReturnTo);
    const raw = window.sessionStorage.getItem(key) || "";
    if (!raw) return null;
    window.sessionStorage.removeItem(key);
    const snapshot = parseStoredSnapshot(raw, normalizedReturnTo);
    if (snapshot) return snapshot;
    return {
      version: 2,
      returnTo: normalizedReturnTo,
      composerText: raw,
      submittedInput: [],
      createdAt: Date.now(),
    };
  } catch {
    return null;
  }
}

export function consumeModelSetupDraft(returnTo: string): string {
  return consumeModelSetupDraftSnapshot(returnTo)?.composerText ?? "";
}

export function ensureFirstModelTaskDraft(returnTo: string): boolean {
  try {
    const key = draftStorageKey(returnTo);
    if (window.sessionStorage.getItem(key)) return false;
    persistModelSetupDraftSnapshot(returnTo, {
      composerText: FIRST_MODEL_TASK,
    });
    return true;
  } catch {
    return false;
  }
}

function isTextContentBlock(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const type = (value as Record<string, unknown>).type;
  return type === "text" || type === "input_text" || type === "output_text";
}

function contentKey(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function findLastUserIndex(input: Array<Record<string, unknown>>): number {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    if (input[index]?.role === "user") return index;
  }
  return -1;
}

export function mergeRestoredModelSetupInput(
  currentInput: Array<Record<string, unknown>>,
  restoredInput: Array<Record<string, unknown>>,
): Array<Record<string, unknown>> {
  const restoredUserIndex = findLastUserIndex(restoredInput);
  const currentUserIndex = findLastUserIndex(currentInput);
  if (restoredUserIndex < 0 || currentUserIndex < 0) return currentInput;

  const restoredContent = restoredInput[restoredUserIndex]?.content;
  if (!Array.isArray(restoredContent)) return currentInput;
  const attachments = restoredContent.filter(
    (item) => !isTextContentBlock(item),
  );
  if (attachments.length === 0) return currentInput;

  const next = currentInput.map((message) => ({ ...message }));
  const currentMessage = next[currentUserIndex];
  const currentContent = currentMessage?.content;
  const currentBlocks = Array.isArray(currentContent)
    ? [...currentContent]
    : typeof currentContent === "string"
      ? [{ type: "text", text: currentContent }]
      : [];
  const seen = new Set(currentBlocks.map(contentKey));
  for (const attachment of attachments) {
    const key = contentKey(attachment);
    if (!seen.has(key)) {
      currentBlocks.push(attachment);
      seen.add(key);
    }
  }
  currentMessage.content = currentBlocks;
  return next;
}
