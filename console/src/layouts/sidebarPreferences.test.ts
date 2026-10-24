import { beforeEach, describe, expect, it } from "vitest";
import {
  applyCustomizationSidebarValues,
  loadSidebarVisibleKeys,
  resetSidebarVisibleKeys,
  saveSidebarVisibleKeys,
  SIDEBAR_VISIBILITY_STORAGE_KEY,
  SIDEBAR_NAV_ITEMS,
} from "./sidebarPreferences";

describe("sidebarPreferences", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("keeps the internal task runtime out of public sidebar preferences", () => {
    const visibleKeys = loadSidebarVisibleKeys();

    expect(Array.from(visibleKeys)).toEqual([
      "chat",
      "works",
      "daily-brief",
      "channels",
      "reminders",
      "cron-jobs",
      "workspace",
      "memory",
      "agents",
      "skills",
      "tools",
      "settings",
    ]);
  });

  it("adds the new daily brief entry once when migrating saved v6 preferences", () => {
    window.localStorage.setItem(
      "lantai:sidebar-visible-nav:v6",
      JSON.stringify(["chat", "works", "settings"]),
    );

    const visibleKeys = loadSidebarVisibleKeys();

    expect(visibleKeys.has("daily-brief")).toBe(true);
    expect(
      JSON.parse(
        window.localStorage.getItem(SIDEBAR_VISIBILITY_STORAGE_KEY) || "[]",
      ),
    ).toContain("daily-brief");
  });

  it("keeps an explicit daily brief hide choice in current preferences", () => {
    const visibleKeys = saveSidebarVisibleKeys([
      "chat",
      "works",
      "channels",
      "settings",
    ]);

    expect(visibleKeys.has("daily-brief")).toBe(false);
    expect(loadSidebarVisibleKeys().has("daily-brief")).toBe(false);
  });

  it("drops the retired terminal entry when migrating a saved v4 sidebar", () => {
    window.localStorage.setItem(
      "lantai:sidebar-visible-nav:v4",
      JSON.stringify(["chat", "workspace", "terminal", "settings"]),
    );

    const visibleKeys = loadSidebarVisibleKeys();

    expect(visibleKeys.has("terminal")).toBe(false);
    expect(visibleKeys.has("browser-workbench")).toBe(false);
  });

  it("drops retired and internal-only entries from saved preferences", () => {
    saveSidebarVisibleKeys([
      "chat",
      "task-center",
      "browser-workbench",
      "tools",
      "settings",
    ]);

    const visibleKeys = loadSidebarVisibleKeys();

    expect(visibleKeys.has("task-center")).toBe(false);
    expect(visibleKeys.has("browser-workbench")).toBe(false);
    expect(visibleKeys.has("tools")).toBe(true);
  });

  it("does not allow the core chat, works, and settings entries to be removed", () => {
    const visibleKeys = saveSidebarVisibleKeys([]);

    expect(Array.from(visibleKeys)).toEqual(["chat", "works", "settings"]);
  });

  it("restores the established defaults", () => {
    window.localStorage.setItem(
      SIDEBAR_VISIBILITY_STORAGE_KEY,
      JSON.stringify(["chat", "browser-workbench", "tools", "settings"]),
    );

    const visibleKeys = resetSidebarVisibleKeys();

    expect(visibleKeys.has("chat")).toBe(true);
    expect(visibleKeys.has("settings")).toBe(true);
    expect(visibleKeys.has("daily-brief")).toBe(true);
    expect(visibleKeys.has("task-center")).toBe(false);
    expect(visibleKeys.has("browser-workbench")).toBe(false);
    expect(visibleKeys.has("capabilities")).toBe(false);
    expect(visibleKeys.has("presentations")).toBe(false);
    expect(visibleKeys.has("tools")).toBe(true);
    expect(window.localStorage.getItem(SIDEBAR_VISIBILITY_STORAGE_KEY)).toBe(
      null,
    );
  });

  it("separates today's mixed agenda from scheduled execution", () => {
    const today = SIDEBAR_NAV_ITEMS.find((item) => item.key === "reminders");
    const scheduled = SIDEBAR_NAV_ITEMS.find(
      (item) => item.key === "cron-jobs",
    );

    expect(today).toMatchObject({ label: "今日安排" });
    expect(today?.description).toContain("提醒");
    expect(today?.description).toContain("定时执行");
    expect(today?.description).not.toContain("日历");
    expect(today?.description).not.toContain("自动化");
    expect(scheduled).toMatchObject({ label: "定时任务" });
    expect(scheduled?.description).toContain("自动执行");
  });

  it("does not save profile visibility overlays as manual preferences", () => {
    const manualKeys = saveSidebarVisibleKeys([
      "chat",
      "works",
      "memory",
      "agents",
      "skills",
      "settings",
    ]);
    const storedBefore = window.localStorage.getItem(
      SIDEBAR_VISIBILITY_STORAGE_KEY,
    );

    const profileSources = {
      "navigation.sidebar.memory.visible": { layer: "profile" },
      "navigation.sidebar.agents.visible": { layer: "profile" },
      "navigation.sidebar.skills.visible": { layer: "profile" },
    };
    const visibleKeys = applyCustomizationSidebarValues(
      {
        "navigation.sidebar.memory.visible": false,
        "navigation.sidebar.agents.visible": false,
        "navigation.sidebar.skills.visible": false,
      },
      profileSources,
    );

    expect(visibleKeys.has("memory")).toBe(false);
    expect(visibleKeys.has("agents")).toBe(false);
    expect(visibleKeys.has("skills")).toBe(false);
    expect(window.localStorage.getItem(SIDEBAR_VISIBILITY_STORAGE_KEY)).toBe(
      storedBefore,
    );
    expect(loadSidebarVisibleKeys()).toEqual(manualKeys);
  });

  it("does not let legacy customization resurrect the internal task center", () => {
    const visibleKeys = applyCustomizationSidebarValues(
      {
        "navigation.sidebar.tasks.visible": true,
      },
      {
        "navigation.sidebar.tasks.visible": { layer: "factory" },
      },
    );

    expect(visibleKeys.has("task-center")).toBe(false);
  });

  it("does not let a legacy profile expose the internal task center", () => {
    const visibleKeys = applyCustomizationSidebarValues(
      {
        "navigation.sidebar.tasks.visible": true,
      },
      {
        "navigation.sidebar.tasks.visible": {
          layer: "profile",
          profile_id: "focused-profile",
        },
      },
    );

    expect(visibleKeys.has("task-center")).toBe(false);
  });
});
