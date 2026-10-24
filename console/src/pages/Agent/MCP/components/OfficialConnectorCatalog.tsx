import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircleFilled, LoadingOutlined } from "@ant-design/icons";
import { Button, Modal } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";

import { api } from "@/api";
import type {
  KdocsConnectionStatus,
  OfficialMCPConnectorInfo,
  WpsNoteConnectionStatus,
} from "@/api/types";
import { useAppMessage } from "@/hooks/useAppMessage";
import { ConnectorBrandIcon } from "@/components/ConnectorBrandIcon";
import {
  openExternalUrl,
  prepareExternalNavigation,
} from "@/utils/openExternalUrl";
import { KdocsConnectionPanel } from "../../Skills/components/KdocsConnectionPanel";
import { OpenChatCutConnectorCard } from "./OpenChatCutConnectorCard";
import styles from "../index.module.less";

interface OfficialConnectorCatalogProps {
  onClientsChanged: () => Promise<void> | void;
}

export function OfficialConnectorCatalog({
  onClientsChanged,
}: OfficialConnectorCatalogProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [remoteConnectors, setRemoteConnectors] = useState<
    OfficialMCPConnectorInfo[]
  >([]);
  const [kdocs, setKdocs] = useState<KdocsConnectionStatus | null>(null);
  const [wpsNote, setWpsNote] = useState<WpsNoteConnectionStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState("");
  const [kdocsModalOpen, setKdocsModalOpen] = useState(false);
  const [wpsNoteModalOpen, setWpsNoteModalOpen] = useState(false);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    try {
      const [remote, kdocsStatus, wpsNoteStatus] = await Promise.all([
        api.listOfficialMCPConnectors(),
        api.getKdocsConnectionStatus().catch(() => null),
        api.getWpsNoteConnectionStatus().catch(() => null),
      ]);
      setRemoteConnectors(remote);
      setKdocs(kdocsStatus);
      setWpsNote(wpsNoteStatus);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("mcp.catalogLoadError"),
      );
    } finally {
      setLoading(false);
    }
  }, [message, t]);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    if (!wpsNoteModalOpen || wpsNote?.connected) return;
    const timer = window.setInterval(() => {
      void api
        .getWpsNoteConnectionStatus()
        .then((status) => {
          setWpsNote(status);
          if (status.connected) message.success(t("mcp.wpsNoteConnected"));
        })
        .catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [message, t, wpsNote?.connected, wpsNoteModalOpen]);

  const configuredKeys = useMemo(
    () =>
      new Set(
        remoteConnectors
          .filter((item) => item.configured)
          .map((item) => item.key),
      ),
    [remoteConnectors],
  );

  const connectRemote = async (connector: OfficialMCPConnectorInfo) => {
    const navigation = prepareExternalNavigation();
    if (!navigation) {
      message.warning(t("mcp.catalogPopupBlocked"));
      return;
    }
    navigation.setTitle(
      t("mcp.catalogAuthorizing", {
        name: connector.name,
      }),
    );
    setBusyKey(connector.key);
    try {
      if (!configuredKeys.has(connector.key)) {
        await api.installOfficialMCPConnector(connector.key);
      }
      const started = await api.startMCPOAuth(connector.key);
      await navigation.open(started.authorization_url);
      setRemoteConnectors((items) =>
        items.map((item) =>
          item.key === connector.key ? { ...item, configured: true } : item,
        ),
      );
      await onClientsChanged();
      message.success(t("mcp.catalogAuthorizationOpened"));
    } catch (error) {
      navigation.close();
      message.error(
        error instanceof Error ? error.message : t("mcp.catalogConnectError"),
      );
    } finally {
      setBusyKey("");
    }
  };

  const closeKdocs = () => {
    setKdocsModalOpen(false);
    void api
      .getKdocsConnectionStatus()
      .then(setKdocs)
      .catch(() => undefined);
  };

  const connectWpsNote = async () => {
    setBusyKey("wps-note");
    try {
      const status = await api.connectWpsNote();
      setWpsNote(status);
      if (status.connected) {
        setWpsNoteModalOpen(true);
        return;
      }
      if (!status.desktop_installed) {
        await openExternalUrl(status.download_url);
        return;
      }
      setWpsNoteModalOpen(true);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("mcp.catalogConnectError"),
      );
    } finally {
      setBusyKey("");
    }
  };

  return (
    <section
      className={`${styles.section} ${styles.catalogSection}`}
      data-testid="mcp-official-catalog"
    >
      <header className={styles.sectionHeader}>
        <h2>{t("mcp.catalogTitle")}</h2>
        <p>{t("mcp.catalogDescription")}</p>
      </header>

      {loading ? (
        <div className={styles.catalogLoading}>
          <LoadingOutlined /> {t("common.loading")}
        </div>
      ) : (
        <div className={styles.catalogGrid}>
          <OpenChatCutConnectorCard onClientsChanged={onClientsChanged} />

          <article className={styles.catalogCard}>
            <ConnectorBrandIcon
              connectorKey="kdocs"
              name={t("mcp.kdocsName")}
              fallback="金"
              className={`${styles.catalogMark} ${styles.catalogBrandMark}`}
            />
            <div className={styles.catalogBody}>
              <div className={styles.catalogTitleRow}>
                <h3>{t("mcp.kdocsName")}</h3>
                {kdocs?.authenticated && (
                  <span className={styles.catalogConnected}>
                    <CheckCircleFilled /> {t("mcp.catalogConnected")}
                  </span>
                )}
              </div>
              <p>{t("mcp.kdocsDescription")}</p>
              <div className={styles.catalogMeta}>
                CLI · Skill{" "}
                {kdocs?.cli_version ? `· v${kdocs.cli_version}` : ""}
              </div>
            </div>
            <Button size="small" onClick={() => setKdocsModalOpen(true)}>
              {kdocs?.authenticated
                ? t("mcp.catalogManage")
                : t("mcp.catalogConnect")}
            </Button>
          </article>

          <article className={styles.catalogCard}>
            <ConnectorBrandIcon
              connectorKey="wps-note"
              name={t("mcp.wpsNoteName")}
              fallback="记"
              className={`${styles.catalogMark} ${styles.catalogBrandMark}`}
            />
            <div className={styles.catalogBody}>
              <div className={styles.catalogTitleRow}>
                <h3>{t("mcp.wpsNoteName")}</h3>
                {wpsNote?.connected && (
                  <span className={styles.catalogConnected}>
                    <CheckCircleFilled /> {t("mcp.catalogConnected")}
                  </span>
                )}
              </div>
              <p>{t("mcp.wpsNoteDescription")}</p>
              <div className={styles.catalogMeta}>
                MCP · CLI
                {wpsNote?.tool_count ? ` · ${wpsNote.tool_count} tools` : ""}
              </div>
            </div>
            <Button
              size="small"
              loading={busyKey === "wps-note"}
              onClick={() => void connectWpsNote()}
            >
              {wpsNote?.connected
                ? t("mcp.catalogManage")
                : t("mcp.catalogConnect")}
            </Button>
          </article>

          {remoteConnectors.map((connector) => (
            <article className={styles.catalogCard} key={connector.key}>
              <ConnectorBrandIcon
                connectorKey={connector.key}
                name={connector.name}
                className={`${styles.catalogMark} ${styles.catalogBrandMark}`}
              />
              <div className={styles.catalogBody}>
                <div className={styles.catalogTitleRow}>
                  <h3>{connector.name}</h3>
                  <span className={styles.catalogOfficial}>
                    {t("mcp.catalogOfficial")}
                  </span>
                </div>
                <p>{connector.description}</p>
                <button
                  type="button"
                  className={styles.catalogDocsLink}
                  onClick={() => {
                    void openExternalUrl(connector.docs_url).catch(
                      (error: unknown) =>
                        message.error(
                          error instanceof Error
                            ? error.message
                            : t("mcp.catalogConnectError"),
                        ),
                    );
                  }}
                >
                  {t("mcp.catalogOfficialDocs")}
                </button>
              </div>
              <Button
                size="small"
                loading={busyKey === connector.key}
                onClick={() => void connectRemote(connector)}
              >
                {connector.configured
                  ? t("mcp.catalogAuthorize")
                  : t("mcp.catalogConnect")}
              </Button>
            </article>
          ))}
        </div>
      )}

      <Modal
        title={t("mcp.kdocsName")}
        open={kdocsModalOpen}
        onCancel={closeKdocs}
        footer={null}
        width={620}
        destroyOnHidden
      >
        <KdocsConnectionPanel />
      </Modal>

      <Modal
        title={t("mcp.wpsNoteConnectTitle")}
        open={wpsNoteModalOpen}
        onCancel={() => setWpsNoteModalOpen(false)}
        footer={
          wpsNote?.connected ? (
            <Button type="primary" onClick={() => setWpsNoteModalOpen(false)}>
              {t("common.close")}
            </Button>
          ) : (
            <Button onClick={() => void connectWpsNote()}>
              {t("mcp.wpsNoteOpenAgain")}
            </Button>
          )
        }
        width={560}
      >
        <p>
          {wpsNote?.connected
            ? t("mcp.wpsNoteConnected")
            : t("mcp.wpsNoteEnableHint")}
        </p>
        {!wpsNote?.connected && (
          <p className={styles.catalogMeta}>{t("mcp.wpsNoteAutoDetecting")}</p>
        )}
      </Modal>
    </section>
  );
}
