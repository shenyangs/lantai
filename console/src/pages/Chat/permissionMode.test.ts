import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_CHAT_PERMISSION_PRESET,
  permissionRequestForPreset,
  permissionPresetStorageKey,
  persistChatPermissionPreset,
  readChatPermissionPreset,
} from "./permissionMode";

describe("chat permission mode", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("maps visible presets to authoritative runtime values", () => {
    expect(permissionRequestForPreset("request")).toEqual({
      permission_mode: "default",
      permission_profile: "workspace",
    });
    expect(permissionRequestForPreset("risk")).toEqual({
      permission_mode: "quiet",
      permission_profile: "workspace",
    });
    expect(permissionRequestForPreset("full")).toEqual({
      permission_mode: "full_auto",
      permission_profile: "full",
    });
  });

  it("persists one selection across resolved session aliases", () => {
    persistChatPermissionPreset(
      "default",
      ["draft-session", "chat-1", "console:chat-1"],
      "request",
    );

    expect(readChatPermissionPreset("default", "draft-session")).toBe(
      "request",
    );
    expect(readChatPermissionPreset("default", "chat-1")).toBe("request");
    expect(readChatPermissionPreset("default", "console:chat-1")).toBe(
      "request",
    );
  });

  it("falls back safely when storage contains an unknown value", () => {
    window.sessionStorage.setItem(
      permissionPresetStorageKey("default", "chat-1"),
      "unsafe",
    );

    expect(readChatPermissionPreset("default", "chat-1")).toBe(
      DEFAULT_CHAT_PERMISSION_PRESET,
    );
  });
});
