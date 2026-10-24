import { useState, useEffect, useCallback, useRef } from "react";
import { Form, Modal } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import api from "../../../api";
import type { AgentsRunningConfig } from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import {
  CONTEXT_MANAGER_BACKEND_MAPPINGS,
  MEMORY_MANAGER_BACKEND_MAPPINGS,
} from "../../../constants/backendMappings";
import type { ToolExecutionLevel } from "./components/ToolExecutionLevelCard";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";

export function useAgentConfig() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [language, setLanguage] = useState<string>("zh");
  const [savingLang, setSavingLang] = useState(false);
  const [timezone, setTimezone] = useState<string>("UTC");
  const [savingTimezone, setSavingTimezone] = useState(false);
  const [approvalLevel, setApprovalLevel] =
    useState<ToolExecutionLevel>("AUTO");
  const initialApprovalLevelRef = useRef<ToolExecutionLevel>("AUTO");
  const loadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());

  const fetchConfig = useCallback(async () => {
    const ownsState = loadGateRef.current.begin();
    setLoading(true);
    setError(null);
    try {
      const [config, langResp, tzResp] = await Promise.all([
        api.getAgentRunningConfig(),
        api.getAgentLanguage(),
        api.getUserTimezone(),
      ]);
      if (!ownsState()) return;
      const loadedLevel = (
        config.approval_level || "AUTO"
      ).toUpperCase() as ToolExecutionLevel;
      setApprovalLevel(loadedLevel);
      initialApprovalLevelRef.current = loadedLevel;
      const contextBackend =
        config.context_manager_backend in CONTEXT_MANAGER_BACKEND_MAPPINGS
          ? config.context_manager_backend
          : "light";
      const memoryBackend =
        config.memory_manager_backend in MEMORY_MANAGER_BACKEND_MAPPINGS
          ? config.memory_manager_backend
          : "remelight";
      form.setFieldsValue({
        max_iters: config.max_iters,
        shell_command_timeout: config.shell_command_timeout ?? 60.0,
        llm_retry_enabled: config.llm_retry_enabled,
        llm_max_retries: config.llm_max_retries,
        llm_backoff_base: config.llm_backoff_base,
        llm_backoff_cap: config.llm_backoff_cap,
        llm_max_concurrent: config.llm_max_concurrent,
        llm_max_qpm: config.llm_max_qpm,
        llm_rate_limit_pause: config.llm_rate_limit_pause,
        llm_rate_limit_jitter: config.llm_rate_limit_jitter,
        llm_acquire_timeout: config.llm_acquire_timeout,
        max_input_length: config.max_input_length,
        context_policy_mode: config.context_policy_mode ?? "smart",
        history_max_length: config.history_max_length,
        context_manager_backend: contextBackend,
        light_context_config: config.light_context_config,
        memory_manager_backend: memoryBackend,
        reme_light_memory_config: config.reme_light_memory_config,
      });
      setLanguage(langResp.language);
      setTimezone(tzResp.timezone || "UTC");
    } catch (err) {
      if (!ownsState()) return;
      const errMsg =
        err instanceof Error ? err.message : t("agentConfig.loadFailed");
      setError(errMsg);
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, [form, t, selectedAgent]);

  useEffect(() => {
    setSaving(false);
    setSavingLang(false);
    setSavingTimezone(false);
    setError(null);
    form.resetFields();
    void fetchConfig();
    return () => {
      loadGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
  }, [fetchConfig]);

  const handleSave = useCallback(async () => {
    const ownsState = mutationScopeRef.current.capture();
    try {
      const values = await form.validateFields();
      if (!ownsState()) return;
      setSaving(true);
      const configToSave: AgentsRunningConfig = {
        ...(values as AgentsRunningConfig),
        approval_level: approvalLevel,
      };
      await api.updateAgentRunningConfig(configToSave);
      if (!ownsState()) return;
      initialApprovalLevelRef.current = approvalLevel;
      message.success(t("agentConfig.saveSuccess"));
    } catch (err) {
      if (!ownsState()) return;
      if (err instanceof Error && "errorFields" in err) return;
      const errMsg =
        err instanceof Error ? err.message : t("agentConfig.saveFailed");
      message.error(errMsg);
    } finally {
      if (ownsState()) setSaving(false);
    }
  }, [form, t, selectedAgent, approvalLevel]);

  const handleLanguageChange = useCallback(
    (value: string): void => {
      if (value === language) return;
      const ownsState = mutationScopeRef.current.capture();
      Modal.confirm({
        title: t("agentConfig.languageConfirmTitle"),
        content: (
          <span style={{ whiteSpace: "pre-line" }}>
            {t("agentConfig.languageConfirmContent")}
          </span>
        ),
        okText: t("agentConfig.languageConfirmOk"),
        cancelText: t("common.cancel"),
        onOk: async () => {
          if (!ownsState()) return;
          setSavingLang(true);
          try {
            const resp = await api.updateAgentLanguage(value);
            if (!ownsState()) return;
            setLanguage(resp.language);
            if (resp.copied_files && resp.copied_files.length > 0) {
              message.success(
                t("agentConfig.languageSaveSuccessWithFiles", {
                  count: resp.copied_files.length,
                }),
              );
            } else {
              message.success(t("agentConfig.languageSaveSuccess"));
            }
          } catch (err) {
            if (!ownsState()) return;
            const errMsg =
              err instanceof Error
                ? err.message
                : t("agentConfig.languageSaveFailed");
            message.error(errMsg);
          } finally {
            if (ownsState()) setSavingLang(false);
          }
        },
      });
    },
    [language, t],
  );

  const handleTimezoneChange = useCallback(
    async (value: string) => {
      if (value === timezone) return;
      const ownsState = mutationScopeRef.current.capture();
      setSavingTimezone(true);
      try {
        await api.updateUserTimezone(value);
        if (!ownsState()) return;
        setTimezone(value);
        message.success(t("agentConfig.timezoneSaveSuccess"));
      } catch (err) {
        if (!ownsState()) return;
        const errMsg =
          err instanceof Error
            ? err.message
            : t("agentConfig.timezoneSaveFailed");
        message.error(errMsg);
      } finally {
        if (ownsState()) setSavingTimezone(false);
      }
    },
    [timezone, t],
  );

  return {
    form,
    loading,
    saving,
    error,
    language,
    savingLang,
    timezone,
    savingTimezone,
    approvalLevel,
    setApprovalLevel,
    fetchConfig,
    handleSave,
    handleLanguageChange,
    handleTimezoneChange,
  };
}
