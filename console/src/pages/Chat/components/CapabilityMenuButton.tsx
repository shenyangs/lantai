import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Spin } from "antd";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  GitBranch,
  Link2,
  ListChecks,
  Microscope,
  Plus,
  Puzzle,
  Repeat2,
  Route,
  Search,
  Settings2,
  Target,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { mcpApi } from "../../../api/modules/mcp";
import { skillApi } from "../../../api/modules/skill";
import type {
  KdocsConnectionStatus,
  MCPClientInfo,
  OfficialMCPConnectorInfo,
  SkillIndexEntry,
  WpsNoteConnectionStatus,
} from "../../../api/types";
import type { WorkMethod } from "../../../api/types/workMethod";
import { SkillVisual } from "../../../components/SkillVisual";
import { ConnectorBrandIcon } from "../../../components/ConnectorBrandIcon";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { createLatestRequestGate } from "../../../utils/latestRequest";
import { getLocalizedSkillDescription } from "../../../utils/displayDescriptions";
import { isSkillBuiltin } from "../../../utils/skill";
import { getSkillDisplayName } from "../../Agent/Skills/skillPresentation";
import styles from "./CapabilityMenuButton.module.less";

interface CapabilityMenuButtonProps {
  selectedAgent: string;
  workMethods: WorkMethod[];
  workMethodId: string;
  onWorkMethodChange: (methodId: string) => void;
  skillsEnabled: boolean;
  selectedSkillName: string | null;
  onPickSkill: (skillName: string) => void;
  onClearSkill: () => void;
  onManageSkills: () => void;
  planEnabled: boolean;
  planDraftMode: boolean;
  onEnsurePlanEnabled: () => Promise<void>;
  onPlanDraftModeChange: (enabled: boolean) => void;
  goalDraftMode: boolean;
  onGoalDraftModeChange: (enabled: boolean) => void;
  onManageConnectors: () => void;
}

type MenuView = "root" | "work_methods" | "skills" | "connectors";

const POPOVER_GAP = 10;
const POPOVER_MARGIN = 14;
const POPOVER_MAX_WIDTH = 340;

const METHOD_ICONS = {
  standard: Route,
  deep_research: Microscope,
  sustained_execution: Repeat2,
  collaboration: GitBranch,
} as const;

function skillSupportsConsole(skill: SkillIndexEntry): boolean {
  const channels = skill.channels?.length ? skill.channels : ["all"];
  return channels.includes("all") || channels.includes("console");
}

interface ComposerOfficialConnector {
  key: string;
  name: string;
  description: string;
  mark: string;
  connected: boolean;
  status: string;
}

