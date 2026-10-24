import React, { useEffect, useMemo, useRef, useState } from "react";
import { Button, Card } from "@/vendor/lantaiDesign";
import { SyncOutlined } from "@ant-design/icons";
import type { ActiveModelsInfo, ProviderInfo } from "../../../../../api/types";
import api from "../../../../../api";
import { ModelManageModal } from "../modals/ModelManageModal";
import { ProviderConfigModal } from "../modals/ProviderConfigModal";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import styles from "../../index.module.less";
import { ProviderIcon } from "../ProviderIconComponent";

interface LocalProviderCardProps {
  provider: ProviderInfo;
  activeModels: ActiveModelsInfo | null;
  onSaved: () => void | Promise<void>;
}

type RuntimeHealth = "checking" | "online" | "offline";

export const LocalProviderCard = React.memo(function LocalProviderCard({
  provider,
  activeModels,
  onSaved,
}: LocalProviderCardProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [modelManageOpen, setModelManageOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth>("checking");
  const [runtimeDetail, setRuntimeDetail] = useState("");
  const autoDiscoveryKeyRef = useRef<string | null>(null);

  const allModels = useMemo(
    () => [...provider.models, ...provider.extra_models],
    [provider.extra_models, provider.models],
  );
  const totalCount = allModels.length;

  useEffect(() => {
    let active = true;
    const discoveryKey = `${provider.id}:${provider.base_url}`;
    setRuntimeHealth("checking");
    setRuntimeDetail("");

    const checkRuntime = async () => {
      try {
        const result = await api.testProviderConnection(provider.id);
        if (!active) return;
        setRuntimeHealth(result.success ? "online" : "offline");
        setRuntimeDetail(result.message || "");

        if (
          result.success &&
          totalCount === 0 &&
          autoDiscoveryKeyRef.current !== discoveryKey
        ) {
          autoDiscoveryKeyRef.current = discoveryKey;
          setSyncing(true);
          const discovery = await api.discoverModels(
            provider.id,
            undefined,
            true,
          );
          if (!active) return;
          if (discovery.success) {
            await onSaved();
          } else {
            setRuntimeHealth("offline");
            setRuntimeDetail(discovery.message || "");
          }
        }
      } catch (error) {
        if (!active) return;
        setRuntimeHealth("offline");
        setRuntimeDetail(error instanceof Error ? error.message : "");
      } finally {
        if (active) setSyncing(false);
      }
    };

    void checkRuntime();

    return () => {
      active = false;
    };
  }, [onSaved, provider.base_url, provider.id, totalCount]);
  const agentReadyCount = allModels.filter(
    (model) => model.supports_tool_use === true,
  ).length;
  const knownToolCapabilityCount = allModels.filter(
    (model) => model.supports_tool_use != null,
  ).length;

  const readinessLabel = useMemo(() => {
    if (totalCount === 0) return t("models.localCapabilitiesAfterSync");
    if (agentReadyCount > 0) {
      return t("models.localAgentReadyCount", { count: agentReadyCount });
    }
    if (knownToolCapabilityCount === totalCount) {
      return t("models.localModelsNoTools");
    }
    return t("models.localToolCapabilityUnknown");
  }, [agentReadyCount, knownToolCapabilityCount, t, totalCount]);

  const status = {
    checking: {
      label: t("models.localRuntimeChecking"),
      className: styles.partial,
      color: "#faad14",
      shadow: "0 0 0 2px rgba(250, 173, 20, 0.2)",
    },
    online: {
      label: t("models.localRuntimeOnline"),
      className: styles.enabled,
      color: "rgba(20, 184, 166, 1)",
      shadow: "0 0 0 2px rgba(20, 184, 166, 0.2)",
    },
    offline: {
      label: t("models.localRuntimeOffline"),
      className: styles.disabled,
      color: "#d9d9d9",
      shadow: "none",
    },
  }[runtimeHealth];

  const handleSync = async () => {
    setSyncing(true);
    try {
      const result = await api.discoverModels(provider.id, undefined, true);
      if (!result.success) {
        setRuntimeHealth("offline");
        setRuntimeDetail(result.message || "");
        message.error(result.message || t("models.autoDiscoverModelsFailed"));
        return;
      }
      setRuntimeHealth("online");
      setRuntimeDetail(result.message || "");
      await onSaved();
      if (result.added_count > 0) {
        message.success(
          t("models.syncLocalModelsSuccess", {
            count: result.models.length,
          }),
        );
      } else {
        message.info(
          t("models.syncLocalModelsNoNew", {
            count: result.models.length,
          }),
        );
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : "";
      setRuntimeHealth("offline");
      setRuntimeDetail(detail);
      message.error(detail || t("models.autoDiscoverModelsFailed"));
    } finally {
      setSyncing(false);
    }
  };

  return (
    <Card hoverable className={styles.providerCard}>
      <div className={styles.cardHeaderRow}>
        <ProviderIcon providerId={provider.id} size={32} />
        <div className={styles.cardStatusHeader} title={runtimeDetail}>
          <span
            className={styles.statusDot}
            style={{
              backgroundColor: status.color,
              boxShadow: status.shadow,
            }}
          />
          <span className={`${styles.statusText} ${status.className}`}>
            {status.label}
          </span>
        </div>
      </div>

      <div className={styles.cardTitleRow}>
        <span className={styles.cardName}>{provider.name}</span>
        <span className={styles.localTag}>{t("models.local")}</span>
      </div>

      <div className={styles.cardInfo}>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>{t("models.localEndpoint")}:</span>
          <span className={styles.infoValue} title={provider.base_url}>
            {provider.base_url || t("models.notSet")}
          </span>
        </div>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>{t("models.model")}:</span>
          <span className={styles.infoValue}>
            {totalCount > 0
              ? t("models.modelsCount", { count: totalCount })
              : t("models.noModels")}
          </span>
        </div>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>
            {t("models.localAgentReadiness")}:
          </span>
          <span className={styles.infoValue}>{readinessLabel}</span>
        </div>
        {runtimeHealth === "offline" && (
          <div className={styles.localRuntimeHint} title={runtimeDetail}>
            {t("models.localRuntimeStartHint")}
          </div>
        )}
      </div>

      <div className={styles.cardActions}>
        <Button
          type="default"
          size="small"
          data-testid={`provider-models-${provider.id}`}
          onClick={(event) => {
            event.stopPropagation();
            setModelManageOpen(true);
          }}
          className={styles.actionBtn}
        >
          {t("models.models")}
        </Button>
        <Button
          type="default"
          size="small"
          icon={<SyncOutlined />}
          loading={syncing}
          onClick={(event) => {
            event.stopPropagation();
            void handleSync();
          }}
          className={styles.actionBtn}
        >
          {t("models.syncLocalModels")}
        </Button>
        <Button
          type="default"
          size="small"
          data-testid={`provider-settings-${provider.id}`}
          onClick={(event) => {
            event.stopPropagation();
            setSettingsOpen(true);
          }}
          className={styles.actionBtn}
        >
          {t("models.settings")}
        </Button>
      </div>

      <ProviderConfigModal
        provider={provider}
        activeModels={activeModels}
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={onSaved}
      />
      <ModelManageModal
        provider={provider}
        open={modelManageOpen}
        onClose={() => setModelManageOpen(false)}
        onSaved={onSaved}
      />
    </Card>
  );
});
