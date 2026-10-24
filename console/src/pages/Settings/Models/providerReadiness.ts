import type { ProviderInfo } from "../../../api/types";

export type RemoteProviderReadiness =
  | "not_configured"
  | "needs_models"
  | "needs_connection_check"
  | "configured_unverified";

export function remoteProviderReadiness(
  provider: ProviderInfo,
): RemoteProviderReadiness {
  const hasRequiredUrl = !provider.is_custom || Boolean(provider.base_url);
  const hasRequiredCredential =
    provider.require_api_key === false || Boolean(provider.api_key);
  const configured =
    provider.id === "lantai-local" || (hasRequiredUrl && hasRequiredCredential);

  if (!configured) return "not_configured";
  if (provider.models.length + provider.extra_models.length === 0) {
    return "needs_models";
  }
  if (provider.id === "kimi-codingplan" && provider.support_connection_check) {
    return "needs_connection_check";
  }
  // ProviderInfo has no persisted successful-call receipt. Configuration and
  // model discovery can only establish that a provider is ready to verify.
  return "configured_unverified";
}
