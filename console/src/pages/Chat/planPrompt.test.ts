import { describe, expect, it } from "vitest";
import { buildPlanPrompt, isPlanCommand } from "./planPrompt";

describe("explicit plan prompt", () => {
  it("wraps a prompt only after the user enables plan mode", () => {
    expect(buildPlanPrompt("帮我修复前端滚动问题")).toBe(
      "/plan 帮我修复前端滚动问题",
    );
  });

  it("does not double-wrap an explicit /plan prompt", () => {
    expect(isPlanCommand("/plan 先拆一下任务")).toBe(true);
    expect(buildPlanPrompt("/plan 先拆一下任务")).toBe("/plan 先拆一下任务");
  });
});
