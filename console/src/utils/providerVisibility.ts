import type { ActiveModelsInfo, ProviderInfo } from "@/api/types";

const PRODUCT_HIDDEN_PROVIDER_IDS = new Set(["lantai-local"]);

export function isProductVisibleProviderId(providerId?: string | null) {
  if (!providerId) return false;
  return !PRODUCT_HIDDEN_PROVIDER_IDS.has(providerId);
}

export function filterProductVisibleProviders<T extends { id: string }>(
  providers: T[],
) {
  return providers.filter((provider) =>
    isProductVisibleProviderId(provider.id),
  );
}

export function stripProductHiddenActiveModel(
  activeModels: ActiveModelsInfo | null,
): ActiveModelsInfo | null {
  const providerId = activeModels?.active_llm?.provider_id;
  if (!providerId || isProductVisibleProviderId(providerId)) {
    return activeModels;
  }

  return {
    ...activeModels,
    active_llm: undefined,
  };
}

export function isProductVisibleProvider(provider: ProviderInfo) {
  return isProductVisibleProviderId(provider.id);
}
