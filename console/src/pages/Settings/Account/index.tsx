import {
  CheckCircleFilled,
  CloseOutlined,
  LinkOutlined,
  LockOutlined,
  LogoutOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { Button, Spin } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { PageHeader } from "../../../components/PageHeader";
import {
  cloudAccountApi,
  type CloudAccountStatus,
} from "../../../api/modules/cloudAccount";
import { prepareExternalNavigation } from "../../../utils/openExternalUrl";
import styles from "./index.module.less";
import {
  buildModelsSetupPath,
  normalizeModelSetupReturnTo,
  parseModelSetupMode,
} from "../../../utils/modelSetupFlow";

const MAX_POLL_ATTEMPTS = 300;

export default function AccountSettingsPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const guidedSetup =
    parseModelSetupMode(searchParams.get("setup")) === "watcha";
  const setupReturnTo = normalizeModelSetupReturnTo(
    searchParams.get("returnTo"),
  );
  const [account, setAccount] = useState<CloudAccountStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [authorizing, setAuthorizing] = useState(false);
  const [attemptId, setAttemptId] = useState("");
  const [error, setError] = useState("");
  const generationRef = useRef(0);
  const continuedRef = useRef(false);

  const continueGuidedSetup = useCallback(
    (next: CloudAccountStatus) => {
      if (!guidedSetup || !next.connected || continuedRef.current) return;
      continuedRef.current = true;
      navigate(buildModelsSetupPath("watcha", setupReturnTo), {
        replace: true,
      });
    },
    [guidedSetup, navigate, setupReturnTo],
  );

  const load = useCallback(async () => {
    const generation = ++generationRef.current;
    setLoading(true);
    setError("");
    try {
      const next = await cloudAccountApi.status();
      if (generation === generationRef.current) {
        setAccount(next);
        continueGuidedSetup(next);
      }
    } catch (cause) {
      if (generation === generationRef.current) {
        setError(cause instanceof Error ? cause.message : "无法读取账号状态");
      }
    } finally {
      if (generation === generationRef.current) setLoading(false);
    }
  }, [continueGuidedSetup]);

  useEffect(() => {
    void load();
    return () => {
      generationRef.current += 1;
    };
  }, [load]);

  const login = useCallback(async () => {
    const generation = ++generationRef.current;
    setAuthorizing(true);
    setAttemptId("");
    setError("");
    const navigation = prepareExternalNavigation();
    try {
      if (!navigation) throw new Error("浏览器阻止了授权窗口，请允许后重试");
      const started = await cloudAccountApi.startWatcha();
      if (generation !== generationRef.current) return;
      setAttemptId(started.attempt_id);
      await navigation.open(started.authorization_url);
      const interval = Math.max(1, started.poll_interval_seconds) * 1000;
      for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, interval));
        if (generation !== generationRef.current) return;
        const next = await cloudAccountApi.pollWatcha(started.attempt_id);
        if (next.status === "pending") continue;
        setAccount(next);
        setAttemptId("");
        continueGuidedSetup(next);
        return;
      }
      throw new Error("授权页等待超时，请重新使用观猹登录");
    } catch (cause) {
      navigation?.close();
      if (generation === generationRef.current) {
        setError(cause instanceof Error ? cause.message : "无法完成观猹登录");
        setAttemptId("");
      }
    } finally {
      if (generation === generationRef.current) setAuthorizing(false);
    }
  }, [continueGuidedSetup]);

  const cancelLogin = useCallback(async () => {
    generationRef.current += 1;
    const activeAttempt = attemptId;
    setAttemptId("");
    setAuthorizing(false);
    setError("");
    if (!activeAttempt) return;
    try {
      await cloudAccountApi.cancelWatcha(activeAttempt);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "没有成功取消这次登录");
    }
  }, [attemptId]);

  const logout = useCallback(async () => {
    generationRef.current += 1;
    setLoading(true);
    setError("");
    try {
      await cloudAccountApi.logout();
      setAccount({ connected: false });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "退出登录失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const connected = Boolean(account?.connected && account.user);
  const displayName = account?.user?.displayName || "观猹用户";
  const initial = displayName.trim().slice(0, 1) || "澜";
  return (
    <main className={styles.page}>
      <PageHeader
        parent="设置"
        current="澜台账号"
        description="通过观猹注册澜台身份；不登录也能继续使用全部本地功能。"
      />

      <div className={styles.content}>
        {guidedSetup ? (
          <section className={styles.setupBanner} aria-label="观猹模型接入">
            <div>
              <small>模型接入 · 第 1 步</small>
              <strong>先确认观猹身份</strong>
              <p>
                登录完成后会自动继续连接
                TokenDance。身份登录不会自动授权模型消费。
              </p>
            </div>
            <ol aria-label="接入进度">
              <li data-current="true">1 观猹登录</li>
              <li>2 TokenDance 授权</li>
              <li>3 选择主模型</li>
            </ol>
          </section>
        ) : null}
        <section
          className={styles.identityPanel}
          data-state={
            authorizing ? "authorizing" : connected ? "connected" : "idle"
          }
          aria-labelledby="account-state-title"
          aria-busy={loading || authorizing}
        >
          <div className={styles.identityHeader}>
            <span className={styles.avatar} aria-hidden="true">
              {connected ? (
                initial
              ) : authorizing ? (
                <Spin size="small" />
              ) : (
                <UserOutlined />
              )}
            </span>
            <div className={styles.identityCopy}>
              <div className={styles.titleRow}>
                <h2 id="account-state-title">
                  {connected
                    ? displayName
                    : authorizing
                      ? "正在等待观猹确认"
                      : "获得你的澜台 UID"}
                </h2>
                <span className={styles.statusBadge}>
                  {connected ? (
                    <>
                      <CheckCircleFilled /> 已连接
                    </>
                  ) : authorizing ? (
                    "授权中"
                  ) : (
                    "未连接"
                  )}
                </span>
              </div>
              <p>
                {connected
                  ? "观猹已确认身份，澜台账号可以用于设备和云端服务。"
                  : authorizing
                    ? "授权页已在系统浏览器打开。完成确认后，这里会自动更新。"
                    : "观猹负责确认你是谁，澜台会为你创建独立账号；整个过程不需要设置新密码。"}
              </p>
            </div>
          </div>

          {loading && !account ? (
            <div className={styles.loadingState}>
              <Spin aria-label="读取账号状态" />
              <span>正在读取账号状态…</span>
            </div>
          ) : connected ? (
            <>
              <dl className={styles.accountFacts}>
                <div>
                  <dt>澜台 UID</dt>
                  <dd>{account?.user?.uid ?? "—"}</dd>
                </div>
                <div>
                  <dt>身份来源</dt>
                  <dd>观猹</dd>
                </div>
              </dl>
              <div className={styles.actions}>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => void load()}
                  loading={loading}
                >
                  刷新状态
                </Button>
                <Button
                  danger
                  icon={<LogoutOutlined />}
                  onClick={() => void logout()}
                  disabled={loading}
                >
                  退出账号
                </Button>
              </div>
            </>
          ) : (
            <>
              <ol className={styles.steps} aria-label="观猹登录流程">
                <li>
                  <LinkOutlined aria-hidden="true" />
                  <span>
                    <strong>浏览器确认</strong>
                    <em>使用现有观猹账号授权</em>
                  </span>
                </li>
                <li>
                  <SafetyCertificateOutlined aria-hidden="true" />
                  <span>
                    <strong>建立身份</strong>
                    <em>外部用户 UID 从 11 开始</em>
                  </span>
                </li>
                <li>
                  <LockOutlined aria-hidden="true" />
                  <span>
                    <strong>回到本机</strong>
                    <em>会话加密保存在这台 Mac</em>
                  </span>
                </li>
              </ol>
              <div className={styles.actions}>
                <Button
                  type="primary"
                  icon={<LinkOutlined />}
                  onClick={() => void login()}
                  loading={authorizing}
                  disabled={authorizing}
                >
                  {authorizing ? "等待观猹授权" : "使用观猹登录"}
                </Button>
                {authorizing ? (
                  <Button
                    icon={<CloseOutlined />}
                    onClick={() => void cancelLogin()}
                  >
                    取消等待
                  </Button>
                ) : null}
              </div>
            </>
          )}

          {error ? (
            <div className={styles.errorBanner} role="alert">
              <WarningOutlined aria-hidden="true" />
              <div>
                <strong>账号操作没有完成</strong>
                <span>{error}</span>
              </div>
            </div>
          ) : null}
        </section>

        <aside className={styles.privacyPanel}>
          <LockOutlined aria-hidden="true" />
          <div>
            <strong>本机资料保持在本机</strong>
            <p>登录不会上传聊天、文件、记忆、工作区内容或 Provider 密钥。</p>
          </div>
        </aside>
      </div>
    </main>
  );
}
