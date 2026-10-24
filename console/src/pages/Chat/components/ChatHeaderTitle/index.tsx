import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Dropdown, Input, Modal, message } from "antd";
import { InboxOutlined, MoreOutlined, RedoOutlined } from "@ant-design/icons";
import {
  SparkCopyLine,
  SparkDeleteLine,
  SparkDownLine,
  SparkEditLine,
  SparkMarkFill,
  SparkMarkLine,
  SparkSearchLine,
} from "@/vendor/lantaiIcons";
import {
  useChatAnywhereSessionsState,
  type ILantaiChatSession,
} from "@/vendor/lantaiChat";
import { useTranslation } from "react-i18next";
import type {
  ChatRuntimeState,
  ChatStatus,
  ChatTurnEventPreview,
} from "../../../../api/types/chat";
import sessionApi from "../../sessionApi";
import { getChannelLabel } from "../../../Control/Channels/components";
import { useAgentStore } from "../../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../../utils/latestRequest";
import { chatApi } from "../../../../api/modules/chat";
import {
  getSessionPreview as getPresentationPreview,
  getSessionStatus,
  isBlankSession,
  matchesSessionFilter,
  type SessionListFilter,
} from "../ChatSessionDrawer/presentation";
import { saveRetryDraft } from "../../retryDraft";
import styles from "./index.module.less";

interface ExtendedChatSession extends ILantaiChatSession {
  realId?: string;
  sessionId?: string;
  userId?: string;
  channel?: string;
  createdAt?: string | null;
  updatedAt?: string | null;
  meta?: Record<string, unknown>;
  status?: ChatStatus;
  generating?: boolean;
  pinned?: boolean;
  archived?: boolean;
  runtimeState?: ChatRuntimeState | null;
  turnEventPreview?: ChatTurnEventPreview | null;
}

interface SessionGroup {
  key: "pinned" | "today" | "yesterday" | "recent" | "older";
  label: string;
  sessions: ExtendedChatSession[];
  totalCount: number;
}

const FALLBACK_TITLE = "chat.newSessionTitle";
const COLLAPSED_GROUPS_KEY = "lantai:chat-header-collapsed-groups";
const INITIAL_VISIBLE_SESSION_COUNT = 80;
const VISIBLE_SESSION_PAGE_SIZE = 80;

const SESSION_FILTERS: Array<{ key: SessionListFilter; label: string }> = [
  { key: "all", label: "全部" },
  { key: "active", label: "进行中" },
  { key: "attention", label: "待处理" },
  { key: "completed", label: "已完成" },
  { key: "archived", label: "已归档" },
];

const areSessionListsEquivalent = (
  current: ILantaiChatSession[],
  next: ILantaiChatSession[],
): boolean =>
  current === next ||
  (current.length === next.length &&
    current.every(
      (session, index) =>
        JSON.stringify(session) === JSON.stringify(next[index]),
    ));

const loadCollapsedGroups = (): Record<SessionGroup["key"], boolean> => {
  const defaults = {
    pinned: false,
    today: false,
    yesterday: false,
    recent: false,
    older: false,
  };
  try {
    const stored = JSON.parse(
      window.localStorage.getItem(COLLAPSED_GROUPS_KEY) || "{}",
    );
    if (stored && typeof stored === "object") {
      return {
        pinned: stored.pinned === true,
        today: stored.today === true,
        yesterday: stored.yesterday === true,
        recent: stored.recent === true,
        older: stored.older === true,
      };
    }
  } catch {
    // Ignore malformed or restricted storage and show every group.
  }
  return defaults;
};

const formatSessionTitle = (
  value: string | null | undefined,
  fallback: string,
): string => {
  const title = String(value || "")
    .replace(/^#{1,6}\s+/, "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[*_~`]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return title || fallback;
};

const startOfLocalDay = (date: Date): Date =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate());

const getSessionTime = (session: ExtendedChatSession): string | null =>
  session.updatedAt || session.createdAt || null;

const getSessionTimestamp = (session: ExtendedChatSession): number => {
  const raw = getSessionTime(session);
  if (!raw) return 0;
  const time = new Date(raw).getTime();
  return Number.isNaN(time) ? 0 : time;
};

