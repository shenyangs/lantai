const crypto = require("crypto");
const childProcess = require("child_process");
const { normalizeConsoleMessage } = require("./consoleMessageDetails.cjs");
const dns = require("dns").promises;
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");

const BRIDGE_HOST = "127.0.0.1";
const TOOLBAR_HEIGHT = 118;
const MAX_EVENT_BUFFER = 200;
const MAX_BRIDGE_CAPABILITY_NONCES = 2048;
const SECURITY_SCOPED_BOOKMARK_STORE_SCHEMA_VERSION = 1;

function makeCapabilityMatrixEntry(
  id,
  mode,
  reasonCode,
  label,
  description = "",
) {
  return {
    id,
    mode,
    reason_code: reasonCode,
    label,
    description,
  };
}

const DEVELOPER_ID_BROWSER_BRIDGE_CAPABILITIES = [
  makeCapabilityMatrixEntry(
    "chat_runtime",
    "enabled",
    "developer_id_allowed",
    "Chat runtime",
  ),
  makeCapabilityMatrixEntry(
    "filesystem_read",
    "enabled",
    "developer_id_allowed",
    "Filesystem read",
  ),
  makeCapabilityMatrixEntry(
    "filesystem_write",
    "enabled",
    "developer_id_allowed",
    "Filesystem write",
  ),
  makeCapabilityMatrixEntry(
    "filesystem_external_grant",
    "enabled",
    "developer_id_allowed",
    "External file grant",
  ),
  makeCapabilityMatrixEntry(
    "browser_read_navigation",
    "enabled",
    "developer_id_allowed",
    "Browser read and navigation",
  ),
  makeCapabilityMatrixEntry(
    "browser_write",
    "interactive_only",
    "developer_id_browser_write_confirmation_required",
    "Browser write automation",
    "Browser writes require profile policy and confirmation when configured.",
  ),
  makeCapabilityMatrixEntry(
    "browser_upload_download",
    "interactive_only",
    "developer_id_browser_file_grant_required",
    "Browser upload and download",
    "File transfer requires explicit grants and broker validation.",
  ),
  makeCapabilityMatrixEntry(
    "shell_python_subprocess",
    "enabled",
    "developer_id_allowed",
    "Shell, Python, and subprocess tools",
  ),
  makeCapabilityMatrixEntry(
    "mcp_plugin_network",
    "enabled",
    "developer_id_allowed",
    "MCP, plugin, and network tools",
  ),
  makeCapabilityMatrixEntry(
    "background_scheduled_execution",
    "enabled",
    "developer_id_allowed",
    "Background and scheduled execution",
  ),
  makeCapabilityMatrixEntry(
    "persistent_resume",
    "enabled",
    "developer_id_allowed",
    "Persistent resume",
  ),
];

const APP_STORE_BROWSER_BRIDGE_CAPABILITIES = [
  makeCapabilityMatrixEntry(
    "chat_runtime",
    "enabled",
    "mas_chat_runtime_allowed",
    "Chat runtime",
  ),
  makeCapabilityMatrixEntry(
    "filesystem_read",
    "read_only",
    "mas_filesystem_read_container_only",
    "Filesystem read",
    "Reads are limited to app container data or user-granted locations.",
  ),
  makeCapabilityMatrixEntry(
    "filesystem_write",
    "interactive_only",
    "mas_filesystem_write_requires_user_grant",
    "Filesystem write",
    "Writes outside the container require an explicit user grant.",
  ),
  makeCapabilityMatrixEntry(
    "filesystem_external_grant",
    "interactive_only",
    "mas_security_scoped_bookmark_required",
    "External file grant",
    "External file access must be restored through a security-scoped bookmark.",
  ),
  makeCapabilityMatrixEntry(
    "browser_read_navigation",
    "read_only",
    "mas_browser_read_only",
    "Browser read and navigation",
    "MAS exposes only audited read-only browser surfaces.",
  ),
  makeCapabilityMatrixEntry(
    "browser_write",
    "disabled",
    "mas_browser_write_disabled",
    "Browser write automation",
    "MAS disables agentic browser writes.",
  ),
  makeCapabilityMatrixEntry(
    "browser_upload_download",
    "interactive_only",
    "mas_security_scoped_bookmark_required",
    "Browser upload and download",
    "MAS browser file transfer requires an active picker-backed security-scoped bookmark grant.",
  ),
  makeCapabilityMatrixEntry(
    "shell_python_subprocess",
    "disabled",
    "mas_subprocess_disabled",
    "Shell, Python, and subprocess tools",
  ),
  makeCapabilityMatrixEntry(
    "mcp_plugin_network",
    "interactive_only",
    "mas_mcp_plugin_network_limited",
    "MCP, plugin, and network tools",
    "MAS network/plugin access must stay user-visible and policy-gated.",
  ),
  makeCapabilityMatrixEntry(
    "background_scheduled_execution",
    "interactive_only",
    "mas_background_execution_limited",
    "Background and scheduled execution",
    "MAS scheduled execution must degrade to visible, user-controlled work.",
  ),
  makeCapabilityMatrixEntry(
    "persistent_resume",
    "interactive_only",
    "mas_persistent_resume_requires_bookmark_restore",
    "Persistent resume",
    "Persistent resume depends on restoring user grants after restart.",
  ),
];

const BROWSER_WRITE_ACTIONS = new Set([
  "click",
  "click_at",
  "drag",
  "fill_form",
  "hover",
  "select",
  "type",
]);

const BROWSER_FILE_TRANSFER_ACTIONS = new Set(["download_wait", "upload_file"]);
const BROWSER_NAVIGATION_ACTIONS = new Set([
  "new_tab",
  "navigate",
  "open",
  "open_workbench",
]);
const SIDECAR_RENDERER_CONTROL_ACTIONS = new Set([
  "close_tab",
  "go_back",
  "go_forward",
  "navigate",
  "new_tab",
  "open",
  "reload",
  "select_tab",
  "stop",
]);

function normalizeBridgeDistribution(value = "") {
  const raw = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[-\s]+/g, "_");
  if (["app_store", "appstore", "mas", "testflight"].includes(raw)) {
    return "app_store";
  }
  return "developer_id";
}

function defaultBridgeDistributionChannel(env = process.env) {
  const candidates = [
    env.LANTAI_RELEASE_CHANNEL,
    env.LANTAI_DISTRIBUTION,
    env.LANTAI_DISTRIBUTION_CHANNEL,
  ];
  if (
    candidates.some(
      (value) => normalizeBridgeDistribution(value) === "app_store",
    )
  ) {
    return "mas";
  }
  if (
    ["1", "true", "yes"].includes(
      String(env.LANTAI_APP_STORE || "").toLowerCase(),
    )
  ) {
    return "mas";
  }
  return candidates.find((value) => String(value || "").trim()) || "";
}

function buildBrowserBridgeCapabilityMatrix(distributionChannel = "") {
  const distribution = normalizeBridgeDistribution(distributionChannel);
  const entries =
    distribution === "app_store"
      ? APP_STORE_BROWSER_BRIDGE_CAPABILITIES
      : DEVELOPER_ID_BROWSER_BRIDGE_CAPABILITIES;
  return {
    schema_version: 1,
    distribution,
    entries: Object.fromEntries(entries.map((entry) => [entry.id, entry])),
  };
}

function sha256Hex(value) {
  return crypto
    .createHash("sha256")
    .update(String(value || ""), "utf8")
    .digest("hex");
}

function secureBookmarkStorageAvailable(safeStorage) {
  if (
    !safeStorage ||
    typeof safeStorage.encryptString !== "function" ||
    typeof safeStorage.decryptString !== "function"
  ) {
    return false;
  }
  if (typeof safeStorage.isEncryptionAvailable === "function") {
    return Boolean(safeStorage.isEncryptionAvailable());
  }
  return true;
}

function emptySecurityScopedBookmarkStore() {
  return {
    schema_version: SECURITY_SCOPED_BOOKMARK_STORE_SCHEMA_VERSION,
    records: {},
  };
}

function fsyncDirectoryBestEffort(directory) {
  let directoryFd;
  try {
    directoryFd = fs.openSync(directory, fs.constants.O_RDONLY);
    fs.fsyncSync(directoryFd);
  } catch {
    // Some platforms do not allow fsync on directories.
  } finally {
    if (directoryFd !== undefined) {
      try {
        fs.closeSync(directoryFd);
      } catch {
        // Best-effort close after durability sync.
      }
    }
  }
}

function writeJsonAtomic(filePath, payload) {
  const directory = path.dirname(filePath);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  try {
    fs.chmodSync(directory, 0o700);
  } catch {
    // Best-effort permissions on platforms that do not support chmod.
  }
  const tmpPath = path.join(
    directory,
    `.${path.basename(filePath)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
  );
  let tmpFd;
  let writeError = null;
  try {
    tmpFd = fs.openSync(
      tmpPath,
      fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY,
      0o600,
    );
    fs.writeFileSync(tmpFd, JSON.stringify(payload, null, 2), "utf8");
    fs.fsyncSync(tmpFd);
  } catch (err) {
    writeError = err;
  } finally {
    if (tmpFd !== undefined) {
      try {
        fs.closeSync(tmpFd);
      } catch {
        // Best-effort close after write failure.
      }
    }
  }
  if (writeError) {
    try {
      fs.rmSync(tmpPath, { force: true });
    } catch {
      // Preserve the original write error.
    }
    throw writeError;
  }
  try {
    fs.renameSync(tmpPath, filePath);
  } catch (err) {
    try {
      fs.rmSync(tmpPath, { force: true });
    } catch {
      // Preserve the original rename error.
    }
    throw err;
  }
  try {
    fs.chmodSync(filePath, 0o600);
  } catch {
    // Best-effort permissions on platforms that do not support chmod.
  }
  fsyncDirectoryBestEffort(directory);
}

function sanitizeProfileId(profileId) {
  const raw = String(profileId || "default")
    .trim()
    .toLowerCase();
  if (!raw) return "default";
  let slug = raw
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
  const whitespaceNormalized = raw.replace(/\s+/g, "-").replace(/^-|-$/g, "");
  let lossy = slug !== whitespaceNormalized;
  if (!slug) {
    slug = "profile";
    lossy = true;
  }
  if (lossy) {
    const digest = crypto
      .createHash("sha256")
      .update(raw, "utf8")
      .digest("hex")
      .slice(0, 12);
    slug = `${slug}-${digest}`;
  }
  return slug;
}

function getProfilePartition(profileId) {
  return `persist:lantai-browser-${sanitizeProfileId(profileId)}`;
}

function readRequestBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 2 * 1024 * 1024) {
        reject(new Error("Request body is too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(raw));
    req.on("error", reject);
  });
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function normalizeUrl(input) {
  const value = String(input || "").trim();
  if (!value) return "about:blank";
  if (/^(about:|https?:)/i.test(value)) return value;
  if (/^[\w.-]+\.[a-z]{2,}(?::\d+)?(?:\/.*)?$/i.test(value)) {
    return `https://${value}`;
  }
  return `https://www.google.com/search?q=${encodeURIComponent(value)}`;
}

function ipv4FromMappedOrCompatibleIpv6(host) {
  const value = String(host || "")
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
  if (net.isIP(value) !== 6) return "";
  const halves = value.split("::");
  if (halves.length > 2) return "";
  const parseHalf = (half) => {
    const output = [];
    for (const token of half.split(":").filter(Boolean)) {
      if (token.includes(".")) {
        if (net.isIP(token) !== 4) return null;
        const octets = token.split(".").map(Number);
        output.push((octets[0] << 8) | octets[1], (octets[2] << 8) | octets[3]);
        continue;
      }
      const segment = Number.parseInt(token, 16);
      if (!Number.isInteger(segment) || segment < 0 || segment > 0xffff)
        return null;
      output.push(segment);
    }
    return output;
  };
  const left = parseHalf(halves[0] || "");
  const right = parseHalf(halves[1] || "");
  if (!left || !right) return "";
  const missing = halves.length === 2 ? 8 - left.length - right.length : 0;
  if (missing < 0) return "";
  const segments = [...left, ...Array(missing).fill(0), ...right];
  if (segments.length !== 8) return "";
  const mappedPrefix =
    segments.slice(0, 5).every((segment) => segment === 0) &&
    (segments[5] === 0 || segments[5] === 0xffff);
  if (!mappedPrefix) return "";
  return [
    segments[6] >>> 8,
    segments[6] & 0xff,
    segments[7] >>> 8,
    segments[7] & 0xff,
  ].join(".");
}

function normalizedIpv4ForPolicy(host) {
  const value = String(host || "")
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
  if (net.isIP(value) === 4) return value;
  return ipv4FromMappedOrCompatibleIpv6(value);
}

function isSyntheticBenchmarkIp(host) {
  const ipv4 = normalizedIpv4ForPolicy(host);
  if (!ipv4) return false;
  const [a, b] = ipv4.split(".").map(Number);
  return a === 198 && (b === 18 || b === 19);
}

function isPrivateOrReservedIp(host) {
  const value = String(host || "")
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
  if (!value) return false;
  const mappedIpv4 = ipv4FromMappedOrCompatibleIpv6(value);
  if (mappedIpv4) return isPrivateOrReservedIp(mappedIpv4);
  const ipVersion = net.isIP(value);
  if (ipVersion === 4) {
    const parts = value.split(".").map((part) => Number(part));
    if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part)))
      return true;
    const [a, b] = parts;
    return (
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 198 && (b === 18 || b === 19)) ||
      a >= 224
    );
  }
  if (ipVersion === 6) {
    return (
      value === "::" ||
      value === "::1" ||
      value.startsWith("fc") ||
      value.startsWith("fd") ||
      value.startsWith("fe8") ||
      value.startsWith("fe9") ||
      value.startsWith("fea") ||
      value.startsWith("feb") ||
      value.startsWith("ff")
    );
  }
  if (/^\d+$/.test(value)) {
    const integerIp = Number(value);
    if (
      !Number.isSafeInteger(integerIp) ||
      integerIp < 0 ||
      integerIp > 0xffffffff
    ) {
      return true;
    }
    const dotted = [
      (integerIp >>> 24) & 255,
      (integerIp >>> 16) & 255,
      (integerIp >>> 8) & 255,
      integerIp & 255,
    ].join(".");
    return isPrivateOrReservedIp(dotted);
  }
  return false;
}

function proxyRulesUseProxy(value) {
  return String(value || "")
    .split(";")
    .map((rule) => rule.trim().split(/\s+/, 1)[0]?.toUpperCase())
    .some((directive) => Boolean(directive) && directive !== "DIRECT");
}

function browserUrlPolicyResult(input, action) {
  const raw = String(input || "").trim();
  const rawScheme =
    raw.match(/^([a-z][a-z0-9+.-]*):/i)?.[1]?.toLowerCase() || "";
  if (
    raw &&
    raw !== "about:blank" &&
    rawScheme &&
    !["http", "https"].includes(rawScheme)
  ) {
    return {
      ok: false,
      action,
      error: "Browser Workbench only allows http and https URLs.",
      error_code: "browser_url_scheme_blocked",
    };
  }
  const normalized = normalizeUrl(input);
  if (normalized === "about:blank") return null;
  let parsed;
  try {
    parsed = new URL(normalized);
  } catch {
    return {
      ok: false,
      action,
      error: "Browser Workbench URL is invalid.",
      error_code: "browser_url_invalid",
    };
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return {
      ok: false,
      action,
      error: "Browser Workbench only allows http and https URLs.",
      error_code: "browser_url_scheme_blocked",
    };
  }
  const hostname = parsed.hostname.toLowerCase().replace(/\.$/, "");
  if (
    hostname === "localhost" ||
    hostname.endsWith(".localhost") ||
    isPrivateOrReservedIp(hostname)
  ) {
    return {
      ok: false,
      action,
      error: "Browser Workbench blocks localhost and private-network URLs.",
      error_code: "browser_private_network_url_blocked",
    };
  }
  return null;
}

function redactHeaders(headers = {}) {
  const output = {};
  for (const [key, value] of Object.entries(headers || {})) {
    const name = String(key || "");
    if (/authorization|cookie|token|api[-_]?key|secret/i.test(name)) {
      output[name] = "[redacted]";
    } else {
      output[name] = value;
    }
  }
  return output;
}

