import { useState, useCallback } from "react";
import { Form } from "@/vendor/lantaiDesign";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useTranslation } from "react-i18next";
import api from "../../../api";
import { useToolGuard, type MergedRule } from "./useToolGuard";

const BUILTIN_TOOLS = [
  "execute_shell_command",
  "execute_python_code",
  "browser_use",
  "browser_workbench",
  "desktop_screenshot",
  "view_image",
  "read_file",
  "write_file",
  "edit_file",
  "append_file",
  "view_text_file",
  "write_text_file",
  "send_file_to_user",
];

export function useSecurityPage() {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [editForm] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [activeTab, setActiveTab] = useState("toolGuard");

  // FileGuard handlers exposed from child component
  const [fileGuardHandlers, setFileGuardHandlers] = useState<{
    save: () => Promise<void>;
    reset: () => void;
    saving: boolean;
  } | null>(null);

  const onFileGuardHandlersReady = useCallback(
    (handlers: {
      save: () => Promise<void>;
      reset: () => void;
      saving: boolean;
    }) => {
      setFileGuardHandlers(handlers);
    },
    [],
  );

  // AllowNoAuthHosts handlers exposed from child component
  const [allowNoAuthHostsHandlers, setAllowNoAuthHostsHandlers] = useState<{
    save: () => Promise<void>;
    reset: () => void;
    saving: boolean;
  } | null>(null);

  const onAllowNoAuthHostsHandlersReady = useCallback(
    (handlers: {
      save: () => Promise<void>;
      reset: () => void;
      saving: boolean;
    }) => {
      setAllowNoAuthHostsHandlers(handlers);
    },
    [],
  );

  const {
    config,
    customRules,
    commandRules,
    setCommandRules,
    showFullAutoMode,
    rememberedApprovals,
    rememberedApprovalsLoading,
    builtinRules,
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
  } = useToolGuard();

  // Modal states
  const [editModal, setEditModal] = useState(false);
  const [editingRule, setEditingRule] = useState<MergedRule | null>(null);
  const [previewRule, setPreviewRule] = useState<MergedRule | null>(null);

  const { message, modal } = useAppMessage();

  // Form handlers
  const handleSave = useCallback(async (): Promise<boolean> => {
    try {
      setSaving(true);
      const values = await form.validateFields();
      const savedBody = buildSaveBody();
      const guardedTools: string[] =
        values.guarded_tools ?? savedBody.guarded_tools ?? [];
      const body = {
        enabled: values.enabled ?? savedBody.enabled,
        permission_mode:
          values.permission_mode ?? savedBody.permission_mode ?? "quiet",
        default_workspace_dir:
          values.default_workspace_dir ?? savedBody.default_workspace_dir ?? "",
        guarded_tools: guardedTools.length > 0 ? guardedTools : null,
        denied_tools: values.denied_tools ?? savedBody.denied_tools ?? [],
        custom_rules: customRules,
        command_rules: savedBody.command_rules ?? [],
        disabled_rules: Array.from(savedBody.disabled_rules),
        auto_denied_rules: savedBody.auto_denied_rules,
        remembered_approvals: savedBody.remembered_approvals ?? [],
        shell_evasion_checks: savedBody.shell_evasion_checks,
      };
      const riskRank: Record<string, number> = {
        plan: 0,
        default: 1,
        quiet: 2,
        accept_edits: 3,
        full_auto: 4,
      };
      const currentMode = config?.permission_mode ?? "quiet";
      const protectionReduced =
        body.enabled === false ||
        (riskRank[body.permission_mode] ?? 0) > (riskRank[currentMode] ?? 0);
      if (protectionReduced) {
        const confirmed = await new Promise<boolean>((resolve) => {
          modal.confirm({
            title: body.enabled
              ? "确认扩大自动执行范围？"
              : "确认关闭工具风险检查？",
            content: body.enabled
              ? `权限模式将从“${currentMode}”改为“${body.permission_mode}”，更多操作可能不再逐次询问。明确禁止规则仍会生效。`
              : "关闭后，只停止附加风险扫描。当前执行模式、命令规则、工作区边界和系统硬性限制仍会继续生效。",
            okText: body.enabled ? "确认扩大范围" : "关闭风险检查",
            cancelText: "返回检查",
            okButtonProps: { danger: true },
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!confirmed) return false;
      }
      const saved = await api.updateToolGuard(body);
      applyConfig(saved);
      form.setFieldsValue({
        enabled: saved.enabled,
        permission_mode: saved.permission_mode ?? "quiet",
        default_workspace_dir: saved.default_workspace_dir ?? "",
        guarded_tools: saved.guarded_tools ?? [],
        denied_tools: saved.denied_tools ?? [],
      });
      message.success(t("security.saveSuccess"));
      return true;
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) {
        return false;
      }
      const errMsg =
        err instanceof Error ? err.message : t("security.saveFailed");
      message.error(errMsg);
      return false;
    } finally {
      setSaving(false);
    }
  }, [
    applyConfig,
    customRules,
    buildSaveBody,
    config,
    form,
    message,
    modal,
    t,
  ]);

  const handleRefreshRememberedApprovals = useCallback(async () => {
    try {
      await refreshRememberedApprovals();
      message.success(t("security.rememberedApprovals.refreshSuccess"));
    } catch (err) {
      const errMsg =
        err instanceof Error
          ? err.message
          : t("security.rememberedApprovals.refreshFailed");
      message.error(errMsg);
    }
  }, [refreshRememberedApprovals, message, t]);

  const handleDeleteRememberedApproval = useCallback(
    async (ruleId: string) => {
      try {
        const response = await deleteRememberedApproval(ruleId);
        if (response.removed) {
          message.success(t("security.rememberedApprovals.deleteSuccess"));
        } else {
          message.warning(t("security.rememberedApprovals.notFound"));
        }
      } catch (err) {
        const errMsg =
          err instanceof Error
            ? err.message
            : t("security.rememberedApprovals.deleteFailed");
        message.error(errMsg);
      }
    },
    [deleteRememberedApproval, message, t],
  );

  const handleClearRememberedApprovals = useCallback(async () => {
    try {
      const response = await clearRememberedApprovals();
      if (response.removed > 0) {
        message.success(t("security.rememberedApprovals.clearSuccess"));
      } else {
        message.info(t("security.rememberedApprovals.empty"));
      }
    } catch (err) {
      const errMsg =
        err instanceof Error
          ? err.message
          : t("security.rememberedApprovals.clearFailed");
      message.error(errMsg);
    }
  }, [clearRememberedApprovals, message, t]);

  const handleReset = useCallback(() => {
    form.resetFields();
    fetchAll();
  }, [form, fetchAll]);

  // Rule modal handlers
  const openAddRule = useCallback(() => {
    setEditingRule(null);
    editForm.resetFields();
    editForm.setFieldsValue({
      severity: "HIGH",
      category: "command_injection",
      tools: [],
      params: [],
      patterns: "",
      exclude_patterns: "",
    });
    setEditModal(true);
  }, [editForm]);

  const openEditRule = useCallback(
    (rule: MergedRule) => {
      setEditingRule(rule);
      editForm.setFieldsValue({
        ...rule,
        patterns: rule.patterns.join("\n"),
        exclude_patterns: rule.exclude_patterns.join("\n"),
      });
      setEditModal(true);
    },
    [editForm],
  );

  const handleEditSave = useCallback(async () => {
    try {
      const values = await editForm.validateFields();
      const patterns = (values.patterns as string)
        .split("\n")
        .map((s: string) => s.trim())
        .filter(Boolean);
      const excludePatterns = ((values.exclude_patterns as string) || "")
        .split("\n")
        .map((s: string) => s.trim())
        .filter(Boolean);

      const rule = {
        id: values.id,
        tools: values.tools ?? [],
        params: values.params ?? [],
        category: values.category,
        severity: values.severity,
        patterns,
        exclude_patterns: excludePatterns,
        description: values.description || "",
        remediation: values.remediation || "",
      };

      if (editingRule) {
        updateCustomRule(editingRule.id, rule);
      } else {
        const allIds = [
          ...builtinRules.map((r) => r.id),
          ...customRules.map((r) => r.id),
        ];
        if (allIds.includes(rule.id)) {
          message.error(t("security.rules.duplicateId"));
          return;
        }
        addCustomRule(rule);
      }
      setEditModal(false);
    } catch {
      // validation failed
    }
  }, [
    editingRule,
    builtinRules,
    customRules,
    updateCustomRule,
    addCustomRule,
    editForm,
    message,
    t,
  ]);

  const toolOptions = BUILTIN_TOOLS.map((name) => ({
    label: name,
    value: name,
  }));

  return {
    // Tab state
    activeTab,
    setActiveTab,

    // Tool Guard form
    form,
    config,
    enabled,
    setEnabled,
    toolOptions,
    saving,
    handleSave,
    handleReset,

    // Rules
    mergedRules,
    builtinRules,
    customRules,
    commandRules,
    setCommandRules,
    showFullAutoMode,
    rememberedApprovals,
    rememberedApprovalsLoading,
    handleRefreshRememberedApprovals,
    handleDeleteRememberedApproval,
    handleClearRememberedApprovals,
    toggleRule,
    toggleAutoDeny,
    deleteCustomRule,
    openAddRule,
    openEditRule,

    // Shell Evasion
    shellEvasionChecks,
    toggleShellEvasionCheck,

    // Modals
    editModal,
    setEditModal,
    editingRule,
    editForm,
    handleEditSave,
    previewRule,
    setPreviewRule,

    // FileGuard
    fileGuardHandlers,
    onFileGuardHandlersReady,

    // AllowNoAuthHosts
    allowNoAuthHostsHandlers,
    onAllowNoAuthHostsHandlersReady,

    // Loading / Error
    loading,
    error,
    fetchAll,
  };
}
