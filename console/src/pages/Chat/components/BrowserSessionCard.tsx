import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  CircleStop,
  ExternalLink,
  Globe2,
  Loader2,
  Pause,
  Play,
} from "lucide-react";
import { browserWorkbenchApi } from "../../../api/modules/browserSession";
import type { BrowserTaskEvent, BrowserTaskRun } from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import styles from "./BrowserSessionCard.module.less";

interface RuntimeToolContent {
  data?: {
    name?: string;
    arguments?: unknown;
    output?: unknown;
    error?: unknown;
  };
}

interface RuntimeToolData {
  status?: string;
  content?: RuntimeToolContent[];
}

export interface BrowserSessionTarget {
  runId: string;
  profileId: string;
  url: string;
  instruction: string;
  task: BrowserTaskRun | null;
  ownerChatId?: string;
}

interface BrowserSessionCardProps {
  data?: RuntimeToolData;
  onInspect?: (target: BrowserSessionTarget) => void;
  autoOpen?: boolean;
}

const TASK_STATUS_LABELS: Record<string, string> = {
  queued: "准备中",
  running_background: "浏览中",
  waiting_for_user_confirmation: "等待确认",
  manual_takeover_required: "需要接管",
  paused: "已暂停",
  completed: "已完成",
  failed: "未完成",
  cancelled: "已停止",
};

const ACTION_LABELS: Record<string, string> = {
  open: "打开页面",
  navigate: "前往页面",
  snapshot: "读取页面",
  screenshot: "保存截图",
  browser_find: "查找页面内容",
  click: "点击页面",
  type: "输入内容",
  fill: "填写内容",
  select: "选择选项",
  scroll: "浏览更多内容",
  upload_file: "上传文件",
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

function parseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text) return value;
  try {
    return JSON.parse(text);
  } catch {
    return value;
  }
}

function unwrapToolValue(value: unknown): unknown {
  const parsed = parseJson(value);
  if (Array.isArray(parsed)) {
    const textBlocks = parsed
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const text = (item as Record<string, unknown>).text;
        return typeof text === "string" ? text : null;
      })
      .filter((item): item is string => Boolean(item));
    if (textBlocks.length === parsed.length && textBlocks.length === 1) {
      return unwrapToolValue(textBlocks[0]);
    }
  }
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const record = parsed as Record<string, unknown>;
    if (typeof record.text === "string" && Object.keys(record).length <= 2) {
      return unwrapToolValue(record.text);
    }
  }
  return parsed;
}

function asRecord(value: unknown): Record<string, unknown> {
  const parsed = unwrapToolValue(value);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : {};
}

function readTask(payload: Record<string, unknown>): BrowserTaskRun | null {
  const candidate = asRecord(payload.task);
  return candidate.run_id && candidate.status
    ? (candidate as unknown as BrowserTaskRun)
    : null;
}

