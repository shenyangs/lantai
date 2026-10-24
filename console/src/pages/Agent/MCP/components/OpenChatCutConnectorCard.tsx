import { useEffect, useState } from "react";
import { CheckCircleFilled, LinkOutlined } from "@ant-design/icons";
import { Button, Input, Modal } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";

import { api } from "@/api";
import type { OpenChatCutMCPInfo } from "@/api/types";
import { ConnectorBrandIcon } from "@/components/ConnectorBrandIcon";
import { useAppMessage } from "@/hooks/useAppMessage";
import { openExternalUrl } from "@/utils/openExternalUrl";
import styles from "../index.module.less";

interface OpenChatCutConnectorCardProps {
  onClientsChanged: () => Promise<void> | void;
}

const DEFAULT_ENDPOINT = "http://127.0.0.1:5199/api/external-mcp/mcp";

export function OpenChatCutConnectorCard({
  onClientsChanged,
}: OpenChatCutConnectorCardProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [info, setInfo] = useState<OpenChatCutMCPInfo | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [endpoint, setEndpoint] = useState(DEFAULT_ENDPOINT);
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void api
      .getOpenChatCutMCP()
      .then((next) => {
        setInfo(next);
        setEndpoint(next.endpoint);
      })
      .catch((error: unknown) =>
        message.error(
          error instanceof Error
            ? error.message
            : t("mcp.openChatCutLoadError"),
        ),
      );
  }, [message, t]);

  const statusLabel = info
    ? t(
        `mcp.openChatCut${
          {
            not_configured: "NotConfigured",
            disabled: "Disabled",
            connecting: "Connecting",
            ready: "Ready",
            error: "Error",
          }[info.connection_status]
        }`,
      )
    : t("common.loading");

  const configure = async (enabled: boolean) => {
    if (!info) return;
    setSaving(true);
    try {
      const configured = await api.configureOpenChatCutMCP({
        endpoint: endpoint.trim() || DEFAULT_ENDPOINT,
        ...(token.trim() ? { token: token.trim() } : {}),
        enabled,
      });
      let next = configured;
      if (enabled) {
        const probe = await api.probeMCPConnector("openchatcut");
        next = {
          ...configured,
          connection_status: probe.status === "ready" ? "ready" : "error",
        };
      }
      setInfo(next);
      setEndpoint(next.endpoint);
      setToken("");
      setModalOpen(false);
      await onClientsChanged();
      message.success(
        enabled
          ? t("mcp.openChatCutSaved")
          : t("mcp.openChatCutDisabledMessage"),
      );
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("mcp.openChatCutSaveError"),
      );
    } finally {
      setSaving(false);
    }
  };

  const openDocs = () => {
    if (!info) return;
    void openExternalUrl(info.docs_url).catch((error: unknown) =>
      message.error(
        error instanceof Error ? error.message : t("mcp.catalogConnectError"),
      ),
    );
  };

  if (!info) return null;

  return (
    <>
      <article className={styles.catalogCard}>
        <ConnectorBrandIcon
          connectorKey="openchatcut"
          name={info.name}
          fallback="剪"
          className={`${styles.catalogMark} ${styles.catalogBrandMark}`}
        />
        <div className={styles.catalogBody}>
          <div className={styles.catalogTitleRow}>
            <h3>{info.name}</h3>
            {info.connection_status === "ready" && (
              <span className={styles.catalogConnected}>
                <CheckCircleFilled /> {statusLabel}
              </span>
            )}
            {info.connection_status !== "ready" && (
              <span className={styles.catalogOfficial}>{statusLabel}</span>
            )}
          </div>
          <p>{info.description}</p>
          <div className={styles.catalogMeta}>
            MCP · {info.license} · {t("mcp.openChatCutManualReview")}
          </div>
          <button
            type="button"
            className={styles.catalogDocsLink}
            onClick={openDocs}
          >
            {t("mcp.catalogDocs")}
          </button>
        </div>
        <Button size="small" onClick={() => setModalOpen(true)}>
          {info.configured ? t("mcp.catalogManage") : t("mcp.catalogConnect")}
        </Button>
      </article>

      <Modal
        title={info.name}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        destroyOnHidden
        width={620}
        footer={
          <div className={styles.openChatCutModalActions}>
            {info.configured && info.enabled && (
              <Button
                danger
                disabled={saving}
                onClick={() => void configure(false)}
              >
                {t("mcp.openChatCutDisable")}
              </Button>
            )}
            <div className={styles.openChatCutModalPrimaryActions}>
              <Button onClick={() => setModalOpen(false)}>
                {t("common.cancel")}
              </Button>
              <Button
                type="primary"
                loading={saving}
                disabled={
                  !endpoint.trim() || (!info.token_configured && !token.trim())
                }
                onClick={() => void configure(true)}
              >
                {t("mcp.openChatCutSave")}
              </Button>
            </div>
          </div>
        }
      >
        <div className={styles.openChatCutModal}>
          <p>{t("mcp.openChatCutSetupHint")}</p>
          <label htmlFor="openchatcut-endpoint">
            {t("mcp.openChatCutEndpointLabel")}
          </label>
          <Input
            id="openchatcut-endpoint"
            aria-label={t("mcp.openChatCutEndpointLabel")}
            value={endpoint}
            onChange={(event) => setEndpoint(event.target.value)}
          />
          <label htmlFor="openchatcut-token">
            {t("mcp.openChatCutTokenLabel")}
          </label>
          <Input.Password
            id="openchatcut-token"
            aria-label={t("mcp.openChatCutTokenLabel")}
            value={token}
            autoComplete="new-password"
            placeholder={
              info.token_configured
                ? t("mcp.openChatCutTokenKeepPlaceholder")
                : t("mcp.openChatCutTokenPlaceholder")
            }
            onChange={(event) => setToken(event.target.value)}
          />
          <div className={styles.openChatCutPrivacyHint}>
            {t("mcp.openChatCutPrivacyHint")}
          </div>
          <Button type="link" icon={<LinkOutlined />} onClick={openDocs}>
            {t("mcp.openChatCutOpenGuide")}
          </Button>
        </div>
      </Modal>
    </>
  );
}
