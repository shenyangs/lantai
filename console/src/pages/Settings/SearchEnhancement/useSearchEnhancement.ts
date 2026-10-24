import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import api from "../../../api";
import type { GuanlanMCPInfo } from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";

export function useSearchEnhancement() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const [guanlan, setGuanlan] = useState<GuanlanMCPInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [mutating, setMutating] = useState(false);
  const loadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());

  const load = useCallback(async () => {
    const ownsState = loadGateRef.current.begin();
    setLoading(true);
    try {
      const data = await api.getGuanlanMCP();
      if (ownsState()) setGuanlan(data);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load Guanlan search enhancement:", error);
      message.error(t("mcp.guanlanLoadError"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, [message, t]);

  useEffect(() => {
    const loadGate = loadGateRef.current;
    const mutationScope = mutationScopeRef.current;
    setGuanlan(null);
    setMutating(false);
    void load();
    return () => {
      loadGate.invalidate();
      mutationScope.invalidate();
    };
  }, [load, selectedAgent]);

  const setEnabled = useCallback(
    async (enabled: boolean) => {
      const ownsState = mutationScopeRef.current.capture();
      setMutating(true);
      try {
        const data = await api.setGuanlanMCP(enabled);
        if (!ownsState()) return;
        setGuanlan(data);
        if (enabled && data.package_status === "error") {
          message.warning(data.package_message || t("mcp.guanlanToggleError"));
        } else {
          message.success(
            enabled
              ? t("mcp.guanlanEnableSuccess")
              : t("mcp.guanlanDisableSuccess"),
          );
        }
      } catch (error: any) {
        if (!ownsState()) return;
        message.error(error?.message || t("mcp.guanlanToggleError"));
      } finally {
        if (ownsState()) setMutating(false);
      }
    },
    [message, t],
  );

  return {
    guanlan,
    loading,
    mutating,
    setEnabled,
    refresh: load,
  };
}
