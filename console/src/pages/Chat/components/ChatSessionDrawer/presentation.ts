export interface SessionPresentationSource {
  id: string;
  name?: string;
  pinned?: boolean;
  archived?: boolean;
  createdAt?: string | null;
  updatedAt?: string | null;
  status?: string;
  generating?: boolean;
  meta?: Record<string, unknown>;
  runtimeState?: {
    status?: string;
    summary?: string | null;
    outcome_status?: string | null;
    delivery_status?: string | null;
  } | null;
  turnEventPreview?: {
    summary_line?: string | null;
  } | null;
}

export type SessionStatusTone =
  | "running"
  | "success"
  | "warning"
  | "danger"
  | "neutral";

export interface SessionStatusPresentation {
  label: string;
  tone: SessionStatusTone;
}

export type SessionListFilter =
  | "all"
  | "active"
  | "attention"
  | "completed"
  | "archived";

export type SessionListRow<T extends SessionPresentationSource> =
  | {
      kind: "group";
      key: string;
      label: string;
      count: number;
      collapsed: boolean;
    }
  | {
      kind: "session";
      key: string;
      session: T;
    };

function timestampMs(session: SessionPresentationSource): number {
  const raw = session.updatedAt || session.createdAt;
  if (!raw) return 0;
  const value = new Date(raw).getTime();
  return Number.isFinite(value) ? value : 0;
}

function startOfLocalDay(value: Date): number {
  return new Date(
    value.getFullYear(),
    value.getMonth(),
    value.getDate(),
  ).getTime();
}

function calendarDayDistance(timestamp: number, now: Date): number {
  if (!timestamp) return Number.POSITIVE_INFINITY;
  return Math.max(
    0,
    Math.round(
      (startOfLocalDay(now) - startOfLocalDay(new Date(timestamp))) /
        86_400_000,
    ),
  );
}

function dateGroup(
  session: SessionPresentationSource,
  now: Date,
): { key: string; label: string } {
  if (session.pinned) return { key: "pinned", label: "置顶" };
  const distance = calendarDayDistance(timestampMs(session), now);
  if (distance === 0) return { key: "today", label: "今天" };
  if (distance === 1) return { key: "yesterday", label: "昨天" };
  if (distance <= 7) return { key: "recent", label: "近 7 天" };
  return { key: "earlier", label: "更早" };
}

function stringMeta(session: SessionPresentationSource, key: string): string {
  const value = session.meta?.[key];
  return typeof value === "string" ? value.trim() : "";
}

