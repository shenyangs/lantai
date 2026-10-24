import { useEffect } from "react";
import { Alert, Button, Modal, Space } from "antd";
import { useTranslation } from "react-i18next";
import type { BackupMeta } from "@/api/types/backup";
import { useBackupRunner } from "../shared/useBackupRunner";
import { buildPreRestoreScope } from "../shared/scope";
import BackupProgress from "./BackupProgress";

interface Props {
  /** The backup being restored. When non-null the modal opens and auto-starts. */
  target: BackupMeta | null;
  /** All currently known agent IDs; passed explicitly to buildPreRestoreScope. */
  agentIds: string[];
  onClose: () => void;
  onSuccess: () => void;
}

/**
 * Automatic pre-restore snapshot modal.
 * Opens when target is set, immediately starts a full backup, shows only a
 * progress bar and a Cancel button. No user form input required.
 */
export default function SilentBackupModal({
  target,
  agentIds,
  onClose,
  onSuccess,
}: Props) {
  const { t } = useTranslation();
  const runner = useBackupRunner({ onSuccess, onClose });

  useEffect(() => {
    if (!target) return;
    const { name, description, scope, agents } = buildPreRestoreScope(agentIds);
    runner.start({
      name,
      description: t(description),
      scope,
      agents,
    });
    // runner.start is stable (doesn't change between renders)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  const handleReturnToRestore = () => {
    runner.reset();
    onClose();
  };

  return (
    <Modal
      title={t("backup.creatingPreRestoreBackup")}
      open={target !== null}
      onCancel={undefined}
      footer={
        runner.phase === "running" ? (
          <Button danger onClick={runner.requestStop}>
            停止备份
          </Button>
        ) : runner.phase === "stop_requested" ? (
          <Button type="primary" onClick={handleReturnToRestore}>
            返回恢复选择
          </Button>
        ) : null
      }
      destroyOnHidden
      centered
      closable={false}
      maskClosable={false}
      keyboard={false}
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <BackupProgress
          progress={runner.progress}
          progressMsg={runner.progressMsg}
        />
        {runner.phase === "stop_requested" ? (
          <Alert
            type="warning"
            showIcon
            message="恢复前快照的停止结果尚未确认"
            description="不会继续执行恢复。请返回列表核实快照是否已生成，再重新选择恢复操作。"
          />
        ) : (
          <Alert
            type="info"
            showIcon
            message="这个快照是继续恢复的前置保护"
            description="停止快照后不会自动继续恢复。"
          />
        )}
      </Space>
    </Modal>
  );
}
