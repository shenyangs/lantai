import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronDown,
  Clipboard,
  ExternalLink,
  Loader2,
  MonitorPlay,
  Rocket,
  TerminalSquare,
  Wrench,
} from "lucide-react";
import {
  buildRegisteredToolDisplayName,
  buildRegisteredToolSummary,
  type ToolStatus,
} from "../toolRenderers/registry";
import { useLocalPreviewHealth } from "../hooks/useLocalPreviewHealth";
import { runtimeApi } from "../../../api/modules/runtime";
import type { PreviewHarnessReport } from "../../../api/types/runtime";
import {
  buildVibeCodingSignal,
  type VibeCodingSignal,
} from "../vibeCodingSignals";
import styles from "./ToolCallCard.module.less";

interface RuntimeToolContent {
  data?: {
    name?: string;
    server_label?: string;
    arguments?: unknown;
    output?: unknown;
    error?: unknown;
  };
}

interface RuntimeToolData {
  status?: string;
  content?: RuntimeToolContent[];
}

interface ToolCallCardProps {
  data?: RuntimeToolData;
}

interface CleanResult {
  summary: string;
  text: string;
}

const SUMMARY_LIMIT = 90;
const DETAILS_LIMIT = 20_000;

function tryParseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed) return value;
  if (!/^[{["]/.test(trimmed)) return value;

  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function stringifyValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") {
    const parsed = tryParseJson(value);
    if (parsed !== value) return stringifyValue(parsed);
    return value;
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function truncate(value: string, limit = SUMMARY_LIMIT): string {
  const text = normalizeWhitespace(value);
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

function readPathName(path?: unknown): string {
  if (typeof path !== "string") return "";
  const normalized = path.replace(/\\/g, "/");
  return normalized.split("/").filter(Boolean).pop() || path;
}

function unwrapContentParts(value: unknown): unknown {
  const parsed = tryParseJson(value);

  if (Array.isArray(parsed)) {
    const contentTexts = parsed
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const record = item as Record<string, unknown>;
        return typeof record.text === "string" ? record.text : null;
      })
      .filter((item): item is string => Boolean(item));

    if (contentTexts.length === parsed.length && contentTexts.length > 0) {
      const unwrapped = contentTexts.map((item) => tryParseJson(item));
      if (unwrapped.length === 1) return unwrapped[0];
      if (unwrapped.every(Array.isArray)) return unwrapped.flat();
      return unwrapped;
    }
  }

  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const record = parsed as Record<string, unknown>;
    if (typeof record.text === "string" && Object.keys(record).length <= 2) {
      return tryParseJson(record.text);
    }
  }

  return parsed;
}

function getStringField(
  record: Record<string, unknown>,
  keys: string[],
): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
  }
  return "";
}

function extractReadableText(value: unknown, depth = 0): string | null {
  if (depth > 4 || value === undefined || value === null) return null;

  const parsed = tryParseJson(value);
  if (typeof parsed === "string") return parsed;
  if (typeof parsed === "number" || typeof parsed === "boolean") {
    return String(parsed);
  }

  if (Array.isArray(parsed)) {
    const parts = parsed
      .map((item) => extractReadableText(item, depth + 1))
      .filter((item): item is string => Boolean(item));
    return parts.length ? parts.join("\n\n") : null;
  }

  if (parsed && typeof parsed === "object") {
    const record = parsed as Record<string, unknown>;
    const preferredKeys = [
      "text",
      "stdout",
      "stderr",
      "output",
      "result",
      "content",
      "message",
      "snippet",
      "summary",
      "title",
    ];

    for (const key of preferredKeys) {
      const text = extractReadableText(record[key], depth + 1);
      if (text) return text;
    }
  }

  return null;
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  const parsed = unwrapContentParts(value);
  if (!Array.isArray(parsed)) return [];
  return parsed.filter(
    (item): item is Record<string, unknown> =>
      !!item && typeof item === "object" && !Array.isArray(item),
  );
}

function looksLikeMemoryHit(record: Record<string, unknown>): boolean {
  return (
    "snippet" in record ||
    "score" in record ||
    "start_line" in record ||
    "end_line" in record
  );
}

