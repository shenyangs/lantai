import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Button, Card, Space, Tag, Typography } from "antd";
import {
  CheckCircleOutlined,
  CopyOutlined,
  DesktopOutlined,
  EyeOutlined,
  FolderOpenOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SafetyOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { PageHeader } from "@/components/PageHeader";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  computerPermissionsApi,
  type ComputerPermissionKey,
  type ComputerPermissionStatus,
  type ComputerPermissionStatusValue,
} from "../../../api/modules/computerPermissions";
import { SettingsNotice } from "../components/SettingsNotice";
import styles from "./index.module.less";

const { Paragraph, Text, Title } = Typography;

interface PermissionMeta {
  key: ComputerPermissionKey;
  title: string;
  icon: ReactNode;
  tone: "blue" | "green" | "gold" | "red" | "gray";
  why: string;
  enables: string[];
  manual: string;
  requestLabel: string;
}

const PERMISSIONS: PermissionMeta[] = [
  {
    key: "accessibility",
    title: "辅助功能",
    icon: <DesktopOutlined />,
    tone: "blue",
    why: "用于让澜台在你明确发起桌面自动化任务时点击按钮、输入文字、操作窗口。",
    enables: ["控制本机应用界面", "执行可见的桌面工作流", "辅助完成跨应用操作"],
    manual: "系统设置 → 隐私与安全性 → 辅助功能，打开澜台。",
    requestLabel: "打开辅助功能设置",
  },
  {
    key: "full_disk_access",
    title: "完全磁盘访问",
    icon: <FolderOpenOutlined />,
    tone: "green",
    why: "用于访问你授权范围内的工作区、下载文件、文档和应用数据，避免文件读取或备份恢复被系统拦截。",
    enables: [
      "读取受保护目录中的项目文件",
      "导入、备份和恢复本地数据",
      "让文件工具少遇到系统级拒绝",
    ],
    manual: "系统设置 → 隐私与安全性 → 完全磁盘访问，添加并打开澜台。",
    requestLabel: "打开磁盘访问设置",
  },
  {
    key: "screen_recording",
    title: "屏幕录制",
    icon: <EyeOutlined />,
    tone: "gold",
    why: "用于你要求澜台观察屏幕、验证界面状态或读取可见窗口内容时截取屏幕画面。",
    enables: ["查看当前屏幕画面", "验证页面和应用界面", "辅助定位视觉类问题"],
    manual: "系统设置 → 隐私与安全性 → 屏幕与系统音频录制，打开澜台。",
    requestLabel: "打开屏幕录制设置",
  },
  {
    key: "automation",
    title: "自动化",
    icon: <ThunderboltOutlined />,
    tone: "gray",
    why: "用于通过系统自动化控制 Finder、浏览器、终端等应用。macOS 通常会在首次操作目标应用时弹窗确认。",
    enables: ["控制指定应用", "打开文件或 URL", "串联多应用流程"],
    manual: "系统设置 → 隐私与安全性 → 自动化，确认澜台允许控制相关应用。",
    requestLabel: "打开自动化设置",
  },
];

function statusLabel(status: ComputerPermissionStatusValue) {
  if (status === "granted") return "已允许";
  if (status === "denied") return "未允许";
  if (status === "unsupported") return "不支持";
  return "需手动确认";
}

function statusTone(status: ComputerPermissionStatusValue) {
  if (status === "granted") return "success";
  if (status === "denied") return "error";
  if (status === "unsupported") return "default";
  return "warning";
}

function statusHint(status: ComputerPermissionStatusValue, canCheck: boolean) {
  if (status === "granted") return "系统已报告允许。";
  if (status === "denied") return "系统已报告未允许，打开设置后勾选澜台。";
  if (status === "unsupported") return "当前系统不支持此权限入口。";
  return canCheck
    ? "暂时无法读取系统状态，请打开设置确认。"
    : "macOS 不向应用稳定暴露此状态，请以系统设置中的开关为准。";
}

