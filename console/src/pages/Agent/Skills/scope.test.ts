import { describe, expect, it } from "vitest";

import { describeSkillChannelScope } from "./scope";

describe("skill channel scope", () => {
  it("explains all as channels, not all agents", () => {
    expect(describeSkillChannelScope(["all"])).toBe("可用于所有消息渠道");
    expect(describeSkillChannelScope(["console", "wechat"])).toBe(
      "适用于 console、wechat 渠道",
    );
  });
});
