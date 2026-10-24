import { useEffect, useRef, useState } from "react";
import { AlertTriangle, MessageCircle, X } from "lucide-react";
import { consoleApi, type PushMessage } from "../../api/modules/console";
import { useApprovalContext } from "../../contexts/ApprovalContext";
import styles from "./index.module.less";

const VISIBLE_POLL_INTERVAL_MS = 5000;
const HIDDEN_POLL_INTERVAL_MS = 30000;
const POLL_REQUEST_TIMEOUT_MS = 12000;
const AUTO_DISMISS_MS = 8000;
const COUNTDOWN_STEP_MS = 500;
const MAX_SEEN_IDS = 500;
const MAX_NEW_PER_POLL = 2;
const TITLE_BLINK_PREFIX = "\u2022 ";

interface BubbleItem extends PushMessage {
  remainingMs: number | null;
  acknowledgementError?: boolean;
}

function formatOccurredAt(value?: number): string | null {
  if (!Number.isFinite(value)) return null;
  const date = new Date((value as number) * 1000);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function getSourceLink(item: PushMessage): {
  href: string;
  label: string;
} | null {
  if (item.source_kind === "cron_job") {
    return { href: "#/cron-jobs", label: "查看定时任务" };
  }
  if (item.session_id) {
    return {
      href: `#/chat/${encodeURIComponent(item.session_id)}`,
      label: "打开来源会话",
    };
  }
  return null;
}

export default function ConsolePollService() {
  const [items, setItems] = useState<BubbleItem[]>([]);
  const [pollPaused, setPollPaused] = useState(false);
  const [acknowledgingIds, setAcknowledgingIds] = useState<Set<string>>(
    new Set(),
  );
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seenIdsRef = useRef<Set<string>>(new Set());
  const seenOrderRef = useRef<string[]>([]);
  const notifiedApprovalIdsRef = useRef<Set<string>>(new Set());
  const originalTitleRef = useRef(document.title);
  const blinkRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const { setApprovals } = useApprovalContext();
  const hasAutoDismissItems = items.some((item) => item.remainingMs !== null);

  const dismiss = async (item: BubbleItem) => {
    if (!item.sticky) {
      setItems((prev) => prev.filter((candidate) => candidate.id !== item.id));
      return;
    }

    setAcknowledgingIds((prev) => new Set(prev).add(item.id));
    try {
      await consoleApi.acknowledgePushMessage(item.id);
      setItems((prev) => prev.filter((candidate) => candidate.id !== item.id));
    } catch {
      setItems((prev) =>
        prev.map((candidate) =>
          candidate.id === item.id
            ? { ...candidate, acknowledgementError: true }
            : candidate,
        ),
      );
    } finally {
      setAcknowledgingIds((prev) => {
        const next = new Set(prev);
        next.delete(item.id);
        return next;
      });
    }
  };

  useEffect(() => {
    originalTitleRef.current = document.title;
  }, []);

  useEffect(() => {
    const prevApprovalsRef = { current: "" };
    let disposed = false;
    let pollInFlight = false;
    let activeController: AbortController | null = null;
    let consecutiveFailures = 0;

    const rememberSeen = (id: string) => {
      const seen = seenIdsRef.current;
      if (seen.has(id)) return;
      seen.add(id);
      seenOrderRef.current.push(id);
      while (seenOrderRef.current.length > MAX_SEEN_IDS) {
        const oldest = seenOrderRef.current.shift();
        if (oldest) seen.delete(oldest);
      }
    };

    const tick = async () => {
      if (disposed || pollInFlight) return;
      pollInFlight = true;
      const controller = new AbortController();
      activeController = controller;
      let deadline: ReturnType<typeof setTimeout> | null = null;
      try {
        const timeout = new Promise<never>((_, reject) => {
          deadline = setTimeout(() => {
            controller.abort();
            reject(new Error("push-message poll timed out"));
          }, POLL_REQUEST_TIMEOUT_MS);
        });
        const res = await Promise.race([
          consoleApi.getPushMessages(undefined, controller.signal),
          timeout,
        ]);
        if (disposed) return;
        consecutiveFailures = 0;
        setPollPaused(false);

        if (res?.pending_approvals) {
          const serialized = JSON.stringify(res.pending_approvals);
          if (serialized !== prevApprovalsRef.current) {
            prevApprovalsRef.current = serialized;
            setApprovals(res.pending_approvals);
          }
          const notified = notifiedApprovalIdsRef.current;
          for (const approval of res.pending_approvals) {
            const requestId = String(approval.request_id || "");
            if (!requestId || notified.has(requestId)) continue;
            notified.add(requestId);
            void window.electronAPI?.notifySystemBehavior?.({
              id: `approval:${requestId}`,
              type: "permission_required",
              title: "澜台需要你的授权",
              body: `${approval.tool_name || "工具"} 正在等待安全审批`,
            });
          }
          if (notified.size > MAX_SEEN_IDS) notified.clear();
        }

        if (!res?.messages?.length) return;
        const unseen = res.messages.filter(
          (message) => !seenIdsRef.current.has(message.id),
        );
        const toAdd = unseen.slice(0, MAX_NEW_PER_POLL);
        if (toAdd.length === 0) return;
        toAdd.forEach((message) => rememberSeen(message.id));
        setItems((prev) => {
          const visibleIds = new Set(prev.map((item) => item.id));
          const additions = toAdd
            .filter((message) => !visibleIds.has(message.id))
            .map((message) => ({
              ...message,
              remainingMs: message.sticky ? null : AUTO_DISMISS_MS,
            }));
          return [...prev, ...additions];
        });
      } catch {
        if (disposed) return;
        consecutiveFailures += 1;
        if (consecutiveFailures >= 2) setPollPaused(true);
      } finally {
        if (deadline) clearTimeout(deadline);
        if (activeController === controller) activeController = null;
        pollInFlight = false;
        if (!disposed) {
          const delay = document.hidden
            ? HIDDEN_POLL_INTERVAL_MS
            : VISIBLE_POLL_INTERVAL_MS;
          pollRef.current = setTimeout(() => void tick(), delay);
        }
      }
    };

    void tick();
    return () => {
      disposed = true;
      activeController?.abort();
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [setApprovals]);

  useEffect(() => {
    if (!hasAutoDismissItems) return;
    const timer = setInterval(() => {
      if (document.hidden) return;
      setItems((prev) =>
        prev
          .map((item) =>
            item.remainingMs === null
              ? item
              : {
                  ...item,
                  remainingMs: item.remainingMs - COUNTDOWN_STEP_MS,
                },
          )
          .filter((item) => item.remainingMs === null || item.remainingMs > 0),
      );
    }, COUNTDOWN_STEP_MS);
    return () => clearInterval(timer);
  }, [hasAutoDismissItems]);

  useEffect(() => {
    if (items.length === 0 || !document.hidden || blinkRef.current) return;
    const original = originalTitleRef.current;
    let showPrefix = true;
    blinkRef.current = setInterval(() => {
      document.title = showPrefix
        ? `${TITLE_BLINK_PREFIX}${original}`
        : original;
      showPrefix = !showPrefix;
    }, 800);
    return () => {
      if (blinkRef.current) {
        clearInterval(blinkRef.current);
        blinkRef.current = null;
      }
      document.title = original;
    };
  }, [items.length]);

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        if (blinkRef.current) {
          clearInterval(blinkRef.current);
          blinkRef.current = null;
        }
        document.title = originalTitleRef.current;
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  if (items.length === 0 && !pollPaused) return null;

  return (
    <div className={styles.wrap} role="region" aria-label="后台通知">
      {pollPaused ? (
        <div className={`${styles.bubble} ${styles.pollPaused}`} role="status">
          <AlertTriangle size={18} className={styles.warningIcon} aria-hidden />
          <div className={styles.content}>
            <p className={styles.text}>通知更新暂停，正在自动重试</p>
            <p className={styles.meta}>
              现有消息仍保留；请勿因未看到新通知而重复执行任务。
            </p>
          </div>
        </div>
      ) : null}
      {items.map((item) => {
        const sourceLink = getSourceLink(item);
        const occurredAt = formatOccurredAt(item.occurred_at);
        const acknowledging = acknowledgingIds.has(item.id);
        return (
          <div
            key={item.id}
            className={`${styles.bubble} ${item.sticky ? styles.sticky : ""}`}
          >
            <MessageCircle size={18} className={styles.icon} aria-hidden />
            <div className={styles.content}>
              <p className={styles.text} title={item.text}>
                {item.text}
              </p>
              <div className={styles.metaRow}>
                {item.sticky ? <span>需确认</span> : null}
                {item.source_name ? <span>{item.source_name}</span> : null}
                {occurredAt ? <time>{occurredAt}</time> : null}
                {sourceLink ? (
                  <a className={styles.sourceLink} href={sourceLink.href}>
                    {sourceLink.label}
                  </a>
                ) : null}
              </div>
              {item.acknowledgementError ? (
                <p className={styles.ackError} role="alert">
                  未能确认，这条消息仍保留。请稍后重试。
                </p>
              ) : null}
            </div>
            <button
              type="button"
              className={styles.close}
              onClick={() => void dismiss(item)}
              aria-label={item.sticky ? "确认并关闭" : "关闭"}
              disabled={acknowledging}
            >
              <X size={14} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
