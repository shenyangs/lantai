import { describe, expect, it } from "vitest";

import {
  TOKEN_USAGE_POPOVER_OFFSET,
  TOKEN_USAGE_POPOVER_PLACEMENT,
} from "./tokenUsagePopover";

describe("ChatTokenUsageMeter", () => {
  it("keeps the popover centered above the meter with a small gap", () => {
    expect(TOKEN_USAGE_POPOVER_PLACEMENT).toBe("top");
    expect(TOKEN_USAGE_POPOVER_OFFSET).toEqual([0, -12]);
  });
});
