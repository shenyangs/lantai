import { Button, Tooltip, Dropdown, Tag } from "@/vendor/lantaiDesign";
import type { ColumnsType } from "antd/es/table";
import type { MenuProps } from "antd";
import type { CronJobSpecOutput } from "../../../../api/types";
import { MoreOutlined } from "@ant-design/icons";
import { TFunction } from "i18next";
import { parseCron } from "./parseCron";
import styles from "../index.module.less";

type CronJob = CronJobSpecOutput;

interface ColumnHandlers {
  onToggleEnabled: (job: CronJob) => void;
  onExecuteNow: (job: CronJob) => void;
  onEdit: (job: CronJob) => void;
  onDelete: (jobId: string) => void;
  t: TFunction;
}

const getScheduleLabel = (cron: string | undefined, t: TFunction) => {
  const cronParts = parseCron(cron || "0 9 * * *");

  switch (cronParts.type) {
    case "hourly":
      return t("cronJobs.cronTypeHourly");
    case "daily":
      return `${t("cronJobs.cronTypeDaily")} ${String(cronParts.hour).padStart(
        2,
        "0",
      )}:${String(cronParts.minute).padStart(2, "0")}`;
    case "weekly": {
      const dayNames = (cronParts.daysOfWeek || [])
        .map((d) => {
          const dayMap: Record<string, string> = {
            mon: t("cronJobs.cronDayMon"),
            tue: t("cronJobs.cronDayTue"),
            wed: t("cronJobs.cronDayWed"),
            thu: t("cronJobs.cronDayThu"),
            fri: t("cronJobs.cronDayFri"),
            sat: t("cronJobs.cronDaySat"),
            sun: t("cronJobs.cronDaySun"),
          };
          return dayMap[d] || d;
        })
        .join(" / ");
      return `${t("cronJobs.cronTypeWeekly")} ${dayNames} ${String(
        cronParts.hour,
      ).padStart(2, "0")}:${String(cronParts.minute).padStart(2, "0")}`;
    }
    case "custom":
    default:
      return cronParts.rawCron || cron || "-";
  }
};

const getJobPreview = (job: CronJob) => {
  if (job.task_type === "text") return job.text || "";
  const input = job.request?.input;
  if (!input) return "";
  if (typeof input === "string") return input;
  try {
    return JSON.stringify(input);
  } catch {
    return String(input);
  }
};

const getTaskTypeLabel = (job: CronJob) =>
  job.task_type === "text" ? "固定文本" : "AI 执行";

export const createColumns = (
  handlers: ColumnHandlers,
): ColumnsType<CronJob> => {
  return [
    {
      title: handlers.t("cronJobs.name"),
      key: "name",
      width: 300,
      fixed: "left",
      render: (_: unknown, record: CronJob) => (
        <div className={styles.jobNameCell}>
          <strong>{record.name || "未命名任务"}</strong>
          <span>{getTaskTypeLabel(record)}</span>
        </div>
      ),
    },
    {
      title: handlers.t("cronJobs.enabled"),
      dataIndex: "enabled",
      key: "enabled",
      width: 100,
      render: (enabled: boolean) => (
        <span className={styles.statusIndicator}>
          <span
            className={`${styles.statusDot} ${
              enabled ? styles.enabled : styles.disabled
            }`}
          />
          {enabled
            ? handlers.t("common.enabled")
            : handlers.t("common.disabled")}
        </span>
      ),
    },
    {
      title: "执行计划",
      key: "schedule",
      width: 260,
      render: (_: unknown, record: CronJob) => {
        const cron = record.schedule?.cron || "0 9 * * *";
        const displayText = getScheduleLabel(cron, handlers.t);
        return (
          <Tooltip title={`Cron 表达式：${cron}`}>
            <div className={styles.scheduleCell}>
              <strong>{displayText}</strong>
              <span>{record.schedule?.timezone || "UTC"}</span>
            </div>
          </Tooltip>
        );
      },
    },
    {
      title: "投递位置",
      key: "dispatch",
      width: 190,
      render: (_: unknown, record: CronJob) => (
        <div className={styles.dispatchCell}>
          <Tag>{record.dispatch?.channel || "console"}</Tag>
          <span>
            {record.dispatch?.mode === "stream" ? "流式" : "最终回复"}
          </span>
        </div>
      ),
    },
    {
      title: "任务内容",
      key: "preview",
      width: 320,
      render: (_: unknown, record: CronJob) => {
        const preview = getJobPreview(record);
        if (!preview) return <span className={styles.mutedText}>无输入</span>;
        return (
          <Tooltip title={preview}>
            <span className={styles.previewText}>{preview}</span>
          </Tooltip>
        );
      },
    },
    {
      title: "运行限制",
      key: "runtime",
      width: 180,
      render: (_: unknown, record: CronJob) => (
        <div className={styles.runtimeCell}>
          <span>并发 {record.runtime?.max_concurrency ?? 1}</span>
          <span>超时 {record.runtime?.timeout_seconds ?? "-"} 秒</span>
        </div>
      ),
    },
    {
      title: handlers.t("cronJobs.action"),
      key: "action",
      width: 240,
      fixed: "right",
      render: (_: unknown, record: CronJob) => {
        const menuItems: MenuProps["items"] = [
          {
            key: "edit",
            label: handlers.t("cronJobs.edit"),
            disabled: record.enabled,
            onClick: () => handlers.onEdit(record),
          },
          {
            key: "delete",
            label: handlers.t("cronJobs.delete"),
            disabled: record.enabled,
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
                handlers.onToggleEnabled(record);
              }}
            >
              {record.enabled
                ? handlers.t("cronJobs.disable")
                : handlers.t("common.enable")}
            </Button>
            <Button
              type="link"
              size="small"
              onClick={(event) => {
                event.stopPropagation();
                handlers.onExecuteNow(record);
              }}
            >
              {handlers.t("cronJobs.executeNow")}
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
