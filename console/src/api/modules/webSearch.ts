import { request } from "../request";
import type { WebSearchStatus, WebSearchUpdate } from "../types/webSearch";

export const webSearchApi = {
  getConfig: () => request<WebSearchStatus>("/web-search/config"),

  updateConfig: (body: WebSearchUpdate) =>
    request<WebSearchStatus>("/web-search/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
