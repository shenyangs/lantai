import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Button,
  DatePicker,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Segmented,
  Select,
  Spin,
  Switch,
  Tooltip,
} from "antd";
import dayjs from "dayjs";
import {
  Ban,
  Bell,
  CalendarDays,
  CheckCircle,
  Clock,
  Eye,
  ListChecks,
  Plus,
  RefreshCw,
  Repeat2,
  ThumbsUp,
} from "lucide-react";
import { reminderApi } from "../../../api/modules/reminder";
import { timeApi } from "../../../api/modules/time";
import type {
  ReminderCreateRequest,
  ReminderItem,
  ReminderPriority,
  ReminderStatus,
  ReminderSettings,
  TimeItem,
  TodayTimeline,
  WatchTopic,
} from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";
import {
  CapabilityChip,
  CapabilityPage,
} from "../../../components/CapabilityLayout";
import styles from "./index.module.less";
import AppState from "../../../components/AppState";

type ReminderTab = "today" | "center" | "topics";

const STATUS_LABEL: Record<ReminderStatus, string> = {
  new: "未读",
  read: "已读",
  snoozed: "稍后",
  dismissed: "已忽略",
  done: "已完成",
};

const PRIORITY_LABEL: Record<ReminderPriority, string> = {
  low: "低",
  normal: "普通",
  high: "高价值",
};

function formatTime(value?: string | null) {
  if (!value) return "未设时间";
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("MM/DD HH:mm") : "未设时间";
}

export function parseNaturalLanguageReminderTime(text: string, now = dayjs()) {
  const value = text.trim();
  if (!value) return null;
  let result = now.second(0).millisecond(0);
  if (/后天/.test(value)) result = result.add(2, "day");
  else if (/明天|明日/.test(value)) result = result.add(1, "day");

  const monthDay = value.match(/(\d{1,2})\s*月\s*(\d{1,2})\s*日?/);
  if (monthDay) {
    const month = Number(monthDay[1]);
    const day = Number(monthDay[2]);
    if (month < 1 || month > 12 || day < 1) return null;
    const buildDate = (year: number) => {
      const firstDay = result
        .year(year)
        .month(month - 1)
        .date(1);
      return day <= firstDay.daysInMonth() ? firstDay.date(day) : null;
    };
    let dated = buildDate(now.year());
    if (!dated) return null;
    if (dated.isBefore(now, "day")) dated = buildDate(now.year() + 1);
    if (!dated) return null;
    result = dated;
  }
  const isoDate = value.match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
  if (isoDate) {
    const year = Number(isoDate[1]);
    const month = Number(isoDate[2]);
    const day = Number(isoDate[3]);
    if (month < 1 || month > 12 || day < 1) return null;
    const firstDay = result
      .year(year)
      .month(month - 1)
      .date(1);
    if (day > firstDay.daysInMonth()) return null;
    result = firstDay.date(day);
  }

  const time = value.match(/(\d{1,2})(?:\s*[点时]|[:：])(\d{1,2})?/);
  let hour = time ? Number(time[1]) : Number.NaN;
  const semanticMinute = /[点时]\s*半/.test(value)
    ? 30
    : /[点时]\s*一刻/.test(value)
      ? 15
      : /[点时]\s*三刻/.test(value)
        ? 45
        : null;
  if (time?.[2] && semanticMinute !== null) return null;
  const minute = time?.[2]
    ? Number(time[2])
    : semanticMinute === null
      ? 0
      : semanticMinute;
  if (Number.isNaN(hour)) {
    if (/今晚|晚上/.test(value)) hour = 20;
    else if (/下午/.test(value)) hour = 15;
    else if (/中午/.test(value)) hour = 12;
    else if (/上午|早上/.test(value)) hour = 9;
    else if (/凌晨/.test(value)) hour = 1;
    else if (/今天|明天|明日|后天|\d{1,2}\s*月\s*\d{1,2}/.test(value)) {
      hour = 9;
    } else {
      return null;
    }
  }
  if (/下午|晚上/.test(value) && hour < 12) hour += 12;
  if (/凌晨/.test(value) && hour === 12) hour = 0;
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
  result = result.hour(hour).minute(minute);
  if (
    !/(今天|明天|明日|后天|\d{4}[-/]|\d{1,2}\s*月)/.test(value) &&
    result.isBefore(now)
  ) {
    result = result.add(1, "day");
  }
  return result.isValid() ? result : null;
}

