import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  betaAccessApi,
  type BetaAccessOperationResult,
  type BetaAccessSnapshot,
  type BetaAccessSnapshotResult,
} from "../../api/modules/betaAccess";
import {
  cloudAccountApi,
  type CloudAccountStatus,
} from "../../api/modules/cloudAccount";
import { prepareExternalNavigation } from "../../utils/openExternalUrl";
import styles from "./BetaAccessPanel.module.less";

const MAX_LOGIN_POLL_ATTEMPTS = 300;

interface GateProps {
  children: ReactNode;
}

function requestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `beta-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function usable(status: string): boolean {
  return status === "active" || status === "grace";
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message
    ? error.message
    : "内测访问请求暂时没有完成，请稍后重试。";
}

function mergeResult(
  previous: BetaAccessSnapshot,
  result: BetaAccessSnapshotResult,
): BetaAccessSnapshot {
  return {
    ...previous,
    ...result,
  };
}

function AccessForm({
  snapshot,
  actionLabel,
  accountConnected = false,
  onActivated,
}: {
  snapshot: BetaAccessSnapshot;
  actionLabel: string;
  accountConnected?: boolean;
  onActivated: (snapshot: BetaAccessSnapshot) => void;
}) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<{
    operationId: string;
    recoveryToken?: string;
  } | null>(null);
  const composingRef = useRef(false);

  const accept = useCallback(
    (result: BetaAccessOperationResult) => {
      if (result.status === "issuance_pending") {
        setPending({
          operationId: result.operation_id || "",
          recoveryToken: result.recovery_token,
        });
        return;
      }
      setPending(null);
      onActivated(mergeResult(snapshot, result));
    },
    [onActivated, snapshot],
  );

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (composingRef.current || !code.length || busy) return;
    setBusy(true);
    setError("");
    try {
      accept(
        await betaAccessApi.activate({
          code,
          request_id: requestId(),
        }),
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const issueExisting = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      accept(await betaAccessApi.issue({ request_id: requestId() }));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const preventImeSubmit = (event: KeyboardEvent<HTMLInputElement>) => {
    if (
      event.key === "Enter" &&
      (composingRef.current || event.nativeEvent.isComposing)
    ) {
      event.preventDefault();
    }
  };

  const recover = async () => {
    if (!pending?.operationId || busy) return;
    setBusy(true);
    setError("");
    try {
      accept(
        await betaAccessApi.recover(
          pending.operationId,
          pending.recoveryToken,
        ),
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={styles.form} onSubmit={submit}>
      <label className={styles.label} htmlFor="beta-invite-code">
        邀请码
      </label>
      <input
        id="beta-invite-code"
        className={styles.input}
        value={code}
        onChange={(event) => setCode(event.target.value)}
        onCompositionStart={() => {
          composingRef.current = true;
        }}
        onCompositionEnd={() => {
          composingRef.current = false;
        }}
        onKeyDown={preventImeSubmit}
        autoComplete="off"
        spellCheck={false}
        disabled={busy || Boolean(pending)}
      />
      <p className={styles.hint}>
        {accountConnected
          ? "将绑定当前澜台账号与这台 Mac，仅需兑换一次。"
          : "无需登录；直接兑换会绑定到这台 Mac。"}
      </p>
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
      {pending ? (
        <button
          className={styles.primaryButton}
          type="button"
          onClick={() => void recover()}
          disabled={busy}
        >
          {busy ? "正在恢复" : "恢复签发结果"}
        </button>
      ) : (
        <div className={styles.formActions}>
          <button
            className={styles.primaryButton}
            type="submit"
            disabled={busy || !code.length}
          >
            {busy ? "正在核验" : actionLabel}
          </button>
          {accountConnected ? (
            <button
              className={styles.secondaryButton}
              type="button"
              onClick={() => void issueExisting()}
              disabled={busy}
            >
              恢复已有访问资格
            </button>
          ) : null}
        </div>
      )}
    </form>
  );
}

function useGateAccount(enabled: boolean) {
  const [account, setAccount] = useState<CloudAccountStatus | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [authorizing, setAuthorizing] = useState(false);
  const [attemptId, setAttemptId] = useState("");
  const [error, setError] = useState("");
  const generationRef = useRef(0);

  const load = useCallback(async () => {
    if (!enabled) return;
    const generation = ++generationRef.current;
    setLoading(true);
    setError("");
    try {
      const next = await cloudAccountApi.status();
      if (generation === generationRef.current) setAccount(next);
    } catch (caught) {
      if (generation === generationRef.current) setError(errorMessage(caught));
    } finally {
      if (generation === generationRef.current) setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    if (enabled) void load();
    return () => {
      generationRef.current += 1;
    };
  }, [enabled, load]);

  const login = useCallback(async () => {
    const generation = ++generationRef.current;
    setAuthorizing(true);
    setAttemptId("");
    setError("");
    const navigation = prepareExternalNavigation();
    try {
      if (!navigation) throw new Error("浏览器阻止了授权窗口，请允许后重试");
      navigation.setTitle("澜台账号登录");
      const started = await cloudAccountApi.startWatcha();
      if (generation !== generationRef.current) return;
      setAttemptId(started.attempt_id);
      await navigation.open(started.authorization_url);
      const interval = Math.max(1, started.poll_interval_seconds) * 1000;
      for (let attempt = 0; attempt < MAX_LOGIN_POLL_ATTEMPTS; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, interval));
        if (generation !== generationRef.current) return;
        const next = await cloudAccountApi.pollWatcha(started.attempt_id);
        if (next.status === "pending") continue;
        setAccount(next);
        setAttemptId("");
        return;
      }
      throw new Error("授权页等待超时，请重新登录");
    } catch (caught) {
      navigation?.close();
      if (generation === generationRef.current) {
        setError(errorMessage(caught));
        setAttemptId("");
      }
    } finally {
      if (generation === generationRef.current) setAuthorizing(false);
    }
  }, []);

  const cancel = useCallback(async () => {
    generationRef.current += 1;
    const activeAttempt = attemptId;
    setAttemptId("");
    setAuthorizing(false);
    setError("");
    if (!activeAttempt) return;
    try {
      await cloudAccountApi.cancelWatcha(activeAttempt);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, [attemptId]);

  return { account, loading, authorizing, error, load, login, cancel };
}

function GateAccountStep({
  snapshot,
  onActivated,
}: {
  snapshot: BetaAccessSnapshot;
  onActivated: (snapshot: BetaAccessSnapshot) => void;
}) {
  const { account, loading, authorizing, error, load, login, cancel } =
    useGateAccount(true);
  const connected = Boolean(account?.connected && account.user);

  if (loading && !account) return <LoadingState />;
  if (connected) {
    const identity = account?.user?.displayName || `UID ${account?.user?.uid ?? "—"}`;
    return (
      <>
        <p className={styles.accountLine}>当前账号：{identity}</p>
        <AccessForm
          snapshot={snapshot}
          actionLabel="进入澜台"
          accountConnected
          onActivated={onActivated}
        />
      </>
    );
  }

  return (
    <div className={styles.accountStep}>
      <p>可以直接绑定这台 Mac；如需把邀请码对应到账号，可先登录。</p>
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.formActions}>
        <button
          className={styles.primaryButton}
          type="button"
          onClick={() => void login()}
          disabled={authorizing}
        >
          {authorizing ? "等待浏览器确认" : "登录澜台账号"}
        </button>
        {authorizing ? (
          <button
            className={styles.secondaryButton}
            type="button"
            onClick={() => void cancel()}
          >
            取消等待
          </button>
        ) : error ? (
          <button
            className={styles.secondaryButton}
            type="button"
            onClick={() => void load()}
          >
            重新读取账号
          </button>
        ) : null}
      </div>
      <AccessForm
        snapshot={snapshot}
        actionLabel="进入澜台"
        onActivated={onActivated}
      />
    </div>
  );
}

function useLocalSnapshot() {
  const [snapshot, setSnapshot] = useState<BetaAccessSnapshot | null>(null);
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    setLoadError("");
    try {
      setSnapshot(await betaAccessApi.status());
    } catch (caught) {
      setLoadError(errorMessage(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return { snapshot, setSnapshot, loadError, load };
}

function LoadingState() {
  return (
    <div className={styles.quietState} role="status">
      正在核对本机许可
    </div>
  );
}

export function BetaAccessGate({ children }: GateProps) {
  const { snapshot, setSnapshot, loadError, load } = useLocalSnapshot();

  if (snapshot && (!snapshot.enforcement_required || usable(snapshot.status))) {
    return <>{children}</>;
  }
  if (!snapshot && !loadError) return <LoadingState />;

  return (
    <main className={styles.gate}>
      <section className={styles.gateCard} aria-labelledby="beta-gate-title">
        <span className={styles.eyebrow}>澜台内测</span>
        <h1 id="beta-gate-title">欢迎回来</h1>
        <p className={styles.lede}>
          首次使用需要兑换邀请码。完成后，澜台会在本机离线核验访问许可。
        </p>
        {loadError && !snapshot ? (
          <div className={styles.errorBlock} role="alert">
            <p>{loadError}</p>
            <button type="button" onClick={() => void load()}>
              重新读取
            </button>
          </div>
        ) : snapshot ? (
          <GateAccountStep
            snapshot={snapshot}
            onActivated={setSnapshot}
          />
        ) : null}
      </section>
    </main>
  );
}

function formatDate(value: number | null): string {
  if (!value) return "—";
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}

export function BetaAccessManager() {
  const { snapshot, setSnapshot, loadError, load } = useLocalSnapshot();

  if (!snapshot && !loadError) return <LoadingState />;
  return (
    <section className={styles.manager} aria-labelledby="beta-manager-title">
      <header>
        <span className={styles.eyebrow}>高级信息</span>
        <h2 id="beta-manager-title">内测访问管理</h2>
        <p>这里只显示本机许可状态，不展示许可证正文或设备私钥。</p>
      </header>
      {loadError && !snapshot ? (
        <div className={styles.errorBlock} role="alert">
          <p>{loadError}</p>
          <button type="button" onClick={() => void load()}>
            重新读取
          </button>
        </div>
      ) : snapshot && usable(snapshot.status) ? (
        <dl className={styles.details}>
          <div>
            <dt>本机状态</dt>
            <dd>{snapshot.status === "grace" ? "离线宽限中" : "可用"}</dd>
          </div>
          <div>
            <dt>建议续期时间</dt>
            <dd>{formatDate(snapshot.renew_by)}</dd>
          </div>
          <div>
            <dt>最终截止时间</dt>
            <dd>{formatDate(snapshot.expires_at)}</dd>
          </div>
          <div>
            <dt>本地决策序号</dt>
            <dd>{snapshot.decision_seq}</dd>
          </div>
        </dl>
      ) : snapshot ? (
        <AccessForm
          snapshot={snapshot}
          actionLabel="激活本机"
          onActivated={setSnapshot}
        />
      ) : null}
    </section>
  );
}
