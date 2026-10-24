import { request } from "../request";

export type ComputerPermissionKey =
  | "accessibility"
  | "full_disk_access"
  | "screen_recording"
  | "automation";

export type ComputerPermissionStatusValue =
  | "granted"
  | "denied"
  | "unknown"
  | "unsupported";

export interface ComputerPermissionItem {
  key: ComputerPermissionKey;
  status: ComputerPermissionStatusValue;
  can_check: boolean;
  can_request: boolean;
  settings_url?: string | null;
}

export interface ComputerPermissionStatus {
  platform: string;
  is_macos: boolean;
  app_path?: string | null;
  permissions: ComputerPermissionItem[];
}

export interface ComputerPermissionActionResult {
  ok: boolean;
  key: ComputerPermissionKey;
  message: string;
}

export const computerPermissionsApi = {
  getComputerPermissions: () =>
    request<ComputerPermissionStatus>("/settings/computer-permissions"),

  requestComputerPermission: (key: ComputerPermissionKey) =>
    request<ComputerPermissionActionResult>(
      `/settings/computer-permissions/${key}/request`,
      { method: "POST" },
    ),

  openComputerPermissionSettings: (key: ComputerPermissionKey) =>
    request<ComputerPermissionActionResult>(
      `/settings/computer-permissions/${key}/open`,
      { method: "POST" },
    ),
};
