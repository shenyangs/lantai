import React from "react";
import type { ProviderInfo, ActiveModelsInfo } from "../../../../../api/types";
import { RemoteProviderCard } from "./RemoteProviderCard";
import { LocalProviderCard } from "./LocalProviderCard";
import { isProductVisibleProvider } from "@/utils/providerVisibility";

interface ProviderCardProps {
  provider: ProviderInfo;
  activeModels: ActiveModelsInfo | null;
  onSaved: () => void;
  featured?: boolean;
  featuredDescription?: string;
  settingsOpen?: boolean;
  onSettingsOpenChange?: (open: boolean) => void;
}

export const ProviderCard = React.memo(function ProviderCard({
  provider,
  activeModels,
  onSaved,
  featured = false,
  featuredDescription,
  settingsOpen,
  onSettingsOpenChange,
}: ProviderCardProps) {
  if (!isProductVisibleProvider(provider)) {
    return null;
  }

  if (provider.id === "ollama" || provider.id === "lmstudio") {
    return (
      <LocalProviderCard
        provider={provider}
        activeModels={activeModels}
        onSaved={onSaved}
      />
    );
  }

  return (
    <RemoteProviderCard
      provider={provider}
      activeModels={activeModels}
      onSaved={onSaved}
      featured={featured}
      featuredDescription={featuredDescription}
      settingsOpen={settingsOpen}
      onSettingsOpenChange={onSettingsOpenChange}
    />
  );
});