function sourceLabel(source: string) {
  return (
    {
      manual: "手动",
      chat: "聊天",
      cron: "定时",
      capability: "能力",
      watch_topic: "关注",
    }[source] || source
  );
}

function statusTone(status: ReminderStatus) {
  if (status === "done") return "success";
  if (status === "dismissed") return "muted";
  if (status === "snoozed") return "warning";
  return "info";
}

function priorityTone(priority: ReminderPriority) {
  if (priority === "high") return "warning";
  if (priority === "low") return "muted";
  return "default";
}

const TIME_KIND_LABEL: Record<TimeItem["kind"], string> = {
  reminder: "提醒",
  task: "任务",
  automation: "定时任务",
};

function timelineTime(item: TimeItem) {
  if (item.all_day) return "全天";
  const parsed = dayjs(item.start_at);
  return parsed.isValid() ? parsed.format("HH:mm") : "待定";
}

function TimelineIcon({ kind }: { kind: TimeItem["kind"] }) {
  if (kind === "task") return <ListChecks size={17} />;
  if (kind === "automation") return <Repeat2 size={17} />;
  return <Bell size={17} />;
}

function itemIsActive(item: ReminderItem) {
  return !["dismissed", "done", "snoozed"].includes(item.status);
}

function sortByDueOrUpdated(items: ReminderItem[]) {
  return [...items].sort((a, b) => {
    const aTime = dayjs(a.due_at || a.updated_at).valueOf();
    const bTime = dayjs(b.due_at || b.updated_at).valueOf();
    return aTime - bTime;
  });
}

interface ReminderCardProps {
  item: ReminderItem;
  actionId: string | null;
  onView: (item: ReminderItem) => void;
  onSnooze: (item: ReminderItem) => void;
  onUseful: (item: ReminderItem) => void;
  onDismiss: (item: ReminderItem) => void;
  onDone: (item: ReminderItem) => void;
}

function ReminderCard({
  item,
  actionId,
  onView,
  onSnooze,
  onUseful,
  onDismiss,
  onDone,
}: ReminderCardProps) {
  const busy = actionId === item.id;
  return (
    <article className={styles.reminderCard}>
      <div className={styles.cardTop}>
        <span className={styles.cardIcon} data-priority={item.priority}>
          <Bell size={18} />
        </span>
        <div className={styles.cardTitleBlock}>
          <strong>{item.title}</strong>
          <span>{item.summary || item.reason || "暂无摘要"}</span>
        </div>
      </div>

      <div className={styles.cardMeta}>
        <CapabilityChip tone={statusTone(item.status)}>
          {STATUS_LABEL[item.status]}
        </CapabilityChip>
        <CapabilityChip tone={priorityTone(item.priority)}>
          {PRIORITY_LABEL[item.priority]}
        </CapabilityChip>
        <CapabilityChip>{sourceLabel(item.source)}</CapabilityChip>
        <CapabilityChip title={item.due_at || undefined}>
          {formatTime(item.due_at)}
        </CapabilityChip>
        {(item.metadata.runtime as Record<string, unknown> | undefined)?.delivery_status === "unknown" && (
          <CapabilityChip tone="warning" title="通知结果尚未确认。为避免重复发送，已暂停再次通知；可选择稍后提醒。">
            通知结果待确认
          </CapabilityChip>
        )}
      </div>

      {item.reason ? (
        <p className={styles.cardReason}>原因：{item.reason}</p>
      ) : null}

      <div className={styles.cardActions}>
        <Tooltip title="查看摘要">
          <Button
            size="small"
            icon={<Eye size={14} />}
            onClick={() => onView(item)}
            aria-label={`查看提醒：${item.title}`}
          />
        </Tooltip>
        <Tooltip title="稍后 4 小时提醒">
          <Button
            size="small"
            icon={<Clock size={14} />}
            loading={busy}
            onClick={() => onSnooze(item)}
            aria-label={`稍后 4 小时提醒：${item.title}`}
          />
        </Tooltip>
        <Tooltip title="有用">
          <Button
            size="small"
            icon={<ThumbsUp size={14} />}
            loading={busy}
            onClick={() => onUseful(item)}
            aria-label={`标记有用：${item.title}`}
          />
        </Tooltip>
        <Tooltip title="不再提醒">
          <Button
            size="small"
            icon={<Ban size={14} />}
            loading={busy}
            onClick={() => onDismiss(item)}
            aria-label={`不再提醒：${item.title}`}
          />
        </Tooltip>
        <Tooltip title="完成">
          <Button
            size="small"
            icon={<CheckCircle size={14} />}
            loading={busy}
            onClick={() => onDone(item)}
            aria-label={`完成提醒：${item.title}`}
          />
        </Tooltip>
      </div>
    </article>
  );
}

