import { useMemo, useState } from "react";
import { Button } from "antd";
import { AlertCircle, CheckCircle2, Loader2, MonitorCog } from "lucide-react";
import styles from "./MacUseToolCard.module.less";

interface RuntimeToolContent {
  data?: { arguments?: unknown; output?: unknown; error?: unknown };
}

interface RuntimeToolData {
  status?: string;
  content?: RuntimeToolContent[];
}

interface MacUsePayload {
  ok?: boolean;
  action?: string;
  run_id?: string;
  message?: string;
  error?: string;
  error_code?: string;
  dispatch_status?: string;
  effect_status?: string;
  observation_id?: string;
  data?: {
    windows?: unknown[];
    screenshot_path?: string;
    [key: string]: unknown;
  };
  evidence?: Array<{ kind?: string; path?: string }>;
}

interface RuntimeWindow extends Window {
  currentSessionId?: string;
}

declare const window: RuntimeWindow;

export function isMacUseToolName(name: string): boolean {
  return [
    "mac_inspect",
    "mac_observe",
    "mac_act",
    "mac_verify",
    "mac_session",
  ].includes(String(name || "").trim());
}

function parse(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function payloadFrom(data?: RuntimeToolData): MacUsePayload | null {
  const raw = parse(
    data?.content?.[1]?.data?.output ?? data?.content?.[1]?.data?.error,
  );
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const record = raw as Record<string, unknown>;
    if (Array.isArray(record.content)) {
      const text = (record.content as Array<Record<string, unknown>>).find(
        (item) => typeof item?.text === "string",
      )?.text;
      const nested = parse(text);
      if (nested && typeof nested === "object" && !Array.isArray(nested)) {
        return nested as MacUsePayload;
      }
    }
    return record as MacUsePayload;
  }
  return null;
}

function argumentsFrom(data?: RuntimeToolData): Record<string, unknown> {
  const parsed = parse(data?.content?.[0]?.data?.arguments);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : {};
}

function actionLabel(action: string): string {
  return (
    {
      status: "检查应用操作能力",
      list_windows: "查看可用应用",
      observe_window: "读取应用窗口",
      focus_window: "切到目标应用",
      click_at: "操作目标位置",
      type_text: "输入文字",
      press_key: "发送按键",
      set_value: "填写内容",
      press_element: "操作控件",
      stop: "停止应用操作",
      pause: "暂停应用操作",
      resume: "继续应用操作",
      session_status: "检查应用操作状态",
    }[action] || "操作 Mac 应用"
  );
}

function effectCopy(payload: MacUsePayload | null, running: boolean) {
  if (running)
    return {
      label: "进行中",
      tone: "default",
      summary: "正在执行并等待真实结果",
    };
  if (!payload)
    return {
      label: "未完成",
      tone: "error",
      summary: "没有收到可核验的执行结果",
    };
  if (payload.effect_status === "verified") {
    return {
      label: "已核验",
      tone: "success",
      summary: payload.message || "应用状态已读回确认",
    };
  }
  if (payload.effect_status === "ambiguous") {
    return {
      label: "待核验",
      tone: "warning",
      summary: payload.message || "动作可能已经发生，不能直接重复",
    };
  }
  if (payload.ok && payload.effect_status === "not_applicable") {
    const count = payload.data?.windows?.length;
    return {
      label: "已读取",
      tone: "success",
      summary:
        typeof count === "number"
          ? `找到 ${count} 个可选窗口`
          : payload.message || "读取完成",
    };
  }
  return {
    label: "未执行",
    tone: "error",
    summary: payload.error || payload.message || "操作没有完成",
  };
}

export default function MacUseToolCard({
  data,
  onRequestTarget,
}: {
  data?: RuntimeToolData;
  onRequestTarget?: () => void;
}) {
  const [showDetails, setShowDetails] = useState(false);
  const rawStatus = String(data?.status || "").toLowerCase();
  const running =
    rawStatus.includes("running") || rawStatus.includes("progress");
  const payload = useMemo(() => payloadFrom(data), [data]);
  const args = useMemo(() => argumentsFrom(data), [data]);
  const effect = effectCopy(payload, running);
  const action = payload?.action || String(args.operation || args.action || "");
  const runId =
    payload?.run_id || String(args.run_id || window.currentSessionId || "");
  const screenshotPath =
    payload?.data?.screenshot_path ||
    payload?.evidence?.find((item) => item.kind === "window_screenshot")?.path;

  return (
    <section className={styles.card} aria-label="Mac 应用操作">
      <div className={styles.header}>
        <div className={styles.identity}>
          {running ? (
            <Loader2 size={20} className="spin" aria-hidden="true" />
          ) : payload?.ok ? (
            <CheckCircle2 size={20} aria-hidden="true" />
          ) : (
            <AlertCircle size={20} aria-hidden="true" />
          )}
          <span className={styles.identityCopy}>
            <strong>{actionLabel(action)}</strong>
            <span>{effect.summary}</span>
          </span>
        </div>
        <span
          className={styles.status}
          data-tone={effect.tone}
          aria-live="polite"
        >
          {effect.label}
        </span>
      </div>
      <div className={styles.actions}>
        {!running && payload?.ok && action === "status" && onRequestTarget ? (
          <Button size="small" type="primary" onClick={onRequestTarget}>
            选择要操作的应用
          </Button>
        ) : null}
        {running && runId && window.electronAPI?.stopMacUse ? (
          <Button
            size="small"
            danger
            onClick={() => void window.electronAPI?.stopMacUse?.(runId)}
          >
            停止
          </Button>
        ) : null}
        {screenshotPath && window.electronAPI?.openPath ? (
          <Button
            size="small"
            icon={<MonitorCog size={14} />}
            onClick={() => void window.electronAPI?.openPath?.(screenshotPath)}
          >
            查看证据
          </Button>
        ) : null}
        {payload ? (
          <Button
            size="small"
            type="text"
            onClick={() => setShowDetails((value) => !value)}
          >
            {showDetails ? "收起详情" : "查看详情"}
          </Button>
        ) : null}
      </div>
      {showDetails && payload ? (
        <div className={styles.details}>
          结果会区分是否发出动作和是否核验成功。
          <pre>{JSON.stringify(payload, null, 2)}</pre>
        </div>
      ) : null}
    </section>
  );
}
