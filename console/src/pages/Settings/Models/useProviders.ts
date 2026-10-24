import { useState, useEffect, useCallback, useRef } from "react";
import api from "../../../api";
import type { ProviderInfo, ActiveModelsInfo } from "../../../api/types";
import { useAgentStore } from "../../../stores/agentStore";
import {
  filterProductVisibleProviders,
  stripProductHiddenActiveModel,
} from "@/utils/providerVisibility";
import { createLatestRequestGate } from "../../../utils/latestRequest";

export function useProviders() {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [activeModels, setActiveModels] = useState<ActiveModelsInfo | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { selectedAgent } = useAgentStore();
  const providersLoadGateRef = useRef(createLatestRequestGate());

  const fetchAll = useCallback(async (showLoading = true) => {
    const ownsState = providersLoadGateRef.current.begin();
    if (showLoading) {
      setLoading(true);
    }
    setError(null);
    try {
      const [provData, activeData] = await Promise.all([
        api.listProviders(),
        api.getActiveModels({ scope: "global" }),
      ]);
      if (!ownsState()) return;
      if (!Array.isArray(provData)) {
        throw new Error(
          "Unexpected API response. Is VITE_API_BASE_URL configured correctly?",
        );
      }
      setProviders(filterProductVisibleProviders(provData));
      if (activeData) {
        setActiveModels(stripProductHiddenActiveModel(activeData));
      }
    } catch (err) {
      if (!ownsState()) return;
      const msg =
        err instanceof Error ? err.message : "Failed to load provider data";
      console.error("Failed to load providers:", err);
      setError(msg);
    } finally {
      if (showLoading && ownsState()) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    setActiveModels(null);
    void fetchAll();
    return () => providersLoadGateRef.current.invalidate();
  }, [fetchAll, selectedAgent]);

  return {
    providers,
    activeModels,
    loading,
    error,
    fetchAll,
  };
}
