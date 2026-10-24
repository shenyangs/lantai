import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readText = (relativePath: string) =>
  readFileSync(new URL(relativePath, import.meta.url), "utf8");

describe("semantic material surface contracts", () => {
  it("defines semantic surfaces without letting sidebar presets rewrite them", () => {
    const tokensCss = readText("./base/tokens.css");
    const sidebarMaterialRules = tokensCss.slice(
      tokensCss.indexOf('html[data-sidebar-material="light-glass"]'),
      tokensCss.indexOf('html[data-density="compact"]'),
    );

    for (const token of [
      "--lt-float-surface",
      "--lt-float-blur",
      "--lt-float-saturate",
      "--lt-occluder-surface",
      "--lt-occluder-blur",
      "--lt-occluder-saturate",
      "--lt-dialog-surface",
      "--lt-dialog-blur",
      "--lt-dialog-saturate",
    ]) {
      expect(
        tokensCss.match(new RegExp(`${token}:`, "g"))?.length ?? 0,
      ).toBeGreaterThanOrEqual(2);
    }

    expect(sidebarMaterialRules).not.toMatch(
      /--lt-glass-(?:surface|toolbar-bg|control-bg|control-hover-bg|border|blur|saturate):/,
    );
  });

  it("uses the occluder tier to keep transcript text illegible beneath the composer", () => {
    const tokensCss = readText("./base/tokens.css");
    const chatCss = readText("../pages/Chat/index.module.less");
    const composerMask = tokensCss.match(
      /--lt-quiet-composer-mask:\s*linear-gradient\(([\s\S]*?)\);/,
    )?.[1];
    const composerFooter = chatCss.match(
      /\.lantai-chat-anywhere-input,\s*\.lantai-chat-anywhere-default-footer \{([\s\S]*?)\}/,
    )?.[1];
    const sender = chatCss.match(
      /\.lantai-sender,\s*\.lantai-sender-content \{([\s\S]*?)\}/,
    )?.[1];

    expect(composerMask).toBeTruthy();
    expect(composerMask).not.toMatch(/var\(--lt-surface\) 0%/);
    expect(composerFooter).toContain("blur(var(--lt-occluder-blur))");
    expect(composerFooter).toContain("saturate(var(--lt-occluder-saturate))");
    expect(sender).toContain("background: var(--lt-occluder-surface)");
    expect(sender).toContain("blur(var(--lt-occluder-blur))");
  });

  it("maps dialogs to opaque surfaces and transient menus to the float tier", () => {
    const antdCss = readText("./vendors/antd-flat.css");
    const overlaysCss = readText("./components/overlays.css");

    expect(antdCss).toContain(
      "background: var(--lt-dialog-surface) !important;",
    );
    expect(antdCss).toContain("blur(var(--lt-dialog-blur))");
    expect(antdCss).toContain(
      "background: var(--lt-float-surface) !important;",
    );
    expect(antdCss).toContain("blur(var(--lt-float-blur))");
    expect(overlaysCss).not.toContain("var(--lt-glass-surface-strong)");

    for (const relativePath of [
      "../components/ContextMenu/index.module.less",
      "../layouts/index.module.less",
      "../pages/Chat/ModelSelector/index.module.less",
    ]) {
      expect(readText(relativePath)).toContain("var(--lt-float-surface)");
    }
  });

  it("provides solid, blur-free fallbacks for reduced transparency and forced colors", () => {
    const tokensCss = readText("./base/tokens.css");
    const accessibilityFallbacks = tokensCss.slice(
      tokensCss.indexOf("@supports not ("),
    );

    for (const declaration of [
      "--lt-float-surface: var(--lt-surface-elevated);",
      "--lt-float-blur: 0px;",
      "--lt-occluder-surface: var(--lt-surface-elevated);",
      "--lt-occluder-blur: 0px;",
      "--lt-dialog-surface: var(--lt-surface-elevated);",
      "--lt-dialog-blur: 0px;",
      "--lt-quiet-composer-mask: var(--lt-surface);",
    ]) {
      expect(accessibilityFallbacks).toContain(declaration);
    }

    expect(accessibilityFallbacks).toContain("@media (forced-colors: active)");
    expect(accessibilityFallbacks).toContain("--lt-float-surface: Canvas;");
    expect(accessibilityFallbacks).toContain("--lt-dialog-surface: Canvas;");
  });

  it("keeps content-bearing blur effects on semantic tokens", () => {
    for (const relativePath of [
      "../pages/Chat/index.module.less",
      "../pages/Chat/ModelSelector/index.module.less",
      "../pages/Settings/AgentStats/index.module.less",
      "../pages/Chat/components/ImageGenerationToolCard.module.less",
    ]) {
      const css = readText(relativePath);
      expect(css).not.toMatch(/backdrop-filter:\s*blur\(\d+(?:\.\d+)?px\)/);
      expect(css).not.toMatch(
        /-webkit-backdrop-filter:\s*blur\(\d+(?:\.\d+)?px\)/,
      );
    }
  });
});
