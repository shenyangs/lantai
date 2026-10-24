import { Button, Empty, Popconfirm, Tooltip } from "antd";
import {
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  FolderOpenOutlined,
  SyncOutlined,
  SwapOutlined,
} from "@ant-design/icons";
import { EyeOff, Eye } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { AgentSummary } from "../../../../api/types/agents";
import { getAgentDisplayName } from "../../../../utils/agentDisplayName";
import { ProviderIcon } from "../../Models/components/ProviderIconComponent";
import { BeverageAgentAvatar } from "../../../../components/BeverageAgentAvatar";
import styles from "../index.module.less";

interface AgentCardGridProps {
  agents: AgentSummary[];
  loading: boolean;
  onEdit: (agent: AgentSummary) => void;
  onDelete: (agentId: string) => void;
  onToggle: (agentId: string, currentEnabled: boolean) => void;
  onSyncModel: (agentId: string) => void;
  onSyncAllModels: () => void;
  syncAllLoading?: boolean;
  canSyncAllModels?: boolean;
  defaultAgentModel?: { provider_id: string; model: string } | null;
}

function compactDescription(text: string): string {
  if (!text) return "";
  const pipeIdx = text.indexOf(" | ");
  if (pipeIdx !== -1) return text.slice(0, pipeIdx).trim();
  const nlIdx = text.indexOf("\n");
  if (nlIdx !== -1) return text.slice(0, nlIdx).trim();
  const cnPeriodIdx = text.indexOf("。");
  if (cnPeriodIdx !== -1) return text.slice(0, cnPeriodIdx + 1).trim();
  const enPeriodIdx = text.indexOf(". ");
  if (enPeriodIdx !== -1) return text.slice(0, enPeriodIdx + 1).trim();
  return text.length > 92 ? `${text.slice(0, 92)}...` : text;
}

function compactWorkspace(path: string): string {
  if (!path) return "未绑定工作区";
  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length <= 2) return normalized;
  return `.../${parts.slice(-2).join("/")}`;
}

