import { Button } from "antd";
import { ArrowUpRight, Play } from "lucide-react";
import dayjs from "dayjs";

import type { RuntimeTaskBoardItem } from "../../../api/types/runtime";
import {
  TASK_LANES,
  groupTaskBoardItems,
  taskSourceLabel,
} from "./taskBoardModel";
import styles from "./index.module.less";

interface TaskBoardProps {
  items: RuntimeTaskBoardItem[];
  actionRunId?: string | null;
  onOpen: (item: RuntimeTaskBoardItem) => void;
  onStart: (item: RuntimeTaskBoardItem) => void;
}

function formatTime(value?: string | null) {
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("MM/DD HH:mm") : "刚刚";
}

export default function TaskBoard({
  items,
  actionRunId,
  onOpen,
  onStart,
}: TaskBoardProps) {
  const grouped = groupTaskBoardItems(items);

  return (
    <div className={styles.board} data-testid="task-board">
      {TASK_LANES.map((lane) => (
        <section className={styles.lane} key={lane.id} data-lane={lane.id}>
          <header className={styles.laneHeader}>
            <div>
              <span className={styles.laneMarker} data-marker={lane.marker} />
              <strong>{lane.label}</strong>
              <em>{grouped[lane.id].length}</em>
            </div>
            <p>{lane.description}</p>
          </header>
          <div className={styles.laneCards}>
            {grouped[lane.id].length ? (
              grouped[lane.id].map((item) => (
                <article className={styles.taskCard} key={item.id}>
                  <button
                    type="button"
                    className={styles.cardMain}
                    onClick={() => onOpen(item)}
                    aria-label={`查看任务：${item.title}`}
                  >
                    <span className={styles.cardEyebrow}>
                      {taskSourceLabel(item.source)}
                      <span>·</span>
                      {formatTime(item.updated_at || item.created_at)}
                    </span>
                    <strong>{item.title}</strong>
                    <span className={styles.cardSummary}>
                      {item.summary || "暂无运行摘要"}
                    </span>
                    {item.next_action ? (
                      <span className={styles.nextAction}>
                        下一步：{item.next_action}
                      </span>
                    ) : null}
                  </button>
                  <footer className={styles.cardFooter}>
                    <span className={styles.runId}>{item.display_id}</span>
                    {item.can_start ? (
                      <Button
                        type="text"
                        size="small"
                        icon={<Play size={14} />}
                        loading={actionRunId === item.id}
                        onClick={() => onStart(item)}
                      >
                        开始
                      </Button>
                    ) : (
                      <Button
                        type="text"
                        size="small"
                        icon={<ArrowUpRight size={14} />}
                        onClick={() => onOpen(item)}
                      >
                        查看
                      </Button>
                    )}
                  </footer>
                </article>
              ))
            ) : (
              <div className={styles.laneEmpty}>没有任务</div>
            )}
          </div>
        </section>
      ))}
    </div>
  );
}
