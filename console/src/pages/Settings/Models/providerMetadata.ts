import type { ProviderInfo } from "../../../api/types/provider";

function normalizeProviderMetaUrl(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }

  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }

  try {
    const url = new URL(trimmed);
    return url.protocol === "https:" || url.protocol === "http:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

export function getProviderDocsUrl(provider: ProviderInfo): string | null {
  return normalizeProviderMetaUrl(provider.meta?.docs_url);
}

export function getProviderApiKeyUrl(provider: ProviderInfo): string | null {
  return normalizeProviderMetaUrl(provider.meta?.api_key_url);
}
