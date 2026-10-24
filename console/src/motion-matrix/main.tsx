import { createRoot } from "react-dom/client";
import { useEffect, useMemo } from "react";
import { HashRouter } from "react-router-dom";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  Circle,
  Clock3,
  Copy,
  FileText,
  ListChecks,
  Loader2,
  Menu,
  MessageSquare,
  MoreHorizontal,
  PanelRightOpen,
  RefreshCcw,
  Search,
  Settings,
  Wrench,
  X,
} from "lucide-react";
import "../i18n";
import "../styles/index.css";
import { ApprovalCard } from "../components/ApprovalCard/ApprovalCard";
import ToolCallCard from "../pages/Chat/components/ToolCallCard";
import SubAgentToolCard from "../pages/Chat/components/SubAgentToolCard";
import SubAgentInspectorPanel from "../pages/Chat/components/SubAgentInspectorPanel";
import ChatSessionItem from "../pages/Chat/components/ChatSessionItem";
import layoutStyles from "../layouts/index.module.less";
import contextMenuStyles from "../components/ContextMenu/index.module.less";
import modelSelectorStyles from "../pages/Chat/ModelSelector/index.module.less";
import planStyles from "../components/PlanPanel/index.module.less";
import traceStyles from "../pages/Chat/components/TurnEventPreview.module.less";
import matrixStyles from "./motion-matrix.module.less";
import type { AgentSummary } from "../api/types/agents";

declare global {
  interface Window {
    __motionMatrixReady?: boolean;
  }
}

const nowSeconds = Math.floor(Date.now() / 1000) - 85;

const toolCallRunningData = {
  status: "running",
  content: [
    {
      data: {
        name: "web_fetch",
        arguments: { url: "https://example.com/report" },
      },
    },
  ],
};

const toolCallDoneData = {
  status: "completed",
  content: [
    {
      data: {
        name: "memory_search",
        arguments: { query: "Apple Design motion guardrails" },
      },
    },
    {
      data: {
        output: [
          {
            type: "text",
            text: "命中 UI motion 准则：高频路径保持低位移、显式 transition 和 reduced motion 降级。",
          },
        ],
      },
    },
  ],
};

const agents: AgentSummary[] = [
  {
    id: "Lantai_Mocha_Agent",
    name: "摩卡",
    description: "视觉设计、前端、页面",
    workspace_dir: "/tmp/mocha",
    enabled: true,
  },
];

function useMatrixMode() {
  return useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    return {
      theme: params.get("theme") === "dark" ? "dark" : "light",
      motion: params.get("motion") === "reduce" ? "reduce" : "normal",
      fixture: params.get("fixture"),
    };
  }, []);
}

function MatrixSection({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className={matrixStyles.section}>
      <header className={matrixStyles.sectionHeader}>
        <span>
          <strong>{title}</strong>
          <small>{description}</small>
        </span>
      </header>
      {children}
    </section>
  );
}

function NavigationFixture() {
  return (
    <MatrixSection
      title="Header / Sidebar"
      description="默认、hover、active、折叠按钮、工作区入口"
    >
      <div className={matrixStyles.shellFixture}>
        <aside className={`${layoutStyles.sider} ${matrixStyles.fixtureSider}`}>
          <button className={layoutStyles.sidebarCollapseButton}>
            <Menu size={16} />
          </button>
          <div className={layoutStyles.navGroup}>
            <div className={layoutStyles.navGroupTitle}>工作区</div>
            <div className={layoutStyles.navGroupItems}>
              <div
                className={`${layoutStyles.navItem} ${layoutStyles.navItemActive}`}
              >
                <MessageSquare className={layoutStyles.navItemIcon} size={16} />
                <span className={layoutStyles.navItemLabel}>Chat</span>
              </div>
              <div className={layoutStyles.navItem}>
                <FileText className={layoutStyles.navItemIcon} size={16} />
                <span className={layoutStyles.navItemLabel}>Memory</span>
                <span className={layoutStyles.navItemCount}>3</span>
              </div>
            </div>
          </div>
          <button className={layoutStyles.sidebarModelCard}>
            <span className={layoutStyles.sidebarModelAvatar}>M</span>
            <span className={layoutStyles.sidebarModelMeta}>
              <strong>Mocha</strong>
              <small>Claude Sonnet</small>
            </span>
            <ChevronDown
              className={layoutStyles.sidebarModelChevron}
              size={14}
            />
          </button>
        </aside>
        <main className={matrixStyles.shellMain}>
          <header className={layoutStyles.header}>
            <button className={layoutStyles.headerWorkspaceButton}>
              <span className={layoutStyles.headerWorkspaceLabel}>
                lantai3 / console
              </span>
            </button>
            <div className={layoutStyles.headerRight}>
              <button className={layoutStyles.headerIconButton}>
                <Settings size={16} />
              </button>
              <button className={layoutStyles.headerIconButton}>
                <MoreHorizontal size={16} />
              </button>
            </div>
          </header>
          <div className={matrixStyles.workspaceHint}>
            高频导航使用轻量 hover、press 和显式 transition。
          </div>
        </main>
      </div>
    </MatrixSection>
  );
}

