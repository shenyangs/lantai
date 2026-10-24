import { describe, expect, it } from "vitest";

import { getSkillDisplayName } from "./skillPresentation";

describe("getSkillDisplayName", () => {
  it("gives built-in role skills a user-facing Chinese name", () => {
    expect(getSkillDisplayName("americano")).toBe("策略与决策");
    expect(getSkillDisplayName("black_coffee")).toBe("财务与会计");
  });

  it("keeps an unknown custom skill readable without changing its identifier", () => {
    expect(getSkillDisplayName("my-custom_skill")).toBe("My Custom Skill");
  });
});
