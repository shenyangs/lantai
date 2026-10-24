/**
 * Final step in the restore flow: lets the user pick full or custom restore mode,
 * choose which agents / config items to include, and confirm before applying.
 *
 * On open it fetches the backup detail (real agent list from workspace_stats).
 * If the detail fetch fails the modal shows an error and disables the OK button,
 * since we need workspace_stats to build the explicit agent_ids list.
 * A "confirmed" checkbox gates the submit button to prevent accidental data loss.
 */
import { useState, useEffect, useMemo, useRef } from "react";
import {
  Modal,
  Checkbox,
  Radio,
  Alert,
  Input,
  Tag,
  Divider,
  Typography,
  Tooltip,
  Space,
} from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import api from "@/api";
import { useAppMessage } from "@/hooks/useAppMessage";
import type {
  BackupMeta,
  BackupDetail,
  RestoreBackupRequest,
  RestoreOperationReceipt,
} from "@/api/types/backup";
import type { AgentSummary } from "@/api/types/agents";
import { isFullBackup } from "../shared/scope";
import RestoreAgentTable from "./RestoreAgentTable";
import styles from "./RestoreBackupModal.module.less";

const { Text } = Typography;

interface Props {
  open: boolean;
  backup: BackupMeta;
  agents: AgentSummary[];
  onClose: () => void;
  onSuccess: () => void;
}

type RestoreMode = "full" | "custom";
type RestorePhase = "editing" | "running" | "unknown" | "rejected";

interface SubmittedRestore {
  operationId: string;
  scopeSummary: string;
  request: RestoreBackupRequest;
}

