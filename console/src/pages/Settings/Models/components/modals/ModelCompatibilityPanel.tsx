import { useState } from "react";
import { Button, Tag } from "@/vendor/lantaiDesign";
import { ExperimentOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import api from "../../../../../api";
import type {
  ModelCompatibilityCheckStatus,
  ModelCompatibilityReport,
} from "../../../../../api/types";
import styles from "../../index.module.less";

interface ModelCompatibilityPanelProps {
  providerId: string;
  modelId: string;
}

const CHECK_STATUS_COLORS: Record<ModelCompatibilityCheckStatus, string> = {
  passed: "success",
  failed: "error",
  not_observed: "default",
};

export function ModelCompatibilityPanel({
  providerId,
  modelId,
}: ModelCompatibilityPanelProps) {
  const { t } = useTranslation();
  const [report, setReport] = useState<ModelCompatibilityReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const runProbe = async () => {
    setLoading(true);
    setError("");
    try {
      const nextReport = await api.checkModelCompatibility(providerId, {
        model_id: modelId,
      });
      setReport(nextReport);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : t("models.compatibilityProbeFailed"),
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className={styles.compatibilityPanel} aria-live="polite">
      <div className={styles.compatibilityPanelHeader}>
        <div>
          <h4>{t("models.compatibilityProbeTitle")}</h4>
          <p>{t("models.compatibilityProbeDisclosure")}</p>
        </div>
        <Button
          size="small"
          icon={<ExperimentOutlined />}
          loading={loading}
          onClick={runProbe}
          aria-label={t(
            report
              ? "models.rerunCompatibilityProbe"
              : "models.runCompatibilityProbe",
          )}
        >
          {t(
            report
              ? "models.rerunCompatibilityProbe"
              : "models.runCompatibilityProbe",
          )}
        </Button>
      </div>

      {error && (
        <div className={styles.compatibilityError} role="alert">
          {error}
        </div>
      )}

      {report && (
        <div className={styles.compatibilityReport}>
          <div className={styles.compatibilitySummary}>
            <span>{t("models.compatibilityOverall")}</span>
            <Tag
              color={
                report.status === "supported"
                  ? "success"
                  : report.status === "partial"
                    ? "warning"
                    : "error"
              }
            >
              {t(`models.compatibilityStatus.${report.status}`)}
            </Tag>
          </div>
          <ul className={styles.compatibilityCheckList}>
            {report.checks.map((check) => (
              <li key={check.id}>
                <span>{t(`models.compatibilityChecks.${check.id}`)}</span>
                <Tag color={CHECK_STATUS_COLORS[check.status]}>
                  {t(`models.compatibilityCheckStatus.${check.status}`)}
                </Tag>
              </li>
            ))}
          </ul>
          <div className={styles.compatibilityMeta}>
            {t("models.compatibilityProbeMeta", {
              duration: report.duration_ms,
              digest: report.trace_digest.slice(0, 12),
            })}
          </div>
        </div>
      )}
    </section>
  );
}
