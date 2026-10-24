import type { Capability, SuggestedSkillDraft } from "../../../api/types";

export function attachSuggestedSkillToCapability(
  capability: Capability,
  skill: Pick<SuggestedSkillDraft, "name" | "attach_to_step_index">,
): Capability {
  const fallbackIndex = capability.steps.findIndex(
    (step) => step.kind === "agent_prompt",
  );
  const requestedIndex =
    typeof skill.attach_to_step_index === "number"
      ? skill.attach_to_step_index
      : fallbackIndex;
  const attachIndex =
    requestedIndex >= 0 && requestedIndex < capability.steps.length
      ? requestedIndex
      : fallbackIndex;
  if (attachIndex < 0) return capability;

  return {
    ...capability,
    steps: capability.steps.map((step, index) =>
      index === attachIndex && step.kind === "agent_prompt"
        ? {
            ...step,
            kind: "skill_prompt",
            skill_name: skill.name,
          }
        : step,
    ),
  };
}
