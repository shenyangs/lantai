import { CalendarDays, FileText, History, Newspaper } from "lucide-react";
import type { DailyBriefHomeSnapshot } from "@/api/types/dailyBrief";
import type { ContinuationSuggestion } from "../continuationSuggestions";
import ContinueWorkCards from "./ContinueWorkCards";
import styles from "../index.module.less";

interface DailyBriefHomeShelfProps {
  snapshot: DailyBriefHomeSnapshot | null;
  continuationItems: ContinuationSuggestion[];
  onOpenBrief: () => void;
  onOpenContinuation: (item: ContinuationSuggestion) => void;
}

const CONTINUATION_ICONS = {
  run: History,
  today: CalendarDays,
  artifact: FileText,
} as const;

function formatTimestamp(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export default function DailyBriefHomeShelf({
  snapshot,
  continuationItems,
  onOpenBrief,
  onOpenContinuation,
}: DailyBriefHomeShelfProps) {
  if (!snapshot || !snapshot.visible || snapshot.state === "hidden") {
    return (
      <ContinueWorkCards
        items={continuationItems}
        onOpen={onOpenContinuation}
      />
    );
  }

  const { card } = snapshot;
  return (
    <section className={styles.dailyBriefHomeShelf} aria-label="接续与发现">
      <div className={styles.continueWorkHeading}>
        <span>今日</span>
        <small>简报与接续</small>
      </div>
      <div className={styles.dailyBriefEntryGrid}>
        <button
          type="button"
          className={`${styles.continueWorkCard} ${styles.dailyBriefEntryCard}`}
          aria-label={`打开澜台简报：${card.status_line}`}
          onClick={onOpenBrief}
        >
          <span className={styles.dailyBriefEntryIcon} aria-hidden="true">
            <Newspaper size={16} />
            {card.has_update ? <i /> : null}
          </span>
          <span className={styles.continueWorkBody}>
            <strong>{card.title}</strong>
            <span>{card.status_line}</span>
            {card.hint ? <small>{card.hint}</small> : null}
          </span>
        </button>

        {continuationItems.slice(0, 1).map((item) => {
          const Icon = CONTINUATION_ICONS[item.kind];
          const formattedTime = formatTimestamp(item.timestamp);
          return (
            <button
              key={item.id}
              type="button"
              className={styles.continueWorkCard}
              aria-label={item.title}
              onClick={() => onOpenContinuation(item)}
            >
              <Icon size={16} aria-hidden="true" />
              <span className={styles.continueWorkBody}>
                <strong>{item.title}</strong>
                <span>{item.summary}</span>
                <small>
                  {item.sourceLabel}
                  {formattedTime ? ` · ${formattedTime}` : ""}
                </small>
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
