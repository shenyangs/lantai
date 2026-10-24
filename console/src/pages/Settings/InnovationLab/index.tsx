import { Button, Input, Modal, Segmented, Switch, message } from "antd";
import { Braces, Check, FlaskConical, LockKeyhole } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { privateEditorApi } from "../../../api/modules/privateEditor";
import type { PrivateEditorSettings } from "../../../api/types/privateEditor";
import { PageHeader } from "../../../components/PageHeader";
import { useTheme, type ThemeMode } from "../../../contexts/ThemeContext";
import {
  appearancePalettes,
  resolveInterfaceMaterial,
  defaultCustomThemeCss,
  getAppearancePalette,
  type AppearancePaletteId,
  type SidebarMaterial,
} from "../../../theme/appearance";
import AppIconPicker from "../Appearance/AppIconPicker";
import styles from "../Appearance/index.module.less";
import CustomizationStudio from "./CustomizationStudio";

const sidebarMaterialOptions: Array<{
  label: string;
  value: SidebarMaterial;
}> = [
  { label: "实色", value: "solid" },
  { label: "悬浮玻璃", value: "mist-glass" },
];

const themeModeOptions: Array<{ label: string; value: ThemeMode }> = [
  { label: "跟随系统", value: "system" },
  { label: "浅色", value: "light" },
  { label: "深色", value: "dark" },
];

