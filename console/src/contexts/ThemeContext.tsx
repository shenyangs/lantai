import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  useRef,
  type ReactNode,
} from "react";
import {
  DEFAULT_APPEARANCE_PALETTE,
  defaultDisplaySettings,
  type AppearanceDisplaySettings,
  type AppearancePaletteId,
  type SidebarMaterial,
  resolveInterfaceMaterial,
} from "../theme/appearance";
import { settingsApi } from "../api/modules/settings";
import {
  CUSTOMIZATION_CHANGED_EVENT,
  customizationApi,
  notifyCustomizationChanged,
  type CustomizationEffectiveState,
} from "../api/modules/customization";

export type ThemeMode = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";
export type DensityMode = "compact" | "comfortable" | "spacious";

const STORAGE_KEY = "lantai-theme";
const DENSITY_STORAGE_KEY = "lantai-density";
const PALETTE_STORAGE_KEY = "lantai-appearance-palette";
const DISPLAY_STORAGE_KEY = "lantai-appearance-display";
const CUSTOM_CSS_STORAGE_KEY = "lantai-custom-theme-css";
const CUSTOM_STYLE_ID = "lantai-custom-theme-style";

interface ThemeContextValue {
  /** User selected preference: light / dark / system */
  themeMode: ThemeMode;
  /** Resolved final theme after applying system preference */
  isDark: boolean;
  densityMode: DensityMode;
  appearancePalette: AppearancePaletteId;
  displaySettings: AppearanceDisplaySettings;
  customThemeCss: string;
  setThemeMode: (mode: ThemeMode) => void;
  setDensityMode: (mode: DensityMode) => void;
  setAppearancePalette: (palette: AppearancePaletteId) => void;
  setDisplaySetting: <K extends keyof AppearanceDisplaySettings>(
    key: K,
    value: AppearanceDisplaySettings[K],
  ) => void;
  setDisplaySettings: (settings: AppearanceDisplaySettings) => void;
  setCustomThemeCss: (css: string) => void;
  /** Convenience toggle: light ↔ dark (skips system) */
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  themeMode: "light",
  isDark: false,
  densityMode: "comfortable",
  appearancePalette: DEFAULT_APPEARANCE_PALETTE,
  displaySettings: defaultDisplaySettings,
  customThemeCss: "",
  setThemeMode: () => {},
  setDensityMode: () => {},
  setAppearancePalette: () => {},
  setDisplaySetting: () => {},
  setDisplaySettings: () => {},
  setCustomThemeCss: () => {},
  toggleTheme: () => {},
});

function getInitialMode(): ThemeMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") {
      return stored;
    }
  } catch {
    // ignore storage errors
  }
  return "system";
}

function getInitialDensity(): DensityMode {
  try {
    const stored = localStorage.getItem(DENSITY_STORAGE_KEY);
    if (
      stored === "compact" ||
      stored === "comfortable" ||
      stored === "spacious"
    ) {
      return stored;
    }
  } catch {
    // ignore storage errors
  }
  return "comfortable";
}

function getInitialPalette(): AppearancePaletteId {
  try {
    const stored = localStorage.getItem(PALETTE_STORAGE_KEY);
    if (
      stored === "lantai" ||
      stored === "mist" ||
      stored === "pine" ||
      stored === "blue" ||
      stored === "warm" ||
      stored === "violet" ||
      stored === "ink" ||
      stored === "custom"
    ) {
      return stored;
    }
  } catch {
    // ignore storage errors
  }
  return DEFAULT_APPEARANCE_PALETTE;
}

function isThemeMode(value: unknown): value is ThemeMode {
  return value === "light" || value === "dark" || value === "system";
}

function isDensityMode(value: unknown): value is DensityMode {
  return value === "compact" || value === "comfortable" || value === "spacious";
}

function isSidebarMaterial(value: unknown): value is SidebarMaterial {
  return (
    value === "solid" ||
    value === "light-glass" ||
    value === "mist-glass" ||
    value === "deep-glass"
  );
}

