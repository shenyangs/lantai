export type TimeItemKind = "reminder" | "task" | "automation";

export interface TimeItem {
  id: string;
  kind: TimeItemKind;
  source: string;
  source_label: string;
  title: string;
  summary: string;
  start_at: string | null;
  end_at: string | null;
  all_day: boolean;
  blocking: boolean;
  status: string;
  action_path: string | null;
  metadata: Record<string, unknown>;
}

export interface TodayTimeline {
  date: string;
  timezone: string;
  generated_at: string;
  items: TimeItem[];
  counts: Record<TimeItemKind, number>;
}
