import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Form, Input } from "antd";
import { useAppMessage } from "../../hooks/useAppMessage";
import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { authApi, AuthStatusError } from "../../api/modules/auth";
import AppState from "../../components/AppState";
import { setAuthToken } from "../../api/config";
import styles from "./index.module.less";

export default function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [loading, setLoading] = useState(false);
  const [isRegister, setIsRegister] = useState(false);
  const [hasUsers, setHasUsers] = useState(true);
  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [statusError, setStatusError] = useState<unknown>(null);
  const [retryKey, setRetryKey] = useState(0);
  const { message } = useAppMessage();

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const timeoutMs = window.electronAPI?.isDesktop ? 6000 : 3000;
    const timer = window.setTimeout(() => {
      if (cancelled) return;
      cancelled = true;
      controller.abort();
      setStatusError(new Error("Authentication status request timed out"));
      setStatus("error");
    }, timeoutMs);
    setStatus("loading");
    setStatusError(null);
    authApi
      .getStatus(controller.signal)
      .then((res) => {
        if (cancelled) return;
        if (!res.enabled) {
          navigate("/chat", { replace: true });
          return;
        }
        setHasUsers(res.has_users);
        setIsRegister(!res.has_users);
        setStatus("ready");
      })
      .catch((error) => {
        if (cancelled) return;
        setStatusError(error);
        setStatus("error");
      })
      .finally(() => window.clearTimeout(timer));
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [navigate, retryKey]);

  const onFinish = async (values: { username: string; password: string }) => {
    if (status !== "ready") return;
    setLoading(true);
    try {
      const raw = searchParams.get("redirect") || "/chat";
      const redirect =
        raw.startsWith("/") && !raw.startsWith("//") ? raw : "/chat";

      if (isRegister) {
        const res = await authApi.register(values.username, values.password);
        if (res.token) {
          setAuthToken(res.token);
          message.success(t("login.registerSuccess"));
          navigate(redirect, { replace: true });
        }
      } else {
        const res = await authApi.login(values.username, values.password);
        if (res.token) {
          setAuthToken(res.token);
          navigate(redirect, { replace: true });
        } else {
          message.info(t("login.authNotEnabled"));
          navigate(redirect, { replace: true });
        }
      }
    } catch (err) {
      message.error(
        isRegister
          ? err instanceof Error
            ? err.message
            : t("login.registerFailed")
          : t("login.failed"),
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className={styles.page}>
      <section className={styles.panel} aria-labelledby="login-title">
        <div className={styles.identity}>
          <img src="/lantai-mark.png" alt="澜台" />
          <h1 id="login-title">
            {status === "ready" && isRegister
              ? t("login.registerTitle")
              : t("login.title")}
          </h1>
          {status === "ready" && !hasUsers && <p>{t("login.firstUserHint")}</p>}
        </div>

        {status === "loading" ? (
          <AppState tone="loading" title={t("login.statusChecking")} compact />
        ) : status === "error" ? (
          <AppState
            tone="error"
            title={t("login.statusCheckFailed")}
            description={
              statusError instanceof AuthStatusError &&
              statusError.status === 503
                ? t("login.authDataUnavailable")
                : t("login.statusCheckDescription")
            }
            actions={
              <Button
                type="primary"
                onClick={() => setRetryKey((value) => value + 1)}
              >
                {t("login.retryStatusCheck")}
              </Button>
            }
            compact
          />
        ) : (
          <Form
            layout="vertical"
            onFinish={onFinish}
            autoComplete="off"
            size="large"
          >
            <Form.Item
              name="username"
              rules={[{ required: true, message: t("login.usernameRequired") }]}
            >
              <Input
                prefix={<UserOutlined className={styles.fieldIcon} />}
                placeholder={t("login.usernamePlaceholder")}
                autoFocus
              />
            </Form.Item>

            <Form.Item
              name="password"
              rules={[{ required: true, message: t("login.passwordRequired") }]}
            >
              <Input.Password
                prefix={<LockOutlined className={styles.fieldIcon} />}
                placeholder={t("login.passwordPlaceholder")}
              />
            </Form.Item>

            <Form.Item className={styles.submitItem}>
              <Button
                type="primary"
                htmlType="submit"
                loading={loading}
                block
                className={styles.submitButton}
              >
                {isRegister ? t("login.register") : t("login.submit")}
              </Button>
            </Form.Item>
          </Form>
        )}
      </section>
    </main>
  );
}
