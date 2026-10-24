import { describe, expect, it } from "vitest";
import {
  getDetailNavigation,
  SETTINGS_DETAIL_NAVIGATION,
} from "./detailNavigation";

describe("settings detail navigation contract", () => {
  it("covers the audited settings entry routes", () => {
    expect(getDetailNavigation("/terminal")?.to).toBe("/settings");
    expect(getDetailNavigation("/innovation-lab")?.to).toBe("/settings");
  });

  it("keeps every destination internal and consistently labelled", () => {
    for (const navigation of Object.values(SETTINGS_DETAIL_NAVIGATION)) {
      expect(navigation.to).toMatch(/^\/(?!\/)/);
      expect(navigation.label).toBe("返回设置中心");
    }
  });

  it("does not treat top-level capability routes as settings details", () => {
    expect(getDetailNavigation("/skills")).toBeNull();
    expect(getDetailNavigation("/capabilities")).toBeNull();
    expect(getDetailNavigation("/agents")).toBeNull();
    expect(getDetailNavigation("/tools")).toBeNull();
    expect(getDetailNavigation("/channels")).toBeNull();
    expect(getDetailNavigation("/cron-jobs")).toBeNull();
    expect(getDetailNavigation("/memory")).toBeNull();
    expect(getDetailNavigation("/calendar")).toBeNull();
    expect(getDetailNavigation("/voice-transcription")).toBeNull();
    expect(getDetailNavigation("/acp")).toBeNull();
  });
});
