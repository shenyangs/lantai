export type ChatPermissionPreset = "request" | "risk" | "full";

export interface ChatPermissionRequestOverride {
  permission_mode: "default" | "quiet" | "full_auto";
  permission_profile: "workspace" | "full";
}

export const DEFAULT_CHAT_PERMISSION_PRESET: ChatPermissionPreset = "risk";

const STORAGE_PREFIX = "lantai.chat.permission-preset.v1";
const VALID_PRESETS = new Set<ChatPermissionPreset>([
  "request",
  "risk",
  "full",
]);

export function permissionRequestForPreset(
  preset: ChatPermissionPreset,
): ChatPermissionRequestOverride {
  if (preset === "request") {
    return {
      permission_mode: "default",
      permission_profile: "workspace",
    };
  }
  if (preset === "full") {
    return {
      permission_mode: "full_auto",
      permission_profile: "full",
    };
  }
  return {
    permission_mode: "quiet",
    permission_profile: "workspace",
  };
}

export function permissionPresetStorageKey(
  agentId: string,
  chatOrSessionId?: string | null,
): string {
  const normalizedAgent = String(agentId || "default").trim() || "default";
  const normalizedSession =
    String(chatOrSessionId || "draft").trim() || "draft";
  return `${STORAGE_PREFIX}:${encodeURIComponent(normalizedAgent)}:${encodeURIComponent(
    normalizedSession,
  )}`;
}

export function readChatPermissionPreset(
  agentId: string,
  chatOrSessionId?: string | null,
): ChatPermissionPreset {
  try {
    const stored = window.sessionStorage.getItem(
      permissionPresetStorageKey(agentId, chatOrSessionId),
    );
    return VALID_PRESETS.has(stored as ChatPermissionPreset)
      ? (stored as ChatPermissionPreset)
      : DEFAULT_CHAT_PERMISSION_PRESET;
  } catch {
    return DEFAULT_CHAT_PERMISSION_PRESET;
  }
}

export function persistChatPermissionPreset(
  agentId: string,
  chatOrSessionIds: Array<string | null | undefined>,
  preset: ChatPermissionPreset,
): void {
  const ids = new Set(
    chatOrSessionIds.map((value) => String(value || "").trim()).filter(Boolean),
  );
  if (ids.size === 0) {
    ids.add("draft");
  }
  try {
    ids.forEach((id) => {
      window.sessionStorage.setItem(
        permissionPresetStorageKey(agentId, id),
        preset,
      );
    });
  } catch {
    // A disabled sessionStorage must not block chat submission.
  }
}
