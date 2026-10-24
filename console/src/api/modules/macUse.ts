import { request } from "../request";

export interface MacUseWindow {
  window_id: string;
  process_id: number;
  process_started_at: number;
  owner_name: string;
  window_title: string;
  app_bundle_id: string;
  app_path: string;
  on_screen: boolean;
  bounds: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
}

export interface MacUseResult {
  ok: boolean;
  action: string;
  run_id: string;
  action_id: string;
  effect_id: string;
  policy_status: "allowed" | "denied" | "needs_authorization";
  dispatch_status: "not_sent" | "sent" | "transport_failed" | "unknown";
  effect_status:
    | "not_applicable"
    | "verified"
    | "no_effect_verified"
    | "ambiguous"
    | "failed";
  message?: string;
  error?: string;
  error_code?: string;
  data: {
    windows?: MacUseWindow[];
    permissions?: Record<string, string>;
    [key: string]: unknown;
  };
}

export interface MacUseTargetSelection {
  app_bundle_id: string;
  window_id: string;
  process_id: number;
  process_started_at: number;
  window_title: string;
  owner_name: string;
  selection_receipt: string;
}

export const macUseApi = {
  status: () => request<MacUseResult>("/mac-use/status"),
  listWindows: () => request<MacUseResult>("/mac-use/windows"),
  stopSession: (runId: string) =>
    request<MacUseResult>(
      `/mac-use/sessions/${encodeURIComponent(runId)}/stop`,
      { method: "POST" },
    ),
};
