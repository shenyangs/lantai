import { request } from "../request";

export interface CloudAccountUser {
  id: string;
  uid?: number;
  displayName?: string | null;
  avatarUrl?: string | null;
  registrationSource?: string | null;
  plan?: string;
  status?: string;
}

export interface CloudAccountStatus {
  connected: boolean;
  provider?: string | null;
  user?: CloudAccountUser | null;
  membership?: { plan?: string; status?: string } | null;
  service_url?: string;
  status?: "pending";
}

export interface WatchaLoginStart {
  attempt_id: string;
  authorization_url: string;
  expires_at: string;
  poll_interval_seconds: number;
}

export const cloudAccountApi = {
  status: () => request<CloudAccountStatus>("/cloud-account/status"),
  startWatcha: () =>
    request<WatchaLoginStart>("/cloud-account/watcha/start", {
      method: "POST",
    }),
  pollWatcha: (attemptId: string) =>
    request<CloudAccountStatus>("/cloud-account/watcha/poll", {
      method: "POST",
      body: JSON.stringify({ attempt_id: attemptId }),
    }),
  cancelWatcha: (attemptId: string) =>
    request<{ cancelled: boolean }>("/cloud-account/watcha/cancel", {
      method: "POST",
      body: JSON.stringify({ attempt_id: attemptId }),
    }),
  logout: () =>
    request<{ connected: false; remote_revocation_confirmed: boolean }>(
      "/cloud-account/logout",
      { method: "POST" },
    ),
};
