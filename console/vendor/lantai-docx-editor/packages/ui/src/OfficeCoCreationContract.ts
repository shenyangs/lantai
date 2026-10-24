/** Provider-independent behavioural contract shared by every Lantai Office surface. */
export const OFFICE_CO_CREATION_PROTOCOL_VERSION = "2.0" as const;

export type OfficeSuite = "docs" | "sheets" | "slides" | "pdf" | "markdown";
export type OfficeCoCreationMode = "ask" | "suggest" | "execute";
export type OfficeCoCreationResponseKind = "answer" | "proposal";
export type OfficeSuggestionStatus =
  | "draft"
  | "refining"
  | "pending"
  | "accepted"
  | "rejected"
  | "applied"
  | "verified"
  | "failed"
  | "rolled_back";
export type OfficeTransactionStage =
  | "snapshot"
  | "plan"
  | "preview"
  | "validate"
  | "apply"
  | "reopen"
  | "verify"
  | "commit"
  | "rollback";

export interface OfficeCapabilityProfile {
  readonly suite: OfficeSuite;
  readonly scopes: readonly string[];
  readonly validationDimensions: readonly string[];
  readonly highRiskCopyRequired: boolean;
  readonly durableObjectKind: string;
}

export interface OfficeMutationEnvelope {
  readonly protocolVersion: typeof OFFICE_CO_CREATION_PROTOCOL_VERSION;
  readonly suite: OfficeSuite;
  readonly mode: OfficeCoCreationMode;
  readonly documentId: string;
  readonly objectId: string;
  readonly baseVersion: number | string;
  readonly idempotencyKey: string;
  readonly requestedBy: "user" | "agent";
  readonly stages: readonly OfficeTransactionStage[];
}

export const OFFICE_CAPABILITY_PROFILES: Readonly<
  Record<OfficeSuite, OfficeCapabilityProfile>
> = {
  docs: {
    suite: "docs",
    scopes: ["selection", "paragraph", "section", "document"],
    validationDimensions: ["content", "style", "revision", "reopen"],
    highRiskCopyRequired: false,
    durableObjectKind: "block",
  },
  sheets: {
    suite: "sheets",
    scopes: ["cell", "range", "table", "sheet", "workbook"],
    validationDimensions: [
      "value",
      "formula",
      "dependency",
      "format",
      "recalculate",
      "reopen",
    ],
    highRiskCopyRequired: false,
    durableObjectKind: "range",
  },
  slides: {
    suite: "slides",
    scopes: ["element", "slide", "slide-set", "deck"],
    validationDimensions: ["copy", "data", "geometry", "render", "reopen"],
    highRiskCopyRequired: false,
    durableObjectKind: "shape",
  },
  pdf: {
    suite: "pdf",
    scopes: ["selection", "annotation", "page", "page-set", "document"],
    validationDimensions: [
      "page-count",
      "content",
      "annotation",
      "render",
      "reopen",
    ],
    highRiskCopyRequired: true,
    durableObjectKind: "page-object",
  },
  markdown: {
    suite: "markdown",
    scopes: ["selection", "block", "section", "document"],
    validationDimensions: ["content", "syntax", "render", "reopen"],
    highRiskCopyRequired: false,
    durableObjectKind: "text-range",
  },
};

const SUGGESTION_TRANSITIONS: Readonly<
  Record<OfficeSuggestionStatus, readonly OfficeSuggestionStatus[]>
> = {
  draft: ["refining", "pending", "rejected"],
  refining: ["draft", "pending", "failed", "rejected"],
  pending: ["refining", "accepted", "rejected"],
  accepted: ["applied", "failed"],
  rejected: [],
  applied: ["verified", "failed", "rolled_back"],
  verified: [],
  failed: ["refining", "rolled_back"],
  rolled_back: ["refining", "rejected"],
};

export function canTransitionOfficeSuggestion(
  from: OfficeSuggestionStatus,
  to: OfficeSuggestionStatus,
): boolean {
  return SUGGESTION_TRANSITIONS[from].includes(to);
}

export function assertOfficeSuggestionTransition(
  from: OfficeSuggestionStatus,
  to: OfficeSuggestionStatus,
): void {
  if (!canTransitionOfficeSuggestion(from, to)) {
    throw new Error(`Invalid Office suggestion transition: ${from} -> ${to}`);
  }
}

