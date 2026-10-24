import { useState, useEffect, useCallback } from "react";
import api from "../../../api";
import type {
  RememberedApproval,
  ToolGuardCommandRule,
  ToolGuardConfig,
  ToolGuardRule,
} from "../../../api/modules/security";

export interface MergedRule extends ToolGuardRule {
  source: "builtin" | "custom";
  disabled: boolean;
  autoDeny: boolean;
}

export function useToolGuard() {
  const [config, setConfig] = useState<ToolGuardConfig | null>(null);
  const [builtinRules, setBuiltinRules] = useState<ToolGuardRule[]>([]);
  const [customRules, setCustomRules] = useState<ToolGuardRule[]>([]);
  const [commandRules, setCommandRules] = useState<ToolGuardCommandRule[]>([]);
  const [showFullAutoMode, setShowFullAutoMode] = useState(true);
  const [rememberedApprovals, setRememberedApprovals] = useState<
    RememberedApproval[]
  >([]);
  const [rememberedApprovalsLoading, setRememberedApprovalsLoading] =
    useState(false);
  const [disabledRules, setDisabledRules] = useState<Set<string>>(new Set());
  const [autoDenyRules, setAutoDenyRules] = useState<Set<string>>(new Set());
  const [shellEvasionChecks, setShellEvasionChecks] = useState<
    Record<string, boolean>
  >({});
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const applyConfig = useCallback((cfg: ToolGuardConfig) => {
    setConfig(cfg);
    setEnabled(cfg.enabled);
    setCustomRules(cfg.custom_rules ?? []);
    setCommandRules(cfg.command_rules ?? []);
    setRememberedApprovals(cfg.remembered_approvals ?? []);
    setDisabledRules(new Set(cfg.disabled_rules ?? []));
    setAutoDenyRules(new Set(cfg.auto_denied_rules ?? []));
    setShellEvasionChecks(cfg.shell_evasion_checks ?? {});
  }, []);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfg, builtin, permissionProfiles] = await Promise.all([
        api.getToolGuard(),
        api.getBuiltinRules(),
        api.listPermissionProfiles(),
      ]);
      applyConfig(cfg);
      setBuiltinRules(builtin);
      setShowFullAutoMode(
        permissionProfiles.some((profile) => profile.id === "full"),
      );
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to load security config";
      console.error("Failed to load tool guard:", err);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [applyConfig]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const toggleRule = useCallback(
    (ruleId: string, currentlyDisabled: boolean) => {
      setDisabledRules((prev) => {
        const next = new Set(prev);
        if (currentlyDisabled) {
          next.delete(ruleId);
        } else {
          next.add(ruleId);
        }
        return next;
      });
    },
    [],
  );

  const toggleAutoDeny = useCallback(
    (ruleId: string, currentlyAutoDeny: boolean) => {
      setAutoDenyRules((prev) => {
        const next = new Set(prev);
        if (currentlyAutoDeny) {
          next.delete(ruleId);
        } else {
          next.add(ruleId);
        }
        return next;
      });
    },
    [],
  );

  const deleteCustomRule = useCallback((ruleId: string) => {
    setCustomRules((prev) => prev.filter((r) => r.id !== ruleId));
    setDisabledRules((prev) => {
      const next = new Set(prev);
      next.delete(ruleId);
      return next;
    });
    setAutoDenyRules((prev) => {
      const next = new Set(prev);
      next.delete(ruleId);
      return next;
    });
  }, []);

  const addCustomRule = useCallback((rule: ToolGuardRule) => {
    setCustomRules((prev) => [...prev, rule]);
  }, []);

  const updateCustomRule = useCallback(
    (ruleId: string, rule: ToolGuardRule) => {
      setCustomRules((prev) => prev.map((r) => (r.id === ruleId ? rule : r)));
    },
    [],
  );

  const mergedRules: MergedRule[] = [
    ...builtinRules.map((r) => ({
      ...r,
      source: "builtin" as const,
      disabled: disabledRules.has(r.id),
      autoDeny: autoDenyRules.has(r.id),
    })),
    ...customRules.map((r) => ({
      ...r,
      source: "custom" as const,
      disabled: disabledRules.has(r.id),
      autoDeny: autoDenyRules.has(r.id),
    })),
  ];

  const toggleShellEvasionCheck = useCallback(
    (checkName: string, checked: boolean) => {
      setShellEvasionChecks((prev) => ({ ...prev, [checkName]: checked }));
    },
    [],
  );

  const refreshRememberedApprovals = useCallback(async () => {
    setRememberedApprovalsLoading(true);
    try {
      const response = await api.getRememberedApprovals();
      const approvals = response.remembered_approvals ?? [];
      setRememberedApprovals(approvals);
      setConfig((prev) =>
        prev
          ? {
              ...prev,
              remembered_approvals: approvals,
            }
          : prev,
      );
    } finally {
      setRememberedApprovalsLoading(false);
    }
  }, []);

  const deleteRememberedApproval = useCallback(
    async (ruleId: string) => {
      const response = await api.deleteRememberedApproval(ruleId);
      if (!response.removed) {
        await refreshRememberedApprovals();
        return response;
      }
      const removeRule = (items: RememberedApproval[]) =>
        items.filter((item) => item.id !== ruleId);
      setRememberedApprovals((prev) => removeRule(prev));
      setConfig((prev) =>
        prev
          ? {
              ...prev,
              remembered_approvals: removeRule(prev.remembered_approvals ?? []),
            }
          : prev,
      );
      return response;
    },
    [refreshRememberedApprovals],
  );

  const clearRememberedApprovals = useCallback(async () => {
    const response = await api.clearRememberedApprovals();
    if (response.removed > 0) {
      setRememberedApprovals([]);
      setConfig((prev) =>
        prev
          ? {
              ...prev,
              remembered_approvals: [],
            }
          : prev,
      );
    } else {
      await refreshRememberedApprovals();
    }
    return response;
  }, [refreshRememberedApprovals]);

  const buildSaveBody = useCallback((): ToolGuardConfig => {
    return {
      enabled,
      permission_mode: config?.permission_mode ?? "quiet",
      default_workspace_dir: config?.default_workspace_dir ?? "",
      guarded_tools: config?.guarded_tools ?? null,
      denied_tools: config?.denied_tools ?? [],
      custom_rules: customRules,
      command_rules: commandRules,
      disabled_rules: Array.from(disabledRules),
      auto_denied_rules: Array.from(autoDenyRules),
      remembered_approvals: rememberedApprovals,
      shell_evasion_checks: shellEvasionChecks,
    };
  }, [
    enabled,
    config,
    customRules,
    commandRules,
    disabledRules,
    autoDenyRules,
    rememberedApprovals,
    shellEvasionChecks,
  ]);

  return {
    config,
    builtinRules,
    customRules,
    commandRules,
    setCommandRules,
    showFullAutoMode,
    rememberedApprovals,
    rememberedApprovalsLoading,
    disabledRules,
    autoDenyRules,
    enabled,
    setEnabled,
    mergedRules,
    shellEvasionChecks,
    toggleShellEvasionCheck,
    loading,
    error,
    fetchAll,
    toggleRule,
    toggleAutoDeny,
    deleteCustomRule,
    addCustomRule,
    updateCustomRule,
    refreshRememberedApprovals,
    deleteRememberedApproval,
    clearRememberedApprovals,
    buildSaveBody,
    applyConfig,
  };
}
