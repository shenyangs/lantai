import { Dropdown, Layout, Tooltip, type MenuProps } from "antd";
import {
  DownOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  LoadingOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { agentsApi } from "../api/modules/agents";
import { chatApi } from "../api/modules/chat";
import {
  workspaceGrantsApi,
  type WorkspaceGrantResponse,
} from "../api/modules/workspaceGrants";
import { useAppMessage } from "../hooks/useAppMessage";
import { useAgentStore } from "../stores/agentStore";
import ModelSelector from "../pages/Chat/ModelSelector";
import {
  CREATE_NEW_CHAT_PENDING_KEY,
  CREATE_NEW_CHAT_REQUEST_EVENT,
  CREATE_NEW_CHAT_SETTLED_EVENT,
  type CreateNewChatSettledDetail,
} from "../pages/Chat/chatSessionEvents";
import ThemeToggleButton from "../components/ThemeToggleButton";
import {
  getActiveTopicWorkspace,
  setActiveTopicWorkspace,
  TOPIC_WORKSPACE_CHANGED_EVENT,
  type TopicWorkspaceState,
} from "../utils/topicWorkspace";
import styles from "./index.module.less";
import { createLatestRequestGate } from "../utils/latestRequest";

const { Header: AntHeader } = Layout;
const MAX_RECENT_WORKSPACES = 5;
type HeaderMenu = "model" | "workspace" | "theme" | null;

function compactPath(path: string | undefined) {
  if (!path) return "选择工作文件夹";
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= 3) return path;
  return `.../${parts.slice(-3).join("/")}`;
}

