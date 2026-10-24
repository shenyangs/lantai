import { useState, useEffect, useCallback } from "react";
import { Button, Modal, Tag, Typography } from "antd";
import { Shield, Check, X, Clock, Copy } from "lucide-react";
import { useTranslation } from "react-i18next";
import styles from "./ApprovalCard.module.less";

const { Text } = Typography;

type ApprovalScope = "once" | "session" | "workspace";
type ApprovalLoading =
  | "approve_once"
  | "approve_session"
  | "approve_workspace"
  | "deny"
  | null;

export interface ApprovalCardProps {
  requestId: string;
  requestCount?: number;
  toolName: string;
  severity: string;
  findingsCount: number;
  findingsSummary: string;
  toolParams: Record<string, unknown>;
  createdAt: number;
  timeoutSeconds: number | null;
  sessionId?: string;
  rootSessionId?: string;
  policyDecision?: Record<string, unknown> | null;
  approvalOptions?: string[];
  sessionRememberable?: boolean;
  rememberable?: boolean;
  permissionScope?: string | null;
  sourceRunId?: string | null;
  sourceAgentName?: string | null;
  keyboardActive?: boolean;
  onApprove: (requestId: string, scope?: ApprovalScope) => Promise<void>;
  onDeny: (requestId: string) => Promise<void>;
  onCancel?: () => void;
  onViewRules?: () => void;
}

