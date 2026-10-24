import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { Drawer } from "antd";
import { useNavigate } from "react-router-dom";
import {
  ApiOutlined,
  ArrowRightOutlined,
  BgColorsOutlined,
  BugOutlined,
  CloudOutlined,
  CodeOutlined,
  DatabaseOutlined,
  DesktopOutlined,
  GlobalOutlined,
  HeartOutlined,
  InfoCircleOutlined,
  LinkOutlined,
  LineChartOutlined,
  LockOutlined,
  NotificationOutlined,
  SafetyOutlined,
  SearchOutlined,
  SlidersOutlined,
  TeamOutlined,
  ToolOutlined,
  UserOutlined,
} from "@ant-design/icons";
import type { SettingsDetailPath } from "../../../navigation/detailNavigation";
import { KEY_TO_PATH } from "../../../layouts/constants";
import { SIDEBAR_NAV_ITEMS } from "../../../layouts/sidebarPreferences";
import styles from "./index.module.less";

interface SettingCard {
  title: string;
  description: string;
  path: SettingsDetailPath;
  icon: ReactNode;
  tone: "blue" | "green" | "gold" | "red";
  group: "基础" | "连接" | "安全与权限" | "运行" | "数据" | "创新实验";
}

const settingCards: SettingCard[] = [
  {
    title: "澜台账号",
    description: "使用观猹身份登录，管理澜台云账号与已连接服务。",
    path: "/account",
    icon: <UserOutlined />,
    tone: "blue",
    group: "基础",
  },
  {
    title: "关于澜台",
    description: "查看版本信息、后台更新状态，并在更新就绪后重启安装。",
    path: "/about",
    icon: <InfoCircleOutlined />,
    tone: "blue",
    group: "基础",
  },
  {
    title: "外观与个性化",
    description: "调整主题、应用图标、左侧栏、输入框与对话过程显示。",
    path: "/appearance",
    icon: <BgColorsOutlined />,
    tone: "blue",
    group: "基础",
  },
  {
    title: "系统行为",
    description: "设置开机启动、任务防休眠、完成提醒与权限通知。",
    path: "/system-behavior",
    icon: <NotificationOutlined />,
    tone: "blue",
    group: "基础",
  },
  {
    title: "模型供应商",
    description: "配置模型来源、授权信息、默认模型和本地模型入口。",
    path: "/models",
    icon: <CloudOutlined />,
    tone: "blue",
    group: "连接",
  },
  {
    title: "全部技能库",
    description: "安装、更新可复用技能，并分发给不同智能体。",
    path: "/skill-pool",
    icon: <ToolOutlined />,
    tone: "green",
    group: "基础",
  },
  {
    title: "环境变量",
    description: "统一维护运行所需的环境变量、密钥和集成参数。",
    path: "/environments",
    icon: <GlobalOutlined />,
    tone: "green",
    group: "连接",
  },
  {
    title: "浏览器连接",
    description: "浏览任务直接在聊天中发起；这里只管理浏览器配对和登录状态。",
    path: "/browser-login",
    icon: <LinkOutlined />,
    tone: "blue",
    group: "连接",
  },
  {
    title: "服务连接",
    description: "连接常用服务；自定义 MCP 与本地协议配置放在高级区域。",
    path: "/mcp",
    icon: <ApiOutlined />,
    tone: "blue",
    group: "连接",
  },
  {
    title: "搜索增强",
    description: "启用观澜，让智能体更好地搜索和理解中文互联网。",
    path: "/search-enhancement",
    icon: <SearchOutlined />,
    tone: "green",
    group: "连接",
  },
  {
    title: "会话",
    description: "查看会话来源、运行状态和历史上下文。",
    path: "/sessions",
    icon: <TeamOutlined />,
    tone: "blue",
    group: "运行",
  },
  {
    title: "心跳",
    description: "配置后台定期检查与轻量提醒，保持任务持续跟进。",
    path: "/heartbeat",
    icon: <HeartOutlined />,
    tone: "green",
    group: "运行",
  },
  {
    title: "高级参数",
    description: "调整 Agent 循环、上下文压缩、记忆注入和请求稳定性。",
    path: "/advanced-parameters",
    icon: <SlidersOutlined />,
    tone: "gold",
    group: "运行",
  },
  {
    title: "工作区命令",
    description: "在当前智能体工作区安全运行一次性命令，仅供测试与排障。",
    path: "/terminal",
    icon: <CodeOutlined />,
    tone: "gold",
    group: "运行",
  },
  {
    title: "运行统计",
    description: "统一查看模型用量、工具健康、会话趋势和本地空间。",
    path: "/agent-stats",
    icon: <LineChartOutlined />,
    tone: "blue",
    group: "数据",
  },
  {
    title: "备份归档",
    description: "备份、导入和恢复工作区与关键配置。",
    path: "/backups",
    icon: <DatabaseOutlined />,
    tone: "green",
    group: "数据",
  },
  {
    title: "权限",
    description:
      "设置后台与未指定场景的默认执行方式；聊天权限仍由各对话单独保存。",
    path: "/permissions",
    icon: <SafetyOutlined />,
    tone: "gold",
    group: "安全与权限",
  },
  {
    title: "安全策略",
    description: "管理附加风险扫描、命令规则、文件访问和连接安全策略。",
    path: "/security",
    icon: <LockOutlined />,
    tone: "gold",
    group: "安全与权限",
  },
  {
    title: "电脑权限",
    description: "申请并说明辅助功能、磁盘、屏幕和自动化权限。",
    path: "/computer-permissions",
    icon: <DesktopOutlined />,
    tone: "gold",
    group: "安全与权限",
  },
  {
    title: "运行调试",
    description: "查看运行日志与关键事件，辅助定位异常。",
    path: "/debug",
    icon: <BugOutlined />,
    tone: "red",
    group: "数据",
  },
];

