import { Suspense, useMemo } from "react";
import { Layout } from "antd";
import { Routes, Route, useLocation, Navigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import Sidebar from "../Sidebar";
import Header from "../Header";
import { useWindowMaterial } from "../../theme/useWindowMaterial";
import ConsolePollService from "../../components/ConsolePollService";
import SystemBehaviorService from "../../components/SystemBehaviorService";
import DocxDoubleWriteService from "../../components/DocxDoubleWriteService";
import MarkdownHandoffService from "../../components/MarkdownHandoffService";
import OfficeWorkbenchNavigationService from "../../components/OfficeWorkbenchNavigationService";
import DailyBriefNotificationService from "../../components/DailyBriefNotificationService";
import ReminderNotificationService from "../../components/ReminderNotificationService";
import { ChunkErrorBoundary } from "../../components/ChunkErrorBoundary";
import { lazyImportWithRetry, lazyWithRetry } from "../../utils/lazyWithRetry";
import { usePlugins } from "../../plugins/PluginContext";
import styles from "../index.module.less";
import AppState from "../../components/AppState";

// Chat is eagerly loaded (default landing page)
import Chat from "../../pages/Chat";

// All other pages are lazily loaded with automatic retry on chunk failure
const ChannelsPage = lazyImportWithRetry("../../pages/Control/Channels");
const WorksPage = lazyImportWithRetry("../../pages/Works");
const RemindersPage = lazyImportWithRetry("../../pages/Control/Reminders");
const SessionsPage = lazyImportWithRetry("../../pages/Control/Sessions");
const CronJobsPage = lazyImportWithRetry("../../pages/Control/CronJobs");
const HeartbeatPage = lazyImportWithRetry("../../pages/Control/Heartbeat");
const SkillsPage = lazyWithRetry(
  () => import("../../pages/Agent/Skills"),
  "../../pages/Agent/Skills",
);
const SkillPoolPage = lazyImportWithRetry("../../pages/Settings/SkillPool");
const ToolsPage = lazyImportWithRetry("../../pages/Agent/Tools");
const CapabilitiesPage = lazyImportWithRetry("../../pages/Agent/Capabilities");
const WorkspacePage = lazyImportWithRetry("../../pages/Agent/Workspace");
const TerminalPage = lazyImportWithRetry("../../pages/Terminal");
const MemoryPage = lazyImportWithRetry("../../pages/Agent/Memory");
const MCPPage = lazyImportWithRetry("../../pages/Agent/MCP");
const ModelsPage = lazyImportWithRetry("../../pages/Settings/Models");
const AccountPage = lazyImportWithRetry("../../pages/Settings/Account");
const EnvironmentsPage = lazyImportWithRetry(
  "../../pages/Settings/Environments",
);
const PermissionsPage = lazyImportWithRetry("../../pages/Settings/Permissions");
const SecurityPage = lazyImportWithRetry("../../pages/Settings/Security");
const AgentStatsPage = lazyImportWithRetry("../../pages/Settings/AgentStats");
const AppearancePage = lazyImportWithRetry("../../pages/Settings/Appearance");
const InnovationLabPage = lazyImportWithRetry(
  "../../pages/Settings/InnovationLab",
);
const BrowserLoginPage = lazyImportWithRetry(
  "../../pages/Settings/BrowserLogin",
);
const SearchEnhancementPage = lazyImportWithRetry(
  "../../pages/Settings/SearchEnhancement",
);
const OfficeDocumentsPage = lazyImportWithRetry(
  "../../pages/Settings/OfficeDocuments",
);
const ComputerPermissionsPage = lazyImportWithRetry(
  "../../pages/Settings/ComputerPermissions",
);
const AgentsPage = lazyImportWithRetry("../../pages/Settings/Agents");
const DebugPage = lazyImportWithRetry("../../pages/Settings/Debug");
const BackupsPage = lazyImportWithRetry("../../pages/Settings/Backups");
const SettingsHomePage = lazyImportWithRetry("../../pages/Settings/Home");
const AboutPage = lazyImportWithRetry("../../pages/Settings/About");
const AdvancedParametersPage = lazyImportWithRetry(
  "../../pages/Settings/AdvancedParameters",
);
const SystemBehaviorPage = lazyImportWithRetry(
  "../../pages/Settings/SystemBehavior",
);
const OfficeWorkbenchPage = lazyImportWithRetry("../../pages/OfficeWorkbench");
const DailyBriefPage = lazyImportWithRetry("../../pages/DailyBrief");

const { Content } = Layout;

function BrowserWorkbenchRedirect() {
  const location = useLocation();
  const taskId = new URLSearchParams(location.search).get("task") || "";
  const target = taskId
    ? `/chat?browser_task=${encodeURIComponent(taskId)}`
    : "/chat";
  return <Navigate to={target} replace />;
}

const pathToKey: Record<string, string> = {
  "/chat": "chat",
  "/works": "works",
  "/reminders": "reminders",
  "/today": "reminders",
  "/channels": "channels",
  "/sessions": "settings",
  "/cron-jobs": "cron-jobs",
  "/heartbeat": "settings",
  "/skills": "skills",
  "/tools": "tools",
  "/capabilities": "capabilities",
  "/mcp": "settings",
  "/search-enhancement": "settings",
  "/workspace": "workspace",
  "/browser-workbench": "chat",
  "/office-workbench": "chat",
  "/daily-brief": "daily-brief",
  "/terminal": "settings",
  "/memory": "memory",
  "/settings": "settings",
  "/about": "settings",
  "/agents": "agents",
  "/models": "settings",
  "/account": "settings",
  "/environments": "settings",
  "/agent-config": "settings",
  "/advanced-parameters": "settings",
  "/permissions": "settings",
  "/security": "settings",
  "/skill-pool": "settings",
  "/token-usage": "settings",
  "/agent-stats": "settings",
  "/appearance": "settings",
  "/innovation-lab": "settings",
  "/browser-login": "settings",
  "/office-documents": "settings",
  "/computer-permissions": "settings",
  "/debug": "settings",
  "/backups": "settings",
  "/system-behavior": "settings",
};

export default function MainLayout() {
  useWindowMaterial();
  const { t } = useTranslation();
  const location = useLocation();
  const currentPath = location.pathname;
  const { pluginRoutes } = usePlugins();
  const isChatRoute = currentPath === "/" || currentPath.startsWith("/chat");
  const isOfficeWorkbenchRoute = currentPath === "/office-workbench";
  const isDailyBriefRoute = currentPath === "/daily-brief";

  // Resolve selected key: check static routes first, then plugin routes
  let selectedKey = pathToKey[currentPath] || "";
  if (!selectedKey) {
    const matchedPlugin = pluginRoutes.find(
      (route) => currentPath === route.path,
    );
    selectedKey = matchedPlugin
      ? matchedPlugin.path.replace(/^\//, "")
      : "chat";
  }

  const contentClassName = useMemo(
    () =>
      [
        "page-content",
        styles.workspaceCanvas,
        isChatRoute ? styles.chatWorkspaceCanvas : "",
        isOfficeWorkbenchRoute || isDailyBriefRoute
          ? styles.officeWorkbenchCanvas
          : "",
      ]
        .filter(Boolean)
        .join(" "),
    [isChatRoute, isDailyBriefRoute, isOfficeWorkbenchRoute],
  );

  return (
    <Layout className={styles.mainLayout}>
      <a className={styles.skipLink} href="#main-content">
        跳到主要内容
      </a>
      <Header />
      <Layout className={styles.shellBody}>
        <Sidebar selectedKey={selectedKey} />
        <Content
          id="main-content"
          tabIndex={-1}
          className={`page-container ${styles.pageContainer}`}
        >
          <ConsolePollService />
          <SystemBehaviorService />
          <DocxDoubleWriteService />
          <MarkdownHandoffService />
          <OfficeWorkbenchNavigationService />
          <DailyBriefNotificationService />
          <ReminderNotificationService />
          <div className={contentClassName}>
            <ChunkErrorBoundary resetKey={currentPath}>
              <Suspense
                fallback={
                  <div className={styles.routeLoading}>
                    <AppState
                      tone="loading"
                      title={t("common.loading")}
                      description={t("common.loadingPage")}
                      compact
                    />
                  </div>
                }
              >
                <Routes>
                  <Route path="/" element={<Navigate to="/chat" replace />} />
                  <Route path="/chat/*" element={<Chat />} />
                  <Route path="/daily-brief" element={<DailyBriefPage />} />
                  <Route
                    path="/office-workbench"
                    element={<OfficeWorkbenchPage />}
                  />
                  <Route path="/works" element={<WorksPage />} />
                  <Route
                    path="/presentations"
                    element={<Navigate to="/chat" replace />}
                  />
                  <Route
                    path="/task-center/*"
                    element={<Navigate to="/chat" replace />}
                  />
                  <Route path="/reminders" element={<RemindersPage />} />
                  <Route path="/today" element={<RemindersPage />} />
                  <Route path="/channels" element={<ChannelsPage />} />
                  <Route path="/sessions" element={<SessionsPage />} />
                  <Route path="/cron-jobs" element={<CronJobsPage />} />
                  <Route path="/heartbeat" element={<HeartbeatPage />} />
                  <Route path="/skills" element={<SkillsPage />} />
                  <Route path="/skill-pool" element={<SkillPoolPage />} />
                  <Route path="/tools" element={<ToolsPage />} />
                  <Route path="/capabilities" element={<CapabilitiesPage />} />
                  <Route path="/mcp" element={<MCPPage />} />
                  <Route
                    path="/search-enhancement"
                    element={<SearchEnhancementPage />}
                  />
                  <Route
                    path="/acp"
                    element={<Navigate to="/settings" replace />}
                  />
                  <Route
                    path="/ACP"
                    element={<Navigate to="/settings" replace />}
                  />
                  <Route path="/workspace" element={<WorkspacePage />} />
                  <Route
                    path="/browser-workbench"
                    element={<BrowserWorkbenchRedirect />}
                  />
                  <Route path="/terminal" element={<TerminalPage />} />
                  <Route
                    path="/coding"
                    element={<Navigate to="/chat" replace />}
                  />
                  <Route path="/memory" element={<MemoryPage />} />
                  <Route path="/settings" element={<SettingsHomePage />} />
                  <Route path="/about" element={<AboutPage />} />
                  <Route path="/agents" element={<AgentsPage />} />
                  <Route path="/models" element={<ModelsPage />} />
                  <Route path="/account" element={<AccountPage />} />
                  <Route path="/environments" element={<EnvironmentsPage />} />
                  <Route
                    path="/agent-config"
                    element={<Navigate to="/advanced-parameters" replace />}
                  />
                  <Route
                    path="/advanced-parameters"
                    element={<AdvancedParametersPage />}
                  />
                  <Route path="/permissions" element={<PermissionsPage />} />
                  <Route path="/security" element={<SecurityPage />} />
                  <Route
                    path="/token-usage"
                    element={
                      <Navigate to="/agent-stats?section=usage" replace />
                    }
                  />
                  <Route path="/agent-stats" element={<AgentStatsPage />} />
                  <Route path="/appearance" element={<AppearancePage />} />
                  <Route
                    path="/innovation-lab"
                    element={<InnovationLabPage />}
                  />
                  <Route path="/browser-login" element={<BrowserLoginPage />} />
                  <Route
                    path="/office-documents"
                    element={<OfficeDocumentsPage />}
                  />
                  <Route
                    path="/computer-permissions"
                    element={<ComputerPermissionsPage />}
                  />
                  <Route path="/debug" element={<DebugPage />} />
                  <Route path="/backups" element={<BackupsPage />} />
                  <Route
                    path="/system-behavior"
                    element={<SystemBehaviorPage />}
                  />
                  {/* Plugin routes — dynamically injected at runtime */}
                  {pluginRoutes.map((route) => (
                    <Route
                      key={route.path}
                      path={route.path}
                      element={<route.component />}
                    />
                  ))}
                </Routes>
              </Suspense>
            </ChunkErrorBoundary>
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}
