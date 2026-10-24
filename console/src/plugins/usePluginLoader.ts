/**
 * usePluginLoader.ts — plugin loading utility
 *
 * Fetches the plugin list, downloads each frontend bundle, and executes it
 * via a same-origin Blob URL so plugins can self-register into the
 * `pluginSystem` singleton (hostExternals.ts). Registration is deliberately
 * sequential because the host binds one active plugin identity at a time.
 *
 * Exports `loadAllPlugins()` — the single function PluginContext calls.
 */

import { getApiUrl, getApiToken } from "../api/config";
import {
  beginPluginRegistration,
  endPluginRegistration,
  pluginSystem,
} from "./hostExternals";

// ─────────────────────────────────────────────────────────────────────────────
// Plugin manifest type (mirrors backend PluginInfo)
// ─────────────────────────────────────────────────────────────────────────────

interface PluginInfo {
  id: string;
  name: string;
  frontend_entry?: string;
  enabled?: boolean;
  permissions?: string[];
}

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs = 2500,
): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    return await fetch(input, {
      ...init,
      signal: init.signal ?? controller.signal,
    });
  } finally {
    window.clearTimeout(timer);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Internal helpers
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Resolve a backend-relative API path (e.g. `/plugins/…/files/index.js`)
 * to a full URL using the same base that all other API calls use.
 */
function resolveUrl(pluginId: string, apiPath: string): string {
  return getApiUrl(`plugins/${pluginId}/files/${apiPath}`);
}

/**
 * Fetch a plugin's JS source, wrap it in a same-origin Blob URL, and
 * execute it via dynamic import.  Blob URL is revoked immediately after.
 */
async function executePluginScript(
  plugin: PluginInfo,
  entryUrl: string,
): Promise<void> {
  const token = getApiToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetchWithTimeout(entryUrl, { headers }, 5000);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} for ${entryUrl}`);
  }

  const jsText = await response.text();
  const blobUrl = URL.createObjectURL(
    new Blob([jsText], { type: "application/javascript" }),
  );
  beginPluginRegistration(plugin.id, plugin.permissions || []);
  try {
    await import(/* @vite-ignore */ blobUrl);
  } catch (error) {
    pluginSystem.remove(plugin.id);
    throw error;
  } finally {
    endPluginRegistration(plugin.id);
    URL.revokeObjectURL(blobUrl);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Public API
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Fetch the plugin list from `GET /api/plugins`, then load every plugin that
 * has a `frontend_entry` in a deterministic sequence. Failures are isolated
 * per plugin so one bad plugin never blocks the others.
 *
 * Returns a summary `{ loaded, failed }` for the caller to surface as an error.
 */
export async function loadAllPlugins(): Promise<{
  loaded: number;
  failed: string[];
}> {
  const failed: string[] = [];

  let plugins: PluginInfo[];
  try {
    const token = getApiToken();
    const headers: Record<string, string> = {};
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetchWithTimeout(getApiUrl("/plugins"), { headers });
    if (!res.ok) {
      console.warn(`[PluginLoader] /api/plugins returned ${res.status}`);
      return { loaded: 0, failed: [] };
    }
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      if (import.meta.env.DEV) {
        console.info(
          "[PluginLoader] plugin API is not available in dev server mode; skipping frontend plugin loading.",
        );
      }
      return { loaded: 0, failed: [] };
    }
    const payload = await res.json();
    plugins = Array.isArray(payload) ? payload : [];
  } catch (err) {
    console.warn("[PluginLoader] failed to fetch plugin list:", err);
    return { loaded: 0, failed: [] };
  }

  const frontendPlugins = plugins.filter(
    (p) =>
      p.enabled !== false &&
      p.frontend_entry &&
      (p.permissions || []).some((permission) =>
        ["ui:route", "ui:tool-renderer"].includes(permission),
      ),
  );

  let loaded = 0;
  for (const plugin of frontendPlugins) {
    try {
      await executePluginScript(
        plugin,
        resolveUrl(plugin.id, plugin.frontend_entry!),
      );
      loaded += 1;
      console.info(`[PluginLoader] ✓ ${plugin.id}`);
    } catch (error) {
      const msg = `${plugin.id}: ${error}`;
      console.error(`[PluginLoader] ✗ ${msg}`);
      failed.push(msg);
    }
  }

  console.info(
    `[PluginLoader] ${loaded}/${frontendPlugins.length} plugin(s) loaded`,
  );
  return { loaded, failed };
}