function cleanPreview(value: string, maxLength = 88): string {
  const cleaned = value
    .replace(
      /<lantai_attachment_roles>\s*[\s\S]*?\s*<\/lantai_attachment_roles>/gi,
      "",
    )
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/```[\w-]*|```/g, " ")
    .replace(/^[#>*+\-\d.)\s]+/gm, "")
    .replace(/[*_~`]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (cleaned.length <= maxLength) return cleaned;
  return `${cleaned.slice(0, maxLength - 1).trimEnd()}…`;
}

export function getSessionTitle(
  session: SessionPresentationSource,
  fallback = "New Chat",
): string {
  return cleanPreview(session.name || "", 56) || fallback;
}

export function getSessionPreview(session: SessionPresentationSource): string {
  const source =
    session.runtimeState?.summary?.trim() ||
    session.turnEventPreview?.summary_line?.trim() ||
    stringMeta(session, "last_result_summary") ||
    stringMeta(session, "last_turn_text");
  const preview = cleanPreview(source || "");
  const outcomeStatus = String(
    session.runtimeState?.outcome_status ||
      stringMeta(session, "last_turn_outcome_status"),
  ).toLowerCase();
  if (outcomeStatus === "needs_input" && preview) {
    return cleanPreview(`等待确认 · ${preview}`);
  }
  if (outcomeStatus === "blocked" && preview) {
    return cleanPreview(`受阻 · ${preview}`);
  }
  return preview;
}

export function getSessionStatus(
  session: SessionPresentationSource,
): SessionStatusPresentation {
  const runtimeStatus = String(
    session.runtimeState?.status || session.status || "",
  ).toLowerCase();
  const turnStatus = stringMeta(session, "last_turn_status").toLowerCase();
  const outcomeStatus = String(
    session.runtimeState?.outcome_status ||
      stringMeta(session, "last_turn_outcome_status"),
  ).toLowerCase();
  const deliveryStatus = String(
    session.runtimeState?.delivery_status ||
      stringMeta(session, "last_turn_delivery_status"),
  ).toLowerCase();
  if (
    session.generating ||
    ["running", "generating", "queued"].includes(runtimeStatus)
  ) {
    return { label: "运行中", tone: "running" };
  }
  if (
    ["failed", "error"].includes(runtimeStatus) ||
    ["failed", "error"].includes(turnStatus)
  ) {
    return { label: "执行失败", tone: "danger" };
  }
  if (runtimeStatus === "stalled" || turnStatus === "stalled") {
    return { label: "待处理", tone: "warning" };
  }
  if (
    ["interrupted", "cancelled", "canceled"].includes(runtimeStatus) ||
    ["interrupted", "cancelled", "canceled"].includes(turnStatus)
  ) {
    return { label: "已停止", tone: "warning" };
  }
  if (outcomeStatus === "needs_input") {
    return { label: "待你处理", tone: "warning" };
  }
  if (outcomeStatus === "blocked") {
    return { label: "受阻", tone: "danger" };
  }
  if (outcomeStatus === "incomplete") {
    return { label: "部分完成", tone: "warning" };
  }
  const hasSettledResult = Boolean(
    session.runtimeState?.summary?.trim() ||
    session.turnEventPreview?.summary_line?.trim() ||
    stringMeta(session, "last_result_summary"),
  );
  if (outcomeStatus === "delivered" || deliveryStatus === "verified") {
    return { label: "任务成功", tone: "success" };
  }
  if (
    hasSettledResult &&
    ["finished", "completed", "succeeded", "success"].includes(turnStatus)
  ) {
    return { label: "回复结束", tone: "neutral" };
  }
  return { label: "最近对话", tone: "neutral" };
}

export function isBlankSession(session: SessionPresentationSource): boolean {
  if (session.pinned || session.archived) return false;
  if (session.name == null) return false;
  const normalizedName = String(session.name || "")
    .trim()
    .toLowerCase();
  if (
    !["", "new chat", "新会话", "chat.newsessiontitle"].includes(normalizedName)
  ) {
    return false;
  }
  if (session.generating || session.status === "running") return false;
  return ![
    stringMeta(session, "last_turn_text"),
    stringMeta(session, "last_result_summary"),
    stringMeta(session, "last_turn_status"),
  ].some(Boolean);
}

export function matchesSessionFilter(
  session: SessionPresentationSource,
  filter: SessionListFilter,
): boolean {
  if (filter === "archived") return session.archived === true;
  if (session.archived) return false;
  const tone = getSessionStatus(session).tone;
  if (filter === "active") return tone === "running";
  if (filter === "attention") return tone === "warning" || tone === "danger";
  if (filter === "completed") return tone === "success";
  return true;
}

export function formatSessionTime(
  session: SessionPresentationSource,
  now = new Date(),
  timeZone?: string,
): string {
  const timestamp = timestampMs(session);
  if (!timestamp) return "";
  const date = new Date(timestamp);
  const pad = (value: number) => String(value).padStart(2, "0");
  if (calendarDayDistance(timestamp, now) === 0) {
    if (timeZone) {
      return new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
        timeZone,
      }).format(date);
    }
    return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function buildSessionRows<T extends SessionPresentationSource>(
  sessions: T[],
  now = new Date(),
  collapsedGroupKeys: ReadonlySet<string> = new Set(),
): SessionListRow<T>[] {
  const order = ["pinned", "today", "yesterday", "recent", "earlier"];
  const grouped = new Map<string, { label: string; sessions: T[] }>();
  [...sessions]
    .filter((session) => !session.archived && !isBlankSession(session))
    .sort((left, right) => timestampMs(right) - timestampMs(left))
    .forEach((session) => {
      const group = dateGroup(session, now);
      const current = grouped.get(group.key) || {
        label: group.label,
        sessions: [],
      };
      current.sessions.push(session);
      grouped.set(group.key, current);
    });

  return order.flatMap((key) => {
    const group = grouped.get(key);
    if (!group?.sessions.length) return [];
    return [
      {
        kind: "group" as const,
        key: `group:${key}`,
        label: group.label,
        count: group.sessions.length,
        collapsed: collapsedGroupKeys.has(key),
      },
      ...(collapsedGroupKeys.has(key)
        ? []
        : group.sessions.map((session) => ({
            kind: "session" as const,
            key: `session:${session.id}`,
            session,
          }))),
    ];
  });
}
