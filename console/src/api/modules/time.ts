import { request } from "../request";
import type { TodayTimeline } from "../types";

export const timeApi = {
  getToday: () => request<TodayTimeline>("/time/today"),
};
