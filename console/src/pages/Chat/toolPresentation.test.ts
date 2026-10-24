import { describe, expect, it } from "vitest";
import { resolveToolPresentation } from "./toolPresentation";

describe("Skill presentation boundary", () => {
  it("keeps an explicitly user-invoked Skill visible", () => {
    const presentation = resolveToolPresentation("read_skill", {
      content: [
        {
          data: {
            name: "read_skill",
            presentation: {
              visibility: "visible",
              surface: "tool",
              risk_level: "low",
              display_name: "读取技能",
              summary: "正在读取 puer",
            },
          },
        },
      ],
    });

    expect(presentation?.visibility).toBe("visible");
    expect(presentation?.surface).toBe("tool");
    expect(presentation?.display_name).toBe("读取技能");
  });

  it("honors the backend's private sub-agent implementation marker", () => {
    const presentation = resolveToolPresentation("read_skill", {
      content: [
        {
          data: {
            name: "read_skill",
            presentation: {
              visibility: "hidden",
              surface: "debug",
              risk_level: "low",
              display_name: "内部专业方法",
              summary: "内部专业方法已就绪",
            },
          },
        },
      ],
    });

    expect(presentation?.visibility).toBe("hidden");
    expect(presentation?.surface).toBe("debug");
  });
});
