import { request } from "../request";
import type {
  AppearanceDisplaySettings,
  AppearancePaletteId,
} from "../../theme/appearance";

export type PersistedThemeMode = "light" | "dark" | "system";
export type PersistedDensityMode = "compact" | "comfortable" | "spacious";

export interface AppearanceSettingsPayload {
  theme_mode?: PersistedThemeMode;
  density_mode?: PersistedDensityMode;
  appearance_palette?: AppearancePaletteId;
  display_settings?: AppearanceDisplaySettings;
  custom_theme_css?: string;
}

export type TurnCompletionNotificationMode =
  | "never"
  | "when_unfocused"
  | "always";

export interface SystemBehaviorSettings {
  launch_at_login: boolean;
  keep_awake_while_running: boolean;
  turn_completion_notification: TurnCompletionNotificationMode;
  permission_notification: boolean;
}

export interface StorageRetentionSettings {
  logs_cache_days: number;
  logs_cache_max_bytes: number;
  runtime_days: number;
  runtime_max_bytes: number;
  generated_images_days: number;
}

export interface StorageCleanupResult {
  bytes_before: number;
  bytes_reclaimable: number;
  files_reclaimable: number;
  files_removed: number;
}

export interface StorageRetentionCleanupPreview {
  policy: StorageRetentionSettings;
  dry_run: boolean;
  logs_cache: StorageCleanupResult;
  runtime: StorageCleanupResult;
  generated_images: StorageCleanupResult;
}

function isAppearanceSettingsPayload(
  value: unknown,
): value is AppearanceSettingsPayload {
  return Boolean(value && typeof value === "object");
}

export const settingsApi = {
  getAppearanceSettings: async () => {
    const payload = await request<unknown>("/settings/appearance");
    return isAppearanceSettingsPayload(payload) ? payload : {};
  },

  updateAppearanceSettings: async (settings: AppearanceSettingsPayload) => {
    const payload = await request<unknown>("/settings/appearance", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
    return isAppearanceSettingsPayload(payload) ? payload : {};
  },

  getSystemBehaviorSettings: () =>
    request<SystemBehaviorSettings>("/settings/system-behavior"),

  updateSystemBehaviorSettings: (settings: Partial<SystemBehaviorSettings>) =>
    request<SystemBehaviorSettings>("/settings/system-behavior", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),

  getStorageRetentionSettings: () =>
    request<StorageRetentionSettings>("/settings/storage-retention"),

  updateStorageRetentionSettings: (
    settings: Partial<StorageRetentionSettings>,
  ) =>
    request<StorageRetentionSettings>("/settings/storage-retention", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),

  previewStorageCleanup: () =>
    request<StorageRetentionCleanupPreview>(
      "/settings/storage-retention/preview",
      { method: "POST" },
    ),

  cleanStorage: () =>
    request<StorageRetentionCleanupPreview>(
      "/settings/storage-retention/cleanup",
      {
        method: "POST",
      },
    ),
};
