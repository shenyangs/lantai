import { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  Input,
  InputNumber,
  Modal,
  QRCode,
  Tag,
} from "@/vendor/lantaiDesign";
import {
  GiftOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  WalletOutlined,
} from "@ant-design/icons";
import api from "@/api";
import { ApiRequestError } from "@/api/request";
import type {
  ProviderInfo,
  TokenDanceBalance,
  TokenDancePaymentSession,
  TokenDanceUsage,
} from "@/api/types/provider";
import { useAppMessage } from "@/hooks/useAppMessage";
import { useProviderOAuth } from "../../useProviderOAuth";
import styles from "../../index.module.less";

interface Props {
  provider: ProviderInfo;
  connected: boolean;
  onSaved: () => void | Promise<void>;
}

const TERMINAL_STATUSES = new Set(["paid", "failed", "closed", "refunded"]);
const PAYMENT_STATUS_LABELS: Record<string, string> = {
  pending: "等待付款",
  paid: "已到账",
  failed: "支付失败",
  closed: "会话已关闭",
  refunded: "已退款",
};

export function TokenDanceAccountPanel({
  provider,
  connected,
  onSaved,
}: Props) {
  const { message } = useAppMessage();
  const {
    authorize,
    authorizing,
    phase: authorizationPhase,
    error: authorizationError,
  } = useProviderOAuth({ provider, onSaved });
  const [balance, setBalance] = useState<TokenDanceBalance | null>(null);
  const [usage, setUsage] = useState<TokenDanceUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [amount, setAmount] = useState<number | null>(100);
  const [creating, setCreating] = useState(false);
  const [payment, setPayment] = useState<TokenDancePaymentSession | null>(null);
  const [redemptionOpen, setRedemptionOpen] = useState(false);
  const [redemptionCode, setRedemptionCode] = useState("");
  const [redeeming, setRedeeming] = useState(false);
  const pollRef = useRef<number | null>(null);
  const pollGenerationRef = useRef(0);
  const accountRequestGenerationRef = useRef(0);
  const authorizationContextRef = useRef<string | null>(null);
  const paymentCreateInFlightRef = useRef(false);
  const redemptionInFlightRef = useRef(false);

  const createPaymentIntentId = () => {
    const randomId =
      globalThis.crypto?.randomUUID?.() ??
      `${Date.now()}_${Math.random().toString(36).slice(2)}`;
    return `tdpi_${randomId.replace(/[^A-Za-z0-9_-]/g, "")}`;
  };

  const stopPolling = useCallback(() => {
    pollGenerationRef.current += 1;
    if (pollRef.current !== null) {
      window.clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!connected) return;
    const generation = accountRequestGenerationRef.current + 1;
    accountRequestGenerationRef.current = generation;
    setLoading(true);
    try {
      const [nextBalance, nextUsage] = await Promise.all([
        api.getTokenDanceBalance(),
        api.getTokenDanceUsage(),
      ]);
      if (accountRequestGenerationRef.current !== generation) return;
      const nextContext = nextBalance.authorization_context_id;
      if (!nextContext || nextUsage.authorization_context_id !== nextContext) {
        authorizationContextRef.current = null;
        stopPolling();
        setRedemptionOpen(false);
        setRedemptionCode("");
        setBalance(null);
        setUsage(null);
        setPayment(null);
        message.error("账号授权已变化，已丢弃本次账户数据，请刷新。");
        return;
      }
      if (
        authorizationContextRef.current &&
        authorizationContextRef.current !== nextContext
      ) {
        stopPolling();
        setPayment(null);
        setRedemptionOpen(false);
        setRedemptionCode("");
      }
      authorizationContextRef.current = nextContext;
      setBalance(nextBalance);
      setUsage(nextUsage);
    } catch (error) {
      if (accountRequestGenerationRef.current !== generation) return;
      authorizationContextRef.current = null;
      stopPolling();
      setRedemptionOpen(false);
      setRedemptionCode("");
      setBalance(null);
      setUsage(null);
      setPayment(null);
      message.error(
        error instanceof Error ? error.message : "账户信息读取失败",
      );
    } finally {
      if (accountRequestGenerationRef.current === generation) {
        setLoading(false);
      }
    }
  }, [connected, message, stopPolling]);

  useEffect(() => {
    if (!connected) {
      accountRequestGenerationRef.current += 1;
      authorizationContextRef.current = null;
      stopPolling();
      setRedemptionOpen(false);
      setRedemptionCode("");
      setBalance(null);
      setUsage(null);
      setPayment(null);
      setLoading(false);
      return;
    }
    void refresh();
    return () => {
      accountRequestGenerationRef.current += 1;
      stopPolling();
    };
  }, [connected, refresh, stopPolling]);

  const pollPayment = useCallback(
    (session: TokenDancePaymentSession) => {
      if (
        !authorizationContextRef.current ||
        session.authorization_context_id !== authorizationContextRef.current
      ) {
        setPayment(null);
        message.error("账号授权已变化，充值会话未显示，请刷新后重试。");
        return;
      }
      stopPolling();
      if (TERMINAL_STATUSES.has(session.status)) return;

      const generation = pollGenerationRef.current;
      let currentSession = session;

      const scheduleNextPoll = () => {
        if (pollGenerationRef.current !== generation) return;
        pollRef.current = window.setTimeout(() => {
          pollRef.current = null;
          void pollOnce();
        }, 3000);
      };

      const pollOnce = async () => {
        if (pollGenerationRef.current !== generation) return;
        if (Date.now() >= currentSession.expired_at * 1000) {
          stopPolling();
          message.error("充值会话已过期，请重新发起充值");
          return;
        }
        try {
          const update = await api.getTokenDancePaymentSession(
            currentSession.id,
          );
          if (pollGenerationRef.current !== generation) return;
          if (
            !authorizationContextRef.current ||
            update.authorization_context_id !==
              authorizationContextRef.current ||
            update.authorization_context_id !==
              currentSession.authorization_context_id
          ) {
            stopPolling();
            setPayment(null);
            message.error("账号授权已变化，已停止查询充值结果。");
            return;
          }
          const next: TokenDancePaymentSession = {
            ...currentSession,
            ...update,
          };
          currentSession = next;
          setPayment(next);
          if (TERMINAL_STATUSES.has(next.status)) {
            stopPolling();
            if (next.status === "paid") {
              message.success("充值已到账");
              void refresh();
            }
          } else {
            scheduleNextPoll();
          }
        } catch {
          if (pollGenerationRef.current !== generation) return;
          stopPolling();
          message.error("充值状态查询失败，请刷新余额确认结果");
        }
      };

      scheduleNextPoll();
    },
    [message, refresh, stopPolling],
  );

  const createPayment = async () => {
    if (paymentCreateInFlightRef.current) return;
    if (!amount || amount < 1 || amount > 100000) return;
    const expectedContext = authorizationContextRef.current;
    if (!expectedContext) {
      message.error("请先刷新词元跳动账户信息，再发起充值。");
      return;
    }
    const generation = pollGenerationRef.current;
    paymentCreateInFlightRef.current = true;
    setCreating(true);
    try {
      const session = await api.createTokenDancePaymentSession(
        amount,
        createPaymentIntentId(),
      );
      if (pollGenerationRef.current !== generation) return;
      if (
        authorizationContextRef.current !== expectedContext ||
        session.authorization_context_id !== expectedContext
      ) {
        setPayment(null);
        message.error("账号授权已变化，充值会话未显示，请刷新后重试。");
        return;
      }
      setPayment(session);
      pollPayment(session);
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "充值会话创建失败",
      );
    } finally {
      paymentCreateInFlightRef.current = false;
      setCreating(false);
    }
  };

  const redeemCode = async () => {
    if (redemptionInFlightRef.current) return;
    const code = redemptionCode.trim();
    if (!code || code.length > 256) return;
    const expectedContext = authorizationContextRef.current;
    if (!expectedContext) {
      message.error("请先刷新词元跳动账户信息，再兑换。");
      return;
    }
    redemptionInFlightRef.current = true;
    setRedeeming(true);
    try {
      const result = await api.redeemTokenDanceCode(code);
      setRedemptionCode("");
      setRedemptionOpen(false);
      if (
        authorizationContextRef.current !== expectedContext ||
        result.authorization_context_id !== expectedContext
      ) {
        message.error("账号授权已变化，兑换结果请到 TokenDance 核对。");
        return;
      }
      message.success(`兑换成功，到账 ¥${result.credits_cny}`);
      void refresh();
    } catch (error) {
      const outcomeUnknown =
        error instanceof ApiRequestError &&
        (error.recoveryAction === "check-result" ||
          error.detail?.outcome_unknown === true ||
          error.httpStatus === 409 ||
          (error.httpStatus ?? 0) >= 500);
      if (outcomeUnknown) {
        setRedemptionCode("");
        setRedemptionOpen(false);
        message.error(
          "兑换结果暂时无法确认，请刷新余额或到 TokenDance 核对后再试。",
        );
      } else {
        message.error(
          error instanceof Error ? error.message : "兑换失败，请稍后重试。",
        );
      }
    } finally {
      redemptionInFlightRef.current = false;
      setRedeeming(false);
    }
  };

  if (!connected) {
    const authorizationWaiting =
      authorizationPhase === "opening" || authorizationPhase === "waiting";
    const authorizationFailed = authorizationPhase === "failed";

    return (
      <div
        className={`${styles.tokendanceAccountPanel} ${styles.tokendanceAuthorizationPanel}`}
        aria-live="polite"
      >
        <div className={styles.tokendanceAuthorizationIntro}>
          <span className={styles.tokendanceAuthorizationEyebrow}>
            <SafetyCertificateOutlined /> 还差一次账号授权
          </span>
          <strong>登录观猹账号，即可使用 TokenDance</strong>
          <p>
            {authorizationWaiting
              ? "授权页已打开，请在浏览器完成确认。"
              : "点击授权后会打开浏览器；完成后回到澜台，状态会自动刷新。"}
          </p>
          {authorizationFailed && authorizationError && (
            <p className={styles.tokendanceAuthorizationError} role="alert">
              {authorizationError}
            </p>
          )}
        </div>
        <div
          className={styles.tokendanceAuthorizationSteps}
          aria-label="TokenDance 授权步骤"
        >
          <span>
            <b>1</b>
            点击立即授权
          </span>
          <span>
            <b>2</b>
            浏览器登录确认
          </span>
          <span>
            <b>3</b>
            回到澜台自动完成
          </span>
        </div>
        <div className={styles.tokendanceAuthorizationAction}>
          <Button
            type="primary"
            icon={<SafetyCertificateOutlined />}
            loading={authorizing}
            onClick={() => void authorize()}
          >
            {authorizationFailed
              ? "重新授权 TokenDance"
              : authorizationWaiting
                ? "等待浏览器确认…"
                : "立即授权 TokenDance"}
          </Button>
          <small>无需复制 API Key，授权信息仅安全保存于本机</small>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className={styles.tokendanceAccountPanel}>
        <div className={styles.tokendanceBalanceBlock}>
          <span>可用余额</span>
          <strong>{balance ? `¥${balance.balance_cny}` : "—"}</strong>
          {balance && <small>累计消耗 ¥{balance.credits_used_cny}</small>}
        </div>
        <div className={styles.tokendanceUsageBlock}>
          <span>近 7 天调用</span>
          <strong>{usage ? `${usage.items.length} 条最近记录` : "—"}</strong>
          <small>
            {usage?.items[0]
              ? `${usage.items[0].model_id} · ¥${usage.items[0].cost_cny}`
              : "暂无用量记录"}
          </small>
        </div>
        <div className={styles.tokendanceAccountActions}>
          <Button
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void refresh()}
          >
            刷新
          </Button>
          <InputNumber
            min={1}
            max={100000}
            precision={0}
            addonBefore="¥"
            value={amount}
            onChange={(value) => setAmount(value)}
            aria-label="充值金额"
          />
          <Button
            type="primary"
            icon={<WalletOutlined />}
            loading={creating}
            disabled={redemptionOpen || redeeming}
            onClick={() => void createPayment()}
          >
            充值
          </Button>
          <Button
            icon={<GiftOutlined />}
            disabled={Boolean(payment) || creating || redeeming}
            onClick={() => {
              setRedemptionCode("");
              setRedemptionOpen(true);
            }}
          >
            兑换码
          </Button>
        </div>
      </div>

      <Modal
        open={Boolean(payment)}
        title="词元跳动充值"
        footer={null}
        destroyOnClose
        onCancel={() => {
          stopPolling();
          setPayment(null);
        }}
      >
        {payment && (
          <div className={styles.tokendancePaymentModal}>
            <QRCode value={payment.payment_url} size={220} />
            <strong>充值 ¥{payment.amount}</strong>
            <p>
              请使用手机扫码完成付款。付款结果会自动刷新，关闭窗口后也可手动刷新余额。
            </p>
            <Tag color={payment.status === "paid" ? "success" : "processing"}>
              {PAYMENT_STATUS_LABELS[payment.status] ?? payment.status}
            </Tag>
          </div>
        )}
      </Modal>

      <Modal
        open={redemptionOpen}
        title="词元跳动兑换码"
        footer={null}
        destroyOnClose
        maskClosable={false}
        closable={!redeeming}
        onCancel={() => {
          if (redemptionInFlightRef.current) return;
          setRedemptionCode("");
          setRedemptionOpen(false);
        }}
      >
        <div className={styles.tokendanceRedemptionModal}>
          <p>兑换额度将进入当前已授权的 TokenDance 账号。</p>
          <Input.Password
            aria-label="兑换码内容"
            autoComplete="off"
            maxLength={256}
            placeholder="输入兑换码"
            value={redemptionCode}
            onChange={(event) => setRedemptionCode(event.target.value)}
            onPressEnter={() => void redeemCode()}
          />
          <Button
            type="primary"
            loading={redeeming}
            disabled={!redemptionCode.trim() || redeeming}
            onClick={() => void redeemCode()}
          >
            确认兑换
          </Button>
        </div>
      </Modal>
    </>
  );
}