export default function RemindersPage() {
  const { message } = useAppMessage();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { selectedAgent } = useAgentStore();
  const [activeTab, setActiveTab] = useState<ReminderTab>("today");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [actionId, setActionId] = useState<string | null>(null);
  const [items, setItems] = useState<ReminderItem[]>([]);
  const [timeline, setTimeline] = useState<TodayTimeline | null>(null);
  const [topics, setTopics] = useState<WatchTopic[]>([]);
  const [settings, setSettings] = useState<ReminderSettings | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [selectedItem, setSelectedItem] = useState<ReminderItem | null>(null);
  const [reminderModalOpen, setReminderModalOpen] = useState(false);
  const [topicModalOpen, setTopicModalOpen] = useState(false);
  const [naturalTimeInput, setNaturalTimeInput] = useState("");
  const [reminderForm] = Form.useForm();
  const [topicForm] = Form.useForm();
  const reminderDueAt = Form.useWatch("due_at", reminderForm);
  const loadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());

  const refresh = useCallback(async () => {
    const ownsState = loadGateRef.current.begin();
    setLoading(true);
    setLoadError("");
    try {
      const [nextItems, nextTimeline, nextTopics, nextSettings] = await Promise.all([
        reminderApi.listReminders(),
        timeApi.getToday(),
        reminderApi.listWatchTopics(),
        reminderApi.getSettings(),
      ]);
      if (!ownsState()) return;
      setItems(nextItems);
      setTimeline(nextTimeline);
      setTopics(nextTopics);
      setSettings(nextSettings);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load reminders:", error);
      setLoadError("提醒与今日时间线加载失败。已有提醒不会被清空。 ");
      message.error("提醒加载失败");
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    const loadGate = loadGateRef.current;
    const mutationScope = mutationScopeRef.current;
    setItems([]);
    setTimeline(null);
    setTopics([]);
    setSettings(null);
    setSettingsBusy(false);
    setSelectedItem(null);
    setReminderModalOpen(false);
    setTopicModalOpen(false);
    setSaving(false);
    setActionId(null);
    reminderForm.resetFields();
    topicForm.resetFields();
    void refresh();
    return () => {
      loadGate.invalidate();
      mutationScope.invalidate();
    };
  }, [refresh, reminderForm, selectedAgent, topicForm]);

  useEffect(() => {
    const reminderId = searchParams.get("reminder");
    const target = items.find((item) => item.id === reminderId);
    if (target) {
      setActiveTab("center");
      setSelectedItem(target);
    }
  }, [items, searchParams]);

  const handleNativePolicy = async (enabled: boolean) => {
    if (!settings) return;
    const ownsState = mutationScopeRef.current.capture();
    setSettingsBusy(true);
    try {
      const next = await reminderApi.setSettings({ ...settings,
        native_notification_policy: enabled ? "explicit_only" : "off",
      });
      if (ownsState()) setSettings(next);
    } catch {
      if (ownsState()) message.error("通知设置更新失败，请刷新确认当前状态");
    } finally {
      if (ownsState()) setSettingsBusy(false);
    }
  };

  const stats = useMemo(() => {
    const active = items.filter(itemIsActive).length;
    const snoozed = items.filter((item) => item.status === "snoozed").length;
    const done = items.filter((item) =>
      ["done", "dismissed"].includes(item.status),
    ).length;
    return { active, snoozed, done };
  }, [items]);

  const activeItems = useMemo(
    () => sortByDueOrUpdated(items.filter(itemIsActive)),
    [items],
  );
  const snoozedItems = useMemo(
    () => sortByDueOrUpdated(items.filter((item) => item.status === "snoozed")),
    [items],
  );
  const archivedItems = useMemo(
    () =>
      sortByDueOrUpdated(
        items.filter((item) => ["done", "dismissed"].includes(item.status)),
      ),
    [items],
  );
  const runAction = async (
    item: ReminderItem,
    action: () => Promise<ReminderItem>,
    success: string,
  ) => {
    const ownsState = mutationScopeRef.current.capture();
    setActionId(item.id);
    try {
      await action();
      if (!ownsState()) return;
      message.success(success);
      await refresh();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to update reminder:", error);
      message.error("提醒更新失败");
    } finally {
      if (ownsState()) setActionId(null);
    }
  };

  const handleCreateReminder = async () => {
    const ownsState = mutationScopeRef.current.capture();
    const values = await reminderForm.validateFields();
    if (!ownsState()) return;
    const payload: ReminderCreateRequest = {
      title: values.title,
      summary: values.summary || "",
      reason: values.reason || "",
      priority: values.priority || "normal",
      source: "manual",
      due_at: values.due_at ? values.due_at.toISOString() : null,
    };
    setSaving(true);
    try {
      await reminderApi.createReminder(payload);
      if (!ownsState()) return;
      message.success("提醒已创建");
      setReminderModalOpen(false);
      setNaturalTimeInput("");
      reminderForm.resetFields();
      await refresh();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to create reminder:", error);
      message.error("提醒创建失败");
    } finally {
      if (ownsState()) setSaving(false);
    }
  };

  const handleCreateTopic = async () => {
    const ownsState = mutationScopeRef.current.capture();
    const values = await topicForm.validateFields();
    if (!ownsState()) return;
    setSaving(true);
    try {
      await reminderApi.createWatchTopic({
        title: values.title,
        description: values.description || "",
        frequency: values.frequency || "daily",
      });
      if (!ownsState()) return;
      message.success("关注已添加");
      setTopicModalOpen(false);
      topicForm.resetFields();
      await refresh();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to create watch topic:", error);
      message.error("关注创建失败");
    } finally {
      if (ownsState()) setSaving(false);
    }
  };

  const handleTopicToggle = async (topicId: string, enabled: boolean) => {
    const ownsState = mutationScopeRef.current.capture();
    try {
      await reminderApi.updateWatchTopic(topicId, { enabled });
      if (ownsState()) await refresh();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to update watch topic:", error);
      message.error("关注更新失败");
    }
  };

  const renderCards = (list: ReminderItem[], emptyText: string) => {
    if (list.length === 0) {
      return (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} />
      );
    }
    return (
      <div className={styles.reminderGrid}>
        {list.map((item) => (
          <ReminderCard
            key={item.id}
            item={item}
            actionId={actionId}
            onView={setSelectedItem}
            onSnooze={(target) =>
              void runAction(
                target,
                () =>
                  reminderApi.snoozeReminder(
                    target.id,
                    dayjs().add(4, "hour").toISOString(),
                  ),
                "已稍后提醒",
              )
            }
            onUseful={(target) =>
              void runAction(
                target,
                () =>
                  reminderApi.updateReminder(target.id, {
                    status: "read",
                    metadata: {
                      ...target.metadata,
                      feedback: "useful",
                    },
                  }),
                "已记录",
              )
            }
            onDismiss={(target) =>
              Modal.confirm({
                title: `不再提醒“${target.title}”？`,
                content: "这条提醒会进入已忽略记录，不会再出现在待办列表中。",
                okText: "不再提醒",
                cancelText: "取消",
                okButtonProps: { danger: true },
                onOk: () =>
                  runAction(
                    target,
                    () => reminderApi.dismissReminder(target.id),
                    "已忽略",
                  ),
              })
            }
            onDone={(target) =>
              Modal.confirm({
                title: `将“${target.title}”标记为完成？`,
                content: "完成后会移入历史记录，不再作为待办提醒。",
                okText: "标记完成",
                cancelText: "取消",
                onOk: () =>
                  runAction(
                    target,
                    () => reminderApi.completeReminder(target.id),
                    "已完成",
                  ),
              })
            }
          />
        ))}
      </div>
    );
  };

  const content = () => {
    if (loading) {
      return (
        <div className={styles.loadingState}>
          <Spin />
        </div>
      );
    }

    if (loadError) {
      return (
        <AppState
          compact
          tone="error"
          title="无法加载今日与提醒"
          description={loadError}
          actions={<Button onClick={() => void refresh()}>重新加载</Button>}
        />
      );
    }

    if (activeTab === "topics") {
      return (
        <div className={styles.topicList}>
          {topics.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无关注"
            />
          ) : (
            topics.map((topic) => (
              <article key={topic.id} className={styles.topicCard}>
                <div>
                  <strong>{topic.title}</strong>
                  <span>{topic.description || "暂无描述"}</span>
                </div>
                <div className={styles.topicMeta}>
                  <CapabilityChip>{topic.frequency}</CapabilityChip>
                  <Switch
                    size="small"
                    checked={topic.enabled}
                    onChange={(enabled) =>
                      void handleTopicToggle(topic.id, enabled)
                    }
                  />
                </div>
              </article>
            ))
          )}
        </div>
      );
    }

    if (activeTab === "today") {
      const timelineItems = timeline?.items || [];
      return (
        <div className={styles.timelineShell}>
          <div className={styles.timelineHeader}>
            <div>
              <span className={styles.eyebrow}>
                {dayjs(timeline?.date).format("M月D日 dddd")}
              </span>
              <h2>今天发生什么</h2>
              <p>提醒、任务和定时执行保留各自来源。</p>
            </div>
          </div>
          {timelineItems.length === 0 ? (
            <AppState
              compact
              title="今天没有待处理安排"
              description="这里会汇总提醒、进行中的任务和自动化。现在可以继续最近工作，或创建一条具体提醒。"
              actions={
                <>
                  <Button onClick={() => navigate("/chat")}>
                    继续最近工作
                  </Button>
                  <Button
                    type="primary"
                    onClick={() => setReminderModalOpen(true)}
                  >
                    创建提醒
                  </Button>
                </>
              }
            />
          ) : (
            <div className={styles.timeline}>
              {timelineItems.map((item) => (
                <article
                  key={item.id}
                  className={styles.timelineItem}
                  data-kind={item.kind}
                >
                  <time dateTime={item.start_at || undefined}>
                    {timelineTime(item)}
                  </time>
                  <span className={styles.timelineRail}>
                    <span className={styles.timelineIcon}>
                      <TimelineIcon kind={item.kind} />
                    </span>
                  </span>
                  <div className={styles.timelineBody}>
                    <div className={styles.timelineTitleRow}>
                      <strong>{item.title}</strong>
                      <CapabilityChip tone={item.blocking ? "info" : "muted"}>
                        {TIME_KIND_LABEL[item.kind]}
                      </CapabilityChip>
                    </div>
                    <span>
                      {item.summary ||
                        `${item.source_label} · ${item.status || "待处理"}`}
                    </span>
                    <div className={styles.timelineMeta}>
                      <span>{item.source_label}</span>
                      {item.end_at && !item.all_day ? (
                        <span>至 {dayjs(item.end_at).format("HH:mm")}</span>
                      ) : null}
                      {item.blocking ? <span>占用时间</span> : null}
                      {item.action_path ? (
                        <button
                          type="button"
                          onClick={() => navigate(item.action_path!)}
                        >
                          查看来源
                        </button>
                      ) : null}
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </div>
      );
    }

    return (
      <div className={styles.centerStack}>
        <section>
          <h2>当前提醒</h2>
          {renderCards(activeItems, "暂无当前提醒")}
        </section>
        {snoozedItems.length > 0 ? (
          <section>
            <h2>稍后提醒</h2>
            {renderCards(snoozedItems, "暂无稍后提醒")}
          </section>
        ) : null}
        {archivedItems.length > 0 ? (
          <section>
            <h2>已完成 / 已忽略</h2>
            {renderCards(archivedItems, "暂无历史提醒")}
          </section>
        ) : null}
      </div>
    );
  };

  return (
    <CapabilityPage
      section="控制"
      title="今日安排"
      description="用一条来源清晰的时间线，看见提醒、任务和定时执行。"
      className={styles.remindersPage}
      stats={[
        {
          label: "提醒",
          value: timeline?.counts.reminder || stats.active,
          tone: "warning",
        },
        {
          label: "任务",
          value: timeline?.counts.task || 0,
          tone: "success",
        },
        {
          label: "定时任务",
          value: timeline?.counts.automation || 0,
          tone: "muted",
        },
      ]}
      actions={
        <>
          <Button icon={<RefreshCw size={15} />} onClick={() => void refresh()}>
            刷新
          </Button>
          <Button
            icon={<Plus size={15} />}
            onClick={() => setTopicModalOpen(true)}
          >
            添加关注
          </Button>
          <Button
            type={
              !loading &&
              !loadError &&
              activeTab === "today" &&
              (timeline?.items.length ?? 0) === 0
                ? "default"
                : "primary"
            }
            icon={<Plus size={15} />}
            onClick={() => setReminderModalOpen(true)}
          >
            新提醒
          </Button>
        </>
      }
      toolbar={
        <>
        <Segmented
          value={activeTab}
          onChange={(value) => setActiveTab(value as ReminderTab)}
          options={[
            {
              label: "今日安排",
              value: "today",
              icon: <CalendarDays size={14} />,
            },
            {
              label: "提醒",
              value: "center",
              icon: <Bell size={14} />,
            },
            {
              label: "关注",
              value: "topics",
              icon: <Eye size={14} />,
            },
          ]}
        />
        <Tooltip title="开启后，澜台在后台时发送到期提醒；系统可能需要允许通知。">
          <span>
            桌面到期通知{" "}
            <Switch size="small" aria-label="桌面到期通知"
              checked={settings?.native_notification_policy === "explicit_only"}
              disabled={!settings} loading={settingsBusy}
              onChange={(enabled) => void handleNativePolicy(enabled)}
            />
          </span>
        </Tooltip>
        </>
      }
    >
      {content()}

      <Modal
        title="新提醒"
        open={reminderModalOpen}
        onCancel={() => {
          setReminderModalOpen(false);
          setNaturalTimeInput("");
        }}
        onOk={() => void handleCreateReminder()}
        okText="创建提醒"
        cancelText="取消"
        okButtonProps={{ loading: saving }}
        forceRender
        destroyOnHidden
      >
        <Form form={reminderForm} layout="vertical">
          <Form.Item name="title" label="标题" rules={[{ required: true }]}>
            <Input maxLength={80} placeholder="例如：提交季度复盘" />
          </Form.Item>
          <Form.Item label="时间描述">
            <Input
              value={naturalTimeInput}
              placeholder="例如：明天下午 3 点、9 月 12 日上午 10 点"
              onChange={(event) => {
                const value = event.target.value;
                setNaturalTimeInput(value);
                const parsed = parseNaturalLanguageReminderTime(value);
                if (parsed) reminderForm.setFieldValue("due_at", parsed);
              }}
            />
          </Form.Item>
          <Form.Item
            name="due_at"
            label="确认时间"
            rules={[{ required: true, message: "请选择提醒时间" }]}
            extra={
              <span className={styles.timePreview}>
                {reminderDueAt
                  ? `${reminderDueAt.format("YYYY年M月D日 dddd HH:mm")} · ${Intl.DateTimeFormat().resolvedOptions().timeZone}`
                  : "请选择时间；保存前会按本机时区确认"}
              </span>
            }
          >
            <DatePicker
              showTime
              format="YYYY-MM-DD HH:mm"
              className={styles.fullWidth}
              placeholder="选择日期和时间"
            />
          </Form.Item>
          <details className={styles.advancedFields}>
            <summary>更多选项</summary>
            <Form.Item name="summary" label="补充说明">
              <Input.TextArea rows={2} maxLength={240} />
            </Form.Item>
            <Form.Item name="reason" label="提醒原因">
              <Input.TextArea rows={2} maxLength={240} />
            </Form.Item>
            <Form.Item name="priority" label="优先级" initialValue="normal">
              <Select
                options={[
                  { label: "普通", value: "normal" },
                  { label: "高价值", value: "high" },
                  { label: "低", value: "low" },
                ]}
              />
            </Form.Item>
          </details>
        </Form>
      </Modal>

      <Modal
        title="添加关注"
        open={topicModalOpen}
        onCancel={() => setTopicModalOpen(false)}
        onOk={() => void handleCreateTopic()}
        okButtonProps={{ loading: saving }}
        forceRender
        destroyOnHidden
      >
        <Form form={topicForm} layout="vertical">
          <Form.Item name="title" label="标题" rules={[{ required: true }]}>
            <Input maxLength={80} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={3} maxLength={320} />
          </Form.Item>
          <Form.Item name="frequency" label="频率" initialValue="daily">
            <Select
              options={[
                { label: "每日", value: "daily" },
                { label: "每周", value: "weekly" },
                { label: "手动", value: "manual" },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={selectedItem?.title}
        open={Boolean(selectedItem)}
        onClose={() => setSelectedItem(null)}
        width={420}
      >
        {selectedItem ? (
          <div className={styles.drawerBody}>
            <p>{selectedItem.summary || "暂无摘要"}</p>
            <dl>
              <dt>原因</dt>
              <dd>{selectedItem.reason || "暂无"}</dd>
              <dt>来源</dt>
              <dd>{sourceLabel(selectedItem.source)}</dd>
              <dt>时间</dt>
              <dd>{formatTime(selectedItem.due_at)}</dd>
              <dt>状态</dt>
              <dd>{STATUS_LABEL[selectedItem.status]}</dd>
            </dl>
          </div>
        ) : null}
      </Drawer>
    </CapabilityPage>
  );
}
