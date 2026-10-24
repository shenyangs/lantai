import type {
  CustomizationPlan,
  CustomizationPreview,
} from "../../../api/modules/customization";

export const CUSTOMIZATION_DRAFT_TOOL_NAME = "prepare_lantai_customization";

export interface CustomizationDraftPayload {
  type: "lantai_customization_draft";
  summary: string;
  profile: { id: string; name: string; created_for_draft?: boolean };
  plan: CustomizationPlan;
  preview: CustomizationPreview;
}

export function isCustomizationDraftToolName(toolName: string): boolean {
  return toolName === CUSTOMIZATION_DRAFT_TOOL_NAME;
}

function parseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text || !/^[{[]/.test(text)) return value;
  try {
    return JSON.parse(text);
  } catch {
    return value;
  }
}

function record(value: unknown): Record<string, unknown> | null {
  const parsed = parseJson(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  return parsed as Record<string, unknown>;
}

function findPayload(value: unknown, depth = 0): Record<string, unknown> | null {
  if (depth > 6) return null;
  const parsed = parseJson(value);
  if (Array.isArray(parsed)) {
    for (const item of parsed) {
      const found = findPayload(item, depth + 1);
      if (found) return found;
    }
    return null;
  }
  const source = record(parsed);
  if (!source) return null;
  if (source.type === "lantai_customization_draft") return source;
  if (typeof source.text === "string") {
    const found = findPayload(source.text, depth + 1);
    if (found) return found;
  }
  for (const key of ["output", "result", "content", "data"]) {
    if (source[key] !== undefined) {
      const found = findPayload(source[key], depth + 1);
      if (found) return found;
    }
  }
  return null;
}

export function parseCustomizationDraftOutput(
  output: unknown,
): CustomizationDraftPayload | null {
  const payload = findPayload(output);
  const profile = record(payload?.profile);
  const plan = record(payload?.plan);
  const preview = record(payload?.preview);
  if (
    !payload ||
    !profile ||
    !plan ||
    !preview ||
    typeof profile.id !== "string" ||
    typeof plan.id !== "string" ||
    !Array.isArray(plan.operations) ||
    !record(preview.changed)
  ) {
    return null;
  }
  return payload as unknown as CustomizationDraftPayload;
}
