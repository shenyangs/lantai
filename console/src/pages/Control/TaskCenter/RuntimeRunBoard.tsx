import dayjs from "dayjs";

import type { RuntimeRunSummary } from "../../../api/types/runtime";
import { CapabilityChip } from "../../../components/CapabilityLayout";
import {
  RUNTIME_BOARD_COLUMNS,
  groupRuntimeRunsByBoardColumn,
} from "./runtimeBoard";
import styles from "./RuntimeRunBoard.module.less";

const SOURCE_LABELS: Record<string, string> = {
  chat: "聊天",
  tool: "工具",
  approval: "审批",
  mission: "任务",
  browser: "浏览器",
  capability: "能力",
  pipeline: "流水线",
  cron: "定时",
  reminder: "提醒",
  heartbeat: "心跳",
  image: "图片",
  skills: "技能",
  system: "系统",
};

function formatTime(value?: string | null): string {
  if (!value) return "暂无时间";
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("MM/DD HH:mm") : "暂无时间";
}

export interface RuntimeRunBoardProps {
  runs: RuntimeRunSummary[];
  onOpen: (run: RuntimeRunSummary) => void;
}

export default function RuntimeRunBoard({
  runs,
  onOpen,
}: RuntimeRunBoardProps) {
  const grouped = groupRuntimeRunsByBoardColumn(runs);

  return (
    <div
      className={styles.board}
      data-testid="runtime-run-board"
      aria-label="运行任务看板"
    >
      {RUNTIME_BOARD_COLUMNS.map((column) => (
        <section key={column.id} className={styles.column}>
          <header className={styles.columnHeader}>
            <span>
              <strong>{column.label}</strong>
              <em>{grouped[column.id].length}</em>
            </span>
            <p>{column.description}</p>
          </header>
          <div className={styles.cards}>
            {grouped[column.id].length === 0 ? (
              <div className={styles.empty}>暂无运行</div>
            ) : (
              grouped[column.id].map((run) => (
                <button
                  key={run.id}
                  type="button"
                  className={styles.card}
                  aria-label={`打开任务：${run.title || "未命名运行"}`}
                  onClick={() => onOpen(run)}
                >
                  <span className={styles.cardTitle}>
                    {run.title || "未命名运行"}
                  </span>
                  <span className={styles.cardSummary}>
                    {run.summary || run.last_event_summary || "暂无摘要"}
                  </span>
                  <span className={styles.cardMeta}>
                    <CapabilityChip tone="muted">
                      {SOURCE_LABELS[run.source] || run.source}
                    </CapabilityChip>
                    <span>{formatTime(run.started_at)}</span>
                  </span>
                  {run.pending_approval ? (
                    <span className={styles.approvalHint}>需要你的确认</span>
                  ) : null}
                </button>
              ))
            )}
          </div>
        </section>
      ))}
    </div>
  );
}
