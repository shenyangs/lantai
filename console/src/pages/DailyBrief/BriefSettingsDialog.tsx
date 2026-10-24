import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Pause, Play, Plus, ShieldCheck, X } from "lucide-react";
import { dailyBriefApi } from "@/api/modules/dailyBrief";
import { reminderApi } from "@/api/modules/reminder";
import type {
  BriefGrant,
  BriefModelCapability,
  BriefSubscription,
  BriefTopicProjection,
  BriefWorkspaceSnapshot,
} from "@/api/types/dailyBrief";
import styles from "./BriefSettingsDialog.module.less";

type Mode = "preview" | "daily";

interface BriefSettingsDialogProps {
  snapshot: BriefWorkspaceSnapshot;
  onClose: () => void;
  onChanged: () => Promise<void>;
}

function actionId(prefix: string): string {
  const suffix =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

function dateInTimezone(timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(
    parts.map((part) => [part.type, part.value]),
  );
  return `${values.year}-${values.month}-${values.day}`;
}

function isValidIanaTimezone(timezone: string): boolean {
  try {
    new Intl.DateTimeFormat("en", { timeZone: timezone.trim() }).format();
    return Boolean(timezone.trim());
  } catch {
    return false;
  }
}

function defaultTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
  } catch {
    return "Asia/Shanghai";
  }
}

function topicPurpose(topic: BriefTopicProjection): string {
  return (
    topic.brief_purpose ||
    topic.description ||
    `持续关注“${topic.title}”的公开变化，并判断它与当前工作的关系`
  );
}