const getGroupKey = (session: ExtendedChatSession): SessionGroup["key"] => {
  if (session.pinned && !session.archived) return "pinned";
  const raw = getSessionTime(session);
  if (!raw) return "older";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "older";

  const today = startOfLocalDay(new Date());
  const sessionDay = startOfLocalDay(date);
  const diffDays = Math.floor(
    (today.getTime() - sessionDay.getTime()) / 86_400_000,
  );

  if (diffDays <= 0) return "today";
  if (diffDays === 1) return "yesterday";
  if (diffDays <= 7) return "recent";
  return "older";
};

const formatSessionTime = (raw: string | null | undefined): string => {
  if (!raw) return "时间未知";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "时间未知";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
};

const getMetaText = (
  meta: Record<string, unknown> | undefined,
  keys: string[],
): string => {
  if (!meta) return "";
  for (const key of keys) {
    const value = meta[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
};

const getSessionPreview = (session: ExtendedChatSession): string => {
  const presentationPreview = getPresentationPreview(session);
  if (presentationPreview) return presentationPreview;
  const fromMeta = getMetaText(session.meta, [
    "last_message_preview",
    "lastMessagePreview",
    "preview",
    "snippet",
    "summary",
    "description",
  ]);
  if (fromMeta) return fromMeta;
  if (session.generating || session.status === "running") return "正在回复中";
  return session.pinned ? "已置顶" : "会话记录";
};

const getBackendChatId = (session: ExtendedChatSession): string | null => {
  if (session.realId) return session.realId;
  return /^\d+$/.test(session.id) ? null : session.id;
};

const getRetryTurnId = (session: ExtendedChatSession): string => {
  const pending = session.meta?.pending_turn;
  if (pending && typeof pending === "object" && !Array.isArray(pending)) {
    const record = pending as Record<string, unknown>;
    const pendingId = record.run_id || record.turn_id;
    if (typeof pendingId === "string" && pendingId.trim()) {
      return pendingId.trim();
    }
  }
  return (
    getMetaText(session.meta, [
      "last_turn_outcome_run_id",
      "last_turn_run_id",
    ]) ||
    getBackendChatId(session) ||
    session.id
  );
};

const ChatHeaderTitle: React.FC = () => {
  const { t } = useTranslation();
  const { selectedAgent } = useAgentStore();
  const { sessions, currentSessionId, setCurrentSessionId, setSessions } =
    useChatAnywhereSessionsState();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<SessionListFilter>("all");
  const [visibleLimit, setVisibleLimit] = useState(
    INITIAL_VISIBLE_SESSION_COUNT,
  );
  const [keyboardSessionId, setKeyboardSessionId] = useState<string | null>(
    null,
  );
  const listLoadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());
  const renameComposingRef = useRef(false);
  const renameSubmittingRef = useRef(false);
  const deletingSessionIdsRef = useRef(new Set<string>());
  const sessionButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const [collapsed, setCollapsed] =
    useState<Record<SessionGroup["key"], boolean>>(loadCollapsedGroups);

  const currentSession = sessions.find((s) => s.id === currentSessionId);
  const fallbackTitle = t(FALLBACK_TITLE, "新会话");
  const chatName = formatSessionTitle(currentSession?.name, fallbackTitle);

  const sortedSessions = useMemo(() => {
    return [...sessions]
      .map((session) => session as ExtendedChatSession)
      .sort((a, b) => {
        if (a.pinned && !b.pinned) return -1;
        if (!a.pinned && b.pinned) return 1;
        return getSessionTimestamp(b) - getSessionTimestamp(a);
      });
  }, [sessions]);

  const filteredSessions = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return sortedSessions.filter((session) => {
      if (isBlankSession(session)) return false;
      if (!matchesSessionFilter(session, filter)) return false;
      if (!normalizedQuery) return true;
      const haystack = [
        formatSessionTitle(session.name, fallbackTitle),
        getSessionPreview(session),
        getMetaText(session.meta, ["last_turn_text"]),
      ]
        .join(" ")
        .toLocaleLowerCase();
      return haystack.includes(normalizedQuery);
    });
  }, [fallbackTitle, filter, query, sortedSessions]);

  const visibleSessions = useMemo(
    () => filteredSessions.slice(0, visibleLimit),
    [filteredSessions, visibleLimit],
  );

  const groups = useMemo<SessionGroup[]>(() => {
    const next: SessionGroup[] = [
      { key: "pinned", label: "置顶", sessions: [], totalCount: 0 },
      { key: "today", label: "今天", sessions: [], totalCount: 0 },
      { key: "yesterday", label: "昨天", sessions: [], totalCount: 0 },
      { key: "recent", label: "近 7 天", sessions: [], totalCount: 0 },
      { key: "older", label: "更早", sessions: [], totalCount: 0 },
    ];
    const byKey = new Map(next.map((group) => [group.key, group]));
    for (const session of filteredSessions) {
      const group = byKey.get(getGroupKey(session));
      if (group) group.totalCount += 1;
    }
    for (const session of visibleSessions) {
      byKey.get(getGroupKey(session))?.sessions.push(session);
    }
    return next.filter((group) => group.sessions.length > 0);
  }, [filteredSessions, visibleSessions]);

  const navigableSessions = useMemo(
    () =>
      groups.flatMap((group) => (collapsed[group.key] ? [] : group.sessions)),
    [collapsed, groups],
  );
  const hasMoreSessions = visibleLimit < filteredSessions.length;

  const refreshSessions = useCallback(async () => {
    const ownsState = listLoadGateRef.current.begin();
    setLoading(true);
    try {
      const list = await sessionApi.getSessionList();
      if (ownsState() && !areSessionListsEquivalent(sessions, list)) {
        setSessions?.(list);
      }
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to refresh session list:", error);
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, [sessions, setSessions]);

  useEffect(() => {
    const listLoadGate = listLoadGateRef.current;
    const mutationScope = mutationScopeRef.current;
    listLoadGate.invalidate();
    mutationScope.invalidate();
    setOpen(false);
    setLoading(false);
    setEditingSessionId(null);
    setEditValue("");
    setQuery("");
    setFilter("all");
    setVisibleLimit(INITIAL_VISIBLE_SESSION_COUNT);
    setKeyboardSessionId(null);
    renameSubmittingRef.current = false;
    return () => {
      listLoadGate.invalidate();
      mutationScope.invalidate();
    };
  }, [selectedAgent]);

  const handleOpenChange = useCallback(
    (nextOpen: boolean) => {
      if (nextOpen === open) return;
      setOpen(nextOpen);
      if (nextOpen) {
        setKeyboardSessionId(currentSessionId || null);
        void refreshSessions();
      }
    },
    [currentSessionId, open, refreshSessions],
  );

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      sessionApi.selectSession(sessionId);
      setCurrentSessionId?.(sessionId);
      setOpen(false);
    },
    [setCurrentSessionId],
  );

  const toggleGroup = useCallback((key: SessionGroup["key"]) => {
    setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  useEffect(() => {
    setVisibleLimit(INITIAL_VISIBLE_SESSION_COUNT);
    setKeyboardSessionId(null);
  }, [filter, query]);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        COLLAPSED_GROUPS_KEY,
        JSON.stringify(collapsed),
      );
    } catch {
      // Ignore storage failures in restricted browser contexts.
    }
  }, [collapsed]);

  const handleRenameStart = useCallback(
    (session: ExtendedChatSession) => {
      renameSubmittingRef.current = false;
      setEditingSessionId(session.id);
      setEditValue(formatSessionTitle(session.name, fallbackTitle));
    },
    [fallbackTitle],
  );

  const handleRenameCancel = useCallback(() => {
    renameSubmittingRef.current = false;
    setEditingSessionId(null);
    setEditValue("");
  }, []);

  const handleRenameSubmit = useCallback(async () => {
    if (!editingSessionId || renameSubmittingRef.current) return;

    const session = sessions.find((item) => item.id === editingSessionId) as
      | ExtendedChatSession
      | undefined;
    const nextName = editValue.trim();
    if (!session || !nextName) {
      handleRenameCancel();
      return;
    }

    if (nextName === formatSessionTitle(session.name, fallbackTitle)) {
      handleRenameCancel();
      return;
    }

    renameSubmittingRef.current = true;
    const ownsState = mutationScopeRef.current.capture();
    try {
      const backendId = getBackendChatId(session);
      if (backendId) {
        await chatApi.updateChat(backendId, { name: nextName });
      }
      if (!ownsState()) return;

      const nextSessions = await sessionApi.updateSession({
        id: session.id,
        name: nextName,
      });
      if (!ownsState()) return;
      setSessions?.(nextSessions);
      handleRenameCancel();
    } catch (error) {
      if (!ownsState()) return;
      renameSubmittingRef.current = false;
      console.error("Failed to rename chat:", error);
    }
  }, [
    editValue,
    editingSessionId,
    fallbackTitle,
    handleRenameCancel,
    sessions,
    setSessions,
  ]);

  const handleSessionPatch = useCallback(
    async (
      session: ExtendedChatSession,
      patch: { pinned?: boolean; archived?: boolean },
    ) => {
      const backendId = getBackendChatId(session);
      if (!backendId) return;
      const ownsState = mutationScopeRef.current.capture();
      try {
        await chatApi.updateChat(backendId, patch);
        if (!ownsState()) return;
        const nextSessions = await sessionApi.updateSession({
          id: session.id,
          ...patch,
        } as Partial<ILantaiChatSession>);
        if (ownsState()) setSessions?.(nextSessions);
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to update chat:", error);
        void message.error("会话状态更新失败");
      }
    },
    [setSessions],
  );

  const handlePinToggle = useCallback(
    (session: ExtendedChatSession) =>
      handleSessionPatch(session, { pinned: !session.pinned }),
    [handleSessionPatch],
  );

  const handleArchiveToggle = useCallback(
    (session: ExtendedChatSession) =>
      handleSessionPatch(
        session,
        session.archived
          ? { archived: false }
          : { archived: true, pinned: false },
      ),
    [handleSessionPatch],
  );

  const handleCopyLink = useCallback(async (session: ExtendedChatSession) => {
    const chatId = getBackendChatId(session);
    if (!chatId) return;
    const url = new URL(window.location.href);
    url.hash = `/chat/${encodeURIComponent(chatId)}`;
    try {
      await navigator.clipboard.writeText(url.toString());
      void message.success("会话链接已复制");
    } catch (error) {
      console.error("Failed to copy chat link:", error);
      void message.error("复制链接失败");
    }
  }, []);

  const handleRetry = useCallback(
    async (session: ExtendedChatSession) => {
      const chatId = getBackendChatId(session);
      if (!chatId) return;
      const ownsState = mutationScopeRef.current.capture();
      try {
        const result = await chatApi.retryTurn(chatId, getRetryTurnId(session));
        if (!ownsState()) return;
        if (!result.text) {
          void message.warning("当前任务没有可重试的输入");
          return;
        }
        saveRetryDraft(chatId, result.text);
        void message.success("已恢复上次输入，可确认后重新发送");
        handleSelectSession(session.id);
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to prepare chat retry:", error);
        void message.error("准备重试失败");
      }
    },
    [handleSelectSession],
  );

  const handleListScroll = useCallback(
    (event: React.UIEvent<HTMLDivElement>) => {
      if (!hasMoreSessions) return;
      const target = event.currentTarget;
      if (target.scrollHeight - target.scrollTop - target.clientHeight < 96) {
        setVisibleLimit((current) => current + VISIBLE_SESSION_PAGE_SIZE);
      }
    },
    [hasMoreSessions],
  );

  const handleListKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (!navigableSessions.length) return;
      if (event.key === "Escape") {
        setOpen(false);
        return;
      }
      const currentIndex = navigableSessions.findIndex(
        (session) => session.id === keyboardSessionId,
      );
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        const fallbackIndex = direction > 0 ? -1 : 0;
        const nextIndex =
          (currentIndex === -1 ? fallbackIndex : currentIndex) + direction;
        const boundedIndex = Math.max(
          0,
          Math.min(navigableSessions.length - 1, nextIndex),
        );
        const nextSession = navigableSessions[boundedIndex];
        setKeyboardSessionId(nextSession.id);
        sessionButtonRefs.current.get(nextSession.id)?.focus();
        return;
      }
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      const current = navigableSessions[currentIndex];
      if (!current) return;
      const group = groups.find((candidate) =>
        candidate.sessions.some((session) => session.id === current.id),
      );
      if (!group) return;
      event.preventDefault();
      setCollapsed((previous) => ({
        ...previous,
        [group.key]: event.key === "ArrowLeft",
      }));
    },
    [groups, keyboardSessionId, navigableSessions],
  );

  const handleDeleteConfirmed = useCallback(
    async (session: ExtendedChatSession) => {
      if (deletingSessionIdsRef.current.has(session.id)) return;
      deletingSessionIdsRef.current.add(session.id);
      const ownsState = mutationScopeRef.current.capture();
      try {
        const nextSessions = await sessionApi.removeSession(session);
        if (!ownsState()) return;

        setSessions?.(nextSessions);
        if (currentSessionId === session.id) {
          const nextSession = nextSessions[0];
          setCurrentSessionId?.(nextSession?.id);
          if (nextSession?.id) sessionApi.selectSession(nextSession.id);
        }
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to delete chat:", error);
      } finally {
        deletingSessionIdsRef.current.delete(session.id);
      }
    },
    [currentSessionId, setCurrentSessionId, setSessions],
  );

  const handleDeleteRequest = useCallback(
    (session: ExtendedChatSession) => {
      const name = formatSessionTitle(session.name, fallbackTitle);
      Modal.confirm({
        title: t("chat.sessionActions.deleteTitle", {
          name,
          defaultValue: `删除“${name}”？`,
        }),
        content: t(
          "chat.sessionActions.deleteDescription",
          "会话记录将被永久删除，无法恢复。",
        ),
        okText: t("chat.sessionActions.deleteConfirm", "删除"),
        cancelText: t("common.cancel", "取消"),
        okButtonProps: { danger: true },
        centered: true,
        onOk: () => handleDeleteConfirmed(session),
      });
    },
    [fallbackTitle, handleDeleteConfirmed, t],
  );

  const hasSessionGroups = groups.length > 0;

  const dropdownContent = (
    <div className={styles.panel} onKeyDown={handleListKeyDown}>
      <div className={styles.listTools}>
        <div className={styles.contextBoundaryNotice} role="note">
          <span className={styles.contextBoundaryTitle}>切换会话</span>
          <span>每个会话的消息与任务上下文彼此独立</span>
        </div>
        <Input
          allowClear
          className={styles.searchInput}
          value={query}
          prefix={<SparkSearchLine size={15} aria-hidden="true" />}
          placeholder="搜索标题、请求或结果"
          aria-label="搜索会话"
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className={styles.filters} role="tablist" aria-label="会话筛选">
          {SESSION_FILTERS.map((item) => (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={filter === item.key}
              className={[
                styles.filterButton,
                filter === item.key ? styles.filterButtonActive : "",
              ].join(" ")}
              onClick={() => setFilter(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className={styles.listSummary} aria-live="polite">
          <span>{filteredSessions.length} 个会话</span>
          {loading && hasSessionGroups ? <span>正在同步…</span> : null}
        </div>
      </div>
      {loading && !hasSessionGroups ? (
        <div
          className={styles.loadingSkeleton}
          role="status"
          aria-label={t("chat.loadingSessions", "正在读取会话")}
        >
          {[0, 1, 2, 3].map((row) => (
            <div key={row} className={styles.skeletonRow}>
              <span className={styles.skeletonStatus} />
              <span className={styles.skeletonCopy}>
                <span className={styles.skeletonTitle} />
                <span className={styles.skeletonPreview} />
              </span>
            </div>
          ))}
        </div>
      ) : !hasSessionGroups ? (
        <div className={styles.empty}>
          {query.trim()
            ? "没有匹配的会话"
            : filter === "archived"
              ? "暂无归档会话"
              : "暂无会话"}
        </div>
      ) : (
        <div
          className={styles.sessionList}
          role="listbox"
          aria-label="会话列表"
          onScroll={handleListScroll}
        >
          {groups.map((group) => {
            const isCollapsed = collapsed[group.key];
            return (
              <section className={styles.group} key={group.key}>
                <button
                  type="button"
                  className={styles.groupHeader}
                  onClick={() => toggleGroup(group.key)}
                  onKeyDown={(event) => {
                    if (
                      event.key !== "ArrowLeft" &&
                      event.key !== "ArrowRight"
                    ) {
                      return;
                    }
                    event.preventDefault();
                    setCollapsed((previous) => ({
                      ...previous,
                      [group.key]: event.key === "ArrowLeft",
                    }));
                  }}
                  aria-expanded={!isCollapsed}
                >
                  <SparkDownLine
                    className={[
                      styles.groupArrow,
                      isCollapsed ? styles.groupArrowCollapsed : "",
                    ].join(" ")}
                  />
                  <span className={styles.groupTitle}>{group.label}</span>
                  <span className={styles.groupCount}>{group.totalCount}</span>
                </button>
                {!isCollapsed && (
                  <div className={styles.groupItems}>
                    {group.sessions.map((session) => {
                      const channelKey = session.channel?.trim() || "";
                      const channelLabel = channelKey
                        ? getChannelLabel(channelKey, t)
                        : "默认渠道";
                      const active = session.id === currentSessionId;
                      const sessionTime = formatSessionTime(
                        getSessionTime(session),
                      );
                      const sessionTitle = formatSessionTitle(
                        session.name,
                        fallbackTitle,
                      );
                      const isEditing = editingSessionId === session.id;
                      const sessionStatus = getSessionStatus(session);
                      const canRetry =
                        ["warning", "danger"].includes(sessionStatus.tone) &&
                        Boolean(getMetaText(session.meta, ["last_turn_text"]));
                      return (
                        <div
                          key={session.id}
                          className={[
                            styles.sessionItem,
                            active ? styles.sessionItemActive : "",
                            isEditing ? styles.sessionItemEditing : "",
                          ].join(" ")}
                        >
                          {isEditing ? (
                            <Input
                              autoFocus
                              className={styles.sessionRenameInput}
                              value={editValue}
                              aria-label={t(
                                "chat.sessionActions.renameInput",
                                "会话名称",
                              )}
                              onChange={(event) =>
                                setEditValue(event.target.value)
                              }
                              onCompositionStart={() => {
                                renameComposingRef.current = true;
                              }}
                              onCompositionEnd={() => {
                                renameComposingRef.current = false;
                              }}
                              onPressEnter={() => {
                                if (!renameComposingRef.current) {
                                  void handleRenameSubmit();
                                }
                              }}
                              onKeyDown={(event) => {
                                if (event.key === "Escape") {
                                  event.preventDefault();
                                  handleRenameCancel();
                                }
                              }}
                              onBlur={() => {
                                window.setTimeout(() => {
                                  if (
                                    !renameComposingRef.current &&
                                    !renameSubmittingRef.current
                                  ) {
                                    void handleRenameSubmit();
                                  }
                                }, 0);
                              }}
                            />
                          ) : (
                            <>
                              <button
                                ref={(node) => {
                                  if (node) {
                                    sessionButtonRefs.current.set(
                                      session.id,
                                      node,
                                    );
                                  } else {
                                    sessionButtonRefs.current.delete(
                                      session.id,
                                    );
                                  }
                                }}
                                type="button"
                                className={styles.sessionSelectButton}
                                onClick={() => handleSelectSession(session.id)}
                                onFocus={() => setKeyboardSessionId(session.id)}
                                role="option"
                                aria-selected={active}
                              >
                                <span
                                  className={styles.sessionStatusIcon}
                                  role="img"
                                  aria-label={sessionStatus.label}
                                >
                                  <span
                                    className={styles.sessionStatusDot}
                                    data-tone={sessionStatus.tone}
                                    aria-hidden="true"
                                  />
                                </span>
                                <span className={styles.sessionTitle}>
                                  {sessionTitle}
                                </span>
                                <span className={styles.sessionMeta}>
                                  {sessionTime} · {channelLabel}
                                </span>
                                <span className={styles.sessionPreview}>
                                  {getSessionPreview(session)}
                                </span>
                              </button>
                              <Dropdown
                                trigger={["click"]}
                                placement="bottomRight"
                                rootClassName={styles.sessionActionsMenu}
                                menu={{
                                  items: [
                                    ...(canRetry
                                      ? [
                                          {
                                            key: "retry",
                                            icon: <RedoOutlined aria-hidden />,
                                            label: "重试上一轮",
                                          },
                                        ]
                                      : []),
                                    {
                                      key: "rename",
                                      icon: <SparkEditLine size={15} />,
                                      label: t(
                                        "chat.contextMenu.rename",
                                        "重命名",
                                      ),
                                    },
                                    {
                                      key: "pin",
                                      icon: session.pinned ? (
                                        <SparkMarkFill size={15} />
                                      ) : (
                                        <SparkMarkLine size={15} />
                                      ),
                                      label: session.pinned
                                        ? "取消置顶"
                                        : "置顶",
                                      disabled: session.archived,
                                    },
                                    {
                                      key: "archive",
                                      icon: <InboxOutlined aria-hidden />,
                                      label: session.archived
                                        ? "恢复会话"
                                        : "归档",
                                      disabled:
                                        !session.archived &&
                                        sessionStatus.tone === "running",
                                    },
                                    {
                                      key: "copy-link",
                                      icon: <SparkCopyLine size={15} />,
                                      label: "复制会话链接",
                                    },
                                    { type: "divider" },
                                    {
                                      key: "delete",
                                      icon: <SparkDeleteLine size={15} />,
                                      label: t(
                                        "chat.contextMenu.delete",
                                        "删除",
                                      ),
                                      danger: true,
                                    },
                                  ],
                                  onClick: ({ key, domEvent }) => {
                                    domEvent.stopPropagation();
                                    if (key === "rename") {
                                      handleRenameStart(session);
                                      return;
                                    }
                                    if (key === "pin") {
                                      void handlePinToggle(session);
                                      return;
                                    }
                                    if (key === "archive") {
                                      void handleArchiveToggle(session);
                                      return;
                                    }
                                    if (key === "copy-link") {
                                      void handleCopyLink(session);
                                      return;
                                    }
                                    if (key === "retry") {
                                      void handleRetry(session);
                                      return;
                                    }
                                    if (key === "delete") {
                                      handleDeleteRequest(session);
                                    }
                                  },
                                }}
                              >
                                <button
                                  type="button"
                                  className={styles.sessionActionButton}
                                  data-session-action="true"
                                  aria-label={t("chat.sessionActions.menu", {
                                    name: sessionTitle,
                                    defaultValue: `会话操作：${sessionTitle}`,
                                  })}
                                  aria-haspopup="menu"
                                >
                                  <MoreOutlined />
                                </button>
                              </Dropdown>
                              {canRetry ? (
                                <button
                                  type="button"
                                  className={styles.sessionQuickActionButton}
                                  aria-label={`重试：${sessionTitle}`}
                                  title="恢复上次输入并确认后重试"
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    void handleRetry(session);
                                  }}
                                >
                                  <RedoOutlined aria-hidden />
                                </button>
                              ) : null}
                            </>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>
            );
          })}
          {hasMoreSessions ? (
            <button
              type="button"
              className={styles.loadMoreButton}
              onClick={() =>
                setVisibleLimit(
                  (current) => current + VISIBLE_SESSION_PAGE_SIZE,
                )
              }
            >
              显示更多（剩余 {filteredSessions.length - visibleLimit}）
            </button>
          ) : null}
        </div>
      )}
    </div>
  );

  return (
    <Dropdown
      open={open}
      onOpenChange={handleOpenChange}
      popupRender={() => dropdownContent}
      trigger={["click"]}
      placement="bottomLeft"
    >
      <button
        type="button"
        data-testid="chat-session-switcher"
        className={[styles.trigger, open ? styles.triggerOpen : ""].join(" ")}
        aria-expanded={open}
      >
        <span className={styles.chatName}>{chatName}</span>
        <SparkDownLine
          className={[styles.triggerArrow, open ? styles.triggerArrowOpen : ""]
            .filter(Boolean)
            .join(" ")}
        />
      </button>
    </Dropdown>
  );
};

export default ChatHeaderTitle;
