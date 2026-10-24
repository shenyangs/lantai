import { request } from "../request";

export interface CommandCheckResponse {
  is_control_command: boolean;
  command_token: string | null;
}

export interface ApprovalCommandResponse {
  success: boolean;
  message: string;
  decision_scope?: "once" | "session" | "workspace";
  remembered?: boolean;
  resolved_request_ids?: string[];
}

export interface SessionPermissionUpdateResponse {
  success: boolean;
  permission_mode: string;
  permission_profile: string;
  resolved_request_ids: string[];
}

export interface SessionPermissionPreviewItem {
  request_id: string;
  tool_name: string;
  severity: string;
}

export interface SessionPermissionPreviewResponse {
  queue_revision: string;
  requests: SessionPermissionPreviewItem[];
}

export const commandsApi = {
  /** Check if text is a system control command */
  checkCommand: async (text: string): Promise<boolean> => {
    const response = await request<CommandCheckResponse>("/commands/check", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    return response.is_control_command;
  },

  /** Send approval command (approve or deny) */
  sendApprovalCommand: async (
    action: "approve" | "deny",
    requestId: string,
    sessionId: string,
    reason?: string,
    decisionScope?: "once" | "session" | "workspace",
    userId?: string,
  ): Promise<ApprovalCommandResponse> => {
    // Use dedicated approval API endpoint (bypasses chat/session system)
    return request<ApprovalCommandResponse>(`/approval/${action}`, {
      method: "POST",
      body: JSON.stringify({
        request_id: requestId,
        session_id: sessionId,
        user_id: userId || undefined,
        reason: reason || undefined,
        decision_scope: decisionScope || undefined,
      }),
    });
  },

  /** Preview the queued approvals affected by a full-access change. */
  previewSessionPermission: async ({
    sessionId,
    userId,
  }: {
    sessionId: string;
    userId: string;
  }): Promise<SessionPermissionPreviewResponse> =>
    request<SessionPermissionPreviewResponse>(
      "/approval/session-permission/preview",
      {
        method: "POST",
        body: JSON.stringify({
          session_id: sessionId,
          user_id: userId,
        }),
      },
    ),

  /** Apply a composer permission change to an already-running root session. */
  updateSessionPermission: async ({
    sessionId,
    userId,
    permissionMode,
    permissionProfile,
    expectedQueueRevision,
  }: {
    sessionId: string;
    userId: string;
    permissionMode: string;
    permissionProfile: string;
    expectedQueueRevision?: string;
  }): Promise<SessionPermissionUpdateResponse> =>
    request<SessionPermissionUpdateResponse>("/approval/session-permission", {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        user_id: userId,
        permission_mode: permissionMode,
        permission_profile: permissionProfile,
        expected_queue_revision: expectedQueueRevision,
      }),
    }),
};