export default function InnovationLabSettingsPage({
  embedded = false,
}: {
  embedded?: boolean;
}) {
  const [messageApi, contextHolder] = message.useMessage();
  const customSectionRef = useRef<HTMLElement | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [privateEditorSettings, setPrivateEditorSettings] =
    useState<PrivateEditorSettings | null>(null);
  const [savingPrivateEditor, setSavingPrivateEditor] = useState(false);
  const {
    themeMode,
    appearancePalette,
    customThemeCss,
    displaySettings,
    setAppearancePalette,
    setThemeMode,
    setCustomThemeCss,
    setDisplaySetting,
  } = useTheme();
  const [draftCss, setDraftCss] = useState(
    customThemeCss || defaultCustomThemeCss,
  );
  const visiblePaletteId =
    appearancePalette === "ink" ? "mist" : appearancePalette;
  const selectedPalette = useMemo(
    () => getAppearancePalette(visiblePaletteId),
    [visiblePaletteId],
  );

  useEffect(() => {
    let active = true;
    void privateEditorApi
      .getSettings()
      .then((settings) => {
        if (active) setPrivateEditorSettings(settings);
      })
      .catch(() => {
        if (active) messageApi.error("无法读取私人编辑实验设置");
      });
    return () => {
      active = false;
    };
  }, [messageApi]);

  const updatePrivateEditor = async (payload: {
    enabled?: boolean;
    research_consent?: boolean;
  }) => {
    setSavingPrivateEditor(true);
    try {
      const settings = await privateEditorApi.updateSettings(payload);
      setPrivateEditorSettings(settings);
      messageApi.success(
        payload.enabled === true
          ? "已开启私人编辑实验入口"
          : payload.enabled === false
            ? "已关闭私人编辑实验入口"
            : "研究同意已更新",
      );
    } catch (reason) {
      messageApi.error(
        reason instanceof Error ? reason.message : "私人编辑设置保存失败",
      );
    } finally {
      setSavingPrivateEditor(false);
    }
  };

  const openCustomEditor = () => {
    setAdvancedOpen(true);
    const focusEditor = () => {
      customSectionRef.current?.scrollIntoView?.({
        behavior: "smooth",
        block: "start",
      });
      customSectionRef.current?.querySelector("textarea")?.focus();
    };
    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(focusEditor);
    } else {
      window.setTimeout(focusEditor, 0);
    }
  };

  const applyPalette = (id: AppearancePaletteId) => {
    setAppearancePalette(id);
    if (id === "custom") openCustomEditor();
  };

  const applyCustomCss = () => {
    setCustomThemeCss(draftCss);
    messageApi.success("自定义主题已应用");
  };

  const clearCustomCss = () => {
    Modal.confirm({
      title: "清除自定义设计令牌？",
      content:
        "当前自定义样式会被移除，并切回默认石墨主题。编辑框将恢复为示例变量。",
      okText: "清除并恢复默认",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => {
        setDraftCss(defaultCustomThemeCss);
        setCustomThemeCss("");
        messageApi.success("已清除自定义设计令牌");
      },
    });
  };

  const Root = embedded ? "div" : "main";

  return (
    <Root className={embedded ? styles.embeddedPersonalization : styles.page}>
      {contextHolder}
      {!embedded ? (
        <PageHeader
          parent="设置"
          current="外观与个性化"
          className={styles.personalizationHeader}
        />
      ) : null}

      <section className={styles.section}>
        <div className={styles.sectionTitle}>
          <h2>外观</h2>
        </div>

        <div className={styles.displayPanel}>
          <div className={styles.displayRow}>
            <div>
              <strong>明暗模式</strong>
              <p>跟随 macOS 外观，或固定使用浅色、深色工作区。</p>
            </div>
            <Segmented
              size="small"
              value={themeMode}
              options={themeModeOptions}
              onChange={(value) => setThemeMode(value as ThemeMode)}
              aria-label="明暗模式"
            />
          </div>
        </div>

        <div className={`${styles.displayRow} ${styles.paletteRow}`}>
          <div>
            <strong>主题色</strong>
            <p>用于选中、焦点与关键动作。</p>
          </div>
          <div className={styles.paletteGrid}>
            {appearancePalettes
              .filter((palette) => palette.id !== "custom")
              .map((palette) => (
                <button
                  key={palette.id}
                  type="button"
                  className={`${styles.paletteCard} ${
                    visiblePaletteId === palette.id
                      ? styles.paletteCardActive
                      : ""
                  }`}
                  onClick={() => applyPalette(palette.id)}
                  aria-pressed={visiblePaletteId === palette.id}
                >
                  <span
                    className={styles.paletteSwatch}
                    style={{ background: palette.swatch }}
                    aria-hidden="true"
                  />
                  <span className={styles.paletteCopy}>
                    <strong>{palette.name}</strong>
                  </span>
                  <Check
                    className={styles.paletteCheck}
                    size={13}
                    aria-hidden="true"
                  />
                </button>
              ))}
            <button
              type="button"
              className={styles.paletteAction}
              aria-pressed={visiblePaletteId === "custom"}
              onClick={() => applyPalette("custom")}
            >
              自定义{visiblePaletteId === "custom" ? " · 已启用" : "…"}
            </button>
          </div>
        </div>

        <div className={styles.displayPanel}>
          <div className={styles.displayRow}>
            <div>
              <strong>界面材质</strong>
              <p>正文与编辑区始终保留清晰底色。</p>
            </div>
            <Segmented
              size="small"
              aria-label="界面材质"
              value={resolveInterfaceMaterial(displaySettings.sidebarMaterial)}
              options={sidebarMaterialOptions}
              onChange={(value) =>
                setDisplaySetting("sidebarMaterial", value as SidebarMaterial)
              }
            />
          </div>
        </div>
        <p className={styles.materialNote}>
          macOS 26
          及以上支持原生玻璃；其他环境使用柔和半透明效果。系统开启“减少透明度”时使用实色。
        </p>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionTitle}>
          <FlaskConical size={18} strokeWidth={1.8} aria-hidden="true" />
          <div>
            <h2>实验功能</h2>
            <p>实验开关只控制入口和作品级操作，不改变普通对话。</p>
          </div>
        </div>

        <div className={styles.displayPanel}>
          <div className={styles.displayRow}>
            <div>
              <strong>私人编辑 · 实验</strong>
              <p>
                在作品中显示“继续共作”。稿件内容仍会按当前 Provider
                设置发送给模型；开启不代表同意研究导出。
              </p>
            </div>
            <Switch
              checked={privateEditorSettings?.enabled ?? false}
              loading={!privateEditorSettings || savingPrivateEditor}
              disabled={!privateEditorSettings || savingPrivateEditor}
              onChange={(enabled) => void updatePrivateEditor({ enabled })}
              aria-label="启用私人编辑实验"
            />
          </div>
          <div className={styles.displayRow}>
            <div>
              <strong>同意导出匿名研究诊断</strong>
              <p>
                与功能开关分开，默认不同意；正文、本地路径和模型提示不会自动上报。
              </p>
            </div>
            <Switch
              checked={privateEditorSettings?.research_consent ?? false}
              loading={savingPrivateEditor}
              disabled={!privateEditorSettings?.enabled || savingPrivateEditor}
              onChange={(research_consent) =>
                void updatePrivateEditor({ research_consent })
              }
              aria-label="同意导出匿名研究诊断"
            />
          </div>
        </div>
      </section>

      <AppIconPicker />

      <details
        className={styles.advancedCustomization}
        open={advancedOpen}
        onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
      >
        <summary>
          <span>
            <strong>高级个性化</strong>
            <small>配置档、版本历史与设计令牌</small>
          </span>
        </summary>
        {advancedOpen ? (
          <>
            <CustomizationStudio />

            <section
              className={styles.section}
              ref={customSectionRef}
              data-testid="innovation-custom-section"
            >
              <div className={styles.sectionTitle}>
                <span className={styles.sectionIcon}>
                  <Braces size={17} strokeWidth={1.8} aria-hidden="true" />
                </span>
                <div>
                  <h2>高级自定义</h2>
                  <p>
                    只开放官方 --lt-* 设计令牌，不接受选择器、脚本、URL
                    或外部样式。
                  </p>
                </div>
              </div>

              <div className={styles.customPanel}>
                <div className={styles.customPanelHeader}>
                  <div className={styles.customPanelIntro}>
                    <span>主题变量</span>
                    <strong>精准调整，不改变界面结构</strong>
                    <p>仅覆盖你写入的变量；主题明暗和其他显示设置保持不变。</p>
                  </div>
                  <div className={styles.customMeta}>
                    <span className={styles.paletteStatus}>
                      <i aria-hidden="true" />
                      基于 {selectedPalette.name}
                    </span>
                    <span>
                      <LockKeyhole size={13} aria-hidden="true" />
                      仅存储在本机
                    </span>
                  </div>
                </div>

                <div className={styles.customWorkspace}>
                  <div className={styles.cssEditor}>
                    <div className={styles.editorToolbar}>
                      <div>
                        <Braces
                          size={14}
                          strokeWidth={1.8}
                          aria-hidden="true"
                        />
                        <strong>安全设计令牌</strong>
                        <span>:root</span>
                      </div>
                      <span>编辑草稿</span>
                    </div>
                    <Input.TextArea
                      aria-label="自定义主题设计令牌"
                      value={draftCss}
                      onChange={(event) => setDraftCss(event.target.value)}
                      autoSize={{ minRows: 9, maxRows: 16 }}
                      spellCheck={false}
                    />
                    <div className={styles.editorActions}>
                      <span>应用后会同步到侧栏、按钮、输入框和对话。</span>
                      <div>
                        <Button onClick={clearCustomCss}>清除自定义</Button>
                        <Button type="primary" onClick={applyCustomCss}>
                          应用变量
                        </Button>
                      </div>
                    </div>
                  </div>

                  <aside
                    className={styles.previewCard}
                    aria-label="主题效果预览"
                  >
                    <div className={styles.previewCardHeader}>
                      <div>
                        <span>主题预览</span>
                        <strong>当前界面</strong>
                      </div>
                      <span className={styles.previewReady}>
                        <Check size={12} strokeWidth={2} aria-hidden="true" />
                        就绪
                      </span>
                    </div>

                    <div className={styles.previewMiniApp} aria-hidden="true">
                      <div className={styles.previewMiniTopbar}>
                        <span className={styles.previewMiniBrand}>澜</span>
                        <span />
                        <i />
                      </div>
                      <div className={styles.previewMiniBody}>
                        <div className={styles.previewMiniAssistant}>
                          <span>沈</span>
                          <div>
                            <strong>沈行知</strong>
                            <i />
                            <i />
                          </div>
                        </div>
                        <span className={styles.previewMiniUser}>继续</span>
                        <div className={styles.previewMiniComposer}>
                          <span>输入消息</span>
                          <i />
                        </div>
                      </div>
                    </div>

                    <p>预览使用当前已应用的主题变量。</p>
                  </aside>
                </div>
              </div>
            </section>
          </>
        ) : null}
      </details>
    </Root>
  );
}
