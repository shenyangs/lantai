import { createGlobalStyle } from "antd-style";
import {
  Button,
  ConfigProvider,
  bailianDarkTheme,
  bailianTheme,
} from "@/vendor/lantaiDesign";
import { App as AntdApp } from "antd";
import { HashRouter, Routes, Route, Navigate } from "react-router-dom";
import { Component, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import zhCN from "antd/locale/zh_CN";
import enUS from "antd/locale/en_US";
import jaJP from "antd/locale/ja_JP";
import ruRU from "antd/locale/ru_RU";
import type { Locale } from "antd/es/locale";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import "dayjs/locale/zh-cn";
import "dayjs/locale/ja";
import "dayjs/locale/ru";
dayjs.extend(relativeTime);
import MainLayout from "./layouts/MainLayout";
import { ThemeProvider, useTheme } from "./contexts/ThemeContext";
import { PluginProvider, usePlugins } from "./plugins/PluginContext";
import { ApprovalProvider } from "./contexts/ApprovalContext";
import { Suspense } from "react";
import { lazyImportWithRetry } from "./utils/lazyWithRetry";

const LoginPage = lazyImportWithRetry("./pages/Login/index");
const BrowserPairPage = lazyImportWithRetry("./pages/BrowserPair/index");
import { authApi, AuthStatusError } from "./api/modules/auth";
import { languageApi } from "./api/modules/language";
import { getApiUrl, getApiToken, clearAuthToken } from "./api/config";
import { getLantaiAntdTheme } from "./theme/antdTheme";
import AppBootState from "./components/AppBootState";
import AppState from "./components/AppState";
import {
  createSafeCrashContext,
  type SafeCrashContext,
} from "./components/crashRecovery";
import { CrashRecoveryPanel } from "./components/CrashRecoveryPanel";
import "./styles/index.css";

const antdLocaleMap: Record<string, Locale> = {
  zh: zhCN,
  en: enUS,
  ja: jaJP,
  ru: ruRU,
};

const dayjsLocaleMap: Record<string, string> = {
  zh: "zh-cn",
  en: "en",
  ja: "ja",
  ru: "ru",
};

const GlobalStyle = createGlobalStyle`
* {
  margin: 0;
  box-sizing: border-box;
}
`;

function AppBootLoading() {
  return (
    <div className="lt-app-viewport">
      <AppBootState />
    </div>
  );
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      reject(new Error("Request timed out"));
    }, timeoutMs);

    promise
      .then(resolve)
      .catch(reject)
      .finally(() => window.clearTimeout(timer));
  });
}

