import { useEffect, useMemo, useState } from "react";
import { SaveOutlined } from "@ant-design/icons";
import { Button, Card, Segmented, Select, Switch } from "@/vendor/lantaiDesign";
import { utilityIntelligenceApi } from "@/api";
import type {
  ProviderInfo,
  UtilityIntelligenceConfig,
  UtilityIntelligenceMode,
  BackgroundSemanticStatus,
} from "@/api/types";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "@/hooks/useAppMessage";
import { isProductVisibleProviderId } from "@/utils/providerVisibility";
import styles from "../../index.module.less";
import { supportsChatProtocol } from "../../modelProtocol";

interface UtilityIntelligenceSectionProps {
  providers: ProviderInfo[];
}

const DEFAULT_CONFIG: UtilityIntelligenceConfig = {
  mode: "follow_active",
  background_processing_enabled: true,
  model: { provider_id: "", model: "" },
  timeout_seconds: 12,
};

export function UtilityIntelligenceSection({
  providers,
}: UtilityIntelligenceSectionProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [config, setConfig] =
    useState<UtilityIntelligenceConfig>(DEFAULT_CONFIG);
  const [savedConfig, setSavedConfig] =
    useState<UtilityIntelligenceConfig>(DEFAULT_CONFIG);
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<BackgroundSemanticStatus | null>(null);

  const eligibleProviders = useMemo(
    () =>
      providers.filter((provider) => {
        if (!isProductVisibleProviderId(provider.id)) return false;
        const hasModels = [
          ...(provider.models ?? []),
          ...(provider.extra_models ?? []),
        ].some(supportsChatProtocol);
        if (!hasModels) return false;
        if (provider.require_api_key === false)
          return Boolean(provider.base_url);
        if (provider.is_custom) return Boolean(provider.base_url);
        return Boolean(provider.api_key);
      }),
    [providers],
  );

  useEffect(() => {
    let active = true;
    void utilityIntelligenceApi
      .getUtilityIntelligenceConfig()
      .then((value) => {
        if (!active) return;
        setConfig(value);
        setSavedConfig(value);
        setLoaded(true);
      })
      .catch(() => {
        if (active) setLoaded(true);
      });
    void utilityIntelligenceApi
      .getBackgroundSemanticStatus()
      .then((value) => {
        if (active) setStatus(value);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  const chosenProvider = eligibleProviders.find(
    (provider) => provider.id === config.model.provider_id,
  );
  const modelOptions = [
    ...(chosenProvider?.models ?? []),
    ...(chosenProvider?.extra_models ?? []),
  ].filter(supportsChatProtocol);
  const canSave =
    loaded &&
    dirty &&
    (config.mode !== "dedicated" ||
      Boolean(config.model.provider_id && config.model.model));

  const updateMode = (value: string | number) => {
    setConfig((current) => ({
      ...current,
      mode: value as UtilityIntelligenceMode,
    }));
    setDirty(true);
  };

  const updateBackgroundProcessing = (enabled: boolean) => {
    setConfig((current) => ({
      ...current,
      background_processing_enabled: enabled,
    }));
    setDirty(true);
  };

  const updateProvider = (providerId: string) => {
    setConfig((current) => ({
      ...current,
      model: { provider_id: providerId, model: "" },
    }));
    setDirty(true);
  };

  const updateModel = (model: string) => {
    setConfig((current) => ({
      ...current,
      model: { ...current.model, model },
    }));
    setDirty(true);
  };

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    try {
      const saved =
        await utilityIntelligenceApi.updateUtilityIntelligenceConfig(config);
      setConfig(saved);
      setSavedConfig(saved);
      setDirty(false);
      message.success(t("models.utilityModelUpdated"));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("models.failedToSave"),
      );
    } finally {
      setSaving(false);
    }
  };

  const discardChanges = () => {
    setConfig(savedConfig);
    setDirty(false);
  };

  return (
    <Card
      className={styles.slotSection}
      data-testid="utility-intelligence-settings"
      title={t("models.utilityModel")}
    >
      <Segmented
        block
        disabled={!loaded}
        value={config.mode}
        onChange={updateMode}
        options={[
          {
            value: "follow_active",
            label: t("models.utilityFollowActive"),
          },
          {
            value: "dedicated",
            label: t("models.utilityDedicated"),
          },
        ]}
      />

      <div className={styles.settingSwitchRow}>
        <div>
          <label className={styles.slotLabel}>
            {t("models.backgroundSemanticProcessing")}
          </label>
          <span className={styles.settingScope}>
            作用于当前智能体的后台语义任务
          </span>
        </div>
        <Switch
          aria-label={t("models.backgroundSemanticProcessing")}
          checked={config.background_processing_enabled}
          disabled={!loaded}
          onChange={updateBackgroundProcessing}
        />
      </div>

      {config.mode === "dedicated" ? (
        <div className={`${styles.slotForm} ${styles.utilitySlotForm}`}>
          <div className={styles.slotField}>
            <label className={styles.slotLabel}>{t("models.provider")}</label>
            <Select
              style={{ width: "100%" }}
              placeholder={t("models.selectProvider")}
              value={config.model.provider_id || undefined}
              onChange={updateProvider}
              options={eligibleProviders.map((provider) => ({
                value: provider.id,
                label: provider.name,
              }))}
            />
          </div>
          <div className={styles.slotField}>
            <label className={styles.slotLabel}>{t("models.model")}</label>
            <Select
              style={{ width: "100%" }}
              disabled={!chosenProvider}
              showSearch
              optionFilterProp="label"
              placeholder={
                chosenProvider
                  ? t("models.selectModel")
                  : t("models.selectProvider")
              }
              value={config.model.model || undefined}
              onChange={updateModel}
              options={modelOptions.map((model) => ({
                value: model.id,
                label: `${model.name} (${model.id})`,
              }))}
            />
          </div>
        </div>
      ) : null}

      <div className={styles.slotActions}>
        <span
          className={styles.slotChangeStatus}
          data-dirty={dirty ? "true" : "false"}
          role="status"
        >
          {dirty ? "尚未保存" : "已保存"}
        </span>
        <Button
          aria-label="取消修改后台智能"
          disabled={!dirty || saving}
          onClick={discardChanges}
        >
          取消修改
        </Button>
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={saving}
          disabled={!canSave}
          onClick={save}
        >
          {t("models.save")}
        </Button>
      </div>
      <p className={styles.slotDescription}>
        {!config.background_processing_enabled
          ? t("models.backgroundSemanticDisabledDescription")
          : config.mode === "dedicated"
            ? t("models.utilityDedicatedDescription")
            : t("models.utilityFollowDescription")}
      </p>
      {status ? (
        <p
          className={styles.slotDescription}
          data-testid="semantic-queue-status"
        >
          {t("models.backgroundSemanticQueueStatus", {
            pending: status.queue.pending,
            failed: status.queue.failed,
          })}
        </p>
      ) : null}
    </Card>
  );
}
