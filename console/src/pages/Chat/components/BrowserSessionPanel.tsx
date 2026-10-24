import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Camera,
  Check,
  ChevronDown,
  CircleStop,
  Globe2,
  GripVertical,
  Loader2,
  Maximize2,
  Minimize2,
  MonitorUp,
  Pause,
  Play,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { browserWorkbenchApi } from "../../../api/modules/browserSession";
import type {
  BrowserRuntime,
  BrowserSkillStatus,
  BrowserTaskArtifacts,
  BrowserTaskEvent,
  BrowserTaskRun,
  BrowserWorkbenchConfig,
} from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { toDisplayUrl } from "../utils";
import type { BrowserSessionTarget } from "./BrowserSessionCard";
import styles from "./BrowserSessionPanel.module.less";

interface BrowserSessionPanelProps {
  target: BrowserSessionTarget | null;
  onClose: () => void;
}

type ControlOwner = "agent" | "user";

const STATUS_LABELS: Record<string, string> = {
  queued: "正在准备浏览器",
  running_background: "澜台正在浏览",
  waiting_for_user_confirmation: "等待你的确认",
  manual_takeover_required: "需要你接管",
  paused: "任务已暂停",
  completed: "任务已完成",
  failed: "任务未完成",
  cancelled: "任务已停止",
};

