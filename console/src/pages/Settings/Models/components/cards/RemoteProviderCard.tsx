import React, { useState } from "react";
import { Card, Button, Modal } from "@/vendor/lantaiDesign";
import type { ProviderInfo, ActiveModelsInfo } from "../../../../../api/types";
import { ProviderConfigModal } from "../modals/ProviderConfigModal";
import { ModelManageModal } from "../modals/ModelManageModal";
import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import styles from "../../index.module.less";
import { ProviderIcon } from "../ProviderIconComponent";
import { remoteProviderReadiness } from "../../providerReadiness";
import { isTokenDanceCredentialConnected } from "../../providerSections";

interface RemoteProviderCardProps {
  provider: ProviderInfo;
  activeModels: ActiveModelsInfo | null;
  onSaved: () => void;
  featured?: boolean;
  featuredDescription?: string;
  settingsOpen?: boolean;
  onSettingsOpenChange?: (open: boolean) => void;
}

export const RemoteProviderCard = React.memo(function RemoteProviderCard({
  provider,
  activeModels,
  onSaved,
  featured = false,
  featuredDescription,
  settingsOpen,
  onSettingsOpenChange,
}: RemoteProviderCardProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [modalOpen, setModalOpen] = useState(false);
  const [modelManageOpen, setModelManageOpen] = useState(false);
  const resolvedSettingsOpen = settingsOpen ?? modalOpen;
  const setSettingsOpen = (open: boolean) => {
    setModalOpen(open);
    onSettingsOpenChange?.(open);
  };

  const handleDeleteProvider = (e: React.MouseEvent) => {
    e.stopPropagation();
    Modal.confirm({
      title: t("models.deleteProvider"),
      content: t("models.deleteProviderConfirm", { name: provider.name }),
      okText: t("common.delete"),
      okButtonProps: { danger: true },
      cancelText: t("models.cancel"),
      onOk: async () => {
        try {
          await api.deleteCustomProvider(provider.id);
          message.success(t("models.providerDeleted", { name: provider.name }));
          onSaved();
        } catch (error) {
          const errMsg =
            error instanceof Error
              ? error.message
              : t("models.providerDeleteFailed");
          message.error(errMsg);
        }
      },
    });
  };

  const totalCount = provider.models.length + provider.extra_models.length;
  const readiness = remoteProviderReadiness(provider);
  const isConfigured = readiness !== "not_configured";
  const isTokenDance = provider.id === "tokendance";
  const tokenDanceConnected =
    isTokenDance && isTokenDanceCredentialConnected(provider);
  const tokenDanceNeedsAuthorization = isTokenDance && !tokenDanceConnected;

  const providerTag = provider.is_custom ? (
    <span className={styles.customTag}>{t("models.custom")}</span>
  ) : (
    <span className={styles.builtinTag}>{t("models.builtin")}</span>
  );

  const statusLabel = tokenDanceNeedsAuthorization
    ? "等待账号授权"
    : tokenDanceConnected && readiness === "configured_unverified"
      ? "账号已授权 · 待验证"
      : readiness === "configured_unverified"
        ? t("models.providerAvailable")
        : readiness === "needs_connection_check"
          ? t("models.providerNeedsCheck")
          : readiness === "needs_models"
            ? t("models.providerNoModels")
            : t("models.providerNotConfigured");
  // Configuration and model discovery do not prove a live request succeeds.
  // Keep every configured provider in the partial state until a persisted
  // connection receipt exists.
  const statusType =
    isConfigured && !tokenDanceNeedsAuthorization ? "partial" : "disabled";
  const statusDotColor =
    isConfigured && !tokenDanceNeedsAuthorization ? "#faad14" : "#d9d9d9";
  const statusDotShadow =
    isConfigured && !tokenDanceNeedsAuthorization
      ? "0 0 0 2px rgba(250, 173, 20, 0.2)"
      : "none";

  const status = (
    <div className={styles.cardStatusHeader}>
      <span
        className={styles.statusDot}
        style={{
          backgroundColor: statusDotColor,
          boxShadow: statusDotShadow,
        }}
      />
      <span
        className={`${styles.statusText} ${
          statusType === "partial" ? styles.partial : styles.disabled
        }`}
      >
        {statusLabel}
      </span>
    </div>
  );

  const actions = (
    <>
      <Button
        type="default"
        size="small"
        onClick={(e) => {
          e.stopPropagation();
          setModelManageOpen(true);
        }}
        className={styles.actionBtn}
      >
        {t("models.models")}
      </Button>
      <Button
        type="default"
        size="small"
        data-testid={`provider-settings-${provider.id}`}
        onClick={(e) => {
          e.stopPropagation();
          setSettingsOpen(true);
        }}
        className={styles.actionBtn}
      >
        {t("models.settings")}
      </Button>
      {provider.is_custom && (
        <Button
          type="default"
          size="small"
          danger
          onClick={handleDeleteProvider}
          className={styles.actionBtn}
        >
          {t("common.delete")}
        </Button>
      )}
    </>
  );

  const dialogs = (
    <>
      <ProviderConfigModal
        provider={provider}
        activeModels={activeModels}
        open={resolvedSettingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={onSaved}
      />
      <ModelManageModal
        provider={provider}
        open={modelManageOpen}
        onClose={() => setModelManageOpen(false)}
        onSaved={onSaved}
      />
    </>
  );

  if (featured) {
    return (
      <Card
        hoverable
        className={`${styles.providerCard} ${styles.featuredProviderCard}`}
      >
        <div className={styles.featuredProviderMain}>
          <div className={styles.featuredProviderIdentity}>
            <ProviderIcon providerId={provider.id} size={40} />
            <div>
              <div className={styles.cardTitleRow}>
                <span className={styles.cardName}>{provider.name}</span>
                {providerTag}
              </div>
              <p>{featuredDescription}</p>
            </div>
          </div>
          {status}
        </div>
        <div className={styles.featuredProviderFooter}>
          <div className={styles.featuredProviderFacts}>
            <span>
              <small>实时目录</small>
              <strong>
                {totalCount > 0
                  ? t("models.modelsCount", { count: totalCount })
                  : t("models.noModels")}
              </strong>
            </span>
            <span>
              <small>能力分流</small>
              <strong>聊天 · 生图 · 生视频</strong>
            </span>
            <span>
              <small>连接方式</small>
              <strong>
                {tokenDanceConnected ? "观猹账号已授权" : "等待账号授权"}
              </strong>
            </span>
          </div>
          <div
            className={`${styles.cardActions} ${styles.featuredProviderActions}`}
          >
            {actions}
          </div>
        </div>
        {dialogs}
      </Card>
    );
  }

  return (
    <Card hoverable className={styles.providerCard}>
      {/* Card Header with Icon and Status */}
      <div className={styles.cardHeaderRow}>
        <ProviderIcon providerId={provider.id} size={32} />
        {status}
      </div>

      {/* Title Row */}
      <div className={styles.cardTitleRow}>
        <span className={styles.cardName}>{provider.name}</span>
        {providerTag}
      </div>

      {/* Info Section */}
      <div className={styles.cardInfo}>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>Base URL:</span>
          {provider.base_url ? (
            <span className={styles.infoValue} title={provider.base_url}>
              {provider.base_url}
            </span>
          ) : (
            <span className={styles.infoEmpty}>{t("models.notSet")}</span>
          )}
        </div>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>API Key:</span>
          {provider.api_key ? (
            <span className={styles.infoValue}>{provider.api_key}</span>
          ) : (
            <span className={styles.infoEmpty}>{t("models.notSet")}</span>
          )}
        </div>
        <div className={styles.infoRow}>
          <span className={styles.infoLabel}>Model:</span>
          <span className={styles.infoValue}>
            {totalCount > 0
              ? t("models.modelsCount", { count: totalCount })
              : t("models.noModels")}
          </span>
        </div>
      </div>

      <div className={styles.cardActions}>{actions}</div>
      {dialogs}
    </Card>
  );
});