function MenuAndSelectorFixture() {
  return (
    <MatrixSection
      title="ContextMenu / ModelSelector"
      description="弹层来源、搜索、恢复提示、菜单项 hover"
    >
      <div className={matrixStyles.popoverGrid}>
        <div className={contextMenuStyles.contextMenu}>
          <button className={contextMenuStyles.menuItem}>
            <Copy className={contextMenuStyles.menuIcon} size={14} />
            <span className={contextMenuStyles.menuLabel}>复制路径</span>
          </button>
          <button className={contextMenuStyles.menuItem}>
            <Settings className={contextMenuStyles.menuIcon} size={14} />
            <span className={contextMenuStyles.menuLabel}>打开设置</span>
          </button>
          <div className={contextMenuStyles.divider} />
          <button className={contextMenuStyles.menuItem}>
            <X className={contextMenuStyles.menuIcon} size={14} />
            <span className={contextMenuStyles.menuLabel}>移除</span>
          </button>
        </div>
        <div className={matrixStyles.selectorFixture}>
          <button className={modelSelectorStyles.trigger}>
            <span className={modelSelectorStyles.triggerName}>
              Claude Sonnet 4.5
            </span>
            <ChevronDown
              className={modelSelectorStyles.triggerArrow}
              size={14}
            />
          </button>
          <div className={modelSelectorStyles.panel}>
            <div className={modelSelectorStyles.searchBox}>
              <Search size={14} />
              <input value="sonnet" readOnly aria-label="模型搜索" />
            </div>
            <div className={modelSelectorStyles.recoveryPanel}>
              <span className={modelSelectorStyles.recoveryIcon}>↺</span>
              <span className={modelSelectorStyles.recoveryText}>
                <strong>恢复上次模型</strong>
                <span>检测到工作区模型恢复建议</span>
              </span>
              <button className={modelSelectorStyles.recoveryButton}>
                恢复
              </button>
            </div>
            <div className={modelSelectorStyles.modelItem}>
              <span className={modelSelectorStyles.modelText}>
                <span className={modelSelectorStyles.modelName}>
                  Claude Sonnet
                </span>
                <span className={modelSelectorStyles.modelId}>
                  anthropic/claude-sonnet-4.5
                </span>
              </span>
              <CheckCircle2
                className={modelSelectorStyles.checkIcon}
                size={14}
              />
            </div>
            <button className={modelSelectorStyles.settingsButton}>
              <Settings size={14} />
              模型设置
            </button>
          </div>
        </div>
      </div>
    </MatrixSection>
  );
}

function ChatListFixture() {
  return (
    <MatrixSection
      title="ChatSessionItem"
      description="默认、选中、活跃状态、操作按钮显现"
    >
      <div className={matrixStyles.sessionList}>
        <ChatSessionItem
          sessionId="motion-active"
          name="UI motion 治理"
          time="刚刚"
          channelKey="console"
          channelLabel="Desktop"
          chatStatus="running"
          active
          onClick={() => undefined}
        />
        <ChatSessionItem
          sessionId="motion-idle"
          name="截图矩阵验证"
          time="12 分钟前"
          chatStatus="idle"
          onClick={() => undefined}
        />
      </div>
    </MatrixSection>
  );
}