function domainFromUrl(value: string): string {
  if (!value.trim()) return "";
  try {
    return new URL(value).hostname;
  } catch {
    return value.replace(/^https?:\/\//, "").split("/")[0] || "";
  }
}

function humanEventLabel(event: BrowserTaskEvent): string {
  const action = ACTION_LABELS[event.action] || "处理页面";
  const labels: Record<string, string> = {
    task_created: "任务已开始",
    task_started: "Agent 已打开浏览器",
    action_started: `正在${action}`,
    action_completed: `${action}完成`,
    action_failed: `${action}失败`,
    agent_observing: "正在查看页面",
    agent_planning: "正在决定下一步",
    agent_checking_policy: "正在检查操作权限",
    agent_executing: `正在${action}`,
    agent_verifying: "正在确认页面结果",
    agent_recovering: "遇到阻碍，正在尝试其他方法",
    confirmation_requested: "需要你确认下一步",
    manual_takeover_required: "需要你接管浏览器",
    task_paused: "任务已暂停",
    task_resumed: "Agent 已继续浏览",
    task_completed: "任务已完成",
    task_failed: "任务未完成",
    task_cancelled: "任务已停止",
  };
  return labels[event.type] || TASK_STATUS_LABELS[event.status] || "正在浏览";
}

function fallbackActionLabel(action: string, toolStatus: string): string {
  const label = ACTION_LABELS[action] || "浏览页面";
  if (toolStatus === "failed") return `${label}未完成`;
  if (toolStatus === "completed") return `${label}完成`;
  return `正在${label}`;
}

export default function BrowserSessionCard({
  data,
  onInspect,
  autoOpen = false,
}: BrowserSessionCardProps) {
  const { message } = useAppMessage();
  const toolName = String(data?.content?.[0]?.data?.name || "browser_task");
  const args = useMemo(
    () => asRecord(data?.content?.[0]?.data?.arguments),
    [data?.content],
  );
  const payload = useMemo(
    () =>
      asRecord(
        data?.content?.[1]?.data?.output ?? data?.content?.[1]?.data?.error,
      ),
    [data?.content],
  );
  const initialTask = useMemo(() => readTask(payload), [payload]);
  const [task, setTask] = useState<BrowserTaskRun | null>(initialTask);
  const [lastActivity, setLastActivity] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const autoOpenedRef = useRef("");

  const runId = String(task?.run_id || args.run_id || "");
  const profileId = String(task?.profile_id || args.profile_id || "default");
  const url = String(args.url || task?.metadata?.current_url || "");
  const instruction = String(
    task?.instruction ||
      args.instruction ||
      fallbackActionLabel(String(args.action || ""), ""),
  );
  const taskStatus = String(task?.status || data?.status || "running");
  const statusLabel = task
    ? TASK_STATUS_LABELS[task.status] || "浏览中"
    : data?.status === "failed"
      ? "未完成"
      : data?.status === "completed"
        ? "已完成"
        : "浏览中";
  const domain = domainFromUrl(url) || task?.target_domains?.[0] || "当前页面";
  const currentActivity = task
    ? lastActivity ||
      ACTION_LABELS[task.current_action] ||
      (task.status === "running_background" ? "正在浏览页面" : statusLabel)
    : fallbackActionLabel(
        String(args.action || ""),
        String(data?.status || ""),
      );
  const canPause = Boolean(
    task && ["queued", "running_background"].includes(task.status),
  );
  const canResume = Boolean(
    task && ["paused", "manual_takeover_required"].includes(task.status),
  );
  const canStop = Boolean(task && !TERMINAL_STATUSES.has(task.status));

  useEffect(() => {
    setTask(initialTask);
  }, [initialTask]);

  useEffect(() => {
    if (!runId || toolName !== "browser_task") return undefined;
    let active = true;
    const refresh = () =>
      browserWorkbenchApi
        .getTask(runId)
        .then((next) => {
          if (active) setTask(next);
        })
        .catch(() => undefined);
    void refresh();
    const unsubscribe = browserWorkbenchApi.subscribeTaskEvents(
      runId,
      (event) => {
        if (!active) return;
        setLastActivity(humanEventLabel(event));
        void refresh();
      },
    );
    return () => {
      active = false;
      unsubscribe();
    };
  }, [runId, toolName]);

  const inspect = useCallback(() => {
    onInspect?.({ runId, profileId, url, instruction, task });
  }, [instruction, onInspect, profileId, runId, task, url]);

  useEffect(() => {
    if (
      !autoOpen ||
      !onInspect ||
      !runId ||
      toolName !== "browser_task" ||
      TERMINAL_STATUSES.has(taskStatus) ||
      autoOpenedRef.current === runId
    ) {
      return;
    }
    const dismissedKey = `lantai:browser-sidecar:dismissed:${runId}`;
    if (window.sessionStorage.getItem(dismissedKey) === "1") return;
    autoOpenedRef.current = runId;
    inspect();
  }, [autoOpen, inspect, onInspect, runId, taskStatus, toolName]);

  const runControl = async (
    action: "pause" | "resume" | "cancel" | "approve" | "deny",
  ) => {
    if (!runId) return;
    setBusyAction(action);
    try {
      const next =
        action === "pause"
          ? await browserWorkbenchApi.pauseTask(runId)
          : action === "resume"
            ? await browserWorkbenchApi.resumeTask(runId)
            : action === "cancel"
              ? await browserWorkbenchApi.cancelTask(runId)
              : await browserWorkbenchApi.confirmTask(runId, {
                  approved: action === "approve",
                  reason:
                    action === "approve"
                      ? "用户在主聊天中允许本次浏览器操作"
                      : "用户在主聊天中拒绝本次浏览器操作",
                });
      setTask(next);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "浏览器任务操作失败",
      );
    } finally {
      setBusyAction("");
    }
  };

  return (
    <article
      className={styles.card}
      data-chat-tool-call="1"
      data-tool-status={taskStatus}
    >
      <header className={styles.header}>
        <span className={styles.browserIcon} aria-hidden="true">
          <Globe2 size={16} />
        </span>
        <div className={styles.heading}>
          <span>{domain}</span>
          <strong>{instruction}</strong>
        </div>
        <span className={styles.status} data-status={taskStatus} role="status">
          {taskStatus === "running_background" || data?.status === "running" ? (
            <Loader2 size={13} className={styles.spin} />
          ) : taskStatus === "completed" || data?.status === "completed" ? (
            <Check size={13} />
          ) : null}
          {statusLabel}
        </span>
      </header>

      <div className={styles.activityRow}>
        <span>{currentActivity}</span>
        {task?.planned_action_count ? (
          <em>
            {task.completed_action_count}/{task.planned_action_count}
          </em>
        ) : null}
      </div>

      {task?.status === "waiting_for_user_confirmation" ? (
        <div className={styles.decisionRow}>
          <div>
            <strong>需要你的确认</strong>
            <span>Agent 准备执行一次会改变页面的操作。</span>
          </div>
          <button
            type="button"
            className={styles.primaryButton}
            onClick={() => void runControl("approve")}
            disabled={Boolean(busyAction)}
          >
            允许一次
          </button>
          <button
            type="button"
            onClick={() => void runControl("deny")}
            disabled={Boolean(busyAction)}
          >
            拒绝
          </button>
        </div>
      ) : null}

      <footer className={styles.actions}>
        <button
          type="button"
          className={styles.inspectButton}
          onClick={inspect}
        >
          <ExternalLink size={14} />
          {task?.status === "manual_takeover_required"
            ? "接管浏览器"
            : "查看网页"}
        </button>
        {canPause ? (
          <button
            type="button"
            onClick={() => void runControl("pause")}
            disabled={Boolean(busyAction)}
          >
            <Pause size={13} />
            暂停
          </button>
        ) : null}
        {canResume ? (
          <button
            type="button"
            onClick={() => void runControl("resume")}
            disabled={Boolean(busyAction)}
          >
            <Play size={13} />
            {task?.status === "manual_takeover_required"
              ? "完成并交还"
              : "继续"}
          </button>
        ) : null}
        {canStop ? (
          <button
            type="button"
            onClick={() => void runControl("cancel")}
            disabled={Boolean(busyAction)}
          >
            <CircleStop size={13} />
            停止
          </button>
        ) : null}
        <button
          type="button"
          className={styles.detailsButton}
          aria-expanded={detailsOpen}
          onClick={() => setDetailsOpen((value) => !value)}
        >
          详情
          <ChevronDown
            size={13}
            className={detailsOpen ? styles.chevronOpen : ""}
          />
        </button>
      </footer>

      {detailsOpen ? (
        <div className={styles.technicalDetails}>
          <span>任务标识</span>
          <code>{runId || "即时浏览操作"}</code>
          <span>浏览身份</span>
          <code>{profileId}</code>
          {task?.error_code ? (
            <>
              <span>停止原因</span>
              <code>{task.error_code}</code>
            </>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
