export type VibeCodingSignalKind = "preview" | "deployment";

export interface VibeCodingSignal {
  kind: VibeCodingSignalKind;
  url: string;
  label: string;
  summary: string;
}

const URL_RE = /\bhttps?:\/\/[^\s"'<>]+/gi;
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"]);
const DEPLOY_HOST_HINTS = [
  "vercel.app",
  "netlify.app",
  "pages.dev",
  "web.app",
  "firebaseapp.com",
  "cloudfront.net",
  "appspot.com",
  "cloudbase.net",
  "tcloudbase.com",
  "github.io",
];

function stringifySignalSource(value: unknown, depth = 0): string {
  if (depth > 3 || value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return value.map((item) => stringifySignalSource(item, depth + 1)).join("\n");
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return "";
}

function trimUrlPunctuation(value: string): string {
  return value.replace(/[),.;，。；）]+$/u, "");
}

export function extractUrls(...values: unknown[]): string[] {
  const seen = new Set<string>();
  const urls: string[] = [];
  for (const value of values) {
    const text = stringifySignalSource(value);
    for (const match of text.matchAll(URL_RE)) {
      const url = trimUrlPunctuation(match[0]);
      if (!url || seen.has(url)) continue;
      seen.add(url);
      urls.push(url);
    }
  }
  return urls;
}

export function isLocalPreviewUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return LOCAL_HOSTS.has(url.hostname.toLowerCase());
  } catch {
    return false;
  }
}

export function isDeploymentUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    if (isLocalPreviewUrl(value)) return false;
    if (url.protocol !== "https:") return false;
    return DEPLOY_HOST_HINTS.some((hint) => host === hint || host.endsWith(`.${hint}`));
  } catch {
    return false;
  }
}

export function shortUrlLabel(value: string): string {
  try {
    const url = new URL(value);
    const host = url.host;
    const path = url.pathname && url.pathname !== "/" ? url.pathname : "";
    const label = `${host}${path}`;
    return label.length > 72 ? `${label.slice(0, 71)}...` : label;
  } catch {
    return value.length > 72 ? `${value.slice(0, 71)}...` : value;
  }
}

export function buildVibeCodingSignal(options: {
  toolName?: string;
  args?: unknown;
  argsText?: string;
  resultText?: string;
}): VibeCodingSignal | null {
  const toolName = String(options.toolName || "").toLowerCase();
  const combinedText = [
    toolName,
    stringifySignalSource(options.args),
    options.argsText || "",
    options.resultText || "",
  ].join("\n");
  const intentText = [
    toolName,
    stringifySignalSource(options.args),
    options.argsText || "",
  ].join("\n");
  const urls = extractUrls(combinedText);
  const previewUrl = urls.find(isLocalPreviewUrl);
  if (previewUrl) {
    const label = shortUrlLabel(previewUrl);
    return {
      kind: "preview",
      url: previewUrl,
      label,
      summary: `预览地址 · ${label}`,
    };
  }

  const deployIntentText = intentText.replace(URL_RE, " ");
  const deployHint =
    /\b(deploy|release|vercel|wrangler|firebase|cloudbase|tcb)\b/i.test(
      deployIntentText,
    );
  const deploymentUrl = urls.find(isDeploymentUrl);
  if (deploymentUrl && (deployHint || toolName.includes("deploy"))) {
    const label = shortUrlLabel(deploymentUrl);
    return {
      kind: "deployment",
      url: deploymentUrl,
      label,
      summary: `部署链接 · ${label}`,
    };
  }

  return null;
}
