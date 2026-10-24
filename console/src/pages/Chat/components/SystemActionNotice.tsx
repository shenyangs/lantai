import { useMemo, useState } from "react";
import { Markdown } from "@/vendor/lantaiChat";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  Circle,
  Info,
  LoaderCircle,
} from "lucide-react";

import { structuredMarkdownComponents } from "./StructuredContent";
import type {
  SystemActionPresentation,
  SystemActionStatus,
} from "./systemActionPresentation";

function stripEmptySummarySections(summary: string): string {
  const lines = summary
    .replace(/-\s*Summary task started in background\s*$/i, "")
    .trim()
    .split("\n");
  const result: string[] = [];

  for (let index = 0; index < lines.length; ) {
    const heading = lines[index];
    if (!/^#{1,6}\s+\S/.test(heading)) {
      result.push(heading);
      index += 1;
      continue;
    }
    let end = index + 1;
    while (end < lines.length && !/^#{1,6}\s+\S/.test(lines[end])) {
      end += 1;
    }
    const body = lines
      .slice(index + 1, end)
      .join("\n")
      .trim();
    if (!/^(?:[（(]?\s*(?:无|none|n\/a)\s*[）)]?|-)?$/i.test(body)) {
      result.push(...lines.slice(index, end));
    }
    index = end;
  }
  return result.join("\n").trim();
}

function formatTokenCount(value: number): string {
  if (value >= 1000) {
    const rounded = value / 1000;
    return `${rounded >= 10 ? rounded.toFixed(0) : rounded.toFixed(1)}k`;
  }
  return String(value);
}

function ActionIcon({ status }: { status: SystemActionStatus }) {
  if (status === "failed" || status === "degraded") {
    return <AlertCircle size={17} aria-hidden="true" />;
  }
  if (status === "running") {
    return (
      <LoaderCircle
        size={17}
        className="lantai-system-action-spinner"
        aria-hidden="true"
      />
    );
  }
  if (status === "completed") {
    return <CheckCircle2 size={17} aria-hidden="true" />;
  }
  return <Info size={17} aria-hidden="true" />;
}

function StepIcon({
  status,
}: {
  status: NonNullable<SystemActionPresentation["steps"]>[number]["status"];
}) {
  if (status === "completed") {
    return <CheckCircle2 size={14} aria-hidden="true" />;
  }
  if (status === "failed") return <AlertCircle size={14} aria-hidden="true" />;
  if (status === "running") {
    return (
      <LoaderCircle
        size={14}
        className="lantai-system-action-spinner"
        aria-hidden="true"
      />
    );
  }
  return <Circle size={12} aria-hidden="true" />;
}

export function SystemActionNotice({
  action,
}: {
  action: SystemActionPresentation;
}) {
  const displaySummary = useMemo(
    () => (action.summary ? stripEmptySummarySections(action.summary) : ""),
    [action.summary],
  );
  const [expanded, setExpanded] = useState(
    action.kind === "compressed_summary" && Boolean(displaySummary),
  );
  const metrics = [
    typeof action.messageCount === "number"
      ? `${action.messageCount} 条消息`
      : "",
    typeof action.beforeTokens === "number" &&
    typeof action.afterTokens === "number"
      ? `${formatTokenCount(action.beforeTokens)} → ${formatTokenCount(
          action.afterTokens,
        )} Token`
      : "",
    typeof action.savedPercent === "number" && action.savedPercent > 0
      ? `减少 ${action.savedPercent}%`
      : "",
  ].filter(Boolean);

  return (
    <section
      className="lantai-system-action"
      data-lantai-system-action={action.kind}
      data-status={action.status}
      role={
        action.status === "failed" || action.status === "degraded"
          ? "alert"
          : "status"
      }
      aria-live={action.status === "running" ? "polite" : undefined}
    >
      <div className="lantai-system-action-main">
        <span className="lantai-system-action-icon">
          <ActionIcon status={action.status} />
        </span>
        <span className="lantai-system-action-copy">
          <strong>{action.title}</strong>
          {action.detail ? <span>{action.detail}</span> : null}
        </span>
        {metrics.length > 0 ? (
          <span className="lantai-system-action-metrics">
            {metrics.map((metric) => (
              <span key={metric}>{metric}</span>
            ))}
          </span>
        ) : null}
        {displaySummary ? (
          <button
            type="button"
            className="lantai-system-action-toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            {expanded ? "收起摘要" : "查看摘要"}
            <ChevronDown size={14} aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {action.steps?.length ? (
        <ol className="lantai-system-action-steps">
          {action.steps.map((step) => (
            <li key={step.id} data-status={step.status}>
              <span className="lantai-system-action-step-icon">
                <StepIcon status={step.status} />
              </span>
              <span className="lantai-system-action-step-copy">
                <strong>{step.title}</strong>
                {step.detail ? <span>{step.detail}</span> : null}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
      {expanded && displaySummary ? (
        <div className="lantai-system-action-details">
          <Markdown
            content={displaySummary}
            components={structuredMarkdownComponents}
          />
        </div>
      ) : null}
      {(action.status === "failed" || action.status === "degraded") &&
      action.technicalReason ? (
        <details className="lantai-system-action-technical">
          <summary>技术详情</summary>
          <code>{action.technicalReason}</code>
        </details>
      ) : null}
    </section>
  );
}