const ACTION_LABELS: Record<string, string> = {
  open: "打开页面",
  navigate: "前往页面",
  snapshot: "读取页面",
  screenshot: "保存截图",
  browser_find: "查找页面内容",
  click: "点击页面",
  type: "输入内容",
  fill: "填写内容",
  select: "选择选项",
  scroll: "浏览更多内容",
  upload_file: "上传文件",
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const PROCESS_EVENT_TYPES = new Set([
  "task_created",
  "task_started",
  "action_waiting_for_confirmation",
  "agent_waiting_for_confirmation",
  "manual_takeover_required",
  "task_paused",
  "task_resumed",
  "task_completed",
  "task_failed",
  "task_cancelled",
  "agent_recovering",
]);
const SIDECAR_WIDTH_KEY = "lantai:browser-sidecar:width";
const MIN_PANEL_WIDTH = 480;
const MIN_CHAT_WIDTH = 390;

function eventLabel(event: BrowserTaskEvent): string {
  const action = ACTION_LABELS[event.action] || "处理页面";
  const labels: Record<string, string> = {
    task_created: "任务已加入队列",
    task_started: "澜台已开始浏览",
    action_started: `正在${action}`,
    action_completed: `${action}完成`,
    action_failed: `${action}失败`,
    action_waiting_for_confirmation: `等待确认${action}`,
    agent_action_completed: `${action}完成`,
    agent_action_failed: `${action}失败`,
    agent_observing: "正在查看当前页面",
    agent_planning: "正在决定下一步",
    agent_checking_policy: "正在检查操作权限",
    agent_executing: `正在${action}`,
    agent_verifying: "正在确认页面结果",
    agent_recovering: "遇到阻碍，正在尝试其他方法",
    agent_waiting_for_confirmation: "需要你确认下一步",
    manual_takeover_required: "需要你接管浏览器",
    task_paused: "任务已暂停",
    task_resumed: "浏览器已交还给澜台",
    task_completed: "任务已完成",
    task_failed: "任务未完成",
    task_cancelled: "任务已停止",
  };
  return labels[event.type] || STATUS_LABELS[event.status] || "正在浏览";
}

function eventObservation(
  event: BrowserTaskEvent,
): Record<string, unknown> | null {
  const observation = event.metadata?.observation;
  return observation && typeof observation === "object"
    ? (observation as Record<string, unknown>)
    : null;
}

function eventScreenshot(event: BrowserTaskEvent): string {
  const observation = eventObservation(event);
  return String(
    event.metadata?.screenshot_path || observation?.screenshot_path || "",
  ).trim();
}

function eventDomain(event: BrowserTaskEvent): string {
  const observation = eventObservation(event);
  const rawUrl = String(observation?.url || event.metadata?.url || "").trim();
  if (!rawUrl) return "";
  try {
    return new URL(rawUrl).hostname;
  } catch {
    return "";
  }
}

function eventDetail(event: BrowserTaskEvent): string {
  const domain = eventDomain(event);
  if (event.type.includes("failed")) {
    return domain
      ? `操作在 ${domain} 未完成，可从此处定位问题`
      : "操作未完成，可从此处定位问题";
  }
  if (
    event.type === "action_waiting_for_confirmation" ||
    event.type === "agent_waiting_for_confirmation"
  ) {
    return "任务已停在安全边界，等待你的决定";
  }
  if (event.type === "manual_takeover_required") {
    return "任务已安全暂停，等待你处理页面阻碍";
  }
  if (event.type === "task_paused") return "后续页面操作已暂停";
  if (event.type === "task_resumed") return "澜台已从当前页面继续";
  if (event.type === "agent_recovering") {
    return domain ? `正在 ${domain} 尝试替代路径` : "正在尝试替代路径";
  }
  if (event.type === "task_completed") return "结果与页面证据已保留";
  if (event.type === "task_cancelled") return "没有继续执行后续页面操作";
  if (domain) return `页面：${domain}`;
  return event.action ? "页面状态已记录" : "任务状态已记录";
}

function processEventTone(event: BrowserTaskEvent): string {
  if (event.type.includes("failed")) return "danger";
  if (
    event.type === "action_waiting_for_confirmation" ||
    event.type === "agent_waiting_for_confirmation" ||
    event.type === "manual_takeover_required" ||
    event.type === "task_paused"
  ) {
    return "warning";
  }
  if (event.type === "task_completed") return "success";
  return "neutral";
}

function processEvents(events: BrowserTaskEvent[]): BrowserTaskEvent[] {
  const meaningful = events.filter(
    (event) =>
      PROCESS_EVENT_TYPES.has(event.type) ||
      event.type === "action_completed" ||
      event.type === "action_failed" ||
      event.type === "agent_action_completed" ||
      event.type === "agent_action_failed" ||
      Boolean(eventScreenshot(event)),
  );
  const source = meaningful.length ? meaningful : events.slice(-10);
  const deduplicated: BrowserTaskEvent[] = [];
  let previousSignature = "";
  for (const event of source) {
    const signature = [
      event.type,
      event.action,
      event.status,
      eventDomain(event),
      eventScreenshot(event),
    ].join("|");
    if (signature === previousSignature) continue;
    previousSignature = signature;
    deduplicated.push(event);
  }
  return deduplicated.slice(-20).reverse();
}

function inferredUrl(
  target: BrowserSessionTarget,
  task: BrowserTaskRun | null,
): string {
  const candidates = [
    target.url,
    task?.metadata?.current_url,
    task?.result?.url,
    task?.target_domains?.[0] ? `https://${task.target_domains[0]}` : "",
  ];
  return (
    candidates.map((item) => String(item || "").trim()).find(Boolean) || ""
  );
}

function displayDomain(url: string, task: BrowserTaskRun | null): string {
  try {
    return new URL(url).hostname;
  } catch {
    return task?.target_domains?.[0] || "当前页面";
  }
}

function runtimeFromEvents(events: BrowserTaskEvent[]): BrowserRuntime | null {
  for (const event of [...events].reverse()) {
    const observation = event.metadata?.observation;
    if (!observation || typeof observation !== "object") continue;
    const runtime = String(
      (observation as Record<string, unknown>).runtime || "",
    );
    if (
      runtime === "electron_bridge" ||
      runtime === "browser_skill" ||
      runtime === "playwright_fallback"
    ) {
      return runtime;
    }
  }
  return null;
}

function observationFromEvents(
  events: BrowserTaskEvent[],
): Record<string, unknown> | null {
  for (const event of [...events].reverse()) {
    const observation = event.metadata?.observation;
    if (observation && typeof observation === "object") {
      return observation as Record<string, unknown>;
    }
  }
  return null;
}

function readableObservationText(events: BrowserTaskEvent[]): string {
  const observation = observationFromEvents(events);
  const rawData = observation?.data;
  if (!rawData || typeof rawData !== "object") return "";
  const nested = (rawData as Record<string, unknown>).data;
  const data =
    nested && typeof nested === "object"
      ? (nested as Record<string, unknown>)
      : (rawData as Record<string, unknown>);
  const raw = String(data.text || data.snapshot || data.visible_text || "");
  return raw
    .split("\n")
    .map((line) =>
      line
        .replace(/^\s*@?e\d+\s+/, "")
        .replace(/^\s*-\s*[a-z][\w-]*\s+/i, "")
        .replace(/\s+\[ref=e\d+\]/g, "")
        .trim(),
    )
    .filter(Boolean)
    .slice(0, 24)
    .join("\n")
    .slice(0, 4000);
}

function resolveRuntime(
  task: BrowserTaskRun | null,
  events: BrowserTaskEvent[],
  config: BrowserWorkbenchConfig | null,
): BrowserRuntime {
  const candidates = [
    task?.pending_confirmation?.runtime,
    task?.metadata?.browser_runtime,
    task?.metadata?.runtime,
    task?.result?.runtime,
    runtimeFromEvents(events),
    config?.primary_runtime,
  ];
  return (
    candidates.find(
      (value) =>
        value === "electron_bridge" ||
        value === "browser_skill" ||
        value === "playwright_fallback",
    ) || "electron_bridge"
  );
}

function initialPanelWidth(): number {
  if (typeof window === "undefined") return 720;
  const stored = Number(window.localStorage.getItem(SIDECAR_WIDTH_KEY));
  const preferred =
    Number.isFinite(stored) && stored > 0 ? stored : window.innerWidth * 0.55;
  return Math.round(
    Math.max(
      MIN_PANEL_WIDTH,
      Math.min(
        preferred,
        Math.max(MIN_PANEL_WIDTH, window.innerWidth - MIN_CHAT_WIDTH),
      ),
    ),
  );
}

function viewportBounds(element: HTMLElement | null) {
  if (!element) return null;
  const rect = element.getBoundingClientRect();
  const bounds = {
    x: Math.round(rect.left),
    y: Math.round(rect.top),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
  return bounds.width > 0 && bounds.height > 0 ? bounds : null;
}

export default function BrowserSessionPanel({
  target,
  onClose,
}: BrowserSessionPanelProps) {
  const { message } = useAppMessage();
  const [task, setTask] = useState<BrowserTaskRun | null>(target?.task || null);
  const [events, setEvents] = useState<BrowserTaskEvent[]>([]);
  const [artifacts, setArtifacts] = useState<BrowserTaskArtifacts | null>(null);
  const [browserConfig, setBrowserConfig] =
    useState<BrowserWorkbenchConfig | null>(null);
  const [browserSkill, setBrowserSkill] = useState<BrowserSkillStatus | null>(
    null,
  );
  const [desktopState, setDesktopState] =
    useState<BrowserWorkbenchState | null>(null);
  const [preview, setPreview] = useState<BrowserSidecarPreview | null>(null);
  const [previewState, setPreviewState] = useState<
    "idle" | "connecting" | "ready" | "error"
  >("idle");
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const [controlOwner, setControlOwner] = useState<ControlOwner>("agent");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [selectedEvidenceId, setSelectedEvidenceId] = useState("");
  const [panelWidth, setPanelWidth] = useState(initialPanelWidth);
  const [expanded, setExpanded] = useState(false);
  const [overlayMode, setOverlayMode] = useState(
    () => window.matchMedia?.("(max-width: 1100px)").matches || false,
  );
  const panelRef = useRef<HTMLElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const previousWidthRef = useRef(panelWidth);
  const takeoverRunRef = useRef("");
  const disposedRef = useRef(false);

  const runId = target?.runId || "";
  const configuredUrl = target ? inferredUrl(target, task) : "";
  const runtime = useMemo(
    () => resolveRuntime(task, events, browserConfig),
    [browserConfig, events, task],
  );
  const internalBrowser = runtime === "electron_bridge";
  const activeTab = useMemo(
    () =>
      desktopState?.tabs.find(
        (item) => item.id === desktopState.active_tab_id,
      ) || null,
    [desktopState],
  );
  const latestObservation = useMemo(
    () => observationFromEvents(events),
    [events],
  );
  const url =
    activeTab?.url || String(latestObservation?.url || "") || configuredUrl;
  const domain = target ? displayDomain(url, task) : "";
  const instruction =
    task?.instruction || target?.instruction || "浏览当前页面";
  const status = task?.status || "running_background";
  const liveInternalBrowser = internalBrowser && !TERMINAL_STATUSES.has(status);
  const recentEvents = useMemo(() => events.slice(-10).reverse(), [events]);
  const timelineEvents = useMemo(() => processEvents(events), [events]);
  const selectedEvidence = useMemo(
    () =>
      timelineEvents.find(
        (event) =>
          event.id === selectedEvidenceId && Boolean(eventScreenshot(event)),
      ) || null,
    [selectedEvidenceId, timelineEvents],
  );
  const keyframeCount = useMemo(
    () => timelineEvents.filter((event) => eventScreenshot(event)).length,
    [timelineEvents],
  );
  const latestActivity = recentEvents[0]
    ? eventLabel(recentEvents[0])
    : ACTION_LABELS[task?.current_action || ""] || STATUS_LABELS[status];
  const latestScreenshot =
    artifacts?.screenshots[
      Math.max(0, (artifacts?.screenshots.length || 1) - 1)
    ] || "";
  const externalObservationText = useMemo(
    () => readableObservationText(events),
    [events],
  );
  const previewUrl = liveInternalBrowser
    ? preview?.data_url || ""
    : latestScreenshot
      ? toDisplayUrl(latestScreenshot)
      : "";
  const browserName =
    browserSkill?.browsers[0]?.browser_name ||
    (runtime === "browser_skill" ? "Chrome" : "外部浏览器");
  const securePage = url.startsWith("https://");
  const latestScreenshotEvent = recentEvents.find(
    (event) =>
      event.action === "screenshot" || Boolean(event.metadata?.screenshot_path),
  );
  const captureTime =
    (liveInternalBrowser ? preview?.captured_at : latestScreenshotEvent?.ts) ||
    "";
  const previewLabel =
    controlOwner === "user"
      ? internalBrowser
        ? "你在操作"
        : `在 ${browserName} 操作`
      : liveInternalBrowser
        ? "实时预览"
        : latestScreenshot
          ? TERMINAL_STATUSES.has(status)
            ? "最后画面"
            : "最新截图"
          : externalObservationText
            ? "页面记录"
            : "等待画面";

  useEffect(() => {
    const query = window.matchMedia?.("(max-width: 1100px)");
    if (!query) return undefined;
    const update = () => setOverlayMode(query.matches);
    update();
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);

  useEffect(() => {
    const clampWidth = () => {
      const maxWidth = Math.max(
        MIN_PANEL_WIDTH,
        window.innerWidth - MIN_CHAT_WIDTH,
      );
      setPanelWidth((current) =>
        Math.max(MIN_PANEL_WIDTH, Math.min(maxWidth, current)),
      );
    };
    window.addEventListener("resize", clampWidth);
    return () => window.removeEventListener("resize", clampWidth);
  }, []);

  const refresh = useCallback(async () => {
    if (!runId) return;
    setLoading(true);
    try {
      const [nextTask, nextEvents, nextArtifacts, nextConfig] =
        await Promise.all([
          browserWorkbenchApi.getTask(runId),
          browserWorkbenchApi.listTaskEvents(runId),
          browserWorkbenchApi.getTaskArtifacts(runId),
          browserWorkbenchApi.getConfig(),
        ]);
      if (disposedRef.current) return;
      setTask(nextTask);
      setEvents(nextEvents);
      setArtifacts(nextArtifacts);
      setBrowserConfig(nextConfig);
      const nextRuntime = resolveRuntime(nextTask, nextEvents, nextConfig);
      if (nextRuntime === "browser_skill") {
        const nextStatus = await browserWorkbenchApi.getBrowserSkillStatus();
        if (!disposedRef.current) setBrowserSkill(nextStatus);
      }
    } catch (error) {
      if (!disposedRef.current) {
        message.error(
          error instanceof Error ? error.message : "读取浏览器任务失败",
        );
      }
    } finally {
      if (!disposedRef.current) setLoading(false);
    }
  }, [message, runId]);

  useEffect(() => {
    if (!runId) return undefined;
    disposedRef.current = false;
    setTask(target?.task || null);
    setEvents([]);
    setArtifacts(null);
    setDesktopState(null);
    setPreview(null);
    setPreviewState("idle");
    setControlOwner("agent");
    setSelectedEvidenceId("");
    takeoverRunRef.current = "";
    void refresh();
    const unsubscribe = browserWorkbenchApi.subscribeTaskEvents(
      runId,
      (event) => {
        if (disposedRef.current) return;
        setEvents((items) =>
          items.some((item) => item.id === event.id)
            ? items
            : [...items, event],
        );
        void browserWorkbenchApi
          .getTask(runId)
          .then((next) => {
            if (!disposedRef.current) setTask(next);
          })
          .catch(() => undefined);
        if (
          event.action === "screenshot" ||
          event.type.includes("completed") ||
          TERMINAL_STATUSES.has(event.status)
        ) {
          void browserWorkbenchApi
            .getTaskArtifacts(runId)
            .then((next) => {
              if (!disposedRef.current) setArtifacts(next);
            })
            .catch(() => undefined);
        }
      },
    );
    return () => {
      disposedRef.current = true;
      unsubscribe();
      if (takeoverRunRef.current === runId) {
        void window.electronAPI?.browserWorkbench?.setSidecarControl?.("agent");
        void browserWorkbenchApi.resumeTask(runId).catch(() => undefined);
        takeoverRunRef.current = "";
      }
    };
  }, [refresh, runId, target?.task]);

  useEffect(() => {
    if (!target || !browserConfig || !liveInternalBrowser) return undefined;
    const desktop = window.electronAPI?.browserWorkbench;
    if (!desktop?.mountSidecar || !desktop.layoutSidecar) {
      setPreviewState("error");
      return undefined;
    }
    let disposed = false;
    let mounted = false;
    let lastBounds = "";
    setPreviewState("connecting");
    const syncBounds = async () => {
      const bounds = viewportBounds(viewportRef.current);
      if (!bounds || disposed) return;
      const signature = JSON.stringify(bounds);
      if (signature === lastBounds && mounted) return;
      lastBounds = signature;
      try {
        const state = mounted
          ? await desktop.layoutSidecar?.(bounds)
          : await desktop.mountSidecar?.({
              run_id: runId,
              profile_id: task?.profile_id || target.profileId || "default",
              url: configuredUrl,
              bounds,
            });
        if (disposed || !state) return;
        mounted = state.ok !== false;
        setDesktopState(state);
        setControlOwner(state.surface?.control_owner || "agent");
        setPreviewState(mounted ? "ready" : "error");
      } catch {
        if (!disposed) setPreviewState("error");
      }
    };
    const frame = window.requestAnimationFrame(() => void syncBounds());
    const observer = new ResizeObserver(() => void syncBounds());
    if (viewportRef.current) observer.observe(viewportRef.current);
    const unsubscribe = desktop.onState?.((state) => {
      if (disposed) return;
      setDesktopState(state);
      setControlOwner(state.surface?.control_owner || "agent");
    });
    return () => {
      disposed = true;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      unsubscribe?.();
      void desktop.unmountSidecar?.();
    };
  }, [
    browserConfig,
    configuredUrl,
    liveInternalBrowser,
    runId,
    target,
    task?.profile_id,
  ]);

  useEffect(() => {
    if (
      !target ||
      !liveInternalBrowser ||
      controlOwner !== "agent" ||
      previewState !== "ready"
    ) {
      return undefined;
    }
    const capture = window.electronAPI?.browserWorkbench?.captureSidecarPreview;
    if (!capture) return undefined;
    let disposed = false;
    let timer = 0;
    const poll = async () => {
      if (document.visibilityState !== "hidden") {
        try {
          const next = await capture();
          if (!disposed && next.ok) setPreview(next);
        } catch {
          // Keep the previous frame; task events still report useful state.
        }
      }
      if (!disposed) timer = window.setTimeout(poll, 1400);
    };
    void poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [controlOwner, liveInternalBrowser, previewState, target]);

  useEffect(() => {
    window.localStorage.setItem(SIDECAR_WIDTH_KEY, String(panelWidth));
  }, [panelWidth]);

  const runControl = useCallback(
    async (action: "pause" | "resume" | "cancel" | "approve" | "deny") => {
      if (!runId) return;
      setBusyAction(action);
      try {
        const next =
          action === "pause"
            ? await browserWorkbenchApi.pauseTask(runId)
            : action === "resume"
              ? await browserWorkbenchApi.resumeTask(runId)
              : action === "cancel"
                ? await browserWorkbenchApi.cancelTask(runId)
                : await browserWorkbenchApi.confirmTask(runId, {
                    approved: action === "approve",
                    reason:
                      action === "approve"
                        ? "用户在主聊天中允许本次浏览器操作"
                        : "用户在主聊天中拒绝本次浏览器操作",
                  });
        setTask(next);
      } catch (error) {
        message.error(
          error instanceof Error ? error.message : "浏览器任务操作失败",
        );
      } finally {
        setBusyAction("");
      }
    },
    [message, runId],
  );

  const beginTakeover = useCallback(async () => {
    if (!runId || TERMINAL_STATUSES.has(status)) return;
    setBusyAction("takeover");
    try {
      const receipt = await browserWorkbenchApi.requestTaskTakeover(runId);
      setTask(receipt.run);
      if (!receipt.ready) {
        throw new Error(
          receipt.message || "当前网页动作尚未结束，请稍后再接管。",
        );
      }
      if (internalBrowser) {
        const state =
          await window.electronAPI?.browserWorkbench?.setSidecarControl?.(
            "user",
          );
        if (!state?.ok) throw new Error(state?.error || "浏览器接管失败");
        setDesktopState(state);
      } else {
        const focused =
          await window.electronAPI?.browserWorkbench?.focusExternalBrowser?.(
            browserName,
          );
        if (!focused?.ok) {
          throw new Error(focused?.error || `无法切换到 ${browserName}`);
        }
        message.success(
          `任务已暂停，请在 ${browserName} 的 Agent Window 中完成操作。`,
        );
      }
      takeoverRunRef.current = runId;
      setControlOwner("user");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "浏览器接管失败");
    } finally {
      setBusyAction("");
    }
  }, [browserName, internalBrowser, message, runId, status]);

  const handBack = useCallback(async () => {
    if (!runId) return;
    setBusyAction("handback");
    try {
      if (internalBrowser) {
        const state =
          await window.electronAPI?.browserWorkbench?.setSidecarControl?.(
            "agent",
          );
        if (!state?.ok) throw new Error(state?.error || "交还浏览器失败");
        setDesktopState(state);
      }
      const resumed = await browserWorkbenchApi.resumeTask(runId);
      setTask(resumed);
      takeoverRunRef.current = "";
      setControlOwner("agent");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "交还浏览器失败");
    } finally {
      setBusyAction("");
    }
  }, [internalBrowser, message, runId]);

  const closePanel = useCallback(async () => {
    if (controlOwner === "user") await handBack();
    onClose();
  }, [controlOwner, handBack, onClose]);
  const closePanelRef = useRef(closePanel);
  useEffect(() => {
    closePanelRef.current = closePanel;
  }, [closePanel]);

  const browserAction = useCallback(
    async (
      action: "go_back" | "go_forward" | "reload" | "select_tab",
      pageId = "",
    ) => {
      if (controlOwner !== "user") return;
      try {
        const state = await window.electronAPI?.browserWorkbench?.action?.({
          action,
          page_id: pageId || desktopState?.active_tab_id || "",
          profile_id: task?.profile_id || target?.profileId || "default",
        });
        if (state) setDesktopState(state);
      } catch (error) {
        message.error(error instanceof Error ? error.message : "网页操作失败");
      }
    },
    [
      controlOwner,
      desktopState?.active_tab_id,
      message,
      target?.profileId,
      task?.profile_id,
    ],
  );

  const resizeFromSeparator = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = panelWidth;
      const maxWidth = Math.max(
        MIN_PANEL_WIDTH,
        window.innerWidth - MIN_CHAT_WIDTH,
      );
      const move = (next: PointerEvent) => {
        const width = Math.round(
          Math.max(
            MIN_PANEL_WIDTH,
            Math.min(maxWidth, startWidth + startX - next.clientX),
          ),
        );
        setPanelWidth(width);
        setExpanded(false);
      };
      const stop = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop, { once: true });
    },
    [panelWidth],
  );

  const resizeWithKeyboard = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const maxWidth = Math.max(
        MIN_PANEL_WIDTH,
        window.innerWidth - MIN_CHAT_WIDTH,
      );
      let next = panelWidth;
      if (event.key === "ArrowLeft") next += 32;
      else if (event.key === "ArrowRight") next -= 32;
      else if (event.key === "Home") next = MIN_PANEL_WIDTH;
      else if (event.key === "End") next = maxWidth;
      else return;
      event.preventDefault();
      setPanelWidth(Math.max(MIN_PANEL_WIDTH, Math.min(maxWidth, next)));
      setExpanded(next >= maxWidth);
    },
    [panelWidth],
  );

  const toggleExpanded = useCallback(() => {
    const maxWidth = Math.max(
      MIN_PANEL_WIDTH,
      window.innerWidth - MIN_CHAT_WIDTH,
    );
    if (expanded) {
      setPanelWidth(previousWidthRef.current);
      setExpanded(false);
      return;
    }
    previousWidthRef.current = panelWidth;
    setPanelWidth(maxWidth);
    setExpanded(true);
  }, [expanded, panelWidth]);

  useEffect(() => {
    if (!target || !overlayMode) return undefined;
    const panel = panelRef.current;
    const chatStage = document.querySelector<HTMLElement>(
      '[data-testid="chat-stage"]',
    );
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    if (chatStage) {
      chatStage.inert = true;
      chatStage.setAttribute("aria-hidden", "true");
    }
    const frame = window.requestAnimationFrame(() => {
      panel
        ?.querySelector<HTMLButtonElement>("[data-sidecar-autofocus]")
        ?.focus();
    });
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        void closePanelRef.current();
        return;
      }
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("hidden"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    panel?.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      panel?.removeEventListener("keydown", handleKeyDown);
      if (chatStage) {
        chatStage.inert = false;
        chatStage.removeAttribute("aria-hidden");
      }
      previousFocus?.focus();
    };
  }, [overlayMode, target]);

  if (!target) return null;

  const panelStyle = {
    "--browser-sidecar-width": `${panelWidth}px`,
  } as CSSProperties;
  const canTakeOver =
    !TERMINAL_STATUSES.has(status) &&
    status !== "waiting_for_user_confirmation";
  const currentStatus =
    controlOwner === "user"
      ? "你正在操作，澜台已暂停"
      : STATUS_LABELS[status] || "澜台正在浏览";

  return (
    <aside
      ref={panelRef}
      className={styles.panel}
      style={panelStyle}
      aria-label="浏览器会话"
      data-control-owner={controlOwner}
      data-runtime={runtime}
      role={overlayMode ? "dialog" : undefined}
      aria-modal={overlayMode || undefined}
      tabIndex={overlayMode ? -1 : undefined}
    >
      {!overlayMode ? (
        <div
          className={styles.resizeHandle}
          role="separator"
          aria-label="调整聊天与浏览器宽度"
          aria-orientation="vertical"
          aria-valuemin={MIN_PANEL_WIDTH}
          aria-valuemax={Math.max(
            MIN_PANEL_WIDTH,
            window.innerWidth - MIN_CHAT_WIDTH,
          )}
          aria-valuenow={panelWidth}
          tabIndex={0}
          onPointerDown={resizeFromSeparator}
          onKeyDown={resizeWithKeyboard}
        >
          <GripVertical size={14} />
        </div>
      ) : null}

      <header className={styles.header}>
        <div className={styles.heading}>
          <Globe2 size={16} />
          <strong>浏览器</strong>
          <span>
            {internalBrowser ? "澜台浏览器" : `${browserName} · 工作`}
          </span>
        </div>
        <div className={styles.headerActions}>
          {!overlayMode ? (
            <button
              type="button"
              onClick={toggleExpanded}
              aria-label={expanded ? "恢复分栏宽度" : "放大浏览器区域"}
              title={expanded ? "恢复宽度" : "放大"}
            >
              {expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
            </button>
          ) : null}
          <button
            type="button"
            data-sidecar-autofocus
            onClick={() => void closePanel()}
            aria-label="收起浏览器"
            title="收起，任务继续"
          >
            <X size={16} />
          </button>
        </div>
      </header>

      {internalBrowser && desktopState?.tabs.length ? (
        <nav
          className={styles.tabStrip}
          aria-label="浏览器标签页"
          role="tablist"
        >
          {desktopState.tabs.slice(0, 5).map((tab, tabIndex) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={tab.id === desktopState.active_tab_id}
              tabIndex={tab.id === desktopState.active_tab_id ? 0 : -1}
              disabled={controlOwner !== "user"}
              data-active={tab.id === desktopState.active_tab_id}
              onClick={() => void browserAction("select_tab", tab.id)}
              onKeyDown={(event) => {
                if (controlOwner !== "user") return;
                const visibleTabs = desktopState.tabs.slice(0, 5);
                let nextIndex: number;
                if (event.key === "ArrowLeft") {
                  nextIndex =
                    (tabIndex - 1 + visibleTabs.length) % visibleTabs.length;
                } else if (event.key === "ArrowRight") {
                  nextIndex = (tabIndex + 1) % visibleTabs.length;
                } else if (event.key === "Home") {
                  nextIndex = 0;
                } else if (event.key === "End") {
                  nextIndex = visibleTabs.length - 1;
                } else {
                  return;
                }
                event.preventDefault();
                const nextTab = visibleTabs[nextIndex];
                void browserAction("select_tab", nextTab.id);
                event.currentTarget
                  .closest('[role="tablist"]')
                  ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
                  .item(nextIndex)
                  .focus();
              }}
            >
              <span>
                {tab.loading ? (
                  <Loader2 size={11} className={styles.spin} />
                ) : (
                  <Globe2 size={11} />
                )}
              </span>
              {tab.title || displayDomain(tab.url, task)}
            </button>
          ))}
          {desktopState.tabs.length > 5 ? (
            <span className={styles.moreTabs}>
              +{desktopState.tabs.length - 5}
            </span>
          ) : null}
        </nav>
      ) : null}

      <div className={styles.browserBar}>
        <div className={styles.navigation}>
          <button
            type="button"
            aria-label="后退"
            disabled={controlOwner !== "user" || !activeTab?.can_go_back}
            onClick={() => void browserAction("go_back")}
          >
            <ArrowLeft size={15} />
          </button>
          <button
            type="button"
            aria-label="前进"
            disabled={controlOwner !== "user" || !activeTab?.can_go_forward}
            onClick={() => void browserAction("go_forward")}
          >
            <ArrowRight size={15} />
          </button>
          <button
            type="button"
            aria-label="刷新网页"
            disabled={controlOwner !== "user" || !internalBrowser}
            onClick={() => void browserAction("reload")}
          >
            <RefreshCw size={14} />
          </button>
        </div>
        <div className={styles.address} title={url}>
          {securePage ? <ShieldCheck size={13} /> : null}
          <span>{domain}</span>
        </div>
        <span className={styles.liveState} data-state={controlOwner}>
          <span>{previewLabel}</span>
          {captureTime ? (
            <time dateTime={captureTime}>
              {new Date(captureTime).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </time>
          ) : null}
        </span>
      </div>

      <div
        ref={viewportRef}
        className={styles.browserViewport}
        role="region"
        aria-label={
          controlOwner === "user" && internalBrowser
            ? "可交互网页区域"
            : `${previewLabel}：${domain}`
        }
      >
        {previewUrl ? (
          <img src={previewUrl} alt={`${domain} 当前页面预览`} />
        ) : !liveInternalBrowser && externalObservationText ? (
          <div className={styles.semanticPreview}>
            <span>当前页面内容</span>
            <strong>{domain}</strong>
            <p>{externalObservationText}</p>
          </div>
        ) : (
          <div className={styles.previewPlaceholder}>
            {previewState === "connecting" || loading ? (
              <Loader2 size={24} className={styles.spin} />
            ) : runtime === "browser_skill" ? (
              <MonitorUp size={25} />
            ) : (
              <Globe2 size={25} />
            )}
            <strong>
              {runtime === "browser_skill"
                ? `任务正在 ${browserName} 中运行`
                : previewState === "error"
                  ? "暂时无法显示网页"
                  : "正在连接网页"}
            </strong>
            <span>
              {runtime === "browser_skill"
                ? "有新的任务截图时会在这里更新；接管期间停止采集。"
                : previewState === "error"
                  ? "任务状态仍会继续更新；收起后可从任务卡重新打开。"
                  : "网页准备好后会自动显示。"}
            </span>
          </div>
        )}
      </div>

      {controlOwner === "user" && !internalBrowser ? (
        <div className={styles.externalControlNotice} role="status">
          <MonitorUp size={15} />
          <span>
            请在 {browserName} 的 BrowserSkill Agent Window
            中操作，完成后交还给澜台。
          </span>
        </div>
      ) : null}

      {status === "waiting_for_user_confirmation" ? (
        <section className={styles.confirmation} aria-label="浏览器操作确认">
          <div>
            <strong>需要你的确认</strong>
            <span>澜台准备执行一次会改变页面的操作。</span>
          </div>
          <button
            type="button"
            className={styles.primaryAction}
            onClick={() => void runControl("approve")}
            disabled={Boolean(busyAction)}
          >
            <Check size={14} />
            允许一次
          </button>
          <button
            type="button"
            onClick={() => void runControl("deny")}
            disabled={Boolean(busyAction)}
          >
            拒绝
          </button>
        </section>
      ) : null}

      <footer className={styles.controlBar}>
        <div className={styles.taskState} aria-live="polite">
          <span data-owner={controlOwner} />
          <div>
            <strong>{currentStatus}</strong>
            <small>{latestActivity || instruction}</small>
          </div>
        </div>
        <div className={styles.controls} aria-label="浏览器任务控制">
          {controlOwner === "user" ? (
            <button
              type="button"
              className={styles.primaryAction}
              onClick={() => void handBack()}
              disabled={Boolean(busyAction)}
            >
              <Play size={14} />
              交还给澜台
            </button>
          ) : canTakeOver ? (
            <button
              type="button"
              className={styles.primaryAction}
              onClick={() => void beginTakeover()}
              disabled={Boolean(busyAction)}
            >
              <Pause size={14} />
              {internalBrowser ? "我来操作" : `切换到 ${browserName}`}
            </button>
          ) : null}
          {status === "paused" && controlOwner === "agent" ? (
            <button
              type="button"
              onClick={() => void runControl("resume")}
              disabled={Boolean(busyAction)}
            >
              <Play size={14} />
              继续任务
            </button>
          ) : null}
          {!TERMINAL_STATUSES.has(status) ? (
            <button
              type="button"
              onClick={() => void runControl("cancel")}
              disabled={Boolean(busyAction)}
            >
              <CircleStop size={14} />
              停止
            </button>
          ) : null}
        </div>
      </footer>

      <section className={styles.details}>
        <button
          type="button"
          aria-expanded={detailsOpen}
          onClick={() => setDetailsOpen((value) => !value)}
        >
          <span>查看过程</span>
          <em>
            {task?.planned_action_count
              ? `${task.completed_action_count} / ${task.planned_action_count} 步`
              : `${timelineEvents.length} 个关键步骤`}
            {keyframeCount ? ` · ${keyframeCount} 个画面` : ""}
          </em>
          <ChevronDown size={14} data-open={detailsOpen} />
        </button>
        {detailsOpen ? (
          <div className={styles.detailsBody}>
            {timelineEvents.length ? (
              <ol className={styles.timeline} aria-label="浏览器关键步骤">
                {timelineEvents.map((event, index) => {
                  const screenshot = eventScreenshot(event);
                  const selected = selectedEvidenceId === event.id;
                  return (
                    <li
                      key={event.id}
                      data-tone={processEventTone(event)}
                      aria-current={index === 0 ? "step" : undefined}
                    >
                      {screenshot ? (
                        <button
                          type="button"
                          className={styles.keyframe}
                          aria-label={`${selected ? "收起" : "查看"}${eventLabel(event)}的页面画面`}
                          aria-pressed={selected}
                          onClick={() =>
                            setSelectedEvidenceId(selected ? "" : event.id)
                          }
                        >
                          <img src={toDisplayUrl(screenshot)} alt="" />
                          <Maximize2 size={12} aria-hidden="true" />
                        </button>
                      ) : (
                        <span className={styles.timelineMarker} />
                      )}
                      <span className={styles.timelineCopy}>
                        <strong>{eventLabel(event)}</strong>
                        <small>{eventDetail(event)}</small>
                      </span>
                      <time dateTime={event.ts}>
                        {new Date(event.ts).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </time>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <p>{loading ? "正在读取任务过程…" : "等待澜台开始浏览"}</p>
            )}
            {selectedEvidence ? (
              <figure className={styles.evidencePreview}>
                <div>
                  <figcaption>
                    <strong>{eventLabel(selectedEvidence)}</strong>
                    <span>
                      {new Date(selectedEvidence.ts).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  </figcaption>
                  <button
                    type="button"
                    onClick={() => setSelectedEvidenceId("")}
                    aria-label="收起页面画面"
                  >
                    <X size={14} />
                  </button>
                </div>
                <img
                  src={toDisplayUrl(eventScreenshot(selectedEvidence))}
                  alt={`${eventDomain(selectedEvidence) || "浏览器"} 在${eventLabel(selectedEvidence)}时的页面画面`}
                />
              </figure>
            ) : null}
            {artifacts ? (
              <div className={styles.evidence}>
                <Camera size={14} />
                <span>{artifacts.screenshots.length} 张页面截图</span>
                <span>{artifacts.observation_summaries.length} 条页面记录</span>
              </div>
            ) : null}
          </div>
        ) : null}
      </section>
    </aside>
  );
}