export default function ComputerPermissionsPage() {
  const { message } = useAppMessage();
  const [status, setStatus] = useState<ComputerPermissionStatus | null>(null);
  const [nativeStatus, setNativeStatus] =
    useState<DesktopComputerPermissionStatus | null>(null);
  const [macUseStatus, setMacUseStatus] = useState<MacUseDesktopResult | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<ComputerPermissionKey | null>(null);
  const [macUseBusy, setMacUseBusy] = useState<
    "accessibility" | "screen_recording" | null
  >(null);
  const refreshSequenceRef = useRef(0);

  const statusByKey = useMemo(() => {
    return new Map(status?.permissions.map((item) => [item.key, item]) ?? []);
  }, [status]);

  const nativeStatusByKey = useMemo(() => {
    return new Map(
      nativeStatus?.permissions.map((item) => [item.key, item]) ?? [],
    );
  }, [nativeStatus]);

  const refreshStatus = useCallback(async () => {
    const refreshSequence = ++refreshSequenceRef.current;
    setLoading(true);
    try {
      const nativeReader =
        window.electronAPI?.getComputerPermissionStatus?.bind(
          window.electronAPI,
        );
      const macUseReader = window.electronAPI?.getMacUseStatus?.bind(
        window.electronAPI,
      );
      const [backendResult, nativeResult, macUseResult] =
        await Promise.allSettled([
          computerPermissionsApi.getComputerPermissions(),
          nativeReader ? nativeReader() : Promise.resolve(null),
          macUseReader ? macUseReader() : Promise.resolve(null),
        ]);

      if (refreshSequence !== refreshSequenceRef.current) return;

      if (backendResult.status === "fulfilled") {
        setStatus(backendResult.value);
      } else {
        message.error(
          backendResult.reason instanceof Error
            ? backendResult.reason.message
            : "读取电脑权限状态失败",
        );
      }

      const nextNativeStatus =
        nativeResult.status === "fulfilled" && nativeResult.value?.ok
          ? nativeResult.value
          : null;
      setNativeStatus(nextNativeStatus);
      setMacUseStatus(
        macUseResult.status === "fulfilled" && macUseResult.value
          ? macUseResult.value
          : null,
      );
    } finally {
      if (refreshSequence === refreshSequenceRef.current) {
        setLoading(false);
      }
    }
  }, [message]);

  useEffect(() => {
    void refreshStatus();

    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") {
        void refreshStatus();
      }
    };
    const refreshWhenFocused = () => void refreshStatus();
    document.addEventListener("visibilitychange", refreshWhenVisible);
    window.addEventListener("focus", refreshWhenFocused);
    return () => {
      document.removeEventListener("visibilitychange", refreshWhenVisible);
      window.removeEventListener("focus", refreshWhenFocused);
      refreshSequenceRef.current += 1;
    };
  }, [refreshStatus]);

  const getEffectiveStatus = useCallback(
    (key: ComputerPermissionKey): ComputerPermissionStatusValue => {
      const nativePermission = nativeStatusByKey.get(key);
      if (nativePermission) return nativePermission.status;
      return statusByKey.get(key)?.status ?? "unknown";
    },
    [nativeStatusByKey, statusByKey],
  );

  const handleRequest = useCallback(
    async (key: ComputerPermissionKey) => {
      setBusyKey(key);
      try {
        await computerPermissionsApi.requestComputerPermission(key);
        message.success("已打开系统设置，请在对应项目中允许澜台。");
        await refreshStatus();
      } catch (error) {
        message.error(
          error instanceof Error ? error.message : "无法发起权限请求",
        );
      } finally {
        setBusyKey(null);
      }
    },
    [message, refreshStatus],
  );

  const handleOpenSettings = useCallback(
    async (key: ComputerPermissionKey) => {
      setBusyKey(key);
      try {
        await computerPermissionsApi.openComputerPermissionSettings(key);
      } catch (error) {
        message.error(
          error instanceof Error ? error.message : "无法打开系统设置",
        );
      } finally {
        setBusyKey(null);
      }
    },
    [message],
  );

  const handleCopyAppPath = useCallback(async () => {
    const appPath = nativeStatus?.app_path || status?.app_path || "";
    if (!appPath) {
      message.warning("当前环境未检测到可复制的应用包路径。");
      return;
    }
    try {
      await navigator.clipboard.writeText(appPath);
      message.success("已复制应用路径。");
    } catch {
      message.error("复制失败，请手动查看页面中的路径。");
    }
  }, [message, nativeStatus?.app_path, status?.app_path]);

  const requestMacUsePermission = useCallback(
    async (key: "accessibility" | "screen_recording") => {
      const requestPermission = window.electronAPI?.requestMacUsePermission;
      if (!requestPermission) {
        message.warning("当前环境没有连接 Mac 应用操作组件。");
        return;
      }
      setMacUseBusy(key);
      try {
        const result = await requestPermission(key);
        if (!result.ok) {
          throw new Error(result.error || "macOS 没有开放该权限");
        }
        message.info("请在 macOS 提示或系统设置中完成授权，然后返回刷新状态。");
        await refreshStatus();
      } catch (error) {
        message.error(error instanceof Error ? error.message : "权限请求失败");
      } finally {
        setMacUseBusy(null);
      }
    },
    [message, refreshStatus],
  );

  const appPath = nativeStatus?.app_path || status?.app_path || "";
  const isMacos =
    status?.is_macos ??
    (nativeStatus
      ? nativeStatus.platform === "darwin"
      : window.electronAPI?.platform === "darwin");

  const grantedCount = PERMISSIONS.filter(
    (item) => getEffectiveStatus(item.key) === "granted",
  ).length;
  const needsReviewCount = PERMISSIONS.length - grantedCount;

  return (
    <div className={styles.permissionsPage}>
      <PageHeader
        parent="设置"
        current="电脑权限"
        description="集中说明澜台需要哪些 macOS 权限、为什么需要，以及如何由你手动授予。"
        extra={
          <Button
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={refreshStatus}
          >
            刷新状态
          </Button>
        }
      />

      <main className={styles.content}>
        <section className={styles.summaryGrid}>
          <div className={styles.summaryPanel}>
            <span className={styles.panelIcon}>
              <SafetyOutlined />
            </span>
            <div>
              <Text type="secondary">已允许</Text>
              <strong>{grantedCount}</strong>
            </div>
          </div>
          <div className={styles.summaryPanel}>
            <span className={styles.panelIcon}>
              <QuestionCircleOutlined />
            </span>
            <div>
              <Text type="secondary">待确认</Text>
              <strong>{needsReviewCount}</strong>
            </div>
          </div>
          <div className={styles.summaryPanelWide}>
            <span className={styles.panelIcon}>
              <ToolOutlined />
            </span>
            <div className={styles.pathCopy}>
              <Text type="secondary">应用路径</Text>
              <code>{appPath || "未检测到当前应用包路径"}</code>
            </div>
            <Button
              icon={<CopyOutlined />}
              disabled={!appPath}
              onClick={handleCopyAppPath}
            >
              复制
            </Button>
          </div>
        </section>

        <SettingsNotice
          className={styles.notice}
          title="权限由你决定，不会一次性强制开启。"
        >
          只在你需要对应能力时开启即可。澜台仍会保留应用内的工具审批、文件防护和安全策略；系统权限只是让
          macOS 允许应用执行你已经确认的本机操作。
        </SettingsNotice>

        <SettingsNotice
          className={styles.notice}
          title={
            macUseStatus?.ok
              ? "Mac 应用操作桥已就绪（实验）"
              : "Mac 应用操作桥尚未就绪（实验）"
          }
        >
          {macUseStatus?.ok ? (
            <div>
              澜台已经能连接随应用签名分发的原生操作组件。当前已验证 TextEdit
              控件读写，其他应用仍按实验能力处理。组件报告：辅助功能
              {String(macUseStatus.data?.accessibility || "unknown") ===
              "granted"
                ? "已允许"
                : "待确认"}
              ，屏幕录制
              {String(macUseStatus.data?.screen_recording || "unknown") ===
              "granted"
                ? "已允许"
                : "待确认"}
              。真正操作时仍会绑定你在对话里选定的应用窗口和当前任务权限。
              <Space wrap style={{ marginTop: 12 }}>
                {String(macUseStatus.data?.accessibility || "unknown") !==
                "granted" ? (
                  <Button
                    size="small"
                    loading={macUseBusy === "accessibility"}
                    onClick={() =>
                      void requestMacUsePermission("accessibility")
                    }
                  >
                    请求 Mac 操作权限
                  </Button>
                ) : null}
                {String(macUseStatus.data?.screen_recording || "unknown") !==
                "granted" ? (
                  <Button
                    size="small"
                    loading={macUseBusy === "screen_recording"}
                    onClick={() =>
                      void requestMacUsePermission("screen_recording")
                    }
                  >
                    请求窗口读取权限
                  </Button>
                ) : null}
              </Space>
            </div>
          ) : (
            <>
              当前环境没有连上 Mac
              应用操作组件。普通聊天、文件和浏览器能力不受影响；正式安装包应包含该组件。
            </>
          )}
        </SettingsNotice>

        {status && !status.is_macos ? (
          <SettingsNotice
            tone="warning"
            className={styles.notice}
            title={`当前系统为 ${status.platform}`}
          >
            此页面的系统设置直达入口主要面向
            macOS。其它系统请在系统隐私或安全设置中手动授权。
          </SettingsNotice>
        ) : null}

        <section className={styles.permissionGrid}>
          {PERMISSIONS.map((item) => {
            const permission = statusByKey.get(item.key);
            const nativePermission = nativeStatusByKey.get(item.key);
            const currentStatus = getEffectiveStatus(item.key);
            const canRequest = permission?.can_request ?? Boolean(isMacos);
            const canCheck =
              nativePermission?.can_check ?? permission?.can_check ?? false;
            const isGranted = currentStatus === "granted";
            return (
              <Card
                key={item.key}
                className={`${styles.permissionCard} ${styles[item.tone]}`}
              >
                <div className={styles.permissionHeader}>
                  <Space size={10} align="center">
                    <span className={styles.permissionIcon}>{item.icon}</span>
                    <Title level={4}>{item.title}</Title>
                  </Space>
                  <Tag
                    icon={
                      currentStatus === "granted" ? (
                        <CheckCircleOutlined />
                      ) : undefined
                    }
                    color={statusTone(currentStatus)}
                  >
                    {statusLabel(currentStatus)}
                  </Tag>
                </div>

                <Paragraph className={styles.why}>{item.why}</Paragraph>

                <div className={styles.canDo}>
                  <Text type="secondary">开启后可以：</Text>
                  <ul>
                    {item.enables.map((text) => (
                      <li key={text}>{text}</li>
                    ))}
                  </ul>
                </div>

                <div className={styles.manualBox}>
                  <Text type="secondary">手动开启路径</Text>
                  <p>{item.manual}</p>
                  <p className={styles.statusHint}>
                    {statusHint(currentStatus, canCheck)}
                  </p>
                </div>

                <div className={styles.actions}>
                  <Button
                    type={isGranted ? "default" : "primary"}
                    icon={
                      isGranted ? <CheckCircleOutlined /> : <SettingOutlined />
                    }
                    disabled={!canRequest || isGranted}
                    loading={busyKey === item.key}
                    onClick={() => void handleRequest(item.key)}
                  >
                    {isGranted ? "已允许" : item.requestLabel}
                  </Button>
                  <Button
                    disabled={!canRequest}
                    onClick={() => void handleOpenSettings(item.key)}
                  >
                    {isGranted ? "查看系统设置" : "手动设置"}
                  </Button>
                </div>
              </Card>
            );
          })}
        </section>
      </main>
    </div>
  );
}