export default function CapabilityMenuButton({
  selectedAgent,
  workMethods,
  workMethodId,
  onWorkMethodChange,
  skillsEnabled,
  selectedSkillName,
  onPickSkill,
  onClearSkill,
  onManageSkills,
  planEnabled,
  planDraftMode,
  onEnsurePlanEnabled,
  onPlanDraftModeChange,
  goalDraftMode,
  onGoalDraftModeChange,
  onManageConnectors,
}: CapabilityMenuButtonProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<MenuView>("root");
  const [clients, setClients] = useState<MCPClientInfo[]>([]);
  const [officialConnectors, setOfficialConnectors] = useState<
    OfficialMCPConnectorInfo[]
  >([]);
  const [kdocsStatus, setKdocsStatus] = useState<KdocsConnectionStatus | null>(
    null,
  );
  const [wpsNoteStatus, setWpsNoteStatus] =
    useState<WpsNoteConnectionStatus | null>(null);
  const [skills, setSkills] = useState<SkillIndexEntry[]>([]);
  const [search, setSearch] = useState("");
  const [connectorLoading, setConnectorLoading] = useState(false);
  const [skillLoading, setSkillLoading] = useState(false);
  const [skillLoadFailed, setSkillLoadFailed] = useState(false);
  const [planMutating, setPlanMutating] = useState(false);
  const [connectorMutating, setConnectorMutating] = useState<string | null>(
    null,
  );
  const connectorGateRef = useRef(createLatestRequestGate());
  const skillGateRef = useRef(createLatestRequestGate());
  const workMethodBeforeGoalRef = useRef<string | null>(null);
  const anchorRef = useRef<HTMLDivElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [popoverPosition, setPopoverPosition] = useState({
    left: POPOVER_MARGIN,
    top: POPOVER_MARGIN,
  });

  const selectedMethod = useMemo(
    () =>
      workMethods.find((method) => method.id === workMethodId) ??
      workMethods[0],
    [workMethodId, workMethods],
  );

  const availableSkills = useMemo(
    () =>
      skills.filter(
        (skill) => skill.enabled !== false && skillSupportsConsole(skill),
      ),
    [skills],
  );

  const visibleSkills = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return availableSkills;
    return availableSkills.filter((skill) => {
      const haystack = [
        skill.name,
        getLocalizedSkillDescription(skill, t),
        ...(skill.tags ?? []),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [availableSkills, search, t]);

  const officialConnectorItems = useMemo<ComposerOfficialConnector[]>(() => {
    const activeClientKeys = new Set(
      clients.filter((client) => client.enabled).map((client) => client.key),
    );
    return [
      {
        key: "kdocs",
        name: "金山文档",
        description: "创建、搜索和管理 WPS 云文档",
        mark: "金",
        connected: Boolean(kdocsStatus?.authenticated),
        status: kdocsStatus?.authenticated ? "已连接" : "连接",
      },
      {
        key: "wps-note",
        name: "WPS 笔记",
        description: "读取、编辑和管理 WPS 笔记",
        mark: "记",
        connected: Boolean(wpsNoteStatus?.connected),
        status: wpsNoteStatus?.connected ? "已连接" : "连接",
      },
      ...officialConnectors.map((connector) => {
        const enabled = activeClientKeys.has(connector.key);
        return {
          key: connector.key,
          name: connector.name,
          description: connector.description,
          mark: connector.name.trim().slice(0, 1).toUpperCase() || "M",
          connected: connector.configured && enabled,
          status: connector.configured
            ? enabled
              ? "已启用"
              : "待启用"
            : "连接",
        };
      }),
    ];
  }, [clients, kdocsStatus, officialConnectors, wpsNoteStatus]);

  const visibleCustomClients = useMemo(() => {
    const officialKeys = new Set(officialConnectors.map((item) => item.key));
    return clients.filter(
      (client) => client.enabled && !officialKeys.has(client.key),
    );
  }, [clients, officialConnectors]);

  const loadConnectors = useCallback(async () => {
    const ownsState = connectorGateRef.current.begin();
    setConnectorLoading(true);
    const [clientResult, catalogResult, kdocsResult, wpsNoteResult] =
      await Promise.allSettled([
        mcpApi.listMCPClients(),
        mcpApi.listOfficialMCPConnectors(),
        skillApi.getKdocsConnectionStatus(),
        skillApi.getWpsNoteConnectionStatus(),
      ]);
    if (!ownsState()) return;
    if (clientResult.status === "fulfilled") {
      setClients(
        Array.isArray(clientResult.value)
          ? clientResult.value.filter((client) => client.enabled)
          : [],
      );
    } else {
      console.error(
        "Failed to load configured composer connectors:",
        clientResult.reason,
      );
      setClients([]);
    }
    if (catalogResult.status === "fulfilled") {
      setOfficialConnectors(
        Array.isArray(catalogResult.value) ? catalogResult.value : [],
      );
    } else {
      console.error(
        "Failed to load official connector catalog:",
        catalogResult.reason,
      );
      setOfficialConnectors([]);
    }
    if (kdocsResult.status === "fulfilled") {
      setKdocsStatus(kdocsResult.value);
    } else {
      console.error("Failed to load Kingsoft Docs status:", kdocsResult.reason);
      setKdocsStatus(null);
    }
    if (wpsNoteResult.status === "fulfilled") {
      setWpsNoteStatus(wpsNoteResult.value);
    } else {
      console.error("Failed to load WPS Note status:", wpsNoteResult.reason);
      setWpsNoteStatus(null);
    }
    setConnectorLoading(false);
  }, []);

  const loadSkills = useCallback(async () => {
    const ownsState = skillGateRef.current.begin();
    setSkillLoading(true);
    setSkillLoadFailed(false);
    try {
      const result = await skillApi.listSkillIndex(selectedAgent);
      if (ownsState()) setSkills(Array.isArray(result) ? result : []);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load composer skills:", error);
      setSkills([]);
      setSkillLoadFailed(true);
    } finally {
      if (ownsState()) setSkillLoading(false);
    }
  }, [selectedAgent]);

  useEffect(() => {
    const connectorGate = connectorGateRef.current;
    if (open && view === "connectors") void loadConnectors();
    return () => connectorGate.invalidate();
  }, [loadConnectors, open, view]);

  useEffect(() => {
    const skillGate = skillGateRef.current;
    if (open && view === "skills" && skillsEnabled) void loadSkills();
    return () => skillGate.invalidate();
  }, [loadSkills, open, skillsEnabled, view]);

  useEffect(() => {
    connectorGateRef.current.invalidate();
    skillGateRef.current.invalidate();
    setOpen(false);
    setView("root");
    setSearch("");
    setClients([]);
    setOfficialConnectors([]);
    setKdocsStatus(null);
    setWpsNoteStatus(null);
    setSkills([]);
  }, [selectedAgent]);

  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !anchorRef.current?.contains(target) &&
        !popoverRef.current?.contains(target)
      ) {
        setOpen(false);
        setView("root");
        setSearch("");
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        setView("root");
        setSearch("");
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  const updatePopoverPosition = useCallback(() => {
    const trigger = triggerRef.current;
    const popover = popoverRef.current;
    if (!trigger || !popover) return;

    const triggerRect = trigger.getBoundingClientRect();
    const popoverRect = popover.getBoundingClientRect();
    const availableWidth = Math.max(0, window.innerWidth - POPOVER_MARGIN * 2);
    const popoverWidth = Math.min(POPOVER_MAX_WIDTH, availableWidth);
    const maxLeft = Math.max(
      POPOVER_MARGIN,
      window.innerWidth - popoverWidth - POPOVER_MARGIN,
    );
    const left = Math.min(Math.max(triggerRect.left, POPOVER_MARGIN), maxLeft);
    const preferredTop = triggerRect.top - popoverRect.height - POPOVER_GAP;
    const maxTop = Math.max(
      POPOVER_MARGIN,
      window.innerHeight - popoverRect.height - POPOVER_MARGIN,
    );
    const top =
      preferredTop >= POPOVER_MARGIN
        ? preferredTop
        : Math.min(triggerRect.bottom + POPOVER_GAP, maxTop);

    setPopoverPosition((previous) => {
      if (previous.left === left && previous.top === top) return previous;
      return { left, top };
    });
  }, []);

  useLayoutEffect(() => {
    if (!open) return undefined;
    updatePopoverPosition();

    const handleViewportChange = () => updatePopoverPosition();
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleViewportChange, true);

    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(handleViewportChange);
    if (popoverRef.current) resizeObserver?.observe(popoverRef.current);

    return () => {
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleViewportChange, true);
      resizeObserver?.disconnect();
    };
  }, [open, updatePopoverPosition, view]);

  const closeMenu = useCallback(() => {
    setOpen(false);
    setView("root");
    setSearch("");
  }, []);

  const handlePlanToggle = useCallback(async () => {
    if (planMutating) return;
    const next = !planDraftMode;
    setPlanMutating(true);
    try {
      if (next && !planEnabled) await onEnsurePlanEnabled();
      if (next) {
        if (
          goalDraftMode &&
          workMethodId === "sustained_execution" &&
          workMethodBeforeGoalRef.current
        ) {
          onWorkMethodChange(workMethodBeforeGoalRef.current);
        }
        onGoalDraftModeChange(false);
      }
      onPlanDraftModeChange(next);
      closeMenu();
      message.success(
        next
          ? "计划模式已开启：下一条消息会先给出方案，确认后再执行。"
          : "计划模式已关闭。",
      );
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "计划模式开启失败，请重试。",
      );
    } finally {
      setPlanMutating(false);
    }
  }, [
    closeMenu,
    message,
    onEnsurePlanEnabled,
    onGoalDraftModeChange,
    onWorkMethodChange,
    onPlanDraftModeChange,
    goalDraftMode,
    planDraftMode,
    planEnabled,
    planMutating,
    workMethodId,
  ]);

  const handleGoalToggle = useCallback(async () => {
    if (planMutating) return;
    const next = !goalDraftMode;
    setPlanMutating(true);
    try {
      if (next && !planEnabled) await onEnsurePlanEnabled();
      if (next) {
        workMethodBeforeGoalRef.current = workMethodId;
        onWorkMethodChange("sustained_execution");
        onPlanDraftModeChange(false);
      } else if (
        workMethodId === "sustained_execution" &&
        workMethodBeforeGoalRef.current
      ) {
        onWorkMethodChange(workMethodBeforeGoalRef.current);
      }
      onGoalDraftModeChange(next);
      closeMenu();
      message.success(
        next
          ? "目标模式已开启：下一条消息会建立目标、拆分步骤并持续推进。"
          : "目标模式已关闭。",
      );
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "目标模式开启失败，请重试。",
      );
    } finally {
      setPlanMutating(false);
    }
  }, [
    closeMenu,
    goalDraftMode,
    message,
    onEnsurePlanEnabled,
    onGoalDraftModeChange,
    onPlanDraftModeChange,
    onWorkMethodChange,
    planEnabled,
    planMutating,
    workMethodId,
  ]);

  const handleConnectorToggle = useCallback(
    async (client: MCPClientInfo) => {
      if (connectorMutating) return;
      setConnectorMutating(client.key);
      try {
        const updated = await mcpApi.toggleMCPClient(client.key);
        setClients((previous) =>
          updated.enabled
            ? previous.map((item) =>
                item.key === updated.key ? updated : item,
              )
            : previous.filter((item) => item.key !== updated.key),
        );
        message.success(
          updated.enabled
            ? `已启用连接器：${updated.name}`
            : `已停用连接器：${updated.name}`,
        );
      } catch (error) {
        message.error(
          error instanceof Error ? error.message : "连接器状态更新失败。",
        );
      } finally {
        setConnectorMutating(null);
      }
    },
    [connectorMutating, message],
  );

  const panelHeader = (title: string, subtitle: string) => (
    <div className={styles.panelHeader}>
      <button
        type="button"
        className={styles.backButton}
        aria-label="返回能力菜单"
        onClick={() => setView("root")}
      >
        <ArrowLeft size={17} />
      </button>
      <span>
        <strong>{title}</strong>
        <small>{subtitle}</small>
      </span>
    </div>
  );

  const rootMenu = (
    <div className={styles.menu} role="menu" aria-label="能力菜单">
      <div className={styles.menuSectionLabel} role="presentation">
        <strong>怎么做</strong>
        <span>改变规划、推进方式与停止条件</span>
      </div>
      <button
        type="button"
        role="menuitem"
        data-chat-work-method-trigger="1"
        aria-label={`工作法：${selectedMethod?.name ?? "标准"}`}
        className={styles.menuItem}
        onClick={() => setView("work_methods")}
      >
        <span className={styles.itemIcon}>
          <Route size={17} strokeWidth={1.9} />
        </span>
        <span className={styles.itemCopy}>
          <span className={styles.itemTitle}>工作法</span>
          <small>{selectedMethod?.summary || "按任务形态选择推进方式"}</small>
        </span>
        <span className={styles.itemValue}>
          {selectedMethod?.name ?? "标准"}
        </span>
        <ChevronRight className={styles.itemArrow} size={15} />
      </button>
      <button
        type="button"
        role="menuitemcheckbox"
        aria-checked={planDraftMode}
        className={`${styles.menuItem} ${
          planDraftMode ? styles.menuItemActive : ""
        }`}
        disabled={planMutating}
        onClick={() => void handlePlanToggle()}
      >
        <span className={styles.itemIcon}>
          {planMutating ? (
            <Spin size="small" />
          ) : (
            <ListChecks size={17} strokeWidth={1.9} />
          )}
        </span>
        <span className={styles.itemCopy}>
          <span className={styles.itemTitle}>计划模式</span>
          <small>下一条先给方案，确认后再执行</small>
        </span>
        <span className={styles.itemValue}>
          {planDraftMode ? "下一条生效" : ""}
        </span>
        <span className={styles.checkSlot}>
          {planDraftMode ? <Check size={15} strokeWidth={2.2} /> : null}
        </span>
      </button>

      <button
        type="button"
        role="menuitemcheckbox"
        aria-checked={goalDraftMode}
        className={`${styles.menuItem} ${
          goalDraftMode ? styles.menuItemActive : ""
        }`}
        disabled={planMutating}
        onClick={() => void handleGoalToggle()}
      >
        <span className={styles.itemIcon}>
          {planMutating ? (
            <Spin size="small" />
          ) : (
            <Target size={17} strokeWidth={1.9} />
          )}
        </span>
        <span className={styles.itemCopy}>
          <span className={styles.itemTitle}>目标模式</span>
          <small>建立目标并持续推进到完成或需要你处理</small>
        </span>
        <span className={styles.itemValue}>
          {goalDraftMode ? "下一条生效" : ""}
        </span>
        <span className={styles.checkSlot}>
          {goalDraftMode ? <Check size={15} strokeWidth={2.2} /> : null}
        </span>
      </button>

      <div className={styles.menuSectionLabel} role="presentation">
        <strong>使用什么</strong>
        <span>为下一条消息选择技能与外部服务</span>
      </div>
      <button
        type="button"
        role="menuitem"
        aria-label={`技能${selectedSkillName ? `：${selectedSkillName}` : ""}`}
        className={styles.menuItem}
        disabled={!skillsEnabled}
        onClick={() => setView("skills")}
      >
        <span className={styles.itemIcon}>
          <Puzzle size={17} strokeWidth={1.9} />
        </span>
        <span className={styles.itemCopy}>
          <span className={styles.itemTitle}>技能</span>
          <small>为这轮调用一项专用方法</small>
        </span>
        <span className={styles.itemValue}>
          {skillsEnabled
            ? selectedSkillName
              ? getSkillDisplayName(selectedSkillName)
              : "未指定"
            : "当前不可用"}
        </span>
        <ChevronRight className={styles.itemArrow} size={15} />
      </button>

      <button
        type="button"
        role="menuitem"
        className={styles.menuItem}
        onClick={() => setView("connectors")}
      >
        <span className={styles.itemIcon}>
          <Link2 size={17} strokeWidth={1.9} />
        </span>
        <span className={styles.itemCopy}>
          <span className={styles.itemTitle}>连接器</span>
          <small>使用已连接的服务与数据</small>
        </span>
        <span className={styles.itemValue}>查看</span>
        <ChevronRight className={styles.itemArrow} size={15} />
      </button>
    </div>
  );

  const workMethodPanel = (
    <div className={styles.panel} aria-label="工作法">
      {panelHeader("工作法", "按任务形态选择推进方式")}
      <div className={styles.optionList} role="menu">
        {workMethods.map((method) => {
          const Icon =
            METHOD_ICONS[method.id as keyof typeof METHOD_ICONS] ?? GitBranch;
          const selected = method.id === selectedMethod?.id;
          return (
            <button
              key={method.id}
              type="button"
              role="menuitemradio"
              aria-checked={selected}
              className={`${styles.optionItem} ${
                selected ? styles.optionItemActive : ""
              }`}
              onClick={() => {
                onWorkMethodChange(method.id);
                closeMenu();
              }}
            >
              <span className={styles.optionIcon}>
                <Icon size={18} strokeWidth={1.9} />
              </span>
              <span className={styles.optionCopy}>
                <strong>{method.name}</strong>
                <small>{method.summary}</small>
              </span>
              <span className={styles.checkSlot}>
                {selected ? <Check size={17} strokeWidth={2.2} /> : null}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );

  const skillPanel = (
    <div className={styles.panel} aria-label="技能">
      {panelHeader("技能", "为下一轮选择一个已启用技能")}
      <label className={styles.searchBox}>
        <Search size={16} />
        <input
          value={search}
          placeholder="搜索技能..."
          onChange={(event) => setSearch(event.target.value)}
        />
      </label>
      <div className={styles.optionList} role="menu">
        {skillLoading ? (
          <div className={styles.centerState}>
            <Spin size="small" />
            <span>正在读取技能...</span>
          </div>
        ) : skillLoadFailed ? (
          <div className={styles.centerState}>
            <span>技能读取失败</span>
            <button type="button" onClick={() => void loadSkills()}>
              重试
            </button>
          </div>
        ) : visibleSkills.length === 0 ? (
          <div className={styles.emptyState}>
            <Puzzle size={18} strokeWidth={1.8} />
            <strong>没有匹配的已启用技能</strong>
          </div>
        ) : (
          visibleSkills.map((skill) => {
            const selected = skill.name === selectedSkillName;
            const description = getLocalizedSkillDescription(skill, t);
            return (
              <button
                key={skill.name}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                aria-label={`${getSkillDisplayName(
                  skill.name,
                  description,
                )}，技术名称 ${skill.name}`}
                className={`${styles.optionItem} ${
                  selected ? styles.optionItemActive : ""
                }`}
                onClick={() => {
                  if (selected) onClearSkill();
                  else onPickSkill(skill.name);
                  closeMenu();
                }}
              >
                <span className={styles.skillVisual}>
                  <SkillVisual name={skill.name} />
                </span>
                <span className={styles.optionCopy}>
                  <strong title={`技术名称：${skill.name}`}>
                    {getSkillDisplayName(skill.name, description)}
                  </strong>
                  <small>{description}</small>
                </span>
                <span className={styles.skillMeta}>
                  {isSkillBuiltin(skill.source) ? "内置" : "自定义"}
                </span>
                <span className={styles.checkSlot}>
                  {selected ? <Check size={17} strokeWidth={2.2} /> : null}
                </span>
              </button>
            );
          })
        )}
      </div>
      <button
        type="button"
        className={styles.manageButton}
        onClick={() => {
          closeMenu();
          onManageSkills();
        }}
      >
        <Settings2 size={17} />
        <span>技能管理</span>
        <ChevronRight size={15} />
      </button>
    </div>
  );

  const connectorPanel = (
    <div className={styles.panel} aria-label="连接器">
      {panelHeader("连接器", "外部工具与数据源")}
      <div className={styles.optionList}>
        {connectorLoading ? (
          <div className={styles.centerState}>
            <Spin size="small" />
            <span>正在读取连接器...</span>
          </div>
        ) : officialConnectorItems.length === 0 &&
          visibleCustomClients.length === 0 ? (
          <div className={styles.emptyState}>
            <Link2 size={18} strokeWidth={1.8} />
            <strong>还没有配置连接器</strong>
            <span>添加 MCP 服务后，就能在对话中读取数据或执行操作。</span>
          </div>
        ) : (
          <>
            {officialConnectorItems.map((connector) => (
              <button
                key={`official-${connector.key}`}
                type="button"
                aria-label={`${connector.name} ${connector.status}`}
                className={`${styles.connectorItem} ${
                  connector.connected ? styles.optionItemActive : ""
                }`}
                onClick={() => {
                  closeMenu();
                  onManageConnectors();
                }}
              >
                <ConnectorBrandIcon
                  connectorKey={connector.key}
                  name={connector.name}
                  fallback={connector.mark}
                  className={styles.connectorMark}
                />
                <span className={styles.optionCopy}>
                  <strong>{connector.name}</strong>
                  <small>{connector.description}</small>
                </span>
                <span
                  className={`${styles.connectorStatus} ${
                    connector.connected ? styles.connectorStatusConnected : ""
                  }`}
                >
                  {connector.status}
                </span>
              </button>
            ))}
            {visibleCustomClients.map((client) => {
              const mutating = connectorMutating === client.key;
              return (
                <button
                  key={client.key}
                  type="button"
                  role="switch"
                  aria-label={client.name}
                  aria-checked={client.enabled}
                  className={`${styles.connectorItem} ${
                    client.enabled ? styles.optionItemActive : ""
                  }`}
                  disabled={Boolean(connectorMutating)}
                  onClick={() => void handleConnectorToggle(client)}
                >
                  <span className={styles.connectorMark}>
                    {client.name.trim().slice(0, 1).toUpperCase() || "M"}
                  </span>
                  <span className={styles.optionCopy}>
                    <strong>{client.name}</strong>
                    <small>
                      {client.description ||
                        (client.transport === "stdio"
                          ? "本地 MCP"
                          : "远程 MCP")}
                    </small>
                  </span>
                  <span
                    className={`${styles.switchTrack} ${
                      client.enabled ? styles.switchTrackOn : ""
                    }`}
                    aria-hidden="true"
                  >
                    {mutating ? <Spin size="small" /> : <i />}
                  </span>
                </button>
              );
            })}
          </>
        )}
      </div>
      <button
        type="button"
        className={styles.manageButton}
        onClick={() => {
          closeMenu();
          onManageConnectors();
        }}
      >
        <Settings2 size={17} />
        <span>连接器管理</span>
        <ChevronRight size={15} />
      </button>
    </div>
  );

  const content =
    view === "work_methods"
      ? workMethodPanel
      : view === "skills"
        ? skillPanel
        : view === "connectors"
          ? connectorPanel
          : rootMenu;
  const hasActiveChoice =
    planDraftMode || Boolean(selectedSkillName) || workMethodId !== "standard";
  const activeChoiceLabel = [
    goalDraftMode
      ? "目标"
      : planDraftMode
        ? "计划"
        : selectedMethod?.name || "标准",
    selectedSkillName ? getSkillDisplayName(selectedSkillName) : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const popover =
    open && typeof document !== "undefined"
      ? createPortal(
          <div
            ref={popoverRef}
            className={styles.popover}
            data-testid="capability-menu-popover"
            style={popoverPosition}
          >
            {content}
          </div>,
          document.body,
        )
      : null;

  return (
    <div ref={anchorRef} className={styles.anchor}>
      {popover}
      <button
        ref={triggerRef}
        type="button"
        data-chat-capability-trigger="1"
        data-active-choice={hasActiveChoice ? "true" : "false"}
        className={`${styles.trigger} ${
          hasActiveChoice ? styles.triggerActive : ""
        }`}
        aria-label="能力菜单"
        title={`当前生效：${activeChoiceLabel}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setOpen((value) => !value);
          if (open) setView("root");
        }}
      >
        <Plus size={18} strokeWidth={1.9} />
        {hasActiveChoice ? (
          <span className={styles.triggerLabel}>{activeChoiceLabel}</span>
        ) : null}
        {hasActiveChoice ? <span className={styles.activeDot} /> : null}
      </button>
    </div>
  );
}
