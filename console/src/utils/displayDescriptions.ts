import type { TFunction } from "i18next";
import type { ToolInfo } from "@/api/modules/tools";
import type { PoolSkillSpec, SkillSpec } from "@/api/types";

type DescribedSkill = Pick<SkillSpec | PoolSkillSpec, "name" | "description">;
type DescribedTool = Pick<ToolInfo, "name" | "description">;

const EMPTY_DESCRIPTION = "-";

function translateDescription(
  t: TFunction,
  key: string,
  fallback?: string,
): string {
  const defaultValue = fallback || EMPTY_DESCRIPTION;
  const translated = t(key, { defaultValue });
  return typeof translated === "string" && translated.trim()
    ? translated
    : defaultValue;
}

export function getLocalizedSkillDescription(
  skill: DescribedSkill,
  t: TFunction,
): string {
  return translateDescription(
    t,
    `skillDescriptions.${skill.name}`,
    skill.description,
  );
}

export function getLocalizedToolDescription(
  tool: DescribedTool,
  t: TFunction,
): string {
  return translateDescription(
    t,
    `toolDescriptions.${tool.name}`,
    tool.description,
  );
}
