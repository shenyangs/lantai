export interface WebSearchStatus {
  configured: boolean;
  enabled: boolean;
  provider: "deepseek" | "bing_rss";
  search_count: number;
  fetch_count: number;
  last_used_at: string;
  migrated_from_quick_search: boolean;
  credential_error: boolean;
}

export interface WebSearchUpdate {
  api_key: string;
  enabled: boolean;
}