function redactTextPreview(value, limit = 4096) {
  return String(value || "")
    .replace(/Bearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer [redacted]")
    .replace(
      /([?&](?:token|access_token|refresh_token|api_key|key|secret)=)[^&\s]+/gi,
      "$1[redacted]",
    )
    .replace(
      /("(?:token|access_token|refresh_token|api_key|secret|password)"\s*:\s*")[^"]+(")/gi,
      "$1[redacted]$2",
    )
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[redacted-email]")
    .slice(0, limit);
}

function sanitizeNetworkBody(body) {
  if (!body || typeof body !== "object") return null;
  const rawBody = typeof body.body === "string" ? body.body : "";
  return {
    body_preview: redactTextPreview(rawBody),
    body_bytes: Buffer.byteLength(
      rawBody,
      body.base64Encoded ? "base64" : "utf8",
    ),
    base64_encoded: Boolean(body.base64Encoded),
    redacted: true,
    truncated: rawBody.length > 4096,
  };
}

function evaluateSafetyError(code) {
  const source = String(code || "");
  const blockedPatterns = [
    /\bfetch\s*\(/i,
    /\bXMLHttpRequest\b/i,
    /\bWebSocket\b/i,
    /\bnavigator\.sendBeacon\b/i,
    /\blocalStorage\b/i,
    /\bsessionStorage\b/i,
    /\bdocument\.cookie\b/i,
    /\blocation\s*[.=]/i,
    /\bwindow\.open\s*\(/i,
    /\.click\s*\(/i,
    /\.submit\s*\(/i,
    /\beval\s*\(/i,
    /\bFunction\s*\(/i,
  ];
  return blockedPatterns.some((pattern) => pattern.test(source))
    ? "Arbitrary evaluate code may read secrets, navigate, submit, or exfiltrate data."
    : "";
}

function safeEvaluateScriptId(raw = {}) {
  const metadata =
    raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
  const scriptId = String(
    raw.safe_script_id ||
      metadata.safe_script_id ||
      raw.script_id ||
      metadata.script_id ||
      "",
  )
    .trim()
    .toLowerCase();
  return scriptId;
}

function safeEvaluateScriptParams(raw = {}) {
  const metadata =
    raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
  const params = raw.safe_script_params || metadata.safe_script_params;
  return params && typeof params === "object" && !Array.isArray(params)
    ? params
    : metadata;
}

function normalizeStringList(value, limit = 50) {
  let source = value;
  if (typeof source === "string") {
    try {
      source = JSON.parse(source);
    } catch {
      source = [source];
    }
  }
  if (!Array.isArray(source)) return [];
  return source
    .map((item) => String(item || "").trim())
    .filter(Boolean)
    .slice(0, limit);
}

function safePageSummaryScript() {
  return `() => ({
    title: document.title || "",
    url: location.href || "",
    text: (document.body?.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 8000),
    links: Array.from(document.querySelectorAll("a[href]")).slice(0, 80).map((link) => ({
      text: (link.innerText || link.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim().slice(0, 180),
      href: link.href || "",
    })),
  })`;
}

function safeSelectorProbeScript(params = {}) {
  const selectors = normalizeStringList(
    params.selectors || params.domain_skill_selectors,
    50,
  );
  return `() => {
    const selectors = ${JSON.stringify(selectors)};
    const sampledNodes = Array.from(document.querySelectorAll("*")).slice(0, 5000);
    const diagnostics = {
      document_ready_state: document.readyState,
      iframe_count: document.querySelectorAll("iframe").length,
      accessible_iframe_count: 0,
      blocked_iframe_count: document.querySelectorAll("iframe").length,
      shadow_host_count: sampledNodes.filter((node) => Boolean(node.shadowRoot)).length,
      open_shadow_root_count: sampledNodes.filter((node) => Boolean(node.shadowRoot)).length,
      sampled_node_count: sampledNodes.length,
    };
    const sampleNodes = (nodes) => nodes.slice(0, 10).map((node) => ({
      scope: "document",
      tag: node.tagName ? node.tagName.toLowerCase() : "",
      text: (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 180),
      href: node.href || node.querySelector?.("a[href]")?.href || "",
    }));
    return selectors.map((selector) => {
      try {
        const nodes = Array.from(document.querySelectorAll(selector));
        return {
          selector,
          ok: true,
          count: nodes.length,
          diagnostics,
          scope_counts: { document: nodes.length, iframe: 0, shadow: 0 },
          blocked_frame_count: diagnostics.blocked_iframe_count,
          samples: sampleNodes(nodes),
          scopes: [{
            scope: "document",
            kind: "document",
            ok: true,
            blocked: false,
            count: nodes.length,
            samples: sampleNodes(nodes),
            url: location.href || "",
            host: null,
            error: "",
          }],
          error: "",
        };
      } catch (error) {
        return {
          selector,
          ok: false,
          count: 0,
          diagnostics,
          scope_counts: { document: 0, iframe: 0, shadow: 0 },
          blocked_frame_count: diagnostics.blocked_iframe_count,
          samples: [],
          scopes: [],
          error: String(error && error.message ? error.message : error),
        };
      }
    });
  }`;
}

function safeWechatEditorOptionsScript() {
  return `() => {
    const textOf = (node) => {
      const raw = node.innerText || node.textContent || node.value || node.placeholder || node.ariaLabel || "";
      return String(raw).replace(/\\s+/g, " ").trim().slice(0, 180);
    };
    const visible = (node) => {
      try {
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      } catch {
        return true;
      }
    };
    const nodes = Array.from(document.querySelectorAll("button,a,[role='button'],label,input,textarea,[contenteditable='true'],span,div"))
      .filter(visible)
      .slice(0, 5000)
      .map((node) => ({ text: textOf(node), placeholder: String(node.placeholder || "").slice(0, 120) }))
      .filter((item) => item.text || item.placeholder);
    const matchFeature = (id, label, patterns) => {
      const samples = nodes.filter((item) => patterns.some((pattern) => String(item.text + " " + item.placeholder).includes(pattern))).slice(0, 8);
      return { id, label, found: samples.length > 0, count: samples.length, samples };
    };
    const options = [
      matchFeature("cover", "cover", ["封面", "从正文选择", "图片库", "AI 配图", "拖拽或选择封面"]),
      matchFeature("original", "original", ["原创", "声明原创", "未声明", "著作权保护"]),
      matchFeature("comment", "comment", ["留言", "精选公开", "留言自动", "留言管理"]),
    ];
    const riskPatterns = ["发布", "发表", "群发", "定时群发", "提交审核", "删除", "支付", "授权", "发送"];
    const manualPatterns = ["扫码登录", "微信扫码", "验证码", "安全验证", "二维码", "登录"];
    const risk_buttons = nodes.filter((item) => riskPatterns.some((pattern) => item.text.includes(pattern))).slice(0, 12);
    const manual_takeover_hints = nodes.filter((item) => manualPatterns.some((pattern) => item.text.includes(pattern))).slice(0, 12);
    return {
      page_title: document.title || "",
      url: location.href || "",
      read_only: true,
      options,
      risk_buttons,
      manual_takeover_hints,
      requires_manual_takeover: manual_takeover_hints.length > 0,
      safety: { no_write_actions: true, publish_requires_confirmation: true, silent_publish_enabled: false },
    };
  }`;
}

function safeWechatInspectControlsScript(params = {}) {
  const selectors = String(params.selectors || "body");
  return `() => {
    const visible = (el) => {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return s.display !== "none" && s.visibility !== "hidden" && r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0;
    };
    const selectors = ${JSON.stringify(selectors)};
    return Array.from(document.querySelectorAll(selectors)).filter(visible).slice(0, 40).map((el) => {
      const r = el.getBoundingClientRect();
      return {
        tag: el.tagName.toLowerCase(),
        id: el.id || "",
        className: String(el.className || ""),
        text: (el.innerText || el.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim().slice(0, 180),
        rect: { x: r.x, y: r.y, width: r.width, height: r.height },
      };
    });
  }`;
}

function resolveSafeEvaluateScript(raw = {}) {
  const scriptId = safeEvaluateScriptId(raw);
  const params = safeEvaluateScriptParams(raw);
  if (scriptId === "browser.page_summary")
    return { ok: true, source: safePageSummaryScript() };
  if (scriptId === "domain_skill.selector_probe") {
    return { ok: true, source: safeSelectorProbeScript(params) };
  }
  if (scriptId === "wechat.editor_options_probe") {
    return { ok: true, source: safeWechatEditorOptionsScript() };
  }
  if (scriptId === "wechat.inspect_controls") {
    return { ok: true, source: safeWechatInspectControlsScript(params) };
  }
  return {
    ok: false,
    error: "Browser evaluate requires a registered read-only script id.",
    error_code: "browser_evaluate_script_registry_required",
  };
}

function parseMaybeJson(value, fallback) {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "object") return value;
  try {
    return JSON.parse(String(value));
  } catch {
    return fallback;
  }
}

function normalizeFormFields(raw = {}) {
  const source =
    parseMaybeJson(raw.fields_json ?? raw.metadata?.fields_json, null) ??
    parseMaybeJson(raw.fields ?? raw.metadata?.fields, null) ??
    raw.fields ??
    raw.metadata?.fields ??
    {};
  if (Array.isArray(source)) {
    return source
      .map((item) => ({
        selector: String(item.selector || item.name || item.id || "").trim(),
        value: String(item.value ?? item.text ?? ""),
        frame_selector: String(
          item.frame_selector || item.frameSelector || "",
        ).trim(),
        shadow_host_selector: String(
          item.shadow_host_selector || item.shadowHostSelector || "",
        ).trim(),
      }))
      .filter((item) => item.selector);
  }
  return Object.entries(source || {})
    .map(([selector, value]) => ({
      selector: String(selector).trim(),
      value: String(value ?? ""),
    }))
    .filter((item) => item.selector);
}

function normalizeDragPath(raw = {}) {
  const parsed =
    parseMaybeJson(
      raw.path_json ??
        raw.paths_json ??
        raw.metadata?.path_json ??
        raw.metadata?.paths_json,
      null,
    ) ??
    raw.path ??
    raw.paths ??
    raw.metadata?.path ??
    raw.metadata?.paths;
  if (Array.isArray(parsed) && parsed.length >= 2) {
    return parsed
      .map((point) => ({
        x: Number(point.x ?? point[0] ?? 0),
        y: Number(point.y ?? point[1] ?? 0),
      }))
      .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
  }
  const startX = Number(
    raw.start_x ?? raw.metadata?.start_x ?? raw.x1 ?? raw.metadata?.x1 ?? 0,
  );
  const startY = Number(
    raw.start_y ?? raw.metadata?.start_y ?? raw.y1 ?? raw.metadata?.y1 ?? 0,
  );
  const endX = Number(
    raw.end_x ?? raw.metadata?.end_x ?? raw.x2 ?? raw.metadata?.x2 ?? 0,
  );
  const endY = Number(
    raw.end_y ?? raw.metadata?.end_y ?? raw.y2 ?? raw.metadata?.y2 ?? 0,
  );
  return [
    { x: startX, y: startY },
    { x: endX, y: endY },
  ].filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
}

function scopedSelectorHelpersSource() {
  return `
    const resolveRoot = (frameSelector, shadowHostSelector) => {
      let root = document;
      const scope = {
        frame_selector: frameSelector || "",
        shadow_host_selector: shadowHostSelector || "",
      };
      if (frameSelector) {
        const frame = document.querySelector(frameSelector);
        if (!frame) return { root: null, scope, error: "Frame element not found." };
        try {
          root = frame.contentDocument;
        } catch {
          return { root: null, scope, error: "Frame is cross-origin or unavailable." };
        }
        if (!root) return { root: null, scope, error: "Frame document is unavailable." };
      }
      if (shadowHostSelector) {
        const host = root.querySelector(shadowHostSelector);
        if (!host) return { root: null, scope, error: "Shadow host element not found." };
        if (!host.shadowRoot) return { root: null, scope, error: "Shadow root is closed or unavailable." };
        root = host.shadowRoot;
      }
      return { root, scope, error: "" };
    };
    const queryAllScoped = (selector, frameSelector, shadowHostSelector) => {
      const resolved = resolveRoot(frameSelector, shadowHostSelector);
      if (!resolved.root) return { nodes: [], scope: resolved.scope, error: resolved.error };
      try {
        return {
          nodes: Array.from(resolved.root.querySelectorAll(selector)),
          scope: resolved.scope,
          error: "",
        };
      } catch (error) {
        return {
          nodes: [],
          scope: resolved.scope,
          error: String(error && error.message ? error.message : error),
        };
      }
    };
    const queryScoped = (selector, frameSelector, shadowHostSelector) => {
      const result = queryAllScoped(selector, frameSelector, shadowHostSelector);
      return { el: result.nodes[0] || null, scope: result.scope, error: result.error };
    };
  `;
}

function buildWorkbenchHtml() {
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>澜台浏览器工作台</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --border: #dfe4ea;
      --text: #1f2329;
      --muted: #697386;
      --accent: #2368c4;
      --danger: #b42318;
      --ok: #067647;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      overflow: hidden;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    .chrome {
      height: 118px;
      border-bottom: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.96);
      display: grid;
      grid-template-rows: 40px 42px 35px;
    }
    .tabs {
      display: flex;
      align-items: end;
      gap: 6px;
      padding: 6px 10px 0;
      overflow-x: auto;
      white-space: nowrap;
    }
    .tab {
      max-width: 220px;
      min-width: 112px;
      height: 32px;
      border: 1px solid var(--border);
      border-bottom: 0;
      border-radius: 8px 8px 0 0;
      background: #eef2f7;
      color: var(--muted);
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 0 8px;
      cursor: pointer;
      font-size: 12px;
    }
    .tab.active {
      background: var(--panel);
      color: var(--text);
      font-weight: 700;
    }
    .tab-title {
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .tab-close {
      border: 0;
      background: transparent;
      color: inherit;
      cursor: pointer;
      width: 20px;
      height: 20px;
      border-radius: 6px;
      flex: 0 0 auto;
    }
    .nav {
      display: grid;
      grid-template-columns: auto auto auto auto 1fr auto auto;
      align-items: center;
      gap: 8px;
      padding: 5px 10px;
      border-top: 1px solid rgba(223, 228, 234, 0.6);
    }
    button {
      border: 1px solid var(--border);
      background: var(--panel);
      color: var(--text);
      border-radius: 8px;
      min-width: 34px;
      height: 32px;
      cursor: pointer;
      font-size: 13px;
    }
    button.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      padding: 0 12px;
      font-weight: 700;
    }
    input {
      width: 100%;
      height: 32px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0 11px;
      background: var(--panel);
      color: var(--text);
      font-size: 13px;
      outline: none;
    }
    .takeover {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto auto auto;
      align-items: center;
      gap: 8px;
      padding: 0 10px 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .status {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      height: 24px;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0 9px;
      background: var(--panel);
      color: var(--muted);
    }
    .badge.ok { color: var(--ok); }
    .badge.risk { color: var(--danger); }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #171a1f;
        --panel: #20242b;
        --border: #343b46;
        --text: #f4f6f8;
        --muted: #a8b0bd;
        --accent: #6ea8fe;
      }
      .chrome { background: rgba(24, 27, 32, 0.97); }
      .tab { background: #252b34; }
    }
  </style>
</head>
<body>
  <div class="chrome">
    <div class="tabs" id="tabs"></div>
    <form class="nav" id="nav-form">
      <button type="button" data-action="go_back" title="后退">‹</button>
      <button type="button" data-action="go_forward" title="前进">›</button>
      <button type="button" data-action="reload" title="刷新">↻</button>
      <button type="button" data-action="new_tab" title="新建标签页">＋</button>
      <input id="address" autocomplete="off" spellcheck="false" placeholder="输入网址或搜索" />
      <button type="submit" class="primary">前往</button>
      <button type="button" data-action="screenshot" title="截图">截图</button>
    </form>
    <div class="takeover">
      <div class="status" id="status">澜台浏览器工作台已就绪</div>
      <span class="badge" id="profile">Profile default</span>
      <span class="badge ok" id="agent">人工浏览</span>
      <button type="button" data-action="agent_pause">暂停</button>
      <button type="button" data-action="agent_resume">继续</button>
      <button type="button" data-action="agent_stop">终止</button>
    </div>
  </div>
  <script>
    const api = window.electronAPI;
    let state = { tabs: [], active_tab_id: null };

    function activeTab() {
      return state.tabs.find((tab) => tab.id === state.active_tab_id) || state.tabs[0];
    }

    function render(next) {
      state = next || state;
      const tabsEl = document.getElementById("tabs");
      tabsEl.innerHTML = "";
      for (const tab of state.tabs || []) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "tab" + (tab.id === state.active_tab_id ? " active" : "");
        button.title = tab.url || "";
        button.onclick = () => api.browserWorkbench.action({ action: "select_tab", tab_id: tab.id });
        const title = document.createElement("span");
        title.className = "tab-title";
        title.textContent = tab.loading ? "加载中..." : (tab.title || tab.url || "新标签页");
        const close = document.createElement("button");
        close.type = "button";
        close.className = "tab-close";
        close.textContent = "×";
        close.onclick = (event) => {
          event.stopPropagation();
          api.browserWorkbench.action({ action: "close_tab", tab_id: tab.id });
        };
        button.appendChild(title);
        button.appendChild(close);
        tabsEl.appendChild(button);
      }
      const tab = activeTab();
      document.getElementById("address").value = tab?.url || "";
      document.getElementById("profile").textContent = "Profile " + (tab?.profile_id || state.profile_id || "default");
      document.getElementById("agent").textContent = state.agent_status || "人工浏览";
      document.getElementById("status").textContent = state.current_action || (tab?.loading ? "页面加载中" : "已就绪");
    }

    document.getElementById("nav-form").addEventListener("submit", (event) => {
      event.preventDefault();
      api.browserWorkbench.action({
        action: "navigate",
        url: document.getElementById("address").value,
      });
    });

    document.body.addEventListener("click", (event) => {
      const action = event.target?.dataset?.action;
      if (!action) return;
      api.browserWorkbench.action({ action });
    });

    api.browserWorkbench.onState(render);
    api.browserWorkbench.getState().then(render);
  </script>
</body>
</html>`;
}

function createBrowserWorkbenchController({
  app,
  BrowserWindow,
  BrowserView,
  ipcMain,
  session = null,
  dialog = null,
  safeStorage = null,
  electronDir,
  appendBackendLog = () => {},
  randomToken = () => crypto.randomBytes(32).toString("hex"),
  createServer = (handler) => http.createServer(handler),
  lookupHost = (hostname) =>
    dns.lookup(hostname, { all: true, verbatim: true }),
  resolveProxyForUrl = null,
  isTrustedIpcEvent = () => true,
  getMainWindow = () => null,
  spawnProcess = childProcess.spawn,
  platform = process.platform,
  distributionChannel = defaultBridgeDistributionChannel(),
}) {
  let workbenchWindow = null;
  let workbenchVisible = false;
  let bridgeServer = null;
  let bridgeBaseUrl = "";
  let bridgeToken = randomToken();
  const bridgeCapabilityMatrix =
    buildBrowserBridgeCapabilityMatrix(distributionChannel);
  const usedBridgeCapabilityNonces = new Map();
  const activeSecurityScopedResources = new Map();
  const activeSecurityScopedResourcePaths = new Map();
  const securityScopedBookmarkStorePath = path.join(
    app.getPath("userData"),
    "browser-workbench",
    "security-scoped-bookmarks.json",
  );
  let activeTabId = null;
  let tabSequence = 0;
  let currentAction = "";
  let agentStatus = "人工浏览";
  let sidecar = {
    visible: false,
    bounds: null,
    runId: "",
    profileId: "default",
    controlOwner: "agent",
  };
  const tabs = new Map();
  const staleTabRecoveries = new Map();
  const consoleEvents = [];
  const networkEvents = [];

  function log(message) {
    appendBackendLog(`[BrowserWorkbench] ${message}`);
  }

  function quarantineSecurityScopedBookmarkStore(reason) {
    const suffix = `${Date.now()}-${crypto.randomBytes(6).toString("hex")}`;
    const quarantinePath = `${securityScopedBookmarkStorePath}.corrupt-${suffix}`;
    try {
      fs.renameSync(securityScopedBookmarkStorePath, quarantinePath);
      try {
        fs.chmodSync(quarantinePath, 0o600);
      } catch {
        // Best-effort permissions on platforms that do not support chmod.
      }
      fsyncDirectoryBestEffort(path.dirname(securityScopedBookmarkStorePath));
      log(`Quarantined invalid security-scoped bookmark store (${reason}).`);
      return quarantinePath;
    } catch (err) {
      throw new Error(
        `Cannot quarantine invalid security-scoped bookmark store: ${err.message}`,
      );
    }
  }

  function loadSecurityScopedBookmarkStore() {
    if (!fs.existsSync(securityScopedBookmarkStorePath)) {
      return emptySecurityScopedBookmarkStore();
    }
    try {
      const parsed = JSON.parse(
        fs.readFileSync(securityScopedBookmarkStorePath, "utf8"),
      );
      if (
        parsed &&
        parsed.schema_version ===
          SECURITY_SCOPED_BOOKMARK_STORE_SCHEMA_VERSION &&
        parsed.records &&
        typeof parsed.records === "object"
      ) {
        return parsed;
      }
      quarantineSecurityScopedBookmarkStore("unsupported_schema");
      return emptySecurityScopedBookmarkStore();
    } catch (err) {
      if (String(err.message || "").startsWith("Cannot quarantine")) {
        throw err;
      }
      quarantineSecurityScopedBookmarkStore("unreadable_store");
      return emptySecurityScopedBookmarkStore();
    }
  }

  function saveSecurityScopedBookmarkStore(store) {
    writeJsonAtomic(securityScopedBookmarkStorePath, store);
  }

  function encryptSecurityScopedBookmarkPayload(payload) {
    if (!secureBookmarkStorageAvailable(safeStorage)) {
      return {
        ok: false,
        error: "Electron safeStorage encryption is unavailable.",
        error_code: "mas_security_scope_secure_store_unavailable",
      };
    }
    try {
      const encrypted = safeStorage.encryptString(JSON.stringify(payload));
      const encryptedBuffer = Buffer.isBuffer(encrypted)
        ? encrypted
        : Buffer.from(encrypted);
      return {
        ok: true,
        encryption: "electron_safe_storage_v1",
        encrypted_payload: encryptedBuffer.toString("base64"),
      };
    } catch (err) {
      return {
        ok: false,
        error: err.message,
        error_code: "mas_security_scope_secure_store_failed",
      };
    }
  }

  function decryptSecurityScopedBookmarkPayload(record) {
    if (record?.encryption !== "electron_safe_storage_v1") {
      return {
        ok: false,
        error: "Unsupported security-scoped bookmark encryption.",
        error_code: "mas_security_scope_secure_store_unavailable",
      };
    }
    if (!secureBookmarkStorageAvailable(safeStorage)) {
      return {
        ok: false,
        error: "Electron safeStorage decryption is unavailable.",
        error_code: "mas_security_scope_secure_store_unavailable",
      };
    }
    try {
      const encrypted = Buffer.from(
        String(record.encrypted_payload || ""),
        "base64",
      );
      const decrypted = safeStorage.decryptString(encrypted);
      const payload = JSON.parse(decrypted);
      return { ok: true, payload };
    } catch (err) {
      return {
        ok: false,
        error: err.message,
        error_code: "mas_security_scope_restore_failed",
      };
    }
  }

  function persistSecurityScopedBookmarkRecord({
    ref,
    filePath,
    bookmark,
    bookmarkSha256,
  }) {
    const encrypted = encryptSecurityScopedBookmarkPayload({
      path: filePath,
      bookmark,
    });
    if (!encrypted.ok) return encrypted;
    try {
      const now = new Date().toISOString();
      const store = loadSecurityScopedBookmarkStore();
      const existing = store.records[ref] || {};
      store.records[ref] = {
        schema_version: 1,
        ref,
        status: "active",
        created_at: existing.created_at || now,
        updated_at: now,
        last_restored_at: "",
        bookmark_sha256: bookmarkSha256,
        path_sha256: `sha256:${sha256Hex(filePath)}`,
        encryption: encrypted.encryption,
        encrypted_payload: encrypted.encrypted_payload,
        stale_reason: "",
        stale_at: "",
      };
      saveSecurityScopedBookmarkStore(store);
      return {
        ok: true,
        path_sha256: store.records[ref].path_sha256,
        bookmark_sha256: bookmarkSha256,
        store_path: securityScopedBookmarkStorePath,
      };
    } catch (err) {
      return {
        ok: false,
        error: err.message,
        error_code: "mas_security_scope_secure_store_failed",
      };
    }
  }

  function updateSecurityScopedBookmarkRecord(ref, updater) {
    const store = loadSecurityScopedBookmarkStore();
    const record = store.records[ref];
    if (!record) return null;
    store.records[ref] = updater({ ...record });
    saveSecurityScopedBookmarkStore(store);
    return store.records[ref];
  }

  function markSecurityScopedBookmarkStale(ref, reason) {
    try {
      updateSecurityScopedBookmarkRecord(ref, (record) => ({
        ...record,
        status: "stale",
        updated_at: new Date().toISOString(),
        stale_at: new Date().toISOString(),
        stale_reason: String(reason || "bookmark_stale"),
      }));
    } catch (err) {
      log(`Failed to mark security-scoped bookmark stale: ${err.message}`);
    }
  }

  function forgetSecurityScopedBookmark(ref) {
    if (!ref) return false;
    const store = loadSecurityScopedBookmarkStore();
    if (!store.records[ref]) return false;
    delete store.records[ref];
    saveSecurityScopedBookmarkStore(store);
    return true;
  }

  function loadPersistedSecurityScopedBookmark(ref) {
    const store = loadSecurityScopedBookmarkStore();
    const record = store.records[ref];
    if (!record) {
      return {
        ok: false,
        error: "Security-scoped bookmark grant was not found.",
        error_code: "mas_security_scope_bookmark_not_found",
      };
    }
    if (record.status === "stale") {
      return {
        ok: false,
        error: "Security-scoped bookmark grant is stale.",
        error_code: "mas_security_scope_bookmark_stale",
        bookmark_stale: true,
        stale_reason: record.stale_reason || "bookmark_stale",
      };
    }
    const decrypted = decryptSecurityScopedBookmarkPayload(record);
    if (!decrypted.ok) {
      if (decrypted.error_code === "mas_security_scope_restore_failed") {
        markSecurityScopedBookmarkStale(ref, "decrypt_failed");
        return {
          ok: false,
          error: decrypted.error,
          error_code: "mas_security_scope_bookmark_stale",
          bookmark_stale: true,
          stale_reason: "decrypt_failed",
        };
      }
      return decrypted;
    }
    const payload = decrypted.payload || {};
    const filePath = String(payload.path || "");
    const bookmark = String(payload.bookmark || "");
    if (!filePath || !bookmark) {
      markSecurityScopedBookmarkStale(ref, "missing_payload");
      return {
        ok: false,
        error: "Security-scoped bookmark payload is incomplete.",
        error_code: "mas_security_scope_bookmark_stale",
        bookmark_stale: true,
      };
    }
    return {
      ok: true,
      ref,
      path: filePath,
      bookmark,
      bookmarkSha256: record.bookmark_sha256 || `sha256:${sha256Hex(bookmark)}`,
      pathSha256: record.path_sha256 || `sha256:${sha256Hex(filePath)}`,
    };
  }

  function getEvidenceRoot() {
    return path.join(app.getPath("userData"), "browser-workbench", "evidence");
  }

  function evidenceFilePath(tab, extension) {
    const safeTabId =
      String(tab?.id || "tab")
        .replace(/[^a-zA-Z0-9_-]+/g, "-")
        .replace(/-+/g, "-")
        .replace(/^-|-$/g, "")
        .slice(0, 64) || "tab";
    const uniqueId = crypto.randomUUID
      ? crypto.randomUUID()
      : crypto.randomBytes(16).toString("hex");
    return path.join(
      getEvidenceRoot(),
      `${safeTabId}-${uniqueId}.${extension}`,
    );
  }

  async function sendCdp(wc, method, params = {}) {
    if (!wc.debugger) {
      throw new Error("Electron debugger API is unavailable.");
    }
    if (!wc.debugger.isAttached?.()) {
      wc.debugger.attach("1.3");
    }
    return await wc.debugger.sendCommand(method, params);
  }

  function pushBounded(list, item) {
    list.push({ ts: new Date().toISOString(), ...item });
    if (list.length > MAX_EVENT_BUFFER)
      list.splice(0, list.length - MAX_EVENT_BUFFER);
  }

  function getWorkbenchWindow() {
    if (workbenchWindow && !workbenchWindow.isDestroyed()) {
      return workbenchWindow;
    }
    return null;
  }

  function getActiveTab() {
    return tabs.get(activeTabId) || null;
  }

  function getMainHostWindow() {
    const win = getMainWindow?.();
    return win && !win.isDestroyed?.() ? win : null;
  }

  function browserHostWindows() {
    return Array.from(
      new Set([getWorkbenchWindow(), getMainHostWindow()].filter(Boolean)),
    );
  }

  function detachViewFromHosts(view) {
    for (const win of browserHostWindows()) {
      try {
        win.removeBrowserView?.(view);
      } catch {
        // A view may already be detached or owned by another host.
      }
    }
  }

  function normalizeSidecarBounds(raw = {}) {
    const host = getMainHostWindow();
    const [hostWidth, hostHeight] = host?.getContentSize?.() || [0, 0];
    const number = (value, fallback = 0) => {
      const parsed = Math.round(Number(value));
      return Number.isFinite(parsed) ? parsed : fallback;
    };
    const x = Math.max(0, Math.min(number(raw.x), Math.max(0, hostWidth - 1)));
    const y = Math.max(0, Math.min(number(raw.y), Math.max(0, hostHeight - 1)));
    const requestedWidth = Math.max(1, number(raw.width, hostWidth - x));
    const requestedHeight = Math.max(1, number(raw.height, hostHeight - y));
    return {
      x,
      y,
      width: Math.max(0, Math.min(requestedWidth, Math.max(0, hostWidth - x))),
      height: Math.max(
        0,
        Math.min(requestedHeight, Math.max(0, hostHeight - y)),
      ),
    };
  }

  function surfaceState() {
    const mode = sidecar.visible
      ? sidecar.controlOwner === "user"
        ? "sidecar_interactive"
        : "sidecar_preview"
      : workbenchVisible && getWorkbenchWindow()
        ? "window"
        : "hidden";
    return {
      mode,
      visible: sidecar.visible,
      control_owner: sidecar.controlOwner,
      run_id: sidecar.runId,
      bounds: sidecar.bounds,
    };
  }

  function rememberStaleTabRecovery(staleTabId, recovery) {
    staleTabRecoveries.delete(staleTabId);
    staleTabRecoveries.set(staleTabId, recovery);
    while (staleTabRecoveries.size > MAX_EVENT_BUFFER) {
      staleTabRecoveries.delete(staleTabRecoveries.keys().next().value);
    }
  }

  function closeTabsForProfile(profileId) {
    const normalizedProfile = sanitizeProfileId(profileId);
    let closed = 0;
    for (const [tabId, tab] of Array.from(tabs.entries())) {
      if (tab.profileId !== normalizedProfile) continue;
      try {
        detachViewFromHosts(tab.view);
        tab.view.webContents.destroy?.();
      } catch {
        // Best-effort cleanup before storage deletion.
      }
      tabs.delete(tabId);
      closed += 1;
      if (activeTabId === tabId) activeTabId = null;
    }
    if (!activeTabId) {
      activeTabId = tabs.keys().next().value || null;
    }
    if (activeTabId) attachActiveView();
    else broadcastState();
    return closed;
  }

  function closeTabsForTaskGroup(profileId, taskGroupId) {
    const normalizedProfile = sanitizeProfileId(profileId);
    const normalizedTaskGroup = String(taskGroupId || "").trim();
    if (!normalizedTaskGroup) return 0;
    let closed = 0;
    for (const [tabId, tab] of Array.from(tabs.entries())) {
      if (
        tab.profileId !== normalizedProfile ||
        tab.taskGroupId !== normalizedTaskGroup
      ) {
        continue;
      }
      try {
        detachViewFromHosts(tab.view);
        tab.view.webContents.destroy?.();
      } catch {
        // Best-effort cleanup after a terminal background task.
      }
      tabs.delete(tabId);
      closed += 1;
      if (activeTabId === tabId) activeTabId = null;
    }
    if (!activeTabId) {
      activeTabId = tabs.keys().next().value || null;
    }
    if (activeTabId) attachActiveView();
    else broadcastState();
    return closed;
  }

  function actionTaskOptions(raw = {}) {
    const metadata =
      raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
    const background = Boolean(
      raw.background_observable ||
      raw.backgroundObservable ||
      metadata.background_observable ||
      metadata.backgroundObservable ||
      raw.task_group_id ||
      raw.taskGroupId ||
      metadata.task_group_id ||
      metadata.taskGroupId,
    );
    const rawTaskGroupId = String(
      raw.task_group_id ||
        raw.taskGroupId ||
        metadata.task_group_id ||
        metadata.taskGroupId ||
        "",
    ).trim();
    return {
      background,
      taskGroupId:
        rawTaskGroupId ||
        (background
          ? `background-${sanitizeProfileId(raw.profile_id || raw.profileId || "default")}`
          : ""),
    };
  }

  function securityScopeRefForSelection(filePath, bookmark) {
    const bookmarkSha256 = crypto
      .createHash("sha256")
      .update(String(bookmark || ""), "utf8")
      .digest("hex");
    const payload = `${String(filePath || "")}|sha256:${bookmarkSha256}`;
    const digest = crypto
      .createHash("sha256")
      .update(payload, "utf8")
      .digest("hex");
    return {
      ref: `browser-security-scope:${digest}`,
      bookmarkSha256: `sha256:${bookmarkSha256}`,
    };
  }

  function securityScopeRefFromAction(raw = {}) {
    const metadata =
      raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
    return String(
      raw.security_scope_ref ||
        raw.securityScopeRef ||
        metadata.security_scope_ref ||
        metadata.securityScopeRef ||
        metadata.browser_security_scope_ref ||
        "",
    ).trim();
  }

  function uploadFilePathFromAction(raw = {}) {
    const metadata =
      raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
    return String(
      raw.path ||
        raw.file_path ||
        raw.filePath ||
        metadata.path ||
        metadata.file_path ||
        "",
    ).trim();
  }

  function rememberSecurityScopedResource(ref, stopAccessing, filePath) {
    if (!ref || typeof stopAccessing !== "function") return;
    const previous = activeSecurityScopedResources.get(ref);
    if (typeof previous === "function") {
      try {
        previous();
      } catch {
        // Best-effort release before replacing the active scoped resource.
      }
    }
    activeSecurityScopedResources.set(ref, stopAccessing);
    activeSecurityScopedResourcePaths.set(
      ref,
      path.resolve(String(filePath || "")),
    );
  }

  function releaseSecurityScopedResource(ref) {
    if (!ref) return false;
    const stopAccessing = activeSecurityScopedResources.get(ref);
    activeSecurityScopedResources.delete(ref);
    activeSecurityScopedResourcePaths.delete(ref);
    if (typeof stopAccessing !== "function") return false;
    try {
      stopAccessing();
      return true;
    } catch (err) {
      log(`Security-scoped resource release failed: ${err.message}`);
      return false;
    }
  }

  function releaseAllSecurityScopedResources() {
    let released = 0;
    for (const ref of [...activeSecurityScopedResources.keys()]) {
      if (releaseSecurityScopedResource(ref)) {
        released += 1;
      }
    }
    activeSecurityScopedResourcePaths.clear();
    return released;
  }

  function isRestoredSecurityScopedTargetAvailable(filePath) {
    try {
      if (!fs.existsSync(filePath)) return false;
      const stat = fs.lstatSync(filePath);
      return stat.isFile() && !stat.isSymbolicLink();
    } catch {
      return false;
    }
  }

  function restoreSecurityScopedResource(ref) {
    if (!ref) {
      return {
        ok: false,
        error: "restore_security_scope requires security_scope_ref.",
        error_code: "mas_security_scope_ref_required",
      };
    }
    if (activeSecurityScopedResources.has(ref)) {
      return {
        ok: true,
        action: "restore_security_scope",
        runtime: "electron_bridge",
        security_scope_ref: ref,
        security_scoped_bookmark: true,
        security_scope_restored: true,
        already_active: true,
      };
    }
    if (typeof app.startAccessingSecurityScopedResource !== "function") {
      return {
        ok: false,
        action: "restore_security_scope",
        runtime: "electron_bridge",
        security_scope_ref: ref,
        error:
          "Electron cannot restore the security-scoped bookmark in this runtime.",
        error_code: "mas_security_scope_start_unavailable",
      };
    }
    const persisted = loadPersistedSecurityScopedBookmark(ref);
    if (!persisted.ok) {
      return {
        ok: false,
        action: "restore_security_scope",
        runtime: "electron_bridge",
        security_scope_ref: ref,
        ...persisted,
      };
    }
    let stopAccessing;
    try {
      stopAccessing = app.startAccessingSecurityScopedResource(
        persisted.bookmark,
      );
    } catch (err) {
      markSecurityScopedBookmarkStale(ref, "restore_failed");
      return {
        ok: false,
        action: "restore_security_scope",
        runtime: "electron_bridge",
        security_scope_ref: ref,
        error: err.message,
        error_code: "mas_security_scope_bookmark_stale",
        bookmark_stale: true,
      };
    }
    if (typeof stopAccessing !== "function") {
      markSecurityScopedBookmarkStale(ref, "restore_handle_missing");
      return {
        ok: false,
        action: "restore_security_scope",
        runtime: "electron_bridge",
        security_scope_ref: ref,
        error: "Electron did not return a security scope release handle.",
        error_code: "mas_security_scope_bookmark_stale",
        bookmark_stale: true,
      };
    }
    if (!isRestoredSecurityScopedTargetAvailable(persisted.path)) {
      try {
        stopAccessing();
      } catch (err) {
        log(
          `Security-scoped resource release failed after stale restore: ${err.message}`,
        );
      }
      markSecurityScopedBookmarkStale(ref, "file_missing_or_invalid");
      return {
        ok: false,
        action: "restore_security_scope",
        runtime: "electron_bridge",
        security_scope_ref: ref,
        error: "Security-scoped bookmark target file is no longer available.",
        error_code: "mas_security_scope_bookmark_stale",
        bookmark_stale: true,
      };
    }
    rememberSecurityScopedResource(ref, stopAccessing, persisted.path);
    try {
      updateSecurityScopedBookmarkRecord(ref, (record) => ({
        ...record,
        status: "active",
        updated_at: new Date().toISOString(),
        last_restored_at: new Date().toISOString(),
        stale_reason: "",
        stale_at: "",
      }));
    } catch (err) {
      log(`Failed to mark security-scoped bookmark restored: ${err.message}`);
    }
    return {
      ok: true,
      action: "restore_security_scope",
      runtime: "electron_bridge",
      security_scope_ref: ref,
      security_scoped_bookmark: true,
      security_scope_restored: true,
      security_scope_bookmark_sha256: persisted.bookmarkSha256,
      path_sha256: persisted.pathSha256,
    };
  }

  function capabilityBlockedResult(capabilityId, action, error) {
    const entry = bridgeCapabilityMatrix.entries[capabilityId];
    if (!entry) return null;
    return {
      ok: false,
      action,
      runtime: "electron_bridge",
      error,
      error_code: entry.reason_code,
      capability_matrix_id: entry.id,
      capability_matrix_mode: entry.mode,
      capability_matrix_reason_code: entry.reason_code,
    };
  }

  function bridgeCapabilityBlocked(action, raw = {}) {
    if (BROWSER_FILE_TRANSFER_ACTIONS.has(action)) {
      const entry = bridgeCapabilityMatrix.entries.browser_upload_download;
      if (entry?.mode === "disabled") {
        return capabilityBlockedResult(
          "browser_upload_download",
          action,
          "Browser file transfer is disabled by the active capability matrix.",
        );
      }
      if (bridgeCapabilityMatrix.distribution === "app_store") {
        const securityScopeRef = securityScopeRefFromAction(raw);
        if (!securityScopeRef) {
          return capabilityBlockedResult(
            "browser_upload_download",
            action,
            "Browser file transfer requires an active security-scoped bookmark grant.",
          );
        }
        if (!activeSecurityScopedResources.has(securityScopeRef)) {
          const restored = restoreSecurityScopedResource(securityScopeRef);
          if (!restored.ok) {
            return {
              ...capabilityBlockedResult(
                "browser_upload_download",
                action,
                restored.error ||
                  "Browser file transfer requires a restorable security-scoped bookmark grant.",
              ),
              error_code: restored.error_code,
              security_scope_ref: securityScopeRef,
              bookmark_stale: Boolean(restored.bookmark_stale),
            };
          }
        }
        if (action === "upload_file") {
          const uploadPath = uploadFilePathFromAction(raw);
          const grantedPath =
            activeSecurityScopedResourcePaths.get(securityScopeRef);
          if (
            uploadPath &&
            (!grantedPath || path.resolve(uploadPath) !== grantedPath)
          ) {
            return {
              ...capabilityBlockedResult(
                "browser_upload_download",
                action,
                "The security-scoped bookmark grant belongs to a different file.",
              ),
              error_code: "mas_security_scope_path_mismatch",
              security_scope_ref: securityScopeRef,
            };
          }
        }
      }
    }

    if (BROWSER_WRITE_ACTIONS.has(action)) {
      const entry = bridgeCapabilityMatrix.entries.browser_write;
      if (entry?.mode === "disabled") {
        return capabilityBlockedResult(
          "browser_write",
          action,
          "Browser write automation is disabled by the active capability matrix.",
        );
      }
    }

    if (BROWSER_NAVIGATION_ACTIONS.has(action)) {
      const entry = bridgeCapabilityMatrix.entries.browser_read_navigation;
      if (entry?.mode === "disabled") {
        return capabilityBlockedResult(
          "browser_read_navigation",
          action,
          "Browser navigation is disabled by the active capability matrix.",
        );
      }
    }

    const task = actionTaskOptions(raw);
    const entry = bridgeCapabilityMatrix.entries.background_scheduled_execution;
    if (task.background && entry?.mode !== "enabled") {
      return capabilityBlockedResult(
        "background_scheduled_execution",
        action,
        "Background browser automation requires visible user control in this distribution.",
      );
    }

    return null;
  }

  function findTabByUrl(url, profileId = "", taskGroupId = "") {
    const normalized = normalizeUrl(url);
    const normalizedProfile = profileId ? sanitizeProfileId(profileId) : "";
    for (const tab of tabs.values()) {
      if (normalizedProfile && tab.profileId !== normalizedProfile) continue;
      if (taskGroupId && tab.taskGroupId !== taskGroupId) continue;
      if (
        (tab.pendingUrl && tab.pendingUrl === normalized) ||
        tab.url === normalized
      ) {
        return tab;
      }
    }
    return null;
  }

  function findTaskTab(profileId, taskGroupId) {
    if (!taskGroupId) return null;
    const normalizedProfile = sanitizeProfileId(profileId);
    for (const tab of tabs.values()) {
      if (
        tab.profileId === normalizedProfile &&
        tab.taskGroupId === taskGroupId
      ) {
        return tab;
      }
    }
    return null;
  }

  function findTabByWebContents(webContents) {
    if (!webContents) return null;
    for (const tab of tabs.values()) {
      if (tab.view?.webContents === webContents) return tab;
    }
    return null;
  }

  function serializeTab(tab) {
    const history = tab.view.webContents.navigationHistory;
    return {
      id: tab.id,
      profile_id: tab.profileId,
      url: tab.url || "about:blank",
      title: tab.title || "",
      loading: Boolean(tab.loading),
      can_go_back:
        typeof history?.canGoBack === "function"
          ? history.canGoBack()
          : Boolean(tab.view.webContents.canGoBack?.()),
      can_go_forward:
        typeof history?.canGoForward === "function"
          ? history.canGoForward()
          : Boolean(tab.view.webContents.canGoForward?.()),
      background: Boolean(tab.background),
      task_group_id: tab.taskGroupId || "",
    };
  }

  function getState(extra = {}) {
    const active = getActiveTab();
    return {
      ok: true,
      bridge_url: bridgeBaseUrl,
      active_tab_id: activeTabId,
      profile_id: active?.profileId || "default",
      tabs: Array.from(tabs.values()).map(serializeTab),
      current_action: currentAction,
      agent_status: agentStatus,
      surface: surfaceState(),
      capability_distribution: bridgeCapabilityMatrix.distribution,
      capability_matrix: bridgeCapabilityMatrix.entries,
      ...extra,
    };
  }

  function broadcastState(extra = {}) {
    if (Object.prototype.hasOwnProperty.call(extra, "current_action")) {
      currentAction = String(extra.current_action || "");
    }
    if (Object.prototype.hasOwnProperty.call(extra, "agent_status")) {
      agentStatus = String(extra.agent_status || "人工浏览");
    }
    const state = getState(extra);
    for (const win of browserHostWindows()) {
      win.webContents?.send?.("lantai:browser-workbench-state", state);
    }
  }

  async function browserUrlBlockedResult(input, action, profileId = "default") {
    const blocked = browserUrlPolicyResult(input, action);
    if (blocked) return blocked;
    const normalized = normalizeUrl(input);
    if (normalized === "about:blank") return null;
    let parsed;
    try {
      parsed = new URL(normalized);
    } catch {
      return {
        ok: false,
        action,
        error: "Browser Workbench URL is invalid.",
        error_code: "browser_url_invalid",
      };
    }
    const hostname = parsed.hostname.toLowerCase().replace(/\.$/, "");
    if (!hostname || net.isIP(hostname)) return null;
    let records;
    try {
      records = await lookupHost(hostname);
    } catch (err) {
      return {
        ok: false,
        action,
        error: `Browser Workbench could not resolve URL host: ${err.message}`,
        error_code: "browser_dns_resolution_failed",
      };
    }
    const addresses = (Array.isArray(records) ? records : [records])
      .map((record) => (typeof record === "string" ? record : record?.address))
      .filter(Boolean);
    const blockedAddresses = addresses.filter((address) =>
      isPrivateOrReservedIp(address),
    );
    if (blockedAddresses.length) {
      // Proxy clients commonly synthesize public DNS into 198.18/15. Only
      // accept that range for a hostname when Electron will actually proxy it.
      const syntheticBenchmarkOnly = blockedAddresses.every((address) =>
        isSyntheticBenchmarkIp(address),
      );
      if (syntheticBenchmarkOnly) {
        let proxyRules = "";
        try {
          if (typeof resolveProxyForUrl === "function") {
            proxyRules = await resolveProxyForUrl(normalized, profileId);
          } else {
            const partitionSession = session?.fromPartition?.(
              getProfilePartition(profileId),
            );
            proxyRules = await partitionSession?.resolveProxy?.(normalized);
          }
        } catch (err) {
          log(
            `Proxy resolution failed for synthetic DNS candidate: ${err.message}`,
          );
        }
        if (proxyRulesUseProxy(proxyRules)) {
          log(`Allowed proxy-routed synthetic DNS address for ${hostname}.`);
          return null;
        }
      }
      return {
        ok: false,
        action,
        error:
          "Browser Workbench blocks URLs resolving to localhost or private networks.",
        error_code: "browser_private_network_url_blocked",
      };
    }
    return null;
  }

  async function enforceLoadedUrlPolicy(tab, action) {
    const wc = tab.view.webContents;
    const finalUrl = wc.getURL?.() || tab.url;
    const blocked = await browserUrlBlockedResult(
      finalUrl,
      action,
      tab.profileId,
    );
    if (!blocked) return null;
    pushBounded(networkEvents, {
      tab_id: tab.id,
      type: "navigation_blocked_after_load",
      url: finalUrl,
      error_code: blocked.error_code,
    });
    try {
      wc.stop?.();
      if (finalUrl !== "about:blank") {
        await wc.loadURL("about:blank");
      }
    } catch {
      // Best-effort containment after a blocked redirect/final URL.
    }
    tab.url = "about:blank";
    tab.pendingUrl = "";
    broadcastState({ current_action: "已阻止不安全跳转" });
    return { ...blocked, final_url: finalUrl };
  }

  async function waitForTabLoadAndPolicy(tab, action) {
    if (tab.loadPromise) {
      try {
        await tab.loadPromise;
      } finally {
        tab.loadPromise = null;
      }
    }
    return enforceLoadedUrlPolicy(tab, action);
  }

  function layoutActiveView() {
    const active = getActiveTab();
    if (!active) return;
    if (sidecar.visible && sidecar.bounds) {
      active.view.setBounds(sidecar.bounds);
      active.view.setAutoResize?.({ width: false, height: false });
      return;
    }
    const win = workbenchVisible ? getWorkbenchWindow() : null;
    if (!win) return;
    const [width, height] = win.getContentSize();
    active.view.setBounds({
      x: 0,
      y: TOOLBAR_HEIGHT,
      width: Math.max(0, width),
      height: Math.max(0, height - TOOLBAR_HEIGHT),
    });
    active.view.setAutoResize?.({ width: true, height: true });
  }

  function attachActiveView() {
    const active = getActiveTab();
    for (const tab of tabs.values()) {
      detachViewFromHosts(tab.view);
    }
    if (!active) {
      broadcastState();
      return;
    }
    const win = sidecar.visible
      ? sidecar.controlOwner === "user"
        ? getMainHostWindow()
        : null
      : workbenchVisible
        ? getWorkbenchWindow()
        : null;
    layoutActiveView();
    if (win) {
      if (typeof win.addBrowserView === "function") {
        win.addBrowserView(active.view);
      } else {
        win.setBrowserView?.(active.view);
      }
    }
    broadcastState();
  }

  function ensureWorkbenchWindow({ focus = true } = {}) {
    sidecar = { ...sidecar, visible: false, controlOwner: "agent" };
    const existing = getWorkbenchWindow();
    if (existing) {
      if (focus) {
        existing.show();
        existing.focus();
      }
      workbenchVisible = true;
      return existing;
    }

    workbenchWindow = new BrowserWindow({
      width: 1280,
      height: 860,
      minWidth: 860,
      minHeight: 560,
      title: "澜台浏览器工作台",
      show: false,
      webPreferences: {
        preload: path.join(electronDir, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
        webSecurity: true,
      },
    });
    workbenchWindow.setMaxListeners?.(50);
    workbenchWindow.loadURL(
      `data:text/html;charset=utf-8,${encodeURIComponent(buildWorkbenchHtml())}`,
    );
    workbenchWindow.once("ready-to-show", () => {
      const win = getWorkbenchWindow();
      if (win) {
        if (focus) {
          win.show();
          win.focus();
        }
        workbenchVisible = true;
        attachActiveView();
      }
    });
    workbenchWindow.on("resize", layoutActiveView);
    workbenchWindow.on("closed", () => {
      workbenchVisible = false;
      if (sidecar.visible) {
        workbenchWindow = null;
        return;
      }
      for (const tab of tabs.values()) {
        try {
          tab.view.webContents.destroy?.();
        } catch {
          // Best-effort cleanup.
        }
      }
      tabs.clear();
      activeTabId = null;
      workbenchWindow = null;
    });
    return workbenchWindow;
  }

  function recoverRendererGoneTab(tab, details = {}) {
    if (tabs.get(tab.id) !== tab) return;
    const wasActive = activeTabId === tab.id;
    const lastUrl = tab.view.webContents.getURL?.() || tab.url || "about:blank";
    const reason = String(details.reason || "renderer_gone");
    try {
      detachViewFromHosts(tab.view);
    } catch {
      // The crashed renderer may already have detached its BrowserView.
    }
    tabs.delete(tab.id);
    if (wasActive) activeTabId = null;
    const replacement = createTab({
      profileId: tab.profileId,
      url: "about:blank",
      activate: wasActive,
      background: tab.background,
      taskGroupId: tab.taskGroupId,
    });
    rememberStaleTabRecovery(tab.id, {
      replacementTabId: replacement.id,
      profileId: tab.profileId,
      lastUrl,
      reason,
    });
    pushBounded(consoleEvents, {
      tab_id: tab.id,
      type: "renderer_gone",
      reason,
      replacement_tab_id: replacement.id,
      last_url: lastUrl,
    });
    broadcastState({
      current_action: "浏览器标签页异常退出，已创建安全空白页",
    });
  }

  function observeTab(tab) {
    const wc = tab.view.webContents;
    if (wc.setWindowOpenHandler && !tab.windowOpenObserved) {
      tab.windowOpenObserved = true;
      wc.setWindowOpenHandler((details = {}) => {
        const targetUrl = normalizeUrl(details.url || "");
        const blocked = browserUrlPolicyResult(targetUrl, "window_open");
        if (blocked) {
          pushBounded(networkEvents, {
            tab_id: tab.id,
            type: "window_open_blocked",
            url: targetUrl,
            error_code: blocked.error_code,
          });
          broadcastState({ current_action: "已阻止不安全弹窗 URL" });
          return { action: "deny" };
        }
        if (!targetUrl || targetUrl === "about:blank") {
          pushBounded(consoleEvents, {
            tab_id: tab.id,
            level: "warning",
            message: "Blocked blank popup window from page.",
          });
          broadcastState({ current_action: "已阻止空白弹窗" });
          return { action: "deny" };
        }
        const existing = findTabByUrl(targetUrl, tab.profileId);
        if (existing) {
          if (!tab.background) {
            activeTabId = existing.id;
            attachActiveView();
          }
          broadcastState({
            current_action: tab.background
              ? "后台弹出标签页已复用"
              : "已切换到现有弹出标签页",
          });
        } else {
          createTab({
            profileId: tab.profileId,
            url: targetUrl,
            activate: !tab.background,
            background: tab.background,
            taskGroupId: tab.taskGroupId,
            source: "window_open",
          });
          broadcastState({ current_action: "弹出窗口已接管为工作台标签页" });
        }
        pushBounded(networkEvents, {
          tab_id: tab.id,
          type: "window_open_routed_to_tab",
          url: targetUrl,
        });
        return { action: "deny" };
      });
    }
    wc.on?.("did-create-window", (childWindow, details = {}) => {
      const targetUrl = normalizeUrl(details.url || "");
      const blocked = browserUrlPolicyResult(targetUrl, "did_create_window");
      try {
        childWindow?.close?.();
      } catch {
        // Best-effort cleanup if Electron created a native child window anyway.
      }
      if (blocked) {
        pushBounded(networkEvents, {
          tab_id: tab.id,
          type: "did_create_window_blocked",
          url: targetUrl,
          error_code: blocked.error_code,
        });
      } else if (
        targetUrl &&
        targetUrl !== "about:blank" &&
        !findTabByUrl(targetUrl, tab.profileId, tab.taskGroupId)
      ) {
        createTab({
          profileId: tab.profileId,
          url: targetUrl,
          activate: !tab.background,
          background: tab.background,
          taskGroupId: tab.taskGroupId,
          source: "did_create_window",
        });
      }
      broadcastState({ current_action: "原生弹窗已收回到工作台" });
    });
    const session = wc.session;
    if (session && !session.__lantaiBrowserWorkbenchDownloadsObserved) {
      session.__lantaiBrowserWorkbenchDownloadsObserved = true;
      session.on?.("will-download", (_event, item, downloadWebContents) => {
        const downloadId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const ownerTab = findTabByWebContents(downloadWebContents) || tab;
        const url = item.getURL?.() || "";
        const filename = item.getFilename?.() || "";
        pushBounded(networkEvents, {
          tab_id: ownerTab.id,
          type: "download_start",
          download_id: downloadId,
          url,
          filename,
        });
        item.once?.("done", (_doneEvent, state) => {
          pushBounded(networkEvents, {
            tab_id: ownerTab.id,
            type: "download_done",
            download_id: downloadId,
            url,
            filename,
            state,
            path: item.getSavePath?.() || "",
          });
        });
      });
    }
    if (wc.debugger && !tab.debuggerObserved) {
      tab.debuggerObserved = true;
      try {
        if (!wc.debugger.isAttached?.()) {
          wc.debugger.attach("1.3");
        }
        Promise.resolve(wc.debugger.sendCommand("Network.enable", {})).catch(
          () => {},
        );
        wc.debugger.on?.("message", (_event, method, params = {}) => {
          if (method === "Network.requestWillBeSent") {
            pushBounded(networkEvents, {
              tab_id: tab.id,
              type: "request",
              request_id: params.requestId,
              url: params.request?.url || "",
              method: params.request?.method || "",
              resource_type: params.type || "",
            });
          } else if (method === "Network.responseReceived") {
            pushBounded(networkEvents, {
              tab_id: tab.id,
              type: "response",
              request_id: params.requestId,
              url: params.response?.url || "",
              status: params.response?.status,
              mime_type: params.response?.mimeType || "",
              headers: redactHeaders(params.response?.headers || {}),
            });
          } else if (method === "Network.loadingFailed") {
            pushBounded(networkEvents, {
              tab_id: tab.id,
              type: "request_failed",
              request_id: params.requestId,
              error: params.errorText || "",
              canceled: Boolean(params.canceled),
            });
          } else if (method === "Network.loadingFinished") {
            pushBounded(networkEvents, {
              tab_id: tab.id,
              type: "request_finished",
              request_id: params.requestId,
              encoded_data_length: params.encodedDataLength,
            });
          }
        });
      } catch (err) {
        pushBounded(consoleEvents, {
          tab_id: tab.id,
          level: "warning",
          message: `DevTools observer unavailable: ${err.message}`,
        });
      }
    }
    wc.on("did-start-loading", () => {
      tab.loading = true;
      broadcastState({ current_action: "页面加载中" });
    });
    wc.on("did-stop-loading", () => {
      tab.loading = false;
      tab.url = wc.getURL?.() || tab.url;
      broadcastState();
    });
    wc.on("did-navigate", (_event, url) => {
      tab.url = url || wc.getURL?.() || tab.url;
      pushBounded(networkEvents, {
        tab_id: tab.id,
        type: "navigation",
        url: tab.url,
      });
      broadcastState();
    });
    wc.on("did-navigate-in-page", (_event, url) => {
      tab.url = url || wc.getURL?.() || tab.url;
      broadcastState();
    });
    wc.on("page-title-updated", (_event, title) => {
      tab.title = title || tab.title;
      broadcastState();
    });
    wc.on("did-fail-load", (_event, errorCode, errorDescription, url) => {
      tab.loading = false;
      pushBounded(networkEvents, {
        tab_id: tab.id,
        type: "load_error",
        url,
        error_code: errorCode,
        error: errorDescription,
      });
      broadcastState({ current_action: `加载失败：${errorDescription}` });
    });
    wc.on("render-process-gone", (_event, details = {}) => {
      recoverRendererGoneTab(tab, details);
    });
    wc.on("console-message", (event, ...legacy) => {
      const { level, message, line, sourceId } = normalizeConsoleMessage(
        event,
        ...legacy,
      );
      pushBounded(consoleEvents, {
        tab_id: tab.id,
        level,
        message,
        line,
        source_id: sourceId,
      });
    });
  }

  function createTab({
    profileId = "default",
    url = "about:blank",
    activate = true,
    background = false,
    taskGroupId = "",
  } = {}) {
    const targetUrl = normalizeUrl(url);
    const id = `tab-${++tabSequence}`;
    const partition = getProfilePartition(profileId);
    const view = new BrowserView({
      webPreferences: {
        partition,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
      },
    });
    const tab = {
      id,
      profileId: sanitizeProfileId(profileId),
      partition,
      view,
      title: "新标签页",
      url: targetUrl,
      pendingUrl: targetUrl,
      loading: false,
      background: Boolean(background || !activate),
      taskGroupId: String(taskGroupId || "").trim(),
      loadPromise: null,
    };
    tabs.set(id, tab);
    observeTab(tab);
    if (activate) {
      activeTabId = id;
      attachActiveView();
    } else {
      broadcastState();
    }
    tab.loadPromise = Promise.resolve(view.webContents.loadURL(targetUrl));
    return tab;
  }

  function ensureActiveTab(profileId = "default") {
    const active = getActiveTab();
    if (active) return active;
    return createTab({ profileId });
  }

  function ensureActionTab(profileId, raw = {}, tabId = "") {
    const task = actionTaskOptions(raw);
    if (tabId && tabs.has(tabId)) {
      return tabs.get(tabId);
    }
    if (task.background) {
      return (
        findTaskTab(profileId, task.taskGroupId) ||
        createTab({
          profileId,
          url: raw.url || "about:blank",
          activate: false,
          background: true,
          taskGroupId: task.taskGroupId,
        })
      );
    }
    return ensureActiveTab(profileId);
  }

  async function snapshotDom(tab) {
    return tab.view.webContents.executeJavaScript(
      `(() => {
        const visibleText = (document.body?.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 12000);
        const cssEscape = (value) => {
          const text = String(value || "");
          if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(text);
          return text.replace(/[^a-zA-Z0-9_-]/g, (char) => "\\\\" + char);
        };
        const attributeSelector = (name, value) => {
          const escaped = String(value || "").replace(/\\\\/g, "\\\\\\\\").replace(/"/g, '\\"');
          return "[" + name + '=\"' + escaped + '\"]';
        };
        const elementSelector = (el, root = document) => {
          if (!el || !el.tagName) return "";
          const id = String(el.id || "").trim();
          if (id) {
            const candidate = "#" + cssEscape(id);
            try {
              if (root.querySelectorAll(candidate).length === 1) return candidate;
            } catch {}
          }
          for (const name of ["data-testid", "data-test", "data-qa", "name", "aria-label"]) {
            const value = String(el.getAttribute(name) || "").trim();
            if (!value) continue;
            const candidate = el.tagName.toLowerCase() + attributeSelector(name, value);
            try {
              if (root.querySelectorAll(candidate).length === 1) return candidate;
            } catch {}
          }
          const parts = [];
          let node = el;
          while (node && node.nodeType === Node.ELEMENT_NODE && node !== root && parts.length < 6) {
            let part = node.tagName.toLowerCase();
            const parent = node.parentElement;
            if (parent) {
              const siblings = Array.from(parent.children).filter((item) => item.tagName === node.tagName);
              if (siblings.length > 1) part += ":nth-of-type(" + (siblings.indexOf(node) + 1) + ")";
            }
            parts.unshift(part);
            node = parent;
          }
          return parts.join(" > ");
        };
        const visible = (el) => {
          if (!el) return false;
          const style = window.getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
        };
        const controlSnapshot = (el, root, scope = {}) => {
          const type = String(el.getAttribute("type") || "").toLowerCase();
          const hasValue = "value" in el ? String(el.value || "").length > 0 : Boolean(el.textContent?.trim());
          return {
            selector: elementSelector(el, root),
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute("role") || "",
            name: (el.innerText || el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.name || el.id || "").trim().slice(0, 160),
            type,
            value_length: type === "password" || type === "hidden" ? (hasValue ? 1 : 0) : ("value" in el ? String(el.value || "").length : String(el.textContent || "").trim().length),
            sensitive: type === "password" || type === "hidden" || /otp|captcha|verification/i.test(String(el.autocomplete || "") + " " + String(el.name || "")),
            disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
            visible: visible(el),
            checked: typeof el.checked === "boolean" ? el.checked : null,
            selected: typeof el.selected === "boolean" ? el.selected : null,
            frame_selector: scope.frame_selector || "",
            shadow_host_selector: scope.shadow_host_selector || ""
          };
        };
        const selector = "a[href],button,input,textarea,select,[role='button'],[role='link'],[role='textbox'],[contenteditable='true']";
        const controls = [];
        const collectControls = (root, scope = {}) => {
          for (const el of Array.from(root.querySelectorAll(selector))) {
            if (controls.length >= 250) break;
            controls.push(controlSnapshot(el, root, scope));
          }
        };
        collectControls(document);
        const frameDiagnostics = [];
        for (const frame of Array.from(document.querySelectorAll("iframe")).slice(0, 20)) {
          const frameSelector = elementSelector(frame, document);
          try {
            const frameDocument = frame.contentDocument || frame.contentWindow?.document;
            if (!frameDocument) throw new Error("frame_document_unavailable");
            collectControls(frameDocument, { frame_selector: frameSelector });
            frameDiagnostics.push({ frame_selector: frameSelector, accessible: true });
            for (const host of Array.from(frameDocument.querySelectorAll("*")).filter((item) => Boolean(item.shadowRoot)).slice(0, 20)) {
              collectControls(host.shadowRoot, {
                frame_selector: frameSelector,
                shadow_host_selector: elementSelector(host, frameDocument)
              });
            }
          } catch {
            frameDiagnostics.push({ frame_selector: frameSelector, accessible: false });
          }
        }
        for (const host of Array.from(document.querySelectorAll("*")).filter((item) => Boolean(item.shadowRoot)).slice(0, 40)) {
          collectControls(host.shadowRoot, { shadow_host_selector: elementSelector(host, document) });
        }
        const links = Array.from(document.querySelectorAll("a[href]")).slice(0, 80).map((a) => ({
          text: (a.innerText || a.getAttribute("aria-label") || "").trim().slice(0, 160),
          href: a.href,
          selector: elementSelector(a, document)
        }));
        const challengeText = [location.href, document.title, visibleText.slice(0, 4000)]
          .join(" ")
          .toLowerCase();
        const visibleSensitiveControls = controls.filter(
          (item) => item.visible && (item.sensitive || item.type === "password")
        );
        const includesAny = (words) => words.some((word) => challengeText.includes(word));
        let page_context = { kind: "normal", requires_manual_takeover: false };
        if (includesAny(["captcha", "人机验证", "安全验证"])) {
          page_context = { kind: "captcha", requires_manual_takeover: true };
        } else if (
          visibleSensitiveControls.length > 0 ||
          includesAny(["扫码登录", "短信验证码", "one-time password", "verification code"])
        ) {
          page_context = { kind: "login", requires_manual_takeover: true };
        } else if (
          includesAny(["checkout", "收银台", "确认付款"]) &&
          controls.some((item) => item.visible && /pay|付款|支付/i.test(item.name))
        ) {
          page_context = { kind: "payment", requires_manual_takeover: true };
        }
        return {
          title: document.title,
          url: location.href,
          text: visibleText,
          links,
          controls,
          page_context,
          diagnostics: {
            document_ready_state: document.readyState,
            control_count: controls.length,
            iframe_count: frameDiagnostics.length,
            accessible_iframe_count: frameDiagnostics.filter((item) => item.accessible).length,
            blocked_iframe_count: frameDiagnostics.filter((item) => !item.accessible).length,
            open_shadow_root_count: Array.from(document.querySelectorAll("*")).filter((item) => Boolean(item.shadowRoot)).length
          }
        };
      })()`,
    );
  }

  async function screenshot(tab, action) {
    const image = await tab.view.webContents.capturePage();
    const root = getEvidenceRoot();
    fs.mkdirSync(root, { recursive: true });
    const target = evidenceFilePath(tab, "png");
    fs.writeFileSync(target, image.toPNG());
    return {
      ok: true,
      action: action.action,
      runtime: "electron_bridge",
      page_id: tab.id,
      profile_id: tab.profileId,
      url: tab.view.webContents.getURL?.() || tab.url,
      path: target,
      screenshot_path: target,
      message: "Screenshot captured.",
    };
  }

  function baseActionResult(tab, action, extra = {}) {
    const wc = tab.view.webContents;
    return {
      ok: true,
      action,
      runtime: "electron_bridge",
      page_id: tab.id,
      profile_id: tab.profileId,
      url: wc.getURL?.() || tab.url,
      ...extra,
    };
  }

  async function waitForDownloadEvent(tab, raw = {}) {
    const timeoutMs = Math.min(
      Number(raw.timeout_ms ?? raw.metadata?.timeout_ms ?? 3000),
      30000,
    );
    const pattern = String(
      raw.filename ||
        raw.url ||
        raw.metadata?.filename ||
        raw.metadata?.url ||
        "",
    ).trim();
    const startedAt = Date.now();
    const findMatch = () =>
      networkEvents
        .filter((item) => item.tab_id === tab.id || !item.tab_id)
        .filter((item) => String(item.type || "").startsWith("download_"))
        .filter((item) => {
          if (!pattern) return true;
          return (
            String(item.filename || "").includes(pattern) ||
            String(item.url || "").includes(pattern)
          );
        })
        .slice(-5);

    while (Date.now() - startedAt < timeoutMs) {
      const matches = findMatch();
      const done = matches.find((item) => item.type === "download_done");
      if (done) return { ok: true, matches, done };
      if (matches.length && timeoutMs <= 250) return { ok: true, matches };
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    const matches = findMatch();
    return {
      ok: false,
      matches,
      error: matches.length
        ? "Download has not completed yet."
        : "No matching download event observed.",
    };
  }

  async function clearProfileStorage(profileId) {
    const normalizedProfile = sanitizeProfileId(profileId);
    if (normalizedProfile === "default") {
      return {
        ok: false,
        action: "clear_profile_storage",
        profile_id: normalizedProfile,
        error:
          "Default browser profile storage cannot be cleared through profile deletion.",
        error_code: "browser_default_profile_storage_protected",
      };
    }
    const partition = getProfilePartition(normalizedProfile);
    const closedTabs = closeTabsForProfile(normalizedProfile);
    if (!session || typeof session.fromPartition !== "function") {
      return {
        ok: false,
        action: "clear_profile_storage",
        profile_id: normalizedProfile,
        partition,
        closed_tabs: closedTabs,
        error: "Electron session API is unavailable.",
        error_code: "browser_profile_storage_unavailable",
      };
    }
    try {
      const partitionSession = session.fromPartition(partition);
      await partitionSession.flushStorageData?.();
      await partitionSession.clearStorageData?.();
      await partitionSession.clearCache?.();
      await partitionSession.clearHostResolverCache?.();
      await partitionSession.flushStorageData?.();
      pushBounded(networkEvents, {
        type: "profile_storage_cleared",
        profile_id: normalizedProfile,
        partition,
        closed_tabs: closedTabs,
      });
      broadcastState({ current_action: "浏览器 Profile 存储已清理" });
      return {
        ok: true,
        action: "clear_profile_storage",
        runtime: "electron_bridge",
        profile_id: normalizedProfile,
        partition,
        closed_tabs: closedTabs,
        message: "Browser profile storage cleared.",
      };
    } catch (err) {
      return {
        ok: false,
        action: "clear_profile_storage",
        runtime: "electron_bridge",
        profile_id: normalizedProfile,
        partition,
        closed_tabs: closedTabs,
        error: err.message,
        error_code: "browser_profile_storage_clear_failed",
      };
    }
  }

  async function selectFileGrant(payload = {}) {
    const requestPayload =
      payload && typeof payload === "object" ? payload : {};
    if (!dialog || typeof dialog.showOpenDialog !== "function") {
      return {
        ok: false,
        error: "Electron dialog API is unavailable.",
        error_code: "electron_dialog_unavailable",
      };
    }
    const options = {
      properties: ["openFile"],
      securityScopedBookmarks:
        bridgeCapabilityMatrix.distribution === "app_store" ? true : undefined,
    };
    const requestedDefaultPath = String(
      requestPayload.default_path || requestPayload.defaultPath || "",
    ).trim();
    if (requestedDefaultPath) {
      options.defaultPath = requestedDefaultPath;
    }
    const win = getWorkbenchWindow();
    const result = win
      ? await dialog.showOpenDialog(win, options)
      : await dialog.showOpenDialog(options);
    if (result.canceled || !result.filePaths?.length) {
      return null;
    }
    const filePath = String(result.filePaths[0] || "");
    const bookmark = String(result.bookmarks?.[0] || "");
    if (bridgeCapabilityMatrix.distribution !== "app_store") {
      return {
        ok: true,
        path: filePath,
        security_scoped_bookmark: false,
        security_scope_ref: "",
        security_scope_bookmark_sha256: "",
      };
    }
    if (!bookmark) {
      return {
        ok: false,
        path: filePath,
        error: "Mac App Store file grants require a security-scoped bookmark.",
        error_code: "mas_security_scoped_bookmark_missing",
      };
    }
    if (typeof app.startAccessingSecurityScopedResource !== "function") {
      return {
        ok: false,
        path: filePath,
        error:
          "Electron cannot restore the security-scoped bookmark in this runtime.",
        error_code: "mas_security_scope_start_unavailable",
      };
    }
    const { ref, bookmarkSha256 } = securityScopeRefForSelection(
      filePath,
      bookmark,
    );
    let stopAccessing;
    try {
      stopAccessing = app.startAccessingSecurityScopedResource(bookmark);
    } catch (err) {
      return {
        ok: false,
        path: filePath,
        error: err.message,
        error_code: "mas_security_scope_start_failed",
      };
    }
    if (typeof stopAccessing !== "function") {
      return {
        ok: false,
        path: filePath,
        error: "Electron did not return a security scope release handle.",
        error_code: "mas_security_scope_start_failed",
      };
    }
    const persisted = persistSecurityScopedBookmarkRecord({
      ref,
      filePath,
      bookmark,
      bookmarkSha256,
    });
    if (!persisted.ok) {
      try {
        stopAccessing();
      } catch {
        // Best-effort release after persistence failure.
      }
      return {
        ok: false,
        path: filePath,
        error: persisted.error,
        error_code: persisted.error_code,
      };
    }
    rememberSecurityScopedResource(ref, stopAccessing, filePath);
    return {
      ok: true,
      path: filePath,
      security_scoped_bookmark: true,
      security_scope_ref: ref,
      security_scope_bookmark_sha256: bookmarkSha256,
      security_scope_bookmark_persisted: true,
      path_sha256: persisted.path_sha256,
    };
  }

  async function performAction(raw = {}) {
    const action = String(raw.action || "status")
      .trim()
      .toLowerCase();
    const profileId = sanitizeProfileId(
      raw.profile_id || raw.profileId || "default",
    );
    const tabId = String(raw.tab_id || raw.page_id || raw.pageId || "");
    if (action === "status" || action === "state") return getState();
    const staleRecovery = tabId ? staleTabRecoveries.get(tabId) : null;
    if (staleRecovery) {
      return {
        ok: false,
        action,
        runtime: "electron_bridge",
        error:
          "Browser tab renderer exited. Use the replacement tab and inspect " +
          "page state before retrying; the previous action was not replayed.",
        error_code: "browser_tab_renderer_gone",
        recoverable: true,
        stale_page_id: tabId,
        replacement_page_id: staleRecovery.replacementTabId,
        profile_id: staleRecovery.profileId,
        last_url: staleRecovery.lastUrl,
        reason: staleRecovery.reason,
      };
    }
    if (action === "release_security_scope") {
      const securityScopeRef = securityScopeRefFromAction(raw);
      const metadata =
        raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
      const forgetBookmark = Boolean(
        raw.forget_bookmark ||
        raw.forgetBookmark ||
        metadata.forget_bookmark ||
        metadata.forgetBookmark,
      );
      const released = releaseSecurityScopedResource(securityScopeRef);
      const forgotten = forgetBookmark
        ? forgetSecurityScopedBookmark(securityScopeRef)
        : false;
      return {
        ok: released || forgotten,
        action,
        runtime: "electron_bridge",
        security_scope_ref: securityScopeRef,
        released,
        bookmark_forgotten: forgotten,
      };
    }
    if (action === "restore_security_scope") {
      return restoreSecurityScopedResource(securityScopeRefFromAction(raw));
    }
    if (action === "forget_security_scope") {
      const securityScopeRef = securityScopeRefFromAction(raw);
      const released = releaseSecurityScopedResource(securityScopeRef);
      const forgotten = forgetSecurityScopedBookmark(securityScopeRef);
      return {
        ok: released || forgotten,
        action,
        runtime: "electron_bridge",
        security_scope_ref: securityScopeRef,
        released,
        bookmark_forgotten: forgotten,
      };
    }
    const capabilityBlocked = bridgeCapabilityBlocked(action, raw);
    if (capabilityBlocked) return capabilityBlocked;
    if (action === "clear_profile_storage") {
      return clearProfileStorage(profileId);
    }
    if (action === "close_task_group") {
      const task = actionTaskOptions(raw);
      if (!task.taskGroupId) {
        return {
          ok: false,
          action,
          runtime: "electron_bridge",
          profile_id: profileId,
          error: "Task group id is required for browser task cleanup.",
          error_code: "browser_task_group_required",
        };
      }
      const closedTabs = closeTabsForTaskGroup(profileId, task.taskGroupId);
      return getState({
        action,
        runtime: "electron_bridge",
        profile_id: profileId,
        task_group_id: task.taskGroupId,
        closed_tabs: closedTabs,
        message: "Browser task tabs closed.",
      });
    }
    if (action === "open_workbench") {
      const blocked = await browserUrlBlockedResult(
        raw.url || "about:blank",
        action,
        profileId,
      );
      if (blocked) return blocked;
      ensureWorkbenchWindow();
      if (!getActiveTab() && tabs.size) {
        activeTabId = tabs.keys().next().value || null;
        attachActiveView();
      }
      let createdTab = null;
      if (!getActiveTab()) {
        createdTab = createTab({ profileId, url: raw.url || "about:blank" });
      }
      if (createdTab) {
        const loadedBlocked = await waitForTabLoadAndPolicy(createdTab, action);
        if (loadedBlocked) return loadedBlocked;
      }
      return getState({ message: "Browser Workbench opened." });
    }

    if (action === "agent_pause") {
      broadcastState({
        agent_status: "已暂停",
        current_action: "等待用户继续",
        message: "Agent browser control paused.",
      });
      return getState({ message: "Agent browser control paused." });
    }

    if (action === "agent_resume") {
      broadcastState({
        agent_status: "Agent 接管",
        current_action: "",
        message: "Agent browser control resumed.",
      });
      return getState({ message: "Agent browser control resumed." });
    }

    if (action === "agent_stop" || action === "agent_terminate") {
      broadcastState({
        agent_status: "已终止",
        current_action: "用户已终止浏览器任务",
        message: "Agent browser control stopped.",
      });
      return getState({ message: "Agent browser control stopped." });
    }

    if (action === "new_tab") {
      const blocked = await browserUrlBlockedResult(
        raw.url || "about:blank",
        action,
        profileId,
      );
      if (blocked) return blocked;
      const task = actionTaskOptions(raw);
      const tab = createTab({
        profileId,
        url: raw.url || "about:blank",
        activate: !task.background,
        background: task.background,
        taskGroupId: task.taskGroupId,
      });
      const loadedBlocked = await waitForTabLoadAndPolicy(tab, action);
      if (loadedBlocked) return loadedBlocked;
      return { ...getState(), page_id: tab.id, url: tab.url };
    }

    if (action === "select_tab") {
      if (!tabs.has(tabId)) {
        return { ok: false, error: `Browser tab not found: ${tabId}` };
      }
      activeTabId = tabId;
      attachActiveView();
      return getState();
    }

    if (action === "close_tab") {
      const targetId = tabId || activeTabId;
      const target = tabs.get(targetId);
      if (!target)
        return { ok: false, error: `Browser tab not found: ${targetId}` };
      try {
        detachViewFromHosts(target.view);
        target.view.webContents.destroy?.();
      } catch {
        // Best-effort cleanup.
      }
      tabs.delete(targetId);
      activeTabId = tabs.keys().next().value || null;
      if (activeTabId) attachActiveView();
      else broadcastState();
      return getState({ message: "Tab closed." });
    }

    if (action === "navigate" || action === "open") {
      const blocked = await browserUrlBlockedResult(
        raw.url || "about:blank",
        action,
        profileId,
      );
      if (blocked) return blocked;
    }

    const task = actionTaskOptions(raw);
    const tab = ensureActionTab(profileId, raw, tabId);
    if (tabId && tabs.has(tabId) && !task.background) {
      activeTabId = tabId;
      attachActiveView();
    }
    const wc = tab.view.webContents;

    if (action === "navigate" || action === "open") {
      const url = normalizeUrl(raw.url);
      await wc.loadURL(url);
      tab.url = wc.getURL?.() || url;
      const loadedBlocked = await enforceLoadedUrlPolicy(tab, action);
      if (loadedBlocked) return loadedBlocked;
      return { ...getState(), page_id: tab.id, url: tab.url };
    }
    if (action === "reload") {
      wc.reload?.();
      return getState({ page_id: tab.id, message: "Reload requested." });
    }
    if (action === "stop") {
      wc.stop?.();
      return getState({ page_id: tab.id, message: "Stop requested." });
    }
    if (action === "go_back") {
      const history = wc.navigationHistory;
      if (history?.canGoBack?.()) history.goBack();
      else if (wc.canGoBack?.()) wc.goBack();
      return getState({ page_id: tab.id });
    }
    if (action === "go_forward") {
      const history = wc.navigationHistory;
      if (history?.canGoForward?.()) history.goForward();
      else if (wc.canGoForward?.()) wc.goForward();
      return getState({ page_id: tab.id });
    }
    if (action === "screenshot" || action === "take_screenshot") {
      return screenshot(tab, { action });
    }
    if (
      action === "snapshot_dom" ||
      action === "snapshot_ax" ||
      action === "snapshot"
    ) {
      const data = await snapshotDom(tab);
      return {
        ok: true,
        action,
        runtime: "electron_bridge",
        page_id: tab.id,
        profile_id: tab.profileId,
        url: wc.getURL?.() || tab.url,
        data,
      };
    }
    if (action === "evaluate") {
      const safeScript = resolveSafeEvaluateScript(raw);
      if (!safeScript.ok) {
        return baseActionResult(tab, action, {
          ok: false,
          error: safeScript.error,
          error_code: safeScript.error_code,
        });
      }
      const data = await wc.executeJavaScript(
        `(() => {
          const fn = ${safeScript.source};
          if (typeof fn === "function") return fn();
          return fn;
        })()`,
      );
      return baseActionResult(tab, action, { data: { value: data } });
    }
    if (action === "extract_table" || action === "extract_tables") {
      const selector =
        String(raw.selector || raw.metadata?.selector || "table").trim() ||
        "table";
      const limit = Math.max(
        1,
        Math.min(Number(raw.limit ?? raw.metadata?.limit ?? 5), 20),
      );
      const tables = await wc.executeJavaScript(
        `(() => {
          const extractKind = "table";
          const selector = ${JSON.stringify(selector)};
          const limit = ${JSON.stringify(limit)};
          const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          return Array.from(document.querySelectorAll(selector)).slice(0, limit).map((table, index) => {
            const rows = Array.from(table.querySelectorAll("tr")).slice(0, 80).map((row) =>
              Array.from(row.querySelectorAll("th,td")).slice(0, 40).map((cell) => clean(cell.innerText))
            ).filter((row) => row.some(Boolean));
            return {
              index,
              caption: clean(table.querySelector("caption")?.innerText),
              row_count: rows.length,
              rows,
              text: clean(table.innerText).slice(0, 5000),
            };
          });
        })()`,
      );
      return baseActionResult(tab, action, {
        data: {
          tables,
          count: Array.isArray(tables) ? tables.length : 0,
          selector,
        },
      });
    }
    if (action === "extract_list" || action === "extract_items") {
      const selector = String(
        raw.selector ||
          raw.metadata?.selector ||
          "article, [role=listitem], li, .item, .card",
      ).trim();
      const limit = Math.max(
        1,
        Math.min(Number(raw.limit ?? raw.metadata?.limit ?? 80), 200),
      );
      const items = await wc.executeJavaScript(
        `(() => {
          const extractKind = "list";
          const selector = ${JSON.stringify(selector)};
          const limit = ${JSON.stringify(limit)};
          const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const seen = new Set();
          const out = [];
          for (const el of Array.from(document.querySelectorAll(selector))) {
            const text = clean(el.innerText || el.textContent).slice(0, 2000);
            if (!text || seen.has(text)) continue;
            seen.add(text);
            const anchor = el.matches("a") ? el : el.querySelector("a[href]");
            out.push({
              text,
              href: anchor?.href || "",
              role: el.getAttribute("role") || "",
              tag: el.tagName.toLowerCase(),
            });
            if (out.length >= limit) break;
          }
          return out;
        })()`,
      );
      return baseActionResult(tab, action, {
        data: {
          items,
          count: Array.isArray(items) ? items.length : 0,
          selector,
        },
      });
    }
    if (action === "console_list" || action === "console_messages") {
      return {
        ok: true,
        action,
        runtime: "electron_bridge",
        page_id: tab.id,
        profile_id: tab.profileId,
        url: wc.getURL?.() || tab.url,
        data: consoleEvents
          .filter((item) => !item.tab_id || item.tab_id === tab.id)
          .slice(-80),
      };
    }
    if (action === "network_list" || action === "network_requests") {
      return {
        ok: true,
        action,
        runtime: "electron_bridge",
        page_id: tab.id,
        profile_id: tab.profileId,
        url: wc.getURL?.() || tab.url,
        data: networkEvents
          .filter((item) => !item.tab_id || item.tab_id === tab.id)
          .slice(-80),
      };
    }
    if (action === "network_get") {
      const requestId = String(
        raw.request_id || raw.requestId || raw.metadata?.request_id || "",
      );
      const urlNeedle = String(raw.url || raw.metadata?.url || "").trim();
      const matches = networkEvents
        .filter((item) => !item.tab_id || item.tab_id === tab.id)
        .filter((item) => {
          if (requestId && item.request_id === requestId) return true;
          if (urlNeedle && String(item.url || "").includes(urlNeedle))
            return true;
          return !requestId && !urlNeedle;
        })
        .slice(-20);
      let body = null;
      let bodyError = null;
      if (requestId) {
        try {
          body = sanitizeNetworkBody(
            await sendCdp(wc, "Network.getResponseBody", { requestId }),
          );
        } catch (err) {
          bodyError = err.message;
        }
      }
      return baseActionResult(tab, action, {
        data: { matches, body, body_error: bodyError },
        ok: matches.length > 0 || Boolean(body),
        error: matches.length || body ? null : "Network request not found.",
      });
    }
    if (action === "performance_trace") {
      const data = await wc.executeJavaScript(
        `(() => {
          const navigation = performance.getEntriesByType("navigation").map((entry) => entry.toJSON?.() || entry);
          const resources = performance.getEntriesByType("resource").slice(-120).map((entry) => ({
            name: entry.name,
            initiatorType: entry.initiatorType,
            duration: entry.duration,
            transferSize: entry.transferSize,
            encodedBodySize: entry.encodedBodySize,
            decodedBodySize: entry.decodedBodySize
          }));
          const paint = performance.getEntriesByType("paint").map((entry) => ({ name: entry.name, startTime: entry.startTime }));
          return { navigation, resources, paint, memory: performance.memory || null, now: performance.now() };
        })()`,
      );
      return baseActionResult(tab, action, { data });
    }
    if (action === "screencast_start") {
      try {
        await sendCdp(wc, "Page.startScreencast", {
          format: raw.format || "jpeg",
          quality: Number(raw.quality ?? 70),
          maxWidth: Number(raw.max_width ?? raw.metadata?.max_width ?? 1280),
          maxHeight: Number(raw.max_height ?? raw.metadata?.max_height ?? 900),
        });
      } catch (err) {
        pushBounded(consoleEvents, {
          tab_id: tab.id,
          level: "warning",
          message: `Screencast best-effort start failed: ${err.message}`,
        });
      }
      broadcastState({
        current_action: "录屏观察中",
        agent_status: "Agent 接管",
      });
      return baseActionResult(tab, action, { data: { screencast: "started" } });
    }
    if (action === "screencast_stop") {
      try {
        await sendCdp(wc, "Page.stopScreencast", {});
      } catch (err) {
        pushBounded(consoleEvents, {
          tab_id: tab.id,
          level: "warning",
          message: `Screencast best-effort stop failed: ${err.message}`,
        });
      }
      broadcastState({ current_action: "录屏观察已停止" });
      return baseActionResult(tab, action, { data: { screencast: "stopped" } });
    }
    if (action === "click") {
      if (raw.selector) {
        const clicked = await wc.executeJavaScript(
          `(() => {
            const selector = ${JSON.stringify(String(raw.selector))};
            const frameSelector = ${JSON.stringify(String(raw.frame_selector || raw.metadata?.frame_selector || ""))};
            const shadowHostSelector = ${JSON.stringify(String(raw.shadow_host_selector || raw.metadata?.shadow_host_selector || ""))};
            ${scopedSelectorHelpersSource()}
            const target = queryScoped(selector, frameSelector, shadowHostSelector);
            const el = target.el;
            if (!el) return { ok: false, scope: target.scope, error: target.error || "Element not found." };
            el.scrollIntoView({ block: "center", inline: "center" });
            el.click();
            return { ok: true, scope: target.scope };
          })()`,
        );
        return baseActionResult(tab, action, {
          ok: Boolean(clicked?.ok),
          data: { scope: clicked?.scope || {} },
          error: clicked?.ok ? null : clicked?.error || "Element not found.",
        });
      }
      return {
        ok: false,
        action,
        error: "click requires selector or click_at coordinates.",
      };
    }
    if (action === "hover") {
      if (raw.selector) {
        const hovered = await wc.executeJavaScript(
          `(() => {
            const el = document.querySelector(${JSON.stringify(String(raw.selector))});
            if (!el) return false;
            el.scrollIntoView({ block: "center", inline: "center" });
            const rect = el.getBoundingClientRect();
            const eventInit = { bubbles: true, cancelable: true, view: window, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
            el.dispatchEvent(new MouseEvent("mouseover", eventInit));
            el.dispatchEvent(new MouseEvent("mousemove", eventInit));
            return true;
          })()`,
        );
        return baseActionResult(tab, action, {
          ok: Boolean(hovered),
          error: hovered ? null : "Element not found.",
        });
      }
      const x = Number(raw.x ?? raw.metadata?.x ?? 0);
      const y = Number(raw.y ?? raw.metadata?.y ?? 0);
      wc.sendInputEvent?.({
        type: "mouseMove",
        x,
        y,
        movementX: 0,
        movementY: 0,
      });
      return baseActionResult(tab, action);
    }
    if (action === "click_at") {
      const x = Number(raw.x ?? raw.metadata?.x ?? 0);
      const y = Number(raw.y ?? raw.metadata?.y ?? 0);
      wc.sendInputEvent?.({
        type: "mouseDown",
        x,
        y,
        button: "left",
        clickCount: 1,
      });
      wc.sendInputEvent?.({
        type: "mouseUp",
        x,
        y,
        button: "left",
        clickCount: 1,
      });
      return {
        ok: true,
        action,
        runtime: "electron_bridge",
        page_id: tab.id,
        profile_id: tab.profileId,
        url: wc.getURL?.() || tab.url,
      };
    }
    if (action === "drag") {
      const pathPoints = normalizeDragPath(raw);
      if (pathPoints.length < 2) {
        return baseActionResult(tab, action, {
          ok: false,
          error: "drag requires a path or start/end coordinates.",
        });
      }
      const [start, ...rest] = pathPoints;
      wc.sendInputEvent?.({
        type: "mouseMove",
        x: start.x,
        y: start.y,
        movementX: 0,
        movementY: 0,
      });
      wc.sendInputEvent?.({
        type: "mouseDown",
        x: start.x,
        y: start.y,
        button: "left",
        clickCount: 1,
      });
      for (const point of rest) {
        wc.sendInputEvent?.({
          type: "mouseMove",
          x: point.x,
          y: point.y,
          movementX: point.x - start.x,
          movementY: point.y - start.y,
        });
      }
      const end = rest[rest.length - 1];
      wc.sendInputEvent?.({
        type: "mouseUp",
        x: end.x,
        y: end.y,
        button: "left",
        clickCount: 1,
      });
      return baseActionResult(tab, action, {
        data: { points: pathPoints.length },
      });
    }
    if (action === "type") {
      const text = String(raw.text || "");
      if (raw.selector) {
        const filled = await wc.executeJavaScript(
          `(() => {
            const selector = ${JSON.stringify(String(raw.selector))};
            const frameSelector = ${JSON.stringify(String(raw.frame_selector || raw.metadata?.frame_selector || ""))};
            const shadowHostSelector = ${JSON.stringify(String(raw.shadow_host_selector || raw.metadata?.shadow_host_selector || ""))};
            ${scopedSelectorHelpersSource()}
            const target = queryScoped(selector, frameSelector, shadowHostSelector);
            const el = target.el;
            if (!el) return { ok: false, scope: target.scope, error: target.error || "Element not found." };
            el.scrollIntoView({ block: "center", inline: "center" });
            el.focus();
            let mode = "text";
            if ("value" in el) {
              el.value = ${JSON.stringify(text)};
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
              mode = "value";
            } else {
              el.textContent = ${JSON.stringify(text)};
            }
            return { ok: true, scope: target.scope, mode };
          })()`,
        );
        return baseActionResult(tab, action, {
          ok: Boolean(filled?.ok),
          data: { scope: filled?.scope || {}, mode: filled?.mode || "" },
          error: filled?.ok ? null : filled?.error || "Element not found.",
        });
      }
      wc.insertText?.(text);
      return {
        ok: true,
        action,
        runtime: "electron_bridge",
        page_id: tab.id,
        profile_id: tab.profileId,
        url: wc.getURL?.() || tab.url,
      };
    }
    if (action === "fill_form") {
      const fields = normalizeFormFields(raw);
      if (!fields.length) {
        return baseActionResult(tab, action, {
          ok: false,
          error: "fill_form requires fields.",
        });
      }
      const data = await wc.executeJavaScript(
        `(() => {
          const fields = ${JSON.stringify(fields)};
          const globalFrameSelector = ${JSON.stringify(String(raw.frame_selector || raw.metadata?.frame_selector || ""))};
          const globalShadowHostSelector = ${JSON.stringify(String(raw.shadow_host_selector || raw.metadata?.shadow_host_selector || ""))};
          const filled = [];
          const missing = [];
          const details = [];
          ${scopedSelectorHelpersSource()}
          const visibleScore = (el) => {
            if (!el) return -1;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            if (style.display === "none" || style.visibility === "hidden" || rect.width <= 0 || rect.height <= 0) return -1;
            return rect.width * rect.height;
          };
          const findTarget = (field) => {
            const frameSelector = field.frame_selector || globalFrameSelector;
            const shadowHostSelector = field.shadow_host_selector || globalShadowHostSelector;
            const result = queryAllScoped(field.selector, frameSelector, shadowHostSelector);
            const el = result.nodes
              .map((el, index) => ({ el, index, score: visibleScore(el) }))
              .filter((item) => item.score >= 0)
              .sort((a, b) => b.score - a.score || a.index - b.index)[0]?.el || result.nodes[0] || null;
            return { el, scope: result.scope, error: result.error };
          };
          const dispatch = (el, type, extra = {}) => {
            try {
              const event = type === "input" || type === "beforeinput"
                ? new InputEvent(type, { bubbles: true, cancelable: true, inputType: "insertText", data: extra.data || "" })
                : new Event(type, { bubbles: true, cancelable: true });
              el.dispatchEvent(event);
            } catch {
              el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true }));
            }
          };
          const setNativeValue = (el, value) => {
            const prototype = Object.getPrototypeOf(el);
            const descriptor = Object.getOwnPropertyDescriptor(prototype, "value") ||
              Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value") ||
              Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
            if (descriptor?.set) {
              descriptor.set.call(el, value);
            } else {
              el.value = value;
            }
          };
          const isRichEditable = (el) => {
            const className = String(el.className || "");
            return Boolean(
              el.isContentEditable ||
              el.getAttribute("contenteditable") === "true" ||
              el.getAttribute("role") === "textbox" ||
              className.includes("ProseMirror") ||
              className.includes("ql-editor") ||
              className.includes("CodeMirror")
            );
          };
          const writeElement = (el, value) => {
            const text = String(value ?? "");
            el.scrollIntoView({ block: "center", inline: "nearest" });
            el.focus();
            if ("value" in el && !isRichEditable(el)) {
              setNativeValue(el, text);
              dispatch(el, "input", { data: text.slice(0, 1000) });
              dispatch(el, "change");
              return "value";
            }
            if (isRichEditable(el)) {
              let mode = "contenteditable_text";
              try {
                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                selection.removeAllRanges();
                selection.addRange(range);
                dispatch(el, "beforeinput", { data: text.slice(0, 1000) });
                if (document.execCommand("insertText", false, text)) {
                  mode = "contenteditable_insert_text";
                } else {
                  el.textContent = text;
                }
              } catch {
                el.textContent = text;
              }
              dispatch(el, "input", { data: text.slice(0, 1000) });
              dispatch(el, "change");
              return mode;
            }
            el.textContent = text;
            dispatch(el, "input", { data: text.slice(0, 1000) });
            dispatch(el, "change");
            return "text";
          };
          for (const field of fields) {
            const target = findTarget(field);
            const el = target.el;
            if (!el) {
              missing.push(field.selector);
              details.push({
                selector: field.selector,
                mode: "missing",
                tag: "",
                scope: target.scope,
                error: target.error || "Element not found.",
                value_length: String(field.value ?? "").length,
              });
              continue;
            }
            const mode = writeElement(el, field.value);
            filled.push(field.selector);
            details.push({
              selector: field.selector,
              mode,
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute("role") || "",
              contenteditable: Boolean(el.isContentEditable || el.getAttribute("contenteditable")),
              scope: target.scope,
              value_length: String(field.value ?? "").length,
            });
          }
          return { filled, missing, details };
        })()`,
      );
      return baseActionResult(tab, action, {
        ok: Boolean(data?.filled?.length) && !data?.missing?.length,
        data,
        error: data?.missing?.length
          ? `Missing fields: ${data.missing.join(", ")}`
          : null,
      });
    }
    if (action === "select") {
      const selected = await wc.executeJavaScript(
        `(() => {
          const selector = ${JSON.stringify(String(raw.selector || ""))};
          const frameSelector = ${JSON.stringify(String(raw.frame_selector || raw.metadata?.frame_selector || ""))};
          const shadowHostSelector = ${JSON.stringify(String(raw.shadow_host_selector || raw.metadata?.shadow_host_selector || ""))};
          ${scopedSelectorHelpersSource()}
          const target = queryScoped(selector, frameSelector, shadowHostSelector);
          const el = target.el;
          if (!el) return { ok: false, scope: target.scope, error: target.error || "Element not found." };
          el.value = ${JSON.stringify(String(raw.text || raw.value || ""))};
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return { ok: true, scope: target.scope };
        })()`,
      );
      return baseActionResult(tab, action, {
        ok: Boolean(selected?.ok),
        data: { scope: selected?.scope || {} },
        error: selected?.ok ? null : selected?.error || "Element not found.",
      });
    }
    if (action === "upload_file") {
      const securityScopeRef = securityScopeRefFromAction(raw);
      const withSecurityScopeRelease = (result) => {
        if (securityScopeRef) {
          releaseSecurityScopedResource(securityScopeRef);
        }
        return result;
      };
      const selector = String(
        raw.selector || raw.metadata?.selector || "input[type=file]",
      ).trim();
      const filePath = uploadFilePathFromAction(raw);
      if (!filePath) {
        return withSecurityScopeRelease(
          baseActionResult(tab, action, {
            ok: false,
            error: "upload_file requires file_path.",
          }),
        );
      }
      if (!raw.file_grant_id && !raw.metadata?.file_grant_id) {
        return withSecurityScopeRelease(
          baseActionResult(tab, action, {
            ok: false,
            error: "Browser file upload requires a picker-backed file grant.",
            error_code: "browser_file_grant_required",
          }),
        );
      }
      if (!fs.existsSync(filePath)) {
        return withSecurityScopeRelease(
          baseActionResult(tab, action, {
            ok: false,
            error: `Upload file does not exist: ${filePath}`,
          }),
        );
      }
      const stat = fs.lstatSync(filePath);
      if (!stat.isFile() || stat.isSymbolicLink()) {
        return withSecurityScopeRelease(
          baseActionResult(tab, action, {
            ok: false,
            error: "Browser file upload grant must reference a regular file.",
            error_code: "browser_file_grant_invalid",
          }),
        );
      }
      try {
        const documentResult = await sendCdp(wc, "DOM.getDocument", {
          pierce: true,
        });
        const nodeResult = await sendCdp(wc, "DOM.querySelector", {
          nodeId: documentResult.root.nodeId,
          selector,
        });
        if (!nodeResult.nodeId) {
          return withSecurityScopeRelease(
            baseActionResult(tab, action, {
              ok: false,
              error: "File input element not found.",
            }),
          );
        }
        await sendCdp(wc, "DOM.setFileInputFiles", {
          nodeId: nodeResult.nodeId,
          files: [filePath],
        });
        return withSecurityScopeRelease(
          baseActionResult(tab, action, {
            data: { selector, file_name: path.basename(filePath) },
          }),
        );
      } catch (err) {
        return withSecurityScopeRelease(
          baseActionResult(tab, action, { ok: false, error: err.message }),
        );
      }
    }
    if (action === "download_wait") {
      const data = await waitForDownloadEvent(tab, raw);
      return baseActionResult(tab, action, {
        ok: data.ok,
        data: { matches: data.matches, done: data.done || null },
        error: data.ok ? null : data.error,
      });
    }
    if (action === "pdf") {
      const data = await wc.printToPDF?.({ printBackground: true });
      const root = getEvidenceRoot();
      fs.mkdirSync(root, { recursive: true });
      const target = evidenceFilePath(tab, "pdf");
      fs.writeFileSync(target, data);
      return {
        ok: true,
        action,
        runtime: "electron_bridge",
        page_id: tab.id,
        profile_id: tab.profileId,
        url: wc.getURL?.() || tab.url,
        path: target,
      };
    }

    return {
      ok: false,
      action,
      runtime: "electron_bridge",
      page_id: tab.id,
      profile_id: tab.profileId,
      url: wc.getURL?.() || tab.url,
      error: `Unsupported Electron Browser Bridge action: ${action}`,
    };
  }

  function isAuthorized(req) {
    const header = req.headers["x-lantai-browser-bridge-token"];
    return typeof header === "string" && header && header === bridgeToken;
  }

  function bridgeCapabilityMessage(expiresAtMs, nonce, bodyHash) {
    return ["v1", String(expiresAtMs), nonce, bodyHash].join("\n");
  }

  function timingSafeHexEqual(left, right) {
    if (typeof left !== "string" || typeof right !== "string") return false;
    if (!/^[a-f0-9]{64}$/i.test(left) || !/^[a-f0-9]{64}$/i.test(right)) {
      return false;
    }
    const leftBuffer = Buffer.from(left, "hex");
    const rightBuffer = Buffer.from(right, "hex");
    return (
      leftBuffer.length === rightBuffer.length &&
      crypto.timingSafeEqual(leftBuffer, rightBuffer)
    );
  }

  function rememberBridgeCapabilityNonce(nonce, expiresAtMs) {
    usedBridgeCapabilityNonces.set(nonce, expiresAtMs);
    const now = Date.now();
    for (const [knownNonce, knownExpiry] of usedBridgeCapabilityNonces) {
      if (
        knownExpiry <= now ||
        usedBridgeCapabilityNonces.size > MAX_BRIDGE_CAPABILITY_NONCES
      ) {
        usedBridgeCapabilityNonces.delete(knownNonce);
      }
    }
  }

  function isAuthorizedAction(req, rawBody) {
    const header = req.headers["x-lantai-browser-bridge-capability"];
    if (typeof header !== "string" || !header) return false;
    const parts = header.split(":");
    if (parts.length !== 5 || parts[0] !== "v1") return false;
    const [, expiresAtRaw, nonce, bodyHash, signature] = parts;
    if (!nonce || usedBridgeCapabilityNonces.has(nonce)) return false;
    const expiresAtMs = Number(expiresAtRaw);
    if (!Number.isFinite(expiresAtMs) || expiresAtMs <= Date.now())
      return false;
    const actualBodyHash = crypto
      .createHash("sha256")
      .update(rawBody)
      .digest("hex");
    if (!timingSafeHexEqual(bodyHash, actualBodyHash)) return false;
    const expectedSignature = crypto
      .createHmac("sha256", bridgeToken)
      .update(bridgeCapabilityMessage(expiresAtRaw, nonce, bodyHash))
      .digest("hex");
    if (!timingSafeHexEqual(signature, expectedSignature)) return false;
    rememberBridgeCapabilityNonce(nonce, expiresAtMs);
    return true;
  }

  function sendUnauthorized(res) {
    sendJson(res, 401, {
      ok: false,
      error: "Unauthorized browser bridge request.",
      error_code: "electron_bridge_unauthorized",
    });
  }

  async function handleHttp(req, res) {
    try {
      const url = new URL(req.url || "/", `http://${BRIDGE_HOST}`);
      if (req.method === "GET" && url.pathname === "/health") {
        sendJson(res, 200, { ok: true, bridge: "browser_workbench" });
        return;
      }
      if (req.method === "GET" && url.pathname === "/state") {
        if (!isAuthorized(req)) {
          sendUnauthorized(res);
          return;
        }
        sendJson(res, 200, getState());
        return;
      }
      if (req.method === "POST" && url.pathname === "/action") {
        const raw = await readRequestBody(req);
        if (!isAuthorizedAction(req, raw)) {
          sendUnauthorized(res);
          return;
        }
        const payload = raw ? JSON.parse(raw) : {};
        const result = await performAction(payload);
        sendJson(res, result.ok === false ? 400 : 200, result);
        return;
      }
      sendJson(res, 404, {
        ok: false,
        error: "Browser bridge endpoint not found.",
        error_code: "electron_bridge_endpoint_not_found",
      });
    } catch (err) {
      log(`HTTP bridge error: ${err.message}`);
      sendJson(res, 500, {
        ok: false,
        error: err.message,
        error_code: "electron_bridge_internal_error",
      });
    }
  }

  async function startBridgeServer() {
    if (bridgeServer && bridgeBaseUrl) return bridgeBaseUrl;
    bridgeServer = createServer((req, res) => {
      void handleHttp(req, res);
    });
    await new Promise((resolve, reject) => {
      bridgeServer.on("error", reject);
      bridgeServer.listen(0, BRIDGE_HOST, () => resolve());
    });
    const address = bridgeServer.address();
    const port = typeof address === "object" && address ? address.port : 0;
    bridgeBaseUrl = `http://${BRIDGE_HOST}:${port}`;
    log(`Bridge listening on ${bridgeBaseUrl}`);
    return bridgeBaseUrl;
  }

  function stopBridgeServer() {
    releaseAllSecurityScopedResources();
    if (bridgeServer) {
      bridgeServer.close();
      bridgeServer = null;
      bridgeBaseUrl = "";
    }
  }

  async function mountSidecar(payload = {}) {
    const mainWindow = getMainHostWindow();
    if (!mainWindow) {
      return {
        ok: false,
        error: "The Lantai main window is unavailable.",
        error_code: "browser_sidecar_host_unavailable",
      };
    }
    const profileId = sanitizeProfileId(payload.profile_id || "default");
    const runId = String(payload.run_id || "").trim();
    const bounds = normalizeSidecarBounds(payload.bounds);
    if (bounds.width < 1 || bounds.height < 1) {
      return {
        ok: false,
        error: "Browser sidecar bounds are not visible.",
        error_code: "browser_sidecar_bounds_invalid",
      };
    }
    const sameRun = sidecar.visible && sidecar.runId === runId;
    sidecar = {
      visible: true,
      bounds,
      runId,
      profileId,
      controlOwner: sameRun ? sidecar.controlOwner : "agent",
    };
    const popup = getWorkbenchWindow();
    if (popup) {
      popup.hide?.();
      workbenchVisible = false;
    }

    let tab = findTaskTab(profileId, runId);
    if (!tab && !runId) {
      const active = getActiveTab();
      if (active?.profileId === profileId) tab = active;
    }
    if (!tab) {
      const targetUrl = normalizeUrl(payload.url || "about:blank");
      const blocked = await browserUrlBlockedResult(
        targetUrl,
        "sidecar_mount",
        profileId,
      );
      if (blocked) {
        sidecar = { ...sidecar, visible: false };
        return blocked;
      }
      tab = createTab({
        profileId,
        url: targetUrl,
        activate: true,
        background: false,
        taskGroupId: runId,
      });
      const loadedBlocked = await waitForTabLoadAndPolicy(tab, "sidecar_mount");
      if (loadedBlocked) {
        sidecar = { ...sidecar, visible: false };
        return loadedBlocked;
      }
    }
    activeTabId = tab.id;
    tab.background = false;
    layoutActiveView();
    attachActiveView();
    return getState({ message: "Browser sidecar mounted." });
  }

  function layoutSidecar(bounds = {}) {
    if (!sidecar.visible) {
      return {
        ok: false,
        error: "Browser sidecar is not mounted.",
        error_code: "browser_sidecar_not_mounted",
      };
    }
    sidecar = { ...sidecar, bounds: normalizeSidecarBounds(bounds) };
    layoutActiveView();
    broadcastState();
    return getState();
  }

  function setSidecarControl(owner = "agent") {
    const controlOwner = String(owner || "")
      .trim()
      .toLowerCase();
    if (!sidecar.visible || !["agent", "user"].includes(controlOwner)) {
      return {
        ok: false,
        error: "Browser sidecar control owner is invalid.",
        error_code: "browser_sidecar_control_invalid",
      };
    }
    sidecar = { ...sidecar, controlOwner };
    agentStatus = controlOwner === "user" ? "用户接管" : "Agent 接管";
    currentAction =
      controlOwner === "user" ? "用户正在操作浏览器" : "浏览器已交还给 Agent";
    attachActiveView();
    return getState({
      message: `Browser control assigned to ${controlOwner}.`,
    });
  }

  async function captureSidecarPreview() {
    if (!sidecar.visible) {
      return {
        ok: false,
        error: "Browser sidecar is not mounted.",
        error_code: "browser_sidecar_not_mounted",
      };
    }
    if (sidecar.controlOwner === "user") {
      return {
        ok: false,
        error: "Preview capture is paused while the user controls the browser.",
        error_code: "browser_sidecar_user_control_active",
      };
    }
    const tab = getActiveTab();
    if (!tab) {
      return {
        ok: false,
        error: "Browser sidecar has no active tab.",
        error_code: "browser_sidecar_tab_unavailable",
      };
    }
    layoutActiveView();
    const image = await tab.view.webContents.capturePage();
    if (sidecar.controlOwner === "user") {
      return {
        ok: false,
        error: "Preview capture stopped when user control began.",
        error_code: "browser_sidecar_user_control_active",
      };
    }
    const requestedWidth = Math.min(
      1400,
      Math.max(320, sidecar.bounds?.width || 960),
    );
    const output =
      image.resize?.({ width: requestedWidth, quality: "good" }) || image;
    const dataUrl =
      output.toDataURL?.() ||
      `data:image/png;base64,${output.toPNG().toString("base64")}`;
    tab.url = tab.view.webContents.getURL?.() || tab.url;
    tab.title = tab.view.webContents.getTitle?.() || tab.title;
    return {
      ok: true,
      runtime: "electron_bridge",
      page_id: tab.id,
      url: tab.url,
      title: tab.title,
      data_url: dataUrl,
      captured_at: new Date().toISOString(),
    };
  }

  async function performRendererAction(payload = {}) {
    const action = String(payload.action || "")
      .trim()
      .toLowerCase();
    if (
      sidecar.visible &&
      sidecar.controlOwner !== "user" &&
      SIDECAR_RENDERER_CONTROL_ACTIONS.has(action)
    ) {
      return {
        ok: false,
        action,
        error: "Take control before changing the live browser page.",
        error_code: "browser_sidecar_takeover_required",
      };
    }
    return performAction(payload);
  }

  function unmountSidecar() {
    const mainWindow = getMainHostWindow();
    for (const tab of tabs.values()) {
      try {
        mainWindow?.removeBrowserView?.(tab.view);
      } catch {
        // The view may already be detached in preview mode.
      }
    }
    sidecar = {
      ...sidecar,
      visible: false,
      bounds: null,
      controlOwner: "agent",
    };
    agentStatus = "Agent 接管";
    currentAction = "";
    broadcastState();
    return getState({
      message: "Browser sidecar hidden; task tabs remain active.",
    });
  }

  function popoutSidecar() {
    if (sidecar.visible && sidecar.controlOwner !== "user") {
      return getState({
        ok: false,
        error:
          "Take control before opening the live browser in a separate window.",
        error_code: "browser_sidecar_takeover_required",
      });
    }
    const previousRunId = sidecar.runId;
    unmountSidecar();
    const win = ensureWorkbenchWindow({ focus: true });
    workbenchVisible = true;
    win.show?.();
    win.focus?.();
    attachActiveView();
    return getState({
      message: "Browser opened in a separate window.",
      previous_sidecar_run_id: previousRunId,
    });
  }

  async function focusExternalBrowser(browserName = "") {
    const normalized = String(browserName || "chrome")
      .trim()
      .toLowerCase();
    const names = {
      chrome: "Google Chrome",
      "google chrome": "Google Chrome",
      chromium: "Chromium",
      edge: "Microsoft Edge",
      "microsoft edge": "Microsoft Edge",
    };
    const applicationName = names[normalized];
    if (!applicationName) {
      return {
        ok: false,
        error: "Unsupported BrowserSkill browser application.",
        error_code: "browser_sidecar_external_browser_unsupported",
      };
    }
    if (platform !== "darwin") {
      return {
        ok: false,
        error:
          "Switching to the BrowserSkill application is currently available on macOS.",
        error_code: "browser_sidecar_external_focus_unsupported",
      };
    }
    try {
      const child = spawnProcess("/usr/bin/open", ["-a", applicationName], {
        detached: false,
        stdio: ["ignore", "ignore", "pipe"],
      });
      const result = await new Promise((resolve) => {
        let stderr = "";
        const timeout = setTimeout(() => {
          child.kill?.();
          resolve({ ok: false, error: "Browser application focus timed out." });
        }, 3000);
        child.stderr?.on?.("data", (chunk) => {
          stderr = `${stderr}${String(chunk || "")}`.slice(-1000);
        });
        child.once?.("error", (error) => {
          clearTimeout(timeout);
          resolve({ ok: false, error: error.message });
        });
        child.once?.("close", (code) => {
          clearTimeout(timeout);
          resolve({
            ok: code === 0,
            error:
              code === 0
                ? ""
                : stderr.trim() ||
                  `Browser application exited with code ${code}.`,
          });
        });
      });
      if (!result.ok) {
        return {
          ...result,
          error_code: "browser_sidecar_external_focus_failed",
        };
      }
      return { ok: true, browser_name: applicationName };
    } catch (err) {
      return {
        ok: false,
        error: err.message,
        error_code: "browser_sidecar_external_focus_failed",
      };
    }
  }

  function installIpcHandlers() {
    function rejectUntrustedIpc(event) {
      if (isTrustedIpcEvent(event)) {
        return null;
      }
      return { ok: false, error: "untrusted_ipc_sender" };
    }

    ipcMain.handle(
      "lantai:browser-workbench-open",
      async (event, payload = {}) => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        const blocked = await browserUrlBlockedResult(
          payload.url || "about:blank",
          "open_workbench",
          payload.profile_id || "default",
        );
        if (blocked) return blocked;
        ensureWorkbenchWindow();
        if (!getActiveTab() || payload.url) {
          const tab = createTab({
            profileId: payload.profile_id || "default",
            url: payload.url || "about:blank",
          });
          const loadedBlocked = await waitForTabLoadAndPolicy(
            tab,
            "open_workbench",
          );
          if (loadedBlocked) return loadedBlocked;
        }
        return getState({ message: "Browser Workbench opened." });
      },
    );
    ipcMain.handle(
      "lantai:browser-workbench-action",
      async (event, payload = {}) => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        return performRendererAction(payload);
      },
    );
    ipcMain.handle(
      "lantai:browser-workbench-select-file-grant",
      async (event, payload = {}) => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        return selectFileGrant(payload);
      },
    );
    ipcMain.handle("lantai:browser-workbench-state", async (event) => {
      const rejected = rejectUntrustedIpc(event);
      if (rejected) return rejected;
      return getState();
    });
    ipcMain.handle(
      "lantai:browser-workbench-sidecar-mount",
      async (event, payload = {}) => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        return mountSidecar(payload);
      },
    );
    ipcMain.handle(
      "lantai:browser-workbench-sidecar-layout",
      async (event, bounds = {}) => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        return layoutSidecar(bounds);
      },
    );
    ipcMain.handle(
      "lantai:browser-workbench-sidecar-control",
      async (event, owner = "agent") => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        return setSidecarControl(owner);
      },
    );
    ipcMain.handle(
      "lantai:browser-workbench-sidecar-preview",
      async (event) => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        return captureSidecarPreview();
      },
    );
    ipcMain.handle(
      "lantai:browser-workbench-sidecar-unmount",
      async (event) => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        return unmountSidecar();
      },
    );
    ipcMain.handle("lantai:browser-workbench-sidecar-popout", async (event) => {
      const rejected = rejectUntrustedIpc(event);
      if (rejected) return rejected;
      return popoutSidecar();
    });
    ipcMain.handle(
      "lantai:browser-workbench-sidecar-focus-external",
      async (event, browserName = "") => {
        const rejected = rejectUntrustedIpc(event);
        if (rejected) return rejected;
        return focusExternalBrowser(browserName);
      },
    );
  }

  return {
    getBridgeBaseUrl: () => bridgeBaseUrl,
    getBridgeToken: () => bridgeToken,
    getProfilePartition,
    getState,
    captureSidecarPreview,
    focusExternalBrowser,
    installIpcHandlers,
    layoutSidecar,
    mountSidecar,
    openWorkbench: (payload = {}) =>
      performAction({ action: "open_workbench", ...payload }),
    popoutSidecar,
    performAction,
    selectFileGrant,
    startBridgeServer,
    stopBridgeServer,
    setSidecarControl,
    unmountSidecar,
  };
}

module.exports = {
  BRIDGE_HOST,
  buildBrowserBridgeCapabilityMatrix,
  createBrowserWorkbenchController,
  getProfilePartition,
  sanitizeProfileId,
};