function ApprovalFixture() {
  return (
    <MatrixSection
      title="ApprovalCard"
      description="高风险确认、参数展开、按钮 press"
    >
      <ApprovalCard
        requestId="motion-fixture"
        toolName="shell_command"
        severity="high"
        findingsCount={2}
        findingsSummary="命令会修改工作区文件，需要逐次确认。"
        toolParams={{
          cmd: "npm run check:motion",
          cwd: "/Users/example/Downloads/codex/lantai_1/项目主文件夹/lantai3/console",
        }}
        createdAt={nowSeconds}
        timeoutSeconds={600}
        policyDecision={{
          risk_level: "high",
          reason: "需要确认命令作用范围。",
          command_category: "validation",
        }}
        rememberable
        approvalOptions={["allow_session", "allow_workspace"]}
        onApprove={async () => undefined}
        onDeny={async () => undefined}
      />
    </MatrixSection>
  );
}

function PlanFixture() {
  return (
    <MatrixSection
      title="InlinePlanPanel"
      description="进度条、进行中状态、reduced motion spinner 降级"
    >
      <section
        className={planStyles.inlinePanel}
        data-expanded="true"
        data-state="in_progress"
      >
        <button className={planStyles.inlineHeader} aria-expanded="true">
          <span className={planStyles.inlineTitleGroup}>
            <span className={planStyles.inlineStatusDot} />
            <ListChecks size={16} />
            <strong>执行计划</strong>
          </span>
          <span className={planStyles.inlineSummary}>
            捕获 light / dark / reduced-motion 截图
          </span>
          <span className={planStyles.inlineProgressMeta}>
            <span>已完成 2/5</span>
            <span className={planStyles.inlineProgressDivider}>·</span>
            <span>正在第 3 步</span>
          </span>
          <ChevronDown size={16} />
        </button>
        <div className={planStyles.inlineProgressTrack}>
          <span style={{ width: "42%" }} />
        </div>
        <ol className={planStyles.inlineTaskList}>
          <li
            className={planStyles.inlineTaskItem}
            data-active="false"
            data-state="done"
          >
            <CheckCircle2 size={15} />
            <span className={planStyles.inlineTaskBody}>
              <span className={planStyles.inlineTaskMeta}>
                <span>步骤 2</span>
                <span>已完成</span>
              </span>
              <span className={planStyles.inlineTaskName}>清理 transition</span>
              <span className={planStyles.inlineTaskOutcome}>Result: 0 处</span>
            </span>
          </li>
          <li
            className={planStyles.inlineTaskItem}
            data-active="true"
            data-state="in_progress"
          >
            <Loader2 className={planStyles.inlineTaskIconSpin} size={15} />
            <span className={planStyles.inlineTaskBody}>
              <span className={planStyles.inlineTaskMeta}>
                <span>步骤 3</span>
                <span>进行中</span>
              </span>
              <span className={planStyles.inlineTaskName}>截图矩阵验证</span>
              <span className={planStyles.inlineTaskDesc}>
                用 Electron 捕获可重复视觉证据。
              </span>
            </span>
          </li>
          <li
            className={planStyles.inlineTaskItem}
            data-active="false"
            data-state="todo"
          >
            <Circle size={15} />
            <span className={planStyles.inlineTaskBody}>
              <span className={planStyles.inlineTaskMeta}>
                <span>步骤 4</span>
                <span>待办</span>
              </span>
              <span className={planStyles.inlineTaskName}>整理验收证据</span>
            </span>
          </li>
        </ol>
        <div className={planStyles.inlineHiddenCount}>另有 2 步</div>
        <div className={planStyles.inlinePlanActions}>
          <span data-warning="false">计划包含验收步骤</span>
          <div>
            <button className={planStyles.inlinePlanPrimaryAction}>
              <PanelRightOpen size={13} />
              查看完整计划
            </button>
            <button>
              <RefreshCcw size={13} />
              调整计划
            </button>
          </div>
        </div>
      </section>
    </MatrixSection>
  );
}

