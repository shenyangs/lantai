import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { dailyBriefApi } from "@/api/modules/dailyBrief";

const POLL_INTERVAL_MS = 60_000;

export default function DailyBriefNotificationService() {
  const navigate = useNavigate();

  useEffect(() => {
    const desktop = window.electronAPI;
    if (!desktop?.notifySystemBehavior || !desktop.onDailyBriefOpen) {
      return undefined;
    }

    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inFlight = false;

    const unsubscribe = desktop.onDailyBriefOpen(({ receiptId }) => {
      const id = String(receiptId || "").trim();
      if (!id || disposed) return;
      navigate(`/daily-brief?delivery=${encodeURIComponent(id)}`);
    });

    const poll = async () => {
      if (disposed || inFlight) return;
      inFlight = true;
      try {
        // Do not claim an outbox row while the user can already see the app.
        if (document.hasFocus()) return;
        const notification = await dailyBriefApi.claimNextNativeDelivery();
        if (!notification || disposed) return;
        let result;
        try {
          result = await desktop.notifySystemBehavior?.({
            id: `daily-brief:${notification.receipt_id}`,
            type: "daily_brief",
            receiptId: notification.receipt_id,
            title: notification.title,
            body: notification.body,
          });
        } catch {
          // The backend already persisted "unknown" before crossing IPC.
          return;
        }
        if (result?.ok === true && result.shown === true) {
          await dailyBriefApi.resolveNativeDelivery(notification.receipt_id, {
            state: "submitted",
            adapter_message_id: "electron-local",
            error: "",
          });
          return;
        }
        if (result?.reason === "duplicate" || result?.status === "unknown") {
          // A duplicate may already have reached the OS; keep unknown.
          return;
        }
        await dailyBriefApi.resolveNativeDelivery(notification.receipt_id, {
          state: "failed",
          error: result?.error || result?.reason || "native_notification_not_shown",
        });
      } catch {
        // Backend warmup and transient reads must not disturb the active UI.
      } finally {
        inFlight = false;
        if (!disposed) timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      unsubscribe?.();
    };
  }, [navigate]);

  return null;
}
