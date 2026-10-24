import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { reminderApi } from "@/api/modules/reminder";
import { buildAuthHeaders, encodeHeaderValue } from "@/api/authHeaders";
import type { NativeReminderIdentity, NativeReminderReceipt } from "@/api/types";
import { useAgentStore } from "@/stores/agentStore";

const IDENTITY_KEYS = ["run_id", "attempt_id", "agent_id", "reminder_id", "caller_id", "receipt_token"] as const;
const POLL_INTERVAL_MS = 2000;

export default function ReminderNotificationService() {
  const navigate = useNavigate();
  const selectedAgent = useAgentStore((state) => state.selectedAgent);

  useEffect(() => {
    const desktop = window.electronAPI;
    if (!desktop?.notifySystemBehavior) return;
    const callerId = crypto.randomUUID();
    // Capture the original authority; request() must not fill in a later agent's grant.
    const headers = { ...buildAuthHeaders() };
    headers["X-Agent-Id"] = encodeHeaderValue(selectedAgent);
    headers["X-Workspace-Grant-Id"] ||= "";
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = desktop.onReminderOpen?.(({ reminderId, agentId }) => {
      if (disposed || !reminderId || !agentId) return;
      useAgentStore.getState().setSelectedAgent(agentId);
      navigate("/reminders?reminder=" + encodeURIComponent(reminderId));
    });

    const poll = async () => {
      try {
        if (disposed || document.hasFocus()) return;
        const payload = await reminderApi.claimNative(callerId, headers);
        if (!payload || payload.agent_id !== selectedAgent ||
            payload.caller_id !== callerId || IDENTITY_KEYS.some((key) => !payload[key])) return;
        const identity: NativeReminderIdentity = {
          run_id: payload.run_id, attempt_id: payload.attempt_id,
          agent_id: payload.agent_id, reminder_id: payload.reminder_id,
          caller_id: payload.caller_id, receipt_token: payload.receipt_token,
        };
        let receipt: NativeReminderReceipt = { ...identity, id: payload.run_id,
          ok: false, shown: false, status: "unknown", reason: "native_confirmation_unknown", error: "" };
        if (disposed || document.hasFocus()) {
          await reminderApi.resolveNative({ ...receipt, status: "failed",
            reason: "desktop_not_submitted" }, headers);
          return;
        }
        try {
          const result = await desktop.notifySystemBehavior!({
            ...payload, id: payload.run_id, type: "reminder",
          });
          const bound = result.id === payload.run_id &&
            IDENTITY_KEYS.every((key) => result[key] === identity[key]);
          if (bound && result.ok === true && result.shown === true && result.status === "shown") {
            receipt = { ...receipt, ok: true, shown: true, status: "shown", reason: "" };
          } else if (bound && result.shown !== true && result.status === "failed") {
            receipt = { ...receipt, status: "failed", reason: result.reason || "native_refused", error: result.error || "" };
          }
        } catch {
          // Crossing IPC without a receipt leaves the durable effect unknown.
        }
        // A shown effect still belongs to this agent after unmount or agent switching.
        await reminderApi.resolveNative(receipt, headers);
      } catch {
        // Backend warmup and unavailable receipts do not interrupt active work.
      } finally {
        if (!disposed) timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
      }
    };
    void poll();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      unsubscribe?.();
    };
  }, [navigate, selectedAgent]);
  return null;
}
