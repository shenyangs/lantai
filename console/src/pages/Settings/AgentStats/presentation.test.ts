import { describe, expect, it } from "vitest";
import { toolSuccessRateLabel } from "./presentation";

describe("toolSuccessRateLabel", () => {
  it("does not present missing tool data as a zero-percent success rate", () => {
    expect(toolSuccessRateLabel(0, 0)).toBe("—");
  });

  it("formats a measured success rate", () => {
    expect(toolSuccessRateLabel(8, 87.5)).toBe("87.5%");
  });
});