function SubscriptionEditor({
  subscription,
  nativeNotificationAvailable,
  onChanged,
  onMessage,
}: {
  subscription: BriefSubscription;
  nativeNotificationAvailable: boolean;
  onChanged: () => Promise<void>;
  onMessage: (value: string) => void;
}) {
  const [timezone, setTimezone] = useState(subscription.timezone);
  const [localTime, setLocalTime] = useState(
    subscription.local_time.slice(0, 5),
  );
  const [homeVisible, setHomeVisible] = useState(subscription.home_visible);
  const [nativeNotifications, setNativeNotifications] = useState(
    subscription.native_notifications,
  );
  const [quietHours, setQuietHours] = useState(
    Boolean(subscription.quiet_hours_start && subscription.quiet_hours_end),
  );
  const [quietStart, setQuietStart] = useState(
    subscription.quiet_hours_start?.slice(0, 5) || "22:00",
  );
  const [quietEnd, setQuietEnd] = useState(
    subscription.quiet_hours_end?.slice(0, 5) || "08:00",
  );
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTimezone(subscription.timezone);
    setLocalTime(subscription.local_time.slice(0, 5));
    setHomeVisible(subscription.home_visible);
    setNativeNotifications(subscription.native_notifications);
    setQuietHours(
      Boolean(subscription.quiet_hours_start && subscription.quiet_hours_end),
    );
    setQuietStart(subscription.quiet_hours_start?.slice(0, 5) || "22:00");
    setQuietEnd(subscription.quiet_hours_end?.slice(0, 5) || "08:00");
  }, [subscription]);

  const update = async (
    changes: Omit<
      Parameters<typeof dailyBriefApi.updateSubscription>[1],
      "expected_revision" | "action_id"
    >,
    success: string,
  ) => {
    if (
      changes.timezone !== undefined &&
      !isValidIanaTimezone(changes.timezone)
    ) {
      onMessage("请输入有效的 IANA 时区，例如 Asia/Shanghai。");
      return;
    }
    setBusy(true);
    onMessage("");
    try {
      await dailyBriefApi.updateSubscription(subscription.id, {
        ...changes,
        expected_revision: subscription.revision,
        action_id: actionId("brief-subscription-update"),
      });
      await onChanged();
      onMessage(success);
    } catch (reason) {
      onMessage(errorMessage(reason, "每日简报设置未能保存"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className={styles.subscriptionCard}>
      <header>
        <div>
          <strong>
            {subscription.status === "active"
              ? "每日简报已开启"
              : "每日简报已暂停"}
          </strong>
          <span>水位只在成功检查后前移；设备睡眠不会被记作按时完成。</span>
        </div>
        <button
          type="button"
          className={styles.quietButton}
          disabled={busy || subscription.status === "archived"}
          onClick={() =>
            void update(
              {
                status: subscription.status === "active" ? "paused" : "active",
              },
              subscription.status === "active"
                ? "每日简报已暂停"
                : "每日简报已恢复",
            )
          }
        >
          {subscription.status === "active" ? (
            <Pause size={14} />
          ) : (
            <Play size={14} />
          )}
          {subscription.status === "active" ? "暂停" : "恢复"}
        </button>
      </header>
      <div className={styles.formGrid}>
        <label>
          IANA 时区
          <input
            value={timezone}
            onChange={(event) => setTimezone(event.target.value)}
          />
        </label>
        <label>
          每日时间
          <input
            type="time"
            value={localTime}
            onChange={(event) => setLocalTime(event.target.value)}
          />
        </label>
      </div>
      <div className={styles.checkList}>
        <label>
          <input
            type="checkbox"
            checked={homeVisible}
            onChange={(event) => setHomeVisible(event.target.checked)}
          />
          在首页显示轻量入口卡片
        </label>
        <label>
          <input
            type="checkbox"
            checked={nativeNotifications}
            disabled={!nativeNotificationAvailable}
            onChange={(event) => setNativeNotifications(event.target.checked)}
          />
          原生通知（单独同意）
        </label>
        {!nativeNotificationAvailable ? (
          <small>当前构建尚未开放原生通知。</small>
        ) : null}
        <label>
          <input
            type="checkbox"
            checked={quietHours}
            onChange={(event) => setQuietHours(event.target.checked)}
          />
          设置安静时段
        </label>
      </div>
      {quietHours ? (
        <div className={styles.formGrid}>
          <label>
            安静开始
            <input
              type="time"
              value={quietStart}
              onChange={(event) => setQuietStart(event.target.value)}
            />
          </label>
          <label>
            安静结束
            <input
              type="time"
              value={quietEnd}
              onChange={(event) => setQuietEnd(event.target.value)}
            />
          </label>
        </div>
      ) : null}
      <footer>
        <button
          type="button"
          className={styles.primaryButton}
          disabled={busy || !timezone.trim() || !localTime}
          onClick={() =>
            void update(
              {
                timezone: timezone.trim(),
                local_time: localTime,
                home_visible: homeVisible,
                native_notifications: nativeNotifications,
                quiet_hours_start: quietHours ? quietStart : null,
                quiet_hours_end: quietHours ? quietEnd : null,
              },
              "每日简报设置已保存",
            )
          }
        >
          {busy ? "正在保存" : "保存调度设置"}
        </button>
      </footer>
    </article>
  );
}

export default function BriefSettingsDialog({
  snapshot,
  onClose,
  onChanged,
}: BriefSettingsDialogProps) {
  const confirmedTopicIds = useMemo(
    () =>
      snapshot.topics
        .filter((topic) => topic.enabled && topic.brief_status === "confirmed")
        .map((topic) => topic.id),
    [snapshot.topics],
  );
  const [selectedTopicIds, setSelectedTopicIds] =
    useState<string[]>(confirmedTopicIds);
  const [purposeDrafts, setPurposeDrafts] = useState<Record<string, string>>(
    () =>
      Object.fromEntries(
        snapshot.topics.map((topic) => [topic.id, topicPurpose(topic)]),
      ),
  );
  const [topicTitle, setTopicTitle] = useState("");
  const [topicDescription, setTopicDescription] = useState("");
  const [timezone] = useState(defaultTimezone);
  const [localTime, setLocalTime] = useState("08:00");
  const [homeVisible, setHomeVisible] = useState(true);
  const [nativeNotifications, setNativeNotifications] = useState(false);
  const [explicitConsent, setExplicitConsent] = useState(false);
  const [memoryContextConsent, setMemoryContextConsent] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<string | null>(null);
  const [showTopics, setShowTopics] = useState(false);
  const [showMore, setShowMore] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [capability, setCapability] = useState<BriefModelCapability | null>(
    null,
  );
  const activationAction = useRef<{ payload: string; id: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    dailyBriefApi
      .getModelCapability()
      .then((value) => {
        if (!cancelled) setCapability(value);
      })
      .catch(() => {
        if (!cancelled)
          setCapability({
            ready: false,
            code: "unavailable",
            message: "模型状态暂时无法读取，请重新打开设置后重试。",
          });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const activeGrants = snapshot.grants.filter(
    (grant) => grant.status === "active",
  );
  const scheduledSubscriptions = snapshot.subscriptions.filter(
    (subscription) => subscription.status !== "archived",
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  useEffect(() => {
    setPurposeDrafts((current) => ({
      ...Object.fromEntries(
        snapshot.topics.map((topic) => [topic.id, topicPurpose(topic)]),
      ),
      ...current,
    }));
    setSelectedTopicIds((current) =>
      current.filter((topicId) => confirmedTopicIds.includes(topicId)),
    );
  }, [confirmedTopicIds, snapshot.topics]);

  const createTopic = async () => {
    if (!topicTitle.trim()) return;
    setBusy("create-topic");
    setMessage("");
    try {
      await reminderApi.createWatchTopic({
        title: topicTitle.trim(),
        description: topicDescription.trim(),
        origin: "manual",
        enabled: true,
        frequency: "daily",
        metadata: { source_scope: "public" },
      });
      setTopicTitle("");
      setTopicDescription("");
      await onChanged();
      setMessage("公开主题已添加；它尚未获得简报研究许可。请再明确确认用途。 ");
    } catch (reason) {
      setMessage(errorMessage(reason, "主题未能添加"));
    } finally {
      setBusy(null);
    }
  };

  const setTopicStatus = async (
    topic: BriefTopicProjection,
    status: "confirmed" | "paused",
  ) => {
    const purpose = (purposeDrafts[topic.id] || "").trim();
    if (status === "confirmed" && !purpose) {
      setMessage("请先写明为什么要关注这个主题。");
      return;
    }
    setBusy(`topic:${topic.id}`);
    setMessage("");
    try {
      let revision = topic.revision;
      if (status === "confirmed" && !topic.enabled) {
        const enabled = await reminderApi.updateWatchTopic(topic.id, {
          enabled: true,
          expected_revision: revision,
        });
        revision = enabled.revision;
      }
      await reminderApi.configureWatchTopicForBrief(topic.id, {
        expected_revision: revision,
        explicit_confirmation: status === "confirmed",
        status,
        purpose,
        scope_refs: ["public:web", "public:primary-documents"],
        evidence_refs: topic.brief_evidence_refs,
        exclusions: ["private:ungranted", "unverified:rumor"],
        valid_until: topic.brief_valid_until,
      });
      if (status === "confirmed") {
        setSelectedTopicIds((current) => [...new Set([...current, topic.id])]);
      } else {
        setSelectedTopicIds((current) =>
          current.filter((id) => id !== topic.id),
        );
      }
      await onChanged();
      setMessage(
        status === "confirmed"
          ? "主题已明确确认为简报范围；尚未创建研究授权。"
          : "主题已暂停，依赖旧版本范围的待处理研究和派生内容会失效。",
      );
    } catch (reason) {
      setMessage(errorMessage(reason, "主题范围未能更新"));
    } finally {
      setBusy(null);
    }
  };

  const validateGrant = (): string | null => {
    if (!snapshot.feature_flags.research)
      return "当前构建的研究门禁尚未开放，不能启动试读或每日研究。";
    if (snapshot.runtime && !snapshot.runtime.running)
      return snapshot.runtime.message;
    if (!capability) return "正在检查后台模型，请稍候。";
    if (!capability.ready) return capability.message;
    if (!explicitConsent) return "请先同意公开来源查证与整理。";
    if (!selectedTopicIds.length && !memoryContextConsent)
      return "请允许澜台从非敏感记忆线索发现方向，或手动添加并确认一个方向。";
    if (!isValidIanaTimezone(timezone))
      return "请输入有效的 IANA 时区，例如 Asia/Shanghai。";
    return null;
  };

  const start = async (mode: Mode) => {
    const validation = validateGrant();
    if (validation) {
      setMessage(validation);
      return;
    }
    setBusy("start");
    setMessage("");
    try {
      const payload = {
        explicit_consent: true,
        configured_model_consent: true,
        mode,
        topic_ids: selectedTopicIds,
        use_memory_clues: memoryContextConsent,
        memory_context_processing_consent: memoryContextConsent,
        timezone: timezone.trim(),
        local_time: localTime,
        home_visible: homeVisible,
        native_notifications: nativeNotifications,
        quiet_hours_start: null,
        quiet_hours_end: null,
      };
      const fingerprint = JSON.stringify(payload);
      if (activationAction.current?.payload !== fingerprint) {
        activationAction.current = {
          payload: fingerprint,
          id: actionId("brief-activate"),
        };
      }
      const result = await dailyBriefApi.activate({
        ...payload,
        action_id: activationAction.current.id,
      });
      setExplicitConsent(false);
      setMemoryContextConsent(false);
      await onChanged();
      setMessage(
        mode === "preview"
          ? result.cycle?.status === "failed"
            ? "上次试读未完成，请在工作台查看原因并明确重试。"
            : "一次试读已排队；开始和完成状态会在工作台更新。"
          : "自动整理已开启；澜台会按你的时间持续发现、查证并整理。",
      );
    } catch (reason) {
      await onChanged();
      setMessage(errorMessage(reason, "简报未能开启"));
    } finally {
      setBusy(null);
    }
  };

  const startExistingPreview = async (grant: BriefGrant) => {
    if (!snapshot.feature_flags.research) {
      setMessage("当前构建的研究门禁尚未开放，不能启动试读。");
      return;
    }
    if (!isValidIanaTimezone(timezone)) {
      setMessage("请输入有效的 IANA 时区，例如 Asia/Shanghai。");
      return;
    }
    setBusy(`preview:${grant.id}`);
    setMessage("");
    try {
      const cycle = await dailyBriefApi.createPreview({
        grant_id: grant.id,
        local_date: dateInTimezone(timezone),
        timezone: timezone.trim(),
        action_id: actionId("brief-preview-create"),
      });
      await onChanged();
      setMessage(
        cycle.status === "failed"
          ? "上次试读未完成，请在工作台查看原因并明确重试。"
          : cycle.status === "succeeded" || cycle.status === "partial"
            ? "这次试读已经完成，可在工作台阅读。"
            : "一次试读已排队；状态会在工作台更新。",
      );
    } catch (reason) {
      setMessage(errorMessage(reason, "试读未能启动"));
    } finally {
      setBusy(null);
    }
  };

  const revokeGrant = async (grant: BriefGrant) => {
    setBusy(`revoke:${grant.id}`);
    setMessage("");
    try {
      await dailyBriefApi.revokeGrant(grant.id, {
        expected_revision: grant.revision,
        action_id: actionId("brief-grant-revoke"),
      });
      setPendingRevoke(null);
      await onChanged();
      setMessage(
        "授权已撤销；依赖它的待处理研究、通知和讨论草稿已按依赖失效。 ",
      );
    } catch (reason) {
      setMessage(errorMessage(reason, "授权未能撤销"));
    } finally {
      setBusy(null);
    }
  };

  const setInvitationHidden = async (hidden: boolean) => {
    setBusy("invitation");
    setMessage("");
    try {
      await dailyBriefApi.setInvitationHidden(hidden);
      await onChanged();
      setMessage(
        hidden ? "首页邀请已隐藏；不会反复劝开。" : "首页轻量邀请已恢复。 ",
      );
    } catch (reason) {
      setMessage(errorMessage(reason, "首页入口偏好未能保存"));
    } finally {
      setBusy(null);
    }
  };

  return createPortal(
    <div className={styles.backdrop} role="presentation">
      <section
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="brief-settings-title"
      >
        <header className={styles.topbar}>
          <div>
            <span>自动发现 · 持续整理</span>
            <h2 id="brief-settings-title">简报设置</h2>
            <p>
              开启后，澜台会从你当前有效的记忆与已确认方向中寻找线索，持续查证公开信息，
              把真正值得你留意的变化整理好。
            </p>
          </div>
          <button
            type="button"
            className={styles.iconButton}
            aria-label="关闭简报设置"
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </header>

        <div className={styles.body}>
          {!scheduledSubscriptions.length ? (
            <section
              className={[styles.section, styles.setupIntro].join(" ")}
              aria-labelledby="brief-start-heading"
            >
              <div className={styles.setupLead}>
                <span>开启自动整理</span>
                <h3 id="brief-start-heading">
                  让澜台每天替你留意值得关注的变化
                </h3>
                <p>
                  不用先想好搜索词。澜台会把记忆当作选题线索，再用公开来源核实、比较与整理；
                  有重要变化才给你判断依据，没有显著增量也会如实说明。
                </p>
              </div>

              {!snapshot.feature_flags.research ? (
                <div className={styles.gateNotice} role="status">
                  <ShieldCheck size={16} />
                  <div>
                    <strong>当前构建尚未开放研究门禁</strong>
                    <span>
                      你可以查看和调整偏好，但还不能启动试读或每日整理。
                    </span>
                  </div>
                </div>
              ) : null}

              <div className={styles.setupCard}>
                <label className={styles.finalConsent}>
                  <input
                    type="checkbox"
                    checked={explicitConsent}
                    onChange={(event) =>
                      setExplicitConsent(event.target.checked)
                    }
                  />
                  <span>
                    允许澜台搜索和读取公开来源，并使用已配置的辅助模型完成查证与整理，同意消耗该模型的额度或产生 API 费用。
                  </span>
                </label>
                <label className={styles.finalConsent}>
                  <input
                    type="checkbox"
                    checked={memoryContextConsent}
                    onChange={(event) =>
                      setMemoryContextConsent(event.target.checked)
                    }
                  />
                  <span>
                    允许把选中的非敏感记忆线索片段交给已配置的辅助模型，提炼为公开关注方向。
                  </span>
                </label>
                <p className={styles.consentNote}>
                  不会把整份记忆或私人文件交给搜索或模型；具体私人内容仍会另行征求许可。
                </p>
                <p className={styles.consentNote}>
                  简报支持你在「设置 → 模型 → 回答后的后台整理」选定的模型。
                  每次生成仍受用量预算限制；旧授权需要重新开启，才会允许使用收费模型。
                </p>
                <p className={styles.consentNote} role="status">
                  {capability?.message || "正在检查后台模型…"}
                </p>
                {snapshot.runtime && !snapshot.runtime.running ? (
                  <p className={styles.gateNotice} role="alert">
                    {snapshot.runtime.message}
                  </p>
                ) : null}

                <div className={styles.scheduleRow}>
                  <label>
                    每天
                    <input
                      type="time"
                      value={localTime}
                      onChange={(event) => setLocalTime(event.target.value)}
                    />
                  </label>
                  <span>按系统时区 {timezone}</span>
                </div>

                <div className={styles.checkList}>
                  <label>
                    <input
                      type="checkbox"
                      checked={homeVisible}
                      onChange={(event) => setHomeVisible(event.target.checked)}
                    />
                    在首页显示轻量入口卡片
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={nativeNotifications}
                      disabled={!snapshot.feature_flags.native_notification}
                      onChange={(event) =>
                        setNativeNotifications(event.target.checked)
                      }
                    />
                    原生通知（单独同意）
                  </label>
                  {!snapshot.feature_flags.native_notification ? (
                    <small>
                      当前构建尚未开放原生通知；首页可见不等于同意通知。
                    </small>
                  ) : null}
                </div>

                <div className={styles.actionRow}>
                  <button
                    type="button"
                    className={styles.primaryButton}
                    disabled={busy !== null || !snapshot.feature_flags.research}
                    onClick={() => void start("daily")}
                  >
                    {busy === "start" ? "正在开启" : "开启每日简报"}
                  </button>
                  <button
                    type="button"
                    className={styles.quietButton}
                    disabled={busy !== null || !snapshot.feature_flags.research}
                    onClick={() => void start("preview")}
                  >
                    先试读一次
                  </button>
                </div>
                <small className={styles.previewHint}>
                  试读只生成这一期，不会自动变成每日简报。
                </small>
              </div>
            </section>
          ) : (
            <section
              className={styles.section}
              aria-labelledby="brief-schedule-heading"
            >
              <div className={styles.sectionHeading}>
                <div>
                  <span>01</span>
                  <h3 id="brief-schedule-heading">自动整理</h3>
                </div>
                <small>可暂停，也可随时调整时间</small>
              </div>
              {scheduledSubscriptions.map((subscription) => (
                <SubscriptionEditor
                  key={subscription.id + ":" + subscription.revision}
                  subscription={subscription}
                  nativeNotificationAvailable={
                    snapshot.feature_flags.native_notification
                  }
                  onChanged={onChanged}
                  onMessage={setMessage}
                />
              ))}
            </section>
          )}

          <div className={styles.disclosureBar}>
            <button
              type="button"
              className={styles.quietButton}
              aria-expanded={showTopics}
              onClick={() => setShowTopics((value) => !value)}
            >
              调整关注方向（可选）
            </button>
            <button
              type="button"
              className={styles.quietButton}
              aria-expanded={showMore}
              onClick={() => setShowMore((value) => !value)}
            >
              更多设置
            </button>
          </div>

          {showTopics ? (
            <section
              className={[styles.section, styles.compactSection].join(" ")}
              aria-labelledby="brief-topics-heading"
            >
              <div className={styles.sectionHeading}>
                <div>
                  <span>02</span>
                  <h3 id="brief-topics-heading">关注方向</h3>
                </div>
                <small>不填写也可以由澜台从记忆线索开始</small>
              </div>
              <p className={styles.empty}>
                这里是长期判断方向，不是搜索关键词表。你可以补充、暂停或明确确认。
              </p>
              <details className={styles.inlineDetails}>
                <summary>手动补充一个方向</summary>
                <div className={styles.newTopic}>
                  <label>
                    方向名称
                    <input
                      value={topicTitle}
                      onChange={(event) => setTopicTitle(event.target.value)}
                      placeholder="例如：AI 办公产品变化"
                    />
                  </label>
                  <label>
                    为什么值得关注（可选）
                    <input
                      value={topicDescription}
                      onChange={(event) =>
                        setTopicDescription(event.target.value)
                      }
                      placeholder="只描述方向，不粘贴私人材料"
                    />
                  </label>
                  <button
                    type="button"
                    className={styles.quietButton}
                    disabled={busy !== null || !topicTitle.trim()}
                    onClick={() => void createTopic()}
                  >
                    <Plus size={14} />
                    添加方向
                  </button>
                </div>
              </details>

              {snapshot.topics.length ? (
                <div className={styles.topicList}>
                  {snapshot.topics.map((topic) => {
                    const confirmed =
                      topic.brief_status === "confirmed" && topic.enabled;
                    return (
                      <article key={topic.id} className={styles.topicCard}>
                        <header>
                          <label className={styles.topicSelect}>
                            <input
                              type="checkbox"
                              disabled={!confirmed}
                              checked={
                                confirmed && selectedTopicIds.includes(topic.id)
                              }
                              onChange={(event) =>
                                setSelectedTopicIds((current) =>
                                  event.target.checked
                                    ? [...new Set([...current, topic.id])]
                                    : current.filter((id) => id !== topic.id),
                                )
                              }
                            />
                            <strong>{topic.title}</strong>
                          </label>
                          <span data-status={topic.brief_status}>
                            {confirmed
                              ? "已确认"
                              : topic.brief_status === "paused"
                                ? "已暂停"
                                : "待确认"}
                          </span>
                        </header>
                        <label>
                          关注理由
                          <input
                            value={purposeDrafts[topic.id] || ""}
                            onChange={(event) =>
                              setPurposeDrafts((current) => ({
                                ...current,
                                [topic.id]: event.target.value,
                              }))
                            }
                          />
                        </label>
                        <p>
                          允许范围：公开网页与公开原始文档；具体私人内容需要另行许可。
                        </p>
                        <footer>
                          {confirmed ? (
                            <button
                              type="button"
                              className={styles.quietButton}
                              disabled={busy !== null}
                              onClick={() =>
                                void setTopicStatus(topic, "paused")
                              }
                            >
                              <Pause size={13} />
                              暂停主题
                            </button>
                          ) : (
                            <button
                              type="button"
                              className={styles.primaryButton}
                              disabled={
                                busy !== null ||
                                !(purposeDrafts[topic.id] || "").trim()
                              }
                              onClick={() =>
                                void setTopicStatus(topic, "confirmed")
                              }
                            >
                              明确确认用于简报
                            </button>
                          )}
                        </footer>
                      </article>
                    );
                  })}
                </div>
              ) : (
                <p className={styles.empty}>
                  还没有手动方向。开启后可先由澜台从记忆线索中发现。
                </p>
              )}
            </section>
          ) : null}

          {showMore ? (
            <section
              className={[styles.section, styles.compactSection].join(" ")}
              aria-labelledby="brief-more-heading"
            >
              <div className={styles.sectionHeading}>
                <div>
                  <span>03</span>
                  <h3 id="brief-more-heading">授权与入口</h3>
                </div>
                <small>撤销会让依赖旧授权的待处理内容失效</small>
              </div>

              {activeGrants.length ? (
                <div className={styles.grantList}>
                  {activeGrants.map((grant) => (
                    <article key={grant.id} className={styles.grantCard}>
                      <div>
                        <strong>
                          {grant.one_time ? "一次试读授权" : "每日简报授权"}
                        </strong>
                        <span>
                          {grant.topic_ids.length
                            ? grant.topic_ids.length + " 个已确认方向"
                            : "由澜台从记忆线索发现方向"}
                          {" · "}公开来源 · 具体私人内容未授权出站
                        </span>
                      </div>
                      <footer>
                        {grant.one_time ? (
                          <button
                            type="button"
                            className={styles.quietButton}
                            disabled={
                              busy !== null || !snapshot.feature_flags.research
                            }
                            onClick={() => void startExistingPreview(grant)}
                          >
                            开始今日试读
                          </button>
                        ) : null}
                        {pendingRevoke === grant.id ? (
                          <>
                            <button
                              type="button"
                              className={styles.quietButton}
                              onClick={() => setPendingRevoke(null)}
                            >
                              取消
                            </button>
                            <button
                              type="button"
                              className={styles.dangerButton}
                              disabled={busy !== null}
                              onClick={() => void revokeGrant(grant)}
                            >
                              确认撤销
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            className={styles.quietButton}
                            disabled={busy !== null}
                            onClick={() => setPendingRevoke(grant.id)}
                          >
                            撤销授权
                          </button>
                        )}
                      </footer>
                    </article>
                  ))}
                </div>
              ) : (
                <p className={styles.empty}>当前没有有效授权。</p>
              )}

              <div className={styles.preferenceRow}>
                <p>
                  {snapshot.invitation_hidden
                    ? "首页邀请当前已隐藏，不会反复出现。"
                    : "首页只显示一张轻量入口卡片，不加载简报正文。"}
                </p>
                <button
                  type="button"
                  className={styles.quietButton}
                  disabled={busy !== null}
                  onClick={() =>
                    void setInvitationHidden(!snapshot.invitation_hidden)
                  }
                >
                  {snapshot.invitation_hidden ? "恢复首页邀请" : "隐藏首页邀请"}
                </button>
              </div>
            </section>
          ) : null}
        </div>

        <footer className={styles.footer}>
          <p role="status">
            {message ||
              "打开设置和浏览已有简报不会启动检索、调用模型或创建研究任务。"}
          </p>
          <button
            type="button"
            className={styles.quietButton}
            onClick={onClose}
          >
            完成
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