function isAppearancePalette(value: unknown): value is AppearancePaletteId {
  return (
    value === "lantai" ||
    value === "mist" ||
    value === "pine" ||
    value === "blue" ||
    value === "warm" ||
    value === "violet" ||
    value === "ink" ||
    value === "custom"
  );
}

function normalizeDisplaySettings(
  parsed?: Partial<AppearanceDisplaySettings> | null,
): AppearanceDisplaySettings {
  return {
    ...defaultDisplaySettings,
    ...(parsed || {}),
    personaMode:
      parsed?.personaMode === "full" ||
      parsed?.personaMode === "balanced" ||
      parsed?.personaMode === "quiet" ||
      parsed?.personaMode === "off"
        ? parsed.personaMode
        : defaultDisplaySettings.personaMode,
    dynamicWelcome:
      typeof parsed?.dynamicWelcome === "boolean"
        ? parsed.dynamicWelcome
        : defaultDisplaySettings.dynamicWelcome,
    aiWelcome:
      typeof parsed?.aiWelcome === "boolean"
        ? parsed.aiWelcome
        : defaultDisplaySettings.aiWelcome,
    welcomeCulturalCalendar:
      parsed?.welcomeCulturalCalendar === "none" ||
      parsed?.welcomeCulturalCalendar === "china" ||
      parsed?.welcomeCulturalCalendar === "global" ||
      parsed?.welcomeCulturalCalendar === "all"
        ? parsed.welcomeCulturalCalendar
        : defaultDisplaySettings.welcomeCulturalCalendar,
    welcomeTone:
      parsed?.welcomeTone === "natural" ||
      parsed?.welcomeTone === "concise" ||
      parsed?.welcomeTone === "restrained"
        ? parsed.welcomeTone
        : defaultDisplaySettings.welcomeTone,
    composerSize:
      parsed?.composerSize === "compact" ||
      parsed?.composerSize === "comfortable" ||
      parsed?.composerSize === "spacious"
        ? parsed.composerSize
        : defaultDisplaySettings.composerSize,
    sidebarMaterial: isSidebarMaterial(parsed?.sidebarMaterial)
      ? parsed.sidebarMaterial
      : defaultDisplaySettings.sidebarMaterial,
  };
}

function getInitialDisplaySettings(): AppearanceDisplaySettings {
  try {
    const raw = localStorage.getItem(DISPLAY_STORAGE_KEY);
    if (!raw) return defaultDisplaySettings;
    const parsed = JSON.parse(raw) as Partial<AppearanceDisplaySettings>;
    return normalizeDisplaySettings(parsed);
  } catch {
    return defaultDisplaySettings;
  }
}

