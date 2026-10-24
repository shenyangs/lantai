import { Layout, Tooltip } from "antd";
import {
  ApiOutlined,
  CalendarOutlined,
  DownOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SearchOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import {
  Bell,
  BookOpenText,
  Bot,
  Brain,
  Boxes,
  GalleryHorizontalEnd,
  Newspaper,
} from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import {
  SparkChatTabFill,
  SparkMagicWandLine,
  SparkToolLine,
} from "@/vendor/lantaiIcons";
import { usePlugins } from "../plugins/PluginContext";
import ChatSearchPanel from "../pages/Chat/components/ChatSearchPanel";
import { channelApi } from "../api/modules/channel";
import { chatApi } from "../api/modules/chat";
import { reminderApi } from "../api/modules/reminder";
import type { ChannelConfig, ChatSpec } from "../api/types";
import { useAgentStore } from "../stores/agentStore";
import { getChannelLabel } from "../pages/Control/Channels/components/constants";
import {
  getChannelLetter,
  getChannelLetterColor,
} from "../pages/Control/Channels/components/channelIcons";
import { KEY_TO_PATH } from "./constants";
import {
  applyCustomizationSidebarValues,
  isSidebarNavVisible,
  loadSidebarVisibleKeys,
  SIDEBAR_NAV_PREFERENCES_EVENT,
} from "./sidebarPreferences";
import styles from "./index.module.less";
import { createLatestRequestGate } from "../utils/latestRequest";
import {
  CUSTOMIZATION_CHANGED_EVENT,
  customizationApi,
  type CustomizationEffectiveState,
} from "../api/modules/customization";

const { Sider } = Layout;
const SIDEBAR_COLLAPSED_KEY = "lantai:sidebar-collapsed";
const CHANNELS_EXPANDED_KEY = "lantai:sidebar-channels-expanded";

interface SidebarProps {
  selectedKey: string;
}

interface NavItem {
  key: string;
  label: string;
  icon: ReactNode;
  path?: string;
}

interface NavGroup {
  title?: string;
  items: NavItem[];
}

interface ConnectionItem {
  key: string;
  label: string;
  enabled: boolean;
  chatCount: number;
  latestChat?: ChatSpec;
  latestAt?: string | null;
}

const CHANNELS_HIDDEN_FROM_CONNECTIONS = new Set(["console"]);
const CONNECTION_ITEM_LIMIT = 5;

function chatTimeValue(chat: ChatSpec) {
  const value = chat.updated_at || chat.created_at || "";
  const time = value ? new Date(value).getTime() : 0;
  return Number.isFinite(time) ? time : 0;
}

function formatConnectionTime(value?: string | null) {
  if (!value) return "暂无记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "暂无记录";

  const now = new Date();
  const dateStart = new Date(
    date.getFullYear(),
    date.getMonth(),
    date.getDate(),
  ).getTime();
  const nowStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const dayDiff = Math.round((nowStart - dateStart) / 86400000);
  const time = date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  if (dayDiff === 0) return `今天 ${time}`;
  if (dayDiff === 1) return `昨天 ${time}`;
  if (dayDiff > 1 && dayDiff < 7) return `${dayDiff}天前 ${time}`;
  return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}

function isChannelEnabled(config: unknown) {
  return (
    !!config &&
    typeof config === "object" &&
    (config as { enabled?: unknown }).enabled === true
  );
}

function buildConnectionItems(
  channels: ChannelConfig | null,
  chats: ChatSpec[],
  translateLabel: (key: string) => string,
) {
  const byChannel = new Map<string, ChatSpec[]>();
  for (const chat of chats) {
    const channel = String(chat.channel || "").trim();
    if (!channel || CHANNELS_HIDDEN_FROM_CONNECTIONS.has(channel)) continue;
    const list = byChannel.get(channel) || [];
    list.push(chat);
    byChannel.set(channel, list);
  }

  const channelKeys = new Set<string>();
  if (channels) {
    for (const [key, config] of Object.entries(channels)) {
      if (
        !CHANNELS_HIDDEN_FROM_CONNECTIONS.has(key) &&
        isChannelEnabled(config)
      ) {
        channelKeys.add(key);
      }
    }
  }
  for (const key of byChannel.keys()) channelKeys.add(key);

  const channelConfigByKey = (channels || {}) as unknown as Record<
    string,
    unknown
  >;
  return Array.from(channelKeys)
    .map<ConnectionItem>((key) => {
      const channelChats = [...(byChannel.get(key) || [])].sort(
        (a, b) => chatTimeValue(b) - chatTimeValue(a),
      );
      const latestChat = channelChats[0];
      return {
        key,
        label: translateLabel(key),
        enabled: channels ? isChannelEnabled(channelConfigByKey[key]) : false,
        chatCount: channelChats.length,
        latestChat,
        latestAt: latestChat?.updated_at || latestChat?.created_at || null,
      };
    })
    .sort((a, b) => {
      if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
      return (
        (b.latestAt ? new Date(b.latestAt).getTime() : 0) -
        (a.latestAt ? new Date(a.latestAt).getTime() : 0)
      );
    });
}

export default function Sidebar({ selectedKey }: SidebarProps) {
  const navigate = useNavigate();
  const navigationRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const nav = navigationRef.current;
    if (!nav) return;
    const revealSelection = () => {
      const active = nav.querySelector<HTMLElement>('[aria-current="page"]');
      if (!active) return;
      const viewport = nav.getBoundingClientRect();
      const item = active.getBoundingClientRect();
      if (viewport.height <= 0) return;
      if (item.bottom > viewport.bottom - 8)
        nav.scrollTop += item.bottom - viewport.bottom + 8;
      else if (item.top < viewport.top + 8)
        nav.scrollTop -= viewport.top + 8 - item.top;
    };
    revealSelection();
    const observer =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(revealSelection)
        : undefined;
    observer?.observe(nav);
    return () => observer?.disconnect();
  }, [selectedKey]);
  const { t, i18n } = useTranslation();
  const translationRef = useRef(t);
  translationRef.current = t;
  const translationLanguage = i18n?.resolvedLanguage || i18n?.language || "";
  const { pluginRoutes } = usePlugins();
  const { selectedAgent, getLastChatId } = useAgentStore();
  const [searchOpen, setSearchOpen] = useState(false);
  const [connections, setConnections] = useState<ConnectionItem[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const [reminderSignalCount, setReminderSignalCount] = useState(0);
  const connectionsLoadGateRef = useRef(createLatestRequestGate());
  const reminderLoadGateRef = useRef(createLatestRequestGate());
  const customizationLoadGateRef = useRef(createLatestRequestGate());
  const effectiveCustomizationRef = useRef<CustomizationEffectiveState | null>(
    null,
  );
  const [visibleNavKeys, setVisibleNavKeys] = useState(loadSidebarVisibleKeys);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [channelsExpanded, setChannelsExpanded] = useState(() => {
    try {
      return window.localStorage.getItem(CHANNELS_EXPANDED_KEY) === "1";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    const reloadVisibleKeys = () => {
      const effective = effectiveCustomizationRef.current;
      setVisibleNavKeys(
        effective
          ? applyCustomizationSidebarValues(effective.values, effective.sources)
          : loadSidebarVisibleKeys(),
      );
    };
    window.addEventListener(SIDEBAR_NAV_PREFERENCES_EVENT, reloadVisibleKeys);
    window.addEventListener("storage", reloadVisibleKeys);

    return () => {
      window.removeEventListener(
        SIDEBAR_NAV_PREFERENCES_EVENT,
        reloadVisibleKeys,
      );
      window.removeEventListener("storage", reloadVisibleKeys);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadGate = customizationLoadGateRef.current;
    const syncCustomization = () => {
      const ownsState = loadGate.begin();
      void customizationApi
        .getEffectiveState()
        .then((effective) => {
          if (cancelled || !ownsState()) return;
          effectiveCustomizationRef.current = effective;
          setVisibleNavKeys(
            applyCustomizationSidebarValues(
              effective.values,
              effective.sources,
            ),
          );
        })
        .catch(() => {});
    };
    syncCustomization();
    window.addEventListener(CUSTOMIZATION_CHANGED_EVENT, syncCustomization);
    return () => {
      cancelled = true;
      loadGate.invalidate();
      window.removeEventListener(
        CUSTOMIZATION_CHANGED_EVENT,
        syncCustomization,
      );
    };
  }, []);

  useEffect(() => {
    const loadGate = connectionsLoadGateRef.current;
    const loadConnections = async () => {
      const ownsState = loadGate.begin();
      setConnectionsLoading(true);
      try {
        const [channels, chats] = await Promise.all([
          channelApi.listChannels(),
          chatApi.listChats(),
        ]);
        if (!ownsState()) return;
        setConnections(
          buildConnectionItems(
            channels,
            Array.isArray(chats) ? chats : [],
            (key) => getChannelLabel(key, translationRef.current),
          ),
        );
      } catch (error) {
        if (ownsState()) {
          console.warn("Failed to load sidebar connections:", error);
          setConnections([]);
        }
      } finally {
        if (ownsState()) setConnectionsLoading(false);
      }
    };

    void loadConnections();
    const handleWindowFocus = () => {
      void loadConnections();
    };
    window.addEventListener("focus", handleWindowFocus);

    return () => {
      loadGate.invalidate();
      window.removeEventListener("focus", handleWindowFocus);
    };
  }, [selectedAgent, translationLanguage]);

  useEffect(() => {
    const loadGate = reminderLoadGateRef.current;
    const loadReminderSignal = async () => {
      const ownsState = loadGate.begin();
      try {
        const brief = await reminderApi.getTodayBrief();
        if (ownsState()) {
          setReminderSignalCount(
            Math.max(0, brief.high_value_count + brief.due_count),
          );
        }
      } catch {
        if (ownsState()) setReminderSignalCount(0);
      }
    };

    void loadReminderSignal();
    window.addEventListener("focus", loadReminderSignal);

    return () => {
      loadGate.invalidate();
      window.removeEventListener("focus", loadReminderSignal);
    };
  }, [selectedAgent]);

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
    } catch {
      // ignore storage failures in restricted browser contexts
    }
  }, [collapsed]);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        CHANNELS_EXPANDED_KEY,
        channelsExpanded ? "1" : "0",
      );
    } catch {
      // ignore storage failures in restricted browser contexts
    }
  }, [channelsExpanded]);

  const navGroups = useMemo<NavGroup[]>(() => {
    const baseGroups: NavGroup[] = [
      {
        items: [
          {
            key: "chat",
            label: "聊天",
            icon: <SparkChatTabFill size={18} />,
            path: "/chat",
          },
          {
            key: "works",
            label: "作品",
            icon: <GalleryHorizontalEnd size={18} />,
            path: "/works",
          },
          {
            key: "daily-brief",
            label: "简报",
            icon: <Newspaper size={18} />,
            path: "/daily-brief",
          },
          {
            key: "channels",
            label: "渠道",
            icon: <ApiOutlined />,
            path: "/channels",
          },
        ],
      },
      {
        title: "控制",
        items: [
          {
            key: "reminders",
            label: "今日安排",
            icon: <Bell size={18} />,
            path: "/today",
          },
          {
            key: "cron-jobs",
            label: "定时任务",
            icon: <CalendarOutlined />,
          },
        ],
      },
      {
        title: "工作区",
        items: [
          {
            key: "workspace",
            label: "知识库",
            icon: <BookOpenText size={18} />,
          },
          {
            key: "memory",
            label: "记忆",
            icon: <Brain size={18} />,
          },
        ],
      },
      {
        title: "能力",
        items: [
          {
            key: "agents",
            label: "智能体",
            icon: <Bot size={18} />,
          },
          {
            key: "capabilities",
            label: "能力中心",
            icon: <Boxes size={18} />,
          },
          {
            key: "skills",
            label: "技能",
            icon: <SparkMagicWandLine size={18} />,
          },
          {
            key: "tools",
            label: "工具",
            icon: <SparkToolLine size={18} />,
          },
        ],
      },
      {
        title: "系统",
        items: [
          {
            key: "settings",
            label: "设置",
            icon: <SettingOutlined />,
            path: "/settings",
          },
        ],
      },
    ];

    const filteredBaseGroups = baseGroups
      .map((group) => ({
        ...group,
        items: group.items.filter((item) =>
          isSidebarNavVisible(item.key, visibleNavKeys),
        ),
      }))
      .filter((group) => group.items.length > 0);

    if (pluginRoutes.length === 0) return filteredBaseGroups;

    return [
      ...filteredBaseGroups.slice(0, -1),
      {
        title: "扩展",
        items: pluginRoutes.map((route) => ({
          key: route.path.replace(/^\//, ""),
          label: route.label,
          icon: <span className={styles.navPluginIcon}>{route.icon}</span>,
          path: route.path,
        })),
      },
      filteredBaseGroups[filteredBaseGroups.length - 1],
    ];
  }, [pluginRoutes, visibleNavKeys]);

  const handleNavigate = (item: NavItem) => {
    if (item.key === "chat") {
      const lastChatId = getLastChatId(selectedAgent);
      navigate(lastChatId ? `/chat/${lastChatId}` : "/chat");
      return;
    }
    navigate(item.path ?? KEY_TO_PATH[item.key] ?? `/${item.key}`);
  };

  const handleConnectionClick = (item: ConnectionItem) => {
    if (item.latestChat?.id) {
      navigate(`/chat/${item.latestChat.id}`);
      return;
    }
    navigate("/channels");
  };

  const visibleConnections = connections.slice(0, CONNECTION_ITEM_LIMIT);
  return (
    <>
      <Sider
        width={collapsed ? 64 : 224}
        className={`${styles.sider} ${collapsed ? styles.siderCollapsed : ""}`}
      >
        <button
          type="button"
          className={styles.sidebarSearch}
          onClick={() => setSearchOpen(true)}
          aria-label="搜索聊天、记忆、资料或作品"
          title={collapsed ? "搜索工作内容" : undefined}
        >
          <SearchOutlined />
          <span className={styles.sidebarSearchLabel}>搜索工作内容...</span>
        </button>

        <nav
          ref={navigationRef}
          className={styles.navShell}
          aria-label="澜台导航"
        >
          <div className={styles.navScrollInner}>
            {navGroups.map((group, groupIndex) => (
              <section
                key={group.title ?? `primary-${groupIndex}`}
                className={styles.navGroup}
              >
                {group.title && (
                  <div className={styles.navGroupTitle}>{group.title}</div>
                )}
                <div className={styles.navGroupItems}>
                  {group.items.map((item) => {
                    const active = selectedKey === item.key;
                    const isChannelsItem = item.key === "channels";
                    const hasReminderDot =
                      item.key === "reminders" && reminderSignalCount > 0;
                    const navButton = (
                      <button
                        key={item.key}
                        type="button"
                        className={`${styles.navItem} ${
                          active ? styles.navItemActive : ""
                        } ${
                          isChannelsItem && channelsExpanded
                            ? styles.navItemExpanded
                            : ""
                        }`}
                        onClick={() => handleNavigate(item)}
                        aria-current={active ? "page" : undefined}
                        aria-label={
                          hasReminderDot
                            ? `${item.label}，有今日待处理项`
                            : item.label
                        }
                      >
                        <span className={styles.navItemIcon}>{item.icon}</span>
                        <span className={styles.navItemLabel}>
                          {item.label}
                        </span>
                        {hasReminderDot ? (
                          <span
                            className={styles.navReminderDot}
                            aria-hidden="true"
                          />
                        ) : null}
                      </button>
                    );
                    const itemNode = collapsed ? (
                      <Tooltip
                        key={item.key}
                        title={item.label}
                        placement="right"
                      >
                        <span className={styles.collapsedTooltipWrap}>
                          {navButton}
                        </span>
                      </Tooltip>
                    ) : (
                      navButton
                    );
                    if (!isChannelsItem || collapsed) return itemNode;
                    return (
                      <div key={item.key} className={styles.channelNavBlock}>
                        <div className={styles.channelNavHeader}>
                          {itemNode}
                          <button
                            type="button"
                            className={`${styles.channelDisclosureButton} ${
                              channelsExpanded
                                ? styles.channelDisclosureButtonExpanded
                                : ""
                            }`}
                            onClick={() =>
                              setChannelsExpanded((value) => !value)
                            }
                            aria-label={
                              channelsExpanded
                                ? "收起渠道最近会话"
                                : "展开渠道最近会话"
                            }
                            aria-expanded={channelsExpanded}
                          >
                            {connections.length > 0 ? (
                              <span className={styles.navItemCount}>
                                {connections.length}
                              </span>
                            ) : null}
                            <DownOutlined
                              className={styles.navItemChevron}
                              aria-hidden="true"
                            />
                          </button>
                        </div>
                        {channelsExpanded && (
                          <div className={styles.channelChildren}>
                            {connectionsLoading ? (
                              <div className={styles.channelChildEmpty}>
                                正在读取渠道...
                              </div>
                            ) : visibleConnections.length === 0 ? (
                              <button
                                type="button"
                                className={styles.channelChildEmptyButton}
                                onClick={() => navigate("/channels")}
                              >
                                暂无渠道，去配置
                              </button>
                            ) : (
                              visibleConnections.map((connection) => (
                                <button
                                  key={connection.key}
                                  type="button"
                                  className={styles.channelChildItem}
                                  onClick={() =>
                                    handleConnectionClick(connection)
                                  }
                                  title={
                                    connection.latestChat
                                      ? `打开 ${connection.label} 最近会话`
                                      : `查看 ${connection.label} 渠道设置`
                                  }
                                >
                                  <span
                                    className={styles.channelChildAvatar}
                                    style={{
                                      backgroundColor: getChannelLetterColor(
                                        connection.key,
                                      ),
                                    }}
                                    aria-hidden="true"
                                  >
                                    {getChannelLetter(connection.key)}
                                  </span>
                                  <span className={styles.channelChildCopy}>
                                    <span className={styles.channelChildName}>
                                      {connection.label}
                                    </span>
                                    <span className={styles.channelChildMeta}>
                                      {connection.chatCount > 0
                                        ? `${connection.chatCount} 个会话`
                                        : "暂无会话"}
                                      {" · "}
                                      {formatConnectionTime(
                                        connection.latestAt,
                                      )}
                                    </span>
                                  </span>
                                </button>
                              ))
                            )}
                            <button
                              type="button"
                              className={styles.channelManageButton}
                              onClick={() => navigate("/channels")}
                            >
                              管理渠道
                            </button>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>
            ))}
          </div>
        </nav>
        <div className={styles.sidebarFooter}>
          <Tooltip
            title={collapsed ? "展开侧边栏" : "收起侧边栏"}
            placement={collapsed ? "right" : "top"}
          >
            <button
              type="button"
              className={styles.sidebarCollapseButton}
              onClick={() => setCollapsed((value) => !value)}
              aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
            >
              {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            </button>
          </Tooltip>
        </div>
      </Sider>
      <ChatSearchPanel open={searchOpen} onClose={() => setSearchOpen(false)} />
    </>
  );
}
