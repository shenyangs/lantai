export const lantaiFontFamily =
  '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", Inter, "Microsoft YaHei", ui-sans-serif, system-ui, sans-serif';

export const lantaiThemeTokens = {
  light: {
    colorPrimary: "#4b6581",
    colorBgBase: "#f5f5f7",
    colorBgContainer: "#ffffff",
    colorBgElevated: "#ffffff",
    colorText: "#1d1d1f",
    colorTextSecondary: "#6e6e73",
    colorTextTertiary: "#98989d",
    colorBorder: "rgba(29, 29, 31, 0.12)",
    colorBorderSecondary: "rgba(29, 29, 31, 0.07)",
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    fontFamily: lantaiFontFamily,
  },
  dark: {
    colorPrimary: "#95b9d9",
    colorBgBase: "#0f1013",
    colorBgContainer: "#17191d",
    colorBgElevated: "#262a31",
    colorBgLayout: "#0f1013",
    colorFill: "rgba(255, 255, 255, 0.1)",
    colorFillSecondary: "rgba(255, 255, 255, 0.07)",
    colorFillTertiary: "rgba(255, 255, 255, 0.045)",
    colorText: "rgba(245, 245, 245, 0.92)",
    colorTextSecondary: "rgba(225, 225, 225, 0.66)",
    colorTextTertiary: "rgba(225, 225, 225, 0.44)",
    colorBorder: "rgba(255, 255, 255, 0.13)",
    colorBorderSecondary: "rgba(255, 255, 255, 0.08)",
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    fontFamily: lantaiFontFamily,
  },
} as const;

export type LantaiResolvedTheme = keyof typeof lantaiThemeTokens;
