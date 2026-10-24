/**
 * User-initiated backup modal: shows a form (name, description, scope) and,
 * once the user confirms, transitions to a progress view via useBackupRunner.
 * Does NOT handle the silent pre-restore case — see SilentBackupModal for that.
 */
import { useEffect, useRef, useState } from "react";
import { Modal, Input, Alert, Button, Space } from "antd";
import { useTranslation } from "react-i18next";
import dayjs from "dayjs";
import type { AgentSummary } from "@/api/types/agents";
import type { BackupEstimate } from "@/api/types/backup";
import { backupApi } from "@/api/modules/backup";
import { useBackupRunner } from "../shared/useBackupRunner";
import { buildScope, defaultCreateScope } from "../shared/scope";
import BackupProgress from "./BackupProgress";
import BackupScopeForm from "./BackupScopeForm";
import type { ScopeFormValue } from "./BackupScopeForm";
import styles from "./CreateBackupModal.module.less";

interface Props {
  open: boolean;
  agents: AgentSummary[];
  onClose: () => void;
  onSuccess: () => void;
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(value) / Math.log(1024)),
  );
  return `${(value / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export default function CreateBackupModal({
  open,
  agents,
  onClose,
  onSuccess,
}: Props) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scope, setScope] = useState<ScopeFormValue>(
    defaultCreateScope(agents.map((a) => a.id)),
  );
  const [estimate, setEstimate] = useState<BackupEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  const estimateGenerationRef = useRef(0);

  const runner = useBackupRunner({ onSuccess, onClose });

  /** Resets form and runner state each time the modal opens for a fresh session. */
  const handleAfterOpenChange = (visible: boolean) => {
    if (visible && !runner.active) {
      setName(`Backup ${dayjs().format("YYYY-MM-DD HH:mm")}`);
      setDescription("");
      setScope(defaultCreateScope(agents.map((a) => a.id)));
      setEstimate(null);
      runner.reset();
    }
  };

  useEffect(() => {
    if (!open || runner.active) return;
    const generation = ++estimateGenerationRef.current;
    const timer = window.setTimeout(() => {
      const built = buildScope(
        scope.backupMode,
        scope.selectedAgents,
        scope.globalConfig,
        scope.includeSkillPool,
        scope.includeSecrets,
        agents.map((agent) => agent.id),
      );
      setEstimating(true);
      void backupApi
        .estimateBackup({
          name: "scope-estimate",
          scope: built.scope,
          agents: built.agents,
        })
        .then((next) => {
          if (estimateGenerationRef.current === generation) setEstimate(next);
        })
        .catch(() => {
          if (estimateGenerationRef.current === generation) setEstimate(null);
        })
        .finally(() => {
          if (estimateGenerationRef.current === generation)
            setEstimating(false);
        });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [agents, open, runner.active, scope]);

  /** Validates the name then hands off to useBackupRunner to start the stream. */
  const handleOk = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    const { scope: backupScope, agents: backupAgentIds } = buildScope(
      scope.backupMode,
      scope.selectedAgents,
      scope.globalConfig,
      scope.includeSkillPool,
      scope.includeSecrets,
      agents.map((agent) => agent.id),
    );
    runner.start({
      name: trimmed,
      description: description.trim() || undefined,
      scope: backupScope,
      agents: backupAgentIds,
    });
  };

  const handleVerifyAndClose = () => {
    onSuccess();
    runner.reset();
    onClose();
  };

  const activeFooter =
    runner.phase === "running" ? (
      <Space>
        <Button onClick={runner.closeObservation}>关闭进度</Button>
        <Button danger onClick={runner.requestStop}>
          停止备份
        </Button>
      </Space>
    ) : runner.phase === "stop_requested" ? (
      <Button type="primary" onClick={handleVerifyAndClose}>
        刷新列表并关闭
      </Button>
    ) : undefined;

  return (
    <Modal
      title={t("backup.createTitle")}
      open={open}
      onCancel={runner.active ? undefined : onClose}
      onOk={runner.active ? undefined : handleOk}
      footer={activeFooter}
      okButtonProps={
        runner.loading
          ? { style: { display: "none" } }
          : { disabled: !name.trim() }
      }
      cancelText={t("common.cancel")}
      okText={scope.backupMode === "full" ? "创建本机灾备" : "创建脱敏导出"}
      destroyOnHidden
      afterOpenChange={handleAfterOpenChange}
      centered
      closable={!runner.active}
      maskClosable={!runner.active}
      keyboard={!runner.active}
    >
      {runner.active ? (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <BackupProgress
            progress={runner.progress}
            progressMsg={runner.progressMsg}
          />
          {runner.phase === "running" ? (
            <Alert
              type="info"
              showIcon
              message="关闭进度不会停止备份"
              description="之后重新打开创建备份窗口，会回到这个任务的当前进度。"
            />
          ) : (
            <Alert
              type="warning"
              showIcon
              message="停止结果尚未确认"
              description="进度连接已中断，无法从本窗口判断备份是否在最终写入前停止。请刷新备份列表，核实是否已出现该名称，再决定是否重建。"
            />
          )}
        </Space>
      ) : (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <div>
            <div className={styles.fieldLabel}>{t("backup.name")}</div>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("backup.namePlaceholder")}
            />
          </div>

          <div>
            <div className={styles.fieldLabel}>
              {t("backup.descriptionLabel")}
            </div>
            <Input.TextArea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("backup.descriptionPlaceholder")}
              rows={2}
            />
          </div>

          <BackupScopeForm value={scope} onChange={setScope} agents={agents} />

          <section
            className={styles.scopeSummary}
            aria-label="备份范围与恢复后果"
          >
            <div>
              <span>用途</span>
              <strong>
                {scope.backupMode === "full"
                  ? "本机故障恢复"
                  : "迁移或安全交接"}
              </strong>
              <small>
                {scope.backupMode === "full"
                  ? "包含密钥与凭据，只应保存在你控制的设备中。"
                  : "固定排除密钥目录并处理常见凭据字段；分享前仍需检查自建资料和自由文本。"}
              </small>
            </div>
            <div>
              <span>预计待打包大小</span>
              <strong>
                {estimating
                  ? "正在计算…"
                  : estimate
                    ? `${formatBytes(estimate.bytes)} · ${estimate.files} 个文件`
                    : "暂时无法计算"}
              </strong>
              <small>实际压缩包大小会因文件内容而变化。</small>
            </div>
            <div>
              <span>恢复后果</span>
              <strong>
                {scope.backupMode === "full"
                  ? "可执行整体替换"
                  : "按所选对象恢复"}
              </strong>
              <small>
                恢复前会再列出将覆盖和新增的对象，并可先创建回退快照。
              </small>
            </div>
          </section>

          <Alert type="info" showIcon message={t("backup.localModelsNotice")} />
          {scope.backupMode === "full" ? (
            <Alert
              type="warning"
              showIcon
              message={t("backup.securityNotice")}
            />
          ) : null}
        </Space>
      )}
    </Modal>
  );
}
