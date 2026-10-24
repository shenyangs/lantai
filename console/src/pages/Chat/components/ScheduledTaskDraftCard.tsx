import { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  CalendarClock,
  CheckCircle2,
  Loader2,
} from "lucide-react";
import { cronJobApi } from "../../../api/modules/cronjob";
import type { CronJobSpecOutput } from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import styles from "./ScheduledTaskDraftCard.module.less";
import {
  buildCronJobSpecFromDraft,
  buildDraftStorageKey,
  formatCronLabel,
  getCronJobInstructionPreview,
  isScheduledTaskToolName,
  parseScheduledTaskDraftOutput,
} from "./scheduledTaskDraft";

interface RuntimeToolContent {
  data?: {
    output?: unknown;
    error?: unknown;
  };
}

interface RuntimeToolData {
  status?: string;
  content?: RuntimeToolContent[];
}

interface ScheduledTaskDraftCardProps {
  data?: RuntimeToolData;
}

interface RuntimeWindow extends Window {
  currentSessionId?: string;
  currentUserId?: string;
  currentChannel?: string;
}

type CreateState = "idle" | "creating" | "created" | "failed";

declare const window: RuntimeWindow;

export { isScheduledTaskToolName };

function getToolStatus(data?: RuntimeToolData): "running" | "failed" | "done" {
  const status = String(data?.status || "").toLowerCase();
  if (status.includes("progress") || status.includes("running")) {
    return "running";
  }
  if (status.includes("fail") || status.includes("error")) return "failed";
  if (data?.content?.[1]?.data?.error) return "failed";
  return "done";
}

function safeGetStorage(key: string): string {
  try {
    return window.localStorage?.getItem(key) || "";
  } catch {
    return "";
  }
}

function safeSetStorage(key: string, value: string): void {
  try {
    window.localStorage?.setItem(key, value);
  } catch {
    // localStorage can be unavailable in hardened desktop contexts.
  }
}

function resolveCurrentContext() {
  const userId = window.currentUserId || "default";
  const channel = window.currentChannel || "console";
  return {
    userId,
    channel,
    sessionId: window.currentSessionId || `${channel}:${userId}`,
    timezone: "Asia/Shanghai",
  };
}

function getCreatedId(created: CronJobSpecOutput | null): string {
  return created?.id || "";
}

export default function ScheduledTaskDraftCard({
  data,
}: ScheduledTaskDraftCardProps) {
  const { message } = useAppMessage();
  const status = getToolStatus(data);
  const output = data?.content?.[1]?.data?.output;
  const error = data?.content?.[1]?.data?.error;
  const draft = useMemo(
    () => parseScheduledTaskDraftOutput(output),
    [output],
  );
  const spec = useMemo(
    () => (draft ? buildCronJobSpecFromDraft(draft, resolveCurrentContext()) : null),
    [draft],
  );
  const storageKey = useMemo(
    () => (spec ? buildDraftStorageKey(spec) : ""),
    [spec],
  );
  const instruction = useMemo(
    () => (spec ? getCronJobInstructionPreview(spec) : ""),
    [spec],
  );
  const [createState, setCreateState] = useState<CreateState>("idle");
  const [createdJobId, setCreatedJobId] = useState("");

  useEffect(() => {
    if (!storageKey) {
      setCreateState("idle");
      setCreatedJobId("");
      return;
    }

    const cachedJobId = safeGetStorage(storageKey);
    if (cachedJobId) {
      setCreateState("created");
      setCreatedJobId(cachedJobId);
      return;
    }

    setCreateState("idle");
    setCreatedJobId("");
  }, [storageKey]);

  const handleCreate = async () => {
    if (!spec || createState === "creating" || createState === "created") {
      return;
    }

    setCreateState("creating");
    try {
      const created = await cronJobApi.createCronJob(spec);
      const jobId = getCreatedId(created);
      safeSetStorage(storageKey, jobId || "created");
      setCreatedJobId(jobId);
      setCreateState("created");
      message.success("定时任务已创建");
    } catch (createError) {
      console.error("Failed to create scheduled task from chat draft", createError);
      setCreateState("failed");
      message.error("创建定时任务失败，请稍后重试");
    }
  };

  if (status === "running") {
    return (
      <div className={styles.card} data-chat-tool-call="1">
        <div className={styles.iconWrap}>
          <Loader2 className={styles.spin} size={18} />
        </div>
        <div>
          <div className={styles.title}>正在整理定时任务草案</div>
          <div className={styles.subtitle}>我会先生成确认卡，不会直接创建任务。</div>
        </div>
      </div>
    );
  }

  if (!draft || !spec) {
    return (
      <div className={`${styles.card} ${styles.error}`} data-chat-tool-call="1">
        <div className={styles.iconWrap}>
          <AlertCircle size={18} />
        </div>
        <div>
          <div className={styles.title}>定时任务草案解析失败</div>
          <div className={styles.subtitle}>
            {typeof error === "string" ? error : "工具没有返回可确认的任务结构。"}
          </div>
        </div>
      </div>
    );
  }

  const created = createState === "created";
  const creating = createState === "creating";
  const actionLabel = created
    ? createdJobId
      ? `已创建：${createdJobId.slice(0, 8)}`
      : "已创建"
    : creating
      ? "创建中..."
      : "确认创建";

  return (
    <div className={styles.card} data-chat-tool-call="1">
      <div className={styles.iconWrap}>
        {created ? <CheckCircle2 size={18} /> : <CalendarClock size={18} />}
      </div>
      <div className={styles.body}>
        <div className={styles.headerRow}>
          <div>
            <div className={styles.title}>定时任务草案</div>
            <div className={styles.subtitle}>
              {draft.summary || "请确认下面的计划，确认后才会创建任务。"}
            </div>
          </div>
          <span className={created ? styles.doneBadge : styles.pendingBadge}>
            {created ? "已创建" : "待确认"}
          </span>
        </div>

        <dl className={styles.detailGrid}>
          <div>
            <dt>任务名称</dt>
            <dd>{spec.name}</dd>
          </div>
          <div>
            <dt>执行时间</dt>
            <dd>{formatCronLabel(spec.schedule.cron, spec.schedule.timezone)}</dd>
          </div>
          <div>
            <dt>执行内容</dt>
            <dd>{instruction || "按定时任务请求执行"}</dd>
          </div>
          <div>
            <dt>投递位置</dt>
            <dd>
              {spec.dispatch.channel || "console"} / 当前会话
            </dd>
          </div>
        </dl>

        {draft.warnings.length > 0 && (
          <div className={styles.warningText}>
            {draft.warnings.map((warning) => (
              <span key={warning}>{warning}</span>
            ))}
          </div>
        )}

        <div className={styles.actions}>
          <button
            type="button"
            className={styles.primaryButton}
            onClick={handleCreate}
            disabled={creating || created}
          >
            {creating && <Loader2 className={styles.buttonSpin} size={14} />}
            {actionLabel}
          </button>
          <span className={styles.actionHint}>
            创建后可在“定时”页面继续编辑。
          </span>
        </div>
      </div>
    </div>
  );
}
