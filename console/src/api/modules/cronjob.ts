import { request } from "../request";
import type {
  CronJobSpecInput,
  CronJobSpecOutput,
  CronJobView,
  CronOccurrenceWindow,
} from "../types";

export const cronJobApi = {
  listCronJobs: () => request<CronJobSpecOutput[]>("/cron/jobs"),

  listCronOccurrences: (
    startAt: string,
    endAt: string,
    maxItems = 2000,
  ) => {
    const query = new URLSearchParams({
      start_at: startAt,
      end_at: endAt,
      max_items: String(maxItems),
    });
    return request<CronOccurrenceWindow>(`/cron/jobs/occurrences?${query}`);
  },

  createCronJob: (spec: CronJobSpecInput) =>
    request<CronJobSpecOutput>("/cron/jobs", {
      method: "POST",
      body: JSON.stringify(spec),
    }),

  getCronJob: (jobId: string) =>
    request<CronJobView>(`/cron/jobs/${encodeURIComponent(jobId)}`),

  replaceCronJob: (jobId: string, spec: CronJobSpecInput) =>
    request<CronJobSpecOutput>(`/cron/jobs/${encodeURIComponent(jobId)}`, {
      method: "PUT",
      body: JSON.stringify(spec),
    }),

  deleteCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
    }),

  pauseCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}/pause`, {
      method: "POST",
    }),

  resumeCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}/resume`, {
      method: "POST",
    }),

  runCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}/run`, {
      method: "POST",
    }),

  triggerCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}/run`, {
      method: "POST",
    }),

  getCronJobState: (jobId: string) =>
    request<unknown>(`/cron/jobs/${encodeURIComponent(jobId)}/state`),
};
