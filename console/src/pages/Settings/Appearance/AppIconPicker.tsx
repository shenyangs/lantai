import { Button, Spin, message } from "antd";
import { AppWindow, Check, Copy, ImagePlus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import styles from "./AppIconPicker.module.less";

const ERROR_MESSAGES: Record<string, string> = {
  custom_icon_missing: "尚未导入自定义图标",
  dock_unavailable: "当前系统不支持即时更换 Dock 图标",
  icon_import_failed: "图标导入失败，请换一张图重试",
  icon_invalid: "无法读取这张图像",
  icon_missing: "这枚内置图标丢失，请重新安装澜台",
  icon_not_square: "请导入正方形图像",
  icon_too_large: "图像不能超过 20 MB",
  icon_too_small: "为避免放大变糊，图像尺寸至少需要 1024 × 1024 px",
  invalid_icon_id: "这枚图标不存在",
  preference_write_failed: "图标已更换，但无法保存选择",
  set_failed: "Dock 图标更换失败",
  untrusted_ipc_sender: "安全校验未通过",
};

function getErrorMessage(error?: string) {
  return ERROR_MESSAGES[String(error || "")] || "操作失败，请重试";
}

function IconOption({
  icon,
  selected,
  disabled,
  onSelect,
}: {
  icon: DesktopAppIconOption;
  selected: boolean;
  disabled: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <button
      type="button"
      className={`${styles.iconOption} ${selected ? styles.selected : ""}`}
      onClick={() => onSelect(icon.id)}
      aria-label={`选择${icon.name}应用图标`}
      aria-pressed={selected}
      disabled={disabled}
    >
      <span className={styles.iconFrame}>
        <img src={icon.previewUrl} alt="" draggable={false} />
        {selected ? (
          <span className={styles.selectedMark} aria-hidden="true">
            <Check size={13} strokeWidth={2.6} />
          </span>
        ) : null}
      </span>
      <span className={styles.iconName}>{icon.name}</span>
    </button>
  );
}

export default function AppIconPicker() {
  const [messageApi, contextHolder] = message.useMessage();
  const [state, setState] = useState<DesktopAppIconState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const api = window.electronAPI;

  const loadState = useCallback(async () => {
    if (!api?.getAppIcon) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const next = await api.getAppIcon();
      setState(next);
      if (!next.ok) messageApi.error(getErrorMessage(next.error));
    } catch {
      messageApi.error("无法读取应用图标设置");
    } finally {
      setLoading(false);
    }
  }, [api, messageApi]);

  useEffect(() => {
    void loadState();
  }, [loadState]);

  const groups = useMemo(() => {
    const icons = state?.builtinIcons || [];
    return {
      brush: icons.filter((icon) => icon.group === "brush"),
      signature: icons.filter((icon) => icon.group === "signature"),
    };
  }, [state]);

  const selectIcon = async (id: string) => {
    if (!api?.selectAppIcon || busyId) return;
    setBusyId(id);
    try {
      const next = await api.selectAppIcon(id);
      setState(next);
      if (next.ok) {
        messageApi.success("应用图标已更换");
      } else {
        messageApi.error(getErrorMessage(next.error));
      }
    } catch {
      messageApi.error("应用图标更换失败");
    } finally {
      setBusyId("");
    }
  };

  const importCustomIcon = async () => {
    if (!api?.importCustomAppIcon || busyId) return;
    setBusyId("custom");
    try {
      const next = await api.importCustomAppIcon();
      setState(next);
      if (next.canceled) return;
      if (next.ok) {
        messageApi.success("自定义图标已导入并应用");
      } else {
        messageApi.error(getErrorMessage(next.error));
      }
    } catch {
      messageApi.error("自定义图标导入失败");
    } finally {
      setBusyId("");
    }
  };

  const copyDesignSpec = async () => {
    if (!state?.designSpec) return;
    try {
      await navigator.clipboard.writeText(state.designSpec);
      messageApi.success("图标设计规范已复制");
    } catch {
      messageApi.error("复制失败");
    }
  };

  return (
    <section className={styles.section}>
      {contextHolder}
      <div className={styles.sectionTitle}>
        <span className={styles.sectionIcon}>
          <AppWindow size={17} strokeWidth={1.8} aria-hidden="true" />
        </span>
        <div>
          <h2>应用图标</h2>
          <p>
            黑底白字的毛笔图标为默认选择；也可切换精选方案或导入自己的设计。
          </p>
        </div>
      </div>

      <div className={styles.panel} aria-busy={loading || Boolean(busyId)}>
        <div className={styles.toolbar}>
          <div>
            <strong>Dock 与应用切换器</strong>
            <span>选择后立即生效，并在下次启动时继续使用。</span>
          </div>
          <div className={styles.toolbarActions}>
            <Button
              icon={<Copy size={15} />}
              onClick={() => void copyDesignSpec()}
              disabled={!state?.designSpec}
            >
              复制设计规范
            </Button>
            <Button
              type="primary"
              icon={<ImagePlus size={15} />}
              loading={busyId === "custom"}
              onClick={() => void importCustomIcon()}
              disabled={!api?.importCustomAppIcon}
            >
              导入自定义图标
            </Button>
          </div>
        </div>

        {loading ? (
          <div className={styles.loadingState}>
            <Spin size="small" />
            <span>正在读取图标……</span>
          </div>
        ) : !state ? (
          <div className={styles.emptyState}>
            <p>应用图标只能在澜台 macOS 桌面端中更换。</p>
            {api?.getAppIcon ? (
              <Button
                icon={<RefreshCw size={15} />}
                onClick={() => void loadState()}
              >
                重试
              </Button>
            ) : null}
          </div>
        ) : (
          <div className={styles.catalog}>
            {!state.supported ? (
              <p className={styles.supportNote}>
                当前系统可预览图标，但只有 macOS 支持即时更换 Dock 图标。
              </p>
            ) : null}

            <div className={styles.iconGroup}>
              <div className={styles.groupHeading}>
                <strong>默认</strong>
                <span>当前正式图标</span>
              </div>
              <div className={styles.iconGrid}>
                <IconOption
                  icon={state.defaultIcon}
                  selected={state.selectedId === "default"}
                  disabled={Boolean(busyId) || !state.supported}
                  onSelect={(id) => void selectIcon(id)}
                />
                {state.customIcon.available ? (
                  <IconOption
                    icon={state.customIcon}
                    selected={state.selectedId === "custom"}
                    disabled={Boolean(busyId) || !state.supported}
                    onSelect={(id) => void selectIcon(id)}
                  />
                ) : null}
              </div>
            </div>

            <div className={styles.iconGroup}>
              <div className={styles.groupHeading}>
                <strong>澜字精选</strong>
                <span>保留毛笔字，只留下克制、耐看的材质</span>
              </div>
              <div className={styles.iconGrid}>
                {groups.brush.map((icon) => (
                  <IconOption
                    key={icon.id}
                    icon={icon}
                    selected={state.selectedId === icon.id}
                    disabled={Boolean(busyId) || !state.supported}
                    onSelect={(id) => void selectIcon(id)}
                  />
                ))}
              </div>
            </div>

            <div className={styles.iconGroup}>
              <div className={styles.groupHeading}>
                <strong>澜台意象</strong>
                <span>围绕水面、平台、墨迹与留白的精选方案</span>
              </div>
              <div className={styles.iconGrid}>
                {groups.signature.map((icon) => (
                  <IconOption
                    key={icon.id}
                    icon={icon}
                    selected={state.selectedId === icon.id}
                    disabled={Boolean(busyId) || !state.supported}
                    onSelect={(id) => void selectIcon(id)}
                  />
                ))}
              </div>
            </div>
          </div>
        )}
        <span className={styles.liveStatus} aria-live="polite">
          {busyId ? "正在更换应用图标" : ""}
        </span>
      </div>
    </section>
  );
}
