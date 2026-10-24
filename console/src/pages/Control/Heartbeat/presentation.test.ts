import { describe, expect, it } from "vitest";
import { heartbeatReasonLabel } from "./index";

describe("heartbeat result presentation", () => {
  it("explains why a heartbeat stayed quiet", () => {
    expect(heartbeatReasonLabel("outside_active_hours", true)).toContain(
      "活跃时段之外",
    );
    expect(heartbeatReasonLabel("ran_without_dispatch", true)).toContain(
      "没有向外部渠道发送",
    );
  });

  it("does not imply a future run when heartbeat is disabled", () => {
    expect(heartbeatReasonLabel("", false)).toBe("心跳已关闭，不会在后台检查");
  });
});
