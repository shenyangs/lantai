import { useEffect, useRef } from "react";
import { useTheme } from "../contexts/ThemeContext";
import { resolveInterfaceMaterial } from "./appearance";

/** Keep the renderer transparent only after the native material is ready. */
export function useWindowMaterial() {
  const { displaySettings, isDark } = useTheme();
  const glass =
    resolveInterfaceMaterial(displaySettings.sidebarMaterial) !== "solid";
  const currentMode = useRef<WindowMaterialMode>("solid");
  useEffect(() => {
    const root = document.documentElement;
    const api = window.electronAPI;
    const reduced = window.matchMedia("(prefers-reduced-transparency: reduce)");
    const contrast = window.matchMedia("(forced-colors: active)");
    let alive = true;
    let revision = 0;
    const applyMode = (mode: WindowMaterialMode) => {
      if (alive) {
        currentMode.current = mode;
        root.dataset.windowMaterial = mode;
      }
    };
    const sync = () => {
      const ticket = ++revision;
      const enabled = glass && !reduced.matches && !contrast.matches;
      const nativeReady =
        currentMode.current === "native" || currentMode.current === "vibrancy";
      if (!enabled || !nativeReady) applyMode(enabled ? "fallback" : "solid");
      if (api?.setWindowMaterial) {
        void api
          .setWindowMaterial({ enabled, dark: isDark })
          .then((mode) => {
            if (ticket === revision) {
              applyMode(mode);
            }
          })
          .catch(() => {
            if (ticket === revision) applyMode(enabled ? "fallback" : "solid");
          });
      }
    };
    const unsubscribe = api?.onWindowMaterialChanged?.(applyMode);
    reduced.addEventListener("change", sync);
    contrast.addEventListener("change", sync);
    sync();
    return () => {
      alive = false;
      unsubscribe?.();
      reduced.removeEventListener("change", sync);
      contrast.removeEventListener("change", sync);
    };
  }, [glass, isDark]);
}