function createRestoreOperationId(): string {
  const randomUUID = globalThis.crypto?.randomUUID?.bind(globalThis.crypto);
  if (randomUUID) return `restore-${randomUUID()}`;
  return `restore-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function receiptFromError(error: unknown): RestoreOperationReceipt | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (!detail || typeof detail !== "object") return null;
  const candidate = detail as Partial<RestoreOperationReceipt>;
  if (
    typeof candidate.operation_id !== "string" ||
    typeof candidate.backup_id !== "string" ||
    !["running", "applied", "rejected", "unknown"].includes(
      candidate.state ?? "",
    )
  ) {
    return null;
  }
  return candidate as RestoreOperationReceipt;
}

export default function RestoreBackupModal({
  open,
  backup,
  agents,
  onClose,
  onSuccess,
}: Props) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const completedRef = useRef(false);
  const [phase, setPhase] = useState<RestorePhase>("editing");
  const [checking, setChecking] = useState(false);
  const [submitted, setSubmitted] = useState<SubmittedRestore | null>(null);

  const fullBackup = isFullBackup(backup.scope);
  const [restoreMode, setRestoreMode] = useState<RestoreMode>(
    fullBackup ? "full" : "custom",
  );

  const [backupDetail, setBackupDetail] = useState<BackupDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailFailed, setDetailFailed] = useState(false);

  const [selectedAgents, setSelectedAgents] = useState<string[]>([]);
  const [globalConfig, setGlobalConfig] = useState(
    backup.scope.include_global_config,
  );
  const [includeSkillPool, setIncludeSkillPool] = useState(
    backup.scope.include_skill_pool,
  );
  const [includeSecrets, setIncludeSecrets] = useState(
    backup.scope.include_secrets,
  );
  const [defaultWorkspaceDir, setDefaultWorkspaceDir] = useState("");
  const [includeAgents, setIncludeAgents] = useState(
    backup.scope.include_agents,
  );
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => {
    if (!open) return;
    completedRef.current = false;
    setPhase("editing");
    setChecking(false);
    setSubmitted(null);
    setRestoreMode(fullBackup ? "full" : "custom");
    setGlobalConfig(backup.scope.include_global_config);
    setIncludeSkillPool(backup.scope.include_skill_pool);
    setIncludeSecrets(backup.scope.include_secrets);
    setIncludeAgents(backup.scope.include_agents);
    setDefaultWorkspaceDir("");
    setConfirmed(false);
    setDetailLoading(true);
    setDetailFailed(false);
    setBackupDetail(null);
    api
      .getBackup(backup.id)
      .then((detail) => {
        setBackupDetail(detail);
        setSelectedAgents(Object.keys(detail.workspace_stats));
      })
      .catch(() => {
        setDetailFailed(true);
        message.error(t("backup.detailLoadFailed"));
      })
      .finally(() => setDetailLoading(false));
    // keying on backup.id is intentional – re-fetch when a different backup is opened.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, backup.id, fullBackup]);

  const existingAgentMap = useMemo(
    () => new Map(agents.map((a) => [a.id, a])),
    [agents],
  );

  // All agent IDs present in the backup (only available after detail loads).
  const allBackupAgentIds = useMemo(
    () => (backupDetail ? Object.keys(backupDetail.workspace_stats) : []),
    [backupDetail],
  );

  const allAgentRows = useMemo(() => {
    return allBackupAgentIds.map((aid) => {
      const agentInfo = existingAgentMap.get(aid);
      // For new (not-yet-existing) agents we can't look up the name from
      // /api/agents, so fall back to the name embedded inside the backup's
      // workspace/agent.json (exposed via workspace_stats[aid].name).
      const backupName = backupDetail?.workspace_stats?.[aid]?.name;
      return {
        key: aid,
        aid,
        name: agentInfo?.name ?? backupName ?? aid,
        isExisting: !!agentInfo,
        currentWorkspaceDir: agentInfo?.workspace_dir ?? "",
      };
    });
  }, [allBackupAgentIds, existingAgentMap, backupDetail]);

  const newCount = useMemo(
    () => allAgentRows.filter((r) => !r.isExisting).length,
    [allAgentRows],
  );
  const hasNewAgents = newCount > 0;

  const selectedExistingCount = useMemo(
    () => selectedAgents.filter((id) => existingAgentMap.has(id)).length,
    [selectedAgents, existingAgentMap],
  );
  const selectedNewCount = useMemo(
    () => selectedAgents.filter((id) => !existingAgentMap.has(id)).length,
    [selectedAgents, existingAgentMap],
  );

  const finishApplied = () => {
    if (completedRef.current) return;
    completedRef.current = true;
    message.success(t("backup.restoreSuccess"));
    onSuccess();
    onClose();
  };

  const applyReceipt = (receipt: RestoreOperationReceipt) => {
    if (receipt.state === "applied") {
      finishApplied();
      return;
    }
    setPhase(receipt.state === "rejected" ? "rejected" : "unknown");
  };

  const verifySubmittedRestore = async (operationId: string) => {
    setChecking(true);
    try {
      const receipt = await api.getRestoreOperation(operationId);
      applyReceipt(receipt);
    } catch {
      // A failed status lookup is not evidence that the restore did not run.
      setPhase("unknown");
    } finally {
      setChecking(false);
    }
  };

  const handleOk = async () => {
    if (submitted) {
      await verifySubmittedRestore(submitted.operationId);
      return;
    }

    const isFull = restoreMode === "full";
    const doIncludeAgents = isFull ? true : includeAgents;
    const agentIds = isFull
      ? allBackupAgentIds
      : includeAgents
        ? selectedAgents
        : [];
    const request: RestoreBackupRequest = {
      operation_id: createRestoreOperationId(),
      mode: restoreMode,
      include_agents: doIncludeAgents,
      agent_ids: [...agentIds],
      include_global_config: isFull ? true : globalConfig,
      include_secrets: isFull ? true : includeSecrets,
      include_skill_pool: isFull ? true : includeSkillPool,
      default_workspace_dir: defaultWorkspaceDir.trim() || null,
    };
    const scopeParts: string[] = [];
    if (request.include_agents) {
      scopeParts.push(`${request.agent_ids.length} 个智能体`);
    }
    if (request.include_global_config) scopeParts.push("全局设置");
    if (request.include_skill_pool) scopeParts.push("技能库");
    if (request.include_secrets) scopeParts.push("密钥");

    const operationId = request.operation_id!;
    setSubmitted({
      operationId,
      scopeSummary: scopeParts.join("、") || "未包含可恢复内容",
      request,
    });
    setPhase("running");

    try {
      const receipt = await api.restoreBackup(backup.id, request);
      applyReceipt(receipt);
    } catch (error) {
      const receipt = receiptFromError(error);
      if (receipt) {
        applyReceipt(receipt);
      } else {
        // The request may have reached the server even though its response was
        // lost. Reconcile the same operation ID; never submit a second restore.
        await verifySubmittedRestore(operationId);
      }
    }
  };

  const summaryText =
    restoreMode === "custom" &&
    (selectedExistingCount > 0 || selectedNewCount > 0)
      ? t("backup.restoreCustomSummary", {
          existing: selectedExistingCount,
          added: selectedNewCount,
        })
      : null;

  // OK is disabled when detail hasn't loaded (we need workspace_stats to build agent_ids),
  // or when the user hasn't confirmed the destructive action.
  const okDisabled =
    submitted === null
      ? !confirmed || detailFailed || (detailLoading && !backupDetail)
      : phase === "running";

  return (
    <Modal
      title={t("backup.restoreTitle")}
      open={open}
      onCancel={submitted ? undefined : onClose}
      onOk={handleOk}
      confirmLoading={checking}
      okButtonProps={{ disabled: okDisabled, danger: true }}
      cancelButtonProps={{ disabled: submitted !== null }}
      okText={
        submitted
          ? phase === "running"
            ? "恢复进行中"
            : "核实恢复结果"
          : restoreMode === "full"
            ? "开始整体恢复"
            : "恢复所选内容"
      }
      cancelText={t("common.cancel")}
      closable={!submitted}
      maskClosable={!submitted}
      keyboard={!submitted}
      destroyOnHidden
      centered
      width={680}
    >
      <div className={styles.modalBody}>
        <div className={styles.backupInfoSection}>
          <Text strong style={{ fontSize: 14 }}>
            {backup.name}
          </Text>
          {backup.description && (
            <div className={styles.backupDescription}>{backup.description}</div>
          )}
        </div>

        <section
          className={styles.restoreImpactSummary}
          aria-label={submitted ? "已提交恢复范围" : "本次恢复影响"}
          role={submitted ? "status" : undefined}
        >
          <div>
            <span>{submitted ? "已提交" : "将恢复"}</span>
            <strong>
              {submitted
                ? submitted.scopeSummary
                : restoreMode === "full"
                  ? `${allBackupAgentIds.length} 个智能体及整套设置`
                  : `${includeAgents ? selectedAgents.length : 0} 个智能体${globalConfig ? "、全局设置" : ""}${includeSkillPool ? "、技能库" : ""}${includeSecrets ? "、密钥" : ""}`}
            </strong>
          </div>
          <div>
            <span>覆盖方式</span>
            <strong>
              {backup.scope.sanitized
                ? "不恢复密钥目录与已识别的凭据；已识别的本机凭据会原样保留"
                : restoreMode === "full"
                  ? "当前注册表、设置、技能库与密钥会被备份内容替换"
                  : "只覆盖勾选对象；未选择的本机对象继续保留"}
            </strong>
          </div>
          <div>
            <span>{submitted ? "操作编号" : "回退路径"}</span>
            <strong>
              {submitted
                ? submitted.operationId
                : "如上一步创建了完整本机灾备，可从备份列表恢复到当前状态"}
            </strong>
          </div>
        </section>

        {submitted && (
          <Alert
            type={phase === "rejected" ? "error" : "warning"}
            showIcon
            message={
              phase === "running"
                ? "恢复请求已提交，正在等待权威结果。请不要重复提交。"
                : phase === "rejected"
                  ? "服务端已记录恢复异常；部分内容可能已改变。请先核实当前状态，不要盲目重试。"
                  : "恢复结果待核实：响应或查询丢失不代表恢复未生效。请用下方按钮查询同一操作，不要重新恢复。"
            }
            className={styles.fullRestoreAlert}
          />
        )}

        {!submitted && (
          <>

        {detailFailed && (
          <Alert
            type="error"
            showIcon
            message={t("backup.detailLoadFailed")}
            className={styles.fullRestoreAlert}
          />
        )}

        {backupDetail?.integrity_status === "verified" && (
          <Alert
            type="success"
            showIcon
            message={t("backup.integrityVerified", {
              count: backupDetail.integrity_file_count ?? 0,
              size: new Intl.NumberFormat(undefined, {
                style: "unit",
                unit: "byte",
                unitDisplay: "narrow",
              }).format(backupDetail.integrity_total_bytes ?? 0),
            })}
            className={styles.fullRestoreAlert}
          />
        )}

        {backupDetail?.integrity_status === "legacy_unverified" && (
          <Alert
            type="warning"
            showIcon
            message={t("backup.integrityLegacy")}
            className={styles.fullRestoreAlert}
          />
        )}

        {hasNewAgents && !detailFailed && (
          <div className={styles.workspaceDirSection}>
            <div className={styles.workspaceDirLabel}>
              {t("backup.defaultWorkspaceDir")}
              <Tooltip title={t("backup.defaultWorkspaceDirHint")}>
                <QuestionCircleOutlined className={styles.hintIcon} />
              </Tooltip>
            </div>
            <Input
              value={defaultWorkspaceDir}
              onChange={(e) => setDefaultWorkspaceDir(e.target.value)}
              placeholder={t("backup.defaultWorkspaceDirPlaceholder")}
            />
          </div>
        )}

        <Divider className={styles.dividerTop} />

        <div className={styles.restoreModeSection}>
          <div className={styles.restoreModeLabel}>
            {t("backup.restoreMode")}
          </div>
          <Radio.Group
            value={restoreMode}
            onChange={(e) => setRestoreMode(e.target.value)}
            className={styles.radioGroup}
          >
            <Radio value="full" disabled={!fullBackup}>
              <div className={styles.radioOption}>
                <div className={styles.radioOptionHeader}>
                  <Text strong>{t("backup.restoreModeFull")}</Text>
                  {!fullBackup && (
                    <Tag color="default" className={styles.radioDisabledTag}>
                      {t("backup.restoreModeFullDisabled")}
                    </Tag>
                  )}
                </div>
                <Text type="secondary" className={styles.radioDesc}>
                  {t("backup.restoreModeFullDesc")}
                </Text>
              </div>
            </Radio>
            <Radio value="custom">
              <div className={styles.radioOption}>
                <Text strong>{t("backup.restoreModeCustom")}</Text>
                <Text type="secondary" className={styles.radioDesc}>
                  {t("backup.restoreModeCustomDesc")}
                </Text>
              </div>
            </Radio>
          </Radio.Group>
        </div>

        {restoreMode === "full" && (
          <Alert
            type="warning"
            showIcon
            message={t("backup.restoreFullWarning")}
            className={styles.fullRestoreAlert}
          />
        )}

        {restoreMode === "custom" && (
          <Space direction="vertical" size={0} className={styles.customOptions}>
            {backup.scope.include_agents && (
              <RestoreAgentTable
                allAgentRows={allAgentRows}
                selectedAgents={selectedAgents}
                onSelectionChange={setSelectedAgents}
                detailLoading={detailLoading}
                defaultWorkspaceDir={defaultWorkspaceDir}
                includeAgents={includeAgents}
                onIncludeAgentsChange={setIncludeAgents}
                summaryText={summaryText}
              />
            )}

            {backup.scope.include_global_config && (
              <div className={styles.checkboxRow}>
                <Checkbox
                  checked={globalConfig}
                  onChange={(e) => setGlobalConfig(e.target.checked)}
                >
                  {t("backup.scopeGlobalConfig")}
                </Checkbox>
              </div>
            )}

            {backup.scope.include_skill_pool && (
              <div className={styles.checkboxRow}>
                <Checkbox
                  checked={includeSkillPool}
                  onChange={(e) => setIncludeSkillPool(e.target.checked)}
                >
                  {t("backup.scopeSkillPool")}
                </Checkbox>
              </div>
            )}

            {backup.scope.include_secrets && (
              <div className={styles.checkboxRow}>
                <Checkbox
                  checked={includeSecrets}
                  onChange={(e) => setIncludeSecrets(e.target.checked)}
                >
                  {t("backup.scopeSecrets")}
                </Checkbox>
                <div className={styles.secretsHint}>
                  {t("backup.scopeSecretsHint")}
                </div>
              </div>
            )}
          </Space>
        )}

        <Divider className={styles.dividerBottom} />

        {restoreMode === "custom" && (
          <Alert
            type="warning"
            showIcon
            message={
              <ul className={styles.restoreWarningList}>
                <li>{t("backup.restoreWarningModify")}</li>
                <li>{t("backup.restoreWarningRestart")}</li>
              </ul>
            }
            className={styles.customRestoreAlert}
          />
        )}

        <Checkbox
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
        >
          {t("backup.restoreConfirm")}
        </Checkbox>
          </>
        )}
      </div>
    </Modal>
  );
}