export function officeModeAllowsMutation(mode: OfficeCoCreationMode): boolean {
  return mode === "execute";
}

export function officeModeRequiresPreview(mode: OfficeCoCreationMode): boolean {
  return mode === "suggest" || mode === "execute";
}

const OFFICE_RESPONSE_MARKER =
  /\n?\[\[LANTAI_OFFICE_RESPONSE:(ANSWER|PROPOSAL)\]\]\s*$/i;

/**
 * The model owns the semantic decision; the client only parses its explicit
 * disposition. Missing or malformed metadata fails closed as a plain answer,
 * so explanatory text can never accidentally become an executable proposal.
 */
export function parseOfficeCoCreationResponse(
  text: string,
  mode: OfficeCoCreationMode,
): {
  readonly text: string;
  readonly responseKind: OfficeCoCreationResponseKind;
} {
  const match = text.match(OFFICE_RESPONSE_MARKER);
  const markerIndex = match?.index;
  return {
    text:
      markerIndex === undefined ? text : text.slice(0, markerIndex).trimEnd(),
    responseKind:
      mode === "suggest" && match?.[1]?.toUpperCase() === "PROPOSAL"
        ? "proposal"
        : "answer",
  };
}

export const OFFICE_MUTATION_STAGES: readonly OfficeTransactionStage[] = [
  "snapshot",
  "plan",
  "preview",
  "validate",
  "apply",
  "reopen",
  "verify",
  "commit",
];

export function validateOfficeMutationStages(
  stages: readonly OfficeTransactionStage[],
): {
  readonly ok: boolean;
  readonly missing: readonly OfficeTransactionStage[];
} {
  if (stages.at(-1) === "rollback")
    return { ok: stages.includes("snapshot"), missing: [] };
  const missing = OFFICE_MUTATION_STAGES.filter(
    (stage) => !stages.includes(stage),
  );
  let previousIndex = -1;
  const ordered = OFFICE_MUTATION_STAGES.every((stage) => {
    const index = stages.indexOf(stage);
    if (index < 0) return true;
    if (index <= previousIndex) return false;
    previousIndex = index;
    return true;
  });
  return { ok: missing.length === 0 && ordered, missing };
}

/** Adds a policy header while preserving the original user wording for display/persistence. */
export function buildOfficeCoCreationInstruction(
  instruction: string,
  mode: OfficeCoCreationMode,
  suite: OfficeSuite,
): string {
  const profile = OFFICE_CAPABILITY_PROFILES[suite];
  const common = [
    `[LANTAI_OFFICE_CO_CREATION protocol=${OFFICE_CO_CREATION_PROTOCOL_VERSION} suite=${suite} mode=${mode}]`,
    `Allowed scopes: ${profile.scopes.join(", ")}.`,
    "Never claim a write, save, export, delivery, verification, or rollback unless the corresponding tool result confirms it.",
  ];
  if (mode === "ask") {
    common.push(
      "READ-ONLY MODE: answer the question. You may inspect the file, but do not call any mutating, saving, exporting, or delivery tool.",
    );
  } else if (mode === "suggest") {
    common.push(
      "SUGGESTION MODE: inspect and propose concrete, reviewable suggestions. Do not mutate, save, export, or deliver the file.",
      "Classify your final response yourself: use PROPOSAL only when it contains a specific modification that can be applied as written and its target scope is clear. Questions about understanding, explanations, summaries, critiques, and follow-up questions are ANSWER responses unless the user explicitly asks you to formulate a concrete change.",
      "End the final response with exactly one metadata line: [[LANTAI_OFFICE_RESPONSE:PROPOSAL]] or [[LANTAI_OFFICE_RESPONSE:ANSWER]]. This line is machine metadata, not user-facing prose.",
    );
  } else {
    common.push(
      "EXECUTION MODE: the user explicitly permits edits for this turn.",
      "Before mutation take a snapshot, plan structured operations, preview and validate them; after applying, reopen/render and verify. Commit only after verification, otherwise roll back visibly.",
      `Validate: ${profile.validationDimensions.join(", ")}. Use durable object/version preconditions and an idempotency key for every write batch.`,
    );
    if (profile.highRiskCopyRequired) {
      common.push(
        "For destructive or structurally risky PDF operations, work on a copy unless the user explicitly chose in-place editing.",
      );
    }
  }
  return `${common.join("\n")}\n[/LANTAI_OFFICE_CO_CREATION]\n\n${instruction}`;
}
