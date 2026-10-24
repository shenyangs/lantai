import { useState, useEffect, useMemo } from "react";
import { SaveOutlined } from "@ant-design/icons";
import { Select, Button, Card } from "@/vendor/lantaiDesign";
import type {
  ActiveModelsInfo,
  ModelSlotRequest,
} from "../../../../../api/types";
import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import { confirmFreeModelSwitch } from "@/utils/freeModelSwitchWarning";
import {
  isProductVisibleProviderId,
  stripProductHiddenActiveModel,
} from "@/utils/providerVisibility";
import styles from "../../index.module.less";
import { isChatCapableModel } from "../../modelProtocol";

interface ModelsSectionProps {
  providers: Array<{
    id: string;
    name: string;
    models?: Array<{
      id: string;
      name: string;
      is_free?: boolean;
      supported_protocols?: string[];
    }>;
    extra_models?: Array<{
      id: string;
      name: string;
      is_free?: boolean;
      supported_protocols?: string[];
    }>;
    base_url?: string;
    api_key?: string;
    is_custom: boolean;
    is_local?: boolean;
    require_api_key?: boolean;
  }>;
  activeModels: ActiveModelsInfo | null;
  onSaved: () => void;
}

export function ModelsSection({
  providers,
  activeModels,
  onSaved,
}: ModelsSectionProps) {
  const { t } = useTranslation();
  const [saving, setSaving] = useState(false);
  const [selectedProviderId, setSelectedProviderId] = useState<
    string | undefined
  >(undefined);
  const [selectedModel, setSelectedModel] = useState<string | undefined>(
    undefined,
  );
  const [dirty, setDirty] = useState(false);
  const { message } = useAppMessage();

  const visibleActiveModels = stripProductHiddenActiveModel(activeModels);
  const currentSlot = visibleActiveModels?.active_llm;

  const eligible = useMemo(
    () =>
      providers.filter((p) => {
        if (!isProductVisibleProviderId(p.id)) return false;
        const hasModels = [...(p.models ?? []), ...(p.extra_models ?? [])].some(
          isChatCapableModel,
        );
        if (!hasModels) return false;
        if (p.require_api_key === false) return !!p.base_url;
        if (p.is_custom) return !!p.base_url;
        if (p.require_api_key ?? true) return !!p.api_key;
        return true;
      }),
    [providers],
  );

  useEffect(() => {
    if (currentSlot) {
      setSelectedProviderId(currentSlot.provider_id || undefined);
      setSelectedModel(currentSlot.model || undefined);
    } else {
      setSelectedProviderId(undefined);
      setSelectedModel(undefined);
    }
    setDirty(false);
  }, [currentSlot?.provider_id, currentSlot?.model]);

  const chosenProvider = eligible.find((p) => p.id === selectedProviderId);
  const modelOptions = [
    ...(chosenProvider?.models ?? []),
    ...(chosenProvider?.extra_models ?? []),
  ].filter(isChatCapableModel);
  const hasModels = modelOptions.length > 0;

  const handleProviderChange = (pid: string) => {
    setSelectedProviderId(pid);
    setSelectedModel(undefined);
    setDirty(true);
  };

  const handleModelChange = (model: string) => {
    setSelectedModel(model);
    setDirty(true);
  };

  const cancelDraft = () => {
    setSelectedProviderId(currentSlot?.provider_id || undefined);
    setSelectedModel(currentSlot?.model || undefined);
    setDirty(false);
  };

  const handleSave = async () => {
    if (!selectedProviderId || !selectedModel) return;

    const selectedProvider = providers.find((p) => p.id === selectedProviderId);
    const selectedModelInfo = [
      ...(selectedProvider?.models ?? []),
      ...(selectedProvider?.extra_models ?? []),
    ].find((model) => model.id === selectedModel);

    if (selectedProvider && selectedModelInfo) {
      const confirmed = await confirmFreeModelSwitch({
        provider: selectedProvider,
        model: selectedModelInfo,
        t,
      });
      if (!confirmed) return;
    }

    const body: ModelSlotRequest = {
      provider_id: selectedProviderId,
      model: selectedModel,
      scope: "global",
    };

    setSaving(true);
    try {
      await api.setActiveLlm(body);
      message.success(t("models.llmModelUpdated"));
      setDirty(false);
      onSaved();
    } catch (error) {
      const errMsg =
        error instanceof Error ? error.message : t("models.failedToSave");
      message.error(errMsg);
    } finally {
      setSaving(false);
    }
  };

  const isActive =
    currentSlot &&
    currentSlot.provider_id === selectedProviderId &&
    currentSlot.model === selectedModel;
  const canSave = dirty && !!selectedProviderId && !!selectedModel;

  return (
    <Card className={styles.slotSection} title="所有智能体的默认模型">
      <div className={styles.slotForm}>
        <div className={styles.slotScope}>
          <span>作用范围</span>
          <strong>所有未设置专用模型的智能体</strong>
          <small>保存后从新消息开始生效；历史回复保留原模型身份。</small>
          <em data-dirty={dirty ? "true" : "false"}>
            {dirty ? "尚未保存" : "已保存"}
          </em>
        </div>
        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.provider")}</label>
          <Select
            style={{ width: "100%" }}
            placeholder={t("models.selectProvider")}
            value={selectedProviderId}
            onChange={handleProviderChange}
            options={eligible.map((p) => ({
              value: p.id,
              label: p.name,
            }))}
          />
        </div>

        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.model")}</label>
          <Select
            style={{ width: "100%" }}
            placeholder={
              hasModels ? t("models.selectModel") : t("models.addModelFirst")
            }
            disabled={!hasModels}
            showSearch
            optionFilterProp="label"
            value={selectedModel}
            onChange={handleModelChange}
            options={modelOptions.map((m) => ({
              value: m.id,
              label: `${m.name} (${m.id})`,
            }))}
          />
        </div>

        <div className={[styles.slotField, styles.slotActionField].join(" ")}>
          <label
            className={[styles.slotLabel, styles.visuallyHiddenLabel].join(" ")}
          >
            {t("models.actions")}
          </label>
          <div className={styles.defaultSlotActions}>
            <Button disabled={!dirty || saving} onClick={cancelDraft}>
              取消修改
            </Button>
            <Button
              type="primary"
              loading={saving}
              disabled={!canSave}
              onClick={handleSave}
              icon={<SaveOutlined />}
            >
              {isActive ? t("models.saved") : t("models.save")}
            </Button>
          </div>
        </div>
      </div>
      <p className={styles.slotDescription}>{t("models.llmDescription")}</p>
    </Card>
  );
}
