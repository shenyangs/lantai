/**
 * hostExternals.ts
 *
 * Exposes shared host dependencies and a reactive plugin registry on
 * `window.Lantai` so plugin bundles can register routes and tool renderers
 * without bundling their own copies of React / antd.
 *
 * Call `installHostExternals()` once at application startup (main.tsx).
 */

import React from "react";
import ReactDOM from "react-dom";
import * as antd from "antd";
import * as antdIcons from "@ant-design/icons";
import { getApiBaseUrl, getApiUrl } from "../api/config";
import { buildAuthHeaders } from "../api/authHeaders";

// ─────────────────────────────────────────────────────────────────────────────
// Public types
// ─────────────────────────────────────────────────────────────────────────────

/** Shared host dependencies exposed to plugin bundles via `window.Lantai.host`. */
export interface HostExternals {
  React: typeof React;
  ReactDOM: typeof ReactDOM;
  antd: typeof antd;
  antdIcons: typeof antdIcons;
  apiBaseUrl: string;
}

export type PluginPermission =
  | "runtime:provider"
  | "runtime:lifecycle"
  | "runtime:command"
  | "runtime:helpers"
  | "config:read"
  | "config:write"
  | "ui:route"
  | "ui:tool-renderer"
  | "api:read"
  | "api:write";

export interface ExtensionHost {
  pluginId: string;
  permissions: readonly PluginPermission[];
  registerRoutes: (routes: PluginRouteDeclaration[]) => void;
  registerToolRender: (renderers: Record<string, React.FC<any>>) => void;
  fetchApi: (path: string, init?: RequestInit) => Promise<Response>;
}

export interface PluginRouteDeclaration {
  /** Full URL path, e.g. "/plugin/my-plugin/dashboard". */
  path: string;
  component: React.ComponentType;
  /** Sidebar display label. */
  label: string;
  /** Emoji or short icon text. */
  icon?: string;
  /** Lower number = appears earlier in sidebar. Defaults to 0. */
  priority?: number;
}

/** Internal per-plugin registration record. */
export interface PluginRegistration {
  pluginId: string;
  routes: PluginRouteDeclaration[];
  toolRenderers: Record<string, React.FC<any>>;
}

// ─────────────────────────────────────────────────────────────────────────────
// PluginSystem — reactive singleton
// ─────────────────────────────────────────────────────────────────────────────

class PluginSystem {
  private records = new Map<string, PluginRegistration>();
  private listeners = new Set<() => void>();

  // ── Write API ───────────────────────────────────────────────────────────

  addRoutes(pluginId: string, routes: PluginRouteDeclaration[]): void {
    const rec = this._record(pluginId);
    rec.routes.push(...routes);
    this._notify();
  }

  addToolRenderers(
    pluginId: string,
    renderers: Record<string, React.FC<any>>,
  ): void {
    const rec = this._record(pluginId);
    Object.assign(rec.toolRenderers, renderers);
    this._notify();
  }

  remove(pluginId: string): void {
    if (this.records.delete(pluginId)) this._notify();
  }

  clear(): void {
    if (this.records.size === 0) return;
    this.records.clear();
    this._notify();
  }

  // ── Read API (consumed by PluginContext / usePlugins) ────────────────────

  /** Merged map of all tool renderers across all plugins. */
  getToolRenderConfig(): Record<string, React.FC<any>> {
    const out: Record<string, React.FC<any>> = {};
    for (const rec of this.records.values())
      Object.assign(out, rec.toolRenderers);
    return out;
  }

  /** Flat list of all page routes across all plugins, sorted by priority. */
  getRoutes(): PluginRouteDeclaration[] {
    const out: PluginRouteDeclaration[] = [];
    for (const rec of this.records.values()) out.push(...rec.routes);
    return out.sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0));
  }

  // ── Subscription ─────────────────────────────────────────────────────────

  /** Subscribe to any registration change. Returns an unsubscribe function. */
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  // ── Internals ────────────────────────────────────────────────────────────

  private _record(pluginId: string): PluginRegistration {
    if (!this.records.has(pluginId)) {
      this.records.set(pluginId, { pluginId, routes: [], toolRenderers: {} });
    }
    return this.records.get(pluginId)!;
  }

  private _notify(): void {
    this.listeners.forEach((fn) => fn());
  }
}

/** Global singleton — imported by PluginContext to subscribe to changes. */
export const pluginSystem = new PluginSystem();

const ALLOWED_EXTENSION_API_PREFIXES = [
  "/capabilities",
  "/task-runs",
  "/runtime/runs",
  "/work-methods",
  "/time",
  "/reminders",
  "/office-documents",
] as const;

let activeRegistration:
  | { pluginId: string; permissions: Set<PluginPermission> }
  | null = null;

