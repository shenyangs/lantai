import type { Artifact, TaskRun, TodayTimeline } from "../../api/types";

export type ContinuationSuggestionKind = "run" | "today" | "artifact";

export interface ContinuationSuggestion {
  id: string;
  kind: ContinuationSuggestionKind;
  title: string;
  summary: string;
  sourceLabel: string;
  timestamp: string | null;
  actionPath: string;
}

const RESUMABLE_STATUSES = new Set([
  "dispatched",
  "acked",
  "running",
  "failed",
  "timed_out",
  "stalled",
  "blocked",
]);

function timestamp(value?: string | null): number {
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : 0;
}

function runPath(run: TaskRun): string {
  return `/chat/${encodeURIComponent(String(run.chat_id))}`;
}

function latestArtifact(
  runs: TaskRun[],
): { run: TaskRun; artifact: Artifact } | null {
  const candidates = runs.flatMap((run) =>
    (run.artifacts || []).map((artifact) => ({ run, artifact })),
  );
  candidates.sort(
    (left, right) =>
      timestamp(right.artifact.created_at) -
      timestamp(left.artifact.created_at),
  );
  return candidates[0] ?? null;
}

export function selectContinuationSuggestions(
  runs: TaskRun[] | null | undefined,
  today: TodayTimeline | null | undefined,
  options: { now?: Date } = {},
): ContinuationSuggestion[] {
  const safeRuns = Array.isArray(runs)
    ? runs.filter((run) => Boolean(run.chat_id))
    : [];
  const now = options.now ?? new Date();
  const sortedRuns = [...safeRuns].sort(
    (left, right) => timestamp(right.started_at) - timestamp(left.started_at),
  );
  const resumable =
    sortedRuns.find((run) => RESUMABLE_STATUSES.has(run.status)) ??
    sortedRuns.find(
      (run) => run.source === "chat" && run.status !== "cancelled",
    );
  const result: ContinuationSuggestion[] = [];

  if (resumable) {
    result.push({
      id: `run:${resumable.id}`,
      kind: "run",
      title: resumable.title || "继续上次任务",
      summary:
        resumable.user_intent ||
        resumable.failure_reason ||
        "回到原任务继续推进",
      sourceLabel: RESUMABLE_STATUSES.has(resumable.status)
        ? "未完成会话"
        : "最近会话",
      timestamp:
        resumable.last_heartbeat_at ||
        resumable.ended_at ||
        resumable.started_at,
      actionPath: runPath(resumable),
    });
  }

  const todayItems = Array.isArray(today?.items) ? [...today.items] : [];
  todayItems.sort((left, right) => {
    const leftTime = timestamp(left.start_at);
    const rightTime = timestamp(right.start_at);
    const leftFuture = leftTime >= now.getTime() ? 0 : 1;
    const rightFuture = rightTime >= now.getTime() ? 0 : 1;
    return leftFuture - rightFuture || leftTime - rightTime;
  });
  const nextToday = todayItems.find(
    (item) => !["completed", "cancelled", "done"].includes(item.status),
  );
  if (nextToday) {
    result.push({
      id: `today:${nextToday.id}`,
      kind: "today",
      title: nextToday.title,
      summary: nextToday.summary || "查看今天的下一项安排",
      sourceLabel: nextToday.source_label || "今日",
      timestamp: nextToday.start_at,
      actionPath: nextToday.action_path || "/today",
    });
  }

  const artifactCandidate = latestArtifact(sortedRuns);
  if (artifactCandidate) {
    const { run, artifact } = artifactCandidate;
    result.push({
      id: `artifact:${artifact.id}`,
      kind: "artifact",
      title: artifact.title || "最近产物",
      summary: artifact.path || artifact.url || "回到来源任务查看产物",
      sourceLabel: "最近产物",
      timestamp: artifact.created_at,
      actionPath: runPath(run),
    });
  }

  return result.slice(0, 3);
}
