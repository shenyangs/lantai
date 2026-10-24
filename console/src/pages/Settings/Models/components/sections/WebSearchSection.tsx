import { useEffect, useState } from "react";
import { SaveOutlined } from "@ant-design/icons";
import { Button, Card, Input, Switch } from "@/vendor/lantaiDesign";
import { webSearchApi } from "@/api";
import type { WebSearchStatus } from "@/api/types";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "@/hooks/useAppMessage";
import styles from "../../index.module.less";

const DEFAULT_STATUS: WebSearchStatus = {
  configured: false,
  enabled: false,
  provider: "bing_rss",
  search_count: 0,
  fetch_count: 0,
  migrated_from_quick_search: false,
  credential_error: false,
  last_used_at: "",
};

export function WebSearchSection() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [status, setStatus] = useState<WebSearchStatus>(DEFAULT_STATUS);
  const [apiKey, setApiKey] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let active = true;
    void webSearchApi
      .getConfig()
      .then((value) => {
        if (!active) return;
        setStatus(value);
        setEnabled(value.enabled);
      })
      .catch(() => {
        if (active) setLoadError(true);
      })
      .finally(() => {
        if (active) setLoaded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  const canSave =
    loaded &&
    !loadError &&
    dirty &&
    (!enabled || Boolean(apiKey.trim() || status.configured)) &&
    !saving;

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    try {
      const updated = await webSearchApi.updateConfig({
        api_key: apiKey.trim(),
        enabled,
      });
      setStatus(updated);
      setEnabled(updated.enabled);
      setApiKey("");
      setDirty(false);
      message.success(t("models.webSearchUpdated"));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("models.failedToSave"),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      className={styles.slotSection}
      data-testid="web-search-settings"
      title={t("models.webSearchTitle")}
    >
      <p className={styles.imageGenerationIntro}>
        {t("models.webSearchDescription")}
      </p>
      <div className={`${styles.slotForm} ${styles.utilitySlotForm}`}>
        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.webSearchKey")}</label>
          <Input.Password
            aria-label={t("models.webSearchKey")}
            disabled={!loaded || loadError || saving}
            autoComplete="new-password"
            value={apiKey}
            placeholder={t("models.webSearchKeyPlaceholder")}
            onChange={(event) => {
              setApiKey(event.target.value);
              setDirty(true);
            }}
          />
        </div>
        <div className={styles.slotActionField}>
          <label className={styles.slotLabel}>
            {t("models.webSearchEnable")}
          </label>
          <Switch
            aria-label={t("models.webSearchEnable")}
            disabled={!loaded || loadError || saving}
            checked={enabled}
            onChange={(checked) => {
              setEnabled(checked);
              setDirty(true);
            }}
          />
        </div>
      </div>
      {loaded && !loadError ? (
        <p className={styles.slotDescription}>
          {status.configured
            ? t("models.webSearchConfigured")
            : t("models.webSearchNotConfigured")}
        </p>
      ) : null}
      <p className={styles.slotDescription}>
        {t("models.webSearchSourceBoundary")}
      </p>
      <p className={styles.slotDescription}>{t("models.webSearchPricing")}</p>
      {status.migrated_from_quick_search ? (
        <p className={styles.slotDescription}>
          {t("models.webSearchMigrated")}
        </p>
      ) : null}
      {status.credential_error || loadError ? (
        <p role="alert" className={styles.slotDescription}>
          {t(
            loadError
              ? "models.webSearchLoadFailed"
              : "models.webSearchCredentialError",
          )}
        </p>
      ) : null}
      {status.configured ? (
        <p className={styles.slotDescription}>
          {t("models.webSearchUsage", {
            search: status.search_count,
            fetch: status.fetch_count,
          })}
        </p>
      ) : null}
      <div className={styles.slotActions}>
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={saving}
          disabled={!canSave}
          onClick={save}
        >
          {t("models.webSearchSave")}
        </Button>
      </div>
    </Card>
  );
}
