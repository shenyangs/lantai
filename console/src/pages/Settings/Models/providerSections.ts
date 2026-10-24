import type { ProviderInfo } from "../../../api/types";

function configured(provider: ProviderInfo): boolean {
  if (provider.is_custom && provider.base_url) return true;
  if (provider.require_api_key === false) return true;
  return Boolean(provider.require_api_key && provider.api_key);
}

function priority(provider: ProviderInfo): number {
  const isConfigured = configured(provider);
  const isAvailable =
    isConfigured && provider.models.length + provider.extra_models.length > 0;
  if (isAvailable && provider.is_custom) return 0;
  if (isAvailable) return 1;
  if (provider.is_custom) return 2;
  if (isConfigured) return 3;
  return 4;
}

export function isTokenDanceCredentialConnected(
  provider: ProviderInfo,
): boolean {
  return (
    provider.id === "tokendance" &&
    Boolean(provider.api_key) &&
    provider.meta?.credential_status === "active"
  );
}

export function partitionProviderSections(
  providers: ProviderInfo[],
  searchQuery: string,
): {
  tokendanceProvider: ProviderInfo | null;
  regularProviders: ProviderInfo[];
  localProviders: ProviderInfo[];
} {
  const tokendanceProvider =
    providers.find((provider) => provider.id === "tokendance") ?? null;
  const regularProviders = providers
    .filter((provider) => !provider.is_local && provider.id !== "tokendance")
    .sort((left, right) => priority(left) - priority(right));
  const localProviders = providers.filter((provider) => provider.is_local);
  const query = searchQuery.trim().toLowerCase();
  if (!query) {
    return { tokendanceProvider, regularProviders, localProviders };
  }
  return {
    tokendanceProvider,
    regularProviders: regularProviders.filter((provider) =>
      provider.name.toLowerCase().includes(query),
    ),
    localProviders: localProviders.filter((provider) =>
      provider.name.toLowerCase().includes(query),
    ),
  };
}