export default function Header() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { selectedAgent, agents, setAgents } = useAgentStore();
  const agentList = Array.isArray(agents) ? agents : [];
  const [recentWorkspaces, setRecentWorkspaces] = useState<
    WorkspaceGrantResponse[]
  >([]);
  const [activeHeaderMenu, setActiveHeaderMenu] = useState<HeaderMenu>(null);
  const workspaceMenuOpen = activeHeaderMenu === "workspace";
  const setHeaderMenuOpen = useCallback(
    (menu: Exclude<HeaderMenu, null>, open: boolean) => {
      setActiveHeaderMenu((current) => {
        if (open) return menu;
        return current === menu ? null : current;
      });
    },
    [],
  );
  const setWorkspaceMenuOpen = useCallback(
    (open: boolean) => setHeaderMenuOpen("workspace", open),
    [setHeaderMenuOpen],
  );
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [newChatPending, setNewChatPending] = useState(false);
  const workspaceGrantsGateRef = useRef(createLatestRequestGate());
  const workspaceMutationGateRef = useRef(createLatestRequestGate());
  const newChatPendingRef = useRef(false);
  const newChatFallbackTimerRef = useRef<number | null>(null);
  const [activeTopicWorkspace, setActiveTopicWorkspaceState] =
    useState<TopicWorkspaceState | null>(() =>
      typeof window === "undefined" ? null : getActiveTopicWorkspace(),
    );

  const selectedAgentInfo = useMemo(
    () => agentList.find((agent) => agent.id === selectedAgent),
    [agentList, selectedAgent],
  );
  useEffect(() => {
    const refreshActiveTopicWorkspace = () => {
      setActiveTopicWorkspaceState(getActiveTopicWorkspace());
    };
    window.addEventListener(
      TOPIC_WORKSPACE_CHANGED_EVENT,
      refreshActiveTopicWorkspace,
    );
    window.addEventListener("storage", refreshActiveTopicWorkspace);
    return () => {
      window.removeEventListener(
        TOPIC_WORKSPACE_CHANGED_EVENT,
        refreshActiveTopicWorkspace,
      );
      window.removeEventListener("storage", refreshActiveTopicWorkspace);
    };
  }, []);

  const selectedAgentWorkspace = selectedAgentInfo?.workspace_dir || "";
  const topicWorkspace =
    activeTopicWorkspace?.agentId === selectedAgent
      ? activeTopicWorkspace
      : null;
  const currentWorkspace =
    topicWorkspace?.effectiveWorkspaceDir || selectedAgentWorkspace;
  const workspaceLabel = compactPath(currentWorkspace);
  const newChatLabel = t("chat.newChatTooltip");

  const refreshAgents = useCallback(async () => {
    const result = await agentsApi.listAgents();
    const nextAgents = Array.isArray(result.agents) ? result.agents : [];
    setAgents(nextAgents);
    return nextAgents;
  }, [setAgents]);

  useEffect(() => {
    if (agentList.length > 0) return;
    void refreshAgents().catch(() => {
      // The rest of the app can still render if the backend is booting.
    });
  }, [agentList.length, refreshAgents]);

  const refreshWorkspaceGrants = useCallback(async () => {
    const ownsState = workspaceGrantsGateRef.current.begin();
    const grants = await workspaceGrantsApi.list();
    if (
      !ownsState() ||
      useAgentStore.getState().selectedAgent !== selectedAgent
    ) {
      return [];
    }
    setRecentWorkspaces(grants.slice(0, MAX_RECENT_WORKSPACES));
    return grants;
  }, [selectedAgent]);

  useEffect(() => {
    void refreshWorkspaceGrants().catch(() => {
      if (useAgentStore.getState().selectedAgent === selectedAgent) {
        setRecentWorkspaces([]);
      }
    });
    return () => workspaceGrantsGateRef.current.invalidate();
  }, [refreshWorkspaceGrants, selectedAgent]);

  useEffect(
    () => () => {
      workspaceMutationGateRef.current.invalidate();
      setWorkspaceBusy(false);
    },
    [selectedAgent],
  );

  useEffect(() => {
    const settleNewChat = (event: Event) => {
      const detail = (event as CustomEvent<CreateNewChatSettledDetail>).detail;
      newChatPendingRef.current = false;
      setNewChatPending(false);
      if (newChatFallbackTimerRef.current !== null) {
        window.clearTimeout(newChatFallbackTimerRef.current);
        newChatFallbackTimerRef.current = null;
      }
      if (detail?.ok === false) {
        message.error("新会话创建失败，请重试");
      }
    };
    window.addEventListener(CREATE_NEW_CHAT_SETTLED_EVENT, settleNewChat);
    return () => {
      window.removeEventListener(CREATE_NEW_CHAT_SETTLED_EVENT, settleNewChat);
      if (newChatFallbackTimerRef.current !== null) {
        window.clearTimeout(newChatFallbackTimerRef.current);
      }
    };
  }, [message]);

  const createNewChat = () => {
    if (newChatPendingRef.current) return;
    newChatPendingRef.current = true;
    setNewChatPending(true);
    window.sessionStorage.setItem(CREATE_NEW_CHAT_PENDING_KEY, "1");
    navigate("/chat");
    window.setTimeout(() => {
      window.dispatchEvent(new Event(CREATE_NEW_CHAT_REQUEST_EVENT));
    }, 80);
    newChatFallbackTimerRef.current = window.setTimeout(() => {
      newChatPendingRef.current = false;
      setNewChatPending(false);
      newChatFallbackTimerRef.current = null;
    }, 60_000);
  };

  const switchWorkspace = useCallback(
    async (workspace: string, workspaceGrantId: string) => {
      const normalized = workspace.trim();
      if (
        !normalized ||
        (normalized === currentWorkspace &&
          workspaceGrantId === topicWorkspace?.workspaceGrantId) ||
        workspaceBusy
      ) {
        return;
      }
      const ownsState = workspaceMutationGateRef.current.begin();
      const mutationAgentId = selectedAgent;
      setWorkspaceBusy(true);
      try {
        const sessionId =
          topicWorkspace?.sessionId ||
          `console:workspace:${Date.now().toString(36)}`;
        const result = await chatApi.ensureTopicWorkspace({
          session_id: sessionId,
          user_id: (window as any).currentUserId || "default",
          channel: (window as any).currentChannel || "console",
          name: "新会话",
          workspace_grant_id: workspaceGrantId,
        });
        if (
          !ownsState() ||
          useAgentStore.getState().selectedAgent !== mutationAgentId
        ) {
          return;
        }
        const nextState: TopicWorkspaceState = {
          agentId: selectedAgent,
          sessionId: result.chat.session_id,
          chatId: result.chat.id,
          topicWorkspaceDir: result.topic_workspace_dir,
          manualWorkspaceDir: null,
          effectiveWorkspaceDir:
            result.project_workspace_dir || result.effective_workspace_dir,
          projectWorkspaceDir: result.project_workspace_dir || normalized,
          workspaceGrantId: result.workspace_grant_id || workspaceGrantId,
          workspaceAccessMode: result.workspace_access_mode || null,
          workspaceTrustState: result.workspace_trust_state || null,
        };
        setActiveTopicWorkspace(nextState);
        setActiveTopicWorkspaceState(nextState);
        await refreshWorkspaceGrants();
        window.dispatchEvent(new Event("lantai:workspace-changed"));
        message.success("已切换工作区");
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to switch workspace:", error);
        message.error(
          error instanceof Error ? error.message : "切换工作区失败",
        );
      } finally {
        if (ownsState()) {
          setWorkspaceBusy(false);
          setWorkspaceMenuOpen(false);
        }
      }
    },
    [
      currentWorkspace,
      message,
      refreshWorkspaceGrants,
      selectedAgent,
      topicWorkspace,
      workspaceBusy,
    ],
  );

  const returnToTopicWorkspace = useCallback(async () => {
    if (!topicWorkspace?.sessionId || !topicWorkspace.topicWorkspaceDir) {
      setWorkspaceMenuOpen(false);
      return;
    }
    const ownsState = workspaceMutationGateRef.current.begin();
    const mutationAgentId = selectedAgent;
    setWorkspaceBusy(true);
    try {
      const result = await chatApi.ensureTopicWorkspace({
        session_id: topicWorkspace.sessionId,
        user_id: (window as any).currentUserId || "default",
        channel: (window as any).currentChannel || "console",
        reset_manual_workspace: true,
      });
      if (
        !ownsState() ||
        useAgentStore.getState().selectedAgent !== mutationAgentId
      ) {
        return;
      }
      const nextState: TopicWorkspaceState = {
        agentId: selectedAgent,
        sessionId: topicWorkspace.sessionId,
        chatId: result.chat.id,
        topicWorkspaceDir: result.topic_workspace_dir,
        manualWorkspaceDir: null,
        effectiveWorkspaceDir: result.effective_workspace_dir,
        projectWorkspaceDir: null,
        workspaceGrantId: null,
      };
      setActiveTopicWorkspace(nextState);
      setActiveTopicWorkspaceState(nextState);
      window.dispatchEvent(new Event("lantai:workspace-changed"));
      message.success("已回到话题工作区");
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to return to topic workspace:", error);
      message.error(
        error instanceof Error ? error.message : "回到话题工作区失败",
      );
    } finally {
      if (ownsState()) {
        setWorkspaceBusy(false);
        setWorkspaceMenuOpen(false);
      }
    }
  }, [message, selectedAgent, topicWorkspace]);

  const openCurrentWorkspace = useCallback(async () => {
    if (!currentWorkspace) {
      message.warning("当前智能体还没有工作区");
      return;
    }
    const result = await window.electronAPI?.openPath?.(currentWorkspace);
    if (result && !result.success) {
      message.error(result.error || "无法打开工作区");
    }
    setWorkspaceMenuOpen(false);
  }, [currentWorkspace, message]);

  const chooseWorkspace = useCallback(async () => {
    const selectWorkspaceGrant = window.electronAPI?.selectWorkspaceGrant;
    if (!selectWorkspaceGrant) {
      message.error("请在澜台桌面版中选择并授权工作区");
      return;
    }
    const selection = await selectWorkspaceGrant({
      agentId: selectedAgent,
      accessMode: "read_write",
      trustState: "trusted",
    });
    if (!selection) return;
    if (!selection.ok || !selection.selectionReceipt) {
      message.error(selection.error || "工作区授权失败");
      return;
    }
    const grant = await workspaceGrantsApi.claim(selection.selectionReceipt);
    await switchWorkspace(grant.project_root, grant.id);
  }, [message, selectedAgent, switchWorkspace]);

  const workspaceMenuItems = useMemo<MenuProps["items"]>(() => {
    const items: MenuProps["items"] = [
      {
        key: "open",
        icon: <FolderOpenOutlined />,
        label: "从 Finder 打开",
        disabled: !currentWorkspace,
        onClick: openCurrentWorkspace,
      },
      {
        key: "choose",
        icon: <FolderAddOutlined />,
        label: "切换到其他文件夹...",
        onClick: chooseWorkspace,
      },
      {
        key: "workspace-page",
        icon: <FolderOutlined />,
        label: "回到话题工作区",
        disabled:
          !topicWorkspace?.topicWorkspaceDir ||
          currentWorkspace === topicWorkspace.topicWorkspaceDir,
        onClick: () => void returnToTopicWorkspace(),
      },
    ];

    const recent = recentWorkspaces.filter(
      (grant) => grant.project_root !== currentWorkspace,
    );
    if (recent.length > 0) {
      items.push({ type: "divider" });
      items.push({
        key: "recent-title",
        label: <span className={styles.workspaceMenuTitle}>最近文件夹</span>,
        disabled: true,
      });
      recent.forEach((grant) => {
        items.push({
          key: `recent:${grant.id}`,
          icon: <FolderOutlined />,
          label: grant.display_name || compactPath(grant.project_root),
          title: grant.project_root,
          onClick: () => void switchWorkspace(grant.project_root, grant.id),
        });
      });
    }

    return items;
  }, [
    chooseWorkspace,
    currentWorkspace,
    openCurrentWorkspace,
    recentWorkspaces,
    returnToTopicWorkspace,
    switchWorkspace,
    topicWorkspace,
  ]);

  return (
    <AntHeader
      className={styles.header}
      style={{
        paddingLeft:
          navigator.platform?.includes("Mac") ||
          navigator.userAgent?.includes("Mac")
            ? 78
            : undefined,
      }}
    >
      <div className={styles.headerRight}>
        <div className={styles.headerModelSelector}>
          <ModelSelector
            open={activeHeaderMenu === "model"}
            onOpenChange={(open) => setHeaderMenuOpen("model", open)}
          />
        </div>
        <Dropdown
          menu={{ items: workspaceMenuItems }}
          open={workspaceMenuOpen}
          onOpenChange={setWorkspaceMenuOpen}
          placement="bottomRight"
          trigger={["click"]}
          rootClassName={styles.workspaceDropdown}
        >
          <button
            type="button"
            className={styles.headerWorkspaceButton}
            disabled={workspaceBusy}
            title={currentWorkspace || undefined}
            aria-haspopup="menu"
            aria-expanded={workspaceMenuOpen}
          >
            <FolderOpenOutlined />
            <span className={styles.headerWorkspaceLabel}>
              {workspaceLabel}
            </span>
            <DownOutlined className={styles.headerWorkspaceArrow} />
          </button>
        </Dropdown>
        <div className={styles.headerDivider} />
        <ThemeToggleButton
          open={activeHeaderMenu === "theme"}
          onOpenChange={(open) => setHeaderMenuOpen("theme", open)}
        />
        <Tooltip title={newChatLabel}>
          <button
            type="button"
            className={styles.headerIconButton}
            onClick={createNewChat}
            disabled={newChatPending}
            aria-busy={newChatPending}
            aria-label={newChatLabel}
          >
            {newChatPending ? <LoadingOutlined spin /> : <PlusOutlined />}
          </button>
        </Tooltip>
      </div>
    </AntHeader>
  );
}
