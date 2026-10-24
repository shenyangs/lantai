import { request } from "../request";

export interface BrowserPairingStatus {
  paired: boolean;
}

export interface BrowserPairingCode {
  code: string;
  expires_at: number;
  expires_in_seconds: number;
}

export interface BrowserPairingComplete extends BrowserPairingStatus {
  expires_at: number;
  expires_in_seconds: number;
}

export const browserPairingApi = {
  getBrowserPairingStatus: () =>
    request<BrowserPairingStatus>("/browser-pairing/status"),

  createBrowserPairingCode: () =>
    request<BrowserPairingCode>("/browser-pairing/code", {
      method: "POST",
    }),

  completeBrowserPairing: (code: string) =>
    request<BrowserPairingComplete>("/browser-pairing/complete", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),

  logoutBrowserPairing: () =>
    request<BrowserPairingStatus>("/browser-pairing/logout", {
      method: "POST",
    }),
};
