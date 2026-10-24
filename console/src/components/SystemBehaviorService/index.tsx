import { useEffect, useRef } from "react";
import { chatApi } from "../../api/modules/chat";
import { settingsApi } from "../../api/modules/settings";

const ACTIVE_POLL_INTERVAL_MS = 5_000;
const IDLE_POLL_INTERVAL_MS = 60_000;

export default function SystemBehaviorService() {
  const runningChatsRef = useRef<
    Map<string, { title: string; chatId?: string }>
  >(new Map());
  const initializedRef = useRef(false);

  useEffect(() => {
    const desktop = window.electronAPI;
    if (!desktop?.setTaskActivity || !desktop.notifySystemBehavior) return;

    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let pollInFlight = false;

    const syncPreferences = async () => {
      try {
        const preferences = await settingsApi.getSystemBehaviorSettings();
        if (!disposed) {
          await desktop.updateSystemBehavior?.(preferences);
        }
      } catch {
        // The backend can still be warming up during the first desktop frame.
      }
    };

    const poll = async () => {
      if (disposed || pollInFlight) return;
      pollInFlight = true;
      try {
        const chats = await chatApi.listChatActivity();
        if (disposed) return;
        const previous = runningChatsRef.current;
        const current = new Map<string, { title: string; chatId?: string }>();

        for (const chat of chats) {
          const title =
            String(chat.name || "未命名任务").trim() || "未命名任务";
          const isCron = chat.kind === "cron";
          const sourceId = `${isCron ? "cron" : "chat"}:${chat.id}`;
          current.set(sourceId, {
            title,
            chatId: isCron ? undefined : chat.id,
          });
          if (!previous.has(sourceId)) {
            await desktop.setTaskActivity?.({
              sourceId,
              running: true,
            });
          }
        }

        for (const [sourceId, { title, chatId }] of previous) {
          if (current.has(sourceId)) continue;
          await desktop.setTaskActivity?.({
            sourceId,
            running: false,
          });
          // Cron owns its settled receipt; only foreground chats notify here.
          if (initializedRef.current && chatId !== undefined) {
            await desktop.notifySystemBehavior?.({
              id: `turn-completed:${chatId}:${Date.now()}`,
              type: "turn_completed",
              title: "澜台已完成一轮任务",
              body: title,
            });
          }
        }

        runningChatsRef.current = current;
        initializedRef.current = true;
      } catch {
        // Polling is best-effort and must never disrupt the chat surface.
      } finally {
        pollInFlight = false;
        if (!disposed) {
          const delay =
            runningChatsRef.current.size > 0
              ? ACTIVE_POLL_INTERVAL_MS
              : IDLE_POLL_INTERVAL_MS;
          timer = setTimeout(() => void poll(), delay);
        }
      }
    };

    void syncPreferences();
    void poll();

    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      for (const sourceId of runningChatsRef.current.keys()) {
        void desktop.setTaskActivity?.({
          sourceId,
          running: false,
        });
      }
      runningChatsRef.current.clear();
    };
  }, []);

  return null;
}
