import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Alert, Drawer, Input, Modal } from "antd";
import { DownOutlined, SearchOutlined } from "@ant-design/icons";
import { VariableSizeList, type ListChildComponentProps } from "react-window";
import { IconButton } from "@/vendor/lantaiDesign";
import { SparkOperateRightLine } from "@/vendor/lantaiIcons";
import {
  useChatAnywhereSessionsState,
  useChatAnywhereSessions,
  type ILantaiChatSession,
} from "@/vendor/lantaiChat";
import { useTranslation } from "react-i18next";
import type {
  ChatRuntimeState,
  ChatStatus,
  ChatTurnEventPreview,
} from "../../../../api/types/chat";
import { chatApi } from "../../../../api/modules/chat";
import sessionApi from "../../sessionApi";
import ChatSessionItem from "../ChatSessionItem";
import { getChannelLabel } from "../../../Control/Channels/components";
import {
  ContextMenu,
  useContextMenu,
  type ContextMenuItem,
} from "../../../../components/ContextMenu";
import { useAgentStore } from "../../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../../utils/latestRequest";
import {
  buildSessionRows,
  formatSessionTime,
  getSessionPreview,
  getSessionStatus,
  getSessionTitle,
  type SessionListRow,
} from "./presentation";
import styles from "./index.module.less";

const ITEM_HEIGHT = 90;
const GROUP_HEIGHT = 40;
const COLLAPSED_GROUPS_KEY = "lantai:chat-session-collapsed-groups";

function loadCollapsedGroupKeys(): Set<string> {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_GROUPS_KEY);
    if (raw === null) return new Set();
    const stored = JSON.parse(raw);
    if (Array.isArray(stored)) {
      return new Set(stored.filter((value) => typeof value === "string"));
    }
  } catch {
    // Ignore restricted or malformed local storage and use the calm default.
  }
  return new Set();
}

interface SessionRowData {
  rows: SessionListRow<ExtendedChatSession>[];
  currentSessionId: string | undefined;
  editingSessionId: string | null;
  editValue: string;
  t: ReturnType<typeof useTranslation>["t"];
  handleSessionClick: (sessionId: string) => void;
  handleGroupToggle: (groupKey: string) => void;
  handleEditStart: (sessionId: string, currentName: string) => void;
  handleDelete: (sessionId: string) => void;
  handlePinToggle: (sessionId: string) => void;
  handleEditChange: (value: string) => void;
  handleEditSubmit: () => void;
  handleEditCancel: () => void;
  handleItemContextMenu: (sessionId: string, event: React.MouseEvent) => void;
}

/** Memoized row renderer — only re-renders when its specific props change */
const SessionRow = React.memo(function SessionRow({
  index,
  style,
  data,
}: ListChildComponentProps<SessionRowData>) {
  const row = data.rows[index];
  if (row.kind === "group") {
    return (
      <div style={style}>
        <button
          type="button"
          className={styles.groupRow}
          onClick={() => data.handleGroupToggle(row.key.replace("group:", ""))}
          aria-expanded={!row.collapsed}
        >
          <span className={styles.groupRowLabel}>
            <DownOutlined
              className={styles.groupChevron}
              data-collapsed={row.collapsed}
              aria-hidden="true"
            />
            {row.label}
          </span>
          <em>{row.count}</em>
        </button>
      </div>
    );
  }
  const session = row.session;
  const channelKey = session.channel?.trim() || "";
  const showChannel = Boolean(channelKey && channelKey !== "console");
  const channelLabel = showChannel
    ? getChannelLabel(channelKey, data.t)
    : undefined;
  const isEditing = data.editingSessionId === session.id;
  const status = getSessionStatus(session);

  return (
    <div style={style}>
      <ChatSessionItem
        sessionId={session.id!}
        name={getSessionTitle(session)}
        time={formatSessionTime(session)}
        preview={getSessionPreview(session)}
        statusLabel={status.label}
        statusTone={status.tone}
        channelKey={showChannel ? channelKey : undefined}
        channelLabel={channelLabel}
        chatStatus={session.status}
        generating={session.generating}
        pinned={session.pinned}
        active={session.id === data.currentSessionId}
        editing={isEditing}
        editValue={isEditing ? data.editValue : undefined}
        onClick={data.handleSessionClick}
        onEdit={data.handleEditStart}
        onDelete={data.handleDelete}
        onPin={data.handlePinToggle}
        onEditChange={data.handleEditChange}
        onEditSubmit={data.handleEditSubmit}
        onEditCancel={data.handleEditCancel}
        onContextMenu={data.handleItemContextMenu}
      />
    </div>
  );
});

