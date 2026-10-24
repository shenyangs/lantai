import { useCallback, useEffect, useRef, useState } from "react";
import { useAppMessage } from "../../../hooks/useAppMessage";
import api from "../../../api";
import type { ToolInfo } from "../../../api/modules/tools";
import { useTranslation } from "react-i18next";
import { useAgentStore } from "../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";

export function useTools() {
  const { t } = useTranslation();
  const { selectedAgent } = useAgentStore();
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [batchLoading, setBatchLoading] = useState(false);
  const [pendingToolNames, setPendingToolNames] = useState<Set<string>>(
    () => new Set(),
  );
  const { message } = useAppMessage();
  const toolsLoadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());
  const pendingMutationsRef = useRef(new Map<string, symbol>());

  const beginMutations = useCallback((names: string[]) => {
    const operations = new Map<string, symbol>();
    for (const name of names) {
      if (pendingMutationsRef.current.has(name)) continue;
      const operation = Symbol(name);
      pendingMutationsRef.current.set(name, operation);
      operations.set(name, operation);
    }
    if (operations.size > 0) {
      setPendingToolNames(new Set(pendingMutationsRef.current.keys()));
    }
    return operations;
  }, []);

  const finishMutations = useCallback((operations: Map<string, symbol>) => {
    let changed = false;
    for (const [name, operation] of operations) {
      if (pendingMutationsRef.current.get(name) !== operation) continue;
      pendingMutationsRef.current.delete(name);
      changed = true;
    }
    if (changed) {
      setPendingToolNames(new Set(pendingMutationsRef.current.keys()));
    }
  }, []);

  const applyServerTools = useCallback((results: ToolInfo[]) => {
    const byName = new Map(results.map((tool) => [tool.name, tool]));
    setTools((current) =>
      current.map((tool) => {
        const result = byName.get(tool.name);
        if (!result) return tool;
        return result.revision >= (tool.revision ?? 0) ? result : tool;
      }),
    );
  }, []);

  const loadTools = useCallback(async () => {
    const ownsState = toolsLoadGateRef.current.begin();
    setLoading(true);
    setLoadError("");
    try {
      const data = await api.listTools();
      if (!ownsState()) return;
      setTools(data);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load tools:", error);
      setLoadError(
        error instanceof Error && error.message
          ? error.message
          : t("tools.loadError"),
      );
      message.error(t("tools.loadError"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    setBatchLoading(false);
    pendingMutationsRef.current.clear();
    setPendingToolNames(new Set());
    setTools([]);
    void loadTools();
    return () => {
      toolsLoadGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
  }, [loadTools, selectedAgent]);

  const toggleEnabled = useCallback(
    async (tool: ToolInfo, enabled = !tool.enabled) => {
      if (tool.locked && tool.enabled && !enabled) {
        message.info("核心工具保持启用，具体调用仍受风险询问与安全策略约束");
        return;
      }
      const operations = beginMutations([tool.name]);
      if (!operations.has(tool.name)) return;
      const ownsState = mutationScopeRef.current.capture();

      try {
        const result = await api.setToolEnabled(
          tool.name,
          enabled,
          tool.revision ?? 0,
        );
        if (!ownsState()) return;
        message.success(
          enabled ? t("tools.enableSuccess") : t("tools.disableSuccess"),
        );
        applyServerTools([result]);
      } catch (_error) {
        if (!ownsState()) return;
        message.error(t("tools.toggleError"));
      } finally {
        finishMutations(operations);
      }
    },
    [applyServerTools, beginMutations, finishMutations, message, t],
  );

  const toggleAsyncExecution = useCallback(
    async (tool: ToolInfo) => {
      const operations = beginMutations([tool.name]);
      if (!operations.has(tool.name)) return;
      const ownsState = mutationScopeRef.current.capture();

      try {
        const result = await api.updateAsyncExecution(
          tool.name,
          !tool.async_execution,
        );
        if (!ownsState()) return;
        message.success(
          result.async_execution
            ? t("tools.asyncExecutionEnabled")
            : t("tools.asyncExecutionDisabled"),
        );
        applyServerTools([result]);
      } catch (_error) {
        if (!ownsState()) return;
        message.error(t("tools.toggleError"));
      } finally {
        finishMutations(operations);
      }
    },
    [applyServerTools, beginMutations, finishMutations, message, t],
  );

  const setEnabledForTools = useCallback(
    async (targetTools: ToolInfo[], enabled: boolean) => {
      const changedTools = targetTools.filter(
        (tool) =>
          !tool.locked &&
          tool.enabled !== enabled &&
          !pendingMutationsRef.current.has(tool.name),
      );
      if (changedTools.length === 0) return;
      const operations = beginMutations(changedTools.map((tool) => tool.name));
      const ownsState = mutationScopeRef.current.capture();
      setBatchLoading(true);
      try {
        const settled = await Promise.allSettled(
          changedTools.map((tool) =>
            api.setToolEnabled(tool.name, enabled, tool.revision ?? 0),
          ),
        );
        if (!ownsState()) return;
        const succeeded: ToolInfo[] = [];
        const failedNames: string[] = [];
        settled.forEach((result, index) => {
          if (result.status === "fulfilled") succeeded.push(result.value);
          else
            failedNames.push(
              changedTools[index].display_name || changedTools[index].name,
            );
        });
        applyServerTools(succeeded);
        if (failedNames.length === 0) {
          message.success(enabled ? "该组工具已启用" : "该组可选工具已关闭");
        } else {
          message.error(
            `已更新 ${succeeded.length} 项，${failedNames.length} 项未完成：${failedNames.join("、")}`,
          );
        }
      } finally {
        finishMutations(operations);
        if (ownsState()) setBatchLoading(false);
      }
    },
    [applyServerTools, beginMutations, finishMutations, message],
  );

  const enableAll = useCallback(async () => {
    const disabledTools = tools.filter((tool) => !tool.enabled && !tool.locked);
    if (disabledTools.length === 0) {
      message.info(t("tools.allEnabled"));
      return;
    }
    await setEnabledForTools(disabledTools, true);
  }, [message, setEnabledForTools, t, tools]);

  const disableAll = useCallback(async () => {
    const enabledTools = tools.filter((tool) => tool.enabled && !tool.locked);
    if (enabledTools.length === 0) {
      message.info(t("tools.allDisabled"));
      return;
    }
    await setEnabledForTools(enabledTools, false);
  }, [message, setEnabledForTools, t, tools]);

  const saveToolConfig = useCallback(
    async (toolName: string, config: Record<string, any>) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        await api.updateToolConfig(toolName, config);
        if (!ownsState()) return;
        message.success(t("tools.configSaved"));
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to save tool config:", error);
        message.error(t("tools.configSaveError"));
        throw error;
      }
    },
    [t],
  );

  return {
    tools,
    loading,
    loadError,
    batchLoading,
    pendingToolNames,
    toggleEnabled,
    toggleAsyncExecution,
    enableAll,
    disableAll,
    setEnabledForTools,
    loadTools,
    saveToolConfig,
  };
}
