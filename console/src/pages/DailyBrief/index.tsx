import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  Bookmark,
  CalendarClock,
  Check,
  ChevronRight,
  CircleAlert,
  ExternalLink,
  History,
  MessageCircle,
  Newspaper,
  RefreshCw,
  Settings2,
  ShieldCheck,
  ThumbsUp,
} from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { dailyBriefApi } from "@/api/modules/dailyBrief";
import type {
  BriefEdition,
  BriefEvidence,
  BriefFeedbackCreateRequest,
  BriefItem,
  BriefWorkspaceSnapshot,
} from "@/api/types/dailyBrief";
import { openExternalUrl } from "@/utils/openExternalUrl";
import BriefSettingsDialog from "./BriefSettingsDialog";
import { dailyBriefReturnPath } from "../Chat/dailyBriefReturnDraft";
import styles from "./index.module.less";

type LoadState = "loading" | "ready" | "error";

function actionId(prefix: string): string {
  const suffix =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function formatDate(
  value: string | null | undefined,
  withTime = false,
): string {
  if (!value) return "未记录";
  if (!withTime && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [year, month, day] = value.split("-").map(Number);
    return `${year}年${month}月${day}日`;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function outcomeLabel(edition: BriefEdition): string {
  if (edition.outcome === "no_material_update") return "已检查，无显著增量";
  if (edition.outcome === "partial") return "部分完成";
  return "已完成";
}

function EvidenceLinks({
  item,
  evidence,
  onOpenSource,
}: {
  item: BriefItem;
  evidence: BriefEvidence[];
  onOpenSource: (url: string) => void;
}) {
  const entries = evidence.filter((source) =>
    item.evidence_ids.includes(source.id),
  );
  if (!entries.length) return null;
  return (
    <div className={styles.evidenceList} aria-label="本条来源">
      {entries.map((source) => (
        <a
          key={source.id}
          href={source.canonical_url}
          rel="noreferrer noopener"
          onClick={(event) => {
            event.preventDefault();
            onOpenSource(source.canonical_url);
          }}
        >
          <span>
            {source.publisher || "来源"} ·{" "}
            {source.title || source.canonical_url}
          </span>
          <small>
            发布 {formatDate(source.published_at)} · 查证{" "}
            {formatDate(source.fetched_at, true)}
          </small>
          <ExternalLink size={13} aria-hidden="true" />
        </a>
      ))}
    </div>
  );
}

function BriefItemCard({
  item,
  edition,
  onFeedback,
  onPrepareDiscussion,
  onOpenSource,
}: {
  item: BriefItem;
  edition: BriefEdition;
  onFeedback: (
    itemId: string,
    kind: BriefFeedbackCreateRequest["kind"],
  ) => void;
  onPrepareDiscussion: (item: BriefItem) => void;
  onOpenSource: (url: string) => void;
}) {
  return (
    <article className={styles.itemCard}>
      <header>
        <span>重点</span>
        <h2>{item.title}</h2>
      </header>
      <dl className={styles.claims}>
        <div>
          <dt>事实</dt>
          <dd>{item.fact}</dd>
        </div>
        <div>
          <dt>与你的关系</dt>
          <dd>{item.relevance}</dd>
        </div>
        {item.analysis ? (
          <div>
            <dt>分析判断</dt>
            <dd>{item.analysis}</dd>
          </div>
        ) : null}
        {item.limitations ? (
          <div>
            <dt>限制与不确定性</dt>
            <dd>{item.limitations}</dd>
          </div>
        ) : null}
        {item.optional_action ? (
          <div>
            <dt>可选行动</dt>
            <dd>{item.optional_action}</dd>
          </div>
        ) : null}
      </dl>
      <EvidenceLinks
        item={item}
        evidence={edition.evidence}
        onOpenSource={onOpenSource}
      />
      <footer className={styles.itemActions}>
        <button type="button" onClick={() => onFeedback(item.id, "useful")}>
          <ThumbsUp size={14} />
          有用
        </button>
        <button type="button" onClick={() => onFeedback(item.id, "incorrect")}>
          <CircleAlert size={14} />
          有误
        </button>
        <button type="button" onClick={() => onFeedback(item.id, "save")}>
          <Bookmark size={14} />
          收藏
        </button>
        <button type="button" onClick={() => onPrepareDiscussion(item)}>
          <MessageCircle size={14} />
          继续讨论
        </button>
      </footer>
    </article>
  );
}

export default function DailyBriefPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const deliveryReceiptId = useMemo(
    () => new URLSearchParams(location.search).get("delivery") || "",
    [location.search],
  );
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [snapshot, setSnapshot] = useState<BriefWorkspaceSnapshot | null>(null);
  const [selectedEdition, setSelectedEdition] = useState<BriefEdition | null>(
    null,
  );
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [discussionItem, setDiscussionItem] = useState<BriefItem | null>(null);
  const [discussionText, setDiscussionText] = useState("");
  const [savingDraft, setSavingDraft] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [pendingRetry, setPendingRetry] = useState<string | null>(null);
  const [retryBusy, setRetryBusy] = useState(false);
  const [extraHistory, setExtraHistory] = useState<BriefEdition[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const historyPageKey = useRef("");
  const historyGeneration = useRef(0);
  const historyRequest = useRef<object | null>(null);
  const alive = useRef(true);
  const refreshing = useRef(false);
  const followLatest = useRef(true);
  const selectionGeneration = useRef(0);
  const selectionPending = useRef(false);
  const selectedRef = useRef<BriefEdition | null>(null);
  const retryAction = useRef<{ id: string; action: string } | null>(null);
  selectedRef.current = selectedEdition;

  const refresh = useCallback(async (initial = false) => {
    if (refreshing.current) return;
    refreshing.current = true;
    const generation = selectionGeneration.current;
    if (initial) {
      setLoadState("loading");
      setError("");
    }
    try {
      const data = await dailyBriefApi.getWorkspaceSnapshot();
      let edition = data.latest_edition;
      if (!followLatest.current && selectedRef.current) {
        const current = selectedRef.current;
        edition =
          [data.latest_edition, ...data.history].find(
            (entry) =>
              entry?.id === current.id && entry.revision === current.revision,
          ) ||
          (await dailyBriefApi
            .getEdition(current.id, current.revision)
            .catch(() => data.latest_edition));
      }
      if (!alive.current) return;
      const pageKey = JSON.stringify([
        data.history_total,
        data.history.map((entry) => [entry.id, entry.revision]),
      ]);
      if (pageKey !== historyPageKey.current) {
        historyPageKey.current = pageKey;
        historyGeneration.current++;
        historyRequest.current = null;
        setHistoryLoading(false);
        setExtraHistory([]);
      }
      setSnapshot(data);
      if (
        generation === selectionGeneration.current &&
        !selectionPending.current
      ) {
        selectedRef.current = edition;
        setSelectedEdition(edition);
      }
      setLoadState("ready");
    } catch (reason) {
      if (!alive.current) return;
      const message =
        reason instanceof Error ? reason.message : "无法读取简报工作台";
      if (initial) {
        setError(message);
        setLoadState("error");
      } else setNotice(message);
    } finally {
      refreshing.current = false;
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh(true);
    return () => {
      alive.current = false;
    };
  }, [refresh]);

  const hasPending =
    snapshot?.cycles?.some(
      (cycle) => cycle.status === "queued" || cycle.status === "running",
    ) || false;
  useEffect(() => {
    const update = () => {
      if (document.visibilityState !== "hidden") void refresh();
    };
    const timer = window.setInterval(update, hasPending ? 5000 : 60000);
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [hasPending, refresh]);

  useEffect(() => {
    if (!deliveryReceiptId || loadState !== "ready") return;
    let cancelled = false;
    const generation = ++selectionGeneration.current;
    selectionPending.current = true;
    followLatest.current = false;
    void (async () => {
      try {
        const receipt =
          await dailyBriefApi.getDeliveryReceipt(deliveryReceiptId);
        const edition = await dailyBriefApi.getEdition(
          receipt.edition_id,
          receipt.edition_revision,
        );
        if (cancelled || generation !== selectionGeneration.current) return;
        selectionPending.current = false;
        selectedRef.current = edition;
        setSelectedEdition(edition);
        await dailyBriefApi.markDeliveryRead(deliveryReceiptId);
        if (!cancelled && generation === selectionGeneration.current)
          navigate("/daily-brief", { replace: true });
      } catch (reason) {
        if (!cancelled && generation === selectionGeneration.current) {
          selectionPending.current = false;
          setNotice(
            reason instanceof Error ? reason.message : "通知对应的简报暂不可读",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
      if (generation === selectionGeneration.current)
        selectionPending.current = false;
    };
  }, [deliveryReceiptId, loadState, navigate]);

  useEffect(() => {
    if (!selectedEdition || deliveryReceiptId) return;
    void dailyBriefApi
      .markEditionRead(selectedEdition.id, selectedEdition.revision)
      .catch(() => {});
  }, [selectedEdition?.id, selectedEdition?.revision, deliveryReceiptId]);

  const confirmRetry = async () => {
    if (!pendingRetry) return;
    setRetryBusy(true);
    if (retryAction.current?.id !== pendingRetry)
      retryAction.current = {
        id: pendingRetry,
        action: actionId("brief-retry"),
      };
    try {
      await dailyBriefApi.retryCycle(pendingRetry, {
        explicit_consent: true,
        action_id: retryAction.current.action,
      });
      setPendingRetry(null);
      setNotice("重试已排队，将使用本次授权的剩余预算。");
      await refresh();
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "重试未能排队");
    } finally {
      setRetryBusy(false);
    }
  };

  const selectSaved = async (editionId: string, revision: number) => {
    const generation = ++selectionGeneration.current;
    selectionPending.current = true;
    followLatest.current = false;
    try {
      const edition = await dailyBriefApi.getEdition(editionId, revision);
      if (!alive.current || generation !== selectionGeneration.current) return;
      selectionPending.current = false;
      selectedRef.current = edition;
      setSelectedEdition(edition);
    } catch (reason) {
      if (alive.current && generation === selectionGeneration.current) {
        selectionPending.current = false;
        setNotice(
          reason instanceof Error ? reason.message : "收藏内容暂不可读",
        );
      }
    }
  };

  const loadHistory = async () => {
    if (!snapshot || historyRequest.current) return;
    const generation = historyGeneration.current;
    const request = {};
    historyRequest.current = request;
    setHistoryLoading(true);
    try {
      const entries = await dailyBriefApi.getEditions(
        50,
        snapshot.history.length + extraHistory.length,
      );
      if (!alive.current || generation !== historyGeneration.current) return;
      setExtraHistory((current) => {
        const existing = [...snapshot.history, ...current];
        return [
          ...current,
          ...entries.filter(
            (entry) =>
              !existing.some(
                (old) => old.id === entry.id && old.revision === entry.revision,
              ),
          ),
        ];
      });
    } catch {
      if (alive.current && generation === historyGeneration.current)
        setNotice("历史简报暂时无法读取");
    } finally {
      if (alive.current && historyRequest.current === request) {
        historyRequest.current = null;
        setHistoryLoading(false);
      }
    }
  };

  const evidenceById = useMemo(
    () =>
      new Map(
        (selectedEdition?.evidence || []).map((entry) => [entry.id, entry]),
      ),
    [selectedEdition],
  );

  const recordFeedback = useCallback(
    async (itemId: string, kind: BriefFeedbackCreateRequest["kind"]) => {
      if (!selectedEdition) return;
      setNotice("");
      try {
        await dailyBriefApi.addFeedback(selectedEdition.id, itemId, {
          kind,
          action_id: actionId("brief-feedback"),
        });
        setNotice(kind === "save" ? "已收藏这条材料" : "反馈已记录");
        if (kind === "save") await refresh();
      } catch (reason) {
        setNotice(reason instanceof Error ? reason.message : "反馈未能保存");
      }
    },
    [selectedEdition, refresh],
  );

  const prepareDiscussion = (item: BriefItem) => {
    setDiscussionItem(item);
    setDiscussionText(`我想继续讨论：${item.title}`);
    setNotice("");
  };

  const openSource = useCallback((url: string) => {
    setNotice("");
    void openExternalUrl(url).catch((reason) => {
      setNotice(reason instanceof Error ? reason.message : "来源链接无法打开");
    });
  }, []);

  const saveDiscussionDraft = async () => {
    if (!selectedEdition || !discussionItem || !discussionText.trim()) return;
    setSavingDraft(true);
    setNotice("");
    try {
      const draft = await dailyBriefApi.createDiscussionDraft(
        selectedEdition.id,
        {
          edition_revision: selectedEdition.revision,
          item_id: discussionItem.id,
          text: discussionText.trim(),
          action_id: actionId("brief-discussion"),
        },
      );
      setNotice("讨论草稿已准备");
      setDiscussionItem(null);
      setDiscussionText("");
      const destination = new URL(
        dailyBriefReturnPath(),
        window.location.origin,
      );
      destination.searchParams.set("briefDraft", draft.id);
      navigate(`${destination.pathname}${destination.search}`);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "草稿未能保存");
    } finally {
      setSavingDraft(false);
    }
  };

  return (
    <main className={styles.page} aria-label="澜台简报工作台">
      <header className={styles.topbar}>
        <button
          type="button"
          className={styles.back}
          onClick={() => navigate(-1)}
        >
          <ArrowLeft size={17} />
          返回
        </button>
        <div className={styles.brand}>
          <Newspaper size={18} />
          <strong>澜台简报</strong>
          <span>研究与编辑工作台</span>
        </div>
        <span className={styles.readOnlyNote}>打开页面不会启动检索或模型</span>
        <button
          type="button"
          className={styles.back}
          onClick={() => void refresh()}
          aria-label="刷新简报"
        >
          <RefreshCw size={16} />
          刷新
        </button>
      </header>

      {loadState === "loading" ? (
        <section className={styles.centerState} role="status">
          <RefreshCw className={styles.spin} size={24} />
          <strong>正在读取已有简报</strong>
          <span>这里只读取本地工作台快照。</span>
        </section>
      ) : null}

      {loadState === "error" ? (
        <section className={styles.centerState} role="alert">
          <CircleAlert size={26} />
          <strong>简报工作台暂时不可用</strong>
          <span>{error}</span>
          <button type="button" onClick={() => void refresh(true)}>
            重新读取
          </button>
        </section>
      ) : null}

      {loadState === "ready" && snapshot ? (
        <div className={styles.layout}>
          <section className={styles.reader}>
            {snapshot.runtime && !snapshot.runtime.running ? (
              <section className={styles.noUpdate} role="alert">
                <CircleAlert size={20} />
                <p>{snapshot.runtime.message}</p>
              </section>
            ) : null}
            {snapshot.cycles?.length ? (
              <section className={styles.railCard} aria-label="生成状态">
                <h2>生成状态</h2>
                {snapshot.cycles.slice(0, 10).map((cycle) => (
                  <article key={cycle.id} className={styles.cycleRow}>
                    <div>
                      <strong>
                        {formatDate(cycle.local_date)} ·{" "}
                        {
                          {
                            queued: "已排队",
                            running: "正在生成",
                            succeeded: "已完成",
                            partial: "部分完成",
                            failed: "未完成",
                            cancelled: "已取消",
                            invalidated: "范围已失效",
                          }[cycle.status]
                        }
                      </strong>
                      <p>{cycle.message}</p>
                      <small>
                        已使用 {cycle.usage.max_searches} 次搜索 ·{" "}
                        {cycle.usage.max_source_reads} 次来源读取 ·{" "}
                        {cycle.usage.max_model_tokens} tokens
                        {cycle.usage_uncertain ? " · 模型用量尚未确认" : ""}
                        {cycle.cost_cny > 0
                          ? ` · ¥${cycle.cost_cny.toFixed(2)}`
                          : ""}
                      </small>
                    </div>
                    {cycle.retryable ? (
                      <button
                        type="button"
                        onClick={() => setPendingRetry(cycle.id)}
                      >
                        重试本次生成
                      </button>
                    ) : null}
                  </article>
                ))}
              </section>
            ) : null}
            {selectedEdition ? (
              <>
                <header className={styles.editionHeader}>
                  <div className={styles.eyebrow}>
                    <span>{formatDate(selectedEdition.local_date)}</span>
                    <i aria-hidden="true" />
                    <span>{outcomeLabel(selectedEdition)}</span>
                  </div>
                  <h1>{selectedEdition.title}</h1>
                  {selectedEdition.summary ? (
                    <p>{selectedEdition.summary}</p>
                  ) : null}
                  <div className={styles.checkWindow}>
                    查证完成于{" "}
                    {formatDate(selectedEdition.checked_finished_at, true)}
                    {selectedEdition.correction_note
                      ? ` · 更正：${selectedEdition.correction_note}`
                      : ""}
                  </div>
                </header>

                {selectedEdition.outcome === "no_material_update" ? (
                  <section className={styles.noUpdate}>
                    <Check size={20} />
                    <div>
                      <strong>本次检查已经完成</strong>
                      <p>
                        没有发现值得加入新一期的显著增量，因此没有为了数量凑内容。
                      </p>
                    </div>
                  </section>
                ) : null}

                <div className={styles.items}>
                  {selectedEdition.items.map((item) => (
                    <BriefItemCard
                      key={item.id}
                      item={item}
                      edition={selectedEdition}
                      onFeedback={(itemId, kind) =>
                        void recordFeedback(itemId, kind)
                      }
                      onPrepareDiscussion={prepareDiscussion}
                      onOpenSource={openSource}
                    />
                  ))}
                </div>

                {selectedEdition.trends.length ? (
                  <section className={styles.trends}>
                    <div className={styles.sectionHeading}>
                      <h2>趋势与反证</h2>
                      <span>可修订，不把旧结论当新证据</span>
                    </div>
                    {selectedEdition.trends.map((trend) => (
                      <article key={trend.id}>
                        <h3>{trend.statement}</h3>
                        {trend.uncertainty ? <p>{trend.uncertainty}</p> : null}
                        <div className={styles.trendEvidence}>
                          <span>
                            支持 {trend.support_evidence_ids.length} 项
                          </span>
                          <span>
                            反证 {trend.counter_evidence_ids.length} 项
                          </span>
                          {trend.falsifiers.length ? (
                            <span>
                              可证伪条件 {trend.falsifiers.join("；")}
                            </span>
                          ) : null}
                        </div>
                        <div className={styles.trendSources}>
                          {[
                            ...trend.support_evidence_ids,
                            ...trend.counter_evidence_ids,
                          ]
                            .map((id) => evidenceById.get(id))
                            .filter((entry): entry is BriefEvidence =>
                              Boolean(entry),
                            )
                            .map((entry) => (
                              <a
                                key={entry.id}
                                href={entry.canonical_url}
                                rel="noreferrer noopener"
                                onClick={(event) => {
                                  event.preventDefault();
                                  openSource(entry.canonical_url);
                                }}
                              >
                                {entry.publisher || entry.title || "来源"}
                              </a>
                            ))}
                        </div>
                      </article>
                    ))}
                  </section>
                ) : null}
              </>
            ) : (
              <section className={styles.welcome}>
                <Newspaper size={28} strokeWidth={1.5} />
                <span>
                  {snapshot.activation_state === "topics_only"
                    ? "关注方向已确认，尚未开启简报"
                    : hasPending
                      ? "简报已排队，正在等待结果"
                      : snapshot.subscriptions.some(
                            (entry) => entry.status === "active",
                          )
                        ? "每日简报已开启，等待下一次整理"
                        : "尚未启用"}
                </span>
                <h1>从你真正关心的变化开始</h1>
                <p>
                  开启后，澜台会从当前有效的记忆与已确认方向中寻找线索，持续查证并整理真正值得你留意的变化。进入这个页面本身不会联网、调用模型或开启订阅。
                </p>
                <button type="button" onClick={() => setSettingsOpen(true)}>
                  开始设置 <ChevronRight size={16} />
                </button>
              </section>
            )}
          </section>

          <aside className={styles.rail} aria-label="简报历史与设置">
            <section className={styles.railCard}>
              <div className={styles.sectionHeading}>
                <h2>
                  <History size={16} />
                  历史简报
                </h2>
              </div>
              {snapshot.history.length ? (
                <div className={styles.historyList}>
                  {[...snapshot.history, ...extraHistory].map((edition) => (
                    <button
                      key={`${edition.id}:${edition.revision}`}
                      type="button"
                      aria-current={
                        selectedEdition?.id === edition.id &&
                        selectedEdition.revision === edition.revision
                          ? "page"
                          : undefined
                      }
                      onClick={() => {
                        selectionGeneration.current++;
                        selectionPending.current = false;
                        followLatest.current = false;
                        selectedRef.current = edition;
                        setSelectedEdition(edition);
                      }}
                    >
                      <span>{formatDate(edition.local_date)}</span>
                      <small>{outcomeLabel(edition)}</small>
                    </button>
                  ))}
                </div>
              ) : (
                <p className={styles.emptyNote}>还没有已发布的简报。</p>
              )}
              {(snapshot.history_total || 0) >
              snapshot.history.length + extraHistory.length ? (
                <button
                  type="button"
                  className={styles.secondaryAction}
                  disabled={historyLoading}
                  onClick={() => void loadHistory()}
                >
                  加载更早的简报
                </button>
              ) : null}
            </section>

            {snapshot.saved_items?.length ? (
              <section className={styles.railCard} aria-label="收藏材料">
                <h2>收藏材料</h2>
                {snapshot.saved_items.map((saved) => (
                  <div key={saved.id} className={styles.cycleRow}>
                    <button
                      type="button"
                      onClick={() =>
                        void selectSaved(
                          saved.edition_id,
                          saved.edition_revision,
                        )
                      }
                    >
                      查看 {formatDate(saved.saved_at)} 的收藏
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        void dailyBriefApi
                          .removeSavedItem(saved.id)
                          .then(() => refresh())
                          .catch(() => setNotice("收藏未能移除"));
                      }}
                    >
                      取消收藏
                    </button>
                  </div>
                ))}
              </section>
            ) : null}

            <section className={styles.railCard}>
              <div className={styles.sectionHeading}>
                <h2>
                  <Settings2 size={16} />
                  自动整理
                </h2>
              </div>
              <dl className={styles.settingsList}>
                <div>
                  <dt>关注主题</dt>
                  <dd>
                    {
                      snapshot.topics.filter(
                        (topic) => topic.brief_status === "confirmed",
                      ).length
                    }{" "}
                    个已确认
                  </dd>
                </div>
                <div>
                  <dt>授权</dt>
                  <dd>
                    {snapshot.grants.some((grant) => grant.status === "active")
                      ? "有效"
                      : "未授权"}
                  </dd>
                </div>
                <div>
                  <dt>每日调度</dt>
                  <dd>
                    {snapshot.subscriptions.some(
                      (item) => item.status === "active",
                    )
                      ? "已开启"
                      : "未开启"}
                  </dd>
                </div>
                <div>
                  <dt>原生通知</dt>
                  <dd>
                    {snapshot.subscriptions.some(
                      (item) => item.native_notifications,
                    )
                      ? "已单独同意"
                      : "未启用"}
                  </dd>
                </div>
              </dl>
              <button
                type="button"
                className={styles.secondaryAction}
                onClick={() => setSettingsOpen(true)}
              >
                <ShieldCheck size={15} />
                简报设置
              </button>
              <p className={styles.safetyNote}>
                需要使用具体私人内容时，澜台会另行征求许可。
              </p>
              {Object.values(snapshot.subscription_issues || {}).map(
                (issue) => (
                  <p key={issue} role="alert">
                    {issue}
                  </p>
                ),
              )}
            </section>

            <section className={styles.railCard}>
              <div className={styles.sectionHeading}>
                <h2>
                  <CalendarClock size={16} />
                  运行边界
                </h2>
              </div>
              <p className={styles.emptyNote}>
                App
                关闭或设备睡眠时不会伪装按时研究；醒来后按获准策略合并为最新一次。
              </p>
            </section>
          </aside>
        </div>
      ) : null}

      {discussionItem && selectedEdition ? (
        <div className={styles.dialogBackdrop} role="presentation">
          <section
            className={styles.dialog}
            role="dialog"
            aria-modal="true"
            aria-labelledby="brief-discussion-title"
          >
            <span>继续讨论</span>
            <h2 id="brief-discussion-title">{discussionItem.title}</h2>
            <textarea
              autoFocus
              value={discussionText}
              onChange={(event) => setDiscussionText(event.target.value)}
              rows={4}
            />
            <p>将带着这条材料和来源进入聊天草稿。确认发送后才会开始讨论。</p>
            <div>
              <button type="button" onClick={() => setDiscussionItem(null)}>
                取消
              </button>
              <button
                type="button"
                disabled={savingDraft || !discussionText.trim()}
                onClick={() => void saveDiscussionDraft()}
              >
                {savingDraft ? "正在准备" : "准备讨论草稿"}
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {settingsOpen && snapshot ? (
        <BriefSettingsDialog
          snapshot={snapshot}
          onClose={() => setSettingsOpen(false)}
          onChanged={refresh}
        />
      ) : null}

      {pendingRetry ? (
        <div className={styles.dialogBackdrop}>
          <section
            className={styles.dialog}
            role="dialog"
            aria-modal="true"
            aria-labelledby="brief-retry-title"
          >
            <h2 id="brief-retry-title">重试本次生成</h2>
            <p>
              将继续使用同一授权和剩余预算。旧的失败记录会保留；预算不足或授权失效时不会启动。
            </p>
            <div>
              <button
                type="button"
                disabled={retryBusy}
                onClick={() => setPendingRetry(null)}
              >
                取消
              </button>
              <button
                type="button"
                disabled={retryBusy}
                onClick={() => void confirmRetry()}
              >
                {retryBusy ? "正在排队" : "确认重试"}
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {notice ? (
        <div className={styles.toast} role="status">
          {notice}
        </div>
      ) : null}
    </main>
  );
}
