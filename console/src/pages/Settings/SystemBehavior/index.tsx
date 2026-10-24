import {
  BellOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  DesktopOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { Alert, App, Button, Select, Spin, Switch } from "antd";
import { useCallback, useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import {
  settingsApi,
  type SystemBehaviorSettings,
  type TurnCompletionNotificationMode,
} from "../../../api/modules/settings";
import styles from "./index.module.less";

const DEFAULT_SETTINGS: SystemBehaviorSettings = {
  launch_at_login: false,
  keep_awake_while_running: false,
  turn_completion_notification: "when_unfocused",
  permission_notification: true,
};

export default function SystemBehaviorPage() {
  const { message, modal } = App.useApp();
  const [settings, setSettings] =
    useState<SystemBehaviorSettings>(DEFAULT_SETTINGS);
  const [loading, setLoading] = useState(true);
  const [savingKey, setSavingKey] = useState<keyof SystemBehaviorSettings>();
  const [saveState, setSaveState] = useState<
    "idle" | "saving" | "saved" | "error"
  >("idle");
  const [launchServices, setLaunchServices] =
    useState<DesktopLaunchServicesState | null>(null);
  const [launchServicesBusy, setLaunchServicesBusy] = useState(false);
  const isDesktop = Boolean(window.electronAPI?.isDesktop);

  useEffect(() => {
    let active = true;
    settingsApi
      .getSystemBehaviorSettings()
      .then(async (next) => {
        if (!active) return;
        setSettings(next);
        await window.electronAPI?.updateSystemBehavior?.(next);
      })
      .catch((error) => {
        if (active) {
          message.error(
            error instanceof Error ? error.message : "读取系统设置失败",
          );
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [message]);

  const inspectLaunchServices = useCallback(async () => {
    if (!window.electronAPI?.getLaunchServicesState) return;
    setLaunchServicesBusy(true);
    try {
      const state = await window.electronAPI.getLaunchServicesState();
      setLaunchServices(state);
      if (!state.ok) throw new Error(state.error || "检查历史文件关联失败");
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "检查历史文件关联失败",
      );
    } finally {
      setLaunchServicesBusy(false);
    }
  }, [message]);

  useEffect(() => {
    if (isDesktop) void inspectLaunchServices();
  }, [inspectLaunchServices, isDesktop]);

  const cleanLaunchServices = async (removeCopies: boolean) => {
    if (!window.electronAPI?.cleanLaunchServices) return;
    setLaunchServicesBusy(true);
    try {
      const state = await window.electronAPI.cleanLaunchServices({
        remove_copies: removeCopies,
      });
      setLaunchServices(state);
      if (!state.ok) throw new Error(state.error || "清理历史文件关联失败");
      const stillDirty =
        (state.stale_registrations?.length || 0) > 0 ||
        (state.disk_copies?.length || 0) > 0;
      if (stillDirty) {
        message.warning("清理后仍发现旧副本，请查看列表后重试。");
      } else {
        message.success(
          removeCopies
            ? `已删除 ${state.removed_copy_count || 0} 个旧副本，并清理历史文件关联。`
            : "已清理历史文件关联。",
        );
      }
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "清理历史文件关联失败",
      );
    } finally {
      setLaunchServicesBusy(false);
    }
  };

  const confirmLaunchServicesCleanup = () => {
    const copies = launchServices?.disk_copies || [];
    if (copies.length === 0) {
      void cleanLaunchServices(false);
      return;
    }
    modal.confirm({
      title: `删除 ${copies.length} 个旧版澜台副本？`,
      icon: <WarningOutlined />,
      width: 620,
      content: (
        <div className={styles.cleanupConfirm}>
          <p>
            旧版澜台仍可能携带已取消的系统文件关联。下面这些副本将被永久删除，
            无法恢复；当前正在运行的
            {launchServices?.current_app?.version
              ? ` ${launchServices.current_app.version} `
              : "澜台"}
            不会受影响。
          </p>
          <div className={styles.pathList}>
            {copies.map((copy) => (
              <code key={copy.path}>
                <b>{copy.version}</b>
                {copy.path}
              </code>
            ))}
          </div>
        </div>
      ),
      okText: "删除旧副本并清理",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => cleanLaunchServices(true),
    });
  };

  const update = async <K extends keyof SystemBehaviorSettings>(
    key: K,
    value: SystemBehaviorSettings[K],
  ) => {
    const previous = settings;
    const optimistic = { ...settings, [key]: value };
    setSettings(optimistic);
    setSavingKey(key);
    setSaveState("saving");
    try {
      const saved = await settingsApi.updateSystemBehaviorSettings({
        [key]: value,
      });
      const desktopResult =
        await window.electronAPI?.updateSystemBehavior?.(saved);
      if (desktopResult && !desktopResult.ok) {
        throw new Error(desktopResult.error || "桌面设置应用失败");
      }
      setSettings(saved);
      setSaveState("saved");
    } catch (error) {
      setSettings(previous);
      setSaveState("error");
      message.error(
        error instanceof Error ? error.message : "保存系统设置失败",
      );
    } finally {
      setSavingKey(undefined);
    }
  };

  return (
    <main className={styles.page}>
      <PageHeader
        parent="设置"
        current="系统行为"
        description="控制澜台如何随系统启动、在长任务中保持运行，以及何时通知你。"
      />

      {loading ? (
        <div className={styles.loading}>
          <Spin />
        </div>
      ) : (
        <div className={styles.content}>
          <div
            className={styles.saveState}
            data-state={saveState}
            aria-live="polite"
          >
            <span>生效方式</span>
            <strong>
              {saveState === "saving"
                ? "正在保存并应用…"
                : saveState === "saved"
                  ? "已保存，立即生效"
                  : saveState === "error"
                    ? "保存失败，已恢复原设置"
                    : "调整后会立即保存"}
            </strong>
          </div>
          {!isDesktop ? (
            <Alert
              type="info"
              showIcon
              message="开机启动和防休眠仅在澜台桌面客户端中生效。"
            />
          ) : null}

          <section>
            <h2>系统设置</h2>
            <div className={styles.panel}>
              <SettingRow
                icon={<DesktopOutlined />}
                title="开机自启动"
                description="登录 macOS 时自动启动澜台。"
                control={
                  <Switch
                    checked={settings.launch_at_login}
                    loading={savingKey === "launch_at_login"}
                    disabled={!isDesktop}
                    onChange={(checked) =>
                      void update("launch_at_login", checked)
                    }
                  />
                }
              />
              <SettingRow
                icon={<BulbOutlined />}
                title="保持电脑唤醒"
                description="仅在会话任务实际运行时阻止电脑睡眠，任务结束后自动恢复。"
                control={
                  <Switch
                    checked={settings.keep_awake_while_running}
                    loading={savingKey === "keep_awake_while_running"}
                    disabled={!isDesktop}
                    onChange={(checked) =>
                      void update("keep_awake_while_running", checked)
                    }
                  />
                }
              />
            </div>
          </section>

          <section>
            <h2>通知</h2>
            <div className={styles.panel}>
              <SettingRow
                icon={<BellOutlined />}
                title="轮次完成通知"
                description="设置澜台完成一轮回复或任务后提醒你的时机。"
                control={
                  <Select<TurnCompletionNotificationMode>
                    value={settings.turn_completion_notification}
                    loading={savingKey === "turn_completion_notification"}
                    className={styles.select}
                    options={[
                      { value: "never", label: "从不" },
                      { value: "when_unfocused", label: "仅当应用失焦时" },
                      { value: "always", label: "总是" },
                    ]}
                    onChange={(value) =>
                      void update("turn_completion_notification", value)
                    }
                  />
                }
              />
              <SettingRow
                icon={<SafetyCertificateOutlined />}
                title="启用权限通知"
                description="任务在后台等待安全审批时显示系统通知；当前正在使用澜台时不打扰。"
                control={
                  <Switch
                    checked={settings.permission_notification}
                    loading={savingKey === "permission_notification"}
                    onChange={(checked) =>
                      void update("permission_notification", checked)
                    }
                  />
                }
              />
            </div>
          </section>

          <section>
            <h2>历史文件关联</h2>
            <div className={styles.associationPanel}>
              <div className={styles.associationHeader}>
                <span className={styles.icon}>
                  <FolderOpenOutlined />
                </span>
                <span className={styles.copy}>
                  <strong>清理旧版澜台文件关联</strong>
                  <em>
                    当前版本不再注册为 Office、PDF 或 Markdown
                    的系统打开方式；此处仅用于清理旧版本遗留。
                  </em>
                </span>
                <div className={styles.associationActions}>
                  <Button
                    icon={<ReloadOutlined />}
                    disabled={!isDesktop}
                    loading={launchServicesBusy && !launchServices}
                    onClick={() => void inspectLaunchServices()}
                  >
                    重新检查
                  </Button>
                  <Button
                    type="primary"
                    danger={Boolean(launchServices?.disk_copies?.length)}
                    icon={
                      launchServices?.disk_copies?.length ? (
                        <DeleteOutlined />
                      ) : (
                        <ReloadOutlined />
                      )
                    }
                    disabled={!isDesktop || !launchServices?.supported}
                    loading={launchServicesBusy && Boolean(launchServices)}
                    onClick={confirmLaunchServicesCleanup}
                  >
                    {launchServices?.disk_copies?.length
                      ? "清理旧版本"
                      : "清理历史关联"}
                  </Button>
                </div>
              </div>

              <LaunchServicesSummary
                state={launchServices}
                desktop={isDesktop}
              />
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

function LaunchServicesSummary({
  state,
  desktop,
}: {
  state: DesktopLaunchServicesState | null;
  desktop: boolean;
}) {
  if (!desktop) {
    return (
      <p className={styles.associationStatus}>
        此功能仅支持 macOS 桌面客户端。
      </p>
    );
  }
  if (!state) {
    return (
      <p className={styles.associationStatus}>正在检查当前版本与历史副本…</p>
    );
  }
  if (!state.ok) {
    return (
      <p className={styles.associationError}>{state.error || "检查失败"}</p>
    );
  }
  const staleRegistrations = state.stale_registrations || [];
  const diskCopies = state.disk_copies || [];
  const clean = staleRegistrations.length === 0 && diskCopies.length === 0;
  return (
    <div className={styles.associationDetails}>
      <div className={clean ? styles.cleanState : styles.warningState}>
        {clean ? <CheckCircleOutlined /> : <WarningOutlined />}
        <span>
          {clean
            ? `当前只保留澜台 ${state.current_app?.version || "当前版本"}`
            : `发现 ${staleRegistrations.length} 条旧注册、${diskCopies.length} 个完整旧 App 副本`}
        </span>
      </div>
      {state.current_app ? (
        <code className={styles.currentPath}>
          <b>当前保留</b>
          {state.current_app.path}
        </code>
      ) : null}
      {diskCopies.length > 0 ? (
        <div className={styles.pathList}>
          {diskCopies.map((copy) => (
            <code key={copy.path}>
              <b>{copy.version}</b>
              {copy.path}
            </code>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SettingRow({
  icon,
  title,
  description,
  control,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  control: React.ReactNode;
}) {
  return (
    <div className={styles.row}>
      <span className={styles.icon}>{icon}</span>
      <span className={styles.copy}>
        <strong>{title}</strong>
        <em>{description}</em>
      </span>
      <span className={styles.control}>{control}</span>
    </div>
  );
}
