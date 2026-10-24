export interface CronJobSchedule {
  type: "cron";
  cron: string;
  timezone?: string;
}

export interface CronJobTarget {
  user_id: string;
  session_id: string;
}

export interface CronJobDispatch {
  type: "channel";
  channel?: string;
  target: CronJobTarget;
  mode?: "stream" | "final";
  meta?: Record<string, unknown>;
}

export interface CronJobRuntime {
  max_concurrency?: number;
  timeout_seconds?: number;
  misfire_grace_seconds?: number;
  share_session?: boolean;
}

export interface CronJobRequest {
  input: unknown;
  session_id?: string | null;
  user_id?: string | null;
  [key: string]: unknown;
}

export interface CapabilityCronRequest {
  capability_id: string;
  parameters?: Record<string, unknown>;
}

export interface PipelineCronRequest {
  pipeline_id: string;
  parameters?: Record<string, unknown>;
}

export interface CronJobSpecInput {
  id?: string;
  name: string;
  enabled?: boolean;
  schedule: CronJobSchedule;
  task_type?: "text" | "agent" | "capability" | "pipeline";
  text?: string;
  request?: CronJobRequest;
  capability_request?: CapabilityCronRequest;
  pipeline_request?: PipelineCronRequest;
  dispatch: CronJobDispatch;
  runtime?: CronJobRuntime;
  meta?: Record<string, unknown>;
}

export interface CronJobSpecOutput extends CronJobSpecInput {
  id: string;
}

export interface CronJobView extends CronJobSpecOutput {
  // Extended view with runtime state
  state?: unknown;
  next_run_time?: number;
  last_run_time?: number;
}

export interface CronOccurrence {
  job_id: string;
  scheduled_at: string;
  schedule_timezone: string;
}

export interface CronOccurrenceWindow {
  start_at: string;
  end_at: string;
  rule_count: number;
  occurrence_count: number;
  items: CronOccurrence[];
  truncated: boolean;
  complete: boolean;
  unavailable_job_ids: string[];
}

export type CronJobSpecInputLegacy = Record<string, unknown>;
export type CronJobSpecOutputLegacy = Record<string, unknown>;
export type CronJobViewLegacy = Record<string, unknown>;
