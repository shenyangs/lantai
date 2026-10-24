import {
  AlertCircle,
  CheckCircle2,
  Circle,
  FlaskConical,
  Loader2,
  Repeat2,
  Route,
  ShieldCheck,
} from "lucide-react";

import styles from "./WorkMethodProjectionCard.module.less";

type WorkMethodId = "deep_research" | "sustained_execution";

interface ProjectionEvent {
  id?: string;
  seq?: number;
  type?: string;
  status?: string;
  source?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
}

interface WorkMethodProjectionCardProps {
  methodId?: string;
  events?: unknown;
  generating: boolean;
}

interface StageDefinition {
  id: string;
  label: string;
}

const STAGES: Record<WorkMethodId, StageDefinition[]> = {
  deep_research: [
    { id: "framing", label: "立题" },
    { id: "collecting", label: "取证" },
    { id: "verifying", label: "核验" },
    { id: "synthesizing", label: "结论" },
  ],
  sustained_execution: [
    { id: "planning", label: "拆解" },
    { id: "executing", label: "推进" },
    { id: "verifying", label: "验收" },
    { id: "closing", label: "收口" },
  ],
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(source: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function normalizeEvents(value: unknown): ProjectionEvent[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter(
      (item): item is ProjectionEvent =>
        Boolean(item) && typeof item === "object" && !Array.isArray(item),
    )
    .sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0));
}

function isTerminal(event: ProjectionEvent): boolean {
  return [
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
    "turn.stalled",
  ].includes(String(event.type || ""));
}

function isFailure(event: ProjectionEvent): boolean {
  return (
    String(event.status || "").toLowerCase() === "failed" ||
    String(event.type || "").includes("failed") ||
    String(event.type || "") === "runtime.error"
  );
}

function stageIndex(methodId: WorkMethodId, events: ProjectionEvent[]): number {
  if (events.some(isTerminal)) return 3;
  const explicit = [...events]
    .reverse()
    .filter((event) => event.type !== "turn.started")
    .map((event) => text(record(event.payload), "phase"))
    .find((phase) => STAGES[methodId].some((stage) => stage.id === phase));
  if (explicit) {
    return STAGES[methodId].findIndex((stage) => stage.id === explicit);
  }
  const hasTool = events.some((event) =>
    String(event.type || "").startsWith("tool."),
  );
  const toolCompleted = events.filter((event) =>
    ["tool.completed", "tool.call.completed"].includes(
      String(event.type || ""),
    ),
  ).length;
  const messageAfterTool = hasTool
    ? events.some(
        (event) =>
          Number(event.seq || 0) >
            Math.max(
              ...events
                .filter((item) => String(item.type || "").startsWith("tool."))
                .map((item) => Number(item.seq || 0)),
            ) && String(event.type || "").startsWith("message."),
      )
    : false;
  if (methodId === "deep_research") {
    if (messageAfterTool) return 3;
    if (toolCompleted >= 2) return 2;
    if (hasTool) return 1;
    return 0;
  }
  if (
    messageAfterTool ||
    events.some((event) => event.type === "item.completed")
  ) {
    return 2;
  }
  if (hasTool) return 1;
  return 0;
}

function terminalCopy(events: ProjectionEvent[]): {
  label: string;
  tone: "success" | "danger" | "neutral";
} | null {
  const terminal = [...events].reverse().find(isTerminal);
  if (!terminal) return null;
  if (terminal.type === "turn.completed" && !isFailure(terminal)) {
    const outcome = text(record(terminal.payload), "outcome_status");
    return outcome === "incomplete"
      ? { label: "部分完成，证据或验收仍有缺口", tone: "neutral" }
      : { label: "已完成并形成可交付结果", tone: "success" };
  }
  if (terminal.type === "turn.interrupted") {
    return { label: "已停止，已完成进度仍被保留", tone: "neutral" };
  }
  if (terminal.type === "turn.stalled") {
    return { label: "需要输入或条件恢复后继续", tone: "neutral" };
  }
  return { label: "未形成可靠结果，已明确结束", tone: "danger" };
}