function ToolCardsFixture() {
  return (
    <MatrixSection
      title="ToolCallCard / SubAgentToolCard"
      description="running、done、failed、子 Agent 状态卡"
    >
      <div className={matrixStyles.cardStack}>
        <ToolCallCard data={toolCallRunningData} />
        <ToolCallCard data={toolCallDoneData} />
        <SubAgentToolCard
          agents={agents}
          data={{
            status: "running",
            content: [
              {
                data: {
                  name: "chat_with_agent",
                  arguments: {
                    to_agent: "Lantai_Mocha_Agent",
                    text: "检查 UI motion 截图矩阵。",
                  },
                },
              },
            ],
          }}
        />
      </div>
    </MatrixSection>
  );
}

function SubAgentInspectorFixture() {
  return (
    <section
      style={{
        position: "relative",
        width: "100%",
        height: 760,
        display: "flex",
        overflow: "hidden",
        border: "1px solid var(--lt-border-subtle)",
        background: "var(--lt-workspace-bg)",
      }}
    >
      <div
        style={{
          minWidth: 0,
          flex: 1,
          padding: 40,
          color: "var(--lt-text-secondary)",
        }}
      >
        <strong style={{ color: "var(--lt-text)" }}>主对话保持在左侧</strong>
        <p>点击协作智能体后，详细过程在右侧独立查看。</p>
      </div>
      <SubAgentInspectorPanel
        target={{
          id: "motion-matrix:mocha",
          agentId: "Lantai_Mocha_Agent",
          agentName: "摩卡",
          role: "产品、体验、交互与原型",
          task: "检查对话页的协作层级、状态反馈和窄窗口降级。",
          status: "running",
          result: "",
          toolName: "chat_with_agent",
          toolCallId: "call-motion-mocha",
          sessionId: "motion:to:Lantai_Mocha_Agent:matrix",
        }}
        onClose={() => undefined}
        onOpenArtifacts={() => undefined}
      />
    </section>
  );
}

function ExecutionStatusOrbFixture({
  status,
  spinning = false,
}: {
  status: "running" | "completed" | "failed";
  spinning?: boolean;
}) {
  return (
    <svg
      className={traceStyles.statusOrb}
      data-status={status}
      data-spinning={spinning ? "1" : "0"}
      viewBox="0 0 20 20"
      aria-hidden="true"
    >
      <g className={traceStyles.statusOrbDots}>
        <circle cx="10" cy="2.8" r="1" />
        <circle cx="15.1" cy="4.9" r="1" />
        <circle cx="17.2" cy="10" r="1" />
        <circle cx="15.1" cy="15.1" r="1" />
        <circle cx="10" cy="17.2" r="1" />
        <circle cx="4.9" cy="15.1" r="1" />
        <circle cx="2.8" cy="10" r="1" />
        <circle cx="4.9" cy="4.9" r="1" />
      </g>
    </svg>
  );
}