export default function SettingsHomePage() {
  const navigate = useNavigate();
  const [allFunctionsOpen, setAllFunctionsOpen] = useState(false);
  const groupedCards = useMemo(
    () =>
      ["基础", "连接", "安全与权限", "运行", "数据", "创新实验"]
        .map((group) => ({
          group,
          cards: settingCards.filter((card) => card.group === group),
        }))
        .filter((entry) => entry.cards.length > 0),
    [],
  );
  const [activeGroup, setActiveGroup] = useState(
    groupedCards[0]?.group ?? "基础",
  );
  return (
    <main className={styles.settingsHome}>
      <section className={styles.settingsTop}>
        <div>
          <h1>设置</h1>
          <p>模型、权限、数据和运行设置都从这里进入。</p>
        </div>
        <button
          type="button"
          className={styles.allFunctionsButton}
          onClick={() => setAllFunctionsOpen(true)}
        >
          打开全部功能
        </button>
      </section>

      <div className={styles.bodyGrid}>
        <nav className={styles.indexRail} aria-label="设置目录">
          <span className={styles.indexTitle}>设置索引</span>
          {groupedCards.map(({ group, cards }) => (
            <a
              key={group}
              href={`#settings-group-${group}`}
              aria-current={activeGroup === group ? "location" : undefined}
              className={`${styles.indexItem} ${
                activeGroup === group ? styles.indexItemActive : ""
              }`}
              onClick={(event) => {
                event.preventDefault();
                setActiveGroup(group);
                document
                  .getElementById(`settings-group-${group}`)
                  ?.scrollIntoView({ behavior: "smooth", block: "start" });
              }}
            >
              <span>{group}</span>
              <em>{cards.length}</em>
            </a>
          ))}
        </nav>

        <section className={styles.cardSections}>
          {groupedCards.map(({ group, cards }) => (
            <div
              key={group}
              id={`settings-group-${group}`}
              className={styles.cardSection}
            >
              <div className={styles.sectionHeader}>
                <h2>{group}</h2>
              </div>
              <div className={styles.cardGrid}>
                {cards.map((card) => (
                  <button
                    key={card.path}
                    type="button"
                    className={`${styles.settingCard} ${styles[card.tone]}`}
                    onClick={() => navigate(card.path)}
                  >
                    <span className={styles.cardIcon}>{card.icon}</span>
                    <span className={styles.cardCopy}>
                      <strong>{card.title}</strong>
                      <em>{card.description}</em>
                    </span>
                    <ArrowRightOutlined className={styles.cardArrow} />
                  </button>
                ))}
              </div>
            </div>
          ))}
        </section>
      </div>
      <Drawer
        title="全部功能"
        open={allFunctionsOpen}
        onClose={() => setAllFunctionsOpen(false)}
        width={420}
        destroyOnClose
      >
        <p className={styles.allFunctionsIntro}>
          侧边栏隐藏的入口仍然可以从这里打开。隐藏只改变常驻导航，不会移除功能。
        </p>
        <div className={styles.allFunctionsList}>
          {SIDEBAR_NAV_ITEMS.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => {
                setAllFunctionsOpen(false);
                navigate(KEY_TO_PATH[item.key] ?? `/${item.key}`);
              }}
            >
              <span>
                <strong>{item.label}</strong>
                <em>{item.description}</em>
              </span>
              <ArrowRightOutlined aria-hidden="true" />
            </button>
          ))}
        </div>
      </Drawer>
    </main>
  );
}