async function withBoundedRetry<T>(
  operation: () => Promise<T>,
  options: { attempts: number; timeoutMs: number; retryDelayMs: number },
): Promise<T> {
  let latestError: unknown;
  for (let attempt = 0; attempt < options.attempts; attempt += 1) {
    try {
      return await withTimeout(operation(), options.timeoutMs);
    } catch (error) {
      latestError = error;
      if (attempt + 1 < options.attempts) {
        await new Promise<void>((resolve) => {
          window.setTimeout(resolve, options.retryDelayMs);
        });
      }
    }
  }
  throw latestError;
}

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const [authDataUnavailable, setAuthDataUnavailable] = useState(false);
  const [status, setStatus] = useState<
    "loading" | "auth-required" | "ok" | "error"
  >("loading");
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setAuthDataUnavailable(false);
    (async () => {
      try {
        // A freshly launched packaged backend can need a few seconds to finish
        // importing its first API route. Keep the check fail-closed, but do not
        // turn a healthy cold start into a permanent authentication error.
        const desktop = Boolean(window.electronAPI?.isDesktop);
        const authRequest = {
          attempts: desktop ? 2 : 1,
          timeoutMs: desktop ? 6000 : 3000,
          retryDelayMs: 350,
        };
        const res = await withBoundedRetry(
          () => authApi.getStatus(),
          authRequest,
        );
        if (cancelled) return;
        if (!res.enabled) {
          setStatus("ok");
          return;
        }
        const token = getApiToken();
        if (!token) {
          setStatus("auth-required");
          return;
        }
        try {
          const r = await withBoundedRetry(
            () =>
              fetch(getApiUrl("/auth/verify"), {
                headers: { Authorization: `Bearer ${token}` },
              }),
            authRequest,
          );
          if (cancelled) return;
          if (r.ok) {
            setStatus("ok");
          } else if (r.status === 401 || r.status === 403) {
            clearAuthToken();
            setStatus("auth-required");
          } else {
            setStatus("error");
          }
        } catch {
          if (!cancelled) {
            setStatus("error");
          }
        }
      } catch (error) {
        if (!cancelled) {
          setAuthDataUnavailable(
            error instanceof AuthStatusError && error.status === 503,
          );
          setStatus("error");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [retryKey]);

  if (status === "loading") return <AppBootLoading />;
  if (status === "auth-required")
    return (
      <Navigate
        to={`/login?redirect=${encodeURIComponent(window.location.pathname)}`}
        replace
      />
    );
  if (status === "error")
    return (
      <div className="lt-app-viewport">
        <AppState
          tone="error"
          title="无法确认访问权限"
          description={
            authDataUnavailable
              ? t("login.authDataUnavailable")
              : "澜台暂时无法连接认证服务。为保护本机数据，本次不会跳过身份校验。"
          }
          actions={
            <Button
              type="primary"
              onClick={() => {
                setStatus("loading");
                setRetryKey((value) => value + 1);
              }}
            >
              重新检查
            </Button>
          }
        />
      </div>
    );
  return <>{children}</>;
}

function getRouterBasename(pathname: string): string | undefined {
  return /^\/console(?:\/|$)/.test(pathname) ? "/console" : undefined;
}

class AppErrorBoundary extends Component<
  { children: React.ReactNode },
  { failed: boolean; context: SafeCrashContext | null }
> {
  state = { failed: false, context: null };

  componentDidCatch() {
    this.setState({
      failed: true,
      context: createSafeCrashContext("app-render"),
    });
  }

  render() {
    if (this.state.failed) {
      return (
        <div className="lt-app-viewport">
          <AppState
            tone="error"
            title="工作台遇到问题"
            description="界面未能正常显示。当前仅能确认显示中断，不能由此判定原操作是否生效。"
            actions={
              this.state.context ? (
                <CrashRecoveryPanel
                  context={this.state.context}
                  onRetry={() =>
                    this.setState({ failed: false, context: null })
                  }
                  onReload={() => window.location.reload()}
                />
              ) : null
            }
          />
        </div>
      );
    }
    return this.props.children;
  }
}

function AppInner() {
  const basename = getRouterBasename(window.location.pathname);
  const { i18n } = useTranslation();
  const { isDark, appearancePalette } = useTheme();
  const { loading: pluginsLoading } = usePlugins();
  const selectedTheme = isDark ? bailianDarkTheme : bailianTheme;
  const lang = i18n.resolvedLanguage || i18n.language || "en";
  const [antdLocale, setAntdLocale] = useState<Locale>(
    antdLocaleMap[lang] ?? enUS,
  );

  useEffect(() => {
    if (!localStorage.getItem("language")) {
      languageApi
        .getLanguage()
        .then(({ language }) => {
          if (language && language !== i18n.language) {
            i18n.changeLanguage(language);
            localStorage.setItem("language", language);
          }
        })
        .catch((err) =>
          console.error("Failed to fetch language preference:", err),
        );
    }
  }, []);

  useEffect(() => {
    const handleLanguageChanged = (lng: string) => {
      const shortLng = lng.split("-")[0];
      setAntdLocale(antdLocaleMap[shortLng] ?? enUS);
      dayjs.locale(dayjsLocaleMap[shortLng] ?? "en");
    };

    // Set initial dayjs locale
    dayjs.locale(dayjsLocaleMap[lang.split("-")[0]] ?? "en");

    i18n.on("languageChanged", handleLanguageChanged);
    return () => {
      i18n.off("languageChanged", handleLanguageChanged);
    };
  }, [i18n]);

  // Wait for plugins to load before rendering routes that might be patched
  if (pluginsLoading) {
    return <AppBootLoading />;
  }

  return (
    <HashRouter basename={basename}>
      <GlobalStyle />
      <ConfigProvider
        {...selectedTheme}
        prefix="lantai"
        prefixCls="lantai"
        locale={antdLocale}
        theme={getLantaiAntdTheme(
          isDark,
          (selectedTheme as any)?.theme,
          appearancePalette,
        )}
      >
        <AntdApp>
          <ApprovalProvider>
            <Routes>
              <Route
                path="/login"
                element={
                  <Suspense fallback={null}>
                    <LoginPage />
                  </Suspense>
                }
              />
              <Route
                path="/browser-pair"
                element={
                  <Suspense fallback={null}>
                    <BrowserPairPage />
                  </Suspense>
                }
              />
              <Route
                path="/*"
                element={
                  <AuthGuard>
                    <MainLayout />
                  </AuthGuard>
                }
              />
            </Routes>
          </ApprovalProvider>
        </AntdApp>
      </ConfigProvider>
    </HashRouter>
  );
}

function App() {
  return (
    <ThemeProvider>
      <PluginProvider>
        <AppErrorBoundary>
          <AppInner />
        </AppErrorBoundary>
      </PluginProvider>
    </ThemeProvider>
  );
}

export default App;
