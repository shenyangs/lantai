import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { Button, Card, Form, Modal, Table } from "@/vendor/lantaiDesign";
import {
  CalendarOutlined,
  ClockCircleOutlined,
  EditOutlined,
  LeftOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  RightOutlined,
  TableOutlined,
} from "@ant-design/icons";
import { Drawer, Empty, Space, Tag } from "antd";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import type { TFunction } from "i18next";
import type {
  CronJobSpecOutput,
  CronOccurrence,
  CronOccurrenceWindow,
} from "../../../api/types";
import { useTranslation } from "react-i18next";
import api from "../../../api";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  createColumns,
  JobDrawer,
  useCronJobs,
  DEFAULT_FORM_VALUES,
} from "./components";
import {
  parseCron,
  serializeCron,
  type CronParts,
} from "./components/parseCron";
import { PageHeader } from "@/components/PageHeader";
import AppState from "../../../components/AppState";
import { createLatestRequestGate } from "../../../utils/latestRequest";
import styles from "./index.module.less";

type CronJob = CronJobSpecOutput;
type ViewMode = "calendar" | "manage";

export interface CalendarOccurrence {
  id: string;
  job: CronJob;
  date: Dayjs;
  timeLabel: string;
  taskTimeLabel: string;
  scheduleTimezone: string;
  scheduleLabel: string;
  status: "pending" | "past";
  sortValue: number;
}

const WEEK_DAYS = [
  { key: "mon", labelKey: "cronJobs.shortMon" },
  { key: "tue", labelKey: "cronJobs.shortTue" },
  { key: "wed", labelKey: "cronJobs.shortWed" },
  { key: "thu", labelKey: "cronJobs.shortThu" },
  { key: "fri", labelKey: "cronJobs.shortFri" },
  { key: "sat", labelKey: "cronJobs.shortSat" },
  { key: "sun", labelKey: "cronJobs.shortSun" },
];

function getWeekStart(date: Dayjs) {
  const weekday = date.day();
  return date.startOf("day").add(weekday === 0 ? -6 : 1 - weekday, "day");
}

function getWeekDays(weekStart: Dayjs) {
  return Array.from({ length: 7 }, (_, index) => weekStart.add(index, "day"));
}