export function AgentCardGrid({
  agents,
  loading,
  onEdit,
  onDelete,
  onToggle,
  onSyncModel,
  onSyncAllModels,
  syncAllLoading = false,
  canSyncAllModels = false,
  defaultAgentModel,
}: AgentCardGridProps) {
  const { t } = useTranslation();

  const isModelSynced = (agent: AgentSummary) => {
    if (!agent.active_model || !defaultAgentModel) return false;
    return (
      agent.active_model.provider_id === defaultAgentModel.provider_id &&
      agent.active_model.model === defaultAgentModel.model
    );
  };

  if (loading && agents.length === 0) {
    return (
      <div className={styles.cardGrid}>
        {Array.from({ length: 6 }).map((_, index) => (
          <div key={index} className={styles.skeletonCard} />
        ))}
      </div>
    );
  }

  if (!loading && agents.length === 0) {
    return (
      <div className={styles.emptyState}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="还没有智能体"
        />
      </div>
    );
  }

  return (
    <div className={styles.cardGrid}>
      {agents.map((agent) => {
        const isDefault = agent.id === "default";
        const modelSynced = isModelSynced(agent);
        const desc = isDefault
          ? "默认工作伙伴：沈行知负责理解、判断和协作，澜台负责系统与长期沉淀。"
          : compactDescription(
              agent.description || "用于专项任务的后台协作智能体。",
            );
        const role = isDefault ? "默认工作伙伴" : "协作智能体 / 后台模块";

        return (
          <article
            key={agent.id}
            className={`${styles.agentCard} ${
              isDefault ? styles.defaultAgentCard : ""
            } ${!agent.enabled ? styles.agentCardDisabled : ""}`}
          >
            <div>
              <div className={styles.cardTop}>
                <div className={styles.agentAvatar}>
                  <BeverageAgentAvatar agentId={agent.id} size={38} />
                </div>
                <div className={styles.cardTitleWrap}>
                  <div className={styles.cardTitleRow}>
                    <Tooltip title={getAgentDisplayName(agent, t)}>
                      <div className={styles.cardTitle}>
                        {getAgentDisplayName(agent, t)}
                      </div>
                    </Tooltip>
                    <span
                      className={`${styles.statusPill} ${
                        agent.enabled ? "" : styles.statusPillOff
                      }`}
                    >
                      <CheckCircleOutlined />
                      {agent.enabled ? "启用" : t("agent.disabled")}
                    </span>
                  </div>
                  <div className={styles.cardRole}>{role}</div>
                </div>
              </div>

              <div className={styles.cardDesc} title={desc}>
                {desc}
              </div>

              <div className={styles.cardMeta}>
                <Tooltip title={agent.workspace_dir}>
                  <span className={styles.metaPill}>
                    <FolderOpenOutlined />
                    {compactWorkspace(agent.workspace_dir)}
                  </span>
                </Tooltip>
                {agent.active_model ? (
                  <span className={styles.modelPill}>
                    <ProviderIcon
                      providerId={agent.active_model.provider_id}
                      size={15}
                    />
                    <Tooltip title={agent.active_model.model}>
                      <span className={styles.modelName}>
                        {agent.active_model.model}
                      </span>
                    </Tooltip>
                  </span>
                ) : (
                  <span className={`${styles.modelPill} ${styles.modelEmpty}`}>
                    {t("agent.modelPlaceholder")}
                  </span>
                )}
                {!isDefault && modelSynced && (
                  <span className={styles.syncPill}>
                    {t("agent.modelSynced")}
                  </span>
                )}
              </div>
            </div>

            <div className={styles.cardFooter}>
              {isDefault ? (
                <Tooltip title={t("agent.syncAllModelTooltip")}>
                  <Button
                    type="text"
                    icon={<SyncOutlined spin={syncAllLoading} />}
                    onClick={onSyncAllModels}
                    loading={syncAllLoading}
                    disabled={!canSyncAllModels}
                    className={styles.syncBtn}
                  >
                    {t("agent.syncAllModel")}
                  </Button>
                </Tooltip>
              ) : defaultAgentModel && !modelSynced ? (
                <Tooltip title={t("agent.syncModelTooltip")}>
                  <Button
                    type="text"
                    icon={<SwapOutlined />}
                    onClick={() => onSyncModel(agent.id)}
                    className={styles.syncBtn}
                  >
                    {t("agent.syncModel")}
                  </Button>
                </Tooltip>
              ) : (
                <span />
              )}

              <div className={styles.softActionGroup}>
                <Button
                  type="text"
                  size="small"
                  icon={<EditOutlined />}
                  onClick={() => onEdit(agent)}
                  disabled={isDefault}
                  className={styles.softAction}
                  title={isDefault ? t("agent.defaultNotEditable") : undefined}
                >
                  {t("common.edit")}
                </Button>
                <Popconfirm
                  title={
                    agent.enabled
                      ? t("agent.disableConfirm")
                      : t("agent.enableConfirm")
                  }
                  description={
                    agent.enabled
                      ? t("agent.disableConfirmDesc")
                      : t("agent.enableConfirmDesc")
                  }
                  onConfirm={() => onToggle(agent.id, agent.enabled)}
                  disabled={isDefault}
                  okText={t("common.confirm")}
                  cancelText={t("common.cancel")}
                >
                  <Button
                    type="text"
                    size="small"
                    icon={
                      agent.enabled ? <EyeOff size={14} /> : <Eye size={14} />
                    }
                    disabled={isDefault}
                    className={styles.softAction}
                    title={
                      isDefault ? t("agent.defaultNotDisablable") : undefined
                    }
                  >
                    {agent.enabled ? "停用" : "启用"}
                  </Button>
                </Popconfirm>
                <Popconfirm
                  title={t("agent.deleteConfirm")}
                  description={t("agent.deleteConfirmDesc")}
                  onConfirm={() => onDelete(agent.id)}
                  disabled={isDefault}
                  okText={t("common.confirm")}
                  cancelText={t("common.cancel")}
                >
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    disabled={isDefault}
                    className={`${styles.softAction} ${styles.dangerAction}`}
                    title={
                      isDefault ? t("agent.defaultNotDeletable") : undefined
                    }
                  >
                    {t("common.delete")}
                  </Button>
                </Popconfirm>
              </div>
            </div>
          </article>
        );
      })}
    </div>
  );
}
