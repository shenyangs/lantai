import { theme as antdTheme, type ThemeConfig } from "antd";
import {
  DEFAULT_APPEARANCE_PALETTE,
  getAppearancePalette,
  type AppearancePaletteId,
} from "./appearance";
import { lantaiThemeTokens } from "./tokens";

export function getLantaiAntdTheme(
  isDark: boolean,
  baseTheme?: ThemeConfig,
  appearancePalette: AppearancePaletteId = DEFAULT_APPEARANCE_PALETTE,
): ThemeConfig {
  const palette = getAppearancePalette(appearancePalette);
  const accent = isDark ? palette.darkAccent ?? palette.accent : palette.accent;
  const baseTokens = isDark ? lantaiThemeTokens.dark : lantaiThemeTokens.light;

  return {
    ...baseTheme,
    algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      ...baseTokens,
      ...(baseTheme?.token ?? {}),
      colorPrimary: accent,
      colorLink: accent,
    },
  };
}
