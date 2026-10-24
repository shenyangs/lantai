import { Button, Popconfirm, Table, Tag, Tooltip } from "@/vendor/lantaiDesign";
import { Space } from "antd";
import { RefreshCcw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { RememberedApproval } from "../../../../api/modules/security";
import styles from "../index.module.less";

const RISK_COLORS: Record<string, string> = {
  low: "blue",
  medium: "gold",
  high: "orange",
  critical: "red",
};

interface RememberedApprovalsTableProps {
  approvals: RememberedApproval[];
  enabled: boolean;
  loading?: boolean;
  onRefresh: () => void | Promise<void>;
  onDelete: (ruleId: string) => void | Promise<void>;
  onClearAll: () => void | Promise<void>;
}

function formatTimestamp(value: string): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function workspaceLabel(workspaceDir: string): string {
  const segments = workspaceDir.split(/[\\/]/).filter(Boolean);
  return segments[segments.length - 1] || workspaceDir || "-";
}

export function RememberedApprovalsTable({
  approvals,
  enabled,
  loading = false,
  onRefresh,
  onDelete,
  onClearAll,
}: RememberedApprovalsTableProps) {
  const { t } = useTranslation();
  const hasApprovals = approvals.length > 0;

  const columns = [
    {
      title: t("security.rememberedApprovals.tool"),
      dataIndex: "tool_name",
      key: "tool_name",
      width: 190,
      render: (toolName: string) => (
        <span className={styles.monoText}>{toolName || "-"}</span>
      ),
    },
    {
      title: t("security.rememberedApprovals.rules"),
      dataIndex: "matched_rule_ids",
      key: "matched_rule_ids",
      render: (ruleIds: string[]) => (
        <Space size={[4, 4]} wrap>
          {(ruleIds ?? []).slice(0, 3).map((ruleId) => (
            <Tag key={ruleId}>{ruleId}</Tag>
          ))}
          {(ruleIds?.length ?? 0) > 3 && <Tag>+{ruleIds.length - 3}</Tag>}
        </Space>
      ),
    },
    {
      title: t("security.rememberedApprovals.risk"),
      dataIndex: "risk_level",
      key: "risk_level",
      width: 96,
      render: (risk: string) => {
        const normalized = String(risk || "medium").toLowerCase();
        return (
          <Tag color={RISK_COLORS[normalized] ?? "default"}>
            {normalized.toUpperCase()}
          </Tag>
        );
      },
    },
    {
      title: t("security.rememberedApprovals.workspace"),
      dataIndex: "workspace_dir",
      key: "workspace_dir",
      ellipsis: true,
      render: (workspaceDir: string) => (
        <Tooltip title={workspaceDir || ""}>
          <span className={styles.pathText}>
            {workspaceLabel(workspaceDir)}
          </span>
        </Tooltip>
      ),
    },
    {
      title: t("security.rememberedApprovals.command"),
      dataIndex: "command_head",
      key: "command_head",
      width: 120,
      render: (commandHead: string) => (
        <span className={styles.monoText}>{commandHead || "-"}</span>
      ),
    },
    {
      title: t("security.rememberedApprovals.updatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (_: string, record: RememberedApproval) => (
        <span>{formatTimestamp(record.updated_at || record.created_at)}</span>
      ),
    },
    {
      title: t("security.rememberedApprovals.actions"),
      key: "actions",
      width: 88,
      render: (_: unknown, record: RememberedApproval) => (
        <Popconfirm
          title={t("security.rememberedApprovals.deleteConfirm")}
          okText={t("common.delete")}
          cancelText={t("common.cancel")}
          onConfirm={() => onDelete(record.id)}
          disabled={!enabled}
        >
          <Tooltip title={t("security.rememberedApprovals.delete")}>
            <Button
              type="text"
              danger
              size="small"
              aria-label={`${t("security.rememberedApprovals.delete")}：${record.id}`}
              icon={<Trash2 size={14} />}
              iconSize={14}
              disabled={!enabled}
            />
          </Tooltip>
        </Popconfirm>
      ),
    },
  ];

  return (
    <>
      <div className={styles.rememberedHeader}>
        <div className={styles.rememberedHeaderMeta}>
          <Tag>{approvals.length}</Tag>
        </div>
        <Space size="small">
          <Tooltip title={t("security.rememberedApprovals.refresh")}>
            <Button
              size="small"
              type="text"
              aria-label={t("security.rememberedApprovals.refresh")}
              icon={<RefreshCcw size={14} />}
              iconSize={14}
              onClick={onRefresh}
              loading={loading}
            />
          </Tooltip>
          <Popconfirm
            title={t("security.rememberedApprovals.clearConfirm")}
            okText={t("common.delete")}
            cancelText={t("common.cancel")}
            onConfirm={onClearAll}
            disabled={!enabled || !hasApprovals}
          >
            <Button
              size="small"
              danger
              disabled={!enabled || !hasApprovals}
              icon={<Trash2 size={14} />}
              iconSize={14}
            >
              {t("security.rememberedApprovals.clearAll")}
            </Button>
          </Popconfirm>
        </Space>
      </div>
      <Table
        rowKey="id"
        columns={columns}
        dataSource={approvals}
        loading={loading}
        pagination={false}
        size="middle"
        scroll={{ x: 980 }}
        locale={{
          emptyText: t("security.rememberedApprovals.empty"),
        }}
      />
    </>
  );
}
