export type ReminderStatus =
  | "new"
  | "read"
  | "snoozed"
  | "dismissed"
  | "done";

export type ReminderSource =
  | "manual"
  | "chat"
  | "cron"
  | "capability"
  | "watch_topic";

export type ReminderPriority = "low" | "normal" | "high";

export interface ReminderSettings {
  quiet_mode: boolean;
  daily_brief_enabled: boolean;
  native_notification_policy: "off" | "explicit_only";
}

export interface NativeReminderIdentity {
  run_id: string;
  attempt_id: string;
  agent_id: string;
  reminder_id: string;
  caller_id: string;
  receipt_token: string;
}

export interface NativeReminderPayload extends NativeReminderIdentity {
  title: string;
  body: string;
}

export interface NativeReminderReceipt extends NativeReminderIdentity {
  id: string;
  ok: boolean;
  shown: boolean;
  status: "shown" | "failed" | "unknown";
  reason: string;
  error: string;
}

export interface ReminderItem {
  id: string;
  title: string;
  summary: string;
  reason: string;
  source: ReminderSource;
  priority: ReminderPriority;
  status: ReminderStatus;
  due_at?: string | null;
  snooze_until?: string | null;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface ReminderCreateRequest {
  title: string;
  summary?: string;
  reason?: string;
  source?: ReminderSource;
  priority?: ReminderPriority;
  due_at?: string | null;
  metadata?: Record<string, unknown>;
}

export interface ReminderUpdateRequest {
  title?: string;
  summary?: string;
  reason?: string;
  source?: ReminderSource;
  priority?: ReminderPriority;
  status?: ReminderStatus;
  due_at?: string | null;
  snooze_until?: string | null;
  metadata?: Record<string, unknown>;
}

export interface WatchTopic {
  id: string;
  title: string;
  description: string;
  origin: string;
  enabled: boolean;
  frequency: string;
  revision: number;
  brief_status:
    | "unconfigured"
    | "proposed"
    | "confirmed"
    | "paused"
    | "archived";
  brief_purpose: string;
  brief_scope_refs: string[];
  brief_evidence_refs: string[];
  brief_exclusions: string[];
  brief_valid_until: string | null;
  created_at: string;
  updated_at: string;
  last_checked_at?: string | null;
  metadata: Record<string, unknown>;
}

export interface WatchTopicCreateRequest {
  title: string;
  description?: string;
  origin?: string;
  enabled?: boolean;
  frequency?: string;
  metadata?: Record<string, unknown>;
}

export interface WatchTopicUpdateRequest {
  title?: string;
  description?: string;
  origin?: string;
  enabled?: boolean;
  frequency?: string;
  last_checked_at?: string | null;
  metadata?: Record<string, unknown>;
  expected_revision?: number;
}

export interface WatchTopicBriefUseRequest {
  expected_revision: number;
  explicit_confirmation: boolean;
  status: "confirmed" | "paused" | "archived";
  purpose: string;
  scope_refs: string[];
  evidence_refs: string[];
  exclusions: string[];
  valid_until: string | null;
}

export interface TodayBrief {
  date: string;
  items: ReminderItem[];
  new_count: number;
  due_count: number;
  high_value_count: number;
  quiet_mode: boolean;
}
