import type {
  ActiveModelsInfo,
  ProviderInfo,
} from "../../../api/types/provider";
import type { ModelSetupMode } from "../../../utils/modelSetupFlow";

export type ModelSetupReadinessStatus =
  | "not_configured"
  | "model_not_selected"
  | "configured_unverified"
  | "verification_stale"
  | "verified";

export interface ModelSetupReadiness {
  status: ModelSetupReadinessStatus;
  provider: ProviderInfo | null;
  modelId: string | null;
  providerConfigured: boolean;
  modelSelected: boolean;
  connectionVerified: boolean;
  modelVerified: boolean;
}

interface DeriveModelSetupReadinessInput {
  mode: ModelSetupMode;
  providers: ProviderInfo[];
  activeModels: ActiveModelsInfo | null;
  targetProviderId?: string | null;
}

function providerMatchesMode(
  provider: ProviderInfo,
  mode: ModelSetupMode,
): boolean {
  if (mode === "watcha") return provider.id === "tokendance";
  if (mode === "local") return provider.is_local;
  return !provider.is_local && provider.id !== "tokendance";
}

function providerHasCompleteConfig(provider: ProviderInfo): boolean {
  if (!provider.base_url.trim()) return false;
  return provider.require_api_key === false || Boolean(provider.api_key);
}

function resolveTargetProvider(
  input: DeriveModelSetupReadinessInput,
): ProviderInfo | null {
  const { activeModels, mode, providers, targetProviderId } = input;
  const explicitTarget = targetProviderId
    ? providers.find((provider) => provider.id === targetProviderId)
    : undefined;
  if (explicitTarget && providerMatchesMode(explicitTarget, mode)) {
    return explicitTarget;
  }

  const activeProviderId = activeModels?.active_llm?.provider_id;
  const activeProvider = providers.find(
    (provider) => provider.id === activeProviderId,
  );
  if (activeProvider && providerMatchesMode(activeProvider, mode)) {
    return activeProvider;
  }

  if (mode === "watcha") {
    return providers.find((provider) => provider.id === "tokendance") ?? null;
  }

  if (mode === "local") {
    return (
      providers.find(
        (provider) =>
          providerMatchesMode(provider, mode) &&
          providerHasCompleteConfig(provider) &&
          provider.models.length + provider.extra_models.length > 0,
      ) ?? null
    );
  }

  return null;
}

export function deriveModelSetupReadiness(
  input: DeriveModelSetupReadinessInput,
): ModelSetupReadiness {
  const provider = resolveTargetProvider(input);
  const providerConfigured = Boolean(
    provider && providerHasCompleteConfig(provider),
  );
  const activeSlot = input.activeModels?.active_llm;
  const modelId =
    provider && activeSlot?.provider_id === provider.id && activeSlot.model
      ? activeSlot.model
      : null;
  const modelSelected = Boolean(modelId);
  const revision = provider?.configuration_revision ?? 1;
  const connectionVerified = Boolean(
    providerConfigured &&
      provider?.connection_verified_at &&
      provider.connection_verified_revision === revision,
  );
  const modelVerified = Boolean(
    modelSelected &&
      connectionVerified &&
      provider?.model_verified_at &&
      provider.model_verified_revision === revision &&
      provider.model_verified_id === modelId,
  );
  const hasHistoricalReceipt = Boolean(
    provider?.connection_verified_revision != null ||
      provider?.model_verified_revision != null,
  );

  let status: ModelSetupReadinessStatus;
  if (!providerConfigured) {
    status = "not_configured";
  } else if (!modelSelected) {
    status = "model_not_selected";
  } else if (modelVerified) {
    status = "verified";
  } else if (hasHistoricalReceipt) {
    status = "verification_stale";
  } else {
    status = "configured_unverified";
  }

  return {
    status,
    provider,
    modelId,
    providerConfigured,
    modelSelected,
    connectionVerified,
    modelVerified,
  };
}