function cleanSnippet(value: unknown): string {
  const raw = typeof value === "string" ? value : stringifyValue(value);
  const lines = raw
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.trimEnd());

  const cleaned: string[] = [];
  let inFrontmatter = false;
  let frontmatterClosed = false;
  let summary = "";

  for (const line of lines) {
    const trimmed = line.trim();
    if (!frontmatterClosed && trimmed === "---") {
      inFrontmatter = !inFrontmatter;
      if (!inFrontmatter) frontmatterClosed = true;
      continue;
    }

    if (inFrontmatter) {
      const summaryMatch = trimmed.match(/^summary:\s*(.+)$/);
      if (summaryMatch) {
        summary = summaryMatch[1].replace(/^["']|["']$/g, "");
      }
      continue;
    }

    if (!trimmed) {
      if (cleaned.length && cleaned[cleaned.length - 1] !== "") {
        cleaned.push("");
      }
      continue;
    }

    cleaned.push(line);
  }

  const body = cleaned
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  const text = [summary ? `摘要：${summary}` : "", body]
    .filter(Boolean)
    .join("\n\n");
  return text || raw.trim();
}

function formatMemoryResults(records: Record<string, unknown>[]): CleanResult {
  if (!records.length) return { summary: "未返回结果", text: "未返回结果" };

  const blocks = records.map((record, index) => {
    const source =
      getStringField(record, ["source"]) || readPathName(record.path);
    const startLine = getStringField(record, ["start_line", "startLine"]);
    const endLine = getStringField(record, ["end_line", "endLine"]);
    const lineRange =
      startLine && endLine
        ? `:${startLine}-${endLine}`
        : startLine
          ? `:${startLine}`
          : "";
    const heading = `[${index + 1}] ${source || "记忆命中"}${lineRange}`;
    const snippet = cleanSnippet(
      record.snippet || record.text || record.content,
    );
    return `${heading}\n${snippet}`;
  });

  const first = records[0];
  const firstSource =
    getStringField(first, ["source"]) || readPathName(first.path);

  return {
    summary: `返回 ${records.length} 条结果${firstSource ? ` · ${firstSource}` : ""}`,
    text: blocks.join("\n\n"),
  };
}

function looksLikeSearchRecord(record: Record<string, unknown>): boolean {
  return Boolean(
    getStringField(record, ["title", "name"]) ||
    getStringField(record, ["url", "link", "href"]) ||
    getStringField(record, ["snippet", "summary", "description"]),
  );
}

function formatSearchResults(records: Record<string, unknown>[]): CleanResult {
  const blocks = records.map((record, index) => {
    const title =
      getStringField(record, ["title", "name"]) || `结果 ${index + 1}`;
    const url = getStringField(record, ["url", "link", "href"]);
    const source = getStringField(record, ["source", "site", "domain"]);
    const role = getStringField(record, ["role", "category"]);
    const snippet = cleanSnippet(
      record.snippet ||
        record.summary ||
        record.description ||
        record.text ||
        record.content,
    );

    return [
      `[${index + 1}] ${title}`,
      url,
      source ? `来源：${source}` : "",
      role ? `角色：${role}` : "",
      snippet,
    ]
      .filter(Boolean)
      .join("\n");
  });

  const firstTitle = getStringField(records[0] || {}, ["title", "name"]);
  return {
    summary: `返回 ${records.length} 条结果${firstTitle ? ` · ${firstTitle}` : ""}`,
    text: blocks.join("\n\n"),
  };
}

function pickOutputObject(value: unknown): unknown {
  const parsed = unwrapContentParts(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return parsed;
  }

  const record = parsed as Record<string, unknown>;
  for (const key of ["result", "output", "content", "data", "text"]) {
    if (record[key] !== undefined) return unwrapContentParts(record[key]);
  }
  return parsed;
}

function formatGenericResult(output: unknown): CleanResult {
  const normalized = pickOutputObject(output);
  const text =
    extractReadableText(normalized) ||
    (Array.isArray(normalized)
      ? normalized.map((item) => stringifyValue(item)).join("\n\n")
      : stringifyValue(normalized));
  const cleaned = cleanSnippet(text);

  return {
    summary: cleaned ? truncate(cleaned) : "暂无输出",
    text: cleaned || "暂无输出",
  };
}

function buildCleanResult(
  toolName: string,
  output: unknown,
  loading: boolean,
): CleanResult {
  if (loading) return { summary: "等待结果", text: "等待结果" };
  if (
    output === undefined ||
    output === null ||
    stringifyValue(output).trim() === ""
  ) {
    return { summary: "暂无输出", text: "暂无输出" };
  }

  const records = asRecordArray(output);
  const lowerToolName = toolName.toLowerCase();
  if (
    records.length &&
    (lowerToolName.includes("memory") || records.every(looksLikeMemoryHit))
  ) {
    return formatMemoryResults(records);
  }

  if (records.length && records.every(looksLikeSearchRecord)) {
    return formatSearchResults(records);
  }

  return formatGenericResult(output);
}

function getToolName(data?: RuntimeToolData): string {
  return data?.content?.[0]?.data?.name || "tool";
}

function getToolTitle(data?: RuntimeToolData): string {
  const first = data?.content?.[0]?.data;
  const name = first?.name || "tool";
  const server = first?.server_label;
  return server ? `${server} / ${name}` : name;
}

function getStatus(
  data: RuntimeToolData | undefined,
  output: unknown,
): ToolStatus {
  const status = String(data?.status || "").toLowerCase();
  if (/(progress|running|queued|created|pending)/.test(status)) {
    return "running";
  }
  if (/(fail|error|cancel|reject|interrupt)/.test(status)) return "failed";
  if (data?.content?.[1]?.data?.error) return "failed";
  if (hasExplicitBusinessFailure(output)) return "failed";
  if (/(complete|succeed|success|done|finished)/.test(status)) return "done";
  if (output === undefined || output === null) return "pending";
  return "unknown";
}

function hasExplicitBusinessFailure(value: unknown, depth = 0): boolean {
  if (depth > 5) return false;
  const parsed = tryParseJson(value);
  if (parsed !== value) return hasExplicitBusinessFailure(parsed, depth + 1);
  if (Array.isArray(parsed)) {
    return parsed.some((item) => hasExplicitBusinessFailure(item, depth + 1));
  }
  if (!parsed || typeof parsed !== "object") return false;
  const record = parsed as Record<string, unknown>;
  if (record.success === false || record.ok === false) return true;
  return [record.result, record.output, record.data].some((item) =>
    hasExplicitBusinessFailure(item, depth + 1),
  );
}

function getStatusLabel(status: ToolStatus): string {
  switch (status) {
    case "running":
      return "执行中";
    case "failed":
      return "未完成";
    case "pending":
      return "等待结果";
    case "unknown":
      return "结果待核实";
    default:
      return "已完成";
  }
}

function getStatusToneLabel(status: ToolStatus): string {
  switch (status) {
    case "running":
      return "正在执行";
    case "failed":
      return "可查看详情";
    case "pending":
      return "已发起";
    case "unknown":
      return "状态未知";
    default:
      return "已归档";
  }
}

function trimDetailText(text: string): string {
  if (text.length <= DETAILS_LIMIT) return text;
  return `${text.slice(0, DETAILS_LIMIT)}\n\n...已截断预览，复制可获取当前完整内容。`;
}

function getToolArguments(data?: RuntimeToolData): unknown {
  return data?.content?.[0]?.data?.arguments;
}

function buildToolContextSummary(
  toolName: string,
  args: unknown,
  status: ToolStatus,
  argsText: string,
  resultText: string,
): string {
  return buildRegisteredToolSummary({
    toolName,
    args,
    status,
    argsText,
    resultText,
  });
}

function hasReadableDetail(text: string): boolean {
  const value = text.trim();
  return Boolean(value && value !== "{}" && value !== "[]");
}

function buildProcessSummary(
  title: string,
  status: ToolStatus,
  cleanResult: CleanResult,
): string {
  if (status === "running") {
    return `${title} 正在执行，当前等待工具返回结果。`;
  }
  if (status === "failed") {
    return `${title} 这一步没有完成，详细返回已保留；任务可以继续尝试其他方式。`;
  }
  if (status === "pending") {
    return `${title} 已发起，当前还没有收到可展示的返回。`;
  }
  if (status === "unknown") {
    return `${title} 返回内容已保留，但没有权威终态；核实后再决定是否重试。`;
  }
  return `${title} 已完成。${cleanResult.summary}`;
}

function CopyableBlock({
  label,
  text,
  copyLabel,
}: {
  label: string;
  text: string;
  copyLabel: string;
}) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const preview = trimDetailText(text || "-");

  const handleCopy = async () => {
    try {
      if (
        !navigator.clipboard ||
        typeof navigator.clipboard.writeText !== "function"
      ) {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(text || "");
      setCopyError("");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
      setCopyError("无法访问剪贴板，请手动选择文本复制。");
    }
  };

  return (
    <section className={styles.detailBlock}>
      <div className={styles.detailHeader}>
        <span>{label}</span>
        <button
          type="button"
          className={styles.copyButton}
          onClick={handleCopy}
          aria-label={copyLabel}
          disabled={!text}
        >
          {copied ? <Check size={14} /> : <Clipboard size={14} />}
        </button>
      </div>
      <pre className={styles.detailContent}>{preview}</pre>
      {copyError ? (
        <p className={styles.copyError} role="alert">
          {copyError}
        </p>
      ) : null}
    </section>
  );
}

function healthLabel(health: ReturnType<typeof useLocalPreviewHealth>): string {
  switch (health) {
    case "checking":
      return "预览健康：检查中";
    case "reachable":
      return "预览健康：可访问";
    case "unreachable":
      return "预览健康：暂未就绪";
    default:
      return "预览健康：等待展开后检查";
  }
}

function healthClass(health: ReturnType<typeof useLocalPreviewHealth>): string {
  switch (health) {
    case "checking":
      return styles.vibeSignalChecking;
    case "reachable":
      return styles.vibeSignalReachable;
    case "unreachable":
      return styles.vibeSignalUnreachable;
    default:
      return "";
  }
}

function previewHarnessDetail(report?: PreviewHarnessReport | null): string {
  if (!report) return "";
  const lines = [
    report.summary,
    report.findings?.[0] ? `首个发现：${report.findings[0].message}` : "",
    report.findings?.[0]?.evidence
      ? `证据：${report.findings[0].evidence}`
      : "",
    report.console_errors?.[0] ? `控制台首错：${report.console_errors[0]}` : "",
    report.page_errors?.[0] ? `页面异常：${report.page_errors[0]}` : "",
    report.network_errors?.[0]
      ? `网络失败：${report.network_errors[0].url} ${
          report.network_errors[0].status ?? ""
        } ${report.network_errors[0].error || ""}`.trim()
      : "",
    report.white_screen_likely ? "渲染判断：页面可能白屏" : "",
  ];
  return lines.filter(Boolean).join("\n");
}

function postPreviewHarnessBackfeed(
  signal: VibeCodingSignal,
  report?: PreviewHarnessReport | null,
) {
  window.postMessage(
    {
      type: "preview:runtime:error",
      source: report ? "preview-harness" : "local-preview-health",
      summary: report?.summary || `本地预览暂不可达：${signal.label}`,
      message:
        report?.summary ||
        "本地预览健康检查失败，可能是 dev server 未启动、端口不一致或页面启动失败。",
      detail: previewHarnessDetail(report),
      url: signal.url,
      consoleErrors: report?.console_errors || [],
      networkErrors: report?.network_errors || [
        {
          url: signal.url,
          error: "preview health check unreachable",
        },
      ],
    },
    window.location.origin,
  );
}

function VibeSignalRow({
  signal,
  health,
}: {
  signal: VibeCodingSignal;
  health: ReturnType<typeof useLocalPreviewHealth>;
}) {
  const Icon = signal.kind === "preview" ? MonitorPlay : Rocket;
  const [inspection, setInspection] = useState<PreviewHarnessReport | null>(
    null,
  );
  const label =
    signal.kind === "preview" && inspection && !inspection.ok
      ? `预览诊断：${inspection.summary}`
      : signal.kind === "preview"
        ? healthLabel(health)
        : "部署闭环：已识别公开链接";
  const reportedPreviewFailureRef = useRef("");

  useEffect(() => {
    if (signal.kind !== "preview" || health !== "unreachable") return;
    if (reportedPreviewFailureRef.current === signal.url) return;
    reportedPreviewFailureRef.current = signal.url;

    let cancelled = false;
    void runtimeApi
      .inspectPreview({
        url: signal.url,
        record_event: false,
      })
      .then((report) => {
        if (cancelled) return;
        setInspection(report);
        postPreviewHarnessBackfeed(signal, report);
      })
      .catch(() => {
        if (cancelled) return;
        postPreviewHarnessBackfeed(signal);
      });

    return () => {
      cancelled = true;
    };
  }, [health, signal.kind, signal.label, signal.url]);

  return (
    <section className={styles.processStep}>
      <span className={styles.stepMarker} aria-hidden="true" />
      <div className={styles.stepBody}>
        <div className={styles.stepTitle}>
          {signal.kind === "preview" ? "预览检查" : "部署结果"}
        </div>
        <div className={`${styles.vibeSignal} ${healthClass(health)}`}>
          <Icon size={14} aria-hidden="true" />
          <span>{label}</span>
          <a href={signal.url} target="_blank" rel="noreferrer">
            {signal.label}
            <ExternalLink size={12} aria-hidden="true" />
          </a>
        </div>
      </div>
    </section>
  );
}

export default function ToolCallCard({ data }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const toolName = getToolName(data);
  const args = getToolArguments(data);
  const output = data?.content?.[1]?.data?.output;
  const error = data?.content?.[1]?.data?.error;
  const resultPayload = output ?? error;
  const status = getStatus(data, output);
  const loading = status === "running";
  const argsText = stringifyValue(args);
  const showArgs = hasReadableDetail(argsText);
  const title =
    buildRegisteredToolDisplayName({
      toolName,
      args,
      status,
      argsText,
    }) || getToolTitle(data);

  useEffect(() => {
    setExpanded(false);
  }, [title]);

  const cleanResult = useMemo(
    () => buildCleanResult(toolName, resultPayload, loading),
    [loading, resultPayload, toolName],
  );

  const vibeSignal = useMemo(
    () =>
      buildVibeCodingSignal({
        toolName,
        args,
        argsText,
        resultText: cleanResult.text,
      }),
    [args, argsText, cleanResult.text, toolName],
  );
  const previewHealth = useLocalPreviewHealth(
    expanded && vibeSignal?.kind === "preview" ? vibeSignal.url : null,
  );
  const toolContextSummary = buildToolContextSummary(
    toolName,
    args,
    status,
    argsText,
    cleanResult.text,
  );

  const processSummary = useMemo(
    () => buildProcessSummary(title, status, cleanResult),
    [cleanResult, status, title],
  );

  const statusClass =
    status === "running"
      ? styles.running
      : status === "failed"
        ? styles.failed
        : status === "pending" || status === "unknown"
          ? styles.pending
          : styles.done;

  return (
    <div
      className={`${styles.toolCallCard} ${statusClass}`}
      data-chat-tool-call="1"
      data-tool-status={status}
    >
      <button
        type="button"
        className={styles.summaryButton}
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-label={`${expanded ? "收起" : "展开"}工具调用 ${title}`}
      >
        <span className={styles.iconWrap} aria-hidden="true">
          {status === "running" ? (
            <Loader2 className={styles.spin} size={15} />
          ) : status === "failed" ? (
            <AlertCircle size={15} />
          ) : (
            <Wrench size={15} />
          )}
        </span>
        <span className={styles.summaryText}>
          <span className={styles.titleLine}>
            <span className={styles.processLabel}>执行轨迹</span>
            <span className={styles.toolName}>{title}</span>
            <span
              className={styles.status}
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {getStatusLabel(status)}
            </span>
          </span>
          <span className={styles.previewLine}>
            <span>{toolContextSummary || getStatusToneLabel(status)}</span>
            <span className={styles.previewDivider}>·</span>
            {cleanResult.summary}
          </span>
        </span>
        <ChevronDown
          size={16}
          className={`${styles.chevron} ${expanded ? styles.chevronOpen : ""}`}
          aria-hidden="true"
        />
      </button>

      {expanded && (
        <div className={styles.details}>
          <section className={styles.processStep}>
            <span className={styles.stepMarker} aria-hidden="true" />
            <div className={styles.stepBody}>
              <div className={styles.stepTitle}>过程摘要</div>
              <p className={styles.processSummary}>{processSummary}</p>
            </div>
          </section>

          <section className={styles.processStep}>
            <span className={styles.stepMarker} aria-hidden="true" />
            <div className={styles.stepBody}>
              <div className={styles.stepTitle}>工具调用</div>
              <div className={styles.toolInvokeLine}>
                <TerminalSquare size={14} aria-hidden="true" />
                <code>{toolName}</code>
                {title !== toolName && <span>{title}</span>}
                <em>{getStatusLabel(status)}</em>
              </div>
            </div>
          </section>

          {vibeSignal && (
            <VibeSignalRow signal={vibeSignal} health={previewHealth} />
          )}

          {showArgs && (
            <CopyableBlock
              label="调用详情"
              text={argsText}
              copyLabel="复制调用详情"
            />
          )}

          <CopyableBlock
            label="返回结果"
            text={cleanResult.text}
            copyLabel="复制返回结果"
          />
        </div>
      )}
    </div>
  );
}
