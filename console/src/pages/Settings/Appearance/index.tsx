import { AppstoreOutlined } from "@ant-design/icons";
import { Button, Segmented, Switch, message } from "antd";
import { useState } from "react";
import {
  defaultDisplaySettings,
  type ComposerSize,
  type PersonaMode,
  type WelcomeCulturalCalendar,
  type WelcomeTone,
} from "../../../theme/appearance";
import { useTheme } from "../../../contexts/ThemeContext";
import { PageHeader } from "../../../components/PageHeader";
import {
  loadSidebarVisibleKeys,
  notifySidebarPreferencesChanged,
  resetSidebarVisibleKeys,
  saveSidebarVisibleKeys,
  SIDEBAR_NAV_ITEMS,
} from "../../../layouts/sidebarPreferences";
import styles from "./index.module.less";
import InnovationLabSettingsPage from "../InnovationLab";

const composerOptions: Array<{ label: string; value: ComposerSize }> = [
  { label: "紧凑", value: "compact" },
  { label: "舒展", value: "comfortable" },
  { label: "沉浸", value: "spacious" },
];

const personaModeOptions: Array<{ label: string; value: PersonaMode }> = [
  { label: "鲜明", value: "full" },
  { label: "自然", value: "balanced" },
  { label: "克制", value: "quiet" },
  { label: "纯任务", value: "off" },
];

const culturalCalendarOptions: Array<{
  label: string;
  value: WelcomeCulturalCalendar;
}> = [
  { label: "不使用", value: "none" },
  { label: "中国节日", value: "china" },
  { label: "全球节日", value: "global" },
  { label: "全部", value: "all" },
];

const welcomeToneOptions: Array<{ label: string; value: WelcomeTone }> = [
  { label: "自然", value: "natural" },
  { label: "短一点", value: "concise" },
  { label: "克制", value: "restrained" },
];

type DisplayToggleKey =
  | "showInnerVoice"
  | "showThinking"
  | "showToolCalls"
  | "showTokenUsage";

const displayRows: Array<{
  key: DisplayToggleKey;
  title: string;
  description: string;
}> = [
  {
    key: "showInnerVoice",
    title: "蛐蛐",
    description: "让沈行知偶尔在正式回答之外，补充一句更自然、更有个性的想法。",
  },
  {
    key: "showThinking",
    title: "显示思考过程",
    description: "展示沈行知形成回答时提供的分析摘要和当前进度。",
  },
  {
    key: "showToolCalls",
    title: "显示工作过程",
    description: "展示沈行知查资料、读取文件或使用其他能力时的进度和结果。",
  },
  {
    key: "showTokenUsage",
    title: "显示模型用量",
    description: "在回答下方显示本次使用的 Token 数量和预估费用。",
  },
] as const;

