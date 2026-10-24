import { AlertCircle, CheckCircle2, Loader2, SlidersHorizontal } from "lucide-react";
import { useMemo, useState } from "react";
import {
  customizationApi,
  notifyCustomizationChanged,
} from "../../../api/modules/customization";
import { useAppMessage } from "../../../hooks/useAppMessage";
import styles from "./ScheduledTaskDraftCard.module.less";
import {
  isCustomizationDraftToolName,
  parseCustomizationDraftOutput,
} from "./customizationDraft";

interface ToolCardProps {
  data?: {
    status?: string;
    content?: Array<{ data?: { output?: unknown; error?: unknown } }>;
  };
}

export { isCustomizationDraftToolName };

function statusOf(data: ToolCardProps["data"]) {
  const value = String(data?.status || "").toLowerCase();
  return /(running|progress|pending|created)/.test(value) ? "running" : "done";
}

function valueText(value: unknown): string {
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (Array.isArray(value)) return value.join("、") || "无";
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "默认");
}

export default function CustomizationDraftCard({ data }: ToolCardProps) {
  const { message } = useAppMessage();
  const output = data?.content?.[1]?.data?.output;
  const error = data?.content?.[1]?.data?.error;
  const draft = useMemo(() => parseCustomizationDraftOutput(output), [output]);
  const [applyState, setApplyState] = useState<"idle" | "applying" | "done">("idle");

  if (statusOf(data) === "running") {
    return (
      <div className={styles.card} data-chat-tool-call="1">
        <div className={styles.iconWrap}><Loader2 className={styles.spin} size={18} /></div>
        <div>
          <div className={styles.title}>正在生成澜台改造预演</div>
          <div className={styles.subtitle}>只生成方案，不会在后台直接改动。</div>
        </div>
      </div>
    );
  }

  if (!draft || !draft.plan.id) {
    return (
      <div className={`${styles.card} ${styles.error}`} data-chat-tool-call="1">
        <div className={styles.iconWrap}><AlertCircle size={18} /></div>
        <div>
          <div className={styles.title}>改造方案解析失败</div>
          <div className={styles.subtitle}>
            {typeof error === "string" ? error : "工具没有返回可确认的版本化方案。"}
          </div>
        </div>
      </div>
    );
  }

  const changes = Object.entries(draft.preview.changed);
  const apply = async () => {
    if (applyState !== "idle") return;
    setApplyState("applying");
    try {
      const receipt = draft.preview.requires_confirmation
        ? await customizationApi.confirmPlan(draft.plan.id!)
        : null;
      const result = await customizationApi.applyPlan(draft.plan.id!, receipt?.token);
      if (result.status !== "committed") throw new Error("健康检查未通过，已自动回滚");
      await customizationApi.activateProfile(draft.profile.id);
      notifyCustomizationChanged();
      setApplyState("done");
      message.success("澜台改造已应用，可随时撤销或恢复原生状态");
    } catch (cause) {
      setApplyState("idle");
      message.error(cause instanceof Error ? cause.message : "应用改造失败");
    }
  };

  return (
    <div className={styles.card} data-chat-tool-call="1" data-customization-draft="1">
      <div className={styles.iconWrap}>
        {applyState === "done" ? <CheckCircle2 size={18} /> : <SlidersHorizontal size={18} />}
      </div>
      <div className={styles.body}>
        <div className={styles.headerRow}>
          <div>
            <div className={styles.title}>澜台改造方案</div>
            <div className={styles.subtitle}>{draft.summary}</div>
          </div>
          <span className={applyState === "done" ? styles.doneBadge : styles.pendingBadge}>
            {applyState === "done" ? "已应用" : "待确认"}
          </span>
        </div>
        <dl className={styles.detailGrid}>
          <div><dt>配置档</dt><dd>{draft.profile.name}</dd></div>
          <div><dt>风险</dt><dd>{draft.preview.risk === "low" ? "低风险" : "需明确确认"}</dd></div>
          {changes.slice(0, 6).map(([path, change]) => (
            <div key={path} title={path}>
              <dt>{path}</dt>
              <dd>{valueText(change.before)} → {valueText(change.after)}</dd>
            </div>
          ))}
        </dl>
        {changes.length > 6 ? <div className={styles.warningText}>另有 {changes.length - 6} 项改动</div> : null}
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.primaryButton}
            disabled={applyState !== "idle"}
            onClick={apply}
          >
            {applyState === "applying" ? <Loader2 className={styles.buttonSpin} size={14} /> : null}
            {applyState === "done" ? "已应用" : applyState === "applying" ? "正在检查并应用" : "确认应用"}
          </button>
          <span className={styles.actionHint}>应用前校验版本；失败自动回滚，不影响聊天和记忆数据。</span>
        </div>
      </div>
    </div>
  );
}
