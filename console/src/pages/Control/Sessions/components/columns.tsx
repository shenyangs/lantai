import { Button, Dropdown, Tag, Tooltip } from "@/vendor/lantaiDesign";
import type { TFunction } from "i18next";
import type { ColumnsType } from "antd/es/table";
import type { MenuProps } from "antd";
import { MoreOutlined } from "@ant-design/icons";
import { formatTime, type Session } from "./constants";
import { CHANNEL_COLORS } from "../../../../constants/channel";
import styles from "../index.module.less";

interface ColumnHandlers {
  onEdit: (session: Session) => void;
  onDelete: (sessionId: string) => void;
  onView: (session: Session) => void;
  t: TFunction;
}

/** Normalize ISO string to UTC for consistent sorting across mixed timezone formats. */
const toUTCTime = (ts: string | null | undefined): number => {
  if (!ts) return 0;
  const normalized =
    /[Z+-]\d{2}:?\d{2}$/.test(ts) || ts.endsWith("Z") ? ts : ts + "Z";
  return new Date(normalized).getTime();
};

export const createColumns = (
  handlers: ColumnHandlers,
): ColumnsType<Session> => {
  const { t } = handlers;

  return [
    {
      title: "会话",
      key: "session",
      width: 360,
      render: (_: unknown, record: Session) => (
        <div className={styles.sessionNameCell}>
          <strong>{record.name || "未命名会话"}</strong>
          <span>
            最近更新 {formatTime(record.updated_at)} ·{" "}
            {record.status === "running" ? "进行中" : "空闲"}
          </span>
        </div>
      ),
    },
    {
      title: "来源",
      dataIndex: "channel",
      key: "channel",
      width: 170,
      render: (channel: string, record: Session) => (
        <div className={styles.sessionSourceCell}>
          <Tag color={CHANNEL_COLORS[channel] || "default"}>
            {channel || "default"}
          </Tag>
          <Tooltip title="内部用户信息已收进详情页">
            <span>
              {record.user_id === "default" ? "默认用户" : "外部用户"}
            </span>
          </Tooltip>
        </div>
      ),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (timestamp: string | number | null) => formatTime(timestamp),
      sorter: (a: Session, b: Session) =>
        toUTCTime(a.created_at) - toUTCTime(b.created_at),
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (timestamp: string | number | null) => formatTime(timestamp),
      sorter: (a: Session, b: Session) =>
        toUTCTime(a.updated_at) - toUTCTime(b.updated_at),
      defaultSortOrder: "descend",
    },
    {
      title: "Action",
      key: "action",
      width: 180,
      fixed: "right",
      render: (_: unknown, record: Session) => {
        const menuItems: MenuProps["items"] = [
          {
            key: "edit",
            label: t("common.edit"),
            onClick: () => handlers.onEdit(record),
          },
          {
            key: "delete",
            label: t("common.delete"),
            danger: true,
            onClick: () => handlers.onDelete(record.id),
          },
        ];

        return (
          <div className={styles.actionColumn}>
            <Button
              type="link"
              size="small"
              onClick={(event) => {
                event.stopPropagation();
                handlers.onView(record);
              }}
            >
              {t("common.view")}
            </Button>
            <Dropdown menu={{ items: menuItems }} placement="bottomRight">
              <Button
                type="text"
                size="small"
                icon={<MoreOutlined />}
                onClick={(event) => event.stopPropagation()}
              />
            </Dropdown>
          </div>
        );
      },
    },
  ];
};
