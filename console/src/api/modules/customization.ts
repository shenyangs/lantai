import { request } from "../request";

export const CUSTOMIZATION_CHANGED_EVENT = "lantai:customization-changed";

export function notifyCustomizationChanged() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(CUSTOMIZATION_CHANGED_EVENT));
  }
}

export type CustomizationRisk = "low" | "medium" | "high" | "forbidden";
export type CustomizationMode = "normal" | "factory" | "safe";

export interface CustomizationRegistryItem {
  id: string;
  category: string;
  title: string;
  description: string;
  value_type: "boolean" | "string" | "number" | "string_array" | "object";
  factory_value: unknown;
  scopes: string[];
  merge_strategy: string;
  risk: CustomizationRisk;
  enum?: unknown[] | null;
  minimum?: number | null;
  maximum?: number | null;
}

export interface CustomizationProfile {
  id: string;
  name: string;
  description: string;
  icon: string;
  color: string;
  revision: number;
  updated_at: string;
  overrides: Record<string, unknown>;
  scope: { kind: string; ref?: string | null };
}

export interface CustomizationSlot {
  index: number;
  profile_id: string | null;
  label: string | null;
  icon: string | null;
  color: string | null;
}

export interface CustomizationState {
  mode: CustomizationMode;
  active_profile_id: string | null;
  active_scoped_profiles: string[];
  last_known_good: Record<string, number>;
  pending_boot_validation?: Record<string, number>;
  updated_at: string;
}

export interface CustomizationEffectiveState {
  values: Record<string, unknown>;
  sources: Record<
    string,
    { layer: string; profile_id?: string | null; revision?: number | null }
  >;
  ignored: Record<string, string>;
  warnings: string[];
}

export interface CustomizationOperation {
  op: "set" | "unset";
  path: string;
  value?: unknown;
}

export interface CustomizationPlan {
  id?: string;
  target_profile_id: string;
  base_revision: number;
  scope?: { kind: string; ref?: string | null };
  intent?: string;
  operations: CustomizationOperation[];
}

export interface CustomizationPreview {
  plan: CustomizationPlan;
  changed: Record<string, { before: unknown; after: unknown }>;
  ignored: Record<string, string>;
  risk: CustomizationRisk;
  requires_confirmation: boolean;
  warnings: string[];
}

export interface CustomizationRevision {
  profile_id: string;
  revision: number;
  parent_revision: number | null;
  actor: string;
  source: string;
  intent: string;
  risk: CustomizationRisk;
  created_at: string;
  status: "committed" | "rolled_back" | "migrated";
}

export interface CustomizationTemplate {
  format: "lantai-profile";
  format_version: 1;
  registry_schema_version: number;
  profile: {
    name: string;
    description: string;
    icon: string;
    color: string;
    overrides: Record<string, unknown>;
  };
  requirements: Array<Record<string, unknown>>;
  omitted: string[];
}

export const customizationApi = {
  getRegistry: () =>
    request<{ schema_version: number; items: CustomizationRegistryItem[] }>(
      "/customization/registry",
    ),
  getState: () => request<CustomizationState>("/customization/state"),
  getEffectiveState: () =>
    request<CustomizationEffectiveState>("/customization/effective-state"),
  listProfiles: () =>
    request<CustomizationProfile[]>("/customization/profiles"),
  createProfile: (body: Partial<CustomizationProfile>) =>
    request<CustomizationProfile>("/customization/profiles", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateProfile: (
    profileId: string,
    body: Pick<
      Partial<CustomizationProfile>,
      "name" | "description" | "icon" | "color"
    >,
  ) =>
    request<CustomizationProfile>(`/customization/profiles/${profileId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteProfile: (profileId: string) =>
    request<void>(`/customization/profiles/${profileId}`, {
      method: "DELETE",
    }),
  copyProfile: (profileId: string, name?: string) =>
    request<CustomizationProfile>(
      `/customization/profiles/${profileId}/copy`,
      {
        method: "POST",
        body: JSON.stringify({ name }),
      },
    ),
  listRevisions: (profileId: string) =>
    request<CustomizationRevision[]>(
      `/customization/profiles/${profileId}/revisions`,
    ),
  restoreRevision: (profileId: string, revision: number) =>
    request<{ status: "committed" | "rolled_back" }>(
      `/customization/profiles/${profileId}/revisions/${revision}/restore`,
      { method: "POST" },
    ),
  undoLast: (profileId: string) =>
    request<{ status: "committed" | "rolled_back" }>(
      `/customization/profiles/${profileId}/undo`,
      { method: "POST" },
    ),
  exportProfile: (profileId: string) =>
    request<CustomizationTemplate>(
      `/customization/profiles/${profileId}/export`,
    ),
  importProfile: (template: CustomizationTemplate | Record<string, unknown>) =>
    request<{
      profile: CustomizationProfile;
      report: {
        accepted: string[];
        ignored: Record<string, string>;
        activated: false;
      };
    }>("/customization/profiles/import", {
      method: "POST",
      body: JSON.stringify(template),
    }),
  listSlots: () => request<CustomizationSlot[]>("/customization/slots"),
  setSlot: (index: number, body: Partial<CustomizationSlot>) =>
    request<CustomizationSlot>(`/customization/slots/${index}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  activateProfile: (profileId: string) =>
    request<CustomizationState>(
      `/customization/profiles/${profileId}/activate`,
      { method: "POST" },
    ),
  previewPlan: (plan: CustomizationPlan) =>
    request<CustomizationPreview>("/customization/plans/preview", {
      method: "POST",
      body: JSON.stringify(plan),
    }),
  planFromIntent: (body: {
    intent: string;
    target_profile_id: string;
    agent_id?: string;
  }) =>
    request<{
      summary: string;
      plan: CustomizationPlan;
      preview: CustomizationPreview;
      model_label: string;
    }>("/customization/plans/from-intent", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  confirmPlan: (planId: string) =>
    request<{ token: string; expires_at: string }>(
      `/customization/plans/${planId}/confirm`,
      { method: "POST" },
    ),
  applyPlan: (planId: string, confirmationToken?: string) =>
    request<{ status: "committed" | "rolled_back" }>(
      "/customization/plans/apply",
      {
        method: "POST",
        body: JSON.stringify({
          plan_id: planId,
          confirmation_token: confirmationToken,
        }),
      },
    ),
  enableFactoryMode: () =>
    request<CustomizationState>("/customization/recovery/factory-mode", {
      method: "POST",
    }),
  enableSafeMode: () =>
    request<CustomizationState>("/customization/recovery/safe-mode", {
      method: "POST",
    }),
  enableNormalMode: () =>
    request<CustomizationState>("/customization/recovery/normal-mode", {
      method: "POST",
    }),
  restoreLastKnownGood: (profileId: string) =>
    request<{ status: "committed" | "rolled_back" }>(
      `/customization/recovery/last-known-good/${profileId}`,
      { method: "POST" },
    ),
};