export default function AppearanceSettingsPage() {
  const [messageApi, contextHolder] = message.useMessage();
  const { displaySettings, setDisplaySetting, setDisplaySettings } = useTheme();
  const [sidebarVisibleKeys, setSidebarVisibleKeys] = useState(
    loadSidebarVisibleKeys,
  );

  const resetDisplay = () => {
    setDisplaySettings({
      ...displaySettings,
      showInnerVoice: defaultDisplaySettings.showInnerVoice,
      personaMode: defaultDisplaySettings.personaMode,
      showThinking: defaultDisplaySettings.showThinking,
      showToolCalls: defaultDisplaySettings.showToolCalls,
      showTokenUsage: defaultDisplaySettings.showTokenUsage,
      dynamicWelcome: defaultDisplaySettings.dynamicWelcome,
      aiWelcome: defaultDisplaySettings.aiWelcome,
      welcomeCulturalCalendar: defaultDisplaySettings.welcomeCulturalCalendar,
      welcomeTone: defaultDisplaySettings.welcomeTone,
      composerSize: defaultDisplaySettings.composerSize,
    });
    messageApi.success("对话显示已恢复默认");
  };

  const setSidebarItemVisible = (key: string, checked: boolean) => {
    const next = new Set(sidebarVisibleKeys);
    if (checked) {
      next.add(key);
    } else {
      next.delete(key);
    }
    const saved = saveSidebarVisibleKeys(next);
    setSidebarVisibleKeys(saved);
    notifySidebarPreferencesChanged();
  };

  const resetSidebarVisibility = () => {
    const next = resetSidebarVisibleKeys();
    setSidebarVisibleKeys(next);
    notifySidebarPreferencesChanged();
    messageApi.success("左侧栏已恢复默认");
  };

  return (
    <main className={styles.page}>
      {contextHolder}
      <PageHeader
        parent="设置"
        current="外观与个性化"
        description="调整主题、应用图标、左侧栏、输入框和聊天内容的显示方式。"
      />

      <aside
        className={styles.effectNotice}
        data-testid="appearance-effect-notice"
      >
        <span>生效方式</span>
        <strong>这些外观与显示调整会立即保存并应用到本机界面。</strong>
      </aside>

      <section
        className={styles.section}
        data-testid="appearance-sidebar-section"
      >
        <div className={styles.sectionTitle}>
          <AppstoreOutlined />
          <div>
            <h2>左侧栏</h2>
            <p>选择哪些功能入口常驻侧边栏；隐藏后仍可从设置或直接地址进入。</p>
          </div>
        </div>

        <div className={styles.displayPanel}>
          {SIDEBAR_NAV_ITEMS.map((item) => (
            <div className={styles.displayRow} key={item.key}>
              <div>
                <strong>{item.label}</strong>
                <p>
                  {item.group} · {item.description}
                </p>
                {item.pinned ? <em>核心入口会始终显示。</em> : null}
              </div>
              <Switch
                checked={sidebarVisibleKeys.has(item.key)}
                disabled={item.pinned}
                aria-label={
                  item.pinned
                    ? `${item.label}固定显示在左侧栏`
                    : `${
                        sidebarVisibleKeys.has(item.key) ? "隐藏" : "显示"
                      }${item.label}入口`
                }
                onChange={(checked) => setSidebarItemVisible(item.key, checked)}
              />
            </div>
          ))}

          <div className={styles.panelActions}>
            <Button onClick={resetSidebarVisibility}>恢复默认侧栏</Button>
          </div>
        </div>
      </section>

      <section
        className={styles.section}
        data-testid="conversation-display-section"
      >
        <div className={styles.sectionTitle}>
          <span className={styles.sectionIcon}>Aa</span>
          <div>
            <h2>对话显示</h2>
            <p>选择沈行知的表达方式，以及对话中需要显示的内容。</p>
          </div>
        </div>

        <div className={styles.displayPanel}>
          <div className={styles.displayRow}>
            <div>
              <strong>沈行知的表达风格</strong>
              <p>调整沈行知说话时的个性与主动程度，不影响完成任务的能力。</p>
            </div>
            <Segmented
              value={displaySettings.personaMode}
              options={personaModeOptions}
              onChange={(value) =>
                setDisplaySetting("personaMode", value as PersonaMode)
              }
            />
          </div>

          <div className={styles.displayRow}>
            <div>
              <strong>动态开场</strong>
              <p>在新聊天空白页按时间和日期更换开场；同一次进入保持不变。</p>
            </div>
            <Switch
              checked={displaySettings.dynamicWelcome}
              aria-label="动态开场"
              onChange={(checked) =>
                setDisplaySetting("dynamicWelcome", checked)
              }
            />
          </div>

          <div className={styles.displayRow}>
            <div>
              <strong>AI 个性化开场</strong>
              <p>
                空闲时提前准备候选，只在下次进入空白页时使用；关闭后仅使用固定文案。
              </p>
            </div>
            <Switch
              checked={displaySettings.aiWelcome}
              disabled={!displaySettings.dynamicWelcome}
              aria-label="AI 个性化开场"
              onChange={(checked) => setDisplaySetting("aiWelcome", checked)}
            />
          </div>

          <div className={styles.displayRow}>
            <div>
              <strong>节日问候</strong>
              <p>只使用你明确选择的文化日历，不根据语言或位置推断。</p>
            </div>
            <Segmented
              aria-label="节日问候"
              value={displaySettings.welcomeCulturalCalendar}
              options={culturalCalendarOptions}
              onChange={(value) =>
                setDisplaySetting(
                  "welcomeCulturalCalendar",
                  value as WelcomeCulturalCalendar,
                )
              }
            />
          </div>

          <div className={styles.displayRow}>
            <div>
              <strong>开场语气</strong>
              <p>控制 AI 候选的表达方式；固定兜底始终保持简短和通用。</p>
            </div>
            <Segmented
              aria-label="开场语气"
              value={displaySettings.welcomeTone}
              options={welcomeToneOptions}
              onChange={(value) =>
                setDisplaySetting("welcomeTone", value as WelcomeTone)
              }
            />
          </div>

          {displayRows.map((row) => (
            <div className={styles.displayRow} key={row.key}>
              <div>
                <strong>{row.title}</strong>
                <p>{row.description}</p>
              </div>
              <Switch
                checked={displaySettings[row.key]}
                aria-label={row.title}
                onChange={(checked) => setDisplaySetting(row.key, checked)}
              />
            </div>
          ))}

          <div className={styles.displayRow}>
            <div>
              <strong>输入框形态</strong>
              <p>调整输入框在页面中占用的空间。</p>
            </div>
            <Segmented
              value={displaySettings.composerSize}
              options={composerOptions}
              onChange={(value) =>
                setDisplaySetting("composerSize", value as ComposerSize)
              }
            />
          </div>

          <div className={styles.panelActions}>
            <Button onClick={resetDisplay}>恢复默认显示</Button>
          </div>
        </div>
      </section>

      <InnovationLabSettingsPage embedded />
    </main>
  );
}