function TraceFixture() {
  return (
    <MatrixSection
      title="TurnEventPreview"
      description="顶栏状态球、浮层、timeline、running/stalled 状态"
    >
      <div
        id="turn-event-preview"
        className={`${traceStyles.wrapper} ${matrixStyles.traceFixture}`}
      >
        <div className={matrixStyles.traceStatusMatrix}>
          <span className={matrixStyles.traceStatusState}>
            <button
              className={traceStyles.traceToggleButton}
              data-running="1"
              aria-label="本轮执行：运行中"
            >
              <ExecutionStatusOrbFixture status="running" spinning />
            </button>
            <strong>运行中</strong>
            <small>旋转</small>
          </span>
          <span className={matrixStyles.traceStatusState}>
            <button
              className={traceStyles.traceToggleButton}
              data-running="0"
              aria-label="本轮执行：已完成"
            >
              <ExecutionStatusOrbFixture status="completed" />
            </button>
            <strong>已完成</strong>
            <small>静止</small>
          </span>
          <span className={matrixStyles.traceStatusState}>
            <button
              className={traceStyles.traceToggleButton}
              data-running="0"
              aria-label="本轮执行：失败"
            >
              <ExecutionStatusOrbFixture status="failed" />
            </button>
            <strong>失败</strong>
            <small>静止</small>
          </span>
        </div>
        <button className={traceStyles.traceToggleButton} data-open="1">
          <ExecutionStatusOrbFixture status="running" spinning />
        </button>
        <div className={`${traceStyles.card} ${matrixStyles.traceFixtureCard}`}>
          <div className={traceStyles.cardToolbar}>
            <strong className={traceStyles.cardTitle}>运行过程预览</strong>
            <button className={traceStyles.cardCloseButton}>
              <X size={13} />
            </button>
          </div>
          <div className={traceStyles.stallNotice}>
            <Clock3 size={14} />
            <span>上一次心跳 18s 前，继续观察。</span>
          </div>
          <div className={traceStyles.timeline}>
            <div className={traceStyles.item} data-phase="test">
              <span className={traceStyles.itemMarker}>
                <Wrench className={traceStyles.itemIcon} size={13} />
              </span>
              <div className={traceStyles.itemBody}>
                <div className={traceStyles.itemHeading}>
                  <span className={traceStyles.itemTitle}>动效规则检查</span>
                  <span className={traceStyles.itemMeta}>
                    <span className={traceStyles.itemStatus}>已完成</span>
                    <span className={traceStyles.itemDuration}>0.04s</span>
                    <span className={traceStyles.itemTime}>00:01</span>
                  </span>
                </div>
                <div className={traceStyles.itemDetail}>
                  Motion guardrail passed.
                </div>
              </div>
            </div>
            <div className={traceStyles.item} data-phase="preview">
              <span className={traceStyles.itemMarker}>
                <Loader2 className={traceStyles.itemIconSpin} size={13} />
              </span>
              <div className={traceStyles.itemBody}>
                <div className={traceStyles.itemHeading}>
                  <span className={traceStyles.itemTitle}>捕获视觉矩阵</span>
                  <span className={traceStyles.itemMeta}>
                    <span className={traceStyles.itemStatus}>进行中</span>
                    <span className={traceStyles.itemTime}>00:07</span>
                  </span>
                </div>
                <div className={traceStyles.itemDetail}>
                  Capturing light, dark and reduced-motion frames.
                </div>
              </div>
            </div>
            <div className={traceStyles.item} data-phase="checkpoint">
              <span className={traceStyles.itemMarker}>
                <AlertCircle className={traceStyles.itemIcon} size={13} />
              </span>
              <div className={traceStyles.itemBody}>
                <div className={traceStyles.itemHeading}>
                  <span className={traceStyles.itemTitle}>系统级验收</span>
                  <span className={traceStyles.itemMeta}>
                    <span className={traceStyles.itemStatus}>待检查</span>
                    <span className={traceStyles.itemTime}>00:18</span>
                  </span>
                </div>
                <div className={traceStyles.itemDetail}>
                  需要 macOS 系统级 reduced motion 手测。
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </MatrixSection>
  );
}

function MotionMatrixApp() {
  const { theme, motion, fixture } = useMatrixMode();

  useEffect(() => {
    document.documentElement.classList.toggle("dark-mode", theme === "dark");
    document.body.classList.toggle("dark-mode", theme === "dark");
    document.body.dataset.motionMatrixTheme = theme;
    document.body.dataset.motionMatrixMode = motion;

    const readyTimer = window.setTimeout(
      () => {
        window.__motionMatrixReady = true;
      },
      motion === "reduce" ? 80 : 420,
    );

    return () => window.clearTimeout(readyTimer);
  }, [motion, theme]);

  return (
    <main className={matrixStyles.page}>
      <header className={matrixStyles.hero}>
        <span className={matrixStyles.eyebrow}>UI motion matrix</span>
        <h1>澜台 Apple Design 融合验证</h1>
        <p>
          Theme: <strong>{theme}</strong> · Motion: <strong>{motion}</strong> ·
          Media reduce:{" "}
          <strong>
            {window.matchMedia("(prefers-reduced-motion: reduce)").matches
              ? "true"
              : "false"}
          </strong>
        </p>
      </header>
      <div
        className={matrixStyles.matrixGrid}
        style={fixture === "inspector" ? { display: "block" } : undefined}
      >
        {fixture === "inspector" ? (
          <SubAgentInspectorFixture />
        ) : fixture === "trace" ? (
          <TraceFixture />
        ) : (
          <>
            <NavigationFixture />
            <MenuAndSelectorFixture />
            <ChatListFixture />
            <ApprovalFixture />
            <PlanFixture />
            <ToolCardsFixture />
            <TraceFixture />
          </>
        )}
      </div>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <HashRouter>
    <MotionMatrixApp />
  </HashRouter>,
);
