import { request } from "../request";

export interface PushMessage {
  id: string;
  text: string;
  sticky?: boolean;
  occurred_at?: number;
  session_id?: string | null;
  source_kind?: string | null;
  source_id?: string | null;
  source_name?: string | null;
}

export interface PendingApproval {
  request_id: string;
  session_id: string;
  root_session_id: string;
  user_id?: string;
  agent_id: string;
  tool_name: string;
  severity: string;
  findings_count: number;
  findings_summary: string;
  tool_params: Record<string, unknown>;
  created_at: number;
  timeout_seconds: number | null;
  policy_decision?: Record<string, unknown> | null;
  approval_options?: string[];
  session_rememberable?: boolean;
  rememberable?: boolean;
  permission_scope?: string | null;
  permission_group_key?: string | null;
  source_run_id?: string | null;
  source_agent_name?: string | null;
}

export const consoleApi = {
  getPushMessages: (sessionId?: string, signal?: AbortSignal) =>
    request<{ messages: PushMessage[]; pending_approvals: PendingApproval[] }>(
      sessionId
        ? `/console/push-messages?session_id=${sessionId}`
        : "/console/push-messages",
      { signal },
    ),
  acknowledgePushMessage: (messageId: string) =>
    request<{ acknowledged: boolean }>(
      `/console/push-messages/${encodeURIComponent(messageId)}/ack`,
      { method: "POST" },
    ),
};
