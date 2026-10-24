import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import api from "@/api";
import type { ProviderInfo } from "@/api/types";
import { useAppMessage } from "@/hooks/useAppMessage";
import { prepareExternalNavigation } from "@/utils/openExternalUrl";

export type ProviderOAuthPhase =
  | "idle"
  | "opening"
  | "waiting"
  | "succeeded"
  | "failed";

interface UseProviderOAuthOptions {
  provider: ProviderInfo;
  onSaved: () => void | Promise<void>;
  onConnected?: () => void;
}

export function useProviderOAuth({
  provider,
  onSaved,
  onConnected,
}: UseProviderOAuthOptions) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [authorizing, setAuthorizing] = useState(false);
  const [phase, setPhase] = useState<ProviderOAuthPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const runRef = useRef(0);
  const inFlightRef = useRef(false);

  useEffect(
    () => () => {
      runRef.current += 1;
      inFlightRef.current = false;
    },
    [],
  );

  const authorize = useCallback(async () => {
    if (inFlightRef.current) return;

    const navigation = prepareExternalNavigation();
    if (!navigation) {
      const blockedMessage = t(
        "models.oauthPopupBlocked",
        "浏览器窗口被拦截，请允许弹窗后重试",
      );
      setPhase("failed");
      setError(blockedMessage);
      message.error(blockedMessage);
      return;
    }

    inFlightRef.current = true;
    const runId = ++runRef.current;
    setAuthorizing(true);
    setPhase("opening");
    setError(null);

    try {
      navigation.setTitle("TokenDance OAuth");
      const started = await api.startProviderOAuth(provider.id);
      if (runRef.current !== runId) {
        navigation.close();
        return;
      }

      await navigation.open(started.authorization_url);
      setPhase("waiting");
      message.info(
        t(
          "models.oauthCompleteInBrowser",
          "请在浏览器完成 TokenDance 授权；完成后澜台会自动刷新",
        ),
      );

      const attempts = Math.max(1, Math.ceil(started.expires_in / 1.2));
      for (let index = 0; index < attempts; index += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
        if (runRef.current !== runId) return;

        const status = await api.getProviderOAuthStatus(
          provider.id,
          started.flow_id,
        );
        if (status.status === "succeeded") {
          try {
            await api.discoverModels(provider.id, undefined, true);
          } catch {
            // Authorization is already usable. Catalog discovery can be
            // retried separately without making the user authorize again.
          }
          await onSaved();
          if (runRef.current !== runId) return;

          setPhase("succeeded");
          message.success(t("models.oauthConnected", "TokenDance 已安全连接"));
          onConnected?.();
          return;
        }
        if (status.status === "failed" || status.status === "expired") {
          throw new Error(
            status.message ||
              t("models.oauthFailed", "TokenDance 授权未完成，请重试"),
          );
        }
      }

      throw new Error(
        t("models.oauthExpired", "TokenDance 授权已超时，请重试"),
      );
    } catch (caughtError) {
      navigation.close();
      if (runRef.current === runId) {
        const errorMessage =
          caughtError instanceof Error
            ? caughtError.message
            : t("models.oauthFailed", "TokenDance 授权未完成，请重试");
        setPhase("failed");
        setError(errorMessage);
        message.error(errorMessage);
      }
    } finally {
      if (runRef.current === runId) {
        inFlightRef.current = false;
        setAuthorizing(false);
      }
    }
  }, [message, onConnected, onSaved, provider.id, t]);

  return { authorize, authorizing, phase, error };
}
