import { describe, expect, it } from "vitest";

import { describeToolState, primaryToolDescription } from "./presentation";

describe("tool comprehensibility", () => {
  it("never equates non-empty configuration with a verified tool", () => {
    expect(
      describeToolState({
        name: "connector",
        enabled: true,
        description: "Connector",
        async_execution: false,
        icon: "",
        requires_config: true,
        config_fields: [
          { name: "token", label: "Token", type: "password", required: true },
        ],
        config_values: { token: "***" },
      }),
    ).toMatchObject({ key: "needs_verification", label: "配置齐全 · 待验证" });
  });

  it("distinguishes disabled and incomplete configuration", () => {
    expect(
      describeToolState({
        name: "connector",
        enabled: false,
        description: "Connector",
        async_execution: false,
        icon: "",
      }).key,
    ).toBe("disabled");
    expect(
      describeToolState({
        name: "connector",
        enabled: true,
        description: "Connector",
        async_execution: false,
        icon: "",
        requires_config: true,
        config_fields: [
          { name: "token", label: "Token", type: "password", required: true },
        ],
        config_values: {},
      }).key,
    ).toBe("needs_configuration");
  });

  it("keeps untranslated implementation copy out of the primary description", () => {
    expect(
      primaryToolDescription(
        {
          name: "run_skill_script",
          enabled: true,
          description: "Run a script bundled with an enabled Skill",
          async_execution: false,
          icon: "",
          category_description: "执行命令、脚本和本地开发流程",
        },
        "Run a script bundled with an enabled Skill",
      ),
    ).toBe("执行命令、脚本和本地开发流程");
  });
});
