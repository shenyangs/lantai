import { request } from "../request";

export interface BackendDebugLogsResponse {
  path: string;
  exists: boolean;
  lines: number;
  updated_at: number | null;
  size: number;
  content: string;
}

export interface DebugTurnEventRecord {
  id: string;
  ts: string;
  seq?: number;
  type?: string | null;
  chat_id?: string | null;
  session_id?: string | null;
  run_id?: string | null;
  scope?: string;
  kind: string;
  status: string;
  summary: string;
  label: string;
  detail: string;
  workspace_dir: string;
  usage?: Record<string, unknown> | null;
}

export interface DebugTurnEventsResponse {
  paths: string[];
  exists: boolean;
  updated_at: number | null;
  size: number;
  event_count: number;
  items: DebugTurnEventRecord[];
}

export const debugApi = {
  getBackendLogs: (lines = 200) =>
    request<BackendDebugLogsResponse>(
      `/console/debug/backend-logs?lines=${lines}`,
    ),
  getTurnEvents: (limit = 50) =>
    request<DebugTurnEventsResponse>(
      `/console/debug/turn-events?limit=${limit}`,
    ),
};
