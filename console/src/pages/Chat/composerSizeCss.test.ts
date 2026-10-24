import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readText = (relativePath: string) =>
  readFileSync(new URL(relativePath, import.meta.url), "utf8");

const composerSizeBlock = (css: string, size: string) =>
  css.match(
    new RegExp(
      `html\\[data-composer-size="${size}"\\] \\{[\\s\\S]*?\\n\\}`,
    ),
  )?.[0];

describe("composer size CSS contract", () => {
  it("gives every display setting a usable editor area", () => {
    const tokensCss = readText("../../styles/base/tokens.css");

    expect(composerSizeBlock(tokensCss, "compact")).toContain(
      "--lt-composer-min-height: 120px;",
    );
    expect(composerSizeBlock(tokensCss, "compact")).toContain(
      "--lt-composer-editor-min-height: 56px;",
    );
    expect(composerSizeBlock(tokensCss, "comfortable")).toContain(
      "--lt-composer-min-height: 148px;",
    );
    expect(composerSizeBlock(tokensCss, "comfortable")).toContain(
      "--lt-composer-editor-min-height: 72px;",
    );
    expect(composerSizeBlock(tokensCss, "spacious")).toContain(
      "--lt-composer-min-height: 172px;",
    );
    expect(composerSizeBlock(tokensCss, "spacious")).toContain(
      "--lt-composer-editor-min-height: 96px;",
    );
  });

  it("uses the shared size tokens in regular, empty and coding chat states", () => {
    const chatModuleCss = readText("./index.module.less");

    expect(chatModuleCss).toContain(
      "padding: var(--lt-composer-content-padding, 16px 16px 52px) !important;",
    );
    expect(chatModuleCss).toContain(
      "min-height: var(--lt-composer-editor-min-height, 72px) !important;",
    );
    expect(chatModuleCss).toContain(
      "min-height: var(--lt-composer-min-height) !important;",
    );
    expect(chatModuleCss).toContain(
      "min-height: var(--lt-composer-editor-min-height) !important;",
    );
    expect(chatModuleCss).not.toContain("--lt-composer-min-height: 94px;");
  });
});
