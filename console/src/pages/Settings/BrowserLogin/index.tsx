import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircleOutlined,
  CopyOutlined,
  GlobalOutlined,
  LinkOutlined,
  LoadingOutlined,
  SafetyOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { PageHeader } from "@/components/PageHeader";
import { browserPairingApi } from "../../../api/modules/browserPairing";
import { browserWorkbenchApi } from "../../../api/modules/browserSession";
import type {
  BrowserSkillStatus,
  BrowserWorkbenchConfig,
} from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import styles from "./index.module.less";

export default function BrowserLoginPage() {
  const { message } = useAppMessage();
  const isDesktop =
    typeof window !== "undefined" && Boolean(window.electronAPI?.isDesktop);
  const browserPairUrl =
    typeof window !== "undefined"
      ? `${window.location.origin}/#/browser-pair`
      : "";
  const [pairingCode, setPairingCode] = useState("");
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [copiedKind, setCopiedKind] = useState<"url" | "code" | null>(null);
  const [browserSkill, setBrowserSkill] = useState<BrowserSkillStatus | null>(
    null,
  );
  const [browserConfig, setBrowserConfig] =
    useState<BrowserWorkbenchConfig | null>(null);
  const [checkingBrowserSkill, setCheckingBrowserSkill] = useState(false);
  const [switchingRuntime, setSwitchingRuntime] = useState(false);

  const refreshBrowserSkill = useCallback(async () => {
    setCheckingBrowserSkill(true);
    try {
      const [status, config] = await Promise.all([
        browserWorkbenchApi.getBrowserSkillStatus(),
        browserWorkbenchApi.getConfig(),
      ]);
      setBrowserSkill(status);
      setBrowserConfig(config);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "检测浏览器伴随能力失败",
      );
    } finally {
      setCheckingBrowserSkill(false);
    }
  }, [message]);

  useEffect(() => {
    void refreshBrowserSkill();
  }, [refreshBrowserSkill]);

  const browserSkillSelected =
    browserConfig?.primary_runtime === "browser_skill";
  const browserSkillReady =
    browserSkill?.connected === true && browserSkill.browsers.length === 1;

  const toggleBrowserSkillRuntime = useCallback(async () => {
    if (!browserSkillReady && !browserSkillSelected) {
      message.warning(
        browserSkill?.browsers.length
          ? "请先只保留一个 BrowserSkill 浏览器连接。"
          : "请先安装 BrowserSkill，并让 Chrome 或 Edge 扩展保持连接。",
      );
      return;
    }
    setSwitchingRuntime(true);
    try {
      const config = await browserWorkbenchApi.updateConfig({
        primary_runtime: browserSkillSelected
          ? "electron_bridge"
          : "browser_skill",
      });
      setBrowserConfig(config);
      message.success(
        config.primary_runtime === "browser_skill"
          ? "已优先使用已登录的 Chrome / Edge"
          : "已恢复澜台内建浏览器",
      );
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "切换浏览器运行时失败",
      );
    } finally {
      setSwitchingRuntime(false);
    }
  }, [
    browserSkill?.browsers.length,
    browserSkillReady,
    browserSkillSelected,
    message,
  ]);

  const expiresText = useMemo(() => {
    if (!expiresAt) return "";
    return new Date(expiresAt * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  }, [expiresAt]);

  const copyText = useCallback(
    async (text: string, kind: "url" | "code") => {
      if (!text) return;
      await navigator.clipboard?.writeText(text);
      setCopiedKind(kind);
      message.success(kind === "url" ? "已复制网页地址" : "已复制配对码");
    },
    [message],
  );

  const createPairingCode = useCallback(async () => {
    if (!isDesktop) {
      message.warning("请在澜台桌面客户端里生成配对码。");
      return;
    }
    setBusy(true);
    setCopiedKind(null);
    try {
      const result = await browserPairingApi.createBrowserPairingCode();
      setPairingCode(result.code);
      setExpiresAt(result.expires_at);
      message.success("配对码已生成，5 分钟内有效。");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "生成配对码失败");
    } finally {
      setBusy(false);
    }
  }, [isDesktop, message]);

  const copyPairingCode = useCallback(() => {
    if (!pairingCode) return;
    void copyText(pairingCode, "code");
  }, [copyText, pairingCode]);

  return (
    <main className={styles.page}>
      <PageHeader
        parent="设置"
        current="浏览器使用"
        description="分别管理“在浏览器里使用澜台”和“让澜台操作已登录浏览器”。"
      />

      <section className={styles.content}>
        <div className={styles.mainColumn}>
          <div className={styles.primaryPanel}>
            <div className={styles.panelIntro}>
              <span className={styles.panelIcon}>
                <LinkOutlined />
              </span>
              <div>
                <span className={styles.purposeLabel}>在浏览器中使用澜台</span>
                <h2>连接这个澜台客户端</h2>
                <p>
                  配对后，浏览器页面会显示同一份会话、模型、知识库和技能配置。
                </p>
              </div>
            </div>

            <div className={styles.addressRow}>
              <span>网页地址</span>
              <code>{browserPairUrl || "等待客户端地址"}</code>
              <button
                type="button"
                onClick={() => void copyText(browserPairUrl, "url")}
                disabled={!browserPairUrl}
              >
                {copiedKind === "url" ? (
                  <CheckCircleOutlined />
                ) : (
                  <CopyOutlined />
                )}
                复制地址
              </button>
            </div>

            <div className={styles.codeBlock} data-empty={!pairingCode}>
              <span>一次性配对码</span>
              <strong>{pairingCode || "点击生成"}</strong>
              <em>
                {expiresText ? `${expiresText} 前有效` : "生成后 5 分钟有效"}
              </em>
            </div>

            <div className={styles.actions}>
              <button
                type="button"
                className={styles.primaryAction}
                onClick={createPairingCode}
                disabled={busy || !isDesktop}
              >
                <SyncOutlined spin={busy} />
                {busy ? "生成中..." : "生成配对码"}
              </button>
              <button
                type="button"
                onClick={copyPairingCode}
                disabled={!pairingCode}
              >
                {copiedKind === "code" ? (
                  <CheckCircleOutlined />
                ) : (
                  <CopyOutlined />
                )}
                复制配对码
              </button>
            </div>
          </div>

          <div className={styles.companionPanel}>
            <div className={styles.panelIntro}>
              <span className={styles.panelIcon}>
                <GlobalOutlined />
              </span>
              <div>
                <div className={styles.companionTitleRow}>
                  <div>
                    <span className={styles.purposeLabel}>
                      让澜台操作已登录浏览器
                    </span>
                    <h2>已登录浏览器伴随</h2>
                  </div>
                  <span data-status={browserSkill?.status || "checking"}>
                    {checkingBrowserSkill
                      ? "检测中"
                      : browserSkill?.connected
                        ? "已连接"
                        : browserSkill?.installed
                          ? "等待扩展"
                          : "未安装"}
                  </span>
                </div>
                <p>
                  接入腾讯 BrowserSkill 后，澜台可在独立 Agent Window
                  中使用你已登录的 Chrome / Edge；具体浏览任务仍从主聊天发起。
                </p>
              </div>
            </div>

            <div className={styles.companionStatus}>
              <strong>{browserSkill?.message || "正在检测本机运行时…"}</strong>
              <span>
                {browserSkill?.connected
                  ? `${browserSkill.browsers.length} 个浏览器已连接 · ${browserSkill.sessions.length} 个活动会话`
                  : "不会自动安装、读取 Cookie 或接管你的普通窗口。"}
              </span>
            </div>

            <div className={styles.actions}>
              <button
                type="button"
                className={browserSkillSelected ? styles.primaryAction : ""}
                onClick={() => void toggleBrowserSkillRuntime()}
                disabled={
                  switchingRuntime ||
                  checkingBrowserSkill ||
                  (!browserSkillReady && !browserSkillSelected)
                }
              >
                {switchingRuntime ? <LoadingOutlined /> : <GlobalOutlined />}
                {browserSkillSelected ? "恢复内建浏览器" : "设为首选"}
              </button>
              <button
                type="button"
                onClick={() => void refreshBrowserSkill()}
                disabled={checkingBrowserSkill}
              >
                <SyncOutlined spin={checkingBrowserSkill} />
                重新检测
              </button>
              <a
                href="https://github.com/Tencent/BrowserSkill"
                target="_blank"
                rel="noreferrer"
              >
                安装与扩展说明
              </a>
            </div>
          </div>
        </div>

        <aside className={styles.sidePanel}>
          <div className={styles.sideHeader}>
            <SafetyOutlined />
            <strong>安全规则</strong>
          </div>
          <ul>
            <li>配对码只在本机回环地址上使用。</li>
            <li>每个配对码只能使用一次。</li>
            <li>后端重启后，浏览器需要重新配对。</li>
            <li>BrowserSkill 默认只操作隔离的 Agent Window。</li>
            <li>借用普通标签页和页面写入仍需澜台确认。</li>
          </ul>
        </aside>
      </section>
    </main>
  );
}
