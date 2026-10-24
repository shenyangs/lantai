import { useEffect, useRef, useState } from "react";
import {
  Button,
  Form,
  InputNumber,
  Select,
  Switch,
} from "@/vendor/lantaiDesign";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { TimePicker } from "antd";
import dayjs from "dayjs";
import customParseFormat from "dayjs/plugin/customParseFormat";
import { useTranslation } from "react-i18next";
import { Activity, CalendarClock, Clock3 } from "lucide-react";
import api from "../../../api";
import { useAgentStore } from "../../../stores/agentStore";
import type { HeartbeatConfig } from "../../../api/types/heartbeat";
import type { ChatSpec } from "../../../api/types";
import { chatApi } from "../../../api/modules/chat";
import { runtimeApi } from "../../../api/modules/runtime";
import type { RuntimeRunSummary } from "../../../api/types/runtime";
import { parseEvery, serializeEvery, type EveryUnit } from "./parseEvery";
import { PageHeader } from "@/components/PageHeader";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";
import styles from "./index.module.less";

dayjs.extend(customParseFormat);

const TIME_FORMAT = "HH:mm";

/** TimePicker that uses "HH:mm" string as value for Form. */
function TimePickerHHmm({
  value,
  onChange,
}: {
  value?: string | null;
  onChange?: (s: string) => void;
}) {
  const strVal =
    typeof value === "string" ? value : Array.isArray(value) ? value[0] : null;
  return (
    <TimePicker
      format={TIME_FORMAT}
      value={strVal ? dayjs(strVal, TIME_FORMAT) : null}
      onChange={(_, str) => {
        const s = typeof str === "string" ? str : str?.[0];
        if (s) onChange?.(s);
      }}
      minuteStep={15}
      needConfirm={false}
      style={{ width: "100%" }}
    />
  );
}

/** Form values: API shape plus flattened fields for interval and time. */
type HeartbeatFormValues = Omit<HeartbeatConfig, "every"> & {
  every?: string;
  everyNumber?: number;
  everyUnit?: EveryUnit;
  useActiveHours?: boolean;
  activeHoursStart?: string;
  activeHoursEnd?: string;
};

const TARGET_OPTIONS = [
  { value: "main", labelKey: "heartbeat.targetMain" },
  { value: "last", labelKey: "heartbeat.targetLast" },
];

const EVERY_UNIT_OPTIONS: { value: EveryUnit; labelKey: string }[] = [
  { value: "m", labelKey: "heartbeat.unitMinutes" },
  { value: "h", labelKey: "heartbeat.unitHours" },
];

const RECOMMENDED_HEARTBEAT_VALUES: HeartbeatFormValues = {
  enabled: true,
  everyNumber: 6,
  everyUnit: "h",
  target: "main",
  useActiveHours: true,
  activeHoursStart: "08:00",
  activeHoursEnd: "22:00",
};

export function heartbeatReasonLabel(reason: string, enabled: boolean) {
  if (!enabled) return "心跳已关闭，不会在后台检查";
  return (
    {
      outside_active_hours: "当前在活跃时段之外，本轮保持安静",
      missing_query_file: "未找到 HEARTBEAT.md，本轮没有执行检查",
      empty_query_file: "HEARTBEAT.md 为空，本轮没有执行检查",
      ran_without_dispatch: "已完成检查，没有向外部渠道发送消息",
      dispatched_last_target: "已完成检查，并把结果送到最近使用的渠道",
      timeout: "最近检查超时，需要查看运行记录",
      cancelled: "最近检查已取消",
      exception: "最近检查遇到错误，需要查看运行记录",
    }[reason] ||
    (reason ? `最近结果：${reason}` : "尚无检查记录，下一次运行后会显示原因")
  );
}

function heartbeatTime(value?: string | null) {
  if (!value) return "尚未检查";
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("M月D日 HH:mm") : "时间未知";
}

function heartbeatResultLabel(run: RuntimeRunSummary | null) {
  if (!run) return "尚无结果";
  const labels: Record<string, string> = {
    completed: "检查完成",
    failed: "检查失败",
    stalled: "检查停滞",
    interrupted: "检查已停止",
    running: "正在检查",
    queued: "等待检查",
    skipped: "本轮跳过",
  };
  return (
    labels[run.status] || run.last_event_summary || run.summary || "结果已记录"
  );
}

