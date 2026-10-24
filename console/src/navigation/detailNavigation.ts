export interface DetailNavigation {
  to: string;
  label: string;
}

export const SETTINGS_DETAIL_NAVIGATION = {
  "/account": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/about": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/appearance": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/innovation-lab": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/system-behavior": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/office-documents": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/models": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/skill-pool": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/environments": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/browser-login": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/mcp": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/search-enhancement": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/sessions": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/heartbeat": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/advanced-parameters": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/terminal": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/agent-stats": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/backups": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/permissions": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/security": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/computer-permissions": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/debug": {
    to: "/settings",
    label: "返回设置中心",
  },
  // Compatibility routes that redirect into a settings detail page.
  "/agent-config": {
    to: "/settings",
    label: "返回设置中心",
  },
  "/token-usage": {
    to: "/settings",
    label: "返回设置中心",
  },
} as const satisfies Readonly<Record<string, DetailNavigation>>;

export type SettingsDetailPath = keyof typeof SETTINGS_DETAIL_NAVIGATION;

export function getDetailNavigation(pathname: string): DetailNavigation | null {
  return SETTINGS_DETAIL_NAVIGATION[pathname as SettingsDetailPath] ?? null;
}