function formatTime(hour = 9, minute = 0) {
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

function formatWeekRange(weekStart: Dayjs, language = "zh") {
  const weekEnd = weekStart.add(6, "day");
  if (language.startsWith("zh")) {
    return `${weekStart.format("YYYY年M月D日")} - ${weekEnd.format("M月D日")}`;
  }
  return `${weekStart.format("MMM D, YYYY")} - ${weekEnd.format("MMM D")}`;
}

function dayKeyToIndex(day: string) {
  return WEEK_DAYS.findIndex((item) => item.key === day);
}

function buildScheduleLabel(parts: CronParts, cron: string, t: TFunction) {
  switch (parts.type) {
    case "hourly":
      return t("cronJobs.cronTypeHourly");
    case "daily":
      return `${t("cronJobs.cronTypeDaily")} ${formatTime(parts.hour, parts.minute)}`;
    case "weekly": {
      const days = (parts.daysOfWeek || [])
        .map((day) => {
          const index = dayKeyToIndex(day);
          return index >= 0 ? t(WEEK_DAYS[index].labelKey) : day;
        })
        .join(" / ");
      return `${t("cronJobs.cronTypeWeekly")} ${days} ${formatTime(parts.hour, parts.minute)}`;
    }
    case "custom":
    default:
      return parts.rawCron || cron;
  }
}

function formatInScheduleTimezone(
  value: string,
  scheduleTimezone: string,
  language: string,
) {
  try {
    return new Intl.DateTimeFormat(language, {
      timeZone: scheduleTimezone,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

export function projectAuthoritativeOccurrences(
  source: CronOccurrence[],
  jobs: CronJob[],
  now: Dayjs,
  language: string,
  t: TFunction,
): CalendarOccurrence[] {
  const jobsById = new Map(jobs.map((job) => [job.id, job]));
  return source
    .flatMap((item): CalendarOccurrence[] => {
      const job = jobsById.get(item.job_id);
      const date = dayjs(item.scheduled_at);
      if (!job || !date.isValid()) return [];
      const cron = job.schedule?.cron || "0 9 * * *";
      return [
        {
          id: `${job.id}-${item.scheduled_at}`,
          job,
          date,
          timeLabel: date.format("HH:mm"),
          taskTimeLabel: formatInScheduleTimezone(
            item.scheduled_at,
            item.schedule_timezone,
            language,
          ),
          scheduleTimezone: item.schedule_timezone,
          scheduleLabel: buildScheduleLabel(parseCron(cron), cron, t),
          status: date.isBefore(now) ? "past" : "pending",
          sortValue: date.valueOf(),
        },
      ];
    })
    .sort((a, b) => a.sortValue - b.sortValue);
}

function getJobPreview(job: CronJob) {
  if (job.task_type === "text") return job.text || "";
  const input = job.request?.input;
  if (!input) return "";
  if (typeof input === "string") return input;
  try {
    return JSON.stringify(input, null, 2);
  } catch {
    return String(input);
  }
}

function getOccurrenceStatusLabel(
  status: CalendarOccurrence["status"],
  t: TFunction,
) {
  if (status === "past") return t("cronJobs.statusPast");
  return t("cronJobs.statusPending");
}

function CronJobsPage() {
  const { t, i18n } = useTranslation();
  const { message } = useAppMessage();
  const {
    jobs,
    loading,
    error,
    fetchJobs,
    createJob,
    updateJob,
    deleteJob,
    toggleEnabled,
    executeNow,
  } = useCronJobs();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingJob, setEditingJob] = useState<CronJob | null>(null);
  const [selectedJob, setSelectedJob] = useState<CronJob | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("calendar");
  const [weekStart, setWeekStart] = useState(() => getWeekStart(dayjs()));
  const [saving, setSaving] = useState(false);
  const [occurrenceWindow, setOccurrenceWindow] =
    useState<CronOccurrenceWindow | null>(null);
  const [occurrenceLoading, setOccurrenceLoading] = useState(false);
  const [occurrenceError, setOccurrenceError] = useState("");
  const [occurrenceWarning, setOccurrenceWarning] = useState("");
  const [occurrenceRefreshKey, setOccurrenceRefreshKey] = useState(0);
  const [form] = Form.useForm<CronJob>();
  const userTimezoneRef = useRef("UTC");
  const occurrenceGateRef = useRef(createLatestRequestGate());
  const now = dayjs();

  const calendarDays = useMemo(() => getWeekDays(weekStart), [weekStart]);
  const occurrences = useMemo(
    () =>
      projectAuthoritativeOccurrences(
        occurrenceWindow?.items || [],
        jobs,
        now,
        i18n.language,
        t,
      ),
    [i18n.language, jobs, now, occurrenceWindow, t],
  );
  const occurrencesByDay = useMemo(() => {
    const grouped = new Map<string, CalendarOccurrence[]>();
    occurrences.forEach((item) => {
      const key = item.date.format("YYYY-MM-DD");
      grouped.set(key, [...(grouped.get(key) || []), item]);
    });
    return grouped;
  }, [occurrences]);
  const enabledCount = jobs.filter((job) => job.enabled).length;
  const pausedCount = jobs.length - enabledCount;
  const occurrenceCountLabel = occurrenceLoading
    ? "…"
    : occurrenceWindow
      ? `${occurrenceWindow.complete ? "" : "≥"}${occurrenceWindow.occurrence_count}`
      : "—";
  const selectedPreview = selectedJob ? getJobPreview(selectedJob) : "";
  const selectedCron = selectedJob?.schedule?.cron || "0 9 * * *";
  const selectedSchedule = selectedJob
    ? buildScheduleLabel(parseCron(selectedCron), selectedCron, t)
    : "";
  const selectedDebugInfo = useMemo(() => {
    if (!selectedJob) return "";
    return JSON.stringify(
      {
        id: selectedJob.id,
        schedule: selectedJob.schedule,
        dispatch: selectedJob.dispatch,
        runtime: selectedJob.runtime,
      },
      null,
      2,
    );
  }, [selectedJob]);

  const copyToClipboard = useCallback(
    async (text: string) => {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          const textArea = document.createElement("textarea");
          textArea.value = text;
          textArea.style.position = "fixed";
          textArea.style.left = "-999999px";
          textArea.style.top = "-999999px";
          document.body.appendChild(textArea);
          textArea.focus();
          textArea.select();
          document.execCommand("copy");
          textArea.remove();
        }
        message.success(t("common.copied"));
      } catch (err) {
        console.error("Failed to copy text:", err);
        message.error(t("common.copyFailed"));
      }
    },
    [message, t],
  );

  useEffect(() => {
    api
      .getUserTimezone()
      .then((res) => {
        if (res.timezone) userTimezoneRef.current = res.timezone;
      })
      .catch((err) => console.error("Failed to fetch user timezone:", err));
  }, []);

  useEffect(() => {
    if (loading || error || jobs.length === 0) {
      occurrenceGateRef.current.invalidate();
      setOccurrenceWindow(null);
      setOccurrenceLoading(false);
      setOccurrenceError("");
      setOccurrenceWarning("");
      return;
    }

    const ownsState = occurrenceGateRef.current.begin();
    const startAt = weekStart.startOf("day").toISOString();
    const endAt = weekStart.add(7, "day").startOf("day").toISOString();
    setOccurrenceWindow(null);
    setOccurrenceLoading(true);
    setOccurrenceError("");
    setOccurrenceWarning("");
    void api
      .listCronOccurrences(startAt, endAt)
      .then((result) => {
        if (!ownsState()) return;
        setOccurrenceWindow(result);
        if (!result.complete) {
          setOccurrenceWarning(t("cronJobs.projectionIncomplete"));
        }
      })
      .catch((loadError: unknown) => {
        if (!ownsState()) return;
        console.error("Failed to load cron occurrences", loadError);
        setOccurrenceError(t("cronJobs.projectionLoadFailed"));
      })
      .finally(() => {
        if (ownsState()) setOccurrenceLoading(false);
      });
  }, [error, jobs, loading, occurrenceRefreshKey, t, weekStart]);

  useEffect(
    () => () => {
      occurrenceGateRef.current.invalidate();
    },
    [],
  );

  const handleCreate = () => {
    setEditingJob(null);
    form.resetFields();
    form.setFieldsValue({
      ...DEFAULT_FORM_VALUES,
      schedule: {
        ...DEFAULT_FORM_VALUES.schedule,
        timezone: userTimezoneRef.current,
      },
    });
    setDrawerOpen(true);
  };

  const handleEdit = (job: CronJob) => {
    setEditingJob(job);

    // Parse cron expression to form fields
    const cronParts = parseCron(job.schedule?.cron || "0 9 * * *");

    const formValues: any = {
      ...job,
      request: {
        ...job.request,
        input: job.request?.input
          ? JSON.stringify(job.request.input, null, 2)
          : "",
      },
      cronType: cronParts.type,
    };

    // Set time picker value
    if (cronParts.type === "daily" || cronParts.type === "weekly") {
      const h = cronParts.hour ?? 9;
      const m = cronParts.minute ?? 0;
      formValues.cronTime = dayjs().hour(h).minute(m);
    }

    // Set days of week
    if (cronParts.type === "weekly" && cronParts.daysOfWeek) {
      formValues.cronDaysOfWeek = cronParts.daysOfWeek;
    }

    // Set custom cron
    if (cronParts.type === "custom" && cronParts.rawCron) {
      formValues.cronCustom = cronParts.rawCron;
    }

    form.setFieldsValue(formValues);
    setDrawerOpen(true);
  };

  const handleDelete = (jobId: string) => {
    Modal.confirm({
      title: t("cronJobs.confirmDelete"),
      content: t("cronJobs.deleteConfirm"),
      okText: t("cronJobs.deleteText"),
      okType: "primary",
      cancelText: t("cronJobs.cancelText"),
      onOk: async () => {
        await deleteJob(jobId);
      },
    });
  };

  const handleToggleEnabled = async (job: CronJob) => {
    if (job.enabled) return toggleEnabled(job);
    const cron = job.schedule?.cron || "0 9 * * *";
    const schedule = buildScheduleLabel(parseCron(cron), cron, t);
    Modal.confirm({
      title: `启用定时任务“${job.name}”？`,
      content: (
        <div style={{ display: "grid", gap: 8 }}>
          <div>
            计划：<strong>{schedule}</strong>
          </div>
          <div>任务：{getJobPreview(job) || "未填写任务内容"}</div>
          <div>
            启用后会按计划自动运行；若配置了渠道投递，结果可能发送到外部会话。
          </div>
        </div>
      ),
      okText: "确认启用",
      cancelText: "返回检查",
      onOk: () => toggleEnabled(job),
    });
  };

  const handleExecuteNow = async (job: CronJob) => {
    Modal.confirm({
      title: t("cronJobs.executeNowTitle"),
      content: t("cronJobs.executeNowContent", { name: job.name }),
      okText: t("cronJobs.executeNowConfirm"),
      okType: "primary",
      cancelText: t("cronJobs.cancelText"),
      onOk: async () => {
        await executeNow(job.id);
      },
    });
  };

  const handleDrawerClose = () => {
    setDrawerOpen(false);
    setEditingJob(null);
  };

  const handleSubmit = async (values: any) => {
    // Serialize cron from form fields
    const cronParts: any = {
      type: values.cronType || "daily",
    };

    if (values.cronType === "daily" || values.cronType === "weekly") {
      if (values.cronTime) {
        cronParts.hour = values.cronTime.hour();
        cronParts.minute = values.cronTime.minute();
      }
    }

    if (values.cronType === "weekly" && values.cronDaysOfWeek) {
      cronParts.daysOfWeek = values.cronDaysOfWeek;
    }

    if (values.cronType === "custom" && values.cronCustom) {
      cronParts.rawCron = values.cronCustom;
    }

    const cronExpression = serializeCron(cronParts);

    const processedValues = {
      ...values,
      schedule: {
        ...values.schedule,
        cron: cronExpression,
      },
    };

    if (processedValues.task_type === "text") {
      // Remove request object entirely for text tasks
      delete processedValues.request;
    } else if (processedValues.task_type === "agent") {
      //Ensure request object exists
      if (!processedValues.request) {
        processedValues.request = {};
      }

      // Parse request input JSON
      if (
        processedValues.request?.input &&
        typeof processedValues.request.input === "string"
      ) {
        try {
          processedValues.request.input = JSON.parse(
            processedValues.request.input,
          );
        } catch (error) {
          console.error("❌ Failed to parse request.input JSON:", error);
        }
      }
    }

    setSaving(true);
    try {
      const success = editingJob
        ? await updateJob(editingJob.id, processedValues)
        : await createJob(processedValues);
      if (success) {
        setDrawerOpen(false);
      }
    } finally {
      setSaving(false);
    }
  };

  const columns = createColumns({
    onToggleEnabled: handleToggleEnabled,
    onExecuteNow: handleExecuteNow,
    onEdit: handleEdit,
    onDelete: handleDelete,
    t,
  });

  return (
    <div className={styles.cronJobsPage}>
      <PageHeader
        items={[{ title: t("nav.control") }, { title: t("cronJobs.title") }]}
        description={t("cronJobs.description")}
        extra={
          <Button
            type={!loading && jobs.length === 0 ? "default" : "primary"}
            icon={<PlusOutlined />}
            onClick={handleCreate}
          >
            {t("cronJobs.createJob")}
          </Button>
        }
      />

      <div className={styles.automationShell}>
        {error ? (
          <AppState
            compact
            tone="error"
            title="无法加载定时任务"
            description={error}
            actions={<Button onClick={() => void fetchJobs()}>重试</Button>}
          />
        ) : null}
        {!error ? (
          <>
            <div className={styles.viewHeader}>
              <div
                className={styles.viewTabs}
                role="tablist"
                aria-label="定时任务视图"
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={viewMode === "calendar"}
                  className={`${styles.viewTab} ${
                    viewMode === "calendar" ? styles.viewTabActive : ""
                  }`}
                  onClick={() => setViewMode("calendar")}
                >
                  <CalendarOutlined />
                  {t("cronJobs.calendarView")}
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={viewMode === "manage"}
                  className={`${styles.viewTab} ${
                    viewMode === "manage" ? styles.viewTabActive : ""
                  }`}
                  onClick={() => setViewMode("manage")}
                >
                  <TableOutlined />
                  {t("cronJobs.managementView")}
                </button>
              </div>

              <div className={styles.summaryRail}>
                <span>
                  <strong>{occurrenceWindow?.rule_count ?? enabledCount}</strong>
                  {t("cronJobs.enabledRules")}
                </span>
                <span>
                  <strong>{pausedCount}</strong>
                  {t("cronJobs.pausedJobs")}
                </span>
                <span>
                  <strong>{occurrenceCountLabel}</strong>
                  {t("cronJobs.plannedOccurrences")}
                </span>
                <span>
                  <strong>{occurrences.length}</strong>
                  {t("cronJobs.renderedBlocks")}
                </span>
              </div>
            </div>

            {viewMode === "calendar" ? (
              <Card
                className={styles.calendarCard}
                styles={{ body: { padding: 0 } }}
              >
                <div className={styles.calendarToolbar}>
                  <div className={styles.weekControls}>
                    <Button
                      icon={<LeftOutlined />}
                      onClick={() =>
                        setWeekStart((prev) => prev.add(-7, "day"))
                      }
                    />
                    <div className={styles.weekRange}>
                      {formatWeekRange(weekStart, i18n.language)}
                    </div>
                    <Button
                      icon={<RightOutlined />}
                      onClick={() => setWeekStart((prev) => prev.add(7, "day"))}
                    />
                    <Button onClick={() => setWeekStart(getWeekStart(dayjs()))}>
                      {t("cronJobs.today")}
                    </Button>
                  </div>
                  <span className={styles.projectionHint}>
                    {occurrenceWarning || t("cronJobs.planOnlyHint")}
                  </span>
                </div>

                <div className={styles.calendarStage}>
                  <div
                    className={`${styles.calendarGrid} ${
                      loading ||
                      occurrenceLoading ||
                      occurrenceError ||
                      jobs.length === 0
                        ? styles.calendarGridQuiet
                        : ""
                    }`}
                  >
                    {calendarDays.map((day, index) => {
                      const dayItems =
                        occurrencesByDay.get(day.format("YYYY-MM-DD")) || [];
                      const isToday = day.isSame(now, "day");

                      return (
                        <section
                          key={day.format("YYYY-MM-DD")}
                          className={`${styles.dayColumn} ${
                            isToday ? styles.dayColumnToday : ""
                          }`}
                        >
                          <header className={styles.dayHeader}>
                            <span>{t(WEEK_DAYS[index].labelKey)}</span>
                            <strong>{day.format("MM/DD")}</strong>
                          </header>
                          <div className={styles.dayBody}>
                            {dayItems.length === 0 ? (
                              occurrenceLoading ||
                              occurrenceError ||
                              occurrenceWarning ? null :
                              jobs.length > 0 ? (
                                <div className={styles.dayEmpty}>
                                  {t("cronJobs.noPlan")}
                                </div>
                              ) : null
                            ) : (
                              dayItems.map((item) => (
                                <button
                                  key={item.id}
                                  type="button"
                                  className={`${styles.occurrenceCard} ${
                                    styles[`occurrence_${item.status}`]
                                  }`}
                                  onClick={() => setSelectedJob(item.job)}
                                >
                                  <span className={styles.occurrenceMeta}>
                                    <strong>
                                      {t("cronJobs.viewerTime")} {item.timeLabel}
                                    </strong>
                                    <em>
                                      {item.job.dispatch?.channel || "console"}
                                    </em>
                                  </span>
                                  <span className={styles.occurrenceName}>
                                    {item.job.name}
                                  </span>
                                  <span className={styles.occurrenceTimezone}>
                                    {t("cronJobs.taskTimezone")} {item.taskTimeLabel}{" "}
                                    {item.scheduleTimezone}
                                  </span>
                                  <span className={styles.occurrenceStatus}>
                                    {getOccurrenceStatusLabel(item.status, t)}
                                  </span>
                                </button>
                              ))
                            )}
                          </div>
                        </section>
                      );
                    })}
                  </div>

                  {loading || occurrenceLoading ? (
                    <div className={styles.calendarOverlay}>
                      <Empty description={t("common.loading")} />
                    </div>
                  ) : occurrenceError ? (
                    <div className={styles.calendarOverlay}>
                      <AppState
                        compact
                        tone="error"
                        title={t("cronJobs.projectionLoadFailedTitle")}
                        description={occurrenceError}
                        actions={
                          <Button
                            onClick={() =>
                              setOccurrenceRefreshKey((value) => value + 1)
                            }
                          >
                            {t("cronJobs.retry")}
                          </Button>
                        }
                      />
                    </div>
                  ) : jobs.length === 0 ? (
                    <div className={styles.calendarOverlay}>
                      <ClockCircleOutlined />
                      <h3>{t("cronJobs.emptyTitle")}</h3>
                      <p>{t("cronJobs.emptyDescription")}</p>
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={handleCreate}
                      >
                        {t("cronJobs.createJob")}
                      </Button>
                    </div>
                  ) : null}
                </div>
              </Card>
            ) : (
              <Card
                className={styles.tableCard}
                styles={{ body: { padding: 0 } }}
              >
                <div className={styles.manageHeader}>
                  <div>
                    <h3>{t("cronJobs.managementView")}</h3>
                    <p>{t("cronJobs.managementDescription")}</p>
                  </div>
                  <Tag color="blue">
                    {t("cronJobs.totalItems", { count: jobs.length })}
                  </Tag>
                </div>
                <Table
                  columns={columns}
                  dataSource={jobs}
                  loading={loading}
                  rowKey="id"
                  scroll={{ x: 1280 }}
                  rowClassName={styles.clickableRow}
                  onRow={(record) => ({
                    onClick: () => setSelectedJob(record),
                  })}
                  pagination={{
                    pageSize: 10,
                    showSizeChanger: false,
                  }}
                />
              </Card>
            )}
          </>
        ) : null}
      </div>

      <Drawer
        open={Boolean(selectedJob)}
        onClose={() => setSelectedJob(null)}
        width={460}
        title={selectedJob?.name || t("cronJobs.taskDetails")}
      >
        {selectedJob ? (
          <div className={styles.detailPanel}>
            <div className={styles.detailHero}>
              <span
                className={`${styles.detailStatusDot} ${
                  selectedJob.enabled ? styles.enabled : styles.disabled
                }`}
              />
              <div>
                <h3>{selectedJob.name}</h3>
                <Space size={6} wrap>
                  <Tag color={selectedJob.enabled ? "green" : "default"}>
                    {selectedJob.enabled
                      ? t("common.enabled")
                      : t("common.disabled")}
                  </Tag>
                  <Tag>{selectedJob.task_type || "agent"}</Tag>
                </Space>
              </div>
            </div>

            <dl className={styles.detailList}>
              <div>
                <dt>{t("cronJobs.scheduleCronLabel")}</dt>
                <dd>{selectedSchedule}</dd>
              </div>
              <div>
                <dt>{t("cronJobs.scheduleTimezone")}</dt>
                <dd>{selectedJob.schedule?.timezone || "UTC"}</dd>
              </div>
              <div>
                <dt>{t("cronJobs.dispatchChannel")}</dt>
                <dd>{selectedJob.dispatch?.channel || "console"}</dd>
              </div>
              <div>
                <dt>{t("cronJobs.dispatchMode")}</dt>
                <dd>{selectedJob.dispatch?.mode || "final"}</dd>
              </div>
            </dl>

            <section className={styles.detailSection}>
              <h4>{t("cronJobs.requestInput")}</h4>
              {selectedPreview ? (
                <pre>{selectedPreview}</pre>
              ) : (
                <p>{t("cronJobs.noPayload")}</p>
              )}
            </section>

            <details className={styles.debugDetails}>
              <summary>调试信息</summary>
              <div className={styles.debugBody}>
                <div className={styles.debugToolbar}>
                  <span>任务 ID、Cron 原始表达式和投递目标</span>
                  <Button
                    size="small"
                    onClick={() => void copyToClipboard(selectedDebugInfo)}
                  >
                    {t("common.copy")}
                  </Button>
                </div>
                <pre>{selectedDebugInfo}</pre>
              </div>
            </details>

            <div className={styles.detailActions}>
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                onClick={() => handleExecuteNow(selectedJob)}
              >
                {t("cronJobs.executeNow")}
              </Button>
              <Button
                icon={
                  selectedJob.enabled ? (
                    <PauseCircleOutlined />
                  ) : (
                    <PlayCircleOutlined />
                  )
                }
                onClick={async () => {
                  const originalJob = selectedJob;
                  const nextJob = {
                    ...selectedJob,
                    enabled: !selectedJob.enabled,
                  };
                  setSelectedJob(nextJob);
                  const success = await handleToggleEnabled(originalJob);
                  if (!success) {
                    setSelectedJob(originalJob);
                  }
                }}
              >
                {selectedJob.enabled
                  ? t("cronJobs.disable")
                  : t("common.enable")}
              </Button>
              <Button
                icon={<EditOutlined />}
                disabled={selectedJob.enabled}
                onClick={() => {
                  const job = selectedJob;
                  setSelectedJob(null);
                  handleEdit(job);
                }}
              >
                {t("cronJobs.edit")}
              </Button>
            </div>
          </div>
        ) : null}
      </Drawer>

      <JobDrawer
        open={drawerOpen}
        editingJob={editingJob}
        form={form}
        saving={saving}
        onClose={handleDrawerClose}
        onSubmit={handleSubmit}
      />
    </div>
  );
}

export default CronJobsPage;