function estimatedNextHeartbeat(
  enabled: boolean,
  every: string,
  lastRun: RuntimeRunSummary | null,
) {
  if (!enabled) return null;
  const parts = parseEvery(every || "6h");
  const amount = Math.max(1, parts.number);
  const base = dayjs(
    lastRun?.ended_at ||
      lastRun?.heartbeat_at ||
      lastRun?.started_at ||
      undefined,
  );
  const next = (base.isValid() ? base : dayjs()).add(
    amount,
    parts.unit === "m" ? "minute" : "hour",
  );
  return next.isBefore(dayjs())
    ? dayjs().add(amount, parts.unit === "m" ? "minute" : "hour")
    : next;
}

function hasPendingTurn(chat: ChatSpec) {
  const pendingTurn = chat.meta?.pending_turn;
  return !!pendingTurn && typeof pendingTurn === "object";
}

function HeartbeatPage() {
  const { t } = useTranslation();
  const { selectedAgent } = useAgentStore();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [enabledValue, setEnabledValue] = useState(false);
  const [runningChatCount, setRunningChatCount] = useState(0);
  const [lastHeartbeatRun, setLastHeartbeatRun] =
    useState<RuntimeRunSummary | null>(null);
  const [lastHeartbeatReason, setLastHeartbeatReason] = useState("");
  const [heartbeatEvery, setHeartbeatEvery] = useState("6h");
  const [form] = Form.useForm<HeartbeatFormValues>();
  const { message } = useAppMessage();
  const loadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());

  const fetchConfig = async () => {
    const ownsState = loadGateRef.current.begin();
    setLoading(true);
    try {
      const [data, chats, heartbeatRuns] = await Promise.all([
        api.getHeartbeatConfig(),
        chatApi.listChats().catch(() => []),
        runtimeApi
          .listRuns({ source: "heartbeat", limit: 1, agentId: selectedAgent })
          .catch(() => ({ total: 0, items: [] })),
      ]);
      if (!ownsState()) return;
      const everyParts = parseEvery(data.every ?? "6h");
      const nextValues = {
        enabled: data.enabled ?? false,
        everyNumber: everyParts.number,
        everyUnit: everyParts.unit,
        target: data.target ?? "main",
        useActiveHours: !!data.activeHours,
        activeHoursStart: data.activeHours?.start ?? "08:00",
        activeHoursEnd: data.activeHours?.end ?? "22:00",
      };
      form.setFieldsValue({
        ...nextValues,
      });
      setEnabledValue(!!nextValues.enabled);
      setHeartbeatEvery(data.every || "6h");
      const latestHeartbeat = heartbeatRuns.items[0] || null;
      setLastHeartbeatRun(latestHeartbeat);
      if (latestHeartbeat) {
        const events = await runtimeApi
          .getRunEvents(latestHeartbeat.id, 0, {
            limit: 100,
            agentId: selectedAgent,
          })
          .catch(() => null);
        if (!ownsState()) return;
        const settled = [...(events?.events || [])]
          .reverse()
          .find(
            (event) =>
              event.type === "heartbeat.run.settled" ||
              event.payload?.legacy_kind === "heartbeat.run.settled",
          );
        setLastHeartbeatReason(
          String(settled?.payload?.result_reason || "").trim(),
        );
      } else {
        setLastHeartbeatReason("");
      }
      setRunningChatCount(
        Array.isArray(chats)
          ? chats.filter(
              (chat) => chat.status === "running" || hasPendingTurn(chat),
            ).length
          : 0,
      );
    } catch (e) {
      if (!ownsState()) return;
      console.error("Failed to load heartbeat config:", e);
      message.error(t("heartbeat.loadFailed"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  };

  const buildRequestBody = (values: HeartbeatFormValues): HeartbeatConfig => {
    const every =
      values.everyNumber != null && values.everyUnit
        ? serializeEvery({
            number: values.everyNumber,
            unit: values.everyUnit,
          })
        : "6h";
    return {
      enabled: values.enabled ?? false,
      every,
      target: values.target ?? "main",
      activeHours:
        values.useActiveHours &&
        values.activeHoursStart &&
        values.activeHoursEnd
          ? {
              start: values.activeHoursStart,
              end: values.activeHoursEnd,
            }
          : undefined,
    };
  };

  useEffect(() => {
    setSaving(false);
    setRunningChatCount(0);
    setEnabledValue(false);
    form.resetFields();
    void fetchConfig();
    return () => {
      loadGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAgent]);

  const onFinish = async (values: HeartbeatFormValues) => {
    const ownsState = mutationScopeRef.current.capture();
    const body = buildRequestBody(values);
    setSaving(true);
    try {
      await api.updateHeartbeatConfig(body);
      if (!ownsState()) return;
      setEnabledValue(body.enabled);
      setHeartbeatEvery(body.every);
      message.success(t("heartbeat.saveSuccess"));
    } catch (e) {
      if (!ownsState()) return;
      console.error("Failed to save heartbeat config:", e);
      message.error(t("heartbeat.saveFailed"));
    } finally {
      if (ownsState()) setSaving(false);
    }
  };

  const applyRecommendedConfig = async () => {
    const ownsState = mutationScopeRef.current.capture();
    form.setFieldsValue(RECOMMENDED_HEARTBEAT_VALUES);
    setEnabledValue(true);
    setSaving(true);
    try {
      await api.updateHeartbeatConfig(
        buildRequestBody(RECOMMENDED_HEARTBEAT_VALUES),
      );
      if (!ownsState()) return;
      setHeartbeatEvery(buildRequestBody(RECOMMENDED_HEARTBEAT_VALUES).every);
      message.success(
        "已启用推荐心跳配置：6 小时一次，静默运行，仅在活跃时段检查。",
      );
    } catch (e) {
      if (!ownsState()) return;
      console.error("Failed to apply recommended heartbeat config:", e);
      message.error(t("heartbeat.saveFailed"));
    } finally {
      if (ownsState()) setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className={styles.heartbeatPage}>
        <PageHeader
          items={[{ title: t("nav.control") }, { title: t("heartbeat.title") }]}
          description={t("heartbeat.description")}
        />
        <span className={styles.description}>{t("common.loading")}</span>
      </div>
    );
  }

  const nextHeartbeat = estimatedNextHeartbeat(
    enabledValue,
    heartbeatEvery,
    lastHeartbeatRun,
  );

  return (
    <div className={styles.heartbeatPage}>
      <PageHeader
        items={[{ title: t("nav.control") }, { title: t("heartbeat.title") }]}
        description={t("heartbeat.description")}
      />
      <div className={styles.heartbeatContent}>
        <section className={styles.statusPanel}>
          <div className={styles.statusPrimary}>
            <div className={styles.statusIcon} aria-hidden="true">
              <Activity size={20} strokeWidth={1.8} />
            </div>
            <div className={styles.statusCopy}>
              <div className={styles.statusLabel}>
                <span
                  className={
                    enabledValue ? styles.statusDotActive : styles.statusDot
                  }
                />
                {enabledValue ? "心跳已开启" : "心跳未开启"}
              </div>
              <h2>
                {enabledValue
                  ? "澜台会按当前节奏定期检查"
                  : "让澜台定期回来看看"}
              </h2>
              <p>
                适合持续跟进长任务、轻提醒和静默巡检。默认保持关闭，由你决定何时启用。
              </p>
              {runningChatCount > 0 && !enabledValue ? (
                <div className={styles.contextNotice}>
                  检测到 {runningChatCount}
                  个进行中的聊天任务，现在开启会更有帮助。
                </div>
              ) : null}
            </div>
          </div>
          <div className={styles.recommendation}>
            <div>
              <h3>建议从这个节奏开始</h3>
              <p>每 6 小时 · 08:00–22:00 · 静默运行</p>
            </div>
            <Button
              type="primary"
              loading={saving}
              onClick={() => void applyRecommendedConfig()}
            >
              采用推荐配置
            </Button>
          </div>
        </section>

        <section className={styles.runEvidencePanel} aria-label="心跳运行状态">
          <div>
            <span>上次检查</span>
            <strong>
              {heartbeatTime(
                lastHeartbeatRun?.ended_at ||
                  lastHeartbeatRun?.heartbeat_at ||
                  lastHeartbeatRun?.started_at,
              )}
            </strong>
          </div>
          <div>
            <span>最近结果</span>
            <strong>{heartbeatResultLabel(lastHeartbeatRun)}</strong>
          </div>
          <div>
            <span>下次检查</span>
            <strong>
              {nextHeartbeat
                ? `预计 ${nextHeartbeat.format("M月D日 HH:mm")}`
                : "开启并保存后安排"}
            </strong>
          </div>
          <div className={styles.quietReason}>
            <span>安静原因</span>
            <strong>
              {heartbeatReasonLabel(lastHeartbeatReason, enabledValue)}
            </strong>
          </div>
        </section>

        <div className={styles.guideRow}>
          <div className={styles.guideItem}>
            <Clock3 size={18} strokeWidth={1.8} aria-hidden="true" />
            <div>
              <strong>适合做什么</strong>
              <span>长任务跟进、轻提醒、静默巡检和保持工作流在线。</span>
            </div>
          </div>
          <div className={styles.guideItem}>
            <CalendarClock size={18} strokeWidth={1.8} aria-hidden="true" />
            <div>
              <strong>和定时任务的区别</strong>
              <span>定时任务到点执行；心跳只会定期检查、提醒和续接。</span>
            </div>
          </div>
        </div>

        <section className={styles.settingsPanel}>
          <Form
            form={form}
            layout="vertical"
            onFinish={onFinish}
            onValuesChange={(_, allValues) => {
              setEnabledValue(!!allValues.enabled);
            }}
            initialValues={{
              enabled: false,
              everyNumber: 6,
              everyUnit: "h",
              target: "main",
              useActiveHours: false,
              activeHoursStart: "08:00",
              activeHoursEnd: "22:00",
            }}
          >
            <div className={styles.settingsHeader}>
              <div>
                <h2>运行设置</h2>
                <p>决定检查频率、回复位置和允许运行的时间。</p>
              </div>
              <div className={styles.enableControl}>
                <div>
                  <strong>{enabledValue ? "正在运行" : "保持关闭"}</strong>
                  <span>
                    {enabledValue ? "保存后按此配置执行" : "不会在后台检查"}
                  </span>
                </div>
                <Form.Item name="enabled" valuePropName="checked" noStyle>
                  <Switch aria-label={t("heartbeat.enabled")} />
                </Form.Item>
              </div>
            </div>

            <div className={styles.settingsBody}>
              <div className={styles.primarySettingsGrid}>
                <Form.Item
                  label={t("heartbeat.every")}
                  required
                  className={styles.everyField}
                >
                  <div className={styles.everyRow}>
                    <Form.Item
                      name="everyNumber"
                      rules={[
                        {
                          required: true,
                          message: t("heartbeat.everyRequired"),
                        },
                        {
                          type: "number",
                          min: 1,
                          message: t("heartbeat.everyMin"),
                        },
                      ]}
                      noStyle
                    >
                      <InputNumber min={1} className={styles.everyNumber} />
                    </Form.Item>
                    <Form.Item name="everyUnit" noStyle>
                      <Select
                        options={EVERY_UNIT_OPTIONS.map((opt) => ({
                          value: opt.value,
                          label: t(opt.labelKey),
                        }))}
                        className={styles.everyUnit}
                      />
                    </Form.Item>
                  </div>
                </Form.Item>

                <Form.Item
                  name="target"
                  label={t("heartbeat.target")}
                  rules={[{ required: true }]}
                >
                  <Select
                    options={TARGET_OPTIONS.map((opt) => ({
                      value: opt.value,
                      label: t(opt.labelKey),
                    }))}
                  />
                </Form.Item>
              </div>

              <div className={styles.activeHoursSection}>
                <div className={styles.activeHoursHeader}>
                  <div>
                    <strong>{t("heartbeat.activeHours")}</strong>
                    <span>限制后台检查时段，避免在休息时间运行。</span>
                  </div>
                  <Form.Item
                    name="useActiveHours"
                    valuePropName="checked"
                    noStyle
                  >
                    <Switch aria-label={t("heartbeat.activeHours")} />
                  </Form.Item>
                </div>

                <Form.Item
                  noStyle
                  shouldUpdate={(prev, cur) =>
                    prev.useActiveHours !== cur.useActiveHours
                  }
                >
                  {({ getFieldValue }) =>
                    getFieldValue("useActiveHours") ? (
                      <div className={styles.activeHoursRow}>
                        <Form.Item
                          name="activeHoursStart"
                          label={t("heartbeat.activeStart")}
                        >
                          <TimePickerHHmm />
                        </Form.Item>
                        <Form.Item
                          name="activeHoursEnd"
                          label={t("heartbeat.activeEnd")}
                        >
                          <TimePickerHHmm />
                        </Form.Item>
                      </div>
                    ) : null
                  }
                </Form.Item>
              </div>

              <div className={styles.formActions}>
                <Button type="primary" htmlType="submit" loading={saving}>
                  {t("common.save")}
                </Button>
              </div>
            </div>
          </Form>
        </section>
      </div>
    </div>
  );
}

export default HeartbeatPage;