function requirePermission(
  pluginId: string,
  permissions: Set<PluginPermission>,
  permission: PluginPermission,
): void {
  if (!permissions.has(permission)) {
    throw new Error(
      `Extension '${pluginId}' requires permission '${permission}'`,
    );
  }
}

function extensionApiPath(path: string): string {
  const normalized = String(path || "").trim();
  if (
    !normalized.startsWith("/") ||
    normalized.startsWith("//") ||
    normalized.includes("..") ||
    !ALLOWED_EXTENSION_API_PREFIXES.some(
      (prefix) =>
        normalized === prefix || normalized.startsWith(`${prefix}/`),
    )
  ) {
    throw new Error(`Extension API path is not allowed: ${normalized}`);
  }
  return normalized;
}

function createExtensionHost(
  pluginId: string,
  permissions: Set<PluginPermission>,
): ExtensionHost {
  return Object.freeze({
    pluginId,
    permissions: Object.freeze([...permissions]),
    registerRoutes(routes: PluginRouteDeclaration[]) {
      requirePermission(pluginId, permissions, "ui:route");
      pluginSystem.addRoutes(pluginId, routes);
    },
    registerToolRender(renderers: Record<string, React.FC<any>>) {
      requirePermission(pluginId, permissions, "ui:tool-renderer");
      pluginSystem.addToolRenderers(pluginId, renderers);
    },
    async fetchApi(path: string, init: RequestInit = {}) {
      const method = String(init.method || "GET").toUpperCase();
      requirePermission(
        pluginId,
        permissions,
        ["GET", "HEAD", "OPTIONS"].includes(method) ? "api:read" : "api:write",
      );
      const headers = new Headers(init.headers);
      for (const [key, value] of Object.entries(buildAuthHeaders())) {
        if (!headers.has(key)) headers.set(key, value);
      }
      return fetch(getApiUrl(extensionApiPath(path)), {
        ...init,
        method,
        headers,
        credentials: init.credentials ?? "same-origin",
      });
    },
  });
}

export function beginPluginRegistration(
  pluginId: string,
  declaredPermissions: string[],
): void {
  const permissions = new Set(
    declaredPermissions as PluginPermission[],
  );
  activeRegistration = { pluginId, permissions };
  pluginSystem.remove(pluginId);
  window.Lantai.extension = createExtensionHost(pluginId, permissions);
}

export function endPluginRegistration(pluginId: string): void {
  if (activeRegistration?.pluginId === pluginId) {
    activeRegistration = null;
    delete window.Lantai.extension;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Global declarations
// ─────────────────────────────────────────────────────────────────────────────

/** Namespace object. */
export interface WindowNamespace {
  /** Shared host dependencies (React, antd, API helpers). */
  host: HostExternals;
  /** Bound only while one trusted extension module is registering. */
  extension?: ExtensionHost;
  /** Register page routes for a plugin. */
  registerRoutes?: (pluginId: string, routes: PluginRouteDeclaration[]) => void;
  /** Register tool-call renderers for a plugin. */
  registerToolRender?: (
    pluginId: string,
    renderers: Record<string, React.FC<any>>,
  ) => void;
}

declare global {
  interface Window {
    Lantai: WindowNamespace;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Install (call once in main.tsx)
// ─────────────────────────────────────────────────────────────────────────────

export function installHostExternals(): void {
  const apiBaseUrl = getApiBaseUrl();

  if (!window.Lantai) {
    (window as any).Lantai = {} as WindowNamespace;
  }

  delete (window.Lantai as unknown as Record<string, unknown>).modules;
  window.Lantai.host = Object.freeze({
    React,
    ReactDOM,
    antd,
    antdIcons,
    apiBaseUrl,
  });

  if (!window.Lantai.registerRoutes) {
    window.Lantai.registerRoutes = (pluginId, routes) => {
      if (activeRegistration?.pluginId !== pluginId) {
        throw new Error(`Extension '${pluginId}' has no active registration`);
      }
      requirePermission(
        pluginId,
        activeRegistration.permissions,
        "ui:route",
      );
      pluginSystem.addRoutes(pluginId, routes);
      console.info(
        `[plugin:${pluginId}] registerRoutes → ${routes.length} route(s)`,
      );
    };
  }

  if (!window.Lantai.registerToolRender) {
    window.Lantai.registerToolRender = (pluginId, renderers) => {
      if (activeRegistration?.pluginId !== pluginId) {
        throw new Error(`Extension '${pluginId}' has no active registration`);
      }
      requirePermission(
        pluginId,
        activeRegistration.permissions,
        "ui:tool-renderer",
      );
      pluginSystem.addToolRenderers(pluginId, renderers);
      console.info(
        `[plugin:${pluginId}] registerToolRender → ${Object.keys(
          renderers,
        ).join(", ")}`,
      );
    };
  }
}
