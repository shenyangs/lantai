import { useEffect, useRef, useState } from "react";
import {
  CheckCircleOutlined,
  DisconnectOutlined,
  LinkOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import { Button, Input, Modal } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";

import { api } from "@/api";
import type { KdocsConnectionStatus } from "@/api/types";
import { useAppMessage } from "@/hooks/useAppMessage";
import { openExternalUrl } from "@/utils/openExternalUrl";
import styles from "./SkillDrawer.module.less";

const DEFAULT_TOKEN_URL = "https://www.kdocs.cn/latest";

export function KdocsConnectionPanel() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [status, setStatus] = useState<KdocsConnectionStatus | null>(null);
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const statusRequestRef = useRef(0);

  useEffect(() => {
    let active = true;
    const requestId = ++statusRequestRef.current;
    setLoading(true);
    api
      .getKdocsConnectionStatus()
      .then((nextStatus) => {
        if (active && statusRequestRef.current === requestId) {
          setStatus(nextStatus);
        }
      })
      .catch((error: unknown) => {
        if (!active || statusRequestRef.current !== requestId) return;
        message.error(
          error instanceof Error
            ? error.message
            : t("skills.kdocsStatusFailed"),
        );
      })
      .finally(() => {
        if (active && statusRequestRef.current === requestId) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [message, t]);

  const connect = async () => {
    const normalizedToken = token.trim();
    if (!normalizedToken) {
      message.error(t("skills.kdocsTokenRequired"));
      return;
    }
    statusRequestRef.current += 1;
    setLoading(false);
    setConnecting(true);
    try {
      const nextStatus = await api.connectKdocs(normalizedToken);
      setStatus(nextStatus);
      message.success(t("skills.kdocsConnectSuccess"));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("skills.kdocsConnectFailed"),
      );
    } finally {
      setToken("");
      setConnecting(false);
    }
  };

  const disconnect = () => {
    Modal.confirm({
      title: t("skills.kdocsDisconnectTitle"),
      content: t("skills.kdocsDisconnectConfirm"),
      okText: t("skills.kdocsDisconnect"),
      cancelText: t("common.cancel"),
      okButtonProps: { danger: true },
      onOk: async () => {
        statusRequestRef.current += 1;
        setDisconnecting(true);
        try {
          const nextStatus = await api.disconnectKdocs();
          setStatus(nextStatus);
          message.success(t("skills.kdocsDisconnectSuccess"));
        } catch (error) {
          message.error(
            error instanceof Error
              ? error.message
              : t("skills.kdocsDisconnectFailed"),
          );
          throw error;
        } finally {
          setDisconnecting(false);
        }
      },
    });
  };

  const tokenUrl = status?.token_url || DEFAULT_TOKEN_URL;
  const authenticated = Boolean(status?.authenticated);

  return (
    <section className={styles.kdocsPanel} aria-label={t("skills.kdocsTitle")}>
      <div className={styles.kdocsHeader}>
        <div>
          <div className={styles.kdocsTitle}>{t("skills.kdocsTitle")}</div>
          <div className={styles.kdocsDescription}>
            {t("skills.kdocsDescription")}
          </div>
        </div>
        <div
          className={`${styles.kdocsStatus} ${
            authenticated ? styles.kdocsStatusConnected : ""
          }`}
        >
          {loading ? (
            <>
              <LoadingOutlined /> {t("skills.kdocsChecking")}
            </>
          ) : authenticated ? (
            <>
              <CheckCircleOutlined /> {t("skills.kdocsConnected")}
            </>
          ) : (
            t("skills.kdocsNotConnected")
          )}
        </div>
      </div>

      {status?.cli_version && (
        <div className={styles.kdocsVersion}>
          {t("skills.kdocsCliVersion", { version: status.cli_version })}
        </div>
      )}

      <div className={styles.kdocsSteps}>
        <span>{t("skills.kdocsStepGetToken")}</span>
        <Button
          size="small"
          icon={<LinkOutlined />}
          onClick={() => {
            void openExternalUrl(tokenUrl).catch((error: unknown) => {
              message.error(
                error instanceof Error
                  ? error.message
                  : t("skills.kdocsOpenTokenFailed"),
              );
            });
          }}
        >
          {t("skills.kdocsOpenTokenPage")}
        </Button>
      </div>

      <label className={styles.kdocsTokenLabel} htmlFor="kdocs-token-input">
        {t("skills.kdocsTokenLabel")}
      </label>
      <div className={styles.kdocsConnectRow}>
        <Input.Password
          id="kdocs-token-input"
          aria-label={t("skills.kdocsTokenLabel")}
          value={token}
          autoComplete="new-password"
          placeholder={t("skills.kdocsTokenPlaceholder")}
          onChange={(event) => setToken(event.target.value)}
          onPressEnter={() => void connect()}
        />
        <Button
          type="primary"
          loading={connecting}
          disabled={!token.trim() || loading}
          onClick={() => void connect()}
        >
          {authenticated
            ? t("skills.kdocsUpdateToken")
            : t("skills.kdocsConnect")}
        </Button>
      </div>
      <div className={styles.kdocsPrivacyHint}>
        {t("skills.kdocsPrivacyHint")}
      </div>

      {authenticated && (
        <Button
          className={styles.kdocsDisconnect}
          type="text"
          danger
          size="small"
          icon={<DisconnectOutlined />}
          loading={disconnecting}
          onClick={disconnect}
        >
          {t("skills.kdocsDisconnect")}
        </Button>
      )}
    </section>
  );
}
