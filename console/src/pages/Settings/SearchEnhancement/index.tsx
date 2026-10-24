import { Button, Switch, Tooltip } from "@/vendor/lantaiDesign";
import { PageHeader } from "@/components/PageHeader";
import {
  Activity,
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  CircleDashed,
  ExternalLink,
  FileSearch,
  Flame,
  KeyRound,
  RefreshCw,
  Search,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import guanlanLogo from "../../../assets/guanlan-logo.png";
import styles from "./index.module.less";
import { useSearchEnhancement } from "./useSearchEnhancement";

const CAPABILITY_META: Record<
  string,
  { icon: LucideIcon; translationKey: string }
> = {
  guanlan_search: {
    icon: Search,
    translationKey: "mcp.guanlanToolSearch",
  },
  guanlan_read: {
    icon: BookOpenText,
    translationKey: "mcp.guanlanToolRead",
  },
  guanlan_hotnews: {
    icon: Flame,
    translationKey: "mcp.guanlanToolHotnews",
  },
  guanlan_research: {
    icon: FileSearch,
    translationKey: "mcp.guanlanToolResearch",
  },
};

function formatRuntimeTime(value: number | null | undefined, locale: string) {
  if (!value) return "—";
  return new Date(value * 1000).toLocaleString(locale, { hour12: false });
}

export default function SearchEnhancementPage() {
  const { t, i18n } = useTranslation();
  const { guanlan, loading, mutating, setEnabled, refresh } =
    useSearchEnhancement();
  const enabled = guanlan?.enabled ?? false;
  const packageStatus = guanlan?.package_status ?? "idle";
  const capabilityStatus =
    guanlan?.capability_status ??
    (!enabled
      ? "disabled"
      : packageStatus === "error"
        ? "error"
        : guanlan?.package_available
          ? "ready"
          : "starting");
  const hasError = capabilityStatus === "error";
  const versionText = guanlan?.package_version
    ? guanlan.runtime_source === "user"
      ? t("mcp.guanlanUserVersion", { version: guanlan.package_version })
      : t("mcp.guanlanBundledVersion", { version: guanlan.package_version })
    : t("mcp.guanlanPreparing");
  const statusText =
    capabilityStatus === "ready"
      ? versionText
      : capabilityStatus === "degraded"
        ? t("mcp.guanlanRuntimeDegraded")
        : capabilityStatus === "error"
          ? t("mcp.guanlanRuntimeError")
          : capabilityStatus === "disabled"
            ? t("mcp.guanlanRuntimeDisabled")
            : t("mcp.guanlanRuntimeStarting");
  const statusIcon = hasError ? (
    <AlertCircle size={14} />
  ) : capabilityStatus === "ready" ? (
    <CheckCircle2 size={14} />
  ) : (
    <CircleDashed size={14} />
  );
  const statusTone = hasError
    ? styles.statusError
    : capabilityStatus === "ready"
      ? styles.statusReady
      : capabilityStatus === "degraded"
        ? styles.statusDegraded
        : styles.statusIdle;
  const runtimeStatusText =
    capabilityStatus === "ready"
      ? t("searchEnhancement.runtimeReady")
      : statusText;
  const capabilityTools = guanlan?.tools ?? [];
  const runtimeSource =
    guanlan?.runtime_source === "user"
      ? t("searchEnhancement.runtimeSourceUser")
      : t("searchEnhancement.runtimeSourceBundled");

  return (
    <main className={styles.page}>
      <PageHeader
        parent={t("searchEnhancement.parent")}
        current={t("searchEnhancement.title")}
        description={t("searchEnhancement.description")}
      />

      <section className={styles.content}>
        <header className={styles.sectionHeader}>
          <Search size={16} aria-hidden="true" />
          <div>
            <h2>{t("searchEnhancement.sectionTitle")}</h2>
            <p>{t("searchEnhancement.sectionDescription")}</p>
          </div>
        </header>

        <div className={styles.panel}>
          <div className={styles.providerRow}>
            <div className={styles.logo} aria-hidden="true">
              <img src={guanlanLogo} alt="" draggable={false} />
            </div>
            <div className={styles.copy}>
              <strong>{t("mcp.guanlanName")}</strong>
              <span>{t("mcp.guanlanSubtitle")}</span>
              <Tooltip
                title={
                  guanlan?.runtime_message ||
                  guanlan?.package_message ||
                  statusText
                }
              >
                <em className={hasError ? styles.errorStatus : undefined}>
                  {statusIcon}
                  {statusText}
                </em>
              </Tooltip>
            </div>
            <div className={styles.control}>
              <Switch
                aria-label={t("searchEnhancement.guanlanToggle")}
                checked={enabled}
                loading={loading || mutating}
                disabled={loading || mutating}
                onChange={(checked) => void setEnabled(checked)}
              />
            </div>
          </div>
        </div>

        <div className={styles.managementGrid}>
          <article className={styles.managementCard}>
            <header className={styles.cardHeader}>
              <span className={styles.cardIcon} aria-hidden="true">
                <Activity size={17} />
              </span>
              <div>
                <h3>{t("searchEnhancement.runtimeTitle")}</h3>
                <p>{t("searchEnhancement.runtimeDescription")}</p>
              </div>
            </header>

            <dl className={styles.runtimeFacts}>
              <div>
                <dt>{t("searchEnhancement.runtimeState")}</dt>
                <dd className={statusTone}>
                  <span aria-hidden="true" />
                  {runtimeStatusText}
                </dd>
              </div>
              <div>
                <dt>{t("searchEnhancement.runtimeSource")}</dt>
                <dd>{runtimeSource}</dd>
              </div>
              <div>
                <dt>{t("searchEnhancement.runtimeVersion")}</dt>
                <dd>{guanlan?.package_version ?? "—"}</dd>
              </div>
              <div>
                <dt>{t("searchEnhancement.runtimeUpdated")}</dt>
                <dd>
                  {formatRuntimeTime(
                    guanlan?.runtime_updated_at,
                    i18n.resolvedLanguage || i18n.language || "zh-CN",
                  )}
                </dd>
              </div>
            </dl>

            <footer className={styles.diagnosticsFooter}>
              <span>
                {t("searchEnhancement.runtimeAttempts", {
                  count: guanlan?.runtime_attempt_count ?? 0,
                })}
                {guanlan?.runtime_error_code
                  ? ` · ${guanlan.runtime_error_code}`
                  : ""}
              </span>
              <Button
                size="small"
                icon={<RefreshCw size={14} />}
                loading={loading}
                disabled={mutating}
                aria-label={t("searchEnhancement.refreshStatus")}
                onClick={() => void refresh()}
              >
                {t("searchEnhancement.refreshStatus")}
              </Button>
            </footer>
          </article>

          <article className={styles.managementCard}>
            <header className={styles.cardHeader}>
              <span className={styles.cardIcon} aria-hidden="true">
                <ShieldCheck size={17} />
              </span>
              <div>
                <h3>{t("searchEnhancement.boundariesTitle")}</h3>
                <p>{t("searchEnhancement.boundariesDescription")}</p>
              </div>
            </header>

            <ul className={styles.boundaryList}>
              <li>
                <ShieldCheck size={15} aria-hidden="true" />
                <span>{t("searchEnhancement.readOnly")}</span>
              </li>
              <li>
                <KeyRound size={15} aria-hidden="true" />
                <span>{t("mcp.guanlanNoKey")}</span>
              </li>
              <li>
                <CheckCircle2 size={15} aria-hidden="true" />
                <span>{t("mcp.guanlanFree")}</span>
              </li>
            </ul>

            <div className={styles.projectMeta}>
              <span>
                {t("searchEnhancement.licenseLabel")}
                <strong>{guanlan?.license ?? "MIT License"}</strong>
              </span>
              <a
                href={
                  guanlan?.repository_url ??
                  "https://github.com/shenyangs/GuanLan"
                }
                target="_blank"
                rel="noreferrer"
                aria-label={t("mcp.guanlanRepo")}
              >
                {t("mcp.guanlanRepo")}
                <ExternalLink size={13} aria-hidden="true" />
              </a>
            </div>
          </article>
        </div>

        <section
          className={styles.capabilitiesCard}
          aria-labelledby="guanlan-capabilities-title"
        >
          <header className={styles.cardHeader}>
            <span className={styles.cardIcon} aria-hidden="true">
              <FileSearch size={17} />
            </span>
            <div>
              <h3 id="guanlan-capabilities-title">
                {t("searchEnhancement.capabilitiesTitle")}
              </h3>
              <p>{t("searchEnhancement.capabilitiesDescription")}</p>
            </div>
          </header>

          <div className={styles.capabilityGrid}>
            {capabilityTools.map((tool) => {
              const meta = CAPABILITY_META[tool.name];
              const CapabilityIcon = meta?.icon ?? FileSearch;
              return (
                <article className={styles.capabilityItem} key={tool.name}>
                  <CapabilityIcon size={17} aria-hidden="true" />
                  <p>
                    {meta ? t(meta.translationKey) : tool.description || tool.name}
                  </p>
                </article>
              );
            })}
          </div>
        </section>
      </section>
    </main>
  );
}
