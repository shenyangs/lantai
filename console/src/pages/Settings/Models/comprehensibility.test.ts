import { describe, expect, it } from "vitest";

import zh from "../../../locales/zh.json";

describe("model settings language", () => {
  it("describes observable effects without requiring runtime vocabulary", () => {
    expect(zh.models.defaultLlm).toBe("全局后备模型");
    expect(zh.models.pageDescription).toContain("未单独选模型的智能体");
    expect(zh.models.llmDescription).toContain("该智能体在聊天顶部的选择");
    expect(zh.models.pageDescription).not.toContain("新对话默认");
    expect(zh.models.utilityModel).toBe("回答后的后台整理");
    expect(zh.models.utilityFollowDescription).not.toMatch(/L1|首 token/);
    expect(zh.models.utilityFollowDescription).toContain("回答之外的模型请求");
    expect(zh.models.utilityFollowDescription).toContain("当前聊天模型提供商的额度");
    expect(zh.models.utilityDedicatedDescription).toContain(
      "独立模型提供商的额度",
    );
    expect(zh.models.utilityDisabledDescription).toContain(
      "不发起任何辅助模型请求",
    );
    expect(zh.models.providerAvailable).toBe("配置齐全 · 待验证");
  });
});
