import { request } from "../request";
import type {
  ReminderCreateRequest,
  ReminderItem,
  ReminderStatus,
  ReminderUpdateRequest,
  ReminderSettings,
  NativeReminderPayload,
  NativeReminderReceipt,
  TodayBrief,
  WatchTopic,
  WatchTopicCreateRequest,
  WatchTopicBriefUseRequest,
  WatchTopicUpdateRequest,
} from "../types";

const reminderPath = (path = "") => `/reminders${path}`;

export const reminderApi = {
  getSettings: () => request<ReminderSettings>(reminderPath("/settings")),
  setSettings: (settings: ReminderSettings) =>
    request<ReminderSettings>(reminderPath("/settings"), {
      method: "PATCH", body: JSON.stringify(settings),
    }, { operation: "mutation", timeoutMs: 3000 }),
  claimNative: (callerId: string, headers: Record<string, string>) =>
    request<NativeReminderPayload | null>(reminderPath("/native/claim"), {
      method: "POST", headers, body: JSON.stringify({ caller_id: callerId }),
    }, { operation: "mutation", timeoutMs: 3000 }),
  resolveNative: (receipt: NativeReminderReceipt, headers: Record<string, string>) =>
    request<{ ok: boolean }>(reminderPath("/native/resolve"), {
      method: "POST", headers, body: JSON.stringify(receipt),
    }, { operation: "mutation", timeoutMs: 3000 }),
  listReminders: (status?: ReminderStatus) => {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<ReminderItem[]>(reminderPath(query));
  },

  createReminder: (payload: ReminderCreateRequest) =>
    request<ReminderItem>(reminderPath(), {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  updateReminder: (reminderId: string, payload: ReminderUpdateRequest) =>
    request<ReminderItem>(
      reminderPath(`/${encodeURIComponent(reminderId)}`),
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    ),

  markReminderRead: (reminderId: string) =>
    request<ReminderItem>(
      reminderPath(`/${encodeURIComponent(reminderId)}/read`),
      { method: "POST" },
    ),

  snoozeReminder: (reminderId: string, until: string) =>
    request<ReminderItem>(
      reminderPath(`/${encodeURIComponent(reminderId)}/snooze`),
      {
        method: "POST",
        body: JSON.stringify({ until }),
      },
    ),

  dismissReminder: (reminderId: string) =>
    request<ReminderItem>(
      reminderPath(`/${encodeURIComponent(reminderId)}/dismiss`),
      { method: "POST" },
    ),

  completeReminder: (reminderId: string) =>
    request<ReminderItem>(
      reminderPath(`/${encodeURIComponent(reminderId)}/done`),
      { method: "POST" },
    ),

  getTodayBrief: () => request<TodayBrief>(reminderPath("/brief/today")),

  listWatchTopics: () =>
    request<WatchTopic[]>(reminderPath("/watch-topics")),

  createWatchTopic: (payload: WatchTopicCreateRequest) =>
    request<WatchTopic>(reminderPath("/watch-topics"), {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  updateWatchTopic: (topicId: string, payload: WatchTopicUpdateRequest) =>
    request<WatchTopic>(
      reminderPath(`/watch-topics/${encodeURIComponent(topicId)}`),
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    ),

  configureWatchTopicForBrief: (
    topicId: string,
    payload: WatchTopicBriefUseRequest,
  ) =>
    request<WatchTopic>(
      reminderPath(`/watch-topics/${encodeURIComponent(topicId)}/brief-use`),
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
      { operation: "mutation" },
    ),

  deleteWatchTopic: (topicId: string) =>
    request<{ ok: boolean }>(
      reminderPath(`/watch-topics/${encodeURIComponent(topicId)}`),
      { method: "DELETE" },
    ),
};
