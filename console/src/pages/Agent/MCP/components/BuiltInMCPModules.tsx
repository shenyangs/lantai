import { useEffect, useRef, useState } from "react";
import {
  BugOutlined,
  FileTextOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import { Switch } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";

import { api } from "@/api";
import type { ChromeDevToolsMCPInfo, OfficeCliMCPInfo } from "@/api/types";
import { useAppMessage } from "@/hooks/useAppMessage";
import { useAgentStore } from "@/stores/agentStore";
import styles from "../index.module.less";

export function BuiltInMCPModules() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const messageRef = useRef(message);
  const translationRef = useRef(t);
  const [officeCli, setOfficeCli] = useState<OfficeCliMCPInfo | null>(null);
  const [chromeDevTools, setChromeDevTools] =
    useState<ChromeDevToolsMCPInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatingKey, setUpdatingKey] = useState("");

  messageRef.current = message;
  translationRef.current = t;

  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([api.getOfficeCliMCP(), api.getChromeDevToolsMCP()])
      .then(([officeResult, chromeResult]) => {
        if (!active) return;
        if (officeResult.status === "fulfilled") {
          setOfficeCli(officeResult.value);
        } else {
          messageRef.current.error(
            translationRef.current("mcp.officeCliLoadError"),
          );
        }
        if (chromeResult.status === "fulfilled") {
          setChromeDevTools(chromeResult.value);
        } else {
          messageRef.current.error(
            translationRef.current("mcp.chromeDevToolsLoadError"),
          );
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selectedAgent]);

  const toggleOfficeCli = async (enabled: boolean) => {
    setUpdatingKey("officecli");
    try {
      setOfficeCli(await api.setOfficeCliMCP(enabled));
      message.success(
        enabled ? t("mcp.officeCliEnabled") : t("mcp.officeCliDisabled"),
      );
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("mcp.officeCliToggleError"),
      );
    } finally {
      setUpdatingKey("");
    }
  };

  const toggleChromeDevTools = async (enabled: boolean) => {
    setUpdatingKey("chrome-devtools");
    try {
      setChromeDevTools(await api.setChromeDevToolsMCP(enabled));
      message.success(
        enabled
          ? t("mcp.chromeDevToolsEnabled")
          : t("mcp.chromeDevToolsDisabled"),
      );
    } catch (error) {
      message.error(
        error instanceof Error
          ? error.message
          : t("mcp.chromeDevToolsToggleError"),
      );
    } finally {
      setUpdatingKey("");
    }
  };

  const statusText = officeCli?.available
    ? officeCli.version || t("mcp.officeCliReady")
    : t("mcp.officeCliUnavailable");
  const chromeStatusText = !chromeDevTools?.available
    ? t("mcp.chromeDevToolsUnavailable")
    : !chromeDevTools.enabled
      ? t("mcp.chromeDevToolsBundled", {
          version: chromeDevTools.package_version,
        })
      : chromeDevTools.connected
        ? t("mcp.chromeDevToolsReady")
        : chromeDevTools.capability_status === "error" ||
            chromeDevTools.capability_status === "degraded"
          ? t("mcp.chromeDevToolsError")
          : t("mcp.chromeDevToolsStarting");
  const chromeStatusHealthy = Boolean(
    chromeDevTools?.available &&
    !["error", "degraded"].includes(chromeDevTools.capability_status),
  );

  return (
    <section className={styles.section}>
      <header className={styles.sectionHeader}>
        <h2>{t("mcp.builtinModules")}</h2>
        <p>{t("mcp.builtinModulesDescription")}</p>
      </header>

      <div className={styles.builtinList}>
        <article className={styles.builtinRow}>
          <div className={styles.builtinIcon} aria-hidden="true">
            <FileTextOutlined />
          </div>
          <div className={styles.builtinBody}>
            <div className={styles.builtinTitleRow}>
              <h3>OfficeCLI</h3>
              <span
                className={
                  officeCli?.available
                    ? styles.builtinReady
                    : styles.builtinUnavailable
                }
              >
                {loading ? <LoadingOutlined /> : statusText}
              </span>
            </div>
            <p>{t("mcp.officeCliDescription")}</p>
          </div>
          <Switch
            aria-label={t("mcp.officeCliToggle")}
            checked={officeCli?.enabled ?? true}
            loading={loading || updatingKey === "officecli"}
            disabled={
              loading ||
              Boolean(updatingKey) ||
              (!officeCli?.available && !officeCli?.enabled)
            }
            onChange={(enabled) => void toggleOfficeCli(enabled)}
          />
        </article>

        <article className={styles.builtinRow}>
          <div className={styles.builtinIcon} aria-hidden="true">
            <BugOutlined />
          </div>
          <div className={styles.builtinBody}>
            <div className={styles.builtinTitleRow}>
              <h3>Chrome DevTools</h3>
              <span
                className={
                  chromeStatusHealthy
                    ? styles.builtinReady
                    : styles.builtinUnavailable
                }
              >
                {loading ? <LoadingOutlined /> : chromeStatusText}
              </span>
            </div>
            <p>{t("mcp.chromeDevToolsDescription")}</p>
            <p className={styles.catalogMeta}>
              {t("mcp.chromeDevToolsPrivacy")}
            </p>
          </div>
          <Switch
            aria-label={t("mcp.chromeDevToolsToggle")}
            checked={chromeDevTools?.enabled ?? false}
            loading={loading || updatingKey === "chrome-devtools"}
            disabled={
              loading || Boolean(updatingKey) || !chromeDevTools?.available
            }
            onChange={(enabled) => void toggleChromeDevTools(enabled)}
          />
        </article>
      </div>
    </section>
  );
}
