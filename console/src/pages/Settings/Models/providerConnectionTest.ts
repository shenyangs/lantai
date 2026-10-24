import type {
  ActiveModelsInfo,
  ProviderInfo,
} from "../../../api/types/provider";

function firstAvailableModelId(provider: ProviderInfo): string | undefined {
  const model = [...provider.models, ...provider.extra_models].find((item) =>
    item.id?.trim(),
  );
  return model?.id.trim();
}

export function getProviderConnectionTestModelId(
  provider: ProviderInfo,
  activeModels: ActiveModelsInfo | null,
): string | undefined {
  const activeLlm = activeModels?.active_llm;
  if (activeLlm?.provider_id === provider.id && activeLlm.model.trim()) {
    return activeLlm.model.trim();
  }
  return firstAvailableModelId(provider);
}

export function canTestProviderConnection(
  provider: ProviderInfo,
  activeModels: ActiveModelsInfo | null,
): boolean {
  return (
    provider.support_connection_check ||
    Boolean(getProviderConnectionTestModelId(provider, activeModels))
  );
}

export function shouldValidateProviderConnectionBeforeSave(
  provider: ProviderInfo,
  activeModels: ActiveModelsInfo | null,
): boolean {
  // TokenDance credentials are identity-bound. A newly entered key must be
  // persisted with its Watcha owner before any request can use it.
  if (provider.id === "tokendance") {
    return false;
  }
  // A local runtime may intentionally be configured before the user starts
  // its server. Keep explicit connection testing available, but never make
  // an offline process block saving a valid endpoint.
  if (provider.is_local) {
    return false;
  }
  if (provider.support_connection_check) {
    return true;
  }
  return (
    !provider.is_custom &&
    Boolean(getProviderConnectionTestModelId(provider, activeModels))
  );
}
