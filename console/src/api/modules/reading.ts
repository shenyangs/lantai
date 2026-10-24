import { request } from "../request";
import type { WikiSourceUploadResult } from "./workspace";

export type ReadingFeed = {
  entry_count?: number;
  id: string;
  url: string;
  title: string;
  folder: string;
  paused: boolean;
  checked_at: number | null;
  error: string;
  unread: number;
};
export type ReadingEntry = {
  link_kind?: "article" | "feed";
  source_feed?: string;
  id: string;
  url: string;
  title: string;
  body?: string;
  excerpt?: string;
  body_kind: "none" | "summary" | "feed" | "page";
  published: string;
  created_at: number;
  fetched_at: number | null;
  is_read: boolean;
  starred: boolean;
  tags: string[];
  archived_at?: number | null;
};
export type ReadingChannel = {
  id: string;
  site: string;
  name: string;
  path: string;
  description: string;
  example: string | string[];
  docs: string;
  categories: string[];
  fields: {
    name: string;
    optional: boolean;
    description: string;
    default: string;
    options: { label: string; value: string }[];
  }[];
  features: {
    requireConfig?: boolean | { name: string; optional?: boolean }[];
    requirePuppeteer?: boolean;
    antiCrawler?: boolean;
  };
};
export type ChannelCatalog = {
  items: ReadingChannel[];
  total: number;
  catalog_total: number;
  categories: string[];
  snapshot: {
    generated_at: string;
    source_revision: string;
    route_count: number;
  };
};
export type RSSHubSettings = {
  mode?: "local" | "external";
  instance_url: string;
  allow_private: boolean;
};
export type ReadingLocalStatus = {
  state: string;
  installed: boolean;
  running: boolean;
  message: string;
  progress: number | null;
  version: string;
  node_version: string;
  browser_available: boolean;
  configured_keys?: string[];
};
export type ChannelSelection = {
  parameters: Record<string, string>;
  folder: string;
  path_override: string;
};
const post = <T>(path: string, body?: unknown) =>
  request<T>(`/reading${path}`, {
    method: "POST",
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
export const readingApi = {
  localStatus: () => request<ReadingLocalStatus>("/reading/local"),
  localAction: (action: "prepare" | "browser" | "start" | "stop") =>
    post<ReadingLocalStatus>("/local", { action }),
  localConfiguration: (values: Record<string, string>) =>
    request<{ configured_keys: string[] }>("/reading/local/config", {
      method: "PUT",
      body: JSON.stringify({ values }),
    }),
  channels: (params: Record<string, string>) =>
    request<ChannelCatalog>(`/reading/channels?${new URLSearchParams(params)}`),
  rsshubSettings: () => request<RSSHubSettings>("/reading/rsshub"),
  saveRSSHubSettings: (settings: RSSHubSettings) =>
    request<RSSHubSettings>("/reading/rsshub", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  saveChannel: (values: { id?: string; name: string; path: string }) =>
    post<ReadingChannel>("/channels/custom", values),
  previewChannel: (id: string, selection: ChannelSelection) =>
    post<{ url: string }>(
      `/channels/${encodeURIComponent(id)}/preview`,
      selection,
    ),
  subscribeChannel: (id: string, selection: ChannelSelection) =>
    post<ReadingFeed>(
      `/channels/${encodeURIComponent(id)}/subscribe`,
      selection,
    ),
  feeds: () => request<ReadingFeed[]>("/reading/feeds"),
  subscribe: (url: string, folder: string) =>
    post<ReadingFeed>("/feeds", { url, folder }),
  refresh: (id: string) => post(`/feeds/${encodeURIComponent(id)}/refresh`),
  updateFeed: (id: string, values: { folder?: string; paused?: boolean }) =>
    request<ReadingFeed>(`/reading/feeds/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(values),
    }),
  unsubscribe: (id: string) =>
    request(`/reading/feeds/${encodeURIComponent(id)}`, { method: "DELETE" }),
  entries: (params: Record<string, string>) =>
    request<{ items: ReadingEntry[]; total: number }>(
      `/reading/entries?${new URLSearchParams(params)}`,
    ),
  entry: (id: string) =>
    request<ReadingEntry>(`/reading/entries/${encodeURIComponent(id)}`),
  save: (url: string) => post<ReadingEntry>("/entries", { url }),
  fetch: (id: string) =>
    post<ReadingEntry>(`/entries/${encodeURIComponent(id)}/fetch`),
  update: (
    id: string,
    values: { is_read?: boolean; starred?: boolean; tags?: string[] },
  ) =>
    request<ReadingEntry>(`/reading/entries/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(values),
    }),
  knowledge: (id: string) =>
    post<WikiSourceUploadResult>(
      `/entries/${encodeURIComponent(id)}/knowledge`,
    ),
  archiveEntries: (ids: string[]) =>
    post<{ archived: number }>("/entries/archive", { ids }),
  restoreEntry: (id: string) =>
    post<ReadingEntry>(`/entries/${encodeURIComponent(id)}/restore`),
  purgeEntry: (id: string) =>
    request<{ deleted: boolean }>(
      `/reading/entries/${encodeURIComponent(id)}`,
      {
        method: "DELETE",
      },
    ),
};
