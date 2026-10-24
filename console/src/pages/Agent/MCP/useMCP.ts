import { useCallback, useEffect, useRef, useState } from "react";
import { useAppMessage } from "../../../hooks/useAppMessage";
import api from "../../../api";
import type { MCPClientInfo } from "../../../api/types";
import type { MCPOAuthConfig } from "../../../api/types";
import { useTranslation } from "react-i18next";
import { useAgentStore } from "../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";

export function useMCP() {
  const { t } = useTranslation();
  const { selectedAgent } = useAgentStore();
  const [clients, setClients] = useState<MCPClientInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [pendingClientKeys, setPendingClientKeys] = useState<Set<string>>(
    () => new Set(),
  );
  const { message } = useAppMessage();
  const clientsLoadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());
  const clientsRef = useRef<MCPClientInfo[]>([]);
  const pendingMutationsRef = useRef(new Map<string, symbol>());

  const beginMutation = useCallback((key: string) => {
    if (pendingMutationsRef.current.has(key)) return null;
    const operation = Symbol(key);
    pendingMutationsRef.current.set(key, operation);
    setPendingClientKeys(new Set(pendingMutationsRef.current.keys()));
    return operation;
  }, []);

  const finishMutation = useCallback((key: string, operation: symbol) => {
    if (pendingMutationsRef.current.get(key) !== operation) return;
    pendingMutationsRef.current.delete(key);
    setPendingClientKeys(new Set(pendingMutationsRef.current.keys()));
  }, []);

  const applyServerClient = useCallback((result: MCPClientInfo) => {
    setClients((current) => {
      const next = current.map((client) =>
        client.key === result.key && result.revision >= (client.revision ?? 0)
          ? result
          : client,
      );
      clientsRef.current = next;
      return next;
    });
  }, []);

  const loadClients = useCallback(async () => {
    const ownsState = clientsLoadGateRef.current.begin();
    setLoading(true);
    setLoadError("");
    try {
      const data = await api.listMCPClients();
      if (!ownsState()) return;
      const visibleClients = data.filter(
        (client) => client.key !== "guanlan" && client.key !== "officecli",
      );
      clientsRef.current = visibleClients;
      setClients(visibleClients);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load MCP clients:", error);
      setLoadError(
        error instanceof Error && error.message
          ? error.message
          : t("mcp.loadError"),
      );
      message.error(t("mcp.loadError"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, [message, t]);

  useEffect(() => {
    const clientsLoadGate = clientsLoadGateRef.current;
    const mutationScope = mutationScopeRef.current;
    pendingMutationsRef.current.clear();
    setPendingClientKeys(new Set());
    clientsRef.current = [];
    setClients([]);
    void loadClients();
    return () => {
      clientsLoadGate.invalidate();
      mutationScope.invalidate();
    };
  }, [loadClients, selectedAgent]);

  const createClient = useCallback(
    async (
      key: string,
      clientData: {
        name: string;
        description?: string;
        command: string;
        enabled?: boolean;
        transport?: "stdio" | "streamable_http" | "sse";
        url?: string;
        headers?: Record<string, string>;
        args?: string[];
        env?: Record<string, string>;
        cwd?: string;
        oauth?: Partial<MCPOAuthConfig>;
        allowed_tools?: string[];
      },
    ) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        await api.createMCPClient({
          client_key: key,
          client: clientData,
        });
        if (!ownsState()) return true;
        message.success(t("mcp.createSuccess"));
        await loadClients();
        return true;
      } catch (error: any) {
        if (!ownsState()) return false;
        const errorMsg = error?.message || t("mcp.createError");
        message.error(errorMsg);
        return false;
      }
    },
    [loadClients, message, t],
  );

  const updateClient = useCallback(
    async (
      key: string,
      updates: {
        name?: string;
        description?: string;
        command?: string;
        enabled?: boolean;
        transport?: "stdio" | "streamable_http" | "sse";
        url?: string;
        headers?: Record<string, string>;
        args?: string[];
        env?: Record<string, string>;
        cwd?: string;
        oauth?: Partial<MCPOAuthConfig>;
        allowed_tools?: string[];
      },
    ) => {
      const current = clientsRef.current.find((client) => client.key === key);
      if (!current) return false;
      const operation = beginMutation(key);
      if (!operation) return false;
      const ownsState = mutationScopeRef.current.capture();
      try {
        const result = await api.updateMCPClient(key, {
          ...updates,
          expected_revision: current.revision ?? 0,
        });
        if (!ownsState()) return true;
        applyServerClient(result);
        message.success(t("mcp.updateSuccess"));
        return true;
      } catch (error: any) {
        if (!ownsState()) return false;
        const errorMsg = error?.message || t("mcp.updateError");
        message.error(errorMsg);
        return false;
      } finally {
        finishMutation(key, operation);
      }
    },
    [applyServerClient, beginMutation, finishMutation, message, t],
  );

  const toggleEnabled = useCallback(
    async (client: MCPClientInfo, enabled = !client.enabled) => {
      const operation = beginMutation(client.key);
      if (!operation) return;
      const ownsState = mutationScopeRef.current.capture();
      try {
        const result = await api.updateMCPClient(client.key, {
          enabled,
          expected_revision: client.revision ?? 0,
        });
        if (!ownsState()) return;
        applyServerClient(result);
        message.success(
          enabled ? t("mcp.enableSuccess") : t("mcp.disableSuccess"),
        );
      } catch (_error) {
        if (!ownsState()) return;
        message.error(t("mcp.toggleError"));
      } finally {
        finishMutation(client.key, operation);
      }
    },
    [applyServerClient, beginMutation, finishMutation, message, t],
  );

  const deleteClient = useCallback(
    async (client: MCPClientInfo) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        await api.deleteMCPClient(client.key);
        if (!ownsState()) return;
        message.success(t("mcp.deleteSuccess"));
        await loadClients();
      } catch (_error) {
        if (!ownsState()) return;
        message.error(t("mcp.deleteError"));
      }
    },
    [loadClients, message, t],
  );

  return {
    clients,
    loading,
    loadError,
    pendingClientKeys,
    createClient,
    updateClient,
    toggleEnabled,
    deleteClient,
    loadClients,
  };
}
