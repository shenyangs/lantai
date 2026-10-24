import { describe, expect, it } from "vitest";
import type { CustomizationRegistryItem } from "../../../api/modules/customization";
import {
  booleanStateLabel,
  formatCustomizationValue,
  getFieldPresentation,
  getNumberMeaning,
  getOptionLabel,
  valuesEqual,
} from "./customizationPresentation";

const item = (partial: Partial<CustomizationRegistryItem>): CustomizationRegistryItem => ({
  id: "appearance.theme_mode",
  category: "appearance",
  title: "明暗模式",
  description: "",
  value_type: "string",
  factory_value: "system",
  scopes: ["global"],
  merge_strategy: "override",
  risk: "low",
  ...partial,
});

describe("customization presentation", () => {
  it("translates internal enum values into user-facing Chinese", () => {
    const theme = item({ enum: ["light", "dark", "system"] });
    expect(getOptionLabel(theme, "system")).toBe("跟随系统");
    expect(formatCustomizationValue(theme, "dark")).toBe("深色");
  });

  it("keeps raw objects and permission controls in expert mode", () => {
    expect(
      getFieldPresentation(
        item({ id: "appearance.design_tokens", value_type: "object", factory_value: {} }),
      ).level,
    ).toBe("expert");
    expect(
      getFieldPresentation(
        item({ id: "permissions.tools.allowed", risk: "high", value_type: "string_array" }),
      ).level,
    ).toBe("expert");
  });

  it("explains booleans and numeric intensity without raw implementation values", () => {
    expect(booleanStateLabel(item({ id: "navigation.sidebar.memory.visible" }), false)).toBe("隐藏");
    expect(getNumberMeaning(item({ id: "persona.proactivity" }), 0.5)).toBe("适中·推荐");
    expect(
      formatCustomizationValue(
        item({ id: "persona.proactivity", value_type: "number", maximum: 1 }),
        0.5,
      ),
    ).toBe("50%");
  });

  it("compares structured values deterministically for no-op protection", () => {
    expect(valuesEqual({ a: 1 }, { a: 1 })).toBe(true);
    expect(valuesEqual(["a"], ["b"])).toBe(false);
  });
});
