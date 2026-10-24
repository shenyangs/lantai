import { describe, expect, it } from "vitest";
import tokensCss from "../styles/base/tokens.css?raw";
import { getLantaiAntdTheme } from "./antdTheme";
import {
  DEFAULT_APPEARANCE_PALETTE,
  appearancePalettes,
  defaultCustomThemeCss,
  getAppearancePalette,
} from "./appearance";

describe("appearance defaults", () => {
  it("keeps graphite first and uses it as the shared fresh-install fallback", () => {
    expect(DEFAULT_APPEARANCE_PALETTE).toBe("mist");
    expect(appearancePalettes.slice(0, 3).map((palette) => palette.id)).toEqual(
      ["mist", "pine", "lantai"],
    );
    expect(getAppearancePalette("ink").id).toBe("mist");
    expect(getLantaiAntdTheme(false).token?.colorPrimary).toBe("#69717d");
    expect(defaultCustomThemeCss).toContain("--lt-accent: #69717d;");
  });

  it("changes only the accent across presets while keeping workspace materials neutral", () => {
    const presets = appearancePalettes.filter(
      (palette) => palette.id !== "custom",
    );
    for (const mode of ["light", "dark"] as const) {
      const baseline = presets[0].preview[mode];
      for (const palette of presets.slice(1)) {
        const { accent: _baselineAccent, ...baselineMaterials } = baseline;
        const { accent: _paletteAccent, ...paletteMaterials } =
          palette.preview[mode];
        expect(paletteMaterials).toEqual(baselineMaterials);
      }
    }
  });

  it("keeps the runtime palette cascade on one neutral material stack", () => {
    const lightContract = [
      "--lt-bg: #fcfcfb;",
      "--lt-bg-layout: #f7f7f4;",
      "--lt-surface: #fffdfb;",
      "--lt-sidebar-bg: #e9e7e2;",
      "--lt-chat-bg: #fffdfb;",
      "--lt-chat-user-bg: #464b49;",
      "--lt-chat-user-text: #f7f5f0;",
    ];
    const darkContract = [
      "--lt-bg: #151515;",
      "--lt-bg-layout: #141414;",
      "--lt-surface: #1b1b1b;",
      "--lt-sidebar-bg: #242424;",
      "--lt-chat-bg: #141414;",
      "--lt-chat-user-bg: #535b58;",
      "--lt-chat-user-text: #f7f5f0;",
    ];

    expect(tokensCss).toContain('html[data-palette="pine"]');
    expect(tokensCss).toContain('html[data-palette="violet"]');
    expect(tokensCss).toContain('html.dark-mode[data-palette="pine"]');
    lightContract.forEach((token) => expect(tokensCss).toContain(token));
    darkContract.forEach((token) => expect(tokensCss).toContain(token));
  });
});
