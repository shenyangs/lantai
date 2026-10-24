import { CalendarDays, FileText, History } from "lucide-react";
import type { ContinuationSuggestion } from "../continuationSuggestions";
import styles from "../index.module.less";

interface ContinueWorkCardsProps {
  items: ContinuationSuggestion[];
  onOpen: (item: ContinuationSuggestion) => void;
}

const ICONS = {
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

export default function ContinueWorkCards({
  items,
  onOpen,
}: ContinueWorkCardsProps) {
  if (items.length === 0) return null;
  return (
    <section className={styles.continueWorkShelf} aria-label="继续上次工作">
      <div className={styles.continueWorkHeading}>
        <span>继续上次工作</span>
        <small>来自任务、今日与产物</small>
      </div>
      <div className={styles.continueWorkCards}>
        {items.map((item) => {
          const Icon = ICONS[item.kind];
          const formattedTime = formatTimestamp(item.timestamp);
          return (
            <button
              key={item.id}
              type="button"
              className={styles.continueWorkCard}
              onClick={() => onOpen(item)}
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
