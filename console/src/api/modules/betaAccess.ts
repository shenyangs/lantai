import { request } from "../request";

export type BetaAccessStatus =
  | "not_activated"
  | "active"
  | "grace"
  | "expired"
  | "revoked"
  | "device_key_unavailable"
  | "cert_invalid"
  | "unsupported"
  | "account_suspended"
  | "clock_check_required";

export interface BetaAccessSnapshot {
  enforcement_required: boolean;
  status: BetaAccessStatus;
  decision_seq: number;
  snapshot_revision: number;
  refresh_due: boolean;
  refresh_scheduled: boolean;
  renew_by: number | null;
  expires_at: number | null;
}

export interface BetaAccessSnapshotResult extends BetaAccessSnapshot {
  operation_id?: string;
}

export type BetaAccessOperationResult =
  | BetaAccessSnapshotResult
  | {
      status: "issuance_pending";
      operation_id: string;
      recovery_token?: string;
    };

export interface BetaActivateRequest {
  // The renderer must preserve the user's text. Normalization belongs to the
  // trusted service's invite-nfc-v1 implementation.
  code: string;
  request_id?: string;
  device_label?: string;
}

export const betaAccessApi = {
  status: () => request<BetaAccessSnapshot>("/beta-access/status"),
  activate: (payload: BetaActivateRequest) =>
    request<BetaAccessOperationResult>("/beta-access/activate", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  issue: (payload: Omit<BetaActivateRequest, "code"> = {}) =>
    request<BetaAccessOperationResult>("/beta-access/issue", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  recover: (operationId: string, recoveryToken?: string) =>
    request<BetaAccessOperationResult>("/beta-access/recover", {
      method: "POST",
      body: JSON.stringify({
        operation_id: operationId,
        ...(recoveryToken ? { recovery_token: recoveryToken } : {}),
      }),
    }),
};