function getInitialCustomCss(): string {
  try {
    return localStorage.getItem(CUSTOM_CSS_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

const SAFE_THEME_TOKEN = /(--lt-[a-z0-9-]+)\s*:\s*([^;{}]+)\s*;/gi;

/** Keep legacy input compatible while refusing selectors, imports and URLs. */
export function sanitizeCustomThemeCss(css: string): string {
  const tokens: string[] = [];
  for (const match of css.matchAll(SAFE_THEME_TOKEN)) {
    const name = match[1].toLowerCase();
    const value = match[2].trim();
    const lowered = value.toLowerCase();
    if (!value || value.length > 256 || lowered.includes("url(")) continue;
    tokens.push(`  ${name}: ${value};`);
  }
  return tokens.length ? `:root {\n${tokens.join("\n")}\n}` : "";
}

function cssFromDesignTokens(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const declarations = Object.entries(value as Record<string, unknown>)
    .filter(
      ([name, token]) =>
        /^--lt-[a-z0-9-]+$/.test(name) && typeof token === "string",
    )
    .map(([name, token]) => `  ${name}: ${String(token).trim()};`);
  return sanitizeCustomThemeCss(`:root {\n${declarations.join("\n")}\n}`);
}

function resolveIsDark(mode: ThemeMode): boolean {
  if (mode === "dark") return true;
  if (mode === "light") return false;
  // system
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [themeMode, setThemeModeState] = useState<ThemeMode>(getInitialMode);
  const [densityMode, setDensityModeState] =
    useState<DensityMode>(getInitialDensity);
  const [appearancePalette, setAppearancePaletteState] =
    useState<AppearancePaletteId>(getInitialPalette);
  const [displaySettingsState, setDisplaySettingsState] =
    useState<AppearanceDisplaySettings>(getInitialDisplaySettings);
  const [customThemeCss, setCustomThemeCssState] =
    useState<string>(getInitialCustomCss);
  const [isDark, setIsDark] = useState<boolean>(() =>
    resolveIsDark(getInitialMode()),
  );
  const themeRevision = useRef(0);
  const themeSaving = useRef(false);
  const effectiveThemeLoaded = useRef(false);
  const themeWrites = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    let cancelled = false;
    const revision = themeRevision.current;
    settingsApi
      .getAppearanceSettings()
      .then((settings) => {
        if (cancelled) return;

        if (
          revision === themeRevision.current &&
          !effectiveThemeLoaded.current &&
          isThemeMode(settings.theme_mode)
        ) {
          setThemeModeState(settings.theme_mode);
          setIsDark(resolveIsDark(settings.theme_mode));
          try {
            localStorage.setItem(STORAGE_KEY, settings.theme_mode);
          } catch {
            // ignore
          }
        }

        if (isAppearancePalette(settings.appearance_palette)) {
          setAppearancePaletteState(settings.appearance_palette);
          try {
            localStorage.setItem(
              PALETTE_STORAGE_KEY,
              settings.appearance_palette,
            );
          } catch {
            // ignore
          }
        }

        let nextDensity: DensityMode | null = isDensityMode(
          settings.density_mode,
        )
          ? settings.density_mode
          : null;
        if (
          settings.display_settings &&
          typeof settings.display_settings === "object"
        ) {
          const normalized = normalizeDisplaySettings(
            settings.display_settings,
          );
          setDisplaySettingsState(normalized);
          nextDensity = nextDensity || normalized.composerSize;
          try {
            localStorage.setItem(
              DISPLAY_STORAGE_KEY,
              JSON.stringify(normalized),
            );
          } catch {
            // ignore
          }
        }
        if (nextDensity) {
          setDensityModeState(nextDensity);
          try {
            localStorage.setItem(DENSITY_STORAGE_KEY, nextDensity);
          } catch {
            // ignore
          }
        }

        if (typeof settings.custom_theme_css === "string") {
          setCustomThemeCssState(settings.custom_theme_css);
          try {
            if (settings.custom_theme_css.trim()) {
              localStorage.setItem(
                CUSTOM_CSS_STORAGE_KEY,
                settings.custom_theme_css,
              );
            } else {
              localStorage.removeItem(CUSTOM_CSS_STORAGE_KEY);
            }
          } catch {
            // ignore
          }
        }
        window.dispatchEvent(new Event(CUSTOMIZATION_CHANGED_EVENT));
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let request = 0;
    const applyEffectiveState = (
      effective: CustomizationEffectiveState,
      revision: number,
    ) => {
      if (cancelled) return;
      const values = effective.values;
      const nextPalette = values["appearance.palette"];
      if (isAppearancePalette(nextPalette)) {
        setAppearancePaletteState(nextPalette);
      }
      const nextTheme = values["appearance.theme_mode"];
      if (
        revision === themeRevision.current &&
        !themeSaving.current &&
        isThemeMode(nextTheme)
      ) {
        effectiveThemeLoaded.current = true;
        setThemeModeState(nextTheme);
        setIsDark(resolveIsDark(nextTheme));
        try {
          localStorage.setItem(STORAGE_KEY, nextTheme);
        } catch {
          // ignore storage errors
        }
      }
      setDisplaySettingsState((current) =>
        normalizeDisplaySettings({
          ...current,
          showInnerVoice:
            typeof values["conversation.show_inner_voice"] === "boolean"
              ? values["conversation.show_inner_voice"]
              : current.showInnerVoice,
          showThinking:
            typeof values["conversation.show_thinking"] === "boolean"
              ? values["conversation.show_thinking"]
              : current.showThinking,
          showToolCalls:
            typeof values["conversation.show_tool_calls"] === "boolean"
              ? values["conversation.show_tool_calls"]
              : current.showToolCalls,
          showTokenUsage:
            typeof values["conversation.show_token_usage"] === "boolean"
              ? values["conversation.show_token_usage"]
              : current.showTokenUsage,
          personaMode:
            typeof values["persona.presence"] === "string"
              ? (values[
                  "persona.presence"
                ] as AppearanceDisplaySettings["personaMode"])
              : current.personaMode,
          composerSize:
            typeof values["appearance.composer_size"] === "string"
              ? (values[
                  "appearance.composer_size"
                ] as AppearanceDisplaySettings["composerSize"])
              : current.composerSize,
          sidebarMaterial:
            typeof values["appearance.sidebar_material"] === "string"
              ? (values["appearance.sidebar_material"] as SidebarMaterial)
              : current.sidebarMaterial,
        }),
      );
      setCustomThemeCssState(
        cssFromDesignTokens(values["appearance.design_tokens"]),
      );
    };
    const loadEffectiveState = () => {
      const ticket = ++request;
      const revision = themeRevision.current;
      void customizationApi
        .getEffectiveState()
        .then((effective) => {
          if (ticket === request) applyEffectiveState(effective, revision);
        })
        .catch(() => {});
    };
    loadEffectiveState();
    window.addEventListener(CUSTOMIZATION_CHANGED_EVENT, loadEffectiveState);
    return () => {
      cancelled = true;
      window.removeEventListener(
        CUSTOMIZATION_CHANGED_EVENT,
        loadEffectiveState,
      );
    };
  }, []);

  // Apply theme attributes to <html> for global CSS variable overrides.
  useEffect(() => {
    const html = document.documentElement;
    html.dataset.theme = isDark ? "dark" : "light";
    html.dataset.themeMode = themeMode;
    if (isDark) {
      html.classList.add("dark-mode");
    } else {
      html.classList.remove("dark-mode");
    }
  }, [isDark, themeMode]);

  useEffect(() => {
    const html = document.documentElement;
    html.dataset.density = densityMode;
    html.dataset.palette = appearancePalette;
    html.dataset.displayInnerVoice = displaySettingsState.showInnerVoice
      ? "1"
      : "0";
    html.dataset.displayThinking = displaySettingsState.showThinking
      ? "1"
      : "0";
    html.dataset.displayTools = displaySettingsState.showToolCalls ? "1" : "0";
    html.dataset.displayTokenUsage = displaySettingsState.showTokenUsage
      ? "1"
      : "0";
    html.dataset.composerSize = displaySettingsState.composerSize;
    html.dataset.sidebarMaterial = resolveInterfaceMaterial(
      displaySettingsState.sidebarMaterial,
    );
  }, [densityMode, appearancePalette, displaySettingsState]);

  useEffect(() => {
    let styleElement = document.getElementById(
      CUSTOM_STYLE_ID,
    ) as HTMLStyleElement | null;
    if (!customThemeCss.trim()) {
      styleElement?.remove();
      return;
    }
    if (!styleElement) {
      styleElement = document.createElement("style");
      styleElement.id = CUSTOM_STYLE_ID;
      document.head.appendChild(styleElement);
    }
    styleElement.textContent = sanitizeCustomThemeCss(customThemeCss);
  }, [customThemeCss]);

  // Listen to system theme changes when mode is "system"
  useEffect(() => {
    if (themeMode !== "system") return;

    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (e: MediaQueryListEvent) => {
      setIsDark(e.matches);
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [themeMode]);

  const syncCustomization = useCallback(
    async (
      intent: string,
      operations: Array<{ op: "set" | "unset"; path: string; value?: unknown }>,
      notify = true,
    ) => {
      try {
        const [state, profiles, slots] = await Promise.all([
          customizationApi.getState(),
          customizationApi.listProfiles(),
          customizationApi.listSlots(),
        ]);
        let profile = profiles.find(
          (item) => item.id === state.active_profile_id,
        );
        if (!profile) {
          profile = await customizationApi.createProfile({ name: "我的界面" });
          const slot = slots.find((item) => !item.profile_id);
          if (slot) {
            await customizationApi.setSlot(slot.index, {
              profile_id: profile.id,
              label: profile.name,
              icon: profile.icon,
              color: profile.color,
            });
          }
        }
        const preview = await customizationApi.previewPlan({
          target_profile_id: profile.id,
          base_revision: profile.revision,
          intent,
          operations,
        });
        const receipt = preview.requires_confirmation
          ? await customizationApi.confirmPlan(preview.plan.id!)
          : null;
        const result = await customizationApi.applyPlan(
          preview.plan.id!,
          receipt?.token,
        );
        if (result.status === "committed") {
          await customizationApi.activateProfile(profile.id);
          if (notify) notifyCustomizationChanged();
          return true;
        }
      } catch {
        // Legacy local projection remains usable if the control plane is offline.
      }
      return false;
    },
    [],
  );

  const setThemeMode = useCallback(
    (mode: ThemeMode) => {
      const revision = ++themeRevision.current;
      themeSaving.current = true;
      setThemeModeState(mode);
      setIsDark(resolveIsDark(mode));
      try {
        localStorage.setItem(STORAGE_KEY, mode);
      } catch {
        // ignore
      }
      // Keep both persistence paths ordered while the UI immediately shows the
      // newest choice. Background profile reads must not undo an in-flight save.
      themeWrites.current = themeWrites.current.then(async () => {
        try {
          await settingsApi.updateAppearanceSettings({ theme_mode: mode });
        } catch {
          // The local preference and customization store remain independent fallbacks.
        }
        const saved = await syncCustomization(
          "调整明暗模式",
          [{ op: "set", path: "appearance.theme_mode", value: mode }],
          false,
        );
        if (revision === themeRevision.current) {
          themeSaving.current = false;
          // Invalidate reads started during the save, including failed saves.
          themeRevision.current += 1;
          if (saved) notifyCustomizationChanged();
        }
      });
    },
    [syncCustomization],
  );

  const setDensityMode = useCallback(
    (mode: DensityMode) => {
      setDensityModeState(mode);
      try {
        localStorage.setItem(DENSITY_STORAGE_KEY, mode);
      } catch {
        // ignore
      }
      void settingsApi.updateAppearanceSettings({ density_mode: mode });
      void syncCustomization("调整输入框形态", [
        { op: "set", path: "appearance.composer_size", value: mode },
      ]);
    },
    [syncCustomization],
  );

  const setAppearancePalette = useCallback(
    (palette: AppearancePaletteId) => {
      setAppearancePaletteState(palette);
      try {
        localStorage.setItem(PALETTE_STORAGE_KEY, palette);
      } catch {
        // ignore
      }
      void settingsApi.updateAppearanceSettings({
        appearance_palette: palette,
      });
      void syncCustomization("调整界面配色", [
        { op: "set", path: "appearance.palette", value: palette },
      ]);
    },
    [syncCustomization],
  );

  const setDisplaySettings = useCallback(
    (settings: AppearanceDisplaySettings) => {
      setDisplaySettingsState(settings);
      setDensityModeState(settings.composerSize);
      try {
        localStorage.setItem(DISPLAY_STORAGE_KEY, JSON.stringify(settings));
        localStorage.setItem(DENSITY_STORAGE_KEY, settings.composerSize);
      } catch {
        // ignore
      }
      void settingsApi.updateAppearanceSettings({
        display_settings: settings,
        density_mode: settings.composerSize,
      });
      void syncCustomization("调整界面显示", [
        {
          op: "set",
          path: "conversation.show_inner_voice",
          value: settings.showInnerVoice,
        },
        {
          op: "set",
          path: "conversation.show_thinking",
          value: settings.showThinking,
        },
        {
          op: "set",
          path: "conversation.show_tool_calls",
          value: settings.showToolCalls,
        },
        {
          op: "set",
          path: "conversation.show_token_usage",
          value: settings.showTokenUsage,
        },
        { op: "set", path: "persona.presence", value: settings.personaMode },
        {
          op: "set",
          path: "appearance.composer_size",
          value: settings.composerSize,
        },
        {
          op: "set",
          path: "appearance.sidebar_material",
          value: settings.sidebarMaterial,
        },
      ]);
    },
    [syncCustomization],
  );

  const setDisplaySetting = useCallback(
    <K extends keyof AppearanceDisplaySettings>(
      key: K,
      value: AppearanceDisplaySettings[K],
    ) => {
      setDisplaySettingsState((current) => {
        const next = { ...current, [key]: value };
        if (key === "composerSize") {
          setDensityModeState(value as DensityMode);
        }
        try {
          localStorage.setItem(DISPLAY_STORAGE_KEY, JSON.stringify(next));
          if (key === "composerSize") {
            localStorage.setItem(DENSITY_STORAGE_KEY, String(value));
          }
        } catch {
          // ignore
        }
        void settingsApi.updateAppearanceSettings({
          display_settings: next,
          ...(key === "composerSize"
            ? { density_mode: value as DensityMode }
            : {}),
        });
        const registryPath: Partial<
          Record<keyof AppearanceDisplaySettings, string>
        > = {
          showInnerVoice: "conversation.show_inner_voice",
          personaMode: "persona.presence",
          showThinking: "conversation.show_thinking",
          showToolCalls: "conversation.show_tool_calls",
          showTokenUsage: "conversation.show_token_usage",
          composerSize: "appearance.composer_size",
          sidebarMaterial: "appearance.sidebar_material",
        };
        const path = registryPath[key];
        if (path) {
          void syncCustomization("调整界面显示", [{ op: "set", path, value }]);
        }
        return next;
      });
    },
    [syncCustomization],
  );

  const setCustomThemeCss = useCallback(
    (css: string) => {
      const safeCss = sanitizeCustomThemeCss(css);
      setCustomThemeCssState(safeCss);
      setAppearancePaletteState(
        safeCss.trim() ? "custom" : DEFAULT_APPEARANCE_PALETTE,
      );
      try {
        if (safeCss.trim()) {
          localStorage.setItem(CUSTOM_CSS_STORAGE_KEY, safeCss);
        } else {
          localStorage.removeItem(CUSTOM_CSS_STORAGE_KEY);
        }
      } catch {
        // ignore
      }
      void settingsApi.updateAppearanceSettings({ custom_theme_css: safeCss });
      const tokens = Object.fromEntries(
        Array.from(safeCss.matchAll(SAFE_THEME_TOKEN)).map((match) => [
          match[1].toLowerCase(),
          match[2].trim(),
        ]),
      );
      void syncCustomization("调整安全设计令牌", [
        { op: "set", path: "appearance.design_tokens", value: tokens },
        {
          op: "set",
          path: "appearance.palette",
          value: safeCss.trim() ? "custom" : DEFAULT_APPEARANCE_PALETTE,
        },
      ]);
    },
    [syncCustomization],
  );

  const toggleTheme = useCallback(() => {
    setThemeMode(isDark ? "light" : "dark");
  }, [isDark, setThemeMode]);

  return (
    <ThemeContext.Provider
      value={{
        themeMode,
        isDark,
        densityMode,
        appearancePalette,
        displaySettings: displaySettingsState,
        customThemeCss,
        setThemeMode,
        setDensityMode,
        setAppearancePalette,
        setDisplaySetting,
        setDisplaySettings,
        setCustomThemeCss,
        toggleTheme,
      }}
    >
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
