import type {
  CronJobDispatch,
  CronJobRequest,
  CronJobRuntime,
  CronJobSpecInput,
} from "../../../api/types";

export const SCHEDULED_TASK_TOOL_NAME = "prepare_scheduled_task";

const DEFAULT_USER_ID = "default";
const DEFAULT_CHANNEL = "console";
const DEFAULT_TIMEZONE = "Asia/Shanghai";
const DEFAULT_PROMPT = "请执行这个定时任务。";

const DAY_LABELS: Record<string, string> = {
  mon: "周一",
  tue: "周二",
  wed: "周三",
  thu: "周四",
  fri: "周五",
  sat: "周六",
  sun: "周日",
};

export interface ScheduledTaskDraftPayload {
  summary: string;
  warnings: string[];
  prompt: string;
  spec: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export interface ScheduledTaskContext {
  sessionId?: string;
  userId?: string;
  channel?: string;
  timezone?: string;
}

export function isScheduledTaskToolName(toolName: string): boolean {
  return toolName === SCHEDULED_TASK_TOOL_NAME;
}

function tryParseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed || !/^[{["]/.test(trimmed)) return value;

  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function unwrapContentParts(value: unknown): unknown {
  const parsed = tryParseJson(value);

  if (Array.isArray(parsed)) {
    const contentTexts = parsed
      .map((item) => {
        const record = asRecord(item);
        return typeof record?.text === "string" ? record.text : null;
      })
      .filter((item): item is string => Boolean(item));

    if (contentTexts.length === parsed.length && contentTexts.length > 0) {
      const unwrapped = contentTexts.map((item) => tryParseJson(item));
      if (unwrapped.length === 1) return unwrapped[0];
      return unwrapped;
    }
  }

  const record = asRecord(parsed);
  if (record && typeof record.text === "string" && Object.keys(record).length <= 2) {
    return tryParseJson(record.text);
  }

  return parsed;
}

function pickDraftRecord(value: unknown, depth = 0): Record<string, unknown> | null {
  if (depth > 5) return null;
  const unwrapped = unwrapContentParts(value);
  const record = asRecord(unwrapped);

  if (!record) return null;
  if (record.type === "scheduled_task_draft" || asRecord(record.spec)) {
    return record;
  }

  for (const key of ["result", "output", "content", "data", "text"]) {
    if (record[key] !== undefined) {
      const nested = pickDraftRecord(record[key], depth + 1);
      if (nested) return nested;
    }
  }

  return null;
}

function readString(record: Record<string, unknown> | null, key: string): string {
  const value = record?.[key];
  return typeof value === "string" ? value.trim() : "";
}

function readBoolean(
  record: Record<string, unknown> | null,
  key: string,
  fallback: boolean,
): boolean {
  const value = record?.[key];
  return typeof value === "boolean" ? value : fallback;
}

function readNumber(
  record: Record<string, unknown> | null,
  key: string,
): number | undefined {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : undefined;
}

function normalizeWarnings(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is string => typeof item === "string");
  }
  return typeof value === "string" && value.trim() ? [value.trim()] : [];
}

function compactRecord(record: Record<string, unknown> | null): Record<string, unknown> {
  if (!record) return {};

  return Object.fromEntries(
    Object.entries(record).filter(([, value]) => value !== undefined),
  );
}

function buildTextInput(text: string): CronJobRequest["input"] {
  return [
    {
      role: "user",
      content: [
        {
          type: "text",
          text,
        },
      ],
    },
  ];
}

function extractTextFromContent(value: unknown): string {
  const parsed = tryParseJson(value);

  if (typeof parsed === "string") return parsed.trim();
  if (Array.isArray(parsed)) {
    return parsed
      .map((item) => extractTextFromContent(item))
      .filter(Boolean)
      .join("\n")
      .trim();
  }

  const record = asRecord(parsed);
  if (!record) return "";

  if (typeof record.text === "string") return record.text.trim();
  if (record.content !== undefined) return extractTextFromContent(record.content);
  if (record.input !== undefined) return extractTextFromContent(record.input);
  return "";
}

function normalizeRequestInput(input: unknown, fallbackPrompt: string): unknown {
  if (input === undefined || input === null || input === "") {
    return buildTextInput(fallbackPrompt);
  }

  const parsed = tryParseJson(input);
  if (typeof parsed === "string") {
    return buildTextInput(parsed.trim() || fallbackPrompt);
  }

  return parsed;
}

function resolvePrompt(
  raw: Record<string, unknown>,
  spec: Record<string, unknown>,
): string {
  const request = asRecord(spec.request);
  return (
    readString(raw, "prompt") ||
    readString(spec, "text") ||
    extractTextFromContent(request?.input) ||
    DEFAULT_PROMPT
  );
}

export function parseScheduledTaskDraftOutput(
  output: unknown,
): ScheduledTaskDraftPayload | null {
  const raw = pickDraftRecord(output);
  if (!raw) return null;

  const spec = asRecord(raw.spec) || raw;
  const prompt = resolvePrompt(raw, spec);

  return {
    summary: readString(raw, "summary"),
    warnings: normalizeWarnings(raw.warnings),
    prompt,
    spec,
    raw,
  };
}

export function buildCronJobSpecFromDraft(
  draft: ScheduledTaskDraftPayload,
  context: ScheduledTaskContext = {},
): CronJobSpecInput {
  const source = draft.spec;
  const scheduleSource = asRecord(source.schedule);
  const dispatchSource = asRecord(source.dispatch);
  const targetSource = asRecord(dispatchSource?.target);
  const runtimeSource = asRecord(source.runtime);
  const requestSource = asRecord(source.request);

  const channel =
    readString(dispatchSource, "channel") || context.channel || DEFAULT_CHANNEL;
  const userId =
    readString(targetSource, "user_id") || context.userId || DEFAULT_USER_ID;
  const sessionId =
    readString(targetSource, "session_id") ||
    context.sessionId ||
    `${channel}:${userId}`;
  const taskType = readString(source, "task_type") === "text" ? "text" : "agent";
  const prompt = draft.prompt || DEFAULT_PROMPT;

  const dispatch: CronJobDispatch = {
    type: "channel",
    channel,
    target: {
      user_id: userId,
      session_id: sessionId,
    },
    mode: readString(dispatchSource, "mode") === "final" ? "final" : "stream",
    meta: compactRecord(asRecord(dispatchSource?.meta)),
  };

  const runtime: CronJobRuntime = {
    max_concurrency: readNumber(runtimeSource, "max_concurrency") ?? 1,
    timeout_seconds: readNumber(runtimeSource, "timeout_seconds") ?? 120,
    misfire_grace_seconds:
      readNumber(runtimeSource, "misfire_grace_seconds") ?? 60,
    share_session: readBoolean(runtimeSource, "share_session", true),
  };

  const base: CronJobSpecInput = {
    name: readString(source, "name") || "定时任务",
    enabled: readBoolean(source, "enabled", true),
    schedule: {
      type: "cron",
      cron: readString(scheduleSource, "cron") || "0 9 * * *",
      timezone:
        readString(scheduleSource, "timezone") ||
        context.timezone ||
        DEFAULT_TIMEZONE,
    },
    task_type: taskType,
    dispatch,
    runtime,
    meta: {
      ...compactRecord(asRecord(source.meta)),
      created_from: "chat_natural_language",
      draft_tool: SCHEDULED_TASK_TOOL_NAME,
      draft_summary: draft.summary,
    },
  };

  if (taskType === "text") {
    return {
      ...base,
      text: readString(source, "text") || prompt,
    };
  }

  return {
    ...base,
    request: {
      ...compactRecord(requestSource),
      input: normalizeRequestInput(requestSource?.input, prompt),
      user_id: userId,
      session_id: sessionId,
    },
  };
}

export function getCronJobInstructionPreview(spec: CronJobSpecInput): string {
  if (spec.task_type === "text") return spec.text || "";
  return extractTextFromContent(spec.request?.input);
}

export function formatCronLabel(cron: string, timezone?: string): string {
  const parts = cron.trim().split(/\s+/);
  const suffix = timezone ? `（${timezone}）` : "";
  if (parts.length !== 5) return `${cron}${suffix}`;

  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
  const minuteNum = Number(minute);
  const hourNum = Number(hour);
  const isPlainTime =
    Number.isInteger(minuteNum) &&
    minuteNum >= 0 &&
    minuteNum <= 59 &&
    Number.isInteger(hourNum) &&
    hourNum >= 0 &&
    hourNum <= 23;

  if (!isPlainTime) return `${cron}${suffix}`;

  const time = `${String(hourNum).padStart(2, "0")}:${String(minuteNum).padStart(
    2,
    "0",
  )}`;

  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "*") {
    return `每天 ${time}${suffix}`;
  }

  if (dayOfMonth === "*" && month === "*" && dayOfWeek !== "*") {
    const days = dayOfWeek
      .split(",")
      .map((day) => DAY_LABELS[day.toLowerCase()] || day)
      .join("、");
    return `每${days} ${time}${suffix}`;
  }

  return `${cron}${suffix}`;
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableStringify(item)).join(",")}]`;
  }

  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`)
    .join(",")}}`;
}

function hashString(value: string): string {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = Math.imul(31, hash) + value.charCodeAt(index);
    hash |= 0;
  }
  return Math.abs(hash).toString(36);
}

export function buildDraftStorageKey(spec: CronJobSpecInput): string {
  return `lantai:scheduled-task-draft:${hashString(
    stableStringify({
      name: spec.name,
      schedule: spec.schedule,
      task_type: spec.task_type,
      text: spec.text,
      request_input: spec.request?.input,
      dispatch: spec.dispatch,
    }),
  )}`;
}