export function ApprovalCard({
  requestId,
  requestCount = 1,
  toolName,
  severity,
  findingsCount,
  findingsSummary,
  toolParams,
  createdAt,
  timeoutSeconds,
  sessionId,
  rootSessionId,
  policyDecision,
  approvalOptions = [],
  sessionRememberable = false,
  rememberable = false,
  permissionScope,
  sourceRunId,
  sourceAgentName,
  keyboardActive = false,
  onApprove,
  onDeny,
  onCancel,
  onViewRules,
}: ApprovalCardProps) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState<ApprovalLoading>(null);
  const hasTimeout =
    typeof timeoutSeconds === "number" &&
    Number.isFinite(timeoutSeconds) &&
    timeoutSeconds > 0;
  const [remaining, setRemaining] = useState<number | null>(
    hasTimeout ? timeoutSeconds : null,
  );
  const [copiedField, setCopiedField] = useState<string | null>(null);
  const [actionError, setActionError] = useState("");
  const isDecisionExpired = useCallback(() => {
    if (!hasTimeout || timeoutSeconds === null) return false;
    const secondsLeft = timeoutSeconds - (Date.now() / 1000 - createdAt);
    return Math.floor(secondsLeft) <= 0;
  }, [createdAt, hasTimeout, timeoutSeconds]);
  const decisionExpired = remaining !== null && remaining <= 0;

  const handleCopy = useCallback(async (text: string, field: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setActionError("");
      setCopiedField(field);
      setTimeout(() => setCopiedField(null), 1500);
    } catch {
      setActionError("复制失败。请手动选择文本复制，或检查系统剪贴板权限。");
    }
  }, []);

  // Check if this is a cross-session approval
  const isCrossSession =
    sessionId && rootSessionId && sessionId !== rootSessionId;
  const showSource =
    Boolean(isCrossSession) ||
    Boolean(sourceAgentName && sourceAgentName !== "default");

  useEffect(() => {
    if (!hasTimeout || timeoutSeconds === null) {
      setRemaining(null);
      return;
    }
    const elapsed = Date.now() / 1000 - createdAt;
    const initialRemaining = Math.max(0, Math.floor(timeoutSeconds - elapsed));
    setRemaining(initialRemaining);

    const timer = setInterval(() => {
      const newElapsed = Date.now() / 1000 - createdAt;
      const newRemaining = Math.max(0, Math.floor(timeoutSeconds - newElapsed));
      setRemaining(newRemaining);

      if (newRemaining <= 0) {
        clearInterval(timer);
      }
    }, 1000);

    return () => clearInterval(timer);
  }, [createdAt, hasTimeout, timeoutSeconds]);

  const handleApprove = useCallback(
    async (scope: ApprovalScope) => {
      if (loading !== null || isDecisionExpired()) {
        setRemaining(0);
        setActionError("这项审批已过期，不能再提交决定。请等待任务刷新状态。");
        return;
      }
      if (scope === "workspace") {
        const confirmed = await new Promise<boolean>((resolve) => {
          Modal.confirm({
            transitionName: "",
            maskTransitionName: "",
            title: "在此工作区始终允许？",
            content: (
              <div style={{ display: "grid", gap: 8 }}>
                <div>
                  工具：<strong>{toolName}</strong>
                </div>
                <div>
                  这个授权会持续应用于当前工作区，后续匹配请求可能不再逐次询问。
                </div>
                <div>你可以随时到“设置 → 权限”查看或撤销这条规则。</div>
              </div>
            ),
            okText: "确认始终允许",
            cancelText: "返回检查",
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!confirmed) return;
        if (isDecisionExpired()) {
          setRemaining(0);
          setActionError(
            "这项审批已过期，不能再提交决定。请等待任务刷新状态。",
          );
          return;
        }
      }
      setLoading(`approve_${scope}` as Exclude<ApprovalLoading, null>);
      try {
        await onApprove(requestId, scope);
        setActionError("");
      } catch (err) {
        console.error("[ApprovalCard] onApprove failed:", err);
        setActionError("审批结果尚未确认；请等待审批状态刷新，不要重复提交。");
      } finally {
        setLoading(null);
      }
    },
    [isDecisionExpired, loading, onApprove, requestId, toolName],
  );

  const handleDeny = useCallback(async () => {
    if (loading !== null || isDecisionExpired()) {
      setRemaining(0);
      setActionError("这项审批已过期，不能再提交决定。请等待任务刷新状态。");
      return;
    }
    setLoading("deny");
    try {
      await onDeny(requestId);
      setActionError("");
    } catch (err) {
      console.error("[ApprovalCard] onDeny failed:", err);
      setActionError("拒绝结果尚未确认；请等待审批状态刷新，不要重复提交。");
    } finally {
      setLoading(null);
    }
  }, [isDecisionExpired, loading, onDeny, requestId]);

  const getSeverityColor = (sev: string) => {
    const s = sev.toLowerCase();
    if (s === "critical" || s === "high") return "error";
    if (s === "medium") return "warning";
    return "default";
  };
  const getSeverityLabel = (sev: string) => {
    const normalized = sev.toLowerCase();
    if (normalized === "critical") return "严重风险";
    if (normalized === "high") return "高风险";
    if (normalized === "medium") return "中风险";
    if (normalized === "low") return "低风险";
    return "需确认";
  };

  const riskLevel = String(
    policyDecision?.risk_level || policyDecision?.risk || severity,
  );
  const matchedRules = Array.isArray(policyDecision?.matched_rules)
    ? policyDecision.matched_rules
    : Array.isArray(policyDecision?.matched_rule_ids)
      ? policyDecision.matched_rule_ids
      : policyDecision?.matched_rule_id
        ? [policyDecision.matched_rule_id]
        : [];
  const approvalCopy = String(
    policyDecision?.approval_copy ||
      policyDecision?.suggested_approval_message ||
      policyDecision?.reason ||
      "",
  ).trim();
  const permissionMode = String(policyDecision?.permission_mode || "").trim();
  const commandCategory = String(policyDecision?.command_category || "").trim();
  const commandHead = String(policyDecision?.command_head || "").trim();
  const decisionSource = String(policyDecision?.decision_source || "").trim();
  const effectiveRuleId = String(
    policyDecision?.effective_rule_id || "",
  ).trim();
  const canRemember = Boolean(rememberable);
  const optionAllowsSession =
    approvalOptions.length === 0 || approvalOptions.includes("allow_session");
  const optionAllowsWorkspace =
    approvalOptions.length === 0 || approvalOptions.includes("allow_workspace");
  const canAllowSession = sessionRememberable && optionAllowsSession;
  const canAllowWorkspace = canRemember && optionAllowsWorkspace;
  const workspaceEscape = Boolean(policyDecision?.workspace_escape);
  const rememberUnavailableReason = workspaceEscape
    ? "会写入或修改工作区之外的路径，只能逐次确认。"
    : canAllowSession
      ? "该授权可在本次会话复用，但不会永久保存。"
      : "该规则需要逐次确认，不能记住到会话或工作区。";
  const showRememberNotice =
    (!canRemember || !sessionRememberable) &&
    (workspaceEscape || optionAllowsSession || optionAllowsWorkspace);

  useEffect(() => {
    if (!keyboardActive) {
      return;
    }
    const handleShortcut = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.repeat ||
        event.isComposing ||
        loading !== null ||
        (remaining !== null && remaining <= 0)
      ) {
        return;
      }
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        target.closest(
          "input, textarea, select, button, a, [contenteditable='true'], [role='textbox']",
        )
      ) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        void handleDeny();
        return;
      }
      if (event.key !== "Enter") {
        return;
      }
      event.preventDefault();
      if ((event.metaKey || event.ctrlKey) && canAllowSession) {
        void handleApprove("session");
        return;
      }
      if (
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey &&
        !event.shiftKey
      ) {
        void handleApprove("once");
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [
    canAllowSession,
    handleApprove,
    handleDeny,
    keyboardActive,
    loading,
    remaining,
  ]);

  return (
    <article
      className={styles.approvalCard}
      aria-labelledby={`approval-title-${requestId}`}
    >
      <div className={styles.header}>
        <div className={styles.titleRow}>
          <span className={styles.iconBadge}>
            <Shield size={16} className={styles.icon} />
          </span>
          <span className={styles.titleStack}>
            <Text id={`approval-title-${requestId}`} className={styles.title}>
              {t("approval.title", "Security Approval Required")}
            </Text>
            <Text className={styles.subtitle}>{toolName}</Text>
          </span>
          {requestCount > 1 ? (
            <Tag color="blue" className={styles.crossSessionTag}>
              {requestCount} 个请求
            </Tag>
          ) : null}
        </div>
        {remaining === null ? (
          <div className={styles.timer} aria-label="等待你的决定">
            <Clock size={14} className={styles.timerIcon} />
            <Text className={styles.timerText}>等待你的决定</Text>
          </div>
        ) : (
          <div className={styles.timer} aria-label={`剩余 ${remaining} 秒`}>
            <Clock size={14} className={styles.timerIcon} />
            <Text className={styles.timerText}>
              {Math.floor(remaining / 60)}:
              {String(remaining % 60).padStart(2, "0")}
            </Text>
          </div>
        )}
      </div>

      <div className={styles.content}>
        {(approvalCopy || findingsSummary) && (
          <div className={styles.summaryBox}>
            <Text className={styles.summaryText}>
              {approvalCopy || findingsSummary}
            </Text>
            <button
              type="button"
              className={`${styles.copyButton} ${
                copiedField === "summary" ? styles.copied : ""
              }`}
              onClick={() =>
                handleCopy(approvalCopy || findingsSummary, "summary")
              }
              title={t("common.copy", "Copy")}
              aria-label={t("common.copy", "Copy")}
            >
              <Copy size={12} />
            </button>
          </div>
        )}

        <div className={styles.metaRow}>
          <Text className={styles.toolName} code>
            {toolName}
          </Text>
          <Tag
            color={getSeverityColor(riskLevel)}
            className={styles.severityTag}
            data-severity={riskLevel.toLowerCase()}
          >
            {getSeverityLabel(riskLevel)}
          </Tag>
          {findingsCount > 1 ? (
            <Text className={styles.findingsCount}>{findingsCount} 项风险</Text>
          ) : null}
          {showSource ? (
            <Tag color="blue" className={styles.crossSessionTag}>
              {sourceAgentName || t("approval.subSession", "Sub-Agent")}
              {sourceRunId ? ` · ${sourceRunId.slice(0, 8)}` : ""}
              {isCrossSession ? ` (${sessionId?.slice(0, 8)})` : ""}
            </Tag>
          ) : null}
        </div>

        {matchedRules.length > 0 ||
        permissionScope ||
        permissionMode ||
        commandCategory ||
        commandHead ||
        decisionSource ? (
          <details className={styles.policyDetails}>
            <summary>权限依据</summary>
            {matchedRules.length > 0 || permissionScope ? (
              <div className={styles.infoRow}>
                <Text className={styles.label}>规则</Text>
                <Text className={styles.value}>
                  {matchedRules.length > 0
                    ? matchedRules.join(", ")
                    : "默认策略"}
                  {permissionScope ? ` · 建议范围 ${permissionScope}` : ""}
                </Text>
              </div>
            ) : null}
            {permissionMode ||
            commandCategory ||
            commandHead ||
            decisionSource ? (
              <div className={styles.infoRow}>
                <Text className={styles.label}>策略</Text>
                <Text className={styles.value}>
                  {[
                    permissionMode ? `模式 ${permissionMode}` : "",
                    commandCategory ? `类别 ${commandCategory}` : "",
                    commandHead ? `命令 ${commandHead}` : "",
                    decisionSource ? `来源 ${decisionSource}` : "",
                    effectiveRuleId ? `规则 ${effectiveRuleId}` : "",
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </Text>
              </div>
            ) : null}
          </details>
        ) : null}

        {toolParams && Object.keys(toolParams).length > 0 && (
          <details className={styles.paramsDetails}>
            <summary className={styles.paramsSummary}>
              {t("approval.parameters", "Parameters")}
            </summary>
            <div className={styles.paramsCodeWrapper}>
              <pre className={styles.paramsCode}>
                {JSON.stringify(toolParams, null, 2)}
              </pre>
              <button
                type="button"
                className={`${styles.copyButton} ${
                  copiedField === "params" ? styles.copied : ""
                }`}
                onClick={() =>
                  handleCopy(JSON.stringify(toolParams, null, 2), "params")
                }
                title={t("common.copy", "Copy")}
                aria-label={t("common.copy", "Copy")}
              >
                <Copy size={12} />
              </button>
            </div>
          </details>
        )}

        {showRememberNotice ? (
          <Text className={styles.rememberNotice}>
            {rememberUnavailableReason}
          </Text>
        ) : null}
      </div>

      <div className={styles.actions}>
        {actionError ? (
          <Text type="danger" role="alert">
            {actionError}
          </Text>
        ) : null}
        <div className={`${styles.actionGroup} ${styles.utilityActions}`}>
          {onCancel && (
            <Button
              type="default"
              className={`${styles.secondaryButton} ${styles.cancelTaskButton}`}
              aria-label="取消整个任务"
              onClick={() =>
                Modal.confirm({
                  transitionName: "",
                  maskTransitionName: "",
                  title: "取消整个任务？",
                  content:
                    "任务当前已暂停等待审批。取消后，尚未完成的步骤和本轮进度将终止。",
                  okText: "取消任务",
                  cancelText: "继续等待",
                  okButtonProps: { danger: true },
                  onOk: onCancel,
                })
              }
              disabled={loading !== null}
            >
              {t("approval.cancelTask", "Cancel Task")}
            </Button>
          )}
          <Button
            danger
            className={styles.denyButton}
            icon={<X size={14} />}
            onClick={handleDeny}
            loading={loading === "deny"}
            disabled={loading !== null || decisionExpired}
          >
            <span>{t("approval.deny", "Deny")}</span>
            {keyboardActive ? <kbd className={styles.keyHint}>Esc</kbd> : null}
          </Button>
          {onViewRules ? (
            <Button
              type="default"
              className={styles.secondaryButton}
              onClick={onViewRules}
              disabled={loading !== null}
            >
              查看规则
            </Button>
          ) : null}
        </div>
        <div className={`${styles.actionGroup} ${styles.scopeActions}`}>
          {canAllowWorkspace ? (
            <Button
              type="default"
              className={styles.scopeButton}
              icon={<Check size={14} />}
              onClick={() => handleApprove("workspace")}
              loading={loading === "approve_workspace"}
              disabled={loading !== null || decisionExpired}
            >
              始终允许
            </Button>
          ) : null}
          {canAllowSession ? (
            <Button
              type="default"
              className={styles.scopeButton}
              icon={<Check size={14} />}
              onClick={() => handleApprove("session")}
              loading={loading === "approve_session"}
              disabled={loading !== null || decisionExpired}
            >
              <span>本次会话允许</span>
              {keyboardActive ? <kbd className={styles.keyHint}>⌘↵</kbd> : null}
            </Button>
          ) : null}
          <Button
            type="primary"
            className={styles.approveButton}
            icon={<Check size={14} />}
            onClick={() => handleApprove("once")}
            loading={loading === "approve_once"}
            disabled={loading !== null || decisionExpired}
          >
            <span>允许一次</span>
            {keyboardActive ? <kbd className={styles.keyHint}>↵</kbd> : null}
          </Button>
        </div>
      </div>
    </article>
  );
}
