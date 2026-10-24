import { describe, expect, it } from "vitest";
import type { Capability } from "../../../api/types";
import { attachSuggestedSkillToCapability } from "./suggestedSkills";

function capability(): Capability {
  const now = new Date().toISOString();
  return {
    id: "cap-1",
    name: "线索整理",
    description: "",
    version: 1,
    enabled: false,
    tags: [],
    parameters: [],
    steps: [
      {
        id: "step-1",
        index: 0,
        title: "执行任务",
        mode: "dynamic",
        kind: "agent_prompt",
        instruction_template: "{{task_goal}}",
        required_parameters: ["task_goal"],
        risk_level: "low",
      },
      {
        id: "step-2",
        index: 1,
        title: "交付",
        mode: "fixed",
        kind: "delivery",
        instruction_template: "交付结果",
        required_parameters: [],
        risk_level: "low",
      },
    ],
    delivery_defaults: [],
    risk_policy: { require_approval_for: ["high"] },
    created_at: now,
    updated_at: now,
  };
}

describe("attachSuggestedSkillToCapability", () => {
  it("converts the target agent step to a skill step", () => {
    const updated = attachSuggestedSkillToCapability(capability(), {
      name: "lead-summary",
      attach_to_step_index: 0,
    });

    expect(updated.steps[0]).toMatchObject({
      kind: "skill_prompt",
      skill_name: "lead-summary",
    });
    expect(updated.steps[1].kind).toBe("delivery");
  });
});