/** Sessions from lantai backend include extra fields beyond the runtime UI type */
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
  runtimeState?: ChatRuntimeState | null;
  turnEventPreview?: ChatTurnEventPreview | null;
}

interface ChatSessionDrawerProps {
  /** Whether the drawer is visible */
  open: boolean;
  /** Callback to close the drawer */
  onClose: () => void;
}

/** Resolve the real backend UUID from an extended session (id may be a local timestamp) */
const getBackendId = (session: ExtendedChatSession): string | null => {
  if (session.realId) return session.realId;
  const id = session.id;
  if (!/^\d+$/.test(id)) return id;
  return null;
};

const ChatSessionDrawer: React.FC<ChatSessionDrawerProps> = (props) => {
  const { t } = useTranslation();
  const { selectedAgent } = useAgentStore();
  const { sessions, currentSessionId, setCurrentSessionId, setSessions } =
    useChatAnywhereSessionsState();

  const { createSession } = useChatAnywhereSessions();

  /** Create a new session and close the drawer */
  const handleCreateSession = useCallback(async () => {
    await createSession();
    props.onClose();
  }, [createSession, props.onClose]);

  /** ID of the session currently being renamed */
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  /** Current value of the rename input */
  const [editValue, setEditValue] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [collapsedGroupKeys, setCollapsedGroupKeys] = useState(
    loadCollapsedGroupKeys,
  );

  /** Whether the session list is being fetched (default true because destroyOnClose re-mounts) */
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");

  /** Height of the virtual list container, measured via ResizeObserver */
  const [listHeight, setListHeight] = useState(0);
  const observerRef = useRef<ResizeObserver | null>(null);
  const listLoadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());
  const deletingSessionIdsRef = useRef(new Set<string>());
  const previousAgentRef = useRef(selectedAgent);

  useEffect(() => {
    if (previousAgentRef.current !== selectedAgent) {
      listLoadGateRef.current.invalidate();
      setEditingSessionId(null);
      setEditValue("");
      setSearchQuery("");
      setContextMenuSessionId(null);
      props.onClose();
    }
    previousAgentRef.current = selectedAgent;
    return () => {
      listLoadGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
  }, [props.onClose, selectedAgent]);

  /** Callback ref: attach a ResizeObserver whenever the wrapper DOM node appears */
  const listWrapperRef = useCallback((node: HTMLDivElement | null) => {
    // Cleanup previous observer
    if (observerRef.current) {
      observerRef.current.disconnect();
      observerRef.current = null;
    }

    if (!node) return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const height = entry.contentRect.height;
        if (height > 0) {
          setListHeight(height);
        }
      }
    });

    observer.observe(node);
    observerRef.current = observer;

    // Measure immediately in case layout is already stable
    const initialHeight = node.clientHeight;
    if (initialHeight > 0) {
      setListHeight(initialHeight);
    }
  }, []);

  /** Shared context menu — only one instance instead of one per item */
  const sharedContextMenu = useContextMenu();
  const [contextMenuSessionId, setContextMenuSessionId] = useState<
    string | null
  >(null);

  const filteredSessions = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase();
    return (sessions as ExtendedChatSession[]).filter((session) => {
      if (!query) return true;
      const status = getSessionStatus(session);
      const searchable = [
        session.name,
        session.channel,
        getChannelLabel(session.channel?.trim() || "", t),
        getSessionPreview(session),
        status.label,
      ]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase();
      return searchable.includes(query);
    });
  }, [searchQuery, sessions, t]);
  const sessionRows = useMemo(
    () =>
      buildSessionRows(
        filteredSessions,
        new Date(),
        searchQuery.trim() ? new Set() : collapsedGroupKeys,
      ),
    [collapsedGroupKeys, filteredSessions, searchQuery],
  );

  useEffect(() => {
    try {
      window.localStorage.setItem(
        COLLAPSED_GROUPS_KEY,
        JSON.stringify([...collapsedGroupKeys]),
      );
    } catch {
      // Ignore storage failures in restricted browser contexts.
    }
  }, [collapsedGroupKeys]);

  const handleGroupToggle = useCallback((groupKey: string) => {
    setCollapsedGroupKeys((current) => {
      const next = new Set(current);
      if (next.has(groupKey)) next.delete(groupKey);
      else next.add(groupKey);
      return next;
    });
  }, []);

  /** Re-fetch session list from the backend and sync to context state */
  const refreshSessions = useCallback(async () => {
    const ownsState = listLoadGateRef.current.begin();
    const list = await sessionApi.getSessionList();
    if (ownsState()) setSessions(list);
  }, [setSessions]);

  /** Open drawer → refresh session list */
  useEffect(() => {
    if (!props.open) return;

    let isCancelled = false;

    const fetchSessions = async () => {
      const ownsState = listLoadGateRef.current.begin();
      setListLoading(true);
      try {
        const list = await sessionApi.getSessionList();
        if (!isCancelled && ownsState()) {
          setSessions(list);
          setListError("");
        }
      } catch (error) {
        console.error("Failed to refresh session list:", error);
        if (!isCancelled && ownsState()) {
          setListError("会话列表加载失败，请重试。已有会话不会被删除。");
        }
      } finally {
        if (!isCancelled && ownsState()) {
          setListLoading(false);
        }
      }
    };

    void fetchSessions();

    return () => {
      isCancelled = true;
      listLoadGateRef.current.invalidate();
    };
  }, [props.open, selectedAgent, setSessions]);

  const handleSessionClick = useCallback(
    (sessionId: string) => {
      sessionApi.selectSession(sessionId);
      setCurrentSessionId(sessionId);
      props.onClose();
    },
    [props.onClose, setCurrentSessionId],
  );

  /** Delete a session: call deleteChat API then refresh the list */
  const handleDelete = useCallback(
    async (sessionId: string) => {
      if (deletingSessionIdsRef.current.has(sessionId)) return;
      const session = sessions.find((s) => s.id === sessionId) as
        | ExtendedChatSession
        | undefined;
      const confirmed = await new Promise<boolean>((resolve) => {
        Modal.confirm({
          transitionName: "",
          maskTransitionName: "",
          title: `永久删除“${getSessionTitle(session || ({ id: sessionId } as ExtendedChatSession))}”？`,
          content:
            "删除后，该会话的消息记录无法在澜台中恢复。相关任务或已生成文件不会自动删除。",
          okText: "永久删除",
          cancelText: "取消",
          okButtonProps: { danger: true },
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
      if (!confirmed) return;
      deletingSessionIdsRef.current.add(sessionId);
      const ownsState = mutationScopeRef.current.capture();
      try {
        const backendId = session ? getBackendId(session) : null;

        if (backendId) {
          await chatApi.deleteChat(backendId);
          if (!ownsState()) return;
        }

        if (currentSessionId === sessionId) {
          const next = sessions.filter((s) => s.id !== sessionId);
          setCurrentSessionId(next[0]?.id);
        }

        await refreshSessions();
        setListError("");
      } catch (error) {
        if (ownsState()) {
          console.error("Failed to delete chat:", error);
          setListError("删除会话失败，记录仍然保留。请稍后重试。");
        }
      } finally {
        deletingSessionIdsRef.current.delete(sessionId);
      }
    },
    [sessions, currentSessionId, setCurrentSessionId, refreshSessions],
  );

  /** Enter rename mode for a session */
  const handleEditStart = useCallback(
    (sessionId: string, currentName: string) => {
      setEditingSessionId(sessionId);
      setEditValue(currentName);
    },
    [],
  );

  /** Update rename input value */
  const handleEditChange = useCallback((value: string) => {
    setEditValue(value);
  }, []);

  /** Submit rename */
  const handleEditSubmit = useCallback(async () => {
    if (!editingSessionId) return;

    const session = sessions.find((s) => s.id === editingSessionId) as
      | ExtendedChatSession
      | undefined;
    const backendId = session ? getBackendId(session) : null;
    const newName = editValue.trim();
    const ownsState = mutationScopeRef.current.capture();

    try {
      if (backendId && newName && session) {
        await chatApi.updateChat(backendId, {
          name: newName,
        });
        if (!ownsState()) return;
      }

      setEditingSessionId(null);
      setEditValue("");
      await refreshSessions();
      if (ownsState()) setListError("");
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to rename chat:", error);
      setListError("重命名失败，原名称仍然保留。请稍后重试。");
    }
  }, [editingSessionId, editValue, sessions, refreshSessions]);

  /** Cancel rename mode */
  const handleEditCancel = useCallback(() => {
    setEditingSessionId(null);
    setEditValue("");
  }, []);

  /** Toggle pin status for a session */
  const handlePinToggle = useCallback(
    async (sessionId: string) => {
      const ownsState = mutationScopeRef.current.capture();
      const session = sessions.find((s) => s.id === sessionId) as
        | ExtendedChatSession
        | undefined;
      const backendId = session ? getBackendId(session) : null;

      if (backendId && session) {
        try {
          const newPinnedState = !session.pinned;
          await chatApi.updateChat(backendId, {
            pinned: newPinnedState,
          });
          if (!ownsState()) return;
          await refreshSessions();
          if (ownsState()) setListError("");
        } catch (error) {
          if (!ownsState()) return;
          console.error("Failed to toggle pin status:", error);
          setListError("置顶状态更新失败，会话顺序未改变。请稍后重试。");
        }
      }
    },
    [sessions, refreshSessions],
  );

  /** Show shared context menu for a specific session */
  const handleItemContextMenu = useCallback(
    (sessionId: string, event: React.MouseEvent) => {
      setContextMenuSessionId(sessionId);
      sharedContextMenu.show(event);
    },
    [sharedContextMenu],
  );

  /** Build context menu items for the currently right-clicked session */
  const contextMenuItems: ContextMenuItem[] = useMemo(() => {
    if (!contextMenuSessionId) return [];
    const session = sessions.find((s) => s.id === contextMenuSessionId) as
      | ExtendedChatSession
      | undefined;
    return [
      {
        key: "open",
        label: t("chat.contextMenu.open", "Open"),
        onClick: () => handleSessionClick(contextMenuSessionId),
      },
      {
        key: "rename",
        label: t("chat.contextMenu.rename", "Rename"),
        onClick: () =>
          handleEditStart(contextMenuSessionId, session?.name || "New Chat"),
      },
      {
        key: "pin",
        label: session?.pinned
          ? t("chat.contextMenu.unpin", "Unpin")
          : t("chat.contextMenu.pin", "Pin"),
        onClick: () => handlePinToggle(contextMenuSessionId),
      },
      { key: "divider-1", label: "", divider: true },
      {
        key: "delete",
        label: t("chat.contextMenu.delete", "Delete"),
        danger: true,
        onClick: () => handleDelete(contextMenuSessionId),
      },
    ];
  }, [
    contextMenuSessionId,
    sessions,
    t,
    handleSessionClick,
    handleEditStart,
    handlePinToggle,
    handleDelete,
  ]);

  const itemData = useMemo<SessionRowData>(
    () => ({
      rows: sessionRows,
      currentSessionId,
      editingSessionId,
      editValue,
      t,
      handleSessionClick,
      handleGroupToggle,
      handleEditStart,
      handleDelete,
      handlePinToggle,
      handleEditChange,
      handleEditSubmit,
      handleEditCancel,
      handleItemContextMenu,
    }),
    [
      sessionRows,
      currentSessionId,
      editingSessionId,
      editValue,
      t,
      handleSessionClick,
      handleGroupToggle,
      handleEditStart,
      handleDelete,
      handlePinToggle,
      handleEditChange,
      handleEditSubmit,
      handleEditCancel,
      handleItemContextMenu,
    ],
  );

  return (
    <Drawer
      open={props.open}
      onClose={props.onClose}
      destroyOnHidden
      placement="right"
      width={380}
      closable={false}
      title={null}
      styles={{
        header: { display: "none" },
        body: {
          padding: 0,
          display: "flex",
          flexDirection: "column",
          height: "100%",
          overflow: "hidden",
        },
        mask: { background: "transparent" },
      }}
      className={styles.drawer}
    >
      {/* Header bar */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.headerTitle}>{t("chat.allChats")}</span>
          <span className={styles.sessionCount}>{sessions.length}</span>
          {listLoading && sessionRows.length > 0 ? (
            <span className={styles.syncingLabel} role="status">
              {t("chat.syncingSessions", "同步中")}
            </span>
          ) : null}
        </div>
        <div className={styles.headerRight}>
          <IconButton
            bordered={false}
            icon={<SparkOperateRightLine />}
            onClick={props.onClose}
          />
        </div>
      </div>

      {/* Create new chat button */}
      <div className={styles.createSection}>
        <Input
          allowClear
          value={searchQuery}
          onChange={(event) => setSearchQuery(event.target.value)}
          prefix={<SearchOutlined />}
          placeholder={t("chat.searchSessions", "搜索会话")}
          className={styles.searchInput}
          aria-label={t("chat.searchSessions", "搜索会话")}
        />
        <button
          type="button"
          className={styles.createButton}
          onClick={handleCreateSession}
        >
          {t("chat.createNewChat")}
        </button>
        {listError ? (
          <Alert
            type="error"
            showIcon
            message={listError}
            action={
              <button type="button" onClick={() => void refreshSessions()}>
                重试
              </button>
            }
          />
        ) : null}
      </div>

      {/* Session list */}
      <div className={styles.listWrapper} ref={listWrapperRef}>
        <div className={styles.topGradient} />
        {listLoading && sessionRows.length === 0 ? (
          <div
            className={styles.loadingSkeleton}
            role="status"
            aria-label={t("chat.loadingSessions", "正在读取会话")}
          >
            {[0, 1, 2, 3, 4].map((row) => (
              <div key={row} className={styles.skeletonRow}>
                <span className={styles.skeletonStatus} />
                <span className={styles.skeletonCopy}>
                  <span className={styles.skeletonTitle} />
                  <span className={styles.skeletonPreview} />
                </span>
              </div>
            ))}
          </div>
        ) : (
          <>
            <VariableSizeList
              height={listHeight}
              width="100%"
              itemCount={sessionRows.length}
              itemSize={(index) =>
                sessionRows[index]?.kind === "group"
                  ? GROUP_HEIGHT
                  : ITEM_HEIGHT
              }
              itemKey={(index) => sessionRows[index]?.key || index}
              overscanCount={20}
              itemData={itemData}
              className={styles.list}
            >
              {SessionRow}
            </VariableSizeList>
            {sessionRows.length === 0 ? (
              <div className={styles.emptyState}>
                {searchQuery.trim()
                  ? t("chat.noSessionSearchResults", "没有匹配的会话")
                  : t("chat.noSessions", "暂无会话")}
              </div>
            ) : null}
          </>
        )}
        <div className={styles.bottomGradient} />
      </div>

      {/* Shared context menu — single instance for all session items */}
      <ContextMenu
        visible={sharedContextMenu.visible}
        x={sharedContextMenu.x}
        y={sharedContextMenu.y}
        items={contextMenuItems}
        onClose={sharedContextMenu.hide}
      />
    </Drawer>
  );
};

export default ChatSessionDrawer;
