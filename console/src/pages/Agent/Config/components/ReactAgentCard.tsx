import { useCallback, useEffect, useRef, useState } from "react";
import {
  Form,
  InputNumber,
  Select,
  Card,
  Alert,
  Switch,
} from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import { useTimezoneOptions } from "../../../../hooks/useTimezoneOptions";
import { planApi } from "../../../../api/modules/plan";
import { useAgentStore } from "../../../../stores/agentStore";
import {
  CONTEXT_MANAGER_BACKEND_OPTIONS,
  MEMORY_MANAGER_BACKEND_OPTIONS,
} from "../../../../constants/backendMappings";
import { createAsyncStateScope } from "../../../../utils/latestRequest";
import styles from "../index.module.less";

const LANGUAGE_OPTIONS = [
  { value: "zh", label: "中文" },
  { value: "en", label: "English" },
  { value: "ru", label: "Русский" },
];

interface ReactAgentCardProps {
  language: string;
  savingLang: boolean;
  onLanguageChange: (value: string) => void;
  timezone: string;
  savingTimezone: boolean;
  onTimezoneChange: (value: string) => void;
}

export function ReactAgentCard({
  language,
  savingLang,
  onLanguageChange,
  timezone,
  savingTimezone,
  onTimezoneChange,
}: ReactAgentCardProps) {
  const { t } = useTranslation();
  const { selectedAgent } = useAgentStore();
  const [planEnabled, setPlanEnabled] = useState(false);
  const [planLoading, setPlanLoading] = useState(false);
  const mutationScopeRef = useRef(createAsyncStateScope());

  useEffect(() => {
    let cancelled = false;
    setPlanEnabled(false);
    setPlanLoading(false);
    planApi
      .getPlanConfig()
      .then((cfg) => {
        if (!cancelled) setPlanEnabled(cfg.enabled);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      mutationScopeRef.current.invalidate();
    };
  }, [selectedAgent]);

  const handlePlanToggle = useCallback(
    async (checked: boolean) => {
      const ownsState = mutationScopeRef.current.capture();
      setPlanLoading(true);
      const prev = planEnabled;
      setPlanEnabled(checked);
      try {
        const res = await planApi.updatePlanConfig({ enabled: checked });
        if (ownsState()) setPlanEnabled(res.enabled);
      } catch {
        if (ownsState()) setPlanEnabled(prev);
      } finally {
        if (ownsState()) setPlanLoading(false);
      }
    },
    [planEnabled],
  );

  return (
    <Card className={styles.formCard} title={t("agentConfig.reactAgentTitle")}>
      <div className={styles.reactAgentRow}>
        <Form.Item
          label={t("agentConfig.language")}
          tooltip={t("agentConfig.languageTooltip")}
          className={styles.reactAgentField}
        >
          <Select
            value={language}
            options={LANGUAGE_OPTIONS}
            onChange={onLanguageChange}
            loading={savingLang}
            disabled={savingLang}
            style={{ width: "100%" }}
          />
        </Form.Item>

        <Form.Item
          label={t("agentConfig.timezone")}
          tooltip={t("agentConfig.timezoneTooltip")}
          className={styles.reactAgentField}
        >
          <Select
            showSearch
            value={timezone}
            placeholder={t("agentConfig.selectTimezone")}
            filterOption={(input, option) =>
              (option?.label?.toString() || "")
                .toLowerCase()
                .includes(input.toLowerCase())
            }
            options={useTimezoneOptions()}
            onChange={onTimezoneChange}
            loading={savingTimezone}
            disabled={savingTimezone}
            style={{ width: "100%" }}
          />
        </Form.Item>

        <Form.Item
          label={t("agentConfig.maxIters")}
          name="max_iters"
          rules={[
            { required: true, message: t("agentConfig.maxItersRequired") },
            { type: "number", min: 1, message: t("agentConfig.maxItersMin") },
          ]}
          tooltip={t("agentConfig.maxItersTooltip")}
          className={styles.reactAgentField}
        >
          <InputNumber
            style={{ width: "100%" }}
            min={1}
            placeholder={t("agentConfig.maxItersPlaceholder")}
          />
        </Form.Item>

        <Form.Item
          label={t("agentConfig.shellCommandTimeout")}
          name="shell_command_timeout"
          rules={[
            {
              required: true,
              message: t("agentConfig.shellCommandTimeoutRequired"),
            },
            {
              type: "number",
              min: 1,
              message: t("agentConfig.shellCommandTimeoutMin"),
            },
          ]}
          tooltip={t("agentConfig.shellCommandTimeoutTooltip")}
          className={styles.reactAgentField}
        >
          <InputNumber
            style={{ width: "100%" }}
            min={1}
            step={10}
            placeholder={t("agentConfig.shellCommandTimeoutPlaceholder")}
          />
        </Form.Item>
      </div>

      <div className={styles.reactAgentRow}>
        <Form.Item
          label={t("agentConfig.contextManagerBackend")}
          name="context_manager_backend"
          tooltip={t("agentConfig.contextManagerBackendTooltip")}
          className={styles.reactAgentField}
        >
          <Select
            options={CONTEXT_MANAGER_BACKEND_OPTIONS}
            style={{ width: "100%" }}
          />
        </Form.Item>

        <Form.Item
          label={t("agentConfig.memoryManagerBackend")}
          name="memory_manager_backend"
          tooltip={t("agentConfig.memoryManagerBackendTooltip")}
          className={styles.reactAgentField}
        >
          <Select
            options={MEMORY_MANAGER_BACKEND_OPTIONS}
            style={{ width: "100%" }}
          />
        </Form.Item>
      </div>
      <Alert
        type="warning"
        showIcon
        message={t("agentConfig.backendRestartWarning")}
        style={{ marginBottom: 16 }}
      />

      <Form.Item
        label={t("agentConfig.contextPolicyMode", {
          defaultValue: "上下文策略",
        })}
        name="context_policy_mode"
        tooltip={t("agentConfig.contextPolicyModeTooltip", {
          defaultValue:
            "智能模式会按模型能力预留输出和安全空间；最大模式使用完整已验证预算；经济模式会更早压缩。",
        })}
      >
        <Select
          style={{ width: "100%" }}
          options={[
            {
              value: "smart",
              label: t("agentConfig.contextPolicySmart", {
                defaultValue: "智能（推荐）",
              }),
            },
            {
              value: "max",
              label: t("agentConfig.contextPolicyMax", {
                defaultValue: "最大",
              }),
            },
            {
              value: "economy",
              label: t("agentConfig.contextPolicyEconomy", {
                defaultValue: "经济",
              }),
            },
          ]}
        />
      </Form.Item>

      <Form.Item
        label={t("agentConfig.maxContextLength")}
        name="max_input_length"
        rules={[
          {
            required: true,
            message: t("agentConfig.maxContextLengthRequired"),
          },
          {
            type: "number",
            min: 1000,
            message: t("agentConfig.maxContextLengthMin"),
          },
        ]}
        tooltip={t("agentConfig.maxContextLengthTooltip", {
          defaultValue:
            "这是用户上限，不再被当作模型真实窗口；最终预算由模型、路由和上下文策略共同决定。",
        })}
      >
        <InputNumber
          style={{ width: "100%" }}
          min={1000}
          step={1024}
          placeholder={t("agentConfig.maxContextLengthPlaceholder")}
        />
      </Form.Item>

      <Form.Item
        label={t("agentConfig.planMode", "Plan Mode")}
        tooltip={t(
          "agentConfig.planModeTooltip",
          "Enable plan mode to use /plan <description> for structured task planning",
        )}
      >
        <Switch
          checked={planEnabled}
          loading={planLoading}
          onChange={handlePlanToggle}
        />
      </Form.Item>
    </Card>
  );
}