function latestSummary(events: ProjectionEvent[]): string {
  for (const event of [...events].reverse()) {
    const payload = record(event.payload);
    const value = text(
      payload,
      "trace_detail",
      "trace_title",
      "failure_reason",
      "next_action",
      "summary",
      "detail",
    );
    if (value) return value;
  }
  return "已接收任务，正在建立本轮执行上下文。";
}

export default function WorkMethodProjectionCard({
  methodId,
  events: rawEvents,
  generating,
}: WorkMethodProjectionCardProps) {
  if (methodId !== "deep_research" && methodId !== "sustained_execution") {
    return null;
  }
  const events = normalizeEvents(rawEvents);
  const stages = STAGES[methodId];
  const current = stageIndex(methodId, events);
  const terminal = terminalCopy(events);
  const failures = events.filter(isFailure).length;
  const completedActions = events.filter((event) =>
    ["tool.completed", "tool.call.completed", "item.completed"].includes(
      String(event.type || ""),
    ),
  ).length;
  const retryCount = events.filter((event) => {
    const payload = record(event.payload);
    return (
      String(event.type || "") === "turn.continue_requested" &&
      text(payload, "mode") === "retry"
    );
  }).length;
  const Icon = methodId === "deep_research" ? FlaskConical : Route;
  const title = methodId === "deep_research" ? "深度研究" : "持续执行";
  const subtitle =
    methodId === "deep_research"
      ? "先取证与交叉核验，再形成结论"
      : "按检查点持续推进，失败时保留进度并调整策略";

  return (
    <section
      className={styles.card}
      data-testid={`work-method-projection-${methodId}`}
      aria-label={`${title}运行投影`}
    >
      <header className={styles.header}>
        <span className={styles.methodIcon} aria-hidden="true">
          <Icon size={17} />
        </span>
        <span className={styles.heading}>
          <strong>{title}</strong>
          <em>{subtitle}</em>
        </span>
        <span
          className={styles.liveState}
          data-terminal={terminal ? terminal.tone : undefined}
          role="status"
        >
          {terminal ? (
            terminal.tone === "success" ? (
              <CheckCircle2 size={14} aria-hidden="true" />
            ) : (
              <AlertCircle size={14} aria-hidden="true" />
            )
          ) : generating ? (
            <Loader2 className={styles.spin} size={14} aria-hidden="true" />
          ) : (
            <Circle size={13} aria-hidden="true" />
          )}
          {terminal?.label || (generating ? "运行中" : "已记录")}
        </span>
      </header>

      <ol className={styles.rail} aria-label={`${title}阶段`}>
        {stages.map((stage, index) => (
          <li
            key={stage.id}
            data-state={
              index < current ? "done" : index === current ? "active" : "next"
            }
          >
            <span aria-hidden="true">
              {index < current ? <CheckCircle2 size={13} /> : index + 1}
            </span>
            <em>{stage.label}</em>
          </li>
        ))}
      </ol>

      <div className={styles.currentBlock}>
        <small>当前进展</small>
        <p>{latestSummary(events)}</p>
      </div>

      <footer className={styles.facts}>
        <span>
          <ShieldCheck size={13} aria-hidden="true" />
          {completedActions > 0
            ? `已完成 ${completedActions} 项可追踪动作`
            : "等待首项可追踪动作"}
        </span>
        {failures > 0 ? (
          <span data-tone="danger">
            <AlertCircle size={13} aria-hidden="true" />
            {failures} 项异常已记录
          </span>
        ) : null}
        {methodId === "sustained_execution" && retryCount > 0 ? (
          <span>
            <Repeat2 size={13} aria-hidden="true" />
            已开启 {retryCount} 次新尝试
          </span>
        ) : null}
      </footer>
    </section>
  );
}
