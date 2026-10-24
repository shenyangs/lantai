import { Modal } from "@/vendor/lantaiDesign";
import { AlertTriangle, FileSearch, ShieldCheck } from "lucide-react";
import React from "react";
import type {
  SecurityScanErrorResponse,
  BlockedSkillFinding,
  BlockedSkillRecord,
  SkillScannerConfig,
} from "../api/modules/security";
import type { TFunction } from "i18next";

export function tryParseScanError(
  error: unknown,
): SecurityScanErrorResponse | null {
  if (!(error instanceof Error)) return null;
  const msg = error.message || "";
  const jsonStart = msg.indexOf("{");
  if (jsonStart === -1) return null;
  try {
    const parsed = JSON.parse(msg.substring(jsonStart));
    if (parsed?.type === "security_scan_failed") {
      return parsed as SecurityScanErrorResponse;
    }
  } catch {
    return null;
  }
  return null;
}

/** Cap long finding lists so modals stay readable; full history remains in alerts. */
const MAX_FINDINGS_IN_MODAL = 5;
const EMBEDDED_SCRIPT_PATTERN =
  /SVG file contains embedded script tags or event handlers that can execute JavaScript/i;

type ScanModalTone = "blocked" | "warn";

function isEmbeddedScriptFinding(finding: BlockedSkillFinding) {
  return (
    EMBEDDED_SCRIPT_PATTERN.test(finding.title || "") ||
    EMBEDDED_SCRIPT_PATTERN.test(finding.description || "")
  );
}

function getFindingTitle(finding: BlockedSkillFinding, t: TFunction) {
  if (isEmbeddedScriptFinding(finding)) {
    return t("security.skillScanner.scanError.embeddedScriptTitle");
  }
  return finding.title;
}

function getFindingDescription(finding: BlockedSkillFinding, t: TFunction) {
  if (isEmbeddedScriptFinding(finding)) {
    return t("security.skillScanner.scanError.embeddedScriptDescription");
  }
  return finding.description;
}

function renderIntro(tone: ScanModalTone, t: TFunction) {
  const isBlocked = tone === "blocked";
  return React.createElement(
    "div",
    { className: `lantai-scan-modal-intro lantai-scan-modal-intro--${tone}` },
    React.createElement(
      "div",
      { className: "lantai-scan-modal-intro-icon" },
      React.createElement(isBlocked ? AlertTriangle : ShieldCheck, {
        size: 20,
        strokeWidth: 2.2,
      }),
    ),
    React.createElement(
      "div",
      null,
      React.createElement(
        "div",
        { className: "lantai-scan-modal-status" },
        t(
          isBlocked
            ? "security.skillScanner.scanError.statusBlocked"
            : "security.skillScanner.scanError.statusEnabled",
        ),
      ),
      React.createElement(
        "div",
        { className: "lantai-scan-modal-summary" },
        t(
          isBlocked
            ? "security.skillScanner.scanError.blockedSummary"
            : "security.skillScanner.scanError.warnSummary",
        ),
      ),
    ),
  );
}

function renderFindings(findings: BlockedSkillFinding[], t: TFunction) {
  const total = findings.length;
  const shown = findings.slice(0, MAX_FINDINGS_IN_MODAL);
  const moreCount = total - shown.length;
  return React.createElement(
    "div",
    null,
    React.createElement(
      "div",
      { className: "lantai-scan-modal-findings-header" },
      React.createElement(FileSearch, { size: 16, strokeWidth: 2.2 }),
      t("security.skillScanner.scanError.findingsTitle"),
    ),
    React.createElement(
      "div",
      { className: "lantai-scan-modal-findings-list" },
      shown.map((f, i) =>
        React.createElement(
          "div",
          {
            key: i,
            className: "lantai-scan-modal-finding",
          },
          React.createElement(
            "strong",
            { className: "lantai-scan-modal-finding-title" },
            getFindingTitle(f, t),
          ),
          React.createElement(
            "div",
            { className: "lantai-scan-modal-finding-path" },
            f.file_path + (f.line_number ? `:${f.line_number}` : ""),
          ),
          getFindingDescription(f, t) &&
            React.createElement(
              "div",
              { className: "lantai-scan-modal-finding-description" },
              getFindingDescription(f, t),
            ),
        ),
      ),
      moreCount > 0 &&
        React.createElement(
          "div",
          {
            key: "more",
            className: "lantai-scan-modal-more",
          },
          t("security.skillScanner.scanError.moreFindings", {
            count: moreCount,
          }),
        ),
    ),
  );
}

export function showScanErrorModal(
  scanError: SecurityScanErrorResponse,
  t: TFunction,
) {
  const findings = scanError.findings || [];
  Modal.error({
    className: "lantai-scan-modal lantai-scan-modal--blocked",
    title: t("security.skillScanner.scanError.blockedTitle"),
    width: 640,
    icon: React.createElement(AlertTriangle, {
      size: 22,
      className: "lantai-scan-modal-root-icon",
    }),
    okText: t("security.skillScanner.scanError.acknowledge"),
    content: React.createElement(
      "div",
      null,
      React.createElement(
        "p",
        null,
        t("security.skillScanner.scanError.description"),
      ),
      renderIntro("blocked", t),
      renderFindings(findings, t),
    ),
  });
}

export function showScanWarnModal(
  findings: BlockedSkillFinding[],
  t: TFunction,
) {
  Modal.info({
    className: "lantai-scan-modal lantai-scan-modal--warn",
    title: t("security.skillScanner.scanError.warnTitle"),
    width: 640,
    icon: React.createElement(ShieldCheck, {
      size: 22,
      className: "lantai-scan-modal-root-icon",
    }),
    okText: t("security.skillScanner.scanError.acknowledge"),
    content: React.createElement(
      "div",
      null,
      React.createElement(
        "p",
        null,
        t("security.skillScanner.scanError.warnDescription"),
      ),
      renderIntro("warn", t),
      renderFindings(findings, t),
    ),
  });
}

/**
 * Check an error for a scan failure, show the modal if found, and return
 * whether it was handled.
 */
export function handleScanError(error: unknown, t: TFunction): boolean {
  const scanError = tryParseScanError(error);
  if (scanError) {
    showScanErrorModal(scanError, t);
    return true;
  }
  return false;
}

/**
 * After a successful operation, check if the scanner recorded any
 * warn-mode findings and show a warning modal if so.
 */
export async function checkScanWarnings(
  skillName: string,
  fetchAlerts: () => Promise<BlockedSkillRecord[]>,
  fetchScannerCfg: () => Promise<SkillScannerConfig>,
  t: TFunction,
): Promise<void> {
  try {
    const [alerts, scannerCfg] = await Promise.all([
      fetchAlerts(),
      fetchScannerCfg(),
    ]);
    if (!alerts.length) return;
    if (
      scannerCfg?.whitelist?.some(
        (w: { skill_name: string }) => w.skill_name === skillName,
      )
    ) {
      return;
    }
    const latestForSkill = alerts
      .filter((a) => a.skill_name === skillName && a.action === "warned")
      .pop();
    if (!latestForSkill) return;
    showScanWarnModal(latestForSkill.findings || [], t);
  } catch {
    // best-effort; don't break the caller on failure
  }
}
