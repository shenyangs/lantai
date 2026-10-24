import { describe, expect, it } from "vitest";
import {
  isCustomizationDraftToolName,
  parseCustomizationDraftOutput,
} from "./customizationDraft";

describe("customizationDraft", () => {
  it("parses the proposal-only tool envelope", () => {
    const payload = {
      type: "lantai_customization_draft",
      summary: "隐藏左侧作品入口",
      profile: { id: "profile_chat", name: "主对话改造" },
      plan: {
        id: "plan_123",
        target_profile_id: "profile_chat",
        base_revision: 0,
        operations: [{ op: "set", path: "navigation.sidebar.artifacts.visible", value: false }],
      },
      preview: {
        plan: {},
        changed: { "navigation.sidebar.artifacts.visible": { before: true, after: false } },
        ignored: {},
        risk: "low",
        requires_confirmation: false,
        warnings: [],
      },
    };
    const parsed = parseCustomizationDraftOutput([
      { type: "text", text: JSON.stringify(payload) },
    ]);
    expect(parsed?.plan.id).toBe("plan_123");
    expect(parsed?.profile.id).toBe("profile_chat");
  });

  it("recognizes only the dedicated proposal tool", () => {
    expect(isCustomizationDraftToolName("prepare_lantai_customization")).toBe(true);
    expect(isCustomizationDraftToolName("write_file")).toBe(false);
  });
});
