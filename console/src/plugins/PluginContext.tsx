/**
 * PluginContext.tsx
 *
 * Reactive plugin context for the host application.
 * Subscribes to the PluginSystem singleton and exposes plugin-registered
 * routes and tool renderers to any component via usePlugins().
 *
 *  const { toolRenderConfig, pluginRoutes, loading, error } = usePlugins();
 */

import React, { createContext, useContext, useEffect, useState } from "react";
import { pluginSystem } from "./hostExternals";
import { loadAllPlugins } from "./usePluginLoader";
import type { PluginRouteDeclaration } from "./hostExternals";
import { customizationApi } from "../api/modules/customization";

// ─────────────────────────────────────────────────────────────────────────────
// Context shape
// ─────────────────────────────────────────────────────────────────────────────

export interface PluginContextValue {
  /** Map of tool-name → React component. Pass to `@/vendor/lantaiChat`. */
  toolRenderConfig: Record<string, React.FC<any>>;
  /** Page routes registered by plugins. Inject into the router + sidebar. */
  pluginRoutes: PluginRouteDeclaration[];
  /** True until the initial plugin-load attempt completes. */
  loading: boolean;
  /** Non-null if one or more plugins failed to load. */
  error: string | null;
}

const PluginContext = createContext<PluginContextValue>({
  toolRenderConfig: {},
  pluginRoutes: [],
  loading: true,
  error: null,
});

// ─────────────────────────────────────────────────────────────────────────────
// Provider
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Wrap your application root with `<PluginProvider>` once.
 * All descendants can then call `usePlugins()` to access plugin-registered
 * routes and tool renderers.
 */
export function PluginProvider({ children }: { children: React.ReactNode }) {
  const [toolRenderConfig, setToolRenderConfig] = useState<
    Record<string, React.FC<any>>
  >(pluginSystem.getToolRenderConfig());
  const [pluginRoutes, setPluginRoutes] = useState<PluginRouteDeclaration[]>(
    pluginSystem.getRoutes(),
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    // Re-sync state whenever any plugin registers new capabilities
    const unsub = pluginSystem.subscribe(() => {
      setToolRenderConfig(pluginSystem.getToolRenderConfig());
      setPluginRoutes(pluginSystem.getRoutes());
    });

    // Safe recovery must decide before any third-party renderer code executes.
    customizationApi
      .getState()
      .catch(() => null)
      .then((customizationState) => {
        if (customizationState?.mode === "safe") {
          pluginSystem.clear();
          return { failed: [] as string[] };
        }
        return loadAllPlugins();
      })
      .then(({ failed }) => {
        if (!disposed && failed.length > 0) setError(failed.join("; "));
      })
      .catch((loadError: unknown) => {
        // Plugin discovery is optional.  An unexpected manifest or renderer
        // error must never leave the entire application behind a permanent
        // loading screen (most visible when two windows start together).
        console.error("[PluginLoader] unexpected initialisation failure:", loadError);
        if (!disposed) {
          setError(loadError instanceof Error ? loadError.message : String(loadError));
        }
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });

    return () => {
      disposed = true;
      unsub();
    };
  }, []);

  return (
    <PluginContext.Provider
      value={{ toolRenderConfig, pluginRoutes, loading, error }}
    >
      {children}
    </PluginContext.Provider>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Consumer hook
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Consume the global plugin context.
 *
 * ```tsx
 * const { toolRenderConfig, pluginRoutes, loading } = usePlugins();
 * ```
 */
export function usePlugins(): PluginContextValue {
  return useContext(PluginContext);
}
